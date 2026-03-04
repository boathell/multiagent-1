from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.comment_utils import (
    build_contract_reminder_comment,
    build_handoff_comment,
    collect_attempted_actions,
)
from app.config import AppConfig
from app.models import FailureClass, IssueContext, PipelineState, Stage, StageResult, StageStatus
from app.orch import comments as orch_comments
from app.orch import events as orch_events
from app.orch import governance as orch_governance
from app.orch import pr_sync as orch_pr_sync
from app.orch import review_context as orch_review_context
from app.quality_gate import QualityGate
from app.review_arbiter import build_arbiter_comment, resolve_review_overflow_with_design
from app.state_machine import next_state, stage_for_state, to_state
from app.store import SQLiteStore
from app.tdd_parser import (
    extract_tdd_sections_from_stage_result,
    parse_issue_contract,
    merge_tdd_sections,
    parse_tdd_sections,
    tdd_missing_sections,
)
from app.workspace_manager import WorkspaceError, WorkspaceManager


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
        self.max_review_loops = self.config.get_review_max_loops()
        self.max_review_arbiter_loops = self.config.get_review_arbiter_max_loops()
        self.max_code_file_lines = max(0, int(self.config.settings.max_code_file_lines))
        self.human_handoff_enabled = bool(self.config.settings.human_handoff_enabled)
        mode = str(self.config.settings.tdd_enforcement_mode or "strict").strip().lower()
        self.tdd_enforcement_mode = mode if mode in {"advisory", "strict"} else "strict"
        self.issue_max_concurrency = self.config.get_issue_max_concurrency()
        self.issue_worktree_enabled = bool(self.config.settings.issue_worktree_enabled)
        self.issue_worktree_root = self.config.get_issue_worktree_root()
        self.issue_worktree_cleanup_enabled = bool(self.config.settings.issue_worktree_cleanup_enabled)
        self.issue_worktree_retention_hours = self.config.get_issue_worktree_retention_hours()
        self._issue_locks: dict[str, asyncio.Lock] = {}
        self._issue_semaphore = asyncio.Semaphore(self.issue_max_concurrency)
        self.workspace_manager = WorkspaceManager(worktree_root=self.issue_worktree_root)

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

            async with self._issue_semaphore:
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

            async with self._issue_semaphore:
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

        contract = parse_issue_contract(str(existing.get("description", "")))
        existing_failure_class = str(existing.get("failure_class", "")).strip()
        if not existing_failure_class:
            self.store.update_issue_fields(issue_id, failure_class="")
        if contract.missing_fields and not self._has_contract_reminder(issue_id):
            contract_comment = build_contract_reminder_comment(
                score=contract.score,
                missing_fields=contract.missing_fields,
            )
            self.store.append_trace(
                issue_id,
                stage="contract",
                status="advisory",
                message="Issue contract missing fields.",
                metadata={"score": contract.score, "missing_fields": contract.missing_fields},
            )
            await self.plane_client.add_comment(
                project_id=project_id,
                issue_id=issue_id,
                comment=contract_comment,
            )

        risk_keywords = self._detect_high_risk_keywords(str(existing.get("description", "")))
        if risk_keywords and self.human_handoff_enabled:
            handoff_result = StageResult(
                status=StageStatus.FAILED,
                summary="High-risk keywords require human handoff.",
                artifacts={
                    "failure_class": FailureClass.QUALITY_ISSUE.value,
                    "handoff_reason": "high_risk_keywords",
                    "risk_keywords": risk_keywords,
                },
            )
            self.store.append_trace(
                issue_id=issue_id,
                stage="design",
                status="failed",
                message=handoff_result.summary,
                metadata=handoff_result.artifacts,
            )
            handoff_comment = build_handoff_comment(
                issue_id=issue_id,
                stage=Stage.DESIGN,
                failure_class=FailureClass.QUALITY_ISSUE.value,
                reason=f"命中高风险关键词：{', '.join(risk_keywords)}",
                attempted=["未启动自动执行"],
                suggested_actions=[
                    "请人工确认风险评估与回滚预案",
                    "必要时拆分低风险子任务后再重试",
                ],
            )
            await self.plane_client.add_comment(
                project_id=project_id,
                issue_id=issue_id,
                comment=handoff_comment,
            )
            self.store.update_issue_fields(
                issue_id,
                state=PipelineState.BLOCKED.value,
                failure_class=FailureClass.QUALITY_ISSUE.value,
                handoff_reason="high_risk_keywords",
            )
            await self._set_state(
                issue_id,
                project_id,
                PipelineState.BLOCKED,
                "High-risk keywords require human handoff.",
                project.state_map,
            )
            return

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

        try:
            workspace_path = self._resolve_issue_workspace(
                issue_id=issue_id,
                project_id=project_id,
                project_local_path=project.local_path,
                base_branch=project.base_branch,
            )
        except WorkspaceError as exc:
            await self._handle_workspace_prepare_failure(
                issue_id=issue_id,
                project_id=project_id,
                project=project,
                error=str(exc),
            )
            return
        self.store.update_issue_fields(issue_id, workspace_path=workspace_path)

        while True:
            stage = stage_for_state(current_state)
            if stage is None:
                break

            result = await self._run_stage_with_retry(
                stage,
                issue_id,
                project_id,
                title,
                project,
                workspace_path,
            )
            failure_class = self._classify_failure(stage, result)
            if failure_class:
                result.artifacts.setdefault("failure_class", failure_class)
            result = self._apply_review_evidence_gate(stage, result)
            next_pipeline_state = next_state(current_state, result.status)

            if stage == Stage.DESIGN and result.status == StageStatus.SUCCESS:
                issue_record = self.store.get_issue(issue_id) or {}
                current_desc = str(issue_record.get("description", ""))
                generated_sections = extract_tdd_sections_from_stage_result(result)
                merged_desc, merged_sections, merged = merge_tdd_sections(
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

                missing_sections = tdd_missing_sections(merged_sections)
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
                    (
                        result,
                        next_pipeline_state,
                        arbiter_trace,
                    ) = await self._resolve_review_overflow_with_design(
                        issue_id=issue_id,
                        project_id=project_id,
                        title=title,
                        project=project,
                        review_result=result,
                        review_loops=loops,
                    )
                    if arbiter_trace is not None:
                        self.store.append_trace(
                            issue_id,
                            stage="design_arbiter",
                            status=arbiter_trace["status"],
                            message=arbiter_trace["message"],
                            metadata=arbiter_trace["metadata"],
                        )
                        await self.plane_client.add_comment(
                            project_id=project_id,
                            issue_id=issue_id,
                            comment=self._build_arbiter_comment(arbiter_trace),
                        )
                    failure_class = self._classify_failure(stage, result)
                    if failure_class:
                        result.artifacts["failure_class"] = failure_class

            handoff_reason = ""
            handoff_comment = ""
            handoff_meta = self._evaluate_handoff_trigger(
                issue_id=issue_id,
                stage=stage,
                result=result,
            )
            if handoff_meta is not None and self.human_handoff_enabled:
                handoff_reason = str(handoff_meta["handoff_reason"])
                failure_class = str(handoff_meta["failure_class"])
                result.artifacts["failure_class"] = failure_class
                result.artifacts["handoff_reason"] = handoff_reason
                result.artifacts["handoff_trigger"] = str(handoff_meta["trigger"])
                if result.status == StageStatus.NEEDS_CHANGES:
                    result = StageResult(
                        status=StageStatus.FAILED,
                        summary=f"Human handoff required: {handoff_reason}",
                        artifacts={**result.artifacts},
                    )
                next_pipeline_state = PipelineState.BLOCKED
                handoff_comment = build_handoff_comment(
                    issue_id=issue_id,
                    stage=stage,
                    failure_class=failure_class,
                    reason=handoff_reason,
                    attempted=collect_attempted_actions(result.artifacts),
                    suggested_actions=handoff_meta["suggested_actions"],
                )

            self.store.append_trace(
                issue_id,
                stage=stage.value,
                status=result.status.value,
                message=result.summary,
                metadata=result.artifacts,
            )
            if failure_class:
                self.store.update_issue_fields(issue_id, failure_class=failure_class)

            comment = self._build_stage_comment(stage, result)
            await self.plane_client.add_comment(project_id=project_id, issue_id=issue_id, comment=comment)

            if stage == Stage.REVIEW and result.status in {StageStatus.NEEDS_CHANGES, StageStatus.FAILED}:
                fix_comment = self._build_review_fix_comment(result)
                await self.plane_client.add_comment(
                    project_id=project_id,
                    issue_id=issue_id,
                    comment=fix_comment,
                )
                await self._sync_review_feedback_to_pr(
                    pr_url=str((self.store.get_issue(issue_id) or {}).get("pr_url", "")),
                    local_path=workspace_path,
                    comment=fix_comment,
                )

            if stage == Stage.CODING and result.status == StageStatus.SUCCESS:
                pr_url = result.artifacts.get("pr_url", "")
                if pr_url:
                    self.store.update_issue_fields(issue_id, pr_url=pr_url)
                    await self._sync_tdd_summary_to_pr(
                        issue_id=issue_id,
                        local_path=workspace_path,
                        pr_url=str(pr_url),
                        coding_result=result,
                    )

            await self._set_state(
                issue_id,
                project_id,
                next_pipeline_state,
                f"Stage {stage.value}: {result.summary}",
                project.state_map,
            )
            current_state = next_pipeline_state

            if current_state == PipelineState.BLOCKED:
                if handoff_comment:
                    await self.plane_client.add_comment(
                        project_id=project_id,
                        issue_id=issue_id,
                        comment=handoff_comment,
                    )
                    self.store.update_issue_fields(
                        issue_id,
                        handoff_reason=handoff_reason,
                    )
                blocked_comment = self._build_blocked_comment(stage, result)
                await self.plane_client.add_comment(
                    project_id=project_id,
                    issue_id=issue_id,
                    comment=blocked_comment,
                )

            if current_state in {PipelineState.DONE, PipelineState.BLOCKED}:
                await self._cleanup_stale_issue_worktrees(project=project)
                break

    async def _run_stage_with_retry(
        self,
        stage: Stage,
        issue_id: str,
        project_id: str,
        title: str,
        project,
        workspace_path: str,
    ) -> StageResult:
        total_attempts = self.max_retries + 1
        last_result = StageResult(status=StageStatus.FAILED, summary="No execution")
        attempt_failure_classes: list[str] = []
        protocol_violation_attempts = 0
        tool_failure_attempts = 0

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
                local_path=workspace_path,
                base_branch=project.base_branch,
                branch=issue.get("branch", ""),
                pr_url=issue.get("pr_url", ""),
                attempts=issue.get("attempts", {}),
                review_loops=int(issue.get("review_loops", 0)),
                arbiter_loops=int(issue.get("arbiter_loops", 0)),
                failure_class=str(issue.get("failure_class", "")),
                handoff_reason=str(issue.get("handoff_reason", "")),
                tdd_sections=parse_tdd_sections(str(issue.get("description", ""))),
                issue_contract=parse_issue_contract(str(issue.get("description", ""))),
            )
            if stage == Stage.REVIEW:
                context.metadata.update(
                    self._collect_review_context(
                        local_path=workspace_path,
                        base_branch=project.base_branch,
                        branch=context.branch,
                    )
                )

            self.store.update_issue_fields(issue_id, last_stage=stage.value)
            result = await self.agent_adapter.run_stage(stage, context)
            result = self._validate_stage_protocol(stage, result)
            if stage == Stage.REVIEW:
                result.artifacts["review_changed_files"] = context.metadata.get("review_changed_files", [])
                result.artifacts["review_diff_range"] = context.metadata.get("review_diff_range", "")
                result.artifacts["review_diff_truncated"] = bool(context.metadata.get("review_diff_truncated"))
                result.artifacts["review_context_error"] = str(
                    context.metadata.get("review_context_error", "")
                ).strip()
                result.artifacts["review_diff_files_included"] = context.metadata.get(
                    "review_diff_files_included",
                    [],
                )

            if stage == Stage.CODING and result.status == StageStatus.SUCCESS:
                gate_result = await self.quality_gate.run(
                    project.checks,
                    workspace_path,
                    max_code_file_lines=self.max_code_file_lines,
                )
                if not gate_result.ok:
                    artifacts: dict[str, Any] = {
                        "checks": [
                            {
                                "command": x.command,
                                "exit_code": x.exit_code,
                            }
                            for x in gate_result.results
                        ],
                    }
                    if gate_result.line_limit_violations:
                        artifacts["line_limit_violations"] = gate_result.line_limit_violations
                        result = StageResult(
                            status=StageStatus.FAILED,
                            summary=f"Code file line limit exceeded ({self.max_code_file_lines}).",
                            artifacts=artifacts,
                        )
                    else:
                        result = StageResult(
                            status=StageStatus.FAILED,
                            summary="Quality gate failed after coding stage.",
                            artifacts=artifacts,
                        )

            if stage == Stage.CODING and result.status == StageStatus.SUCCESS:
                pr = self.github_client.create_branch_commit_and_pr(
                    issue_id=issue_id,
                    title=title,
                    body="Generated by multi-agent orchestrator",
                    local_path=workspace_path,
                    base_branch=project.base_branch,
                    repo_url=project.repo_url,
                )
                result.artifacts["branch"] = pr.branch
                result.artifacts["pr_url"] = pr.pr_url
                self.store.update_issue_fields(issue_id, branch=pr.branch, pr_url=pr.pr_url)

            if result.status != StageStatus.FAILED:
                self.store.reset_attempt(issue_id, stage.value)
                return result

            failure_class = self._classify_failure(stage, result)
            if failure_class:
                result.artifacts.setdefault("failure_class", failure_class)
                attempt_failure_classes.append(failure_class)
            if failure_class in {FailureClass.GEMINI_ISSUE.value, FailureClass.ENV_ISSUE.value}:
                tool_failure_attempts += 1
            if failure_class == FailureClass.PROTOCOL_VIOLATION.value:
                protocol_violation_attempts += 1
            result.artifacts["attempt_failure_classes"] = attempt_failure_classes.copy()
            result.artifacts["tool_failure_attempts"] = tool_failure_attempts
            result.artifacts["protocol_violation_attempts"] = protocol_violation_attempts

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

        last_result.artifacts["attempt_failure_classes"] = attempt_failure_classes.copy()
        last_result.artifacts["tool_failure_attempts"] = tool_failure_attempts
        last_result.artifacts["protocol_violation_attempts"] = protocol_violation_attempts
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

    def _validate_stage_protocol(self, stage: Stage, result: StageResult) -> StageResult:
        return orch_governance.validate_stage_protocol(
            stage=stage,
            result=result,
            tdd_enforcement_mode=self.tdd_enforcement_mode,
        )

    @staticmethod
    def _first_non_empty_line(text: str) -> str:
        return orch_governance.first_non_empty_line(text)

    def _apply_review_evidence_gate(self, stage: Stage, result: StageResult) -> StageResult:
        return orch_governance.apply_review_evidence_gate(
            stage=stage,
            result=result,
            agents_use_mock=bool(self.config.settings.agents_use_mock),
        )

    @staticmethod
    def _review_mentions_key_files(stdout: str, changed_files: list[Any]) -> bool:
        return orch_governance.review_mentions_key_files(stdout, changed_files)

    def _classify_failure(self, stage: Stage, result: StageResult) -> str:
        return orch_governance.classify_failure(stage, result)

    def _evaluate_handoff_trigger(
        self,
        *,
        issue_id: str,
        stage: Stage,
        result: StageResult,
    ) -> dict[str, Any] | None:
        return orch_governance.evaluate_handoff_trigger(
            stage=stage,
            result=result,
            traces=self.store.get_trace(issue_id),
            human_handoff_enabled=self.human_handoff_enabled,
        )

    @staticmethod
    def _build_review_fix_comment(result: StageResult) -> str:
        return orch_comments.build_review_fix_comment(result)

    async def _sync_tdd_summary_to_pr(
        self,
        *,
        issue_id: str,
        local_path: str,
        pr_url: str,
        coding_result: StageResult,
    ) -> None:
        await orch_pr_sync.sync_tdd_summary_to_pr(
            store=self.store,
            github_client=self.github_client,
            logger=self.logger,
            issue_id=issue_id,
            local_path=local_path,
            pr_url=pr_url,
            coding_result=coding_result,
        )

    async def _sync_review_feedback_to_pr(self, *, pr_url: str, local_path: str, comment: str) -> None:
        await orch_pr_sync.sync_review_feedback_to_pr(
            github_client=self.github_client,
            logger=self.logger,
            pr_url=pr_url,
            local_path=local_path,
            comment=comment,
        )

    def _resolve_issue_workspace(
        self,
        *,
        issue_id: str,
        project_id: str,
        project_local_path: str,
        base_branch: str,
    ) -> str:
        if not self.issue_worktree_enabled:
            return project_local_path
        return self.workspace_manager.prepare_workspace(
            project_local_path=project_local_path,
            project_id=project_id,
            issue_id=issue_id,
            base_branch=base_branch,
        )

    async def _handle_workspace_prepare_failure(
        self,
        *,
        issue_id: str,
        project_id: str,
        project,
        error: str,
    ) -> None:
        failure_class = FailureClass.ENV_ISSUE.value
        message = f"Issue workspace prepare failed: {error}"
        self.store.append_trace(
            issue_id=issue_id,
            stage="workspace",
            status="failed",
            message=message,
            metadata={
                "failure_class": failure_class,
                "workspace_root": str(self.issue_worktree_root),
            },
        )
        self.store.update_issue_fields(
            issue_id,
            state=PipelineState.BLOCKED.value,
            failure_class=failure_class,
            handoff_reason="workspace_prepare_failed",
        )

        handoff_comment = build_handoff_comment(
            issue_id=issue_id,
            stage=Stage.CODING,
            failure_class=failure_class,
            reason=f"Issue 独立工作区初始化失败：{error}",
            attempted=["准备 issue 专属 git worktree"],
            suggested_actions=[
                "请确认仓库目录可写且是有效 git 仓库",
                "检查 git worktree 功能是否可用后重试",
            ],
        )
        await self.plane_client.add_comment(
            project_id=project_id,
            issue_id=issue_id,
            comment=handoff_comment,
        )
        await self._set_state(
            issue_id=issue_id,
            project_id=project_id,
            state=PipelineState.BLOCKED,
            message=message,
            state_map=project.state_map,
        )

    async def _cleanup_stale_issue_worktrees(self, *, project) -> None:
        if not self.issue_worktree_enabled or not self.issue_worktree_cleanup_enabled:
            return

        try:
            active_paths = self.store.list_active_workspace_paths()
            summary = self.workspace_manager.cleanup_stale_worktrees(
                project_local_path=project.local_path,
                active_workspace_paths=active_paths,
                retention_hours=self.issue_worktree_retention_hours,
            )
        except WorkspaceError as exc:
            self.logger.warning("Issue worktree cleanup skipped: %s", exc)
            return

        removed = int(summary.get("removed", 0))
        errors = summary.get("errors", [])
        if removed > 0:
            self.logger.info(
                "Issue worktree cleanup removed=%s retention_hours=%s",
                removed,
                self.issue_worktree_retention_hours,
            )
        if isinstance(errors, list) and errors:
            self.logger.warning("Issue worktree cleanup errors: %s", " | ".join(str(x) for x in errors[:5]))

    @staticmethod
    def _extract_named_section(text: str, token: str) -> str:
        return orch_pr_sync.extract_named_section(text, token)

    @staticmethod
    def _build_stage_comment(stage: Stage, result: StageResult) -> str:
        return orch_comments.build_stage_comment(stage, result)

    @staticmethod
    def _build_retry_comment(stage: Stage, attempt: int, total_attempts: int, summary: str) -> str:
        return orch_comments.build_retry_comment(stage, attempt, total_attempts, summary)

    @staticmethod
    def _build_blocked_comment(stage: Stage, result: StageResult) -> str:
        return orch_comments.build_blocked_comment(stage, result)

    @staticmethod
    def _build_arbiter_comment(arbiter_trace: dict[str, Any]) -> str:
        return build_arbiter_comment(arbiter_trace)

    async def _resolve_review_overflow_with_design(
        self,
        issue_id: str,
        project_id: str,
        title: str,
        project,
        review_result: StageResult,
        review_loops: int,
    ) -> tuple[StageResult, PipelineState, dict[str, Any] | None]:
        return await resolve_review_overflow_with_design(
            store=self.store,
            agent_adapter=self.agent_adapter,
            issue_id=issue_id,
            project_id=project_id,
            title=title,
            project=project,
            review_result=review_result,
            review_loops=review_loops,
            max_review_loops=self.max_review_loops,
            max_review_arbiter_loops=self.max_review_arbiter_loops,
        )

    @staticmethod
    def extract_event_id(payload: dict[str, Any]) -> str:
        return orch_events.extract_event_id(payload)

    @staticmethod
    def extract_event_type(payload: dict[str, Any]) -> str:
        return orch_events.extract_event_type(payload)

    @staticmethod
    def extract_issue(payload: dict[str, Any]) -> dict[str, str] | None:
        return orch_events.extract_issue(payload)

    @staticmethod
    def _extract_issue_description(item: dict[str, Any]) -> str:
        return orch_events.extract_issue_description(item)

    @staticmethod
    def _normalize_description(raw: str) -> str:
        return orch_events.normalize_description(raw)

    def _has_tdd_reminder(self, issue_id: str) -> bool:
        traces = self.store.get_trace(issue_id)
        return any(t.get("stage") == "tdd" and t.get("status") == "reminder" for t in traces)

    def _has_contract_reminder(self, issue_id: str) -> bool:
        traces = self.store.get_trace(issue_id)
        return any(t.get("stage") == "contract" and t.get("status") == "advisory" for t in traces)

    @staticmethod
    def _detect_high_risk_keywords(description: str) -> list[str]:
        return orch_governance.detect_high_risk_keywords(description)

    @staticmethod
    def _description_to_html(description: str) -> str:
        return orch_events.description_to_html(description)

    @staticmethod
    def _run_git(local_path: str, args: list[str]) -> tuple[bool, str]:
        return orch_review_context.run_git(local_path, args)

    def _collect_review_context(self, local_path: str, base_branch: str, branch: str) -> dict[str, Any]:
        return orch_review_context.collect_review_context(local_path, base_branch, branch)

    def _collect_diff_by_files(
        self,
        local_path: str,
        diff_range: str,
        changed_files: list[str],
        max_chars: int,
    ) -> tuple[str, list[str], bool]:
        return orch_review_context.collect_diff_by_files(
            local_path=local_path,
            diff_range=diff_range,
            changed_files=changed_files,
            max_chars=max_chars,
        )

    @staticmethod
    def _prioritize_review_files(changed_files: list[str]) -> list[str]:
        return orch_review_context.prioritize_review_files(changed_files)

    @staticmethod
    def should_start_pipeline(event_type: str, issue_data: dict[str, str]) -> bool:
        return orch_events.should_start_pipeline(event_type, issue_data)
