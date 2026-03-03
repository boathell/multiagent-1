"""TDD 解析模块测试。"""

from __future__ import annotations

import pytest

from app.models import StageResult, StageStatus
from app.tdd_parser import (
    detect_tdd_section_marker,
    extract_inline_tdd_section_content,
    extract_tdd_sections_from_stage_result,
    merge_tdd_sections,
    parse_tdd_sections,
    tdd_missing_sections,
    matches_tdd_stage_heading,
    deduplicate_tdd_sections,
)


def test_parse_tdd_sections_cn_template():
    """测试解析中文模板格式的 TDD 段落。"""
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
    """测试保留 Red 阶段的用例内容。"""
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
    """测试忽略非标题行中的 red/green/refactor 提及。"""
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


def test_parse_tdd_sections_empty_input():
    """测试空输入返回空段落。"""
    sections = parse_tdd_sections("")
    assert sections == {"red": "", "green": "", "refactor": "", "acceptance": ""}


def test_parse_tdd_sections_with_inline_tokens():
    """测试解析行内 TDD 标记。"""
    description = """
TDD_RED: - RED-001: test case
TDD_GREEN: - implementation
TDD_REFACTOR: - refactoring
TDD_ACCEPTANCE: - acceptance criteria
"""
    sections = parse_tdd_sections(description)
    assert "RED-001" in sections["red"]
    assert "implementation" in sections["green"]
    assert "refactoring" in sections["refactor"]
    assert "acceptance criteria" in sections["acceptance"]


def test_detect_tdd_section_marker_various_formats():
    """测试检测各种格式的段落标记。"""
    # Red 阶段
    assert detect_tdd_section_marker("### Red 阶段") == "red"
    assert detect_tdd_section_marker("TDD_RED: content") == "red"
    assert detect_tdd_section_marker("TDD-Red: content") == "red"
    assert detect_tdd_section_marker("red_result: something") == "red"
    assert detect_tdd_section_marker("red_stage: something") == "red"
    
    # Green 阶段
    assert detect_tdd_section_marker("### Green 阶段") == "green"
    assert detect_tdd_section_marker("TDD_GREEN: content") == "green"
    assert detect_tdd_section_marker("green_result: something") == "green"
    
    # Refactor 阶段
    assert detect_tdd_section_marker("### Refactor 阶段") == "refactor"
    assert detect_tdd_section_marker("TDD_REFACTOR: content") == "refactor"
    assert detect_tdd_section_marker("重构阶段") == "refactor"
    assert detect_tdd_section_marker("refactor_note: something") == "refactor"
    
    # Acceptance 阶段
    assert detect_tdd_section_marker("### 验收标准") == "acceptance"
    assert detect_tdd_section_marker("TDD_ACCEPTANCE: content") == "acceptance"
    assert detect_tdd_section_marker("Acceptance Criteria") == "acceptance"
    assert detect_tdd_section_marker("DoD") == "acceptance"
    
    # 非标记行
    assert detect_tdd_section_marker("普通文本") is None
    assert detect_tdd_section_marker("") is None


def test_extract_inline_tdd_section_content():
    """测试提取行内 TDD 段落内容。"""
    assert extract_inline_tdd_section_content("TDD_RED: content here", "red") == "content here"
    assert extract_inline_tdd_section_content("TDD_GREEN: impl here", "green") == "impl here"
    assert extract_inline_tdd_section_content("TDD_REFACTOR: refactor here", "refactor") == "refactor here"
    assert extract_inline_tdd_section_content("TDD_ACCEPTANCE: criteria here", "acceptance") == "criteria here"
    
    # 测试缺失值
    assert extract_inline_tdd_section_content("TDD_RED: missing", "red") == ""
    assert extract_inline_tdd_section_content("TDD_RED: (missing)", "red") == ""
    assert extract_inline_tdd_section_content("TDD_RED: n/a", "red") == ""


def test_matches_tdd_stage_heading():
    """测试阶段标题匹配。"""
    assert matches_tdd_stage_heading("red", "red") is True
    assert matches_tdd_stage_heading("red:", "red") is True
    assert matches_tdd_stage_heading("red stage", "red") is True
    assert matches_tdd_stage_heading("red阶段", "red") is True
    assert matches_tdd_stage_heading("green", "green") is True
    assert matches_tdd_stage_heading("refactor", "refactor") is True
    
    assert matches_tdd_stage_heading("redundant", "red") is False
    assert matches_tdd_stage_heading("greenery", "green") is False


def test_extract_tdd_sections_from_stage_result():
    """测试从阶段结果中提取 TDD 段落。"""
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


def test_extract_tdd_sections_from_stage_result_empty():
    """测试从空结果中提取段落。"""
    result = StageResult(
        status=StageStatus.SUCCESS,
        summary="design done",
        artifacts={"stdout": ""},
    )
    sections = extract_tdd_sections_from_stage_result(result)
    assert sections == {"red": "", "green": "", "refactor": "", "acceptance": ""}


