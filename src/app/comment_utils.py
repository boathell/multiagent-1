from __future__ import annotations

import re
from typing import Any

from app.models import FailureClass, Stage, StageStatus


def stage_zh_name(stage: Stage) -> str:
    mapping = {
        Stage.DESIGN: "设计",
        Stage.CODING: "编码",
        Stage.REVIEW: "审查",
    }
    return mapping.get(stage, stage.value)


def status_zh_name(status: StageStatus) -> str:
    mapping = {
        StageStatus.SUCCESS: "成功",
        StageStatus.FAILED: "失败",
        StageStatus.NEEDS_CHANGES: "需修改",
    }
    return mapping.get(status, status.value)


def human_stage_summary(stage: Stage, status: StageStatus) -> str:
    if status == StageStatus.SUCCESS:
        if stage == Stage.DESIGN:
            return "设计阶段执行成功"
        if stage == Stage.CODING:
            return "编码阶段执行成功"
        return "审查阶段执行成功"
    if status == StageStatus.NEEDS_CHANGES:
        return "审查未通过，需要修改后重试"
    return "阶段执行失败"


def format_stage_reason(summary: str, status: StageStatus) -> str:
    lowered = summary.lower()
    if status == StageStatus.NEEDS_CHANGES:
        return "审查要求修改"

    timeout_match = re.search(r"timeout after (\d+)s", lowered)
    if timeout_match:
        return f"执行超时（{timeout_match.group(1)}s）"
    if "command not found" in lowered:
        return "命令未找到"
    if "execution error" in lowered:
        return "执行异常"
    if "quality gate failed" in lowered:
        return "质量门禁未通过"
    if "review loop exceeded" in lowered:
        return "审查回流超过上限"
    if "review arbiter exceeded" in lowered:
        return "设计仲裁超过上限"
    if "arbiter decided to stop review" in lowered:
        return "设计仲裁决定停止审查"
    if "code file line limit exceeded" in lowered:
        return "单文件代码行数超过上限"
    if "fetch failed" in lowered:
        return "模型网络请求失败"
    if "protocol violation" in lowered:
        return "Agent 输出协议不符合约定"
    return "阶段执行失败"


def failure_class_zh_name(failure_class: str) -> str:
    mapping = {
        FailureClass.QUALITY_ISSUE.value: "质量问题",
        FailureClass.GEMINI_ISSUE.value: "Gemini问题",
        FailureClass.ENV_ISSUE.value: "环境问题",
        FailureClass.PROTOCOL_VIOLATION.value: "协议违规",
    }
    return mapping.get(failure_class, failure_class or "未知")


def build_handoff_comment(
    *,
    issue_id: str,
    stage: Stage,
    failure_class: str,
    reason: str,
    attempted: list[str],
    suggested_actions: list[str],
) -> str:
    attempted_text = "；".join(x for x in attempted if x) or "无"
    action_text = "；".join(x for x in suggested_actions if x) or "请人工评估后处理"
    retry_cmd = f"curl -X POST http://127.0.0.1:8787/internal/issues/{issue_id}/retry"

    human_parts = [
        "[HUMAN-HANDOFF]",
        f"阶段：{stage_zh_name(stage)}",
        f"分类：{failure_class_zh_name(failure_class)}",
        f"问题：{reason[:240]}",
        "影响：自动编排已暂停，等待人工处理。",
        f"已尝试：{attempted_text[:240]}",
        f"建议动作：{action_text[:240]}",
        f"重试命令：{retry_cmd}",
    ]
    machine_parts = [
        "[ORCH]",
        "stage=human_handoff",
        f"source_stage={stage.value}",
        f"failure_class={failure_class}",
        f"reason={reason[:240]}",
        f"retry_cmd={retry_cmd}",
    ]
    return "\n".join([" | ".join(human_parts), " | ".join(machine_parts)])


def build_contract_reminder_comment(
    *,
    score: int,
    missing_fields: list[str],
) -> str:
    field_map = {
        "goal": "目标",
        "scope": "范围",
        "dod": "DoD",
        "risk": "风险",
        "rollback": "回滚",
    }
    missing = ", ".join(field_map.get(x, x) for x in missing_fields) if missing_fields else "无"
    human_parts = [
        "[TDD-Contract-提醒]",
        f"合同完整度：{score}/100",
        f"缺项：{missing}",
        "策略：建议补全后再进入严格协作；当前先按降级模式继续。",
    ]
    machine_parts = [
        "[ORCH]",
        "stage=contract",
        f"score={score}",
        f"missing_fields={missing_fields}",
        "mode=advisory",
    ]
    return "\n".join([" | ".join(human_parts), " | ".join(machine_parts)])


def build_review_fix_comment(
    *,
    failure_class: str,
    checklist: list[str],
) -> str:
    lines = checklist or ["补充审查证据并重新提交审查。"]
    clean = [f"- {item}" for item in lines[:6]]
    human_header = (
        "[编排器-修复清单] 审查未通过，请按以下步骤处理："
        f"（分类：{failure_class_zh_name(failure_class)}）"
    )
    machine_line = f"[ORCH] | stage=review_fixlist | failure_class={failure_class} | items={len(clean)}"
    return "\n".join([human_header, *clean, machine_line])


def collect_attempted_actions(metadata: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    attempts = metadata.get("attempt_failure_classes")
    if isinstance(attempts, list) and attempts:
        actions.append(f"阶段内自动重试 {len(attempts)} 次")
    if metadata.get("review_loops"):
        actions.append(f"Review 回流次数={metadata.get('review_loops')}")
    if metadata.get("arbiter_loops"):
        actions.append(f"设计仲裁次数={metadata.get('arbiter_loops')}")
    return actions
