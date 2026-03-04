from __future__ import annotations

from pathlib import Path

import pytest

from app.models import Stage, StageResult, StageStatus
from app.orchestrator import Orchestrator
from app.quality_gate import QualityGate
from app.store import SQLiteStore
from app.tdd_parser import (
    extract_tdd_sections_from_stage_result,
    merge_tdd_sections,
    parse_tdd_sections,
    tdd_missing_sections,
)
from .helpers import FakeGitHubClient, FakePlaneClient, ScriptedAgent


def test_extract_event_id_handles_non_dict_data():
    payload = {"event": "ping", "data": "plain-text-payload"}
    event_id = Orchestrator.extract_event_id(payload)
    assert event_id.startswith("ping:unknown:")


def test_extract_issue_includes_description():
    payload = {
        "event": "work_item.created",
        "data": {
            "work_item": {
                "id": "1201",
                "project_id": "p1",
                "name": "TDD task",
                "state_name": "Todo",
                "description_html": "<h3>Red 阶段</h3><p>case A</p><h3>Green 阶段</h3><p>impl A</p>",
            }
        },
    }
    issue = Orchestrator.extract_issue(payload)
    assert issue is not None
    assert "Red 阶段" in issue["description"]
    assert "Green 阶段" in issue["description"]


@pytest.mark.asyncio
async def test_contract_missing_adds_advisory_comment(make_config, tmp_path: Path):
    config = make_config(project_id="p1")
    store = SQLiteStore(str(tmp_path / "db-contract.sqlite"))
    plane = FakePlaneClient()
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=plane,
        github_client=FakeGitHubClient(),
        agent_adapter=ScriptedAgent(),
        quality_gate=QualityGate(),
    )

    await orch.process_issue(
        issue_id="contract-1",
        project_id="p1",
        title="contract advisory",
        force=False,
        description="## 目标\n- 只给目标，不给范围和回滚",
    )
    assert any("[TDD-Contract-提醒]" in comment for _, _, comment in plane.comments)


def test_parse_tdd_sections_cn_template():
    description = """
### Red 阶段（先失败）
- T1
### Green 阶段（最小实现）
- G1
### Refactor 阶段（重构）
- R1
### 验收标准（DoD）
- A1
"""
    sections = parse_tdd_sections(description)
    assert "T1" in sections["red"]
    assert "G1" in sections["green"]
    assert "R1" in sections["refactor"]
    assert "A1" in sections["acceptance"]


def test_parse_tdd_sections_keeps_red_case_content():
    description = """
### Red 阶段
- RED-001: invalid params should fail
### Green 阶段
- add minimal validation
### Refactor 阶段
- extract helper
"""
    sections = parse_tdd_sections(description)
    assert "RED-001" in sections["red"]
    assert "minimal validation" in sections["green"]
    assert "extract helper" in sections["refactor"]


def test_parse_tdd_sections_ignores_inline_red_green_refactor_mentions():
    description = """
1. 背景与目标
验证 Design 阶段自动补全 Red/Green/Refactor，不由人工填写。
2. 范围 / 非目标
- 范围：验证编排行为与评论
- 非目标：不修改业务功能
"""
    sections = parse_tdd_sections(description)
    assert sections["red"] == ""
    assert sections["green"] == ""
    assert sections["refactor"] == ""


def test_design_stage_extracts_tdd_sections():
    stdout = (
        "TDD_RED: | 测试ID | 文件 |\n"
        "|---|---|\n"
        "| RED-001 | tests/test_orchestrator.py |\n"
        "TDD_GREEN: | 模块 | 最小改动 |\n"
        "|---|---|\n"
        "| src/app/orchestrator.py | 实现最小改动 |\n"
        "TDD_REFACTOR: | 目标 | 风险 |\n"
        "|---|---|\n"
        "| 提取方法 | 导入路径更新 |\n"
        "TDD_ACCEPTANCE: - [x] 所有新增 Red 用例先失败后通过"
    )
    result = StageResult(
        status=StageStatus.SUCCESS,
        summary="design done",
        artifacts={"stdout": stdout},
    )
    sections = extract_tdd_sections_from_stage_result(result)
    assert "RED-001" in sections["red"]
    assert "最小改动" in sections["green"]
    assert "导入路径更新" in sections["refactor"]
    assert "先失败后通过" in sections["acceptance"]


