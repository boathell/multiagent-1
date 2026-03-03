from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.models import PipelineState, Stage, StageResult, StageStatus
from app.orchestrator import Orchestrator
from app.quality_gate import QualityGate
from app.store import SQLiteStore


class FakePlaneClient:
    def __init__(self) -> None:
        self.state_updates: list[tuple[str, str, str]] = []
        self.comments: list[tuple[str, str, str]] = []

    async def update_work_item_state(
        self, project_id: str, issue_id: str, state_name: str, state_map=None
    ) -> None:
        self.state_updates.append((project_id, issue_id, state_name))

    async def add_comment(self, project_id: str, issue_id: str, comment: str) -> None:
        self.comments.append((project_id, issue_id, comment))


class FakeGitHubClient:
    class PR:
        def __init__(self, branch: str, pr_url: str) -> None:
            self.branch = branch
            self.pr_url = pr_url

    def create_branch_commit_and_pr(
        self,
        issue_id: str,
        title: str,
        body: str,
        local_path: str,
        base_branch: str,
        repo_url: str = "",
    ):
        return self.PR(branch=f"plane/{issue_id}", pr_url=f"https://example.com/pr/{issue_id}")


class ScriptedAgent:
    def __init__(self, scripted: dict[Stage, list[StageResult]] | None = None) -> None:
        self.scripted = scripted or {}

    async def run_stage(self, stage: Stage, context):
        queue = self.scripted.get(stage, [])
        if queue:
            return queue.pop(0)
        return StageResult(status=StageStatus.SUCCESS, summary=f"{stage.value} ok")


@pytest.mark.asyncio
async def test_happy_path(make_config, tmp_path: Path):
    config = make_config(project_id="p1")
    store = SQLiteStore(str(tmp_path / "db1.sqlite"))
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=FakePlaneClient(),
        github_client=FakeGitHubClient(),
        agent_adapter=ScriptedAgent(),
        quality_gate=QualityGate(),
    )

    await orch.process_issue(issue_id="1001", project_id="p1", title="happy", force=False)

    issue = store.get_issue("1001")
    assert issue is not None
    assert issue["state"] == PipelineState.DONE.value
    assert issue["pr_url"].endswith("/1001")


@pytest.mark.asyncio
async def test_coding_retry_then_success(make_config, tmp_path: Path):
    config = make_config(project_id="p1")
    store = SQLiteStore(str(tmp_path / "db2.sqlite"))
    plane = FakePlaneClient()
    agent = ScriptedAgent(
        {
            Stage.CODING: [
                StageResult(status=StageStatus.FAILED, summary="fail once"),
                StageResult(status=StageStatus.SUCCESS, summary="then pass"),
            ]
        }
    )
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=plane,
        github_client=FakeGitHubClient(),
        agent_adapter=agent,
        quality_gate=QualityGate(),
    )

    await orch.process_issue(issue_id="1002", project_id="p1", title="retry", force=False)

    issue = store.get_issue("1002")
    assert issue is not None
    assert issue["state"] == PipelineState.DONE.value

    trace = store.get_trace("1002")
    assert any("Attempt 1/2 failed" in t["message"] for t in trace)
    assert any("status=retry" in comment for _, _, comment in plane.comments)


@pytest.mark.asyncio
async def test_review_needs_changes_once_then_done(make_config, tmp_path: Path):
    config = make_config(project_id="p1")
    store = SQLiteStore(str(tmp_path / "db3.sqlite"))
    agent = ScriptedAgent(
        {
            Stage.REVIEW: [
                StageResult(status=StageStatus.NEEDS_CHANGES, summary="need updates"),
                StageResult(status=StageStatus.SUCCESS, summary="approved"),
            ]
        }
    )
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=FakePlaneClient(),
        github_client=FakeGitHubClient(),
        agent_adapter=agent,
        quality_gate=QualityGate(),
    )

    await orch.process_issue(issue_id="1003", project_id="p1", title="loop", force=False)

    issue = store.get_issue("1003")
    assert issue is not None
    assert issue["state"] == PipelineState.DONE.value
    assert issue["review_loops"] == 1


