from __future__ import annotations

from app.models import Stage, StageResult, StageStatus


class FakePlaneClient:
    def __init__(self) -> None:
        self.state_updates: list[tuple[str, str, str]] = []
        self.comments: list[tuple[str, str, str]] = []
        self.description_updates: list[tuple[str, str, str]] = []

    async def update_work_item_state(
        self,
        project_id: str,
        issue_id: str,
        state_name: str,
        state_map=None,
    ) -> None:
        self.state_updates.append((project_id, issue_id, state_name))

    async def add_comment(self, project_id: str, issue_id: str, comment: str) -> None:
        self.comments.append((project_id, issue_id, comment))

    async def update_work_item_description(
        self,
        project_id: str,
        issue_id: str,
        description_html: str,
    ) -> None:
        self.description_updates.append((project_id, issue_id, description_html))


class FakeGitHubClient:
    class PR:
        def __init__(self, branch: str, pr_url: str) -> None:
            self.branch = branch
            self.pr_url = pr_url

    def __init__(self) -> None:
        self.comments: list[tuple[str, str, str]] = []

    def create_branch_commit_and_pr(
        self,
        issue_id: str,
        title: str,
        body: str,
        local_path: str,
        base_branch: str,
        repo_url: str = "",
    ):
        return self.PR(branch=f"plane/{issue_id}", pr_url=f"https://example.com/pr/{issue_id}")

    def add_pr_comment(self, pr_url: str, body: str, local_path: str) -> None:
        self.comments.append((pr_url, body, local_path))


class ScriptedAgent:
    def __init__(self, scripted: dict[Stage, list[StageResult]] | None = None) -> None:
        self.scripted = scripted or {}

    async def run_stage(self, stage: Stage, context):
        queue = self.scripted.get(stage, [])
        if queue:
            return queue.pop(0)
        if stage == Stage.DESIGN:
            return StageResult(
                status=StageStatus.SUCCESS,
                summary="design ok",
                artifacts={
                    "stdout": (
                        "TDD_RED: - RED-001: 先失败\n"
                        "TDD_GREEN: - 最小实现\n"
                        "TDD_REFACTOR: - 重构命名\n"
                        "TDD_ACCEPTANCE: - 回归通过"
                    )
                },
            )
        if stage == Stage.CODING:
            return StageResult(
                status=StageStatus.SUCCESS,
                summary="coding ok",
                artifacts={
                    "stdout": (
                        "RED_RESULT: 已执行失败测试\n"
                        "GREEN_RESULT: 最小实现通过\n"
                        "REFACTOR_NOTE: 提取重复代码\n"
                        "CHANGED_FILES: src/app/orchestrator.py"
                    )
                },
            )
        if stage == Stage.REVIEW:
            return StageResult(
                status=StageStatus.SUCCESS,
                summary="review ok",
                artifacts={"stdout": "APPROVED\n证据完整"},
            )
        return StageResult(status=StageStatus.SUCCESS, summary=f"{stage.value} ok")

