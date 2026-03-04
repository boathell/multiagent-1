from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.models import FailureClass, PipelineState, Stage
from app.orchestrator import Orchestrator
from app.quality_gate import QualityGate
from app.store import SQLiteStore
from .helpers import FakeGitHubClient, FakePlaneClient, ScriptedAgent


def _run_git(cwd: Path, args: list[str]) -> None:
    subprocess.run(
        args,
        cwd=str(cwd),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _init_repo(path: Path) -> None:
    _run_git(path, ["git", "init", "-b", "main"])
    _run_git(path, ["git", "config", "user.name", "tester"])
    _run_git(path, ["git", "config", "user.email", "tester@example.com"])
    (path / "README.md").write_text("# demo\n", encoding="utf-8")
    _run_git(path, ["git", "add", "README.md"])
    _run_git(path, ["git", "commit", "-m", "init"])


@pytest.mark.asyncio
async def test_issue_uses_dedicated_worktree_path(make_config, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    worktree_root = tmp_path / "wt-root"

    class RecordingAgent(ScriptedAgent):
        def __init__(self) -> None:
            super().__init__()
            self.local_paths: list[str] = []

        async def run_stage(self, stage: Stage, context):
            self.local_paths.append(context.local_path)
            return await super().run_stage(stage, context)

    config = make_config(
        project_id="p1",
        issue_worktree_enabled=True,
        issue_worktree_root=str(worktree_root),
    )
    config.projects["p1"].local_path = str(repo)

    store = SQLiteStore(str(tmp_path / "workspace.sqlite"))
    agent = RecordingAgent()
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=FakePlaneClient(),
        github_client=FakeGitHubClient(),
        agent_adapter=agent,
        quality_gate=QualityGate(),
    )

    await orch.process_issue(issue_id="ws-1", project_id="p1", title="workspace one", force=False)
    await orch.process_issue(issue_id="ws-2", project_id="p1", title="workspace two", force=False)

    issue_1 = store.get_issue("ws-1")
    issue_2 = store.get_issue("ws-2")
    assert issue_1 is not None
    assert issue_2 is not None
    assert issue_1["workspace_path"] != ""
    assert issue_2["workspace_path"] != ""
    assert issue_1["workspace_path"] != issue_2["workspace_path"]
    assert Path(issue_1["workspace_path"]).is_dir()
    assert Path(issue_2["workspace_path"]).is_dir()
    assert issue_1["workspace_path"] != str(repo)
    assert issue_2["workspace_path"] != str(repo)
    assert issue_1["workspace_path"] in agent.local_paths
    assert issue_2["workspace_path"] in agent.local_paths


@pytest.mark.asyncio
async def test_non_git_project_blocks_when_worktree_enabled(make_config, tmp_path: Path):
    config = make_config(
        project_id="p1",
        issue_worktree_enabled=True,
        issue_worktree_root=str(tmp_path / "wt-root"),
    )
    store = SQLiteStore(str(tmp_path / "workspace-fail.sqlite"))
    plane = FakePlaneClient()
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=plane,
        github_client=FakeGitHubClient(),
        agent_adapter=ScriptedAgent(),
        quality_gate=QualityGate(),
    )

    await orch.process_issue(issue_id="ws-fail", project_id="p1", title="workspace fail", force=False)

    issue = store.get_issue("ws-fail")
    assert issue is not None
    assert issue["state"] == PipelineState.BLOCKED.value
    assert issue["failure_class"] == FailureClass.ENV_ISSUE.value
    assert issue["handoff_reason"] == "workspace_prepare_failed"
    trace = store.get_trace("ws-fail")
    assert any(t["stage"] == "workspace" and t["status"] == "failed" for t in trace)
    assert any("git worktree" in comment.lower() for _, _, comment in plane.comments)


@pytest.mark.asyncio
async def test_cleanup_removes_stale_worktree_after_terminal_state(make_config, tmp_path: Path):
    repo = tmp_path / "repo-cleanup"
    repo.mkdir()
    _init_repo(repo)

    worktree_root = tmp_path / "wt-cleanup"
    config = make_config(
        project_id="p1",
        issue_worktree_enabled=True,
        issue_worktree_root=str(worktree_root),
        issue_worktree_cleanup_enabled=True,
        issue_worktree_retention_hours=0,
    )
    config.projects["p1"].local_path = str(repo)

    store = SQLiteStore(str(tmp_path / "workspace-cleanup.sqlite"))
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=FakePlaneClient(),
        github_client=FakeGitHubClient(),
        agent_adapter=ScriptedAgent(),
        quality_gate=QualityGate(),
    )

    await orch.process_issue(issue_id="cleanup-1", project_id="p1", title="cleanup", force=False)

    issue = store.get_issue("cleanup-1")
    assert issue is not None
    assert issue["state"] == PipelineState.DONE.value
    assert issue["workspace_path"] != ""
    assert not Path(issue["workspace_path"]).exists()


@pytest.mark.asyncio
async def test_cleanup_skips_active_workspace(make_config, tmp_path: Path):
    repo = tmp_path / "repo-active"
    repo.mkdir()
    _init_repo(repo)

    worktree_root = tmp_path / "wt-active"
    config = make_config(
        project_id="p1",
        issue_worktree_enabled=True,
        issue_worktree_root=str(worktree_root),
        issue_worktree_cleanup_enabled=True,
        issue_worktree_retention_hours=0,
    )
    config.projects["p1"].local_path = str(repo)

    store = SQLiteStore(str(tmp_path / "workspace-active.sqlite"))
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=FakePlaneClient(),
        github_client=FakeGitHubClient(),
        agent_adapter=ScriptedAgent(),
        quality_gate=QualityGate(),
    )

    active_workspace = orch.workspace_manager.prepare_workspace(
        project_local_path=str(repo),
        project_id="p1",
        issue_id="active-keep",
        base_branch="main",
    )
    store.upsert_issue("active-keep", "p1", "active keep", PipelineState.CODING.value)
    store.update_issue_fields("active-keep", workspace_path=active_workspace)

    await orch.process_issue(issue_id="cleanup-trigger", project_id="p1", title="trigger", force=False)

    assert Path(active_workspace).exists()
