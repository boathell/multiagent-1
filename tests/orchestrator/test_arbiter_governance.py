from __future__ import annotations

from pathlib import Path

import pytest

from app.models import PipelineState, Stage, StageResult, StageStatus
from app.orchestrator import Orchestrator
from app.quality_gate import QualityGate
from app.store import SQLiteStore
from .helpers import FakeGitHubClient, FakePlaneClient, ScriptedAgent


@pytest.mark.asyncio
async def test_review_over_limit_triggers_arbiter_continue(make_config, tmp_path: Path):
    config = make_config(
        project_id="p1",
        agent_config={"review": {"max_loops": 1, "arbiter_max_loops": 1}},
    )
    store = SQLiteStore(str(tmp_path / "db3-arbiter-continue.sqlite"))
    plane = FakePlaneClient()

    class ArbiterContinueAgent:
        def __init__(self) -> None:
            self.review_calls = 0

        async def run_stage(self, stage: Stage, context):
            if stage == Stage.DESIGN and context.metadata.get("design_mode") == "review_arbiter":
                return StageResult(
                    status=StageStatus.SUCCESS,
                    summary="arbiter continue",
                    artifacts={
                        "stdout": (
                            "CONTINUE_CODING\n"
                            "DIAGNOSIS: GEMINI_ISSUE\n"
                            "REASON: review 出现 timeout 与 503，先继续 coding 缩小 diff。\n"
                            "ACTIONS: 拆分变更后重提审查。"
                        )
                    },
                )
            if stage == Stage.REVIEW:
                self.review_calls += 1
                if self.review_calls == 1:
                    return StageResult(status=StageStatus.NEEDS_CHANGES, summary="gemini timeout after 300s.")
                return StageResult(status=StageStatus.SUCCESS, summary="approved")
            return StageResult(status=StageStatus.SUCCESS, summary=f"{stage.value} ok")

    agent = ArbiterContinueAgent()
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=plane,
        github_client=FakeGitHubClient(),
        agent_adapter=agent,
        quality_gate=QualityGate(),
    )

    store.upsert_issue("1003-a", "p1", "arbiter continue", PipelineState.REVIEW.value, description="desc")
    store.update_issue_fields("1003-a", review_loops=1)

    await orch.process_issue(issue_id="1003-a", project_id="p1", title="arbiter continue", force=True)

    issue = store.get_issue("1003-a")
    assert issue is not None
    assert issue["state"] == PipelineState.DONE.value
    assert issue["arbiter_loops"] == 1
    assert any("阶段：设计仲裁" in comment and "结论：继续编码" in comment for _, _, comment in plane.comments)


@pytest.mark.asyncio
async def test_review_over_limit_triggers_arbiter_stop(make_config, tmp_path: Path):
    config = make_config(
        project_id="p1",
        agent_config={"review": {"max_loops": 1, "arbiter_max_loops": 1}},
    )
    store = SQLiteStore(str(tmp_path / "db3-arbiter-stop.sqlite"))
    plane = FakePlaneClient()

    class ArbiterStopAgent:
        async def run_stage(self, stage: Stage, context):
            if stage == Stage.DESIGN and context.metadata.get("design_mode") == "review_arbiter":
                return StageResult(
                    status=StageStatus.SUCCESS,
                    summary="arbiter stop",
                    artifacts={
                        "stdout": (
                            "STOP_REVIEW\n"
                            "DIAGNOSIS: QUALITY_ISSUE\n"
                            "REASON: 代码质量问题未收敛，需要人工介入。\n"
                            "ACTIONS: 停止自动审查。"
                        )
                    },
                )
            if stage == Stage.REVIEW:
                return StageResult(status=StageStatus.NEEDS_CHANGES, summary="needs changes")
            return StageResult(status=StageStatus.SUCCESS, summary=f"{stage.value} ok")

    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=plane,
        github_client=FakeGitHubClient(),
        agent_adapter=ArbiterStopAgent(),
        quality_gate=QualityGate(),
    )

    store.upsert_issue("1003-b", "p1", "arbiter stop", PipelineState.REVIEW.value, description="desc")
    store.update_issue_fields("1003-b", review_loops=1)

    await orch.process_issue(issue_id="1003-b", project_id="p1", title="arbiter stop", force=True)

    issue = store.get_issue("1003-b")
    assert issue is not None
    assert issue["state"] == PipelineState.BLOCKED.value
    assert any("阶段：设计仲裁" in comment and "结论：停止审查" in comment for _, _, comment in plane.comments)


