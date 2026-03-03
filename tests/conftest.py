from __future__ import annotations

from pathlib import Path

import pytest

from app.config import AppConfig, Settings
from app.models import ProjectConfig


@pytest.fixture
def make_config(tmp_path: Path):
    def _make(project_id: str = "p1", checks: list[str] | None = None) -> AppConfig:
        settings = Settings(
            APP_SQLITE_PATH=str(tmp_path / "orchestrator.db"),
            PLANE_BASE_URL="",
            PLANE_WORKSPACE_SLUG="test_workspace",
            PLANE_API_TOKEN="",
            PLANE_WEBHOOK_SECRET="",
            GITHUB_USE_MOCK=True,
            AGENTS_USE_MOCK=True,
        )
        project = ProjectConfig(
            plane_project_id=project_id,
            repo_url="https://github.com/example/repo.git",
            local_path=str(tmp_path),
            base_branch="main",
            checks=checks or [],
        )
        return AppConfig(settings=settings, projects={project_id: project}, agent_config={"use_mock": True})

    return _make
