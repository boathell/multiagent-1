from __future__ import annotations

import asyncio
import os
import shlex
from typing import Any

from app.models import IssueContext, Stage, StageResult, StageStatus


class CliAgentAdapter:
    def __init__(self, agent_config: dict[str, Any] | None = None) -> None:
        self._agent_config = agent_config or {}
        self._agents = self._agent_config.get("agents", {})
        self._stage_agent_map = {
            Stage.DESIGN: "claude",
            Stage.CODING: "codex",
            Stage.REVIEW: "gemini",
        }

    async def run_stage(self, stage: Stage, context: IssueContext) -> StageResult:
        agent_name = self._stage_agent_map[stage]
        cfg = self._agents.get(agent_name, {})
        command = str(cfg.get("command", "")).strip()
        if not command:
            return StageResult(
                status=StageStatus.FAILED,
                summary=f"Agent command not configured for {agent_name}.",
            )

        args = cfg.get("args", [])
        if not isinstance(args, list):
            args = []
        args = [str(x) for x in args]
        env_overrides = cfg.get("env", {})
        child_env = None
        if isinstance(env_overrides, dict) and env_overrides:
            child_env = os.environ.copy()
            for key, value in env_overrides.items():
                child_env[str(key)] = str(value)
        timeout_sec = int(cfg.get("timeout_sec", 300))
        prompt_mode = str(cfg.get("prompt_mode", "stdin")).lower()
        prompt = self._build_prompt(stage, context)

        cmd_parts = shlex.split(command)
        if not cmd_parts:
            return StageResult(
                status=StageStatus.FAILED,
                summary=f"Invalid command for {agent_name}.",
            )

        run_args = [*cmd_parts[1:], *args]
        stdin_data = None
        if prompt_mode == "arg":
            run_args.append(prompt)
        else:
            stdin_data = prompt.encode("utf-8")

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                cmd_parts[0],
                *run_args,
                cwd=context.local_path or None,
                env=child_env,
                stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(stdin_data), timeout=timeout_sec)
        except asyncio.TimeoutError:
            if proc is not None and proc.returncode is None:
                proc.kill()
                try:
                    await proc.communicate()
                except Exception:  # noqa: BLE001
                    pass
            return StageResult(
                status=StageStatus.FAILED,
                summary=f"{agent_name} timeout after {timeout_sec}s.",
            )
        except FileNotFoundError:
            return StageResult(
                status=StageStatus.FAILED,
                summary=f"{agent_name} command not found: {cmd_parts[0]}",
            )
        except Exception as exc:  # noqa: BLE001
            return StageResult(
                status=StageStatus.FAILED,
                summary=f"{agent_name} execution error: {exc}",
            )

        out = (stdout or b"").decode("utf-8", errors="replace").strip()
        err = (stderr or b"").decode("utf-8", errors="replace").strip()
        summary = self._pick_summary(out, err, stage, agent_name)
        status = self._resolve_status(stage, proc.returncode or 0, out)

        return StageResult(
            status=status,
            summary=summary,
            artifacts={
                "agent": agent_name,
                "exit_code": proc.returncode or 0,
                "stdout": out[:2000],
                "stderr": err[:2000],
            },
        )

    @staticmethod
    def _resolve_status(stage: Stage, exit_code: int, stdout: str) -> StageStatus:
        if exit_code != 0:
            return StageStatus.FAILED
        if stage != Stage.REVIEW:
            return StageStatus.SUCCESS

        first_line = ""
        for line in stdout.splitlines():
            stripped = line.strip()
            if stripped:
                first_line = stripped.lower()
                break
        if first_line.startswith("approved"):
            return StageStatus.SUCCESS
        if first_line.startswith("needs_changes") or first_line.startswith("request_changes"):
            return StageStatus.NEEDS_CHANGES

        lowered = stdout.lower()
        if "needs_changes" in lowered or "request_changes" in lowered or "changes requested" in lowered:
            return StageStatus.NEEDS_CHANGES
        return StageStatus.SUCCESS

    @staticmethod
    def _pick_summary(stdout: str, stderr: str, stage: Stage, agent_name: str) -> str:
        first_line = ""
        for text in (stdout, stderr):
            for line in text.splitlines():
                stripped = line.strip()
                if stripped:
                    first_line = stripped
                    break
            if first_line:
                break
        if first_line:
            return f"{agent_name}({stage.value}): {first_line[:240]}"
        return f"{agent_name}({stage.value}) finished."

    @staticmethod
    def _build_prompt(stage: Stage, context: IssueContext) -> str:
        base = [
            "You are an execution agent in a multi-agent pipeline.",
            f"Stage: {stage.value}",
            f"Issue ID: {context.issue_id}",
            f"Project ID: {context.project_id}",
            f"Title: {context.title}",
            f"Repository: {context.repo_url}",
            f"Workspace: {context.local_path}",
        ]

        if stage == Stage.DESIGN:
            base.append(
                "Create an implementation design with concise acceptance criteria and main risks."
            )
        elif stage == Stage.CODING:
            base.append(
                "Implement the issue in the local workspace and report what changed."
            )
        else:
            base.append(
                "Review the changes and output first line exactly one token: APPROVED or NEEDS_CHANGES."
            )
            base.append("You have NO tool access and NO filesystem access in this run. Do NOT call tools.")
            base.append(
                "If concrete code diff is missing, perform lightweight process review and default to APPROVED; "
                "use NEEDS_CHANGES only for clear blockers shown in the prompt."
            )
            base.append("Then explain concise reasons and actionable fixes.")

        return "\n".join(base)
