from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SH_TZ = ZoneInfo("Asia/Shanghai")


def shanghai_now_iso() -> str:
    return datetime.now(tz=SH_TZ).isoformat(timespec="seconds")


class SQLiteStore:
    def __init__(self, db_path: str) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = path
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS processed_events (
                    event_id TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS issue_runs (
                    issue_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    workspace_path TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL,
                    branch TEXT NOT NULL DEFAULT '',
                    pr_url TEXT NOT NULL DEFAULT '',
                    attempts_json TEXT NOT NULL DEFAULT '{}',
                    review_loops INTEGER NOT NULL DEFAULT 0,
                    arbiter_loops INTEGER NOT NULL DEFAULT 0,
                    failure_class TEXT NOT NULL DEFAULT '',
                    handoff_reason TEXT NOT NULL DEFAULT '',
                    last_stage TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS issue_traces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                """
            )
            self._ensure_issue_runs_columns(conn)

    @staticmethod
    def _ensure_issue_runs_columns(conn: sqlite3.Connection) -> None:
        cols = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(issue_runs)").fetchall()
            if len(row) > 1
        }
        if "description" not in cols:
            conn.execute("ALTER TABLE issue_runs ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        if "workspace_path" not in cols:
            conn.execute("ALTER TABLE issue_runs ADD COLUMN workspace_path TEXT NOT NULL DEFAULT ''")
        if "arbiter_loops" not in cols:
            conn.execute("ALTER TABLE issue_runs ADD COLUMN arbiter_loops INTEGER NOT NULL DEFAULT 0")
        if "failure_class" not in cols:
            conn.execute("ALTER TABLE issue_runs ADD COLUMN failure_class TEXT NOT NULL DEFAULT ''")
        if "handoff_reason" not in cols:
            conn.execute("ALTER TABLE issue_runs ADD COLUMN handoff_reason TEXT NOT NULL DEFAULT ''")

    def is_event_processed(self, event_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT event_id FROM processed_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return row is not None

    def mark_event_processed(self, event_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO processed_events(event_id, processed_at) VALUES(?, ?)",
                (event_id, shanghai_now_iso()),
            )

    def get_issue(self, issue_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM issue_runs WHERE issue_id = ?",
                (issue_id,),
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["attempts"] = json.loads(data.get("attempts_json") or "{}")
        data.pop("attempts_json", None)
        return data

    def upsert_issue(
        self,
        issue_id: str,
        project_id: str,
        title: str,
        state: str,
        description: str = "",
    ) -> None:
        now = shanghai_now_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO issue_runs(issue_id, project_id, title, description, state, updated_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(issue_id) DO UPDATE SET
                    project_id = excluded.project_id,
                    title = CASE WHEN excluded.title != '' THEN excluded.title ELSE issue_runs.title END,
                    description = CASE
                        WHEN excluded.description != '' THEN excluded.description
                        ELSE issue_runs.description
                    END,
                    state = excluded.state,
                    updated_at = excluded.updated_at
                """,
                (issue_id, project_id, title, description, state, now),
            )

    def update_issue_fields(self, issue_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "project_id",
            "title",
            "state",
            "branch",
            "pr_url",
            "description",
            "workspace_path",
            "attempts",
            "review_loops",
            "arbiter_loops",
            "failure_class",
            "handoff_reason",
            "last_stage",
        }
        updates: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "attempts":
                updates.append("attempts_json = ?")
                values.append(json.dumps(value, ensure_ascii=True))
            else:
                updates.append(f"{key} = ?")
                values.append(value)

        if not updates:
            return

        updates.append("updated_at = ?")
        values.append(shanghai_now_iso())
        values.append(issue_id)

        with self._conn() as conn:
            conn.execute(
                f"UPDATE issue_runs SET {', '.join(updates)} WHERE issue_id = ?",
                tuple(values),
            )

    def increment_attempt(self, issue_id: str, stage: str) -> int:
        with self._lock:
            issue = self.get_issue(issue_id)
            if issue is None:
                raise KeyError(f"Issue not found: {issue_id}")
            attempts = issue.get("attempts") or {}
            attempts[str(stage)] = int(attempts.get(str(stage), 0)) + 1
            self.update_issue_fields(issue_id, attempts=attempts)
            return attempts[str(stage)]

    def reset_attempt(self, issue_id: str, stage: str) -> None:
        with self._lock:
            issue = self.get_issue(issue_id)
            if issue is None:
                return
            attempts = issue.get("attempts") or {}
            attempts[str(stage)] = 0
            self.update_issue_fields(issue_id, attempts=attempts)

    def append_trace(
        self,
        issue_id: str,
        stage: str,
        status: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO issue_traces(issue_id, timestamp, stage, status, message, metadata_json)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    issue_id,
                    shanghai_now_iso(),
                    stage,
                    status,
                    message,
                    json.dumps(metadata or {}, ensure_ascii=True),
                ),
            )

    def get_trace(self, issue_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM issue_traces WHERE issue_id = ? ORDER BY id DESC",
                (issue_id,),
            ).fetchall()
        traces: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            record["metadata"] = json.loads(record.get("metadata_json") or "{}")
            record.pop("metadata_json", None)
            traces.append(record)
        return traces

    def list_active_workspace_paths(self) -> set[str]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT workspace_path
                FROM issue_runs
                WHERE workspace_path != ''
                  AND lower(state) NOT IN ('done', 'blocked')
                """
            ).fetchall()
        return {str(row["workspace_path"]).strip() for row in rows if str(row["workspace_path"]).strip()}
