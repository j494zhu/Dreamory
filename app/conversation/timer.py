"""
Timer service — 让她有时间感,并能"5分钟后主动来找你"。

工作方式(与 heat_tracker 同款的 lifespan 后台服务):
  1. 生成端在回复末尾用 <timer minutes="X">备忘</timer> 约一个闹钟
     (pipeline 解析后写入 timer_pings 表,持久化,重启不丢)。
  2. 本服务每 TIMER_POLL_SECONDS 秒扫一次到点的 pending 闹钟。
  3. 到点后先把状态置为 firing(认领,防止重复触发),再发起一次
     对用户隐藏的 LLM 调用(pipeline.handle_timer_fire):走完整 L1 组装 +
     注入"你之前说过到点来找他"的情境,生成主动消息、落 L3、存状态。
  4. 生成的消息经 event_bus(SSE)推给在线的前端;离线也没关系,
     消息已在 L3,下次打开对话时自然可见 —— 就像真的收到了她的留言。

失败处理:标记 failed 并记日志,绝不让后台闹钟把主流程带崩。
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.config import settings
from app.models import Chat, TimerPing, now_ms

logger = logging.getLogger(__name__)


class TimerService:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def _tick(self) -> None:
        """扫描并触发所有到点的闹钟。每个闹钟独立 session,互不拖累。"""
        from app.db import SessionLocal

        async with SessionLocal() as session:
            due = (
                await session.execute(
                    select(TimerPing)
                    .where(TimerPing.status == "pending", TimerPing.due_ms <= now_ms())
                    .order_by(TimerPing.due_ms.asc())
                    .limit(8)
                )
            ).scalars().all()
            if not due:
                return
            # 认领:先置 firing 并提交,进程内轮询不会重复拿到
            for ping in due:
                ping.status = "firing"
            await session.commit()
            claimed = [(p.id, p.chat_id, p.topic, p.due_ms) for p in due]

        for ping_id, chat_id, topic, due_at in claimed:
            await self._fire(ping_id, chat_id, topic, due_at)

    async def _fire(self, ping_id, chat_id, topic: str, due_ms: int) -> None:
        from app.conversation import pipeline
        from app.conversation.bus import event_bus
        from app.db import SessionLocal

        try:
            async with SessionLocal() as session:
                chat = await session.get(Chat, chat_id)
                ping = await session.get(TimerPing, ping_id)
                if chat is None:  # 对话已删,闹钟作废
                    if ping:
                        ping.status = "failed"
                        await session.commit()
                    return
                payload = await pipeline.handle_timer_fire(session, chat, topic, due_ms)
                if ping:
                    ping.status = "fired"
                await session.commit()
            # 提交之后才推送:前端收到的消息一定已经在 L3 里了
            event_bus.publish(chat_id, payload)
            logger.info("timer fired for chat %s: %s", chat_id, topic[:50])
        except Exception:
            logger.exception("timer fire failed (chat %s)", chat_id)
            try:
                async with SessionLocal() as session:
                    ping = await session.get(TimerPing, ping_id)
                    if ping:
                        ping.status = "failed"
                        await session.commit()
            except Exception:
                logger.exception("failed to mark timer ping %s as failed", ping_id)

    # ── background loop, owned by the app lifespan ───────────────────
    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=settings.timer_poll_seconds
                    )
                except asyncio.TimeoutError:
                    pass
                if self._stop.is_set():
                    return
                try:
                    await self._tick()
                except Exception:  # 轮询自身出错也不能停摆
                    logger.exception("timer tick failed")
        except asyncio.CancelledError:
            pass

    def start(self) -> None:
        if self._task is None and settings.timer_enabled:
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


# module-level singleton
timer_service = TimerService()
