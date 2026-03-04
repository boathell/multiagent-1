from __future__ import annotations

from typing import Any

from app.models import FailureClass, Stage, StageResult, StageStatus
from app.tdd_parser import parse_tdd_sections


def validate_stage_protocol(stage: Stage, result: StageResult, tdd_enforcement_mode: str) -> StageResult:
    stdout = str(result.artifacts.get("stdout", "")).strip()
    missing_fields: list[str] = []
    protocol_ok = True

    if stage == Stage.DESIGN and result.status == StageStatus.SUCCESS:
        if not stdout:
            return result
        tokens = ("TDD_RED", "TDD_GREEN", "TDD_REFACTOR", "TDD_ACCEPTANCE")
        parsed_sections = parse_tdd_sections(stdout)
        for token, section_key in zip(tokens, ("red", "green", "refactor", "acceptance"), strict=False):
            if token in stdout.upper():
                continue
            if str(parsed_sections.get(section_key, "")).strip():
                continue
            missing_fields.append(token)
        protocol_ok = not missing_fields
    elif stage == Stage.CODING and result.status == StageStatus.SUCCESS:
        if not stdout:
            return result
        tokens = ("RED_RESULT", "GREEN_RESULT", "REFACTOR_NOTE", "CHANGED_FILES")
        missing_fields = [token for token in tokens if token not in stdout.upper()]
        protocol_ok = not missing_fields
    elif stage == Stage.REVIEW and result.status in {StageStatus.SUCCESS, StageStatus.NEEDS_CHANGES}:
        if not stdout:
            return result
        first_line = first_non_empty_line(stdout).upper()
        if first_line not in {"APPROVED", "NEEDS_CHANGES"}:
            protocol_ok = False
            missing_fields = ["FIRST_LINE(APPROVED|NEEDS_CHANGES)"]

    if protocol_ok:
        return result

    artifacts = {
        **result.artifacts,
        "failure_class": FailureClass.PROTOCOL_VIOLATION.value,
        "protocol_violation_fields": missing_fields,
    }
    if tdd_enforcement_mode == "advisory":
        artifacts["protocol_violation_advisory"] = True
        return StageResult(
            status=result.status,
            summary=result.summary,
            artifacts=artifacts,
        )

    return StageResult(
        status=StageStatus.FAILED,
        summary=f"Protocol violation: missing {', '.join(missing_fields)}",
        artifacts=artifacts,
    )


def first_non_empty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def apply_review_evidence_gate(stage: Stage, result: StageResult, agents_use_mock: bool) -> StageResult:
    if stage != Stage.REVIEW:
        return result

    artifacts = {**result.artifacts}
    signals: list[str] = []
    review_context_error = str(artifacts.get("review_context_error", "")).strip()
    review_diff_truncated = bool(artifacts.get("review_diff_truncated"))
    changed_files = artifacts.get("review_changed_files")
    if not isinstance(changed_files, list):
        changed_files = []
    stdout = str(artifacts.get("stdout", "")).strip()

    if agents_use_mock and not changed_files and review_context_error:
        return result

    if review_context_error:
        signals.append("review_context_error")

    if result.status == StageStatus.SUCCESS:
        if not changed_files:
            signals.append("missing_changed_files")
        if changed_files and not review_mentions_key_files(stdout, changed_files):
            signals.append("missing_evidence")
        if review_diff_truncated:
            signals.append("diff_truncated")

    if not signals:
        return result

    known_signals = list(dict.fromkeys([*(artifacts.get("review_failure_signals") or []), *signals]))
    artifacts["review_failure_signals"] = known_signals
    artifacts["failure_class"] = FailureClass.GEMINI_ISSUE.value
    artifacts["review_evidence_integrity"] = "incomplete"
    if result.status == StageStatus.SUCCESS:
        return StageResult(
            status=StageStatus.NEEDS_CHANGES,
            summary="Review evidence incomplete, require changes.",
            artifacts=artifacts,
        )
    return StageResult(
        status=result.status,
        summary=result.summary,
        artifacts=artifacts,
    )


def review_mentions_key_files(stdout: str, changed_files: list[Any]) -> bool:
    lowered = stdout.lower()
    for item in changed_files:
        path = str(item).strip()
        if not path:
            continue
        basename = path.split("/")[-1].lower()
        if basename and basename in lowered:
            return True
    return False


