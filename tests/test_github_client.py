from __future__ import annotations

import subprocess
from pathlib import Path

from app.adapters.github_client import GitHubClient
from app.config import Settings


def _make_client(tmp_path: Path) -> GitHubClient:
    settings = Settings(
        APP_SQLITE_PATH=str(tmp_path / "orchestrator.db"),
        PLANE_BASE_URL="",
        PLANE_WORKSPACE_SLUG="test_workspace",
        PLANE_API_TOKEN="",
        PLANE_WEBHOOK_SECRET="",
        GITHUB_USE_MOCK=False,
        AGENTS_USE_MOCK=True,
    )
    return GitHubClient(settings)


def _called_process_error(cmd: list[str], stderr: str) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(returncode=1, cmd=cmd, output="", stderr=stderr)


def test_create_pr_auto_creates_repo_and_renames_on_conflict(monkeypatch, tmp_path: Path):
    client = _make_client(tmp_path)
    calls: list[list[str]] = []

    issue_id = "2001"
    title = "init repo"
    branch = client.branch_name(issue_id, title)

    def fake_run(cmd: list[str], cwd: str) -> str:
        calls.append(cmd)
        if cmd == ["git", "rev-parse", "--is-inside-work-tree"]:
            raise _called_process_error(cmd, "not a git repository")
        if cmd == ["git", "config", "user.name"]:
            raise _called_process_error(cmd, "key does not exist")
        if cmd == ["git", "config", "user.email"]:
            raise _called_process_error(cmd, "key does not exist")
        if cmd == ["git", "remote", "get-url", "origin"]:
            raise _called_process_error(cmd, "No such remote")
        if cmd[:4] == ["gh", "repo", "create", "boathell/multiagent"]:
            raise _called_process_error(cmd, "name already exists on this account")
        if cmd == ["git", "rev-parse", "--verify", "HEAD"]:
            raise _called_process_error(cmd, "Needed a single revision")
        if cmd[:3] == ["gh", "pr", "create"]:
            return "https://github.com/boathell/multiagent-1/pull/1"
        if cmd == ["gh", "api", "user", "-q", ".login"]:
            return "boathell"
        return ""

    monkeypatch.setattr(client, "_run", fake_run)

    result = client.create_branch_commit_and_pr(
        issue_id=issue_id,
        title=title,
        body="body",
        local_path=str(tmp_path),
        base_branch="main",
        repo_url="https://github.com/boathell/multiagent.git",
    )

    assert result.branch == branch
    assert result.pr_url.endswith("/pull/1")
    assert ["gh", "repo", "create", "boathell/multiagent", "--public", "--disable-issues", "--disable-wiki", "--description", "Created by multiagent orchestrator"] in calls
    assert ["gh", "repo", "create", "boathell/multiagent-1", "--public", "--disable-issues", "--disable-wiki", "--description", "Created by multiagent orchestrator"] in calls
    assert ["git", "remote", "add", "origin", "https://github.com/boathell/multiagent-1.git"] in calls
    assert ["git", "checkout", "--orphan", "main"] in calls
    assert ["git", "push", "-u", "origin", "main"] in calls


def test_create_pr_uses_existing_origin_without_repo_create(monkeypatch, tmp_path: Path):
    client = _make_client(tmp_path)
    calls: list[list[str]] = []

    issue_id = "2002"
    title = "existing remote"
    branch = client.branch_name(issue_id, title)

    def fake_run(cmd: list[str], cwd: str) -> str:
        calls.append(cmd)
        if cmd == ["git", "remote", "get-url", "origin"]:
            return "https://github.com/boathell/existing.git"
        if cmd == ["git", "ls-remote", "--heads", "origin", "main"]:
            return "abc123\trefs/heads/main"
        if cmd[:3] == ["gh", "pr", "create"]:
            return "https://github.com/boathell/existing/pull/2"
        if cmd == ["git", "rev-parse", "--is-inside-work-tree"]:
            return "true"
        if cmd == ["git", "config", "user.name"]:
            return "boathell"
        if cmd == ["git", "config", "user.email"]:
            return "boathell@users.noreply.github.com"
        if cmd == ["git", "rev-parse", "--verify", "HEAD"]:
            return "abc123"
        if cmd == ["git", "show-ref", "--verify", "refs/heads/main"]:
            return "abc123 refs/heads/main"
        return ""

    monkeypatch.setattr(client, "_run", fake_run)

    result = client.create_branch_commit_and_pr(
        issue_id=issue_id,
        title=title,
        body="body",
        local_path=str(tmp_path),
        base_branch="main",
        repo_url="https://github.com/boathell/existing.git",
    )

    assert result.branch == branch
    assert result.pr_url.endswith("/pull/2")
    assert not any(cmd[:3] == ["gh", "repo", "create"] for cmd in calls)