@pytest.mark.asyncio
async def test_review_over_limit_gemini_diagnosis_forces_continue(make_config, tmp_path: Path):
    config = make_config(
        project_id="p1",
        agent_config={"review": {"max_loops": 1, "arbiter_max_loops": 1}},
    )
    store = SQLiteStore(str(tmp_path / "db3-arbiter-gemini-force-continue.sqlite"))
    plane = FakePlaneClient()

    class GeminiIssueAgent:
        def __init__(self) -> None:
            self.review_calls = 0

        async def run_stage(self, stage: Stage, context):
            if stage == Stage.DESIGN and context.metadata.get("design_mode") == "review_arbiter":
                return StageResult(
                    status=StageStatus.SUCCESS,
                    summary="arbiter output inconsistent",
                    artifacts={
                        "stdout": (
                            "STOP_REVIEW\n"
                            "DIAGNOSIS: GEMINI_ISSUE\n"
                            "REASON: 模型服务不稳定。\n"
                            "ACTIONS: 建议继续编码缩小改动。"
                        )
                    },
                )
            if stage == Stage.REVIEW:
                self.review_calls += 1
                if self.review_calls == 1:
                    return StageResult(status=StageStatus.NEEDS_CHANGES, summary="needs changes")
                return StageResult(status=StageStatus.SUCCESS, summary="approved")
            return StageResult(status=StageStatus.SUCCESS, summary=f"{stage.value} ok")

    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=plane,
        github_client=FakeGitHubClient(),
        agent_adapter=GeminiIssueAgent(),
        quality_gate=QualityGate(),
    )

    store.upsert_issue("1003-b2", "p1", "arbiter gemini force continue", PipelineState.REVIEW.value, description="desc")
    store.update_issue_fields("1003-b2", review_loops=1)

    await orch.process_issue(issue_id="1003-b2", project_id="p1", title="arbiter gemini force continue", force=True)

    issue = store.get_issue("1003-b2")
    assert issue is not None
    assert issue["state"] == PipelineState.DONE.value
    assert any("阶段：设计仲裁" in comment and "结论：继续编码" in comment for _, _, comment in plane.comments)


@pytest.mark.asyncio
async def test_review_over_limit_arbiter_failure_falls_back_to_continue_on_gemini_signals(
    make_config,
    tmp_path: Path,
):
    config = make_config(
        project_id="p1",
        agent_config={"review": {"max_loops": 1, "arbiter_max_loops": 1}},
    )
    store = SQLiteStore(str(tmp_path / "db3-arbiter-fallback-continue.sqlite"))
    plane = FakePlaneClient()

    class FallbackContinueAgent:
        def __init__(self) -> None:
            self.review_calls = 0

        async def run_stage(self, stage: Stage, context):
            if stage == Stage.DESIGN and context.metadata.get("design_mode") == "review_arbiter":
                return StageResult(status=StageStatus.FAILED, summary="claude timeout after 300s.")
            if stage == Stage.REVIEW:
                self.review_calls += 1
                if self.review_calls == 1:
                    return StageResult(
                        status=StageStatus.NEEDS_CHANGES,
                        summary="review unstable",
                        artifacts={"stderr": "ApiError: 503 UNAVAILABLE"},
                    )
                return StageResult(status=StageStatus.SUCCESS, summary="approved")
            return StageResult(status=StageStatus.SUCCESS, summary=f"{stage.value} ok")

    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=plane,
        github_client=FakeGitHubClient(),
        agent_adapter=FallbackContinueAgent(),
        quality_gate=QualityGate(),
    )

    store.upsert_issue("1003-d", "p1", "arbiter fallback continue", PipelineState.REVIEW.value, description="desc")
    store.update_issue_fields("1003-d", review_loops=1)

    await orch.process_issue(issue_id="1003-d", project_id="p1", title="arbiter fallback continue", force=True)

    issue = store.get_issue("1003-d")
    assert issue is not None
    assert issue["state"] == PipelineState.DONE.value
    assert any("阶段：设计仲裁" in comment and "结论：继续编码" in comment for _, _, comment in plane.comments)


