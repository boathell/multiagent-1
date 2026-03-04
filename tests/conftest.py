from __future__ import annotations

from pathlib import Path

import pytest

from app.config import AppConfig, Settings
from app.models import ProjectConfig


@pytest.fixture
def make_config(tmp_path: Path):
    def _make(
        project_id: str = "p1",
        checks: list[str] | None = None,
        review_max_loops: int | None = None,
        review_arbiter_max_loops: int | None = None,
        max_code_file_lines: int = 1000,
        human_handoff_enabled: bool = True,
        tdd_enforcement_mode: str = "strict",
        issue_max_concurrency: int | None = None,
        issue_worktree_enabled: bool = False,
        issue_worktree_root: str | None = None,
        issue_worktree_cleanup_enabled: bool = False,
        issue_worktree_retention_hours: int | None = None,
        agent_config: dict | None = None,
    ) -> AppConfig:
        settings_kwargs: dict[str, object] = {
            "APP_SQLITE_PATH": str(tmp_path / "orchestrator.db"),
            "PLANE_BASE_URL": "",
            "PLANE_WORKSPACE_SLUG": "test_workspace",
            "PLANE_API_TOKEN": "",
            "PLANE_WEBHOOK_SECRET": "",
            "MAX_CODE_FILE_LINES": max_code_file_lines,
            "HUMAN_HANDOFF_ENABLED": human_handoff_enabled,
            "TDD_ENFORCEMENT_MODE": tdd_enforcement_mode,
            "GITHUB_USE_MOCK": True,
            "AGENTS_USE_MOCK": True,
            "ISSUE_WORKTREE_ENABLED": issue_worktree_enabled,
            "ISSUE_WORKTREE_CLEANUP_ENABLED": issue_worktree_cleanup_enabled,
        }
        if review_max_loops is not None:
            settings_kwargs["REVIEW_MAX_LOOPS"] = review_max_loops
        if review_arbiter_max_loops is not None:
            settings_kwargs["REVIEW_ARBITER_MAX_LOOPS"] = review_arbiter_max_loops
        if issue_max_concurrency is not None:
            settings_kwargs["ISSUE_MAX_CONCURRENCY"] = issue_max_concurrency
        if issue_worktree_root is not None:
            settings_kwargs["ISSUE_WORKTREE_ROOT"] = issue_worktree_root
        if issue_worktree_retention_hours is not None:
            settings_kwargs["ISSUE_WORKTREE_RETENTION_HOURS"] = issue_worktree_retention_hours
        settings = Settings(**settings_kwargs)

        resolved_agent_config = {"use_mock": True, **(agent_config or {})}
        review_config: dict[str, int] = {}
        raw_review = resolved_agent_config.get("review", {})
        if isinstance(raw_review, dict):
            for key in ("max_loops", "arbiter_max_loops"):
                value = raw_review.get(key)
                if value is None:
                    continue
                review_config[key] = int(value)

        project = ProjectConfig(
            plane_project_id=project_id,
            repo_url="https://github.com/example/repo.git",
            local_path=str(tmp_path),
            base_branch="main",
            checks=checks or [],
        )
        return AppConfig(
            settings=settings,
            projects={project_id: project},
            agent_config=resolved_agent_config,
            review_config=review_config,
        )

    return _make
