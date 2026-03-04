from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.models import PipelineState, Stage, StageResult, StageStatus
from app.orchestrator import Orchestrator
from app.quality_gate import QualityGate
from app.store import SQLiteStore
from .helpers import FakeGitHubClient, FakePlaneClient, ScriptedAgent


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
async def test_code_file_line_limit_blocks_coding_stage(make_config, tmp_path: Path):
    config = make_config(project_id="p1", max_code_file_lines=1000)
    long_file = tmp_path / "too_long.py"
    long_file.write_text("\n".join("x=1" for _ in range(1001)), encoding="utf-8")

    store = SQLiteStore(str(tmp_path / "db4-lines.sqlite"))
    plane = FakePlaneClient()
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=plane,
        github_client=FakeGitHubClient(),
        agent_adapter=ScriptedAgent(),
        quality_gate=QualityGate(),
    )

    await orch.process_issue(issue_id="1004-lines", project_id="p1", title="line-limit", force=False)

    issue = store.get_issue("1004-lines")
    assert issue is not None
    assert issue["state"] == PipelineState.BLOCKED.value
    trace = store.get_trace("1004-lines")
    coding_trace = next((t for t in trace if t["stage"] == "coding" and t["status"] == "failed"), None)
    assert coding_trace is not None
    assert "line_limit_violations" in coding_trace["metadata"]
    assert any("超限文件" in comment for _, _, comment in plane.comments)


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


@pytest.mark.asyncio
async def test_handle_webhook_limits_global_issue_concurrency(make_config, tmp_path: Path):
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

    config = make_config(project_id="p1", issue_max_concurrency=2)
    store = SQLiteStore(str(tmp_path / "db7.sqlite"))
    agent = SlowAgent()
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=FakePlaneClient(),
        github_client=FakeGitHubClient(),
        agent_adapter=agent,
        quality_gate=QualityGate(),
    )

    async def send_event(idx: int) -> dict[str, str]:
        payload = {
            "event": "work_item.created",
            "event_id": f"evt-limit-{idx}",
            "data": {
                "work_item": {
                    "id": f"limit-{idx}",
                    "project_id": "p1",
                    "name": f"limit-{idx}",
                    "state_name": "Todo",
                    "updated_at": f"2026-03-03T00:00:{idx:02d}Z",
                }
            },
        }
        return await orch.handle_webhook(payload)

    results = await asyncio.gather(*(send_event(i) for i in range(1, 6)))

    assert all(item["status"] == "accepted" for item in results)
    assert agent.max_running == 2