@pytest.mark.asyncio
async def test_review_over_limit_arbiter_failure_falls_back_to_stop_on_quality_signals(
    make_config,
    tmp_path: Path,
):
    config = make_config(
        project_id="p1",
        agent_config={"review": {"max_loops": 1, "arbiter_max_loops": 1}},
    )
    store = SQLiteStore(str(tmp_path / "db3-arbiter-fallback-stop.sqlite"))
    plane = FakePlaneClient()

    class FallbackStopAgent:
        async def run_stage(self, stage: Stage, context):
            if stage == Stage.DESIGN and context.metadata.get("design_mode") == "review_arbiter":
                return StageResult(status=StageStatus.FAILED, summary="claude timeout after 300s.")
            if stage == Stage.REVIEW:
                return StageResult(status=StageStatus.NEEDS_CHANGES, summary="needs logic fix")
            return StageResult(status=StageStatus.SUCCESS, summary=f"{stage.value} ok")

    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=plane,
        github_client=FakeGitHubClient(),
        agent_adapter=FallbackStopAgent(),
        quality_gate=QualityGate(),
    )

    store.upsert_issue("1003-e", "p1", "arbiter fallback stop", PipelineState.REVIEW.value, description="desc")
    store.update_issue_fields("1003-e", review_loops=1)

    await orch.process_issue(issue_id="1003-e", project_id="p1", title="arbiter fallback stop", force=True)

    issue = store.get_issue("1003-e")
    assert issue is not None
    assert issue["state"] == PipelineState.BLOCKED.value
    assert any("阶段：设计仲裁" in comment and "结论：停止审查" in comment for _, _, comment in plane.comments)


@pytest.mark.asyncio
async def test_review_over_limit_blocks_when_arbiter_limit_exceeded(make_config, tmp_path: Path):
    config = make_config(
        project_id="p1",
        agent_config={"review": {"max_loops": 1, "arbiter_max_loops": 1}},
    )
    store = SQLiteStore(str(tmp_path / "db3-arbiter-limit.sqlite"))

    class CountingAgent:
        def __init__(self) -> None:
            self.design_calls = 0

        async def run_stage(self, stage: Stage, context):
            if stage == Stage.DESIGN and context.metadata.get("design_mode") == "review_arbiter":
                self.design_calls += 1
            if stage == Stage.REVIEW:
                return StageResult(status=StageStatus.NEEDS_CHANGES, summary="needs changes")
            return StageResult(status=StageStatus.SUCCESS, summary=f"{stage.value} ok")

    agent = CountingAgent()
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=FakePlaneClient(),
        github_client=FakeGitHubClient(),
        agent_adapter=agent,
        quality_gate=QualityGate(),
    )

    store.upsert_issue("1003-c", "p1", "arbiter limit", PipelineState.REVIEW.value, description="desc")
    store.update_issue_fields("1003-c", review_loops=1, arbiter_loops=1)

    await orch.process_issue(issue_id="1003-c", project_id="p1", title="arbiter limit", force=True)

    issue = store.get_issue("1003-c")
    assert issue is not None
    assert issue["state"] == PipelineState.BLOCKED.value
    assert agent.design_calls == 0
    trace = store.get_trace("1003-c")
    assert any("Review arbiter exceeded max limit." in t["message"] for t in trace)


@pytest.mark.asyncio
async def test_protocol_violation_triggers_human_handoff(make_config, tmp_path: Path):
    config = make_config(project_id="p1", tdd_enforcement_mode="strict")
    store = SQLiteStore(str(tmp_path / "db-protocol-handoff.sqlite"))
    plane = FakePlaneClient()

    class BadCodingAgent:
        async def run_stage(self, stage: Stage, context):
            if stage == Stage.DESIGN:
                return StageResult(
                    status=StageStatus.SUCCESS,
                    summary="design ok",
                    artifacts={
                        "stdout": (
                            "TDD_RED: case\nTDD_GREEN: impl\nTDD_REFACTOR: refactor\nTDD_ACCEPTANCE: done"
                        )
                    },
                )
            if stage == Stage.CODING:
                return StageResult(
                    status=StageStatus.SUCCESS,
                    summary="coding output without protocol tokens",
                    artifacts={"stdout": "仅描述修复内容，未按约定 token 输出"},
                )
            return StageResult(status=StageStatus.SUCCESS, summary="approved", artifacts={"stdout": "APPROVED\nok"})

    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=plane,
        github_client=FakeGitHubClient(),
        agent_adapter=BadCodingAgent(),
        quality_gate=QualityGate(),
    )
    await orch.process_issue(issue_id="pvh-1", project_id="p1", title="protocol handoff", force=False)

    issue = store.get_issue("pvh-1")
    assert issue is not None
    assert issue["state"] == PipelineState.BLOCKED.value
    assert issue["failure_class"] == "PROTOCOL_VIOLATION"
    assert any("[HUMAN-HANDOFF]" in comment for _, _, comment in plane.comments)
