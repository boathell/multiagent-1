from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path


class WorkspaceError(RuntimeError):
    pass


class WorkspaceManager:
    def __init__(self, worktree_root: Path) -> None:
        self._worktree_root = worktree_root

    @staticmethod
    def _slug(value: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-._")
        return cleaned or "unknown"

    @staticmethod
    def _run_git(cwd: Path, args: list[str]) -> tuple[bool, str]:
        try:
            completed = subprocess.run(
                args,
                cwd=str(cwd),
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

        output = "\n".join(x for x in [completed.stdout, completed.stderr] if x).strip()
        if completed.returncode != 0:
            return False, output or f"exit={completed.returncode}"
        return True, output

    @staticmethod
    def _is_git_repo(path: Path) -> bool:
        ok, _ = WorkspaceManager._run_git(path, ["git", "rev-parse", "--is-inside-work-tree"])
        return ok

    @staticmethod
    def _ensure_clean_target_path(path: Path) -> None:
        if not path.exists():
            return

        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
            return

        if WorkspaceManager._is_git_repo(path):
            return

        raise WorkspaceError(f"workspace path exists and is not reusable git repo: {path}")

    def workspace_path(self, project_id: str, issue_id: str) -> Path:
        project_seg = self._slug(str(project_id))
        issue_seg = self._slug(str(issue_id))
        return (self._worktree_root / project_seg / issue_seg).resolve()

    def prepare_workspace(
        self,
        *,
        project_local_path: str,
        project_id: str,
        issue_id: str,
        base_branch: str,
    ) -> str:
        project_root = Path(project_local_path).resolve()
        if not project_root.exists() or not project_root.is_dir():
            raise WorkspaceError(f"project local path is invalid: {project_root}")
        if not self._is_git_repo(project_root):
            raise WorkspaceError(f"project local path is not a git repository: {project_root}")

        workspace = self.workspace_path(project_id=project_id, issue_id=issue_id)
        if workspace.exists() and self._is_git_repo(workspace):
            return str(workspace)

        workspace.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_clean_target_path(workspace)

        refs: list[str] = []
        branch = str(base_branch or "").strip()
        if branch:
            refs.append(branch)
        refs.append("HEAD")

        errors: list[str] = []
        for ref in refs:
            ok, output = self._run_git(
                project_root,
                ["git", "worktree", "add", "--detach", str(workspace), ref],
            )
            if ok:
                return str(workspace)
            errors.append(f"ref={ref}: {output}")

        raise WorkspaceError(
            f"failed to prepare issue worktree for issue={issue_id}: {' | '.join(errors)}"
        )

    @staticmethod
    def _cleanup_empty_parents(path: Path, stop_at: Path) -> None:
        current = path.parent
        stop = stop_at.resolve()
        while True:
            resolved = current.resolve()
            if resolved == stop:
                return
            if not current.exists() or not current.is_dir():
                current = current.parent
                continue
            if any(current.iterdir()):
                return
            current.rmdir()
            current = current.parent

    def cleanup_stale_worktrees(
        self,
        *,
        project_local_path: str,
        active_workspace_paths: set[str],
        retention_hours: int,
    ) -> dict[str, int | list[str]]:
        project_root = Path(project_local_path).resolve()
        if not self._is_git_repo(project_root):
            raise WorkspaceError(f"project local path is not a git repository: {project_root}")

        root = self._worktree_root.resolve()
        if not root.exists():
            return {
                "scanned": 0,
                "removed": 0,
                "skipped_active": 0,
                "skipped_fresh": 0,
                "errors": [],
            }

        active = {str(Path(path).resolve()) for path in active_workspace_paths}
        retention_seconds = max(0, int(retention_hours)) * 3600
        now = time.time()
        scanned = 0
        removed = 0
        skipped_active = 0
        skipped_fresh = 0
        errors: list[str] = []

        for project_dir in root.iterdir():
            if not project_dir.is_dir():
                continue
            for issue_dir in project_dir.iterdir():
                if not issue_dir.is_dir():
                    continue
                workspace = issue_dir.resolve()
                scanned += 1
                workspace_str = str(workspace)
                if workspace_str in active:
                    skipped_active += 1
                    continue

                age_seconds = now - workspace.stat().st_mtime
                if age_seconds < retention_seconds:
                    skipped_fresh += 1
                    continue

                ok, output = self._run_git(
                    project_root,
                    ["git", "worktree", "remove", "--force", workspace_str],
                )
                if not ok:
                    errors.append(f"{workspace_str}: {output}")
                    continue

                removed += 1
                self._cleanup_empty_parents(workspace, stop_at=root)

        if removed > 0:
            self._run_git(project_root, ["git", "worktree", "prune"])

        return {
            "scanned": scanned,
            "removed": removed,
            "skipped_active": skipped_active,
            "skipped_fresh": skipped_fresh,
            "errors": errors,
        }