def test_create_pr_reuses_existing_pr_when_branch_already_has_pr(monkeypatch, tmp_path: Path):
    client = _make_client(tmp_path)
    calls: list[list[str]] = []
    issue_id = "2003"
    title = "existing pr"
    branch = client.branch_name(issue_id, title)

    def fake_run(cmd: list[str], cwd: str) -> str:
        calls.append(cmd)
        if cmd == ["git", "remote", "get-url", "origin"]:
            return "https://github.com/boathell/existing.git"
        if cmd == ["git", "rev-parse", "--is-inside-work-tree"]:
            return "true"
        if cmd == ["git", "config", "user.name"]:
            return "boathell"
        if cmd == ["git", "config", "user.email"]:
            return "boathell@users.noreply.github.com"
        if cmd == ["git", "rev-parse", "--verify", "HEAD"]:
            return "abc123"
        if cmd == ["git", "show-ref", "--verify", "refs/heads/main"]:
            return "abc123 refs/heads/main"
        if cmd == ["git", "ls-remote", "--heads", "origin", "main"]:
            return "abc123\trefs/heads/main"
        if cmd == ["git", "diff", "--cached", "--quiet"]:
            return ""
        if cmd[:3] == ["gh", "pr", "create"]:
            raise _called_process_error(cmd, "a pull request already exists for branch")
        if cmd[:3] == ["gh", "pr", "list"]:
            return '[{"url":"https://github.com/boathell/existing/pull/99"}]'
        return ""

    monkeypatch.setattr(client, "_run", fake_run)

    result = client.create_branch_commit_and_pr(
        issue_id=issue_id,
        title=title,
        body="body",
        local_path=str(tmp_path),
        base_branch="main",
        repo_url="https://github.com/boathell/existing.git",
    )
    assert result.branch == branch
    assert result.pr_url.endswith("/pull/99")


def test_create_pr_commits_when_staged_changes_detected(monkeypatch, tmp_path: Path):
    client = _make_client(tmp_path)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], cwd: str) -> str:
        calls.append(cmd)
        if cmd == ["git", "remote", "get-url", "origin"]:
            return "https://github.com/boathell/existing.git"
        if cmd == ["git", "rev-parse", "--is-inside-work-tree"]:
            return "true"
        if cmd == ["git", "config", "user.name"]:
            return "boathell"
        if cmd == ["git", "config", "user.email"]:
            return "boathell@users.noreply.github.com"
        if cmd == ["git", "rev-parse", "--verify", "HEAD"]:
            return "abc123"
        if cmd == ["git", "show-ref", "--verify", "refs/heads/main"]:
            return "abc123 refs/heads/main"
        if cmd == ["git", "ls-remote", "--heads", "origin", "main"]:
            return "abc123\trefs/heads/main"
        if cmd == ["git", "diff", "--cached", "--quiet"]:
            raise _called_process_error(cmd, "")
        if cmd[:3] == ["gh", "pr", "create"]:
            return "https://github.com/boathell/existing/pull/100"
        return ""

    monkeypatch.setattr(client, "_run", fake_run)

    result = client.create_branch_commit_and_pr(
        issue_id="2004",
        title="staged changes",
        body="body",
        local_path=str(tmp_path),
        base_branch="main",
        repo_url="https://github.com/boathell/existing.git",
    )
    assert result.pr_url.endswith("/pull/100")
    assert any(cmd[:2] == ["git", "commit"] for cmd in calls)
