from __future__ import annotations

import subprocess
from typing import Any


def run_git(local_path: str, args: list[str]) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            args,
            cwd=local_path,
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
    return True, output.strip()


def collect_review_context(local_path: str, base_branch: str, branch: str) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "review_base_branch": base_branch,
        "review_branch": branch,
        "review_changed_files": [],
        "review_diff": "",
        "review_diff_range": "",
        "review_diff_truncated": False,
        "review_diff_files_included": [],
    }
    if not local_path:
        meta["review_context_error"] = "workspace path is empty"
        return meta

    ok_git, _ = run_git(local_path, ["git", "rev-parse", "--is-inside-work-tree"])
    if not ok_git:
        meta["review_context_error"] = f"not a git repo: {local_path}"
        return meta

    ok_head, head_branch = run_git(local_path, ["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if ok_head and head_branch and not meta["review_branch"]:
        meta["review_branch"] = head_branch

    branch_ref = str(meta["review_branch"] or "").strip()
    range_candidates: list[str] = []
    if branch_ref and branch_ref != "HEAD":
        range_candidates.extend(
            [
                f"origin/{base_branch}...{branch_ref}",
                f"{base_branch}...{branch_ref}",
                f"origin/{base_branch}..{branch_ref}",
                f"{base_branch}..{branch_ref}",
            ]
        )
    range_candidates.extend(["origin/HEAD..HEAD", "HEAD~1..HEAD"])

    chosen_range = ""
    changed_files: list[str] = []
    diff_text = ""
    last_error = ""
    for rng in range_candidates:
        ok_files, files_out = run_git(local_path, ["git", "diff", "--name-only", rng])
        ok_diff, diff_out = run_git(local_path, ["git", "diff", "--no-color", rng])
        if not ok_files or not ok_diff:
            last_error = files_out if not ok_files else diff_out
            continue
        chosen_range = rng
        changed_files = [x.strip() for x in files_out.splitlines() if x.strip()]
        diff_text = diff_out
        break

    if not chosen_range:
        ok_files, files_out = run_git(local_path, ["git", "diff", "--name-only"])
        ok_diff, diff_out = run_git(local_path, ["git", "diff", "--no-color"])
        if ok_files and ok_diff:
            chosen_range = "working-tree"
            changed_files = [x.strip() for x in files_out.splitlines() if x.strip()]
            diff_text = diff_out
        else:
            meta["review_context_error"] = last_error or files_out or diff_out or "failed to collect diff"
            return meta

    max_chars = 50_000
    meta["review_diff_range"] = chosen_range
    meta["review_changed_files"] = changed_files

    if changed_files:
        sliced_diff, included_files, truncated = collect_diff_by_files(
            local_path=local_path,
            diff_range=chosen_range,
            changed_files=changed_files,
            max_chars=max_chars,
        )
        meta["review_diff"] = sliced_diff
        meta["review_diff_files_included"] = included_files
        meta["review_diff_truncated"] = truncated
    else:
        meta["review_diff_truncated"] = len(diff_text) > max_chars
        meta["review_diff"] = diff_text[:max_chars]

    if not str(meta["review_diff"]).strip():
        meta["review_context_error"] = "empty diff"
    return meta


def collect_diff_by_files(
    local_path: str,
    diff_range: str,
    changed_files: list[str],
    max_chars: int,
) -> tuple[str, list[str], bool]:
    ordered_files = prioritize_review_files(changed_files)
    chunks: list[str] = []
    included: list[str] = []
    total = 0
    truncated = False

    for path in ordered_files:
        ok_patch, patch_out = run_git(
            local_path,
            ["git", "diff", "--no-color", diff_range, "--", path],
        )
        if not ok_patch or not patch_out.strip():
            continue
        chunk = patch_out.strip()
        remaining = max_chars - total
        if remaining <= 0:
            truncated = True
            break
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            included.append(path)
            truncated = True
            total = max_chars
            break
        chunks.append(chunk)
        included.append(path)
        total += len(chunk)

    if not chunks:
        return "", [], False

    return "\n\n".join(chunks), included, truncated


def prioritize_review_files(changed_files: list[str]) -> list[str]:
    def score(path: str) -> tuple[int, str]:
        p = path.lower()
        if p.startswith("tests/"):
            return (0, p)
        if "orchestrator" in p:
            return (1, p)
        if p.startswith("src/"):
            return (2, p)
        return (3, p)

    return sorted(changed_files, key=score)

