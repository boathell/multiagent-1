from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import httpx

from app.config import Settings


class PlaneClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._logger = logging.getLogger("app.adapters.plane")
        self._state_cache: dict[tuple[str, str], dict[str, str]] = {}

    def _enabled(self) -> bool:
        return bool(self._settings.plane_base_url and self._settings.plane_api_token)

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self._settings.plane_api_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @staticmethod
    def _is_uuid(value: str) -> bool:
        try:
            UUID(str(value))
            return True
        except ValueError:
            return False

    async def _fetch_state_name_map(self, workspace: str, project_id: str) -> dict[str, str]:
        key = (workspace, project_id)
        cached = self._state_cache.get(key)
        if cached is not None:
            return cached

        endpoint = (
            f"{self._settings.plane_base_url}/api/v1/workspaces/{workspace}"
            f"/projects/{project_id}/states/"
        )
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.get(endpoint, headers=self._headers())
            if response.status_code >= 300:
                self._logger.warning(
                    "Plane list states failed project_id=%s status=%s body=%s",
                    project_id,
                    response.status_code,
                    response.text,
                )
                return {}
            data = response.json()
        mapping: dict[str, str] = {}
        for item in data.get("results", []):
            state_id = item.get("id")
            state_name = item.get("name")
            if state_id and state_name:
                mapping[str(state_name)] = str(state_id)
        self._state_cache[key] = mapping
        return mapping

    async def update_work_item_state(
        self,
        project_id: str,
        issue_id: str,
        state_name: str,
        state_map: dict[str, str] | None = None,
    ) -> None:
        if not self._enabled():
            self._logger.info(
                "Plane disabled, skip update state issue_id=%s state=%s",
                issue_id,
                state_name,
            )
            return

        workspace = self._settings.plane_workspace_slug
        target = (state_map or {}).get(state_name, state_name)
        state_id = target if self._is_uuid(target) else None
        if state_id is None:
            state_name_map = await self._fetch_state_name_map(workspace=workspace, project_id=project_id)
            state_id = state_name_map.get(target) or state_name_map.get(state_name)
        payload: dict[str, Any] = {}
        if state_id:
            payload["state"] = state_id
        else:
            payload["state_name"] = target
            self._logger.warning(
                "Plane state id unresolved project_id=%s requested_state=%s target=%s",
                project_id,
                state_name,
                target,
            )

        endpoint = (
            f"{self._settings.plane_base_url}/api/v1/workspaces/{workspace}"
            f"/projects/{project_id}/work-items/{issue_id}/"
        )
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.patch(endpoint, headers=self._headers(), json=payload)
            if response.status_code >= 300:
                raise RuntimeError(
                    f"Plane update state failed: {response.status_code} {response.text}"
                )

    async def add_comment(self, project_id: str, issue_id: str, comment: str) -> None:
        if not self._enabled():
            self._logger.info("Plane disabled, skip comment issue_id=%s", issue_id)
            return

        workspace = self._settings.plane_workspace_slug
        endpoint = (
            f"{self._settings.plane_base_url}/api/v1/workspaces/{workspace}"
            f"/projects/{project_id}/work-items/{issue_id}/comments/"
        )
        payload = {"comment_html": comment}
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.post(endpoint, headers=self._headers(), json=payload)
            if response.status_code >= 300:
                raise RuntimeError(
                    f"Plane add comment failed: {response.status_code} {response.text}"
                )

    async def update_work_item_description(
        self,
        project_id: str,
        issue_id: str,
        description_html: str,
    ) -> None:
        if not self._enabled():
            self._logger.info("Plane disabled, skip description update issue_id=%s", issue_id)
            return

        workspace = self._settings.plane_workspace_slug
        endpoint = (
            f"{self._settings.plane_base_url}/api/v1/workspaces/{workspace}"
            f"/projects/{project_id}/work-items/{issue_id}/"
        )
        payload = {"description_html": description_html}
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.patch(endpoint, headers=self._headers(), json=payload)
            if response.status_code >= 300:
                raise RuntimeError(
                    f"Plane update description failed: {response.status_code} {response.text}"
                )
