"""TDD 段落解析模块。

提供从文本中提取和合并 TDD (Red/Green/Refactor/Acceptance) 段落的功能。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models import StageResult


# TDD 段落标识符
TDD_SECTIONS = ("red", "green", "refactor", "acceptance")

# 段落标签映射
SECTION_LABELS = {
    "red": "Red 阶段",
    "green": "Green 阶段",
    "refactor": "Refactor 阶段",
    "acceptance": "验收标准（DoD）",
}

# 段落标记的正则表达式模式
SECTION_PATTERNS = {
    "red": [
        r"^tdd[\s_-]*red(?:\s*[:：].*)?$",
        r"^red_result.*$",
        r"^red_stage.*$",
    ],
    "green": [
        r"^tdd[\s_-]*green(?:\s*[:：].*)?$",
        r"^green_result.*$",
        r"^green_stage.*$",
    ],
    "refactor": [
        r"^tdd[\s_-]*refactor(?:\s*[:：].*)?$",
        r"^重构阶段.*$",
        r"^refactor_note.*$",
        r"^refactor_stage.*$",
    ],
    "acceptance": [
        r"^tdd[\s_-]*acceptance(?:\s*[:：].*)?$",
        r"^验收标准.*$",
        r"^acceptance criteria.*$",
        r"^dod$",
        r"^acceptance.*$",
    ],
}

# 行内内容提取的正则模式
INLINE_PATTERNS = {
    "red": (r"tdd[\s_-]*red", r"red_result", r"red_stage"),
    "green": (r"tdd[\s_-]*green", r"green_result", r"green_stage"),
    "refactor": (r"tdd[\s_-]*refactor", r"refactor_note", r"refactor_stage"),
    "acceptance": (r"tdd[\s_-]*acceptance", r"acceptance(?:\s+criteria)?", r"dod"),
}


def parse_tdd_sections(description: str) -> dict[str, str]:
    """从描述文本中解析 TDD 段落。

    Args:
        description: 输入的描述文本

    Returns:
        包含 red/green/refactor/acceptance 四个键的字典
    """
    sections = {k: "" for k in TDD_SECTIONS}
    if not description.strip():
        return sections

    buffers = {k: [] for k in sections}
    current: str | None = None

    for raw_line in description.splitlines():
        line = raw_line.strip()
        marker = detect_tdd_section_marker(line)
        if marker:
            current = marker
            inline_content = extract_inline_tdd_section_content(line, marker)
            if inline_content:
                buffers[current].append(inline_content)
            continue
        if current:
            buffers[current].append(raw_line.rstrip())

    for key, lines in buffers.items():
        sections[key] = "\n".join(x for x in lines if x.strip()).strip()

    return sections


def detect_tdd_section_marker(line: str) -> str | None:
    """检测一行文本是否是 TDD 段落标记。

    Args:
        line: 输入行文本

    Returns:
        检测到的段落类型 (red/green/refactor/acceptance) 或 None
    """
    if not line:
        return None

    # 清理行文本（保留下划线用于匹配 red_result 等标记）
    cleaned = line.lower()
    cleaned = re.sub(r"^[\s#>*`\-0-9\.\)\(]+", "", cleaned)
    # 只移除 markdown 格式符号，保留下划线
    cleaned = cleaned.replace("*", "").replace("`", "").strip()

    if not cleaned:
        return None

    # 检查各段落的匹配模式
    for section, patterns in SECTION_PATTERNS.items():
        for pattern in patterns:
            if re.match(pattern, cleaned):
                return section

    # 检查阶段标题匹配
    for section in ("red", "green", "refactor"):
        if matches_tdd_stage_heading(cleaned, section):
            return section

    return None


def extract_inline_tdd_section_content(line: str, marker: str) -> str:
    """从行内提取 TDD 段落内容（标记后的内容）。

    Args:
        line: 输入行文本
        marker: 段落标记类型

    Returns:
        提取的内容或空字符串
    """
    for token in INLINE_PATTERNS.get(marker, ()):
        matched = re.match(
            rf"(?i)^[\s#>*`\-0-9\.\)\(]*{token}\s*[:：]\s*(.+)$", line.strip()
        )
        if not matched:
            continue
        content = matched.group(1).strip()
        if content.lower() in {"(missing)", "missing", "n/a", "na", "none"}:
            return ""
        return content
    return ""


def matches_tdd_stage_heading(cleaned: str, token: str) -> bool:
    """检查清理后的文本是否匹配 TDD 阶段标题。

    Args:
        cleaned: 清理后的小写文本
        token: 要匹配的标记

    Returns:
        是否匹配
    """
    if cleaned in {token, f"{token}:", f"{token}："}:
        return True
    pattern = rf"^{re.escape(token)}(?:\s*(?:阶段|stage).*)?$"
    return bool(re.match(pattern, cleaned))


def extract_tdd_sections_from_stage_result(result: "StageResult") -> dict[str, str]:
    """从阶段结果中提取 TDD 段落。

    Args:
        result: 阶段执行结果

    Returns:
        包含 TDD 段落的字典
    """
    stdout = str(result.artifacts.get("stdout", "")).strip()
    if not stdout:
        return {k: "" for k in TDD_SECTIONS}
    return parse_tdd_sections(stdout)


def merge_tdd_sections(
    base_description: str,
    generated_sections: dict[str, str],
) -> tuple[str, dict[str, str], bool]:
    """合并基础描述与生成的 TDD 段落。

    Args:
        base_description: 原始描述文本
        generated_sections: 生成的 TDD 段落

    Returns:
        (合并后的描述, 合并后的段落字典, 是否有变更)
    """
    normalized_base = base_description.strip()
    base_sections = parse_tdd_sections(normalized_base)

    appended_parts: list[str] = []
    for key in TDD_SECTIONS:
        original = str(base_sections.get(key, "")).strip()
        generated = str(generated_sections.get(key, "")).strip()
        if original or not generated:
            continue
        appended_parts.append(f"### {SECTION_LABELS[key]}\n{generated}")

    if not appended_parts:
        return normalized_base, base_sections, False

    chunks = [normalized_base] if normalized_base else []
    chunks.extend(appended_parts)
    merged_description = "\n\n".join(chunks).strip()
    merged_sections = parse_tdd_sections(merged_description)
    return merged_description, merged_sections, True


def tdd_missing_sections(tdd_sections: dict[str, str]) -> list[str]:
    """检测缺失的必填 TDD 段落。

    Args:
        tdd_sections: TDD 段落字典

    Returns:
        缺失的段落类型列表
    """
    required = ("red", "green", "refactor")
    return [name for name in required if not str(tdd_sections.get(name, "")).strip()]


def deduplicate_tdd_sections(description: str) -> str:
    """去重描述中的 TDD 段落。

    如果同一个段落出现多次，只保留第一个。

    Args:
        description: 输入描述

    Returns:
        去重后的描述
    """
    sections = parse_tdd_sections(description)
    seen: set[str] = set()
    lines_out: list[str] = []
    current_section: str | None = None
    buffer: list[str] = []

    def flush_buffer() -> None:
        nonlocal buffer
        if buffer:
            lines_out.extend(buffer)
            buffer = []

    for raw_line in description.splitlines():
        line = raw_line.strip()
        marker = detect_tdd_section_marker(line)

        if marker:
            flush_buffer()
            current_section = marker
            if marker in seen:
                # 跳过已见过的段落
                buffer = []
                continue
            seen.add(marker)
            lines_out.append(raw_line)
        elif current_section and marker is None:
            # 检查是否是新的非 TDD 段落开始（可能是标题）
            if line.startswith("#") and line.lower().replace("#", "").strip() not in {
                "", " ", "背景", "目标", "范围"
            }:
                # 可能是另一个章节标题，重置当前段落
                flush_buffer()
                current_section = None
            buffer.append(raw_line)
        else:
            flush_buffer()
            lines_out.append(raw_line)

    flush_buffer()
    return "\n".join(lines_out).strip()
