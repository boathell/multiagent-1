from __future__ import annotations

from pathlib import Path

import pytest

from app.models import Stage, StageResult, StageStatus
from app.orchestrator import Orchestrator
from app.quality_gate import QualityGate
from app.store import SQLiteStore
from .helpers import FakeGitHubClient, FakePlaneClient, ScriptedAgent


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
    assert "摘要：编码阶段执行成功" in comment
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


def test_build_arbiter_comment_contains_decision_fields():
    comment = Orchestrator._build_arbiter_comment(
        {
            "status": "success",
            "message": "Design arbiter decided continue_coding.",
            "metadata": {
                "arbiter_action": "CONTINUE_CODING",
                "arbiter_diagnosis": "GEMINI_ISSUE",
                "arbiter_reason": "Gemini 返回 503，建议继续编码并缩小 diff。",
            },
        }
    )
    assert "阶段：设计仲裁" in comment
    assert "结论：继续编码" in comment
    assert "判因：Gemini问题" in comment
    assert "stage=design_arbiter" in comment
    assert "action=CONTINUE_CODING" in comment


def test_build_retry_and_blocked_comment_templates():
    retry_comment = Orchestrator._build_retry_comment(
        Stage.CODING,
        attempt=1,
        total_attempts=2,
        summary="quality gate failed after coding stage.",
    )
    assert "状态：重试中" in retry_comment
    assert "摘要：阶段失败后自动重试" in retry_comment
    assert "原因：质量门禁未通过" in retry_comment
    assert "status=retry" in retry_comment

    blocked_comment = Orchestrator._build_blocked_comment(
        Stage.REVIEW,
        StageResult(status=StageStatus.FAILED, summary="review loop exceeded max limit."),
    )
    assert "状态：已阻塞" in blocked_comment
    assert "摘要：达到失败上限，流程暂停" in blocked_comment
    assert "原因：审查回流超过上限" in blocked_comment
    assert "status=blocked_notice" in blocked_comment


@pytest.mark.asyncio
async def test_coding_success_syncs_tdd_summary_to_pr(make_config, tmp_path: Path):
    config = make_config(project_id="p1")
    store = SQLiteStore(str(tmp_path / "db-pr-sync.sqlite"))
    github = FakeGitHubClient()
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=FakePlaneClient(),
        github_client=github,
        agent_adapter=ScriptedAgent(),
        quality_gate=QualityGate(),
    )
    await orch.process_issue(
        issue_id="pr-sync-1",
        project_id="p1",
        title="sync pr",
        force=False,
        description=(
            "## 风险\n- review 可能超时\n"
            "## 回滚\n- git revert"
        ),
    )
    assert any("## TDD 执行摘要" in body for _, body, _ in github.comments)


@pytest.mark.asyncio
async def test_review_failure_syncs_fixlist_to_plane_and_pr(make_config, tmp_path: Path):
    config = make_config(project_id="p1")
    store = SQLiteStore(str(tmp_path / "db-review-fix.sqlite"))
    plane = FakePlaneClient()
    github = FakeGitHubClient()
    agent = ScriptedAgent(
        {
            Stage.REVIEW: [
                StageResult(
                    status=StageStatus.NEEDS_CHANGES,
                    summary="review failed",
                    artifacts={"stdout": "NEEDS_CHANGES\n- 修复 tests/test_orchestrator.py\n- 补充 diff 证据"},
                ),
                StageResult(
                    status=StageStatus.SUCCESS,
                    summary="approved",
                    artifacts={"stdout": "APPROVED\nok"},
                ),
            ]
        }
    )
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=plane,
        github_client=github,
        agent_adapter=agent,
        quality_gate=QualityGate(),
    )

    await orch.process_issue(issue_id="review-fix-1", project_id="p1", title="review fix sync", force=False)
    assert any("编排器-修复清单" in comment for _, _, comment in plane.comments)
    assert any("编排器-修复清单" in body for _, body, _ in github.comments)
