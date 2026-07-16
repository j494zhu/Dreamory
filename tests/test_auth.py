"""
测试期访问控制(0.6.1)单测:token 提取 + 纯决策逻辑。不碰 DB / 网络。

    pytest tests/test_auth.py -v
"""
from types import SimpleNamespace

from app import auth


def _req(headers=None, query=None):
    return SimpleNamespace(headers=headers or {}, query_params=query or {},
                           path_params={})


# ── extract_token:header 优先,query 兜底(SSE 只能走 query)──────────
def test_extract_token_prefers_header():
    r = _req(headers={"x-access-token": "H"}, query={"token": "Q"})
    assert auth.extract_token(r) == "H"


def test_extract_token_falls_back_to_query():
    assert auth.extract_token(_req(query={"token": "Q"})) == "Q"
    assert auth.extract_token(_req()) == ""


# ── check:纯决策 ─────────────────────────────────────────────────────
def test_auth_disabled_when_admin_token_empty():
    # ADMIN_TOKEN 未设置 → 鉴权整体关闭(本地开发,行为与 0.6.0 一致)
    assert auth.check("", None, "")
    assert auth.check("anything", "whatever", "")


def test_admin_token_passes_everything():
    assert auth.check("adm", None, "adm")
    assert auth.check("adm", "chat-key", "adm")


def test_chat_token_only_opens_its_own_door():
    assert auth.check("chat-key", "chat-key", "adm")          # 本 chat:放行
    assert not auth.check("chat-key", "other-key", "adm")     # 别的 chat:拒绝
    assert not auth.check("chat-key", None, "adm")            # 全局端点:拒绝
    assert not auth.check("", "chat-key", "adm")              # 没带钥匙:拒绝
    assert not auth.check("wrong", "chat-key", "adm")


def test_empty_chat_token_never_matches_empty_request_token():
    # 旧行还没补钥匙时,空对空不能算配对
    assert not auth.check("", "", "adm")
    assert not auth.check("", None, "adm")
