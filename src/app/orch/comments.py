from __future__ import annotations

from app.comment_utils import (
    build_review_fix_comment as build_review_fix_comment_from_utils,
    format_stage_reason,
    human_stage_summary,
    stage_zh_name,
    status_zh_name,
)
from app.models import Stage, StageResult, StageStatus


def build_review_fix_comment(result: StageResult) -> str:
    stdout = str(result.artifacts.get("stdout", "")).strip()
    checklist: list[str] = []
    for line in stdout.splitlines()[1:]:
        text = line.strip("- ").strip()
        if not text:
            continue
        checklist.append(text)
        if len(checklist) >= 5:
            break
    return build_review_fix_comment_from_utils(
        failure_class=str(result.artifacts.get("failure_class", "")),
        checklist=checklist,
    )


def build_stage_comment(stage: Stage, result: StageResult) -> str:
    human_parts = [
        "[编排器]",
        f"阶段：{stage_zh_name(stage)}",
        f"状态：{status_zh_name(result.status)}",
        f"摘要：{human_stage_summary(stage, result.status)}",
    ]
    if result.status in {StageStatus.FAILED, StageStatus.NEEDS_CHANGES}:
        human_parts.append(f"原因：{format_stage_reason(result.summary, result.status)}")
    protocol_fields = result.artifacts.get("protocol_violation_fields")
    if isinstance(protocol_fields, list) and protocol_fields:
        human_parts.append(f"协议缺项：{', '.join(str(x) for x in protocol_fields[:6])}")
    violations = result.artifacts.get("line_limit_violations")
    if isinstance(violations, list) and violations:
        display = []
        for item in violations[:5]:
            if isinstance(item, dict):
                path = str(item.get("path", "")).strip()
                lines = str(item.get("lines", "")).strip()
                if path and lines:
                    display.append(f"{path}({lines}行)")
        if display:
            human_parts.append(f"超限文件：{', '.join(display)}")
    if result.artifacts.get("pr_url"):
        human_parts.append(f"PR：{result.artifacts['pr_url']}")

    machine_parts = [
        "[ORCH]",
        f"stage={stage.value}",
        f"status={result.status.value}",
        f"summary={result.summary}",
    ]
    if result.artifacts.get("pr_url"):
        machine_parts.append(f"pr_url={result.artifacts['pr_url']}")
    if isinstance(violations, list) and violations:
        machine_parts.append(f"line_limit_violations={violations}")
    if isinstance(protocol_fields, list) and protocol_fields:
        machine_parts.append(f"protocol_violation_fields={protocol_fields}")
    return "\n".join([" | ".join(human_parts), " | ".join(machine_parts)])


def build_retry_comment(stage: Stage, attempt: int, total_attempts: int, summary: str) -> str:
    human_parts = [
        "[编排器]",
        f"阶段：{stage_zh_name(stage)}",
        "状态：重试中",
        "摘要：阶段失败后自动重试",
        f"原因：{format_stage_reason(summary, StageStatus.FAILED)}",
        f"尝试：{attempt}/{total_attempts}",
    ]
    machine_parts = [
        "[ORCH]",
        f"stage={stage.value}",
        "status=retry",
        f"attempt={attempt}",
        f"total_attempts={total_attempts}",
        f"summary={summary}",
    ]
    return "\n".join([" | ".join(human_parts), " | ".join(machine_parts)])


def build_blocked_comment(stage: Stage, result: StageResult) -> str:
    handoff_reason = str(result.artifacts.get("handoff_reason", "")).strip()
    human_parts = [
        "[编排器]",
        f"阶段：{stage_zh_name(stage)}",
        "状态：已阻塞",
        "摘要：达到失败上限，流程暂停",
        f"原因：{format_stage_reason(result.summary, result.status)}",
        "处理：请修复后调用 internal retry 接口或在 Plane 中重新触发。",
    ]
    if handoff_reason:
        human_parts.append(f"接管原因：{handoff_reason}")
    machine_parts = [
        "[ORCH]",
        f"stage={stage.value}",
        "status=blocked_notice",
        f"summary={result.summary}",
    ]
    if handoff_reason:
        machine_parts.append(f"handoff_reason={handoff_reason}")
    return "\n".join([" | ".join(human_parts), " | ".join(machine_parts)])

