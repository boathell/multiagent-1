from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings


@dataclass
class PullRequestResult:
    branch: str
    pr_url: str


class GitHubClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._logger = logging.getLogger("app.adapters.github")
        self._gh_login_cache: str | None = None

    @staticmethod
    def _slug(text: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
        return cleaned[:40] or "task"

    @staticmethod
    def branch_name(issue_id: str, title: str) -> str:
        return f"plane/{issue_id}-{GitHubClient._slug(title)}"

    def _run(self, cmd: list[str], cwd: str) -> str:
        completed = subprocess.run(
            cmd,
            cwd=cwd,
            env=os.environ.copy(),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return completed.stdout.strip()

    def _try_run(self, cmd: list[str], cwd: str) -> tuple[bool, str]:
        try:
            return True, self._run(cmd, cwd=cwd)
        except subprocess.CalledProcessError as exc:
            output = "\n".join(x for x in [exc.stdout, exc.stderr] if x).strip()
            return False, output

    @staticmethod
    def _parse_repo_url(repo_url: str) -> tuple[str, str] | None:
        text = (repo_url or "").strip()
        if not text:
            return None
        http_match = re.match(r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", text)
        if http_match:
            return http_match.group(1), http_match.group(2)
        ssh_match = re.match(r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$", text)
        if ssh_match:
            return ssh_match.group(1), ssh_match.group(2)
        return None

    @staticmethod
    def _https_repo_url(owner: str, repo: str) -> str:
        return f"https://github.com/{owner}/{repo}.git"

    def _gh_login(self, cwd: str) -> str:
        if self._gh_login_cache:
            return self._gh_login_cache
        login = self._run(["gh", "api", "user", "-q", ".login"], cwd=cwd)
        self._gh_login_cache = login.strip()
        return self._gh_login_cache

    def _ensure_local_repo(self, local_path: str) -> None:
        ok, _ = self._try_run(["git", "rev-parse", "--is-inside-work-tree"], cwd=local_path)
        if not ok:
            self._run(["git", "init"], cwd=local_path)

    def _ensure_git_identity(self, local_path: str) -> None:
        ok_name, _ = self._try_run(["git", "config", "user.name"], cwd=local_path)
        ok_email, _ = self._try_run(["git", "config", "user.email"], cwd=local_path)
        if ok_name and ok_email:
            return

        login = self._gh_login(local_path)
        if not ok_name:
            self._run(["git", "config", "user.name", login], cwd=local_path)
        if not ok_email:
            self._run(["git", "config", "user.email", f"{login}@users.noreply.github.com"], cwd=local_path)

    def _origin_exists(self, local_path: str) -> tuple[bool, str]:
        ok, output = self._try_run(["git", "remote", "get-url", "origin"], cwd=local_path)
        return ok, output.strip()

    def _create_repo_with_fallback_name(self, owner: str, repo: str, cwd: str) -> tuple[str, str]:
        for idx in range(0, 20):
            candidate_repo = repo if idx == 0 else f"{repo}-{idx}"
            full_name = f"{owner}/{candidate_repo}"
            cmd = [
                "gh",
                "repo",
                "create",
                full_name,
                "--public",
                "--disable-issues",
                "--disable-wiki",
                "--description",
                "Created by multiagent orchestrator",
            ]
            ok, output = self._try_run(cmd, cwd=cwd)
            if ok:
                self._logger.info("Created GitHub repo: %s", full_name)
                return full_name, self._https_repo_url(owner, candidate_repo)

            lowered = output.lower()
            if "already exists" in lowered or "name already exists" in lowered:
                self._logger.info("GitHub repo name conflict: %s, trying next suffix", full_name)
                continue
            raise RuntimeError(f"Failed to create GitHub repo {full_name}: {output}")

        raise RuntimeError(f"Failed to create GitHub repo: all fallback names conflicted for {owner}/{repo}")

    def _ensure_origin_remote(self, local_path: str, repo_url: str) -> str:
        ok, current_origin = self._origin_exists(local_path)
        if ok and current_origin:
            return current_origin

        parsed = self._parse_repo_url(repo_url)
        if parsed is not None:
            owner, repo = parsed
        else:
            owner = self._gh_login(local_path)
            repo = self._slug(Path(local_path).name)

        _, created_url = self._create_repo_with_fallback_name(owner=owner, repo=repo, cwd=local_path)
        self._run(["git", "remote", "add", "origin", created_url], cwd=local_path)
        return created_url

    def _ensure_base_branch_ready(self, local_path: str, base_branch: str) -> None:
        has_head, _ = self._try_run(["git", "rev-parse", "--verify", "HEAD"], cwd=local_path)
        if not has_head:
            self._run(["git", "checkout", "--orphan", base_branch], cwd=local_path)
            self._run(["git", "commit", "--allow-empty", "-m", "chore: bootstrap repository"], cwd=local_path)
            self._run(["git", "push", "-u", "origin", base_branch], cwd=local_path)
            return

        has_base_local, _ = self._try_run(
            ["git", "show-ref", "--verify", f"refs/heads/{base_branch}"],
            cwd=local_path,
        )
        if not has_base_local:
            self._run(["git", "branch", base_branch], cwd=local_path)

        has_remote_base, remote_output = self._try_run(
            ["git", "ls-remote", "--heads", "origin", base_branch],
            cwd=local_path,
        )
        if not has_remote_base or not remote_output.strip():
            self._run(["git", "push", "-u", "origin", base_branch], cwd=local_path)

    def _has_staged_changes(self, local_path: str) -> bool:
        ok, output = self._try_run(["git", "diff", "--cached", "--quiet"], cwd=local_path)
        if ok:
            return False
        if output.strip():
            raise RuntimeError(f"Failed to detect staged changes: {output}")
        return True

    def _find_existing_pr_url(self, local_path: str, branch: str) -> str:
        ok, output = self._try_run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "all",
                "--json",
                "url",
                "--limit",
                "1",
            ],
            cwd=local_path,
        )
        if not ok or not output.strip():
            return ""
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            return ""
        if isinstance(data, list) and data and isinstance(data[0], dict):
            url = str(data[0].get("url", "")).strip()
            return url
        return ""

    def create_branch_commit_and_pr(
        self,
        issue_id: str,
        title: str,
        body: str,
        local_path: str,
        base_branch: str,
        repo_url: str = "",
    ) -> PullRequestResult:
        branch = self.branch_name(issue_id, title)

        if self._settings.github_use_mock:
            pr_url = f"https://github.com/mock/repo/pull/{issue_id}"
            self._logger.info("GitHub mock enabled, generated PR %s", pr_url)
            return PullRequestResult(branch=branch, pr_url=pr_url)

        self._ensure_local_repo(local_path)
        self._ensure_git_identity(local_path)
        remote_url = self._ensure_origin_remote(local_path, repo_url=repo_url)
        self._ensure_base_branch_ready(local_path, base_branch=base_branch)
        self._logger.info("Using GitHub remote: %s", remote_url)

        self._run(["git", "checkout", "-B", branch], cwd=local_path)
        self._run(["git", "add", "-A"], cwd=local_path)

        if self._has_staged_changes(local_path):
            self._run(["git", "commit", "-m", f"feat(issue-{issue_id}): {title}"], cwd=local_path)

        self._run(["git", "push", "-u", "origin", branch], cwd=local_path)
        ok, pr_output = self._try_run(
            [
                "gh",
                "pr",
                "create",
                "--title",
                f"[Plane {issue_id}] {title}",
                "--body",
                body,
                "--base",
                base_branch,
                "--head",
                branch,
            ],
            cwd=local_path,
        )
        if ok:
            pr_url = pr_output.strip()
        else:
            lowered = pr_output.lower()
            if "already exists" in lowered or "a pull request already exists" in lowered:
                pr_url = self._find_existing_pr_url(local_path, branch)
                if not pr_url:
                    raise RuntimeError(f"PR exists but failed to resolve URL for branch {branch}")
            else:
                raise RuntimeError(f"Failed to create PR for branch {branch}: {pr_output}")
        return PullRequestResult(branch=branch, pr_url=pr_url)