def test_merge_tdd_sections_appends_missing():
    """测试合并时追加缺失的段落。"""
    base_description = "## 背景\n仅有业务背景，无 TDD 细节。"
    generated_sections = {
        "red": "- RED-001: 缺少提取逻辑\n",
        "green": "- 实现 `_extract_tdd_sections_from_stage_result`\n",
        "refactor": "- 提取公共解析函数\n",
        "acceptance": "",
    }
    merged_desc, merged_sections, merged = merge_tdd_sections(
        base_description=base_description,
        generated_sections=generated_sections,
    )
    assert merged is True
    assert "### Red 阶段" in merged_desc
    assert "RED-001" in merged_sections["red"]
    assert "提取公共解析函数" in merged_sections["refactor"]


def test_merge_tdd_sections_no_change_when_exists():
    """测试已有段落时不重复追加。"""
    base_description = "## 背景\n### Red 阶段\n- 已有内容"
    generated_sections = {
        "red": "- 新内容\n",
        "green": "",
        "refactor": "",
        "acceptance": "",
    }
    merged_desc, merged_sections, merged = merge_tdd_sections(
        base_description=base_description,
        generated_sections=generated_sections,
    )
    assert merged is False
    assert "已有内容" in merged_desc
    assert "新内容" not in merged_desc


def test_merge_tdd_sections_empty_base():
    """测试空基础描述时的合并。"""
    base_description = ""
    generated_sections = {
        "red": "- RED-001: test\n",
        "green": "- impl\n",
        "refactor": "- refactor\n",
        "acceptance": "- acceptance\n",
    }
    merged_desc, merged_sections, merged = merge_tdd_sections(
        base_description=base_description,
        generated_sections=generated_sections,
    )
    assert merged is True
    assert "### Red 阶段" in merged_desc
    assert "### Green 阶段" in merged_desc
    assert "RED-001" in merged_sections["red"]


def test_tdd_missing_sections_detects_absence():
    """测试检测缺失的段落。"""
    sections = {
        "red": "- RED-001: 失败用例\n",
        "green": "- 通过 RED-001\n",
        "refactor": "",
        "acceptance": "- [x] 回归通过",
    }
    missing = tdd_missing_sections(sections)
    assert missing == ["refactor"]


def test_tdd_missing_sections_all_present():
    """测试所有段落都存在时返回空列表。"""
    sections = {
        "red": "- RED-001\n",
        "green": "- impl\n",
        "refactor": "- refactor\n",
        "acceptance": "",
    }
    missing = tdd_missing_sections(sections)
    assert missing == []


def test_tdd_missing_sections_all_missing():
    """测试所有必填段落都缺失时。"""
    sections = {
        "red": "",
        "green": "",
        "refactor": "",
        "acceptance": "- acceptance",
    }
    missing = tdd_missing_sections(sections)
    assert missing == ["red", "green", "refactor"]


def test_deduplicate_tdd_sections():
    """测试段落去重。"""
    description = """
### Red 阶段
- Content 1
### Red 阶段
- Content 2
### Green 阶段
- Green content
"""
    result = deduplicate_tdd_sections(description)
    # 应该只保留第一个 Red 阶段
    assert result.count("### Red 阶段") == 1
    assert "Content 1" in result
    assert "Green content" in result


def test_parse_tdd_sections_with_markdown_headings():
    """测试解析 Markdown 标题格式的 TDD 段落。"""
    description = """
### Red 阶段
- RED-001: 新增失败用例

### Green 阶段
- 最小实现通过 RED-001

### Refactor 阶段
- 抽取重复逻辑

### 验收标准
- 回归测试通过
"""
    sections = parse_tdd_sections(description)
    assert "RED-001" in sections["red"]
    assert "最小实现" in sections["green"]
    assert "抽取重复逻辑" in sections["refactor"]
    assert "回归测试通过" in sections["acceptance"]


def test_detect_tdd_section_marker_with_punctuation():
    """测试带标点的段落标记检测。"""
    assert detect_tdd_section_marker("Red:") == "red"
    assert detect_tdd_section_marker("Red：") == "red"  # 中文冒号
    assert detect_tdd_section_marker("TDD-Red:") == "red"
    assert detect_tdd_section_marker("# Red 阶段") == "red"
    assert detect_tdd_section_marker("> Red 阶段") == "red"


def test_extract_tdd_sections_preserves_multiline_content():
    """测试保留多行内容。"""
    description = """
### Red 阶段
- Line 1
- Line 2
- Line 3

### Green 阶段
- Single line
"""
    sections = parse_tdd_sections(description)
    assert "Line 1" in sections["red"]
    assert "Line 2" in sections["red"]
    assert "Line 3" in sections["red"]
    assert "Single line" in sections["green"]


def test_extract_tdd_sections_from_stage_result_with_nested_markdown():
    """测试从包含嵌套 Markdown 的结果中提取段落。"""
    stdout = """
Some intro text

### Red 阶段
| 测试ID | 文件 | 用例名 |
|---|---|---|
| RED-001 | test.py | test_case |

### Green 阶段
- Implementation detail

### Refactor 阶段
N/A

### 验收标准
- [x] Done
"""
    result = StageResult(
        status=StageStatus.SUCCESS,
        summary="design",
        artifacts={"stdout": stdout},
    )
    sections = extract_tdd_sections_from_stage_result(result)
    assert "RED-001" in sections["red"]
    assert "Implementation detail" in sections["green"]
    assert "N/A" in sections["refactor"] or sections["refactor"] == ""
    assert "Done" in sections["acceptance"]