def classify_failure(stage: Stage, result: StageResult) -> str:
    explicit = str(result.artifacts.get("failure_class", "")).strip().upper()
    if explicit in {x.value for x in FailureClass}:
        return explicit

    if result.artifacts.get("protocol_violation_fields"):
        return FailureClass.PROTOCOL_VIOLATION.value

    text = "\n".join(
        [
            str(result.summary),
            str(result.artifacts.get("stdout", "")),
            str(result.artifacts.get("stderr", "")),
        ]
    ).lower()
    if any(x in text for x in ("timeout after", "503", "fetch failed", "review context error", "diff truncated")):
        return FailureClass.GEMINI_ISSUE.value
    if any(
        x in text
        for x in (
            "command not found",
            "not a git repo",
            "failed to create pr",
            "permission denied",
            "authentication failed",
        )
    ):
        return FailureClass.ENV_ISSUE.value
    if stage == Stage.REVIEW and result.status == StageStatus.NEEDS_CHANGES:
        return FailureClass.QUALITY_ISSUE.value
    if result.status == StageStatus.FAILED:
        return FailureClass.QUALITY_ISSUE.value
    return ""


def evaluate_handoff_trigger(
    *,
    stage: Stage,
    result: StageResult,
    traces: list[dict[str, Any]],
    human_handoff_enabled: bool,
) -> dict[str, Any] | None:
    if not human_handoff_enabled:
        return None

    failure_class = classify_failure(stage, result) or FailureClass.QUALITY_ISSUE.value
    tool_failure_attempts = int(result.artifacts.get("tool_failure_attempts", 0))
    protocol_violation_attempts = int(result.artifacts.get("protocol_violation_attempts", 0))
    summary_lower = str(result.summary).lower()

    if tool_failure_attempts >= 2:
        return {
            "trigger": "tool_failure_consecutive",
            "failure_class": failure_class,
            "handoff_reason": "同阶段工具故障连续 2 次（timeout/503/fetch failed）。",
            "suggested_actions": [
                "检查对应 CLI 登录状态与网络连通性",
                "确认代理/证书/模型服务状态后再 retry",
            ],
        }

    if protocol_violation_attempts >= 2:
        return {
            "trigger": "protocol_violation_consecutive",
            "failure_class": FailureClass.PROTOCOL_VIOLATION.value,
            "handoff_reason": "协议违规连续 2 次，自动流程暂停。",
            "suggested_actions": [
                "人工修正 agent 输出格式后再 retry",
                "必要时缩小任务范围，确保 token 合规",
            ],
        }

    if "review arbiter exceeded max limit" in summary_lower:
        return {
            "trigger": "review_arbiter_overflow",
            "failure_class": FailureClass.QUALITY_ISSUE.value,
            "handoff_reason": "Review 回流与仲裁均超过上限。",
            "suggested_actions": [
                "由人工判断是质量问题还是评审工具问题",
                "必要时拆分 issue 后重新触发",
            ],
        }
    if "review arbiter decided to stop review" in summary_lower:
        return {
            "trigger": "review_arbiter_stop",
            "failure_class": FailureClass.QUALITY_ISSUE.value,
            "handoff_reason": "设计仲裁判定停止审查，需人工接管。",
            "suggested_actions": [
                "人工确认审查意见是否可执行",
                "若继续自动流程，请先缩小改动并重试",
            ],
        }

    protocol_failures = 0
    for trace in traces:
        if str(trace.get("stage")) != stage.value:
            continue
        if str(trace.get("status")) != "failed":
            break
        metadata = trace.get("metadata", {}) or {}
        if str(metadata.get("failure_class", "")).upper() == FailureClass.PROTOCOL_VIOLATION.value:
            protocol_failures += 1
        else:
            break
    if protocol_failures >= 2:
        return {
            "trigger": "protocol_violation_trace_consecutive",
            "failure_class": FailureClass.PROTOCOL_VIOLATION.value,
            "handoff_reason": "协议违规已连续出现，建议人工接管。",
            "suggested_actions": [
                "检查 prompts 与输出模板一致性",
                "必要时改为人工执行当前阶段",
            ],
        }

    return None


def detect_high_risk_keywords(description: str) -> list[str]:
    lowered = description.lower()
    keywords = [
        "数据迁移",
        "drop table",
        "truncate table",
        "delete from",
        "权限变更",
        "grant ",
        "revoke ",
        "rm -rf",
        "生产数据库",
    ]
    hits: list[str] = []
    for token in keywords:
        if token in lowered:
            hits.append(token)
    return hits

