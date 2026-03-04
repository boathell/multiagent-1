from __future__ import annotations

import re
from typing import Any

from app.models import IssueContext, PipelineState, Stage, StageResult, StageStatus
from app.store import SQLiteStore
from app.tdd_parser import parse_tdd_sections


def build_arbiter_comment(arbiter_trace: dict[str, Any]) -> str:
    metadata = arbiter_trace.get("metadata", {}) or {}
    action = str(metadata.get("arbiter_action", "STOP_REVIEW")).strip().upper()
    diagnosis = str(metadata.get("arbiter_diagnosis", "QUALITY_ISSUE")).strip().upper()
    reason = str(metadata.get("arbiter_reason", "")).strip() or "未提供"

    action_zh = "继续编码" if action == "CONTINUE_CODING" else "停止审查"
    diagnosis_zh = "Gemini问题" if diagnosis == "GEMINI_ISSUE" else "质量问题"

    human_parts = [
        "[编排器]",
        "阶段：设计仲裁",
        f"结论：{action_zh}",
        f"判因：{diagnosis_zh}",
        f"原因：{reason[:240]}",
    ]
    machine_parts = [
        "[ORCH]",
        "stage=design_arbiter",
        f"status={arbiter_trace.get('status', 'success')}",
        f"action={action}",
        f"diagnosis={diagnosis}",
        f"reason={reason[:240]}",
    ]
    return "\n".join([" | ".join(human_parts), " | ".join(machine_parts)])


async def resolve_review_overflow_with_design(
    *,
    store: SQLiteStore,
    agent_adapter,
    issue_id: str,
    project_id: str,
    title: str,
    project: Any,
    review_result: StageResult,
    review_loops: int,
    max_review_loops: int,
    max_review_arbiter_loops: int,

) -> tuple[StageResult, PipelineState, dict[str, Any] | None]:
    issue = store.get_issue(issue_id) or {}
    current_arbiter_loops = int(issue.get("arbiter_loops", 0))
    if current_arbiter_loops >= max_review_arbiter_loops:
        metadata = {
            "arbiter_action": "STOP_REVIEW",
            "arbiter_diagnosis": "QUALITY_ISSUE",
            "arbiter_reason": "设计仲裁次数超过上限，停止审查。",
            "arbiter_loops": current_arbiter_loops,
            "arbiter_max_loops": max_review_arbiter_loops,
            "failure_class": "QUALITY_ISSUE",
            "handoff_reason": "review_arbiter_exceeded",
        }
        review_result = StageResult(
            status=StageStatus.FAILED,
            summary="Review arbiter exceeded max limit.",
            artifacts={**review_result.artifacts, **metadata},
        )
        arbiter_trace = {
            "status": "failed",
            "message": "Review arbiter exceeded max limit.",
            "metadata": metadata,
        }
        return review_result, PipelineState.BLOCKED, arbiter_trace

    next_arbiter_loops = current_arbiter_loops + 1
    store.update_issue_fields(issue_id, arbiter_loops=next_arbiter_loops)
    issue = store.get_issue(issue_id) or issue

    latest_review_trace = _find_latest_trace(store=store, issue_id=issue_id, stage="review")
    latest_review_metadata = latest_review_trace.get("metadata", {}) if latest_review_trace else {}
    review_signals = _detect_review_failure_signals(
        review_result=review_result,
        review_metadata=latest_review_metadata if isinstance(latest_review_metadata, dict) else {},
    )

    context = IssueContext(
        issue_id=issue_id,
        project_id=project_id,
        title=title,
        description=str(issue.get("description", "")),
        repo_url=project.repo_url,
        local_path=project.local_path,
        base_branch=project.base_branch,
        branch=issue.get("branch", ""),
        pr_url=issue.get("pr_url", ""),
        attempts=issue.get("attempts", {}),
        review_loops=review_loops,
        arbiter_loops=next_arbiter_loops,
        tdd_sections=parse_tdd_sections(str(issue.get("description", ""))),
    )
    context.metadata.update(
        {
            "design_mode": "review_arbiter",
            "review_failure_summary": review_result.summary,
            "review_failure_stdout": str(review_result.artifacts.get("stdout", ""))[:2000],
            "review_failure_stderr": str(review_result.artifacts.get("stderr", ""))[:2000],
            "review_failure_signals": review_signals,
            "review_context_error": str(review_result.artifacts.get("review_context_error", "")),
            "review_loops": review_loops,
            "max_review_loops": max_review_loops,
            "arbiter_loops": next_arbiter_loops,
            "max_arbiter_loops": max_review_arbiter_loops,
        }
    )
    store.update_issue_fields(issue_id, last_stage=Stage.DESIGN.value)
    arbiter_result = await agent_adapter.run_stage(Stage.DESIGN, context)
    if arbiter_result.status == StageStatus.SUCCESS:
        action, diagnosis, reason = _parse_arbiter_decision(arbiter_result)
    else:
        action, diagnosis, reason = _fallback_arbiter_decision(
            review_signals=review_signals,
            failure_summary=arbiter_result.summary,
        )
    arbiter_metadata: dict[str, Any] = {
        "arbiter_action": action,
        "arbiter_diagnosis": diagnosis,
        "arbiter_reason": reason,
        "failure_class": diagnosis if diagnosis in {"QUALITY_ISSUE", "GEMINI_ISSUE"} else "QUALITY_ISSUE",
        "arbiter_loops": next_arbiter_loops,
        "arbiter_max_loops": max_review_arbiter_loops,
        "review_loops": review_loops,
        "review_max_loops": max_review_loops,
        "review_failure_signals": review_signals,
        "arbiter_summary": arbiter_result.summary,
        "arbiter_stdout": str(arbiter_result.artifacts.get("stdout", ""))[:2000],
        "arbiter_stderr": str(arbiter_result.artifacts.get("stderr", ""))[:2000],
    }

    arbiter_trace = {
        "status": "success" if arbiter_result.status == StageStatus.SUCCESS else "failed",
        "message": f"Design arbiter decided {action.lower()}.",
        "metadata": arbiter_metadata,
    }

    if action == "CONTINUE_CODING":
        merged_artifacts = {**review_result.artifacts, **arbiter_metadata}
        review_result = StageResult(
            status=StageStatus.NEEDS_CHANGES,
            summary="Review loop exceeded max limit; design arbiter decided continue coding.",
            artifacts=merged_artifacts,
        )
        return review_result, PipelineState.CODING, arbiter_trace

    merged_artifacts = {**review_result.artifacts, **arbiter_metadata}
    review_result = StageResult(
        status=StageStatus.FAILED,
        summary="Review arbiter decided to stop review.",
        artifacts=merged_artifacts,
    )
    return review_result, PipelineState.BLOCKED, arbiter_trace


