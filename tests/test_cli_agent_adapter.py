from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from app.adapters.agents.cli_adapter import CliAgentAdapter
from app.models import IssueContext, Stage, StageStatus


def _ctx() -> IssueContext:
    return IssueContext(
        issue_id="i-1",
        project_id="p-1",
        title="demo",
        description="### Red 阶段\n- case A\n### Green 阶段\n- impl A\n### Refactor 阶段\n- cleanup",
        repo_url="https://github.com/boathell/multiagent.git",
        local_path="/tmp",
        tdd_sections={
            "red": "case A should fail first",
            "green": "minimal impl",
            "refactor": "cleanup naming",
            "acceptance": "all tests pass",
        },
    )


@pytest.mark.asyncio
async def test_cli_adapter_missing_command_fails():
    adapter = CliAgentAdapter({"agents": {"claude": {}}})
    result = await adapter.run_stage(Stage.DESIGN, _ctx())
    assert result.status == StageStatus.FAILED


@pytest.mark.asyncio
async def test_cli_adapter_design_success_from_stdin():
    adapter = CliAgentAdapter(
        {
            "agents": {
                "claude": {
                    "command": sys.executable,
                    "args": ["-c", "import sys; _=sys.stdin.read(); print('design ok')"],
                    "timeout_sec": 5,
                    "prompt_mode": "stdin",
                }
            }
        }
    )
    result = await adapter.run_stage(Stage.DESIGN, _ctx())
    assert result.status == StageStatus.SUCCESS
    assert "design ok" in result.summary


@pytest.mark.asyncio
async def test_cli_adapter_review_needs_changes():
    adapter = CliAgentAdapter(
        {
            "agents": {
                "gemini": {
                    "command": sys.executable,
                    "args": ["-c", "import sys; _=sys.stdin.read(); print('NEEDS_CHANGES: fix style')"],
                    "timeout_sec": 5,
                    "prompt_mode": "stdin",
                }
            }
        }
    )
    result = await adapter.run_stage(Stage.REVIEW, _ctx())
    assert result.status == StageStatus.NEEDS_CHANGES


@pytest.mark.asyncio
async def test_cli_adapter_review_first_line_approved_wins():
    adapter = CliAgentAdapter(
        {
            "agents": {
                "gemini": {
                    "command": sys.executable,
                    "args": [
                        "-c",
                        "print('APPROVED'); print('use NEEDS_CHANGES only for hard blockers')",
                    ],
                    "timeout_sec": 5,
                    "prompt_mode": "stdin",
                }
            }
        }
    )
    result = await adapter.run_stage(Stage.REVIEW, _ctx())
    assert result.status == StageStatus.SUCCESS


@pytest.mark.asyncio
async def test_cli_adapter_arg_prompt_and_cwd(tmp_path: Path):
    ctx = _ctx()
    ctx.local_path = str(tmp_path)
    adapter = CliAgentAdapter(
        {
            "agents": {
                "gemini": {
                    "command": sys.executable,
                    "args": [
                        "-c",
                        "import os,sys;print(sys.argv[-1].splitlines()[0]);print(os.getcwd())",
                    ],
                    "timeout_sec": 5,
                    "prompt_mode": "arg",
                }
            }
        }
    )
    result = await adapter.run_stage(Stage.REVIEW, ctx)
    assert result.status == StageStatus.SUCCESS
    assert "You are an execution agent in a multi-agent pipeline." in result.artifacts["stdout"]
    assert str(tmp_path) in result.artifacts["stdout"]


def test_cli_adapter_build_prompt_contains_tdd_sections():
    prompt = CliAgentAdapter._build_prompt(Stage.CODING, _ctx())
    assert "TDD Template Context:" in prompt
    assert "Respond in Simplified Chinese" in prompt
    assert "TDD_RED:" in prompt
    assert "TDD_GREEN:" in prompt
    assert "TDD_REFACTOR:" in prompt
    assert "RED_RESULT" in prompt
    assert "GREEN_RESULT" in prompt
    assert "REFACTOR_NOTE" in prompt


def test_cli_adapter_design_prompt_requires_tdd_sections():
    prompt = CliAgentAdapter._build_prompt(Stage.DESIGN, _ctx())
    assert "### Red 阶段" in prompt
    assert "### Green 阶段" in prompt
    assert "### Refactor 阶段" in prompt
    assert "### 验收标准（DoD）" in prompt
    assert "All narrative text must be in Simplified Chinese." in prompt


def test_cli_adapter_review_prompt_includes_diff_context():
    ctx = _ctx()
    ctx.metadata.update(
        {
            "review_changed_files": ["src/app/orchestrator.py", "tests/test_orchestrator.py"],
            "review_diff_range": "main...plane/123",
            "review_diff": "diff --git a/a.py b/a.py\n+print('ok')",
        }
    )
    prompt = CliAgentAdapter._build_prompt(Stage.REVIEW, ctx)
    assert "Review Context:" in prompt
    assert "Changed Files: src/app/orchestrator.py, tests/test_orchestrator.py" in prompt
    assert "Diff Range: main...plane/123" in prompt
    assert "Code Diff for Review:" in prompt
    assert "```diff" in prompt
    assert "NO tool access" not in prompt
    assert "default to APPROVED" not in prompt
    assert "If code diff/context is missing, return NEEDS_CHANGES" in prompt


@pytest.mark.asyncio
async def test_cli_adapter_timeout_kills_process(monkeypatch: pytest.MonkeyPatch):
    class DummyProcess:
        def __init__(self) -> None:
            self.returncode = None
            self.kill_called = False
            self.communicate_calls = 0

        async def communicate(self, _input=None):
            self.communicate_calls += 1
            if self.kill_called:
                return b"", b""
            await asyncio.sleep(10)
            return b"", b""

        def kill(self) -> None:
            self.kill_called = True
            self.returncode = -9

    proc = DummyProcess()

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    adapter = CliAgentAdapter(
        {
            "agents": {
                "gemini": {
                    "command": "gemini",
                    "args": ["-p"],
                    "timeout_sec": 1,
                    "prompt_mode": "arg",
                }
            }
        }
    )
    result = await adapter.run_stage(Stage.REVIEW, _ctx())

    assert result.status == StageStatus.FAILED
    assert "timeout after 1s" in result.summary
    assert result.artifacts["command"] == "gemini -p"
    assert result.artifacts["prompt_mode"] == "arg"
    assert result.artifacts["timeout_sec"] == 1
    assert proc.kill_called is True
    assert proc.communicate_calls >= 2
