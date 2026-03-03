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
        repo_url="https://github.com/boathell/multiagent.git",
        local_path="/tmp",
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
    assert proc.kill_called is True
    assert proc.communicate_calls >= 2
