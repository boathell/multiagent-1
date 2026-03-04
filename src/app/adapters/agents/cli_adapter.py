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
        self._tdd_enabled = bool(self._agent_config.get("tdd_enabled", True))
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
        context.metadata["tdd_enabled"] = self._tdd_enabled
        prompt = self._build_prompt(stage, context)
        command_line = " ".join([command, *args]).strip()

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
                artifacts={
                    "agent": agent_name,
                    "command": command_line,
                    "prompt_mode": prompt_mode,
                    "timeout_sec": timeout_sec,
                    "cwd": context.local_path,
                },
            )
        except FileNotFoundError:
            return StageResult(
                status=StageStatus.FAILED,
                summary=f"{agent_name} command not found: {cmd_parts[0]}",
                artifacts={
                    "agent": agent_name,
                    "command": command_line,
                    "prompt_mode": prompt_mode,
                    "timeout_sec": timeout_sec,
                    "cwd": context.local_path,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return StageResult(
                status=StageStatus.FAILED,
                summary=f"{agent_name} execution error: {exc}",
                artifacts={
                    "agent": agent_name,
                    "command": command_line,
                    "prompt_mode": prompt_mode,
                    "timeout_sec": timeout_sec,
                    "cwd": context.local_path,
                },
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
                "command": command_line,
                "prompt_mode": prompt_mode,
                "timeout_sec": timeout_sec,
                "cwd": context.local_path,
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
            "Respond in Simplified Chinese unless strict control tokens are required.",
            f"Stage: {stage.value}",
            f"Issue ID: {context.issue_id}",
            f"Project ID: {context.project_id}",
            f"Title: {context.title}",
            f"Repository: {context.repo_url}",
            f"Workspace: {context.local_path}",
        ]
        if context.metadata.get("tdd_enabled", True):
            base.extend(CliAgentAdapter._tdd_prompt_lines(context))

        if stage == Stage.DESIGN:
            if str(context.metadata.get("design_mode", "")).strip().lower() == "review_arbiter":
                base.extend(CliAgentAdapter._review_arbiter_prompt_lines(context))
                base.append("All narrative text must be in Simplified Chinese.")
                base.append("First non-empty line must be exactly one token: CONTINUE_CODING or STOP_REVIEW.")
                base.append("Then output exactly 3 lines with prefixes: DIAGNOSIS:, REASON:, ACTIONS:.")
                base.append("DIAGNOSIS must be exactly one token: QUALITY_ISSUE or GEMINI_ISSUE.")
                return "\n".join(base)
            base.append(
                "You own test and implementation design. If Red/Green/Refactor content is missing, generate it."
            )
            base.append("All narrative text must be in Simplified Chinese.")
            base.append(
                "Output markdown headings exactly: ### Red 阶段, ### Green 阶段, "
                "### Refactor 阶段, ### 验收标准（DoD）."
            )
            base.append("Also include concise GAPS and RISKS bullet points.")
        elif stage == Stage.CODING:
            base.append(
                "Follow TDD sequence strictly: RED (failing tests) -> GREEN (minimal implementation) -> REFACTOR."
            )
            base.append("All narrative text must be in Simplified Chinese.")
            base.append("Output concise sections: RED_RESULT, GREEN_RESULT, REFACTOR_NOTE, CHANGED_FILES.")
        else:
            base.extend(CliAgentAdapter._review_context_lines(context))
            base.append(
                "Review the changes and output first line exactly one token: APPROVED or NEEDS_CHANGES."
            )
            base.append("From the second line onward, write in Simplified Chinese.")
            base.append("You should perform strict code review based on provided git diff and changed files.")
            base.append("Verify TDD evidence completeness and that RED->GREEN->REFACTOR order is respected.")
            base.append("If code diff/context is missing, return NEEDS_CHANGES and explain missing evidence.")
            base.append("Then explain concise reasons and actionable fixes.")

        return "\n".join(base)

    @staticmethod
    def _tdd_prompt_lines(context: IssueContext) -> list[str]:
        lines = ["TDD Template Context:"]
        if context.description.strip():
            lines.append(f"Issue Description (normalized, truncated): {context.description[:1200]}")
        else:
            lines.append("Issue Description: (empty)")

        sections = context.tdd_sections or {}
        for key in ("red", "green", "refactor", "acceptance"):
            value = str(sections.get(key, "")).strip()
            if value:
                lines.append(f"TDD_{key.upper()}: {value[:800]}")
            else:
                lines.append(f"TDD_{key.upper()}: (missing)")
        return lines

    @staticmethod
    def _review_context_lines(context: IssueContext) -> list[str]:
        lines = ["Review Context:"]
        changed_files = context.metadata.get("review_changed_files")
        if isinstance(changed_files, list) and changed_files:
            joined = ", ".join(str(x) for x in changed_files[:80])
            lines.append(f"Changed Files: {joined}")
        else:
            lines.append("Changed Files: (none)")

        diff_range = str(context.metadata.get("review_diff_range", "")).strip()
        if diff_range:
            lines.append(f"Diff Range: {diff_range}")

        diff_text = str(context.metadata.get("review_diff", "")).strip()
        included = context.metadata.get("review_diff_files_included")
        if isinstance(included, list) and included:
            lines.append(f"Diff Files Included: {', '.join(str(x) for x in included[:40])}")
        if diff_text:
            lines.append("Code Diff for Review:")
            lines.append("```diff")
            lines.append(diff_text)
            lines.append("```")
            if context.metadata.get("review_diff_truncated"):
                lines.append("Diff Notice: truncated due to size; treat as partial evidence.")
        else:
            lines.append("Code Diff for Review: (missing)")

        err = str(context.metadata.get("review_context_error", "")).strip()
        if err:
            lines.append(f"Review Context Error: {err}")
        return lines

    @staticmethod
    def _review_arbiter_prompt_lines(context: IssueContext) -> list[str]:
        lines = ["Review Arbiter Context:"]
        lines.append(f"Review Loop Count: {context.review_loops}")
        lines.append(f"Arbiter Loop Count: {context.arbiter_loops}")

        failure_summary = str(context.metadata.get("review_failure_summary", "")).strip()
        if failure_summary:
            lines.append(f"Latest Review Failure Summary: {failure_summary[:400]}")

        failure_stdout = str(context.metadata.get("review_failure_stdout", "")).strip()
        if failure_stdout:
            lines.append(f"Latest Review Failure Stdout: {failure_stdout[:800]}")

        failure_stderr = str(context.metadata.get("review_failure_stderr", "")).strip()
        if failure_stderr:
            lines.append(f"Latest Review Failure Stderr: {failure_stderr[:800]}")

        signals = context.metadata.get("review_failure_signals")
        if isinstance(signals, list) and signals:
            lines.append(f"Failure Signals: {', '.join(str(x) for x in signals[:20])}")
        else:
            lines.append("Failure Signals: (none)")
        return lines
