from pathlib import Path

from fastapi.testclient import TestClient

from app.adapters.agents.cli_adapter import CliAgentAdapter
from app.main import create_app


def test_healthz(make_config, tmp_path: Path):
    app = create_app(make_config(project_id="p1"))
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_webhook_and_trace(make_config, tmp_path: Path):
    app = create_app(make_config(project_id="p1"))
    client = TestClient(app)

    payload = {
        "event": "work_item.created",
        "event_id": "evt-api-1",
        "data": {
            "work_item": {
                "id": "2001",
                "project_id": "p1",
                "name": "api flow",
                "state_name": "Todo",
                "updated_at": "2026-03-03T00:00:00Z",
            }
        },
    }

    resp = client.post("/webhooks/plane", json=payload)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    trace_resp = client.get("/internal/issues/2001/trace")
    assert trace_resp.status_code == 200
    assert trace_resp.json()["ok"] is True
    assert len(trace_resp.json()["trace"]) > 0


def test_create_app_uses_cli_adapter_when_mock_disabled(make_config):
    cfg = make_config(project_id="p1")
    cfg.settings.agents_use_mock = False
    cfg.agent_config = {
        "agents": {
            "claude": {"command": "kimi-cli", "timeout_sec": 300},
            "codex": {"command": "codex", "timeout_sec": 300},
            "gemini": {"command": "gemini", "timeout_sec": 300},
        }
    }
    app = create_app(cfg)
    assert isinstance(app.state.orchestrator.agent_adapter, CliAgentAdapter)
