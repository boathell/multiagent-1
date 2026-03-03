from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from app.models import PipelineState, Stage, StageResult, StageStatus
from app.orchestrator import Orchestrator
from app.quality_gate import QualityGate
from app.store import SQLiteStore


class FakePlaneClient:
    def __init__(self) -> None:
        self.state_updates: list[tuple[str, str, str]] = []
        self.comments: list[tuple[str, str, str]] = []
        self.description_updates: list[tuple[str, str, str]] = []

    async def update_work_item_state(
        self, project_id: str, issue_id: str, state_name: str, state_map=None
    ) -> None:
        self.state_updates.append((project_id, issue_id, state_name))

    async def add_comment(self, project_id: str, issue_id: str, comment: str) -> None:
        self.comments.append((project_id, issue_id, comment))

    async def update_work_item_description(
        self,
        project_id: str,
        issue_id: str,
        description_html: str,
    ) -> None:
        self.description_updates.append((project_id, issue_id, description_html))


class FakeGitHubClient:
    class PR:
        def __init__(self, branch: str, pr_url: str) -> None:
            self.branch = branch
            self.pr_url = pr_url

    def create_branch_commit_and_pr(
        self,
        issue_id: str,
        title: str,
        body: str,
        local_path: str,
        base_branch: str,
        repo_url: str = "",
    ):
        return self.PR(branch=f"plane/{issue_id}", pr_url=f"https://example.com/pr/{issue_id}")


class ScriptedAgent:
    def __init__(self, scripted: dict[Stage, list[StageResult]] | None = None) -> None:
        self.scripted = scripted or {}

    async def run_stage(self, stage: Stage, context):
        queue = self.scripted.get(stage, [])
        if queue:
            return queue.pop(0)
        return StageResult(status=StageStatus.SUCCESS, summary=f"{stage.value} ok")


@pytest.mark.asyncio
async def test_happy_path(make_config, tmp_path: Path):
    config = make_config(project_id="p1")
    store = SQLiteStore(str(tmp_path / "db1.sqlite"))
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=FakePlaneClient(),
        github_client=FakeGitHubClient(),
        agent_adapter=ScriptedAgent(),
        quality_gate=QualityGate(),
    )

    await orch.process_issue(issue_id="1001", project_id="p1", title="happy", force=False)

    issue = store.get_issue("1001")
    assert issue is not None
    assert issue["state"] == PipelineState.DONE.value
    assert issue["pr_url"].endswith("/1001")


