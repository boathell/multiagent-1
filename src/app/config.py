from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models import ProjectConfig


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = Field(default="dev", alias="APP_ENV")
    app_log_level: str = Field(default="INFO", alias="APP_LOG_LEVEL")
    app_sqlite_path: str = Field(default=".data/orchestrator.db", alias="APP_SQLITE_PATH")

    plane_base_url: str = Field(default="", alias="PLANE_BASE_URL")
    plane_workspace_slug: str = Field(default="", alias="PLANE_WORKSPACE_SLUG")
    plane_api_token: str = Field(default="", alias="PLANE_API_TOKEN")
    plane_webhook_secret: str = Field(default="", alias="PLANE_WEBHOOK_SECRET")

    github_token: str = Field(default="", alias="GITHUB_TOKEN")
    github_use_mock: bool = Field(default=True, alias="GITHUB_USE_MOCK")

    feishu_webhook_url: str = Field(default="", alias="FEISHU_WEBHOOK_URL")
    feishu_signing_secret: str = Field(default="", alias="FEISHU_SIGNING_SECRET")

    agents_use_mock: bool = Field(default=True, alias="AGENTS_USE_MOCK")


@dataclass
class AppConfig:
    settings: Settings
    projects: dict[str, ProjectConfig]
    agent_config: dict

    def get_project(self, project_id: str) -> ProjectConfig | None:
        return self.projects.get(str(project_id))


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Invalid YAML root in {path}")
    return data


def _load_projects(path: Path) -> dict[str, ProjectConfig]:
    raw = _load_yaml(path).get("projects", {})
    projects: dict[str, ProjectConfig] = {}
    for project_id, value in raw.items():
        if not isinstance(value, dict):
            continue
        projects[str(project_id)] = ProjectConfig(
            plane_project_id=str(project_id),
            repo_url=str(value.get("repo_url", "")),
            local_path=str(value.get("local_path", "")),
            base_branch=str(value.get("base_branch", "main")),
            checks=[str(x) for x in value.get("checks", [])],
            state_map={str(k): str(v) for k, v in value.get("state_map", {}).items()},
        )
    return projects


def load_app_config(base_dir: Path | None = None) -> AppConfig:
    root = base_dir or Path.cwd()
    settings = Settings()
    projects_path = root / "config" / "projects.yaml"
    agents_path = root / "config" / "agents.yaml"

    return AppConfig(
        settings=settings,
        projects=_load_projects(projects_path),
        agent_config=_load_yaml(agents_path),
    )
