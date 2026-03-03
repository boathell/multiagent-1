from __future__ import annotations

import asyncio
import logging
import re
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
                )
                return {"status": "ignored", "reason": "event filtered", "event_id": event_id}

            await self.process_issue(
                issue_id=issue_id,
                project_id=issue_data["project_id"],
                title=issue_data["title"],
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
    ) -> None:
        project = self.config.get_project(project_id)
        if project is None:
            self.store.append_trace(
                issue_id,
                stage="system",
                status="blocked",
                message=f"No project mapping for project_id={project_id}",
            )
            self.store.upsert_issue(issue_id, project_id, title, PipelineState.BLOCKED.value)
            return

        existing = self.store.get_issue(issue_id)
        if existing is None:
            self.store.upsert_issue(issue_id, project_id, title, PipelineState.TODO.value)
            existing = self.store.get_issue(issue_id)
        assert existing is not None

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
                repo_url=project.repo_url,
                local_path=project.local_path,
                base_branch=project.base_branch,
                branch=issue.get("branch", ""),
                pr_url=issue.get("pr_url", ""),
                attempts=issue.get("attempts", {}),
                review_loops=int(issue.get("review_loops", 0)),
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

    @staticmethod
    def _build_stage_comment(stage: Stage, result: StageResult) -> str:
        stage_zh_map = Orchestrator._stage_zh_map()
        status_zh_map = Orchestrator._status_zh_map()

        human_parts = [
            "[编排器]",
            f"阶段：{stage_zh_map.get(stage, stage.value)}",
            f"状态：{status_zh_map.get(result.status, result.status.value)}",
        ]
        if result.status in {StageStatus.FAILED, StageStatus.NEEDS_CHANGES}:
            human_parts.append(f"原因：{Orchestrator._format_reason(result.summary, result.status)}")
        human_parts.append(f"摘要：{result.summary}")
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
            f"原因：{Orchestrator._format_reason(summary, StageStatus.FAILED)}",
            f"尝试：{attempt}/{total_attempts}",
            f"摘要：{summary}",
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
            f"原因：{Orchestrator._format_reason(result.summary, result.status)}",
            "处理：请修复后调用 internal retry 接口或在 Plane 中重新触发。",
            f"摘要：{result.summary}",
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
            "state": str(state_name),
        }

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
