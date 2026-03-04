from __future__ import annotations

import sqlite3
from pathlib import Path

from app.store import SQLiteStore


def _create_legacy_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS issue_runs (
            issue_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL,
            branch TEXT NOT NULL DEFAULT '',
            pr_url TEXT NOT NULL DEFAULT '',
            attempts_json TEXT NOT NULL DEFAULT '{}',
            review_loops INTEGER NOT NULL DEFAULT 0,
            last_stage TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def test_store_migrates_issue_runs_add_description_column(tmp_path: Path):
    db_path = tmp_path / "legacy.sqlite"
    _create_legacy_db(db_path)

    SQLiteStore(str(db_path))

    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(issue_runs)").fetchall()}
    conn.close()
    assert "description" in cols
    assert "arbiter_loops" in cols
    assert "failure_class" in cols
    assert "handoff_reason" in cols


def test_store_upsert_issue_preserves_description_when_blank(tmp_path: Path):
    store = SQLiteStore(str(tmp_path / "store.sqlite"))
    store.upsert_issue("i-1", "p-1", "title", "Todo", description="desc-v1")
    store.upsert_issue("i-1", "p-1", "title2", "Design", description="")

    issue = store.get_issue("i-1")
    assert issue is not None
    assert issue["title"] == "title2"
    assert issue["description"] == "desc-v1"


def test_store_update_issue_failure_fields(tmp_path: Path):
    store = SQLiteStore(str(tmp_path / "store2.sqlite"))
    store.upsert_issue("i-2", "p-1", "title", "Todo", description="desc")
    store.update_issue_fields(
        "i-2",
        failure_class="PROTOCOL_VIOLATION",
        handoff_reason="protocol_violation_consecutive",
    )
    issue = store.get_issue("i-2")
    assert issue is not None
    assert issue["failure_class"] == "PROTOCOL_VIOLATION"
    assert issue["handoff_reason"] == "protocol_violation_consecutive"
