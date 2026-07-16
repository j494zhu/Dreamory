"""
访问控制(0.6.1,测试期)— 不做用户系统,做"每个 chat 一把钥匙"。

- ADMIN_TOKEN 为空 → 鉴权整体关闭(本地开发/离线测试完全不受影响)。
- 设置后:
    * 带 admin token(header `X-Access-Token` 或 `?token=`,SSE 只能走 query)
      → 全部端点放行;
    * 带某个 chat 的 access_token → 只放行该 chat 的路径(/api/chats/{chat_id}/…);
    * 其余请求(列出所有 chat、新建 chat、memory/Dream 等全局端点)→ 401。
- 测试者拿到的是 `/?chat=<id>&token=<access_token>` 形式的专属链接,彼此隔离;
  正经的多用户账号系统等产品化再说。

guard 以 router 级依赖挂在 main.py(include_router),不侵入各端点签名。
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException, Request

from app.config import settings


def extract_token(request: Request) -> str:
    return (request.headers.get("x-access-token")
            or request.query_params.get("token")
            or "")


def check(token: str, chat_token: str | None, admin_token: str) -> bool:
    """纯决策(可单测):admin 全通;chat token 只对本 chat 有效;鉴权关闭全通。"""
    if not admin_token:
        return True
    if token and token == admin_token:
        return True
    return bool(token and chat_token and token == chat_token)


async def access_guard(request: Request) -> None:
    """router 级依赖。带 chat_id 的路径查库比对该 chat 的钥匙,其余仅认 admin。"""
    if not settings.admin_token:
        return
    token = extract_token(request)
    if token == settings.admin_token:
        return

    raw = request.path_params.get("chat_id")
    if raw and token:
        try:
            cid = uuid.UUID(str(raw))
        except ValueError:
            raise HTTPException(401, "access denied")
        from app.db import SessionLocal
        from app.models import Chat

        async with SessionLocal() as s:
            chat = await s.get(Chat, cid)
        if chat is not None and check(token, chat.access_token, settings.admin_token):
            return
    raise HTTPException(401, "access denied")
