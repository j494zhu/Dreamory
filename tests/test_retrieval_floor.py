"""自动检索的绝对相关性下限(纯函数 apply_score_floor)。

背景:kNN top-K 永远会凑满 K 条 —— 池子里没有相关内容时,"最不不相关"的
底噪照样进 L1(实测:新对话检索池只剩生活正史,0.4x 的冰淇淋/花店全数命中)。
0.2.2 只给工具路径加了下限,自动路径漏了;现在下限收口进 retrieve() 本身。

    pytest tests/test_retrieval_floor.py -v
"""
from app.memory.retrieval import Hit, apply_score_floor
from app.models import Memory, MemoryKind


def _hit(raw: float, kind: MemoryKind = MemoryKind.message) -> Hit:
    return Hit(Memory(kind=kind), score=raw, axis="content", raw=raw)


def test_floor_drops_noise_keeps_relevant():
    hits = [_hit(0.72), _hit(0.51), _hit(0.47), _hit(0.36)]
    kept = apply_score_floor(hits, min_score=0.50, life_min_score=0.60)
    assert [h.raw for h in kept] == [0.72, 0.51]


def test_life_event_needs_higher_bar():
    """生活正史归话题种子通道管:0.5x 的擦边命中不该被自动'想起',真聊到才行。"""
    hits = [_hit(0.55, MemoryKind.message), _hit(0.55, MemoryKind.life_event),
            _hit(0.65, MemoryKind.life_event)]
    kept = apply_score_floor(hits, min_score=0.50, life_min_score=0.60)
    assert [(h.memory.kind, h.raw) for h in kept] == [
        (MemoryKind.message, 0.55), (MemoryKind.life_event, 0.65)]


def test_floor_uses_raw_not_biased_score():
    """goal 偏置是排序手段,不是相关性:偏置抬高的 score 救不了低裸分的命中。"""
    h = Hit(Memory(kind=MemoryKind.message), score=0.70, axis="content", raw=0.42)
    assert apply_score_floor([h], min_score=0.50, life_min_score=0.60) == []


def test_zero_floor_disables_filtering():
    """工具路径(她的主动搜索)关掉内置下限,自己做两段式过滤。"""
    hits = [_hit(0.30), _hit(0.10, MemoryKind.life_event)]
    assert apply_score_floor(hits, min_score=0.0, life_min_score=0.60) == hits


def test_life_bar_never_below_general_floor():
    """误配置(life 线 < 通用线)时取两者较高者,不会给生活正史开后门。"""
    hits = [_hit(0.45, MemoryKind.life_event)]
    assert apply_score_floor(hits, min_score=0.50, life_min_score=0.30) == []
