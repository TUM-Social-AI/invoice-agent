"""FastAPI app: upload a PDF, start a run, stream its progress as Server-Sent Events."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from src.ui.events import RunHub
from src.ui.runner import MAX_UPLOAD_BYTES, RunManager, UploadError

STATIC_DIR = Path(__file__).parent / "static"
POLL_INTERVAL_S = 0.15
HEARTBEAT_EVERY_S = 15.0


def _sse(event: dict) -> str:
    return f"id: {event['seq']}\nevent: {event['type']}\ndata: {json.dumps(event, default=str)}\n\n"


async def _event_stream(hub: RunHub, request: Request, start_after: int) -> AsyncIterator[str]:
    sent = start_after
    idle = 0.0
    while True:
        batch = hub.events_after(sent)
        if batch:
            for event in batch:
                yield _sse(event)
            sent = batch[-1]["seq"]
            idle = 0.0
            continue
        if hub.finished:
            # Re-check once: the final event may have landed between the read and the state check.
            if not hub.events_after(sent):
                return
            continue
        if await request.is_disconnected():
            return
        await asyncio.sleep(POLL_INTERVAL_S)
        idle += POLL_INTERVAL_S
        if idle >= HEARTBEAT_EVERY_S:
            yield ": keep-alive\n\n"
            idle = 0.0


def create_app(manager: RunManager) -> FastAPI:
    app = FastAPI(title="Invoice Agent UI")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.post("/api/runs", status_code=201)
    async def create_run(file: UploadFile = File(...)) -> dict:
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        try:
            hub = manager.submit(file.filename or "invoice.pdf", data)
        except UploadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return hub.snapshot()

    @app.get("/api/runs")
    def list_runs() -> list[dict]:
        return manager.list_runs()

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        hub = manager.get(run_id)
        if hub is None:
            raise HTTPException(status_code=404, detail="Unknown run")
        return hub.snapshot()

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request) -> StreamingResponse:
        hub = manager.get(run_id)
        if hub is None:
            raise HTTPException(status_code=404, detail="Unknown run")
        # EventSource resends the last seen id on auto-reconnect, so a dropped connection resumes.
        try:
            start_after = int(request.headers.get("last-event-id", "0"))
        except ValueError:
            start_after = 0
        return StreamingResponse(
            _event_stream(hub, request, start_after),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