def test_merge_tdd_sections_appends_missing():
    base_description = "## 背景\n仅有业务背景，无 TDD 细节。"
    result = StageResult(
        status=StageStatus.SUCCESS,
        summary="design done",
        artifacts={
            "stdout": (
                "TDD_RED: - RED-001: 缺少提取逻辑\n"
                "TDD_GREEN: - 实现 `_extract_tdd_sections_from_stage_result`\n"
                "TDD_REFACTOR: - 提取公共解析函数"
            )
        },
    )
    generated = extract_tdd_sections_from_stage_result(result)
    merged_desc, merged_sections, merged = merge_tdd_sections(
        base_description=base_description,
        generated_sections=generated,
    )
    assert merged is True
    assert "### Red 阶段" in merged_desc
    assert "RED-001" in merged_sections["red"]
    assert "提取公共解析函数" in merged_sections["refactor"]


def test_tdd_missing_sections_detects_absence():
    result = StageResult(
        status=StageStatus.SUCCESS,
        summary="design done",
        artifacts={
            "stdout": (
                "TDD_RED: - RED-001: 失败用例\n"
                "TDD_GREEN: - 通过 RED-001\n"
                "TDD_ACCEPTANCE: - [x] 回归通过"
            )
        },
    )
    sections = extract_tdd_sections_from_stage_result(result)
    missing = tdd_missing_sections(sections)
    assert missing == ["refactor"]


def test_description_to_html_renders_headings_and_list():
    description = "### Red 阶段\n- case 1\n普通说明"
    html = Orchestrator._description_to_html(description)
    assert "<h3>Red 阶段</h3>" in html
    assert "<li>case 1</li>" in html
    assert "<p>普通说明</p>" in html


@pytest.mark.asyncio
async def test_tdd_sections_sync_to_plane(make_config, tmp_path: Path):
    config = make_config(project_id="p1")
    store = SQLiteStore(str(tmp_path / "db-sync.sqlite"))
    plane = FakePlaneClient()
    agent = ScriptedAgent(
        {
            Stage.DESIGN: [
                StageResult(
                    status=StageStatus.SUCCESS,
                    summary="design with inline tdd tokens",
                    artifacts={
                        "stdout": (
                            "TDD_RED: - RED-001: 先写失败测试\n"
                            "TDD_GREEN: - 最小实现通过失败测试\n"
                            "TDD_REFACTOR: - 抽取复用函数\n"
                            "TDD_ACCEPTANCE: - [x] 回归通过"
                        )
                    },
                )
            ]
        }
    )
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=plane,
        github_client=FakeGitHubClient(),
        agent_adapter=agent,
        quality_gate=QualityGate(),
    )

    await orch.process_issue(
        issue_id="1010",
        project_id="p1",
        title="sync desc",
        force=False,
        description="只有背景说明",
    )

    issue = store.get_issue("1010")
    assert issue is not None
    sections = parse_tdd_sections(issue["description"])
    assert "RED-001" in sections["red"]
    assert len(plane.description_updates) == 1
    assert "Red 阶段" in plane.description_updates[0][2]


@pytest.mark.asyncio
async def test_design_success_triggers_plane_desc_sync(make_config, tmp_path: Path):
    await test_tdd_sections_sync_to_plane(make_config, tmp_path)


