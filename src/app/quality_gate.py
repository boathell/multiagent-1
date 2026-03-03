from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class GateResult:
    ok: bool
    results: list[CommandResult]


class QualityGate:
    async def run(self, commands: list[str], workdir: str) -> GateResult:
        results: list[CommandResult] = []
        if not commands:
            return GateResult(ok=True, results=results)

        for cmd in commands:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            result = CommandResult(
                command=cmd,
                exit_code=proc.returncode or 0,
                stdout=(stdout or b"").decode("utf-8", errors="replace"),
                stderr=(stderr or b"").decode("utf-8", errors="replace"),
            )
            results.append(result)
            if result.exit_code != 0:
                return GateResult(ok=False, results=results)

        return GateResult(ok=True, results=results)

