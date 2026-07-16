"""
FastAPI entrypoint.

Lifespan wires the one-time schema bootstrap and the background heat flusher
(the L2 Hot Zone's batch write-back loop) to app start/stop.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.auth import access_guard
from app.config import settings
from app.conversation.night_agent import night_service
from app.conversation.timer import timer_service
from app.db import init_db
from app.memory.l2_hot import heat_tracker
from app.routers import chat, memory

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    heat_tracker.start()
    timer_service.start()   # 她的闹钟:到点主动来找用户
    night_service.start()   # 夜间代理:她睡着后蒸馏记忆/写日记/排明天
    try:
        yield
    finally:
        await night_service.stop()
        await timer_service.stop()
        await heat_tracker.stop()


app = FastAPI(title="Dreamory", version=__version__, lifespan=lifespan)

# 测试期访问控制(ADMIN_TOKEN 为空时 guard 直接放行,行为与 0.6.0 一致):
# 带 chat_id 的路径认该 chat 的 access_token;全局端点(列表/新建/memory)仅认 admin。
app.include_router(chat.router, dependencies=[Depends(access_guard)])
app.include_router(memory.router, dependencies=[Depends(access_guard)])


@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "embedding_backend": settings.embedding_backend,
        "dream_enabled": settings.dream_enabled,
        "timer_enabled": settings.timer_enabled,
    }


# ── Frontend (HTML/CSS/JS) ───────────────────────────────────────────
@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