@pytest.mark.asyncio
async def test_missing_tdd_sections_adds_reminder_comment(make_config, tmp_path: Path):
    config = make_config(project_id="p1", tdd_enforcement_mode="advisory")
    store = SQLiteStore(str(tmp_path / "db7.sqlite"))
    plane = FakePlaneClient()
    agent = ScriptedAgent(
        {
            Stage.DESIGN: [
                StageResult(
                    status=StageStatus.SUCCESS,
                    summary="design without tdd sections",
                    artifacts={"stdout": "设计说明：先实现后补测试。"},
                )
            ]
        }
    )
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=plane,
        github_client=FakeGitHubClient(),
        agent_adapter=agent,
        quality_gate=QualityGate(),
    )

    await orch.process_issue(
        issue_id="1007",
        project_id="p1",
        title="missing tdd",
        force=False,
        description="just title and notes",
    )

    assert any("[TDD-提醒]" in comment for _, _, comment in plane.comments)


@pytest.mark.asyncio
async def test_design_autofill_red_green_refactor(make_config, tmp_path: Path):
    config = make_config(project_id="p1")
    store = SQLiteStore(str(tmp_path / "db-autofill.sqlite"))
    plane = FakePlaneClient()
    agent = ScriptedAgent(
        {
            Stage.DESIGN: [
                StageResult(
                    status=StageStatus.SUCCESS,
                    summary="design with markdown tdd sections",
                    artifacts={
                        "stdout": (
                            "### Red 阶段\n"
                            "- RED-001: 新增失败用例\n\n"
                            "### Green 阶段\n"
                            "- 最小实现通过 RED-001\n\n"
                            "### Refactor 阶段\n"
                            "- 抽取重复逻辑\n\n"
                            "### 验收标准\n"
                            "- 回归测试通过"
                        )
                    },
                )
            ]
        }
    )
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=plane,
        github_client=FakeGitHubClient(),
        agent_adapter=agent,
        quality_gate=QualityGate(),
    )

    await orch.process_issue(
        issue_id="1008-a",
        project_id="p1",
        title="autofill tdd sections",
        force=False,
        description="## 背景与目标\n- 只提供业务目标，不预填 TDD 细节",
    )

    issue = store.get_issue("1008-a")
    assert issue is not None
    assert "### Red 阶段" in issue["description"]
    assert "### Green 阶段" in issue["description"]
    assert "### Refactor 阶段" in issue["description"]
    assert "### 验收标准（DoD）" in issue["description"]
    assert "RED-001" in issue["description"]


@pytest.mark.asyncio
async def test_no_tdd_reminder_when_autofill_success(make_config, tmp_path: Path):
    config = make_config(project_id="p1")
    store = SQLiteStore(str(tmp_path / "db8.sqlite"))
    plane = FakePlaneClient()
    agent = ScriptedAgent(
        {
            Stage.DESIGN: [
                StageResult(
                    status=StageStatus.SUCCESS,
                    summary="design with tdd sections",
                    artifacts={
                        "stdout": (
                            "### Red 阶段\n"
                            "- RED-001: 新增失败用例\n\n"
                            "### Green 阶段\n"
                            "- 最小实现通过 RED-001\n\n"
                            "### Refactor 阶段\n"
                            "- 提取校验函数并回归\n\n"
                            "### 验收标准（DoD）\n"
                            "- 全量测试通过"
                        )
                    },
                )
            ]
        }
    )
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=plane,
        github_client=FakeGitHubClient(),
        agent_adapter=agent,
        quality_gate=QualityGate(),
    )

    await orch.process_issue(
        issue_id="1008",
        project_id="p1",
        title="autofill tdd",
        force=False,
        description="## 背景与目标\n- 只提供业务目标，不预填 TDD 细节",
    )

    issue = store.get_issue("1008")
    assert issue is not None
    sections = parse_tdd_sections(issue["description"])
    assert "RED-001" in sections["red"]
    assert "最小实现" in sections["green"]
    assert "提取校验函数" in sections["refactor"]
    assert "全量测试通过" in sections["acceptance"]
    assert len(plane.description_updates) == 1
    assert "Red 阶段" in plane.description_updates[0][2]
    assert not any("[TDD-提醒]" in comment for _, _, comment in plane.comments)
