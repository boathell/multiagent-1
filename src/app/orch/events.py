from __future__ import annotations

import html
import re
from typing import Any

from app.models import PipelineState


def extract_event_id(payload: dict[str, Any]) -> str:
    candidate = payload.get("event_id") or payload.get("id")
    if candidate:
        return str(candidate)
    data = payload.get("data", {})
    if not isinstance(data, dict):
        data = {}
    item = data.get("work_item") or data.get("issue") or data
    if isinstance(item, dict):
        issue_id = item.get("id", "unknown")
        updated_at = item.get("updated_at") or item.get("created_at") or "na"
    else:
        issue_id = "unknown"
        updated_at = "na"
    event_name = payload.get("event") or payload.get("type") or "unknown"
    return f"{event_name}:{issue_id}:{updated_at}"


def extract_event_type(payload: dict[str, Any]) -> str:
    return str(payload.get("event") or payload.get("event_type") or payload.get("type") or "")


def extract_issue(payload: dict[str, Any]) -> dict[str, str] | None:
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    item = data.get("work_item") or data.get("issue") or data
    if not isinstance(item, dict):
        return None

    issue_id = item.get("id") or item.get("issue_id")
    project_id = item.get("project_id")
    if project_id is None and isinstance(item.get("project"), dict):
        project_id = item["project"].get("id")

    title = item.get("name") or item.get("title") or "Untitled"
    description = extract_issue_description(item)
    state_name = item.get("state_name")
    if not state_name and isinstance(item.get("state"), dict):
        state_name = item["state"].get("name")
    state_name = state_name or PipelineState.TODO.value

    if issue_id is None or project_id is None:
        return None

    return {
        "issue_id": str(issue_id),
        "project_id": str(project_id),
        "title": str(title),
        "description": description,
        "state": str(state_name),
    }


def extract_issue_description(item: dict[str, Any]) -> str:
    for key in ("description_html", "description", "description_binary", "description_markdown"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return normalize_description(value)
    return ""


def normalize_description(raw: str) -> str:
    text = raw
    text = re.sub(r"</h[1-6]>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.IGNORECASE)
    text = re.sub(r"</li>", "\n", text, flags=re.IGNORECASE)
    text = text.replace("</p>", "\n").replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\r\n", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def description_to_html(description: str) -> str:
    lines = description.splitlines()
    parts: list[str] = []
    list_items: list[str] = []

    def flush_list() -> None:
        nonlocal list_items
        if not list_items:
            return
        li = "".join(f"<li>{html.escape(item)}</li>" for item in list_items)
        parts.append(f"<ul>{li}</ul>")
        list_items = []

    for raw in lines:
        line = raw.strip()
        if not line:
            flush_list()
            continue

        if line.startswith("- "):
            list_items.append(line[2:].strip())
            continue

        flush_list()
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            level = len(heading.group(1))
            text = heading.group(2).strip()
            parts.append(f"<h{level}>{html.escape(text)}</h{level}>")
            continue
        parts.append(f"<p>{html.escape(line)}</p>")

    flush_list()
    return "".join(parts) if parts else "<p></p>"


def should_start_pipeline(event_type: str, issue_data: dict[str, str]) -> bool:
    state = issue_data["state"].lower()
    event = event_type.lower()
    if "created" in event:
        return True
    if "updated" in event and state in {
        PipelineState.TODO.value.lower(),
        PipelineState.DESIGN.value.lower(),
        PipelineState.CODING.value.lower(),
        PipelineState.REVIEW.value.lower(),
    }:
        return True
    return False

