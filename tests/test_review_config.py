from __future__ import annotations

from pathlib import Path

from app.config import AppConfig, load_app_config
from app.orchestrator import Orchestrator
from app.quality_gate import QualityGate
from app.store import SQLiteStore


class _NoopPlaneClient:
    async def add_comment(self, project_id: str, issue_id: str, comment: str) -> None:
        return None

    async def set_issue_state(self, project_id: str, issue_id: str, state_name: str) -> None:
        return None


def _write_config_files(tmp_path: Path, agents_yaml: str) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "projects.yaml").write_text("projects: {}\n", encoding="utf-8")
    (config_dir / "agents.yaml").write_text(agents_yaml, encoding="utf-8")


def _make_orchestrator(app_config: AppConfig, tmp_path: Path) -> Orchestrator:
    store = SQLiteStore(str(tmp_path / "review-config.sqlite"))
    return Orchestrator(
        app_config=app_config,
        store=store,
        plane_client=_NoopPlaneClient(),
        github_client=object(),
        agent_adapter=object(),
        quality_gate=QualityGate(),
    )


def test_load_review_max_loops_from_yaml(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("REVIEW_MAX_LOOPS", raising=False)
    monkeypatch.delenv("REVIEW_ARBITER_MAX_LOOPS", raising=False)
    _write_config_files(
        tmp_path,
        "review:\n  max_loops: 3\n  arbiter_max_loops: 1\n",
    )

    config = load_app_config(base_dir=tmp_path)
    orchestrator = _make_orchestrator(config, tmp_path)

    assert orchestrator.max_review_loops == 3


def test_load_review_arbiter_max_loops_from_yaml(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("REVIEW_MAX_LOOPS", raising=False)
    monkeypatch.delenv("REVIEW_ARBITER_MAX_LOOPS", raising=False)
    _write_config_files(
        tmp_path,
        "review:\n  max_loops: 1\n  arbiter_max_loops: 2\n",
    )

    config = load_app_config(base_dir=tmp_path)
    orchestrator = _make_orchestrator(config, tmp_path)

    assert orchestrator.max_review_arbiter_loops == 2


def test_review_defaults_when_yaml_missing(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("REVIEW_MAX_LOOPS", raising=False)
    monkeypatch.delenv("REVIEW_ARBITER_MAX_LOOPS", raising=False)
    _write_config_files(tmp_path, "use_mock: true\n")

    config = load_app_config(base_dir=tmp_path)
    orchestrator = _make_orchestrator(config, tmp_path)

    assert config.get_review_max_loops() == 1
    assert config.get_review_arbiter_max_loops() == 1
    assert orchestrator.max_review_loops == 1
    assert orchestrator.max_review_arbiter_loops == 1


def test_review_max_loops_env_overrides_yaml(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REVIEW_MAX_LOOPS", "7")
    monkeypatch.delenv("REVIEW_ARBITER_MAX_LOOPS", raising=False)
    _write_config_files(
        tmp_path,
        "review:\n  max_loops: 3\n  arbiter_max_loops: 2\n",
    )

    config = load_app_config(base_dir=tmp_path)
    orchestrator = _make_orchestrator(config, tmp_path)

    assert config.get_review_max_loops() == 7
    assert orchestrator.max_review_loops == 7


def test_review_max_loops_use_yaml_when_env_missing(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("REVIEW_MAX_LOOPS", raising=False)
    _write_config_files(
        tmp_path,
        "review:\n  max_loops: 4\n",
    )

    config = load_app_config(base_dir=tmp_path)
    orchestrator = _make_orchestrator(config, tmp_path)

    assert config.get_review_max_loops() == 4
    assert orchestrator.max_review_loops == 4


def test_review_max_loops_use_default_when_env_and_yaml_missing(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("REVIEW_MAX_LOOPS", raising=False)
    _write_config_files(tmp_path, "tdd_enabled: true\n")

    config = load_app_config(base_dir=tmp_path)
    orchestrator = _make_orchestrator(config, tmp_path)

    assert config.get_review_max_loops() == 1
    assert orchestrator.max_review_loops == 1


def test_issue_workspace_defaults_when_env_missing(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ISSUE_MAX_CONCURRENCY", raising=False)
    monkeypatch.delenv("ISSUE_WORKTREE_ENABLED", raising=False)
    monkeypatch.delenv("ISSUE_WORKTREE_ROOT", raising=False)
    monkeypatch.delenv("ISSUE_WORKTREE_CLEANUP_ENABLED", raising=False)
    monkeypatch.delenv("ISSUE_WORKTREE_RETENTION_HOURS", raising=False)
    _write_config_files(tmp_path, "use_mock: true\n")

    config = load_app_config(base_dir=tmp_path)

    assert config.get_issue_max_concurrency() == 2
    assert config.settings.issue_worktree_enabled is True
    assert config.get_issue_worktree_root(base_dir=tmp_path) == tmp_path / ".data" / "worktrees"
    assert config.settings.issue_worktree_cleanup_enabled is False
    assert config.get_issue_worktree_retention_hours() == 72


def test_issue_workspace_env_overrides(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ISSUE_MAX_CONCURRENCY", "5")
    monkeypatch.setenv("ISSUE_WORKTREE_ENABLED", "false")
    monkeypatch.setenv("ISSUE_WORKTREE_ROOT", "/tmp/custom-worktrees")
    monkeypatch.setenv("ISSUE_WORKTREE_CLEANUP_ENABLED", "true")
    monkeypatch.setenv("ISSUE_WORKTREE_RETENTION_HOURS", "8")
    _write_config_files(tmp_path, "use_mock: true\n")

    config = load_app_config(base_dir=tmp_path)

    assert config.get_issue_max_concurrency() == 5
    assert config.settings.issue_worktree_enabled is False
    assert config.get_issue_worktree_root(base_dir=tmp_path) == Path("/tmp/custom-worktrees")
    assert config.settings.issue_worktree_cleanup_enabled is True
    assert config.get_issue_worktree_retention_hours() == 8
