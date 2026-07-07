"""
Per-chat in-memory event bus — the push channel for agent-initiated messages.

选型说明:主动消息(定时器到点、未来的多段延迟连发)需要服务端 → 浏览器的推送。
相比 long-polling(每次都空转一整个请求周期)和 WebSocket(双向、握手/心跳都要自管),
SSE(Server-Sent Events)正好卡在需求点上:单向下行、原生断线重连、纯 HTTP、
前端一行 `new EventSource(url)`。

这是一个"可丢弃"的派生结构:订阅者掉线丢几条推送没关系,消息本体永远在 L3,
前端重连/刷新时通过 GET /messages 补齐 —— 符合铁律(缓存可丢,L3 是唯一真相)。
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections import defaultdict


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[uuid.UUID, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, chat_id: uuid.UUID) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subs[chat_id].add(q)
        return q

    def unsubscribe(self, chat_id: uuid.UUID, q: asyncio.Queue) -> None:
        subs = self._subs.get(chat_id)
        if subs:
            subs.discard(q)
            if not subs:
                self._subs.pop(chat_id, None)

    def publish(self, chat_id: uuid.UUID, event: dict) -> None:
        """Fire-and-forget:队列满(客户端卡死)就丢弃,绝不阻塞发布方。"""
        for q in self._subs.get(chat_id, ()):  # 无订阅者时静默丢弃
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


def sse_format(event: dict) -> str:
    """dict → SSE wire format(单条 data 行,JSON 载荷)。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


# module-level singleton(与 heat_tracker 同款模式)
event_bus = EventBus()
