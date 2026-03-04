from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path


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
    line_limit_violations: list[dict[str, int | str]] = field(default_factory=list)


class QualityGate:
    CODE_EXTENSIONS = {
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".swift",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
    }
    IGNORED_DIRS = {".git", ".venv", ".data", "__pycache__", "node_modules"}

    async def run(
        self,
        commands: list[str],
        workdir: str,
        max_code_file_lines: int | None = None,
    ) -> GateResult:
        results: list[CommandResult] = []
        violations = self._check_code_file_line_limits(workdir, max_code_file_lines)
        if violations:
            return GateResult(ok=False, results=results, line_limit_violations=violations)

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

    def _check_code_file_line_limits(
        self,
        workdir: str,
        max_code_file_lines: int | None,
    ) -> list[dict[str, int | str]]:
        if not max_code_file_lines or max_code_file_lines <= 0:
            return []

        root = Path(workdir)
        violations: list[dict[str, int | str]] = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in self.IGNORED_DIRS for part in path.parts):
                continue
            if path.suffix.lower() not in self.CODE_EXTENSIONS:
                continue

            try:
                with path.open("r", encoding="utf-8", errors="ignore") as f:
                    line_count = sum(1 for _ in f)
            except Exception:  # noqa: BLE001
                continue

            if line_count > max_code_file_lines:
                try:
                    relative = str(path.relative_to(root))
                except ValueError:
                    relative = str(path)
                violations.append({"path": relative, "lines": line_count})

        return sorted(violations, key=lambda x: int(x["lines"]), reverse=True)