def _find_latest_trace(store: SQLiteStore, issue_id: str, stage: str) -> dict[str, Any] | None:
    traces = store.get_trace(issue_id)
    for trace in traces:
        if str(trace.get("stage", "")).lower() == stage.lower():
            return trace
    return None


def _parse_arbiter_decision(result: StageResult) -> tuple[str, str, str]:
    stdout = str(result.artifacts.get("stdout", "")).strip()
    raw = stdout or result.summary

    first_line = ""
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped:
            first_line = stripped.upper()
            break

    action = first_line if first_line in {"CONTINUE_CODING", "STOP_REVIEW"} else "STOP_REVIEW"

    diagnosis_match = re.search(r"(?im)^DIAGNOSIS:\s*(QUALITY_ISSUE|GEMINI_ISSUE)\s*$", raw)
    diagnosis = diagnosis_match.group(1) if diagnosis_match else "QUALITY_ISSUE"

    reason_match = re.search(r"(?im)^REASON:\s*(.+)$", raw)
    reason = reason_match.group(1).strip() if reason_match else ""
    if not reason:
        if result.status != StageStatus.SUCCESS:
            reason = result.summary.strip() or "设计仲裁执行失败。"
            return action, diagnosis, reason
        if action == "STOP_REVIEW" and first_line not in {"CONTINUE_CODING", "STOP_REVIEW"}:
            reason = "设计仲裁输出无效，默认停止审查。"
        else:
            reason = result.summary.strip() or "设计仲裁未给出原因。"

    if diagnosis == "GEMINI_ISSUE" and action != "CONTINUE_CODING":
        action = "CONTINUE_CODING"
        reason = f"{reason}（判因为 Gemini 问题，自动改为继续编码）"

    return action, diagnosis, reason


def _fallback_arbiter_decision(
    *,
    review_signals: list[str],
    failure_summary: str,
) -> tuple[str, str, str]:
    gemini_signals = {"timeout", "api_503", "fetch_failed", "review_context_error", "diff_truncated"}
    if any(sig in gemini_signals for sig in review_signals):
        return (
            "CONTINUE_CODING",
            "GEMINI_ISSUE",
            f"设计仲裁执行失败（{failure_summary}），按信号降级判定为 Gemini 问题，继续编码。",
        )
    return (
        "STOP_REVIEW",
        "QUALITY_ISSUE",
        f"设计仲裁执行失败（{failure_summary}），按降级策略停止审查。",
    )


def _detect_review_failure_signals(
    review_result: StageResult,
    review_metadata: dict[str, Any],
) -> list[str]:
    signals: list[str] = []
    text = "\n".join(
        [
            str(review_result.summary),
            str(review_result.artifacts.get("stdout", "")),
            str(review_result.artifacts.get("stderr", "")),
        ]
    ).lower()

    def add_signal(name: str) -> None:
        if name not in signals:
            signals.append(name)

    if "timeout after" in text:
        add_signal("timeout")
    if "fetch failed" in text:
        add_signal("fetch_failed")
    if "503" in text or "unavailable" in text:
        add_signal("api_503")
    if "review context error" in text:
        add_signal("review_context_error")
    if "truncated" in text:
        add_signal("diff_truncated")

    if bool(review_metadata.get("review_diff_truncated")):
        add_signal("diff_truncated")
    if str(review_metadata.get("review_context_error", "")).strip():
        add_signal("review_context_error")
    if not signals:
        add_signal("quality_needs_changes")
    return signals