@pytest.mark.asyncio
async def test_quality_gate_failure_blocks_issue(make_config, tmp_path: Path):
    config = make_config(project_id="p1", checks=["python -c 'import sys; sys.exit(1)'"])
    store = SQLiteStore(str(tmp_path / "db4.sqlite"))
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=FakePlaneClient(),
        github_client=FakeGitHubClient(),
        agent_adapter=ScriptedAgent(),
        quality_gate=QualityGate(),
    )

    await orch.process_issue(issue_id="1004", project_id="p1", title="gate", force=False)

    issue = store.get_issue("1004")
    assert issue is not None
    assert issue["state"] == PipelineState.BLOCKED.value


@pytest.mark.asyncio
async def test_webhook_idempotency(make_config, tmp_path: Path):
    config = make_config(project_id="p1")
    store = SQLiteStore(str(tmp_path / "db5.sqlite"))
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=FakePlaneClient(),
        github_client=FakeGitHubClient(),
        agent_adapter=ScriptedAgent(),
        quality_gate=QualityGate(),
    )

    payload = {
        "event": "work_item.created",
        "event_id": "evt-123",
        "data": {
            "work_item": {
                "id": "1005",
                "project_id": "p1",
                "name": "idempotent",
                "state_name": "Todo",
                "updated_at": "2026-03-03T00:00:00Z",
            }
        },
    }

    first = await orch.handle_webhook(payload)
    second = await orch.handle_webhook(payload)

    assert first["status"] == "accepted"
    assert second["status"] == "duplicate"


def test_extract_event_id_handles_non_dict_data():
    payload = {"event": "ping", "data": "plain-text-payload"}
    event_id = Orchestrator.extract_event_id(payload)
    assert event_id.startswith("ping:unknown:")


def test_build_stage_comment_includes_chinese_and_machine_fields():
    result = StageResult(
        status=StageStatus.SUCCESS,
        summary="编码完成",
        artifacts={"pr_url": "https://example.com/pr/1007"},
    )
    comment = Orchestrator._build_stage_comment(Stage.CODING, result)
    assert "[编排器]" in comment
    assert "阶段：编码" in comment
    assert "状态：成功" in comment
    assert "PR：https://example.com/pr/1007" in comment
    assert "[ORCH]" in comment
    assert "stage=coding" in comment
    assert "status=success" in comment


def test_build_stage_comment_failed_contains_reason():
    result = StageResult(status=StageStatus.FAILED, summary="gemini timeout after 300s.")
    comment = Orchestrator._build_stage_comment(Stage.REVIEW, result)
    assert "状态：失败" in comment
    assert "原因：执行超时（300s）" in comment
    assert "status=failed" in comment


def test_build_retry_and_blocked_comment_templates():
    retry_comment = Orchestrator._build_retry_comment(
        Stage.CODING,
        attempt=1,
        total_attempts=2,
        summary="quality gate failed after coding stage.",
    )
    assert "状态：重试中" in retry_comment
    assert "原因：质量门禁未通过" in retry_comment
    assert "status=retry" in retry_comment

    blocked_comment = Orchestrator._build_blocked_comment(
        Stage.REVIEW,
        StageResult(status=StageStatus.FAILED, summary="review loop exceeded max limit."),
    )
    assert "状态：已阻塞" in blocked_comment
    assert "原因：审查回流超过上限" in blocked_comment
    assert "status=blocked_notice" in blocked_comment


@pytest.mark.asyncio
async def test_retry_issue_serializes_same_issue(make_config, tmp_path: Path):
    class SlowAgent:
        def __init__(self) -> None:
            self.running = 0
            self.max_running = 0

        async def run_stage(self, stage: Stage, context):
            self.running += 1
            self.max_running = max(self.max_running, self.running)
            await asyncio.sleep(0.05)
            self.running -= 1
            return StageResult(status=StageStatus.SUCCESS, summary=f"{stage.value} ok")

    config = make_config(project_id="p1")
    store = SQLiteStore(str(tmp_path / "db6.sqlite"))
    agent = SlowAgent()
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=FakePlaneClient(),
        github_client=FakeGitHubClient(),
        agent_adapter=agent,
        quality_gate=QualityGate(),
    )
    store.upsert_issue("1006", "p1", "concurrent retry", PipelineState.BLOCKED.value)
    store.update_issue_fields("1006", last_stage=Stage.REVIEW.value)

    await asyncio.gather(orch.retry_issue("1006"), orch.retry_issue("1006"))

    assert agent.max_running == 1
