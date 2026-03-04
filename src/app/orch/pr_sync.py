from __future__ import annotations

import re
from typing import Any

from app.models import StageResult
from app.tdd_parser import parse_issue_contract


def extract_named_section(text: str, token: str) -> str:
    pattern = rf"(?ims)^{re.escape(token)}\s*[:：]\s*(.+?)(?=^[A-Z_]+\s*[:：]|\Z)"
    matched = re.search(pattern, text)
    if not matched:
        return ""
    value = matched.group(1).strip()
    value = re.sub(r"\s+", " ", value)
    return value[:240]


async def sync_tdd_summary_to_pr(
    *,
    store: Any,
    github_client: Any,
    logger: Any,
    issue_id: str,
    local_path: str,
    pr_url: str,
    coding_result: StageResult,
) -> None:
    if not pr_url.strip():
        return
    issue = store.get_issue(issue_id) or {}
    contract = parse_issue_contract(str(issue.get("description", "")))
    stdout = str(coding_result.artifacts.get("stdout", "")).strip()
    red = extract_named_section(stdout, "RED_RESULT")
    green = extract_named_section(stdout, "GREEN_RESULT")
    refactor = extract_named_section(stdout, "REFACTOR_NOTE")
    checks = coding_result.artifacts.get("checks")

    lines = [
        "## TDD 执行摘要",
        f"- RED: {red or '未提供'}",
        f"- GREEN: {green or '未提供'}",
        f"- REFACTOR: {refactor or '未提供'}",
    ]
    if isinstance(checks, list) and checks:
        lines.append("## 关键检查")
        for item in checks[:6]:
            if not isinstance(item, dict):
                continue
            cmd = str(item.get("command", "")).strip()
            exit_code = item.get("exit_code", "")
            if cmd:
                lines.append(f"- `{cmd}` -> exit {exit_code}")
    if contract.risk or contract.rollback:
        lines.append("## 风险与回滚")
        lines.append(f"- 风险: {contract.risk or '未提供'}")
        lines.append(f"- 回滚: {contract.rollback or '未提供'}")

    comment = "\n".join(lines)
    try:
        github_client.add_pr_comment(pr_url=pr_url, body=comment, local_path=local_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("GitHub PR comment failed: %s", exc)


async def sync_review_feedback_to_pr(
    *,
    github_client: Any,
    logger: Any,
    pr_url: str,
    local_path: str,
    comment: str,
) -> None:
    if not pr_url.strip():
        return
    try:
        github_client.add_pr_comment(pr_url=pr_url, body=comment, local_path=local_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("GitHub review feedback sync failed: %s", exc)

