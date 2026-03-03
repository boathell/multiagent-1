from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/internal", tags=["internal"])


@router.post("/issues/{issue_id}/retry")
async def retry_issue(issue_id: str, request: Request) -> dict:
    orchestrator = request.app.state.orchestrator
    result = await orchestrator.retry_issue(issue_id)
    if result["status"] == "not_found":
        raise HTTPException(status_code=404, detail="issue not found")
    return {"ok": True, "result": result}


@router.get("/issues/{issue_id}/trace")
async def get_trace(issue_id: str, request: Request) -> dict:
    orchestrator = request.app.state.orchestrator
    trace = orchestrator.get_trace(issue_id)
    return {"ok": True, "issue_id": issue_id, "trace": trace}

