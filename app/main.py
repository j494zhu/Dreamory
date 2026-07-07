"""
FastAPI entrypoint.

Lifespan wires the one-time schema bootstrap and the background heat flusher
(the L2 Hot Zone's batch write-back loop) to app start/stop.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import settings
from app.db import init_db
from app.memory.l2_hot import heat_tracker
from app.routers import chat, memory

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    heat_tracker.start()
    try:
        yield
    finally:
        await heat_tracker.stop()


app = FastAPI(title="Dreamory", version=__version__, lifespan=lifespan)

app.include_router(chat.router)
app.include_router(memory.router)


@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "embedding_backend": settings.embedding_backend,
        "dream_enabled": settings.dream_enabled,
    }


# ── Frontend (HTML/CSS/JS) ───────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