@pytest.mark.asyncio
async def test_coding_retry_then_success(make_config, tmp_path: Path):
    config = make_config(project_id="p1")
    store = SQLiteStore(str(tmp_path / "db2.sqlite"))
    plane = FakePlaneClient()
    agent = ScriptedAgent(
        {
            Stage.CODING: [
                StageResult(status=StageStatus.FAILED, summary="fail once"),
                StageResult(status=StageStatus.SUCCESS, summary="then pass"),
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

    await orch.process_issue(issue_id="1002", project_id="p1", title="retry", force=False)

    issue = store.get_issue("1002")
    assert issue is not None
    assert issue["state"] == PipelineState.DONE.value

    trace = store.get_trace("1002")
    assert any("Attempt 1/2 failed" in t["message"] for t in trace)
    assert any("status=retry" in comment for _, _, comment in plane.comments)


@pytest.mark.asyncio
async def test_review_needs_changes_once_then_done(make_config, tmp_path: Path):
    config = make_config(project_id="p1")
    store = SQLiteStore(str(tmp_path / "db3.sqlite"))
    agent = ScriptedAgent(
        {
            Stage.REVIEW: [
                StageResult(status=StageStatus.NEEDS_CHANGES, summary="need updates"),
                StageResult(status=StageStatus.SUCCESS, summary="approved"),
            ]
        }
    )
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=FakePlaneClient(),
        github_client=FakeGitHubClient(),
        agent_adapter=agent,
        quality_gate=QualityGate(),
    )

    await orch.process_issue(issue_id="1003", project_id="p1", title="loop", force=False)

    issue = store.get_issue("1003")
    assert issue is not None
    assert issue["state"] == PipelineState.DONE.value
    assert issue["review_loops"] == 1


@pytest.mark.asyncio
async def test_quality_gate_failure_blocks_issue(make_config, tmp_path: Path):
    config = make_config(project_id="p1", checks=["python -c 'import sys; sys.exit(1)'"])
    store = SQLiteStore(str(tmp_path / "db4.sqlite"))
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=FakePlaneClient(),
        github_client=FakeGitHubClient(),
        agent_adapter=ScriptedAgent(),
        quality_gate=QualityGate(),
    )

    await orch.process_issue(issue_id="1004", project_id="p1", title="gate", force=False)

    issue = store.get_issue("1004")
    assert issue is not None
    assert issue["state"] == PipelineState.BLOCKED.value


@pytest.mark.asyncio
async def test_webhook_idempotency(make_config, tmp_path: Path):
    config = make_config(project_id="p1")
    store = SQLiteStore(str(tmp_path / "db5.sqlite"))
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=FakePlaneClient(),
        github_client=FakeGitHubClient(),
        agent_adapter=ScriptedAgent(),
        quality_gate=QualityGate(),
    )

    payload = {
        "event": "work_item.created",
        "event_id": "evt-123",
        "data": {
            "work_item": {
                "id": "1005",
                "project_id": "p1",
                "name": "idempotent",
                "state_name": "Todo",
                "updated_at": "2026-03-03T00:00:00Z",
            }
        },
    }

    first = await orch.handle_webhook(payload)
    second = await orch.handle_webhook(payload)

    assert first["status"] == "accepted"
    assert second["status"] == "duplicate"


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
    sections = Orchestrator.parse_tdd_sections(description)
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
    sections = Orchestrator.parse_tdd_sections(description)
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
    sections = Orchestrator.parse_tdd_sections(description)
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
    sections = Orchestrator._extract_tdd_sections_from_stage_result(result)
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
    generated = Orchestrator._extract_tdd_sections_from_stage_result(result)
    merged_desc, merged_sections, merged = Orchestrator._merge_tdd_sections(
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
    sections = Orchestrator._extract_tdd_sections_from_stage_result(result)
    missing = Orchestrator._tdd_missing_sections(sections)
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
    sections = Orchestrator.parse_tdd_sections(issue["description"])
    assert "RED-001" in sections["red"]
    assert len(plane.description_updates) == 1
    assert "Red 阶段" in plane.description_updates[0][2]


@pytest.mark.asyncio
async def test_design_success_triggers_plane_desc_sync(make_config, tmp_path: Path):
    await test_tdd_sections_sync_to_plane(make_config, tmp_path)


@pytest.mark.asyncio
async def test_missing_tdd_sections_adds_reminder_comment(make_config, tmp_path: Path):
    config = make_config(project_id="p1")
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
    sections = Orchestrator.parse_tdd_sections(issue["description"])
    assert "RED-001" in sections["red"]
    assert "最小实现" in sections["green"]
    assert "提取校验函数" in sections["refactor"]
    assert "全量测试通过" in sections["acceptance"]
    assert len(plane.description_updates) == 1
    assert "Red 阶段" in plane.description_updates[0][2]
    assert not any("[TDD-提醒]" in comment for _, _, comment in plane.comments)


def test_build_stage_comment_includes_chinese_and_machine_fields():
    result = StageResult(
        status=StageStatus.SUCCESS,
        summary="编码完成",
        artifacts={"pr_url": "https://example.com/pr/1007"},
    )
    comment = Orchestrator._build_stage_comment(Stage.CODING, result)
    assert "[编排器]" in comment
    assert "阶段：编码" in comment
    assert "状态：成功" in comment
    assert "摘要：编码阶段执行成功" in comment
    assert "PR：https://example.com/pr/1007" in comment
    assert "[ORCH]" in comment
    assert "stage=coding" in comment
    assert "status=success" in comment


def test_build_stage_comment_failed_contains_reason():
    result = StageResult(status=StageStatus.FAILED, summary="gemini timeout after 300s.")
    comment = Orchestrator._build_stage_comment(Stage.REVIEW, result)
    assert "状态：失败" in comment
    assert "原因：执行超时（300s）" in comment
    assert "status=failed" in comment


def test_build_retry_and_blocked_comment_templates():
    retry_comment = Orchestrator._build_retry_comment(
        Stage.CODING,
        attempt=1,
        total_attempts=2,
        summary="quality gate failed after coding stage.",
    )
    assert "状态：重试中" in retry_comment
    assert "摘要：阶段失败后自动重试" in retry_comment
    assert "原因：质量门禁未通过" in retry_comment
    assert "status=retry" in retry_comment

    blocked_comment = Orchestrator._build_blocked_comment(
        Stage.REVIEW,
        StageResult(status=StageStatus.FAILED, summary="review loop exceeded max limit."),
    )
    assert "状态：已阻塞" in blocked_comment
    assert "摘要：达到失败上限，流程暂停" in blocked_comment
    assert "原因：审查回流超过上限" in blocked_comment
    assert "status=blocked_notice" in blocked_comment


@pytest.mark.asyncio
async def test_retry_issue_serializes_same_issue(make_config, tmp_path: Path):
    class SlowAgent:
        def __init__(self) -> None:
            self.running = 0
            self.max_running = 0

        async def run_stage(self, stage: Stage, context):
            self.running += 1
            self.max_running = max(self.max_running, self.running)
            await asyncio.sleep(0.05)
            self.running -= 1
            return StageResult(status=StageStatus.SUCCESS, summary=f"{stage.value} ok")

    config = make_config(project_id="p1")
    store = SQLiteStore(str(tmp_path / "db6.sqlite"))
    agent = SlowAgent()
    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=FakePlaneClient(),
        github_client=FakeGitHubClient(),
        agent_adapter=agent,
        quality_gate=QualityGate(),
    )
    store.upsert_issue("1006", "p1", "concurrent retry", PipelineState.BLOCKED.value)
    store.update_issue_fields("1006", last_stage=Stage.REVIEW.value)

    await asyncio.gather(orch.retry_issue("1006"), orch.retry_issue("1006"))

    assert agent.max_running == 1


