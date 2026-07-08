"""
可注入时钟 — 全项目时间语义的唯一入口。

生产环境 offset 恒为 0,行为与直接调 time.time() 完全一致。
加速老化测试(scripts/simulate.py)通过 advance() 把"现在"往前拨:
一晚上的冷落、三天的离线、一周的相处,都可以在几分钟内跑完——
时间效应(激素衰减/好感度缓降/会话边界/作息判定/夜间代理)全部真实生效。

规则:业务代码不再直接调 time.time()/datetime.now()/int(time.time()*1000),
一律走 clock.now_s() / clock.now_dt() / clock.now_ms()。
(uuid7 主键仍用真实时间——它只承担身份+粗排序,回拨反而会破坏单调性。)
"""
from __future__ import annotations

import time
from datetime import datetime

_offset_ms: int = 0


def now_ms() -> int:
    return int(time.time() * 1000) + _offset_ms


def now_s() -> float:
    return time.time() + _offset_ms / 1000.0


def now_dt() -> datetime:
    return datetime.fromtimestamp(now_s())


def advance(seconds: float) -> None:
    """把"现在"往前拨(仅测试/模拟用;生产代码永远不该调用)。"""
    global _offset_ms
    _offset_ms += int(seconds * 1000)


def offset_ms() -> int:
    return _offset_ms


def reset() -> None:
    """归零(测试隔离用)。"""
    global _offset_ms
    _offset_ms = 0
