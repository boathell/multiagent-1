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


class FailureClass(str, Enum):
    QUALITY_ISSUE = "QUALITY_ISSUE"
    GEMINI_ISSUE = "GEMINI_ISSUE"
    ENV_ISSUE = "ENV_ISSUE"
    PROTOCOL_VIOLATION = "PROTOCOL_VIOLATION"


@dataclass
class IssueContract:
    goal: str = ""
    scope: str = ""
    dod: str = ""
    risk: str = ""
    rollback: str = ""
    score: int = 0
    missing_fields: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "scope": self.scope,
            "dod": self.dod,
            "risk": self.risk,
            "rollback": self.rollback,
            "score": self.score,
            "missing_fields": list(self.missing_fields),
        }


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
    arbiter_loops: int = 0
    failure_class: str = ""
    handoff_reason: str = ""
    tdd_sections: dict[str, str] = field(default_factory=dict)
    issue_contract: IssueContract = field(default_factory=IssueContract)
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