@pytest.mark.asyncio
async def test_review_stage_collects_git_diff_context(make_config, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(cmd: list[str]) -> None:
        subprocess.run(
            cmd,
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    run(["git", "init", "-b", "main"])
    run(["git", "config", "user.name", "tester"])
    run(["git", "config", "user.email", "tester@example.com"])
    (repo / "demo.txt").write_text("v1\n", encoding="utf-8")
    run(["git", "add", "demo.txt"])
    run(["git", "commit", "-m", "init"])
    run(["git", "checkout", "-b", "plane/1009-demo"])
    (repo / "demo.txt").write_text("v1\nv2\n", encoding="utf-8")
    run(["git", "add", "demo.txt"])
    run(["git", "commit", "-m", "update"])

    config = make_config(project_id="p1")
    config.projects["p1"].local_path = str(repo)

    class CaptureReviewAgent:
        def __init__(self) -> None:
            self.captured = None

        async def run_stage(self, stage: Stage, context):
            if stage == Stage.REVIEW:
                self.captured = context
            return StageResult(status=StageStatus.SUCCESS, summary=f"{stage.value} ok")

    agent = CaptureReviewAgent()
    store = SQLiteStore(str(tmp_path / "db-review.sqlite"))
    store.upsert_issue("1009", "p1", "review ctx", PipelineState.REVIEW.value)
    store.update_issue_fields("1009", branch="plane/1009-demo")

    orch = Orchestrator(
        app_config=config,
        store=store,
        plane_client=FakePlaneClient(),
        github_client=FakeGitHubClient(),
        agent_adapter=agent,
        quality_gate=QualityGate(),
    )

    result = await orch._run_stage_with_retry(
        stage=Stage.REVIEW,
        issue_id="1009",
        project_id="p1",
        title="review ctx",
        project=config.projects["p1"],
    )
    assert result.status == StageStatus.SUCCESS
    assert agent.captured is not None
    assert "demo.txt" in agent.captured.metadata["review_changed_files"]
    assert "v2" in agent.captured.metadata["review_diff"]
