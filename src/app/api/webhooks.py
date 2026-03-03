from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.models import PipelineState

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _validate_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not secret:
        return True
    if not signature:
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature)


def _parse_orch_comment(comment: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for seg in comment.split("|"):
        s = seg.strip()
        if "=" in s:
            k, v = s.split("=", 1)
            parsed[k.strip()] = v.strip()
    return parsed


def _parse_cn_orch_comment(comment: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    stage_match = re.search(r"阶段[:：]\s*(设计|编码|审查)", comment)
    if stage_match:
        stage_map = {"设计": "design", "编码": "coding", "审查": "review"}
        parsed["stage"] = stage_map[stage_match.group(1)]

    status_match = re.search(r"状态[:：]\s*(成功|失败|需修改)", comment)
    if status_match:
        status_map = {"成功": "success", "失败": "failed", "需修改": "needs_changes"}
        parsed["status"] = status_map[status_match.group(1)]

    pr_match = re.search(r"PR[:：]\s*(https?://\S+)", comment)
    if pr_match:
        parsed["pr_url"] = pr_match.group(1)
    return parsed


def _extract_notify_message(payload: dict[str, Any]) -> tuple[str, list[str]] | None:
    event_type = str(payload.get("event") or payload.get("type") or "")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

    if "created" in event_type.lower() and "comment" not in event_type.lower():
        item = data.get("work_item") or data.get("issue") or data
        if isinstance(item, dict):
            issue_id = item.get("id", "")
            title = item.get("name") or item.get("title") or "Untitled"
            return (
                "[Plane] 任务开始",
                [f"Issue: {issue_id}", f"Title: {title}", "Stage: Design"],
            )

    if "comment" in event_type.lower():
        comment_obj = data.get("comment") if isinstance(data.get("comment"), dict) else data
        raw = str(comment_obj.get("comment_html") or comment_obj.get("comment") or "")
        if "[ORCH]" not in raw and "[编排器]" not in raw:
            return None
        parsed = _parse_orch_comment(raw)
        if "stage" not in parsed:
            parsed.update(_parse_cn_orch_comment(raw))
        stage = parsed.get("stage", "")
        status = parsed.get("status", "")
        summary = parsed.get("summary", "")
        pr_url = parsed.get("pr_url")
        if stage == "coding" and pr_url:
            return (
                "[Plane] PR 已创建",
                [f"Stage: Coding", f"Status: {status}", f"PR: {pr_url}", f"Summary: {summary}"],
            )
        if stage == "review":
            return (
                "[Plane] Review 结论",
                [f"Stage: Review", f"Status: {status}", f"Summary: {summary}"],
            )

    if "updated" in event_type.lower():
        item = data.get("work_item") or data.get("issue") or data
        if isinstance(item, dict):
            state_name = str(item.get("state_name") or (item.get("state") or {}).get("name") or "")
            issue_id = item.get("id", "")
            if state_name.lower() == PipelineState.DONE.value.lower():
                return ("[Plane] 任务完成", [f"Issue: {issue_id}", f"State: {state_name}"])
            if state_name.lower() == PipelineState.BLOCKED.value.lower():
                return ("[Plane] 任务失败/阻塞", [f"Issue: {issue_id}", f"State: {state_name}"])

    return None


@router.post("/plane")
async def plane_webhook(request: Request) -> dict[str, Any]:
    body = await request.body()
    signature = request.headers.get("x-plane-signature")

    settings = request.app.state.config.settings
    if not _validate_signature(settings.plane_webhook_secret, body, signature):
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    payload = await request.json()
    orchestrator = request.app.state.orchestrator
    notifier = request.app.state.feishu_notifier

    result = await orchestrator.handle_webhook(payload)

    msg = _extract_notify_message(payload)
    if msg is not None and notifier.enabled():
        title, lines = msg
        await notifier.send_text(title=title, lines=lines)

    return {"ok": True, "result": result}


@router.post("/plane/relay")
async def plane_webhook_relay(request: Request) -> dict[str, Any]:
    body = await request.body()
    signature = request.headers.get("x-plane-signature")
    settings = request.app.state.config.settings
    if not _validate_signature(settings.plane_webhook_secret, body, signature):
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    payload = await request.json()
    notifier = request.app.state.feishu_notifier
    msg = _extract_notify_message(payload)
    sent = False
    if msg is not None and notifier.enabled():
        title, lines = msg
        await notifier.send_text(title=title, lines=lines)
        sent = True

    return {"ok": True, "sent": sent}
