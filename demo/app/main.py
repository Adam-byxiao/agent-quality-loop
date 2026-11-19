"""Platform API and the demo UI host."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import runner, store

WEB = Path(__file__).resolve().parent.parent / "web"


def _seed() -> None:
    if store.has_cases():
        return
    last_error: Exception | None = None
    for _ in range(30):
        try:
            store.import_sessions(runner.fetch_sessions())
            return
        except Exception as exc:  # noqa: BLE001 - stand-in may still be starting
            last_error = exc
            time.sleep(0.2)
    print(f"seed import skipped: {last_error}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.init()
    _seed()
    yield


app = FastAPI(title="Agent Quality Loop demo", lifespan=lifespan)


class BatchCreate(BaseModel):
    cycle: str = "演示迭代"
    failed_turn_ids: list[str] = []


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "platform"}


@app.get("/api/overview")
def overview():
    return store.overview()


@app.post("/api/import")
def import_sessions():
    try:
        payload = runner.fetch_sessions()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"observability unavailable: {exc}") from exc
    return store.import_sessions(payload)


@app.get("/api/failed-turns")
def failed_turns():
    return store.list_failed_turns()


@app.get("/api/failed-turns/{failed_turn_id}")
def failed_turn(failed_turn_id: str):
    item = store.get_failed_turn(failed_turn_id)
    if not item:
        raise HTTPException(status_code=404, detail="failed turn not found")
    return item


@app.post("/api/batches")
def create_batch(payload: BatchCreate):
    ids = payload.failed_turn_ids or [item["id"] for item in store.list_failed_turns()]
    if not ids:
        raise HTTPException(status_code=400, detail="no failed turns to repair")
    batch_id = store.create_batch(payload.cycle, ids)
    return {"batch_id": batch_id}


@app.get("/api/batches")
def batches():
    return store.list_batches()


@app.get("/api/batches/{batch_id}")
def batch(batch_id: str):
    item = store.get_batch(batch_id)
    if not item:
        raise HTTPException(status_code=404, detail="batch not found")
    return {
        "batch": item,
        "items": store.batch_items(batch_id),
        "assets": store.assets(batch_id),
        "running": runner.is_running(batch_id),
    }


@app.post("/api/batches/{batch_id}/start")
def start_batch(batch_id: str):
    item = store.get_batch(batch_id)
    if not item:
        raise HTTPException(status_code=404, detail="batch not found")
    if item["status"] != "frozen":
        raise HTTPException(status_code=409, detail="only frozen batches can start")
    if not runner.start(batch_id):
        raise HTTPException(status_code=409, detail="batch is already running")
    return {"started": True}


@app.post("/api/batches/{batch_id}/stop")
def stop_batch(batch_id: str):
    item = store.get_batch(batch_id)
    if not item:
        raise HTTPException(status_code=404, detail="batch not found")
    if item["status"] != "running":
        raise HTTPException(status_code=409, detail="batch is not running")
    runner.request_stop(batch_id)
    return {"stopping": True}


@app.get("/api/batches/{batch_id}/events")
def batch_events(batch_id: str, after_id: int = 0):
    if not store.get_batch(batch_id):
        raise HTTPException(status_code=404, detail="batch not found")
    return store.events(batch_id, after_id)


@app.get("/api/batches/{batch_id}/assets")
def batch_assets(batch_id: str):
    if not store.get_batch(batch_id):
        raise HTTPException(status_code=404, detail="batch not found")
    return store.assets(batch_id)


@app.get("/api/batches/{batch_id}/report")
def batch_report(batch_id: str):
    if not store.get_batch(batch_id):
        raise HTTPException(status_code=404, detail="batch not found")
    report = store.get_report(batch_id)
    if not report:
        return JSONResponse(status_code=404, content={"detail": "batch has not finished"})
    return report


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/app.js")
def app_js():
    return FileResponse(WEB / "app.js", media_type="application/javascript")


@app.get("/style.css")
def style_css():
    return FileResponse(WEB / "style.css", media_type="text/css")
