from __future__ import annotations

import asyncio
import html
import logging
import re
import subprocess
from typing import Any

from app.config import AppConfig
from app.models import IssueContext, PipelineState, Stage, StageResult, StageStatus
from app.quality_gate import QualityGate
from app.state_machine import next_state, stage_for_state, to_state
from app.store import SQLiteStore


class Orchestrator:
    def __init__(
        self,
        app_config: AppConfig,
        store: SQLiteStore,
        plane_client,
        github_client,
        agent_adapter,
        quality_gate: QualityGate,
    ) -> None:
        self.config = app_config
        self.store = store
        self.plane_client = plane_client
        self.github_client = github_client
        self.agent_adapter = agent_adapter
        self.quality_gate = quality_gate
        self.logger = logging.getLogger("app.orchestrator")
        self.max_retries = 1
        self.max_review_loops = 1
        self._issue_locks: dict[str, asyncio.Lock] = {}

    async def handle_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        event_id = self.extract_event_id(payload)
        if self.store.is_event_processed(event_id):
            return {"status": "duplicate", "event_id": event_id}

        event_type = self.extract_event_type(payload)
        issue_data = self.extract_issue(payload)
        if issue_data is None:
            self.store.mark_event_processed(event_id)
            return {"status": "ignored", "reason": "no issue payload", "event_id": event_id}

        issue_id = issue_data["issue_id"]
        lock = self._issue_locks.setdefault(issue_id, asyncio.Lock())
        async with lock:
            self.store.mark_event_processed(event_id)
            should_run = self.should_start_pipeline(event_type, issue_data)
            if not should_run:
                self.store.upsert_issue(
                    issue_id=issue_id,
                    project_id=issue_data["project_id"],
                    title=issue_data["title"],
                    state=issue_data["state"],
                    description=issue_data["description"],
                )
                return {"status": "ignored", "reason": "event filtered", "event_id": event_id}

            await self.process_issue(
                issue_id=issue_id,
                project_id=issue_data["project_id"],
                title=issue_data["title"],
                description=issue_data["description"],
                force=False,
            )

        return {"status": "accepted", "event_id": event_id, "issue_id": issue_id}

    async def retry_issue(self, issue_id: str) -> dict[str, Any]:
        issue = self.store.get_issue(issue_id)
        if issue is None:
            return {"status": "not_found", "issue_id": issue_id}

        lock = self._issue_locks.setdefault(issue_id, asyncio.Lock())
        async with lock:
            issue = self.store.get_issue(issue_id)
            if issue is None:
                return {"status": "not_found", "issue_id": issue_id}

            state = to_state(issue["state"])
            if state == PipelineState.BLOCKED:
                last_stage = issue.get("last_stage")
                if last_stage == Stage.DESIGN.value:
                    state = PipelineState.DESIGN
                elif last_stage == Stage.REVIEW.value:
                    state = PipelineState.REVIEW
                else:
                    state = PipelineState.CODING
                self.store.update_issue_fields(issue_id, state=state.value)

            await self.process_issue(
                issue_id=issue_id,
                project_id=issue["project_id"],
                title=issue["title"],
                description=str(issue.get("description", "")),
                force=True,
            )
        return {"status": "replayed", "issue_id": issue_id}

    def get_trace(self, issue_id: str) -> list[dict[str, Any]]:
        return self.store.get_trace(issue_id)

    async def process_issue(
        self,
        issue_id: str,
        project_id: str,
        title: str,
        force: bool,
        description: str = "",
    ) -> None:
        project = self.config.get_project(project_id)
        if project is None:
            self.store.append_trace(
                issue_id,
                stage="system",
                status="blocked",
                message=f"No project mapping for project_id={project_id}",
            )
            self.store.upsert_issue(
                issue_id,
                project_id,
                title,
                PipelineState.BLOCKED.value,
                description=description,
            )
            return

        existing = self.store.get_issue(issue_id)
        if existing is None:
            self.store.upsert_issue(
                issue_id,
                project_id,
                title,
                PipelineState.TODO.value,
                description=description,
            )
            existing = self.store.get_issue(issue_id)
        assert existing is not None
        if description:
            self.store.update_issue_fields(issue_id, description=description)
            existing = self.store.get_issue(issue_id) or existing

        current_state = to_state(existing["state"])
        if current_state == PipelineState.TODO:
            await self._set_state(
                issue_id,
                project_id,
                PipelineState.DESIGN,
                "Pipeline started",
                project.state_map,
            )
            current_state = PipelineState.DESIGN

        if not force and current_state in {PipelineState.DONE, PipelineState.BLOCKED}:
            self.store.append_trace(
                issue_id,
                stage="system",
                status="ignored",
                message=f"Issue already in terminal state: {current_state.value}",
            )
            return

        while True:
            stage = stage_for_state(current_state)
            if stage is None:
                break

            result = await self._run_stage_with_retry(stage, issue_id, project_id, title, project)
            next_pipeline_state = next_state(current_state, result.status)

            if stage == Stage.DESIGN and result.status == StageStatus.SUCCESS:
                issue_record = self.store.get_issue(issue_id) or {}
                current_desc = str(issue_record.get("description", ""))
                generated_sections = self._extract_tdd_sections_from_stage_result(result)
                merged_desc, merged_sections, merged = self._merge_tdd_sections(
                    base_description=current_desc,
                    generated_sections=generated_sections,
                )
                if merged:
                    self.store.update_issue_fields(issue_id, description=merged_desc)
                    result.artifacts["tdd_autofill"] = {
                        k: bool(v.strip()) for k, v in generated_sections.items()
                    }
                    await self._sync_plane_description(
                        project_id=project_id,
                        issue_id=issue_id,
                        description=merged_desc,
                    )

                missing_sections = self._tdd_missing_sections(merged_sections)
                if missing_sections and not self._has_tdd_reminder(issue_id):
                    labels = {"red": "Red", "green": "Green", "refactor": "Refactor"}
                    missing_names = ", ".join(labels.get(x, x) for x in missing_sections)
                    tdd_msg = (
                        "[TDD-提醒] 设计阶段未补全 Red/Green/Refactor，已按降级模式执行。"
                        f" 当前缺失: {missing_names}"
                    )
                    self.store.append_trace(issue_id, stage="tdd", status="reminder", message=tdd_msg)
                    await self.plane_client.add_comment(
                        project_id=project_id,
                        issue_id=issue_id,
                        comment=tdd_msg,
                    )

            if stage == Stage.REVIEW and result.status == StageStatus.NEEDS_CHANGES:
                issue_record = self.store.get_issue(issue_id)
                loops = int((issue_record or {}).get("review_loops", 0)) + 1
                self.store.update_issue_fields(issue_id, review_loops=loops)
                if loops > self.max_review_loops:
                    result = StageResult(
                        status=StageStatus.FAILED,
                        summary="Review loop exceeded max limit.",
                    )
                    next_pipeline_state = PipelineState.BLOCKED

            self.store.append_trace(
                issue_id,
                stage=stage.value,
                status=result.status.value,
                message=result.summary,
                metadata=result.artifacts,
            )

            comment = self._build_stage_comment(stage, result)
            await self.plane_client.add_comment(project_id=project_id, issue_id=issue_id, comment=comment)

            if stage == Stage.CODING and result.status == StageStatus.SUCCESS:
                pr_url = result.artifacts.get("pr_url", "")
                if pr_url:
                    self.store.update_issue_fields(issue_id, pr_url=pr_url)

            await self._set_state(
                issue_id,
                project_id,
                next_pipeline_state,
                f"Stage {stage.value}: {result.summary}",
                project.state_map,
            )
            current_state = next_pipeline_state

            if current_state == PipelineState.BLOCKED:
                blocked_comment = self._build_blocked_comment(stage, result)
                await self.plane_client.add_comment(
                    project_id=project_id,
                    issue_id=issue_id,
                    comment=blocked_comment,
                )

            if current_state in {PipelineState.DONE, PipelineState.BLOCKED}:
                break

    async def _run_stage_with_retry(
        self,
        stage: Stage,
        issue_id: str,
        project_id: str,
        title: str,
        project,
    ) -> StageResult:
        total_attempts = self.max_retries + 1
        last_result = StageResult(status=StageStatus.FAILED, summary="No execution")

        for _ in range(total_attempts):
            attempt_number = self.store.increment_attempt(issue_id, stage.value)
            issue = self.store.get_issue(issue_id)
            assert issue is not None
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
                review_loops=int(issue.get("review_loops", 0)),
                tdd_sections=self.parse_tdd_sections(str(issue.get("description", ""))),
            )
            if stage == Stage.REVIEW:
                context.metadata.update(
                    self._collect_review_context(
                        local_path=project.local_path,
                        base_branch=project.base_branch,
                        branch=context.branch,
                    )
                )

            self.store.update_issue_fields(issue_id, last_stage=stage.value)
            result = await self.agent_adapter.run_stage(stage, context)

            if stage == Stage.CODING and result.status == StageStatus.SUCCESS:
                pr = self.github_client.create_branch_commit_and_pr(
                    issue_id=issue_id,
                    title=title,
                    body="Generated by multi-agent orchestrator",
                    local_path=project.local_path,
                    base_branch=project.base_branch,
                    repo_url=project.repo_url,
                )
                result.artifacts["branch"] = pr.branch
                result.artifacts["pr_url"] = pr.pr_url
                self.store.update_issue_fields(issue_id, branch=pr.branch, pr_url=pr.pr_url)

                gate_result = await self.quality_gate.run(project.checks, project.local_path)
                if not gate_result.ok:
                    result = StageResult(
                        status=StageStatus.FAILED,
                        summary="Quality gate failed after coding stage.",
                        artifacts={
                            "checks": [
                                {
                                    "command": x.command,
                                    "exit_code": x.exit_code,
                                }
                                for x in gate_result.results
                            ]
                        },
                    )

            if result.status != StageStatus.FAILED:
                self.store.reset_attempt(issue_id, stage.value)
                return result

            last_result = result
            self.store.append_trace(
                issue_id,
                stage=stage.value,
                status="retry",
                message=f"Attempt {attempt_number}/{total_attempts} failed: {result.summary}",
            )
            if attempt_number < total_attempts:
                retry_comment = self._build_retry_comment(stage, attempt_number, total_attempts, result.summary)
                await self.plane_client.add_comment(
                    project_id=project_id,
                    issue_id=issue_id,
                    comment=retry_comment,
                )

        return last_result

    async def _set_state(
        self,
        issue_id: str,
        project_id: str,
        state: PipelineState,
        message: str,
        state_map: dict[str, str] | None = None,
    ) -> None:
        self.store.update_issue_fields(issue_id, state=state.value)
        self.store.append_trace(
            issue_id,
            stage="state",
            status=state.value.lower(),
            message=message,
        )

        try:
            await self.plane_client.update_work_item_state(
                project_id=project_id,
                issue_id=issue_id,
                state_name=state.value,
                state_map=state_map,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "Plane state update failed: issue_id=%s state=%s err=%s",
                issue_id,
                state.value,
                exc,
            )

    async def _sync_plane_description(self, project_id: str, issue_id: str, description: str) -> None:
        update_fn = getattr(self.plane_client, "update_work_item_description", None)
        if not callable(update_fn):
            return
        try:
            await update_fn(
                project_id=project_id,
                issue_id=issue_id,
                description_html=self._description_to_html(description),
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(
                "Plane description update failed: issue_id=%s err=%s",
                issue_id,
                exc,
            )

    @staticmethod
    def _build_stage_comment(stage: Stage, result: StageResult) -> str:
        stage_zh_map = Orchestrator._stage_zh_map()
        status_zh_map = Orchestrator._status_zh_map()

        human_parts = [
            "[编排器]",
            f"阶段：{stage_zh_map.get(stage, stage.value)}",
            f"状态：{status_zh_map.get(result.status, result.status.value)}",
            f"摘要：{Orchestrator._human_stage_summary(stage, result.status)}",
        ]
        if result.status in {StageStatus.FAILED, StageStatus.NEEDS_CHANGES}:
            human_parts.append(f"原因：{Orchestrator._format_reason(result.summary, result.status)}")
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
        return "\n".join([" | ".join(human_parts), " | ".join(machine_parts)])

    @staticmethod
    def _build_retry_comment(stage: Stage, attempt: int, total_attempts: int, summary: str) -> str:
        human_parts = [
            "[编排器]",
            f"阶段：{Orchestrator._stage_zh_map().get(stage, stage.value)}",
            "状态：重试中",
            "摘要：阶段失败后自动重试",
            f"原因：{Orchestrator._format_reason(summary, StageStatus.FAILED)}",
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

    @staticmethod
    def _build_blocked_comment(stage: Stage, result: StageResult) -> str:
        human_parts = [
            "[编排器]",
            f"阶段：{Orchestrator._stage_zh_map().get(stage, stage.value)}",
            "状态：已阻塞",
            "摘要：达到失败上限，流程暂停",
            f"原因：{Orchestrator._format_reason(result.summary, result.status)}",
            "处理：请修复后调用 internal retry 接口或在 Plane 中重新触发。",
        ]
        machine_parts = [
            "[ORCH]",
            f"stage={stage.value}",
            "status=blocked_notice",
            f"summary={result.summary}",
        ]
        return "\n".join([" | ".join(human_parts), " | ".join(machine_parts)])

    @staticmethod
    def _stage_zh_map() -> dict[Stage, str]:
        return {
            Stage.DESIGN: "设计",
            Stage.CODING: "编码",
            Stage.REVIEW: "审查",
        }

    @staticmethod
    def _status_zh_map() -> dict[StageStatus, str]:
        return {
            StageStatus.SUCCESS: "成功",
            StageStatus.FAILED: "失败",
            StageStatus.NEEDS_CHANGES: "需修改",
        }

    @staticmethod
    def _human_stage_summary(stage: Stage, status: StageStatus) -> str:
        if status == StageStatus.SUCCESS:
            if stage == Stage.DESIGN:
                return "设计阶段执行成功"
            if stage == Stage.CODING:
                return "编码阶段执行成功"
            return "审查阶段执行成功"
        if status == StageStatus.NEEDS_CHANGES:
            return "审查未通过，需要修改后重试"
        return "阶段执行失败"

    @staticmethod
    def _format_reason(summary: str, status: StageStatus) -> str:
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
        if "fetch failed" in lowered:
            return "模型网络请求失败"
        return "阶段执行失败"

    @staticmethod
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

    @staticmethod
    def extract_event_type(payload: dict[str, Any]) -> str:
        return str(payload.get("event") or payload.get("event_type") or payload.get("type") or "")

    @staticmethod
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
        description = Orchestrator._extract_issue_description(item)
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

    @staticmethod
    def _extract_issue_description(item: dict[str, Any]) -> str:
        for key in ("description_html", "description", "description_binary", "description_markdown"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return Orchestrator._normalize_description(value)
        return ""

    @staticmethod
    def _normalize_description(raw: str) -> str:
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

    @staticmethod
    def parse_tdd_sections(description: str) -> dict[str, str]:
        sections = {"red": "", "green": "", "refactor": "", "acceptance": ""}
        if not description.strip():
            return sections

        buffers = {k: [] for k in sections}
        current: str | None = None
        for raw_line in description.splitlines():
            line = raw_line.strip()
            marker = Orchestrator._detect_tdd_section_marker(line)
            if marker:
                current = marker
                inline_content = Orchestrator._extract_inline_tdd_section_content(line, marker)
                if inline_content:
                    buffers[current].append(inline_content)
                continue
            if current:
                buffers[current].append(raw_line.rstrip())

        for key, lines in buffers.items():
            sections[key] = "\n".join(x for x in lines if x.strip()).strip()
        return sections

    @staticmethod
    def _detect_tdd_section_marker(line: str) -> str | None:
        if not line:
            return None
        cleaned = line.lower()
        cleaned = re.sub(r"^[\s#>*`\-0-9\.\)\(]+", "", cleaned)
        cleaned = re.sub(r"[*_`]", "", cleaned).strip()
        if not cleaned:
            return None

        if re.match(r"^tdd[\s_-]*red(?:\s*[:：].*)?$", cleaned):
            return "red"
        if Orchestrator._matches_tdd_stage_heading(cleaned, "red"):
            return "red"
        if cleaned.startswith("red_result") or cleaned.startswith("red_stage"):
            return "red"
        if re.match(r"^tdd[\s_-]*green(?:\s*[:：].*)?$", cleaned):
            return "green"
        if Orchestrator._matches_tdd_stage_heading(cleaned, "green"):
            return "green"
        if cleaned.startswith("green_result") or cleaned.startswith("green_stage"):
            return "green"
        if re.match(r"^tdd[\s_-]*refactor(?:\s*[:：].*)?$", cleaned):
            return "refactor"
        if Orchestrator._matches_tdd_stage_heading(cleaned, "refactor"):
            return "refactor"
        if cleaned.startswith("重构阶段"):
            return "refactor"
        if cleaned.startswith("refactor_note") or cleaned.startswith("refactor_stage"):
            return "refactor"
        if re.match(r"^tdd[\s_-]*acceptance(?:\s*[:：].*)?$", cleaned):
            return "acceptance"
        if cleaned.startswith("验收标准") or cleaned.startswith("acceptance criteria") or cleaned == "dod":
            return "acceptance"
        if cleaned.startswith("acceptance") or cleaned.startswith("dod"):
            return "acceptance"
        return None

    @staticmethod
    def _extract_inline_tdd_section_content(line: str, marker: str) -> str:
        token_map = {
            "red": (r"tdd[\s_-]*red", r"red_result", r"red_stage"),
            "green": (r"tdd[\s_-]*green", r"green_result", r"green_stage"),
            "refactor": (r"tdd[\s_-]*refactor", r"refactor_note", r"refactor_stage"),
            "acceptance": (r"tdd[\s_-]*acceptance", r"acceptance(?:\s+criteria)?", r"dod"),
        }
        for token in token_map.get(marker, ()):
            matched = re.match(rf"(?i)^[\s#>*`\-0-9\.\)\(]*{token}\s*[:：]\s*(.+)$", line.strip())
            if not matched:
                continue
            content = matched.group(1).strip()
            if content.lower() in {"(missing)", "missing", "n/a", "na", "none"}:
                return ""
            return content
        return ""

    @staticmethod
    def _matches_tdd_stage_heading(cleaned: str, token: str) -> bool:
        if cleaned in {token, f"{token}:", f"{token}："}:
            return True
        pattern = rf"^{re.escape(token)}(?:\s*(?:阶段|stage).*)?$"
        return bool(re.match(pattern, cleaned))

    @staticmethod
    def _tdd_missing_sections(tdd_sections: dict[str, str]) -> list[str]:
        required = ("red", "green", "refactor")
        return [name for name in required if not str(tdd_sections.get(name, "")).strip()]

    def _has_tdd_reminder(self, issue_id: str) -> bool:
        traces = self.store.get_trace(issue_id)
        return any(t.get("stage") == "tdd" and t.get("status") == "reminder" for t in traces)

    @staticmethod
    def _extract_tdd_sections_from_stage_result(result: StageResult) -> dict[str, str]:
        stdout = str(result.artifacts.get("stdout", "")).strip()
        if not stdout:
            return {"red": "", "green": "", "refactor": "", "acceptance": ""}
        return Orchestrator.parse_tdd_sections(stdout)

    @staticmethod
    def _merge_tdd_sections(
        base_description: str,
        generated_sections: dict[str, str],
    ) -> tuple[str, dict[str, str], bool]:
        normalized_base = base_description.strip()
        base_sections = Orchestrator.parse_tdd_sections(normalized_base)
        section_labels = {
            "red": "Red 阶段",
            "green": "Green 阶段",
            "refactor": "Refactor 阶段",
            "acceptance": "验收标准（DoD）",
        }

        appended_parts: list[str] = []
        for key in ("red", "green", "refactor", "acceptance"):
            original = str(base_sections.get(key, "")).strip()
            generated = str(generated_sections.get(key, "")).strip()
            if original or not generated:
                continue
            appended_parts.append(f"### {section_labels[key]}\n{generated}")

        if not appended_parts:
            return normalized_base, base_sections, False

        chunks = [normalized_base] if normalized_base else []
        chunks.extend(appended_parts)
        merged_description = "\n\n".join(chunks).strip()
        merged_sections = Orchestrator.parse_tdd_sections(merged_description)
        return merged_description, merged_sections, True

    @staticmethod
    def _description_to_html(description: str) -> str:
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

    @staticmethod
    def _run_git(local_path: str, args: list[str]) -> tuple[bool, str]:
        try:
            completed = subprocess.run(
                args,
                cwd=local_path,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        output = "\n".join(x for x in [completed.stdout, completed.stderr] if x).strip()
        if completed.returncode != 0:
            return False, output or f"exit={completed.returncode}"
        return True, output.strip()

    def _collect_review_context(self, local_path: str, base_branch: str, branch: str) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "review_base_branch": base_branch,
            "review_branch": branch,
            "review_changed_files": [],
            "review_diff": "",
            "review_diff_range": "",
            "review_diff_truncated": False,
        }
        if not local_path:
            meta["review_context_error"] = "workspace path is empty"
            return meta

        ok_git, _ = self._run_git(local_path, ["git", "rev-parse", "--is-inside-work-tree"])
        if not ok_git:
            meta["review_context_error"] = f"not a git repo: {local_path}"
            return meta

        ok_head, head_branch = self._run_git(local_path, ["git", "rev-parse", "--abbrev-ref", "HEAD"])
        if ok_head and head_branch and not meta["review_branch"]:
            meta["review_branch"] = head_branch

        branch_ref = str(meta["review_branch"] or "").strip()
        range_candidates: list[str] = []
        if branch_ref and branch_ref != "HEAD":
            range_candidates.extend(
                [
                    f"origin/{base_branch}...{branch_ref}",
                    f"{base_branch}...{branch_ref}",
                    f"origin/{base_branch}..{branch_ref}",
                    f"{base_branch}..{branch_ref}",
                ]
            )
        range_candidates.extend(["origin/HEAD..HEAD", "HEAD~1..HEAD"])

        chosen_range = ""
        changed_files: list[str] = []
        diff_text = ""
        last_error = ""
        for rng in range_candidates:
            ok_files, files_out = self._run_git(local_path, ["git", "diff", "--name-only", rng])
            ok_diff, diff_out = self._run_git(local_path, ["git", "diff", "--no-color", rng])
            if not ok_files or not ok_diff:
                last_error = files_out if not ok_files else diff_out
                continue
            chosen_range = rng
            changed_files = [x.strip() for x in files_out.splitlines() if x.strip()]
            diff_text = diff_out
            break

        if not chosen_range:
            ok_files, files_out = self._run_git(local_path, ["git", "diff", "--name-only"])
            ok_diff, diff_out = self._run_git(local_path, ["git", "diff", "--no-color"])
            if ok_files and ok_diff:
                chosen_range = "working-tree"
                changed_files = [x.strip() for x in files_out.splitlines() if x.strip()]
                diff_text = diff_out
            else:
                meta["review_context_error"] = last_error or files_out or diff_out or "failed to collect diff"
                return meta

        max_chars = 120_000
        meta["review_diff_range"] = chosen_range
        meta["review_changed_files"] = changed_files
        meta["review_diff_truncated"] = len(diff_text) > max_chars
        meta["review_diff"] = diff_text[:max_chars]
        if not diff_text.strip():
            meta["review_context_error"] = "empty diff"
        return meta

    @staticmethod
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
