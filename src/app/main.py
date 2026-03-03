from __future__ import annotations

from fastapi import FastAPI

from app.adapters.agents.cli_adapter import CliAgentAdapter
from app.adapters.agents.mock_adapter import MockAgentAdapter
from app.adapters.github_client import GitHubClient
from app.adapters.plane_client import PlaneClient
from app.api.internal import router as internal_router
from app.api.webhooks import router as webhook_router
from app.config import AppConfig, load_app_config
from app.logging_config import setup_logging
from app.notify.feishu import FeishuNotifier
from app.orchestrator import Orchestrator
from app.quality_gate import QualityGate
from app.store import SQLiteStore


def create_app(app_config: AppConfig | None = None) -> FastAPI:
    config = app_config or load_app_config()
    setup_logging(config.settings.app_log_level)

    store = SQLiteStore(config.settings.app_sqlite_path)
    plane_client = PlaneClient(config.settings)
    github_client = GitHubClient(config.settings)
    if config.settings.agents_use_mock:
        agent_adapter = MockAgentAdapter()
    else:
        agent_adapter = CliAgentAdapter(config.agent_config)
    quality_gate = QualityGate()
    feishu_notifier = FeishuNotifier(
        webhook_url=config.settings.feishu_webhook_url,
        signing_secret=config.settings.feishu_signing_secret,
    )

    orchestrator = Orchestrator(
        app_config=config,
        store=store,
        plane_client=plane_client,
        github_client=github_client,
        agent_adapter=agent_adapter,
        quality_gate=quality_gate,
    )

    app = FastAPI(title="multiagent-orchestrator", version="0.1.0")
    app.state.config = config
    app.state.store = store
    app.state.orchestrator = orchestrator
    app.state.feishu_notifier = feishu_notifier

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    app.include_router(webhook_router)
    app.include_router(internal_router)

    return app


app = create_app()
