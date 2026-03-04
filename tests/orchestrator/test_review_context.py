from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.models import PipelineState, Stage, StageResult, StageStatus
from app.orchestrator import Orchestrator
from app.quality_gate import QualityGate
from app.store import SQLiteStore
from .helpers import FakeGitHubClient, FakePlaneClient


@pytest.mark.asyncio
async def test_review_stage_collects_git_diff_context(make_config, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(cmd: list[str]) -> None:
        subprocess.run(
            cmd,
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    run(["git", "init", "-b", "main"])
    run(["git", "config", "user.name", "tester"])
    run(["git", "config", "user.email", "tester@example.com"])
    (repo / "demo.txt").write_text("v1\n", encoding="utf-8")
    run(["git", "add", "demo.txt"])
    run(["git", "commit", "-m", "init"])
    run(["git", "checkout", "-b", "plane/1009-demo"])
    (repo / "demo.txt").write_text("v1\nv2\n", encoding="utf-8")
    run(["git", "add", "demo.txt"])
    run(["git", "commit", "-m", "update"])

    config = make_config(project_id="p1")
    config.projects["p1"].local_path = str(repo)

    class CaptureReviewAgent:
        def __init__(self) -> None:
            self.captured = None

        async def run_stage(self, stage: Stage, context):
            if stage == Stage.REVIEW:
                self.captured = context
            return StageResult(status=StageStatus.SUCCESS, summary=f"{stage.value} ok")

    agent = CaptureReviewAgent()
    store = SQLiteStore(str(tmp_path / "db-review.sqlite"))
    store.upsert_issue("1009", "p1", "review ctx", PipelineState.REVIEW.value)
    store.update_issue_fields("1009", branch="plane/1009-demo")

    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=FakePlaneClient(),
        github_client=FakeGitHubClient(),
        agent_adapter=agent,
        quality_gate=QualityGate(),
    )

    result = await orch._run_stage_with_retry(
        stage=Stage.REVIEW,
        issue_id="1009",
        project_id="p1",
        title="review ctx",
        project=config.projects["p1"],
    )
    assert result.status == StageStatus.SUCCESS
    assert agent.captured is not None
    assert "demo.txt" in agent.captured.metadata["review_changed_files"]
    assert "v2" in agent.captured.metadata["review_diff"]
    assert "demo.txt" in agent.captured.metadata["review_diff_files_included"]
