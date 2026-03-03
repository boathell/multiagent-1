from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PipelineState(str, Enum):
    TODO = "Todo"
    DESIGN = "Design"
    CODING = "Coding"
    REVIEW = "Review"
    DONE = "Done"
    BLOCKED = "Blocked"


class Stage(str, Enum):
    DESIGN = "design"
    CODING = "coding"
    REVIEW = "review"


class StageStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    NEEDS_CHANGES = "needs_changes"


@dataclass
class IssueContext:
    issue_id: str
    project_id: str
    title: str = ""
    description: str = ""
    repo_url: str = ""
    local_path: str = ""
    base_branch: str = "main"
    branch: str = ""
    pr_url: str = ""
    attempts: dict[str, int] = field(default_factory=dict)
    review_loops: int = 0
    tdd_sections: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StageResult:
    status: StageStatus
    summary: str
    artifacts: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProjectConfig:
    plane_project_id: str
    repo_url: str
    local_path: str
    base_branch: str = "main"
    checks: list[str] = field(default_factory=list)
    state_map: dict[str, str] = field(default_factory=dict)
