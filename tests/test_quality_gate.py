from __future__ import annotations

import pytest

from app.quality_gate import QualityGate


@pytest.mark.asyncio
async def test_quality_gate_blocks_when_code_file_exceeds_limit(tmp_path):
    long_file = tmp_path / "too_long.py"
    long_file.write_text("\n".join("x=1" for _ in range(1001)), encoding="utf-8")

    gate = QualityGate()
    result = await gate.run(commands=[], workdir=str(tmp_path), max_code_file_lines=1000)

    assert result.ok is False
    assert result.line_limit_violations
    assert result.line_limit_violations[0]["path"] == "too_long.py"
    assert result.line_limit_violations[0]["lines"] == 1001


@pytest.mark.asyncio
async def test_quality_gate_passes_when_code_file_within_limit(tmp_path):
    ok_file = tmp_path / "ok.py"
    ok_file.write_text("\n".join("x=1" for _ in range(1000)), encoding="utf-8")

    gate = QualityGate()
    result = await gate.run(commands=[], workdir=str(tmp_path), max_code_file_lines=1000)

    assert result.ok is True
    assert result.line_limit_violations == []

