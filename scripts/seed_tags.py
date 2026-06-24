"""
Cold-start the controlled tag vocabulary (M2: "先用手动种子词表冷启动").

Tags are organised by FACET (orthogonal dimensions), not free-form labels, so the
vocabulary stays small and each facet is its own controlled mini-lexicon.

    python -m scripts.seed_tags
"""
import asyncio

from app.db import SessionLocal, init_db
from app.memory import tags as tag_ops

# facet -> [(tag, example texts that define its centroid)]
SEEDS: dict[str, dict[str, list[str]]] = {
    "domain": {
        "work": ["今天加班到很晚", "项目上线压力好大", "老板又改需求了", "开了一整天的会"],
        "health": ["最近睡不好", "去医院检查了身体", "感冒了好难受", "开始健身减肥"],
        "relationship": ["我们之间最近有点冷", "异地恋好辛苦", "好想见你", "我们纪念日快到了"],
        "family": ["我妈又催婚了", "回家陪爸妈吃饭", "弟弟考上大学了"],
    },
    "type": {
        "venting": ["我真的受够了", "今天太倒霉了", "烦死了不想说话"],
        "sharing-joy": ["我升职啦", "今天遇到超开心的事", "我们去看了演唱会好棒"],
        "plan": ["这周末一起出去玩吧", "我们计划一下旅行", "下个月我去找你"],
        "conflict": ["你为什么总是这样", "我们又吵架了", "你根本不在乎我"],
    },
    "time": {
        "anniversary": ["今天是我们在一起两周年", "纪念日快乐", "认识你整整三年了"],
    },
}


async def main() -> None:
    await init_db()
    n = 0
    async with SessionLocal() as session:
        for facet, tags in SEEDS.items():
            for name, examples in tags.items():
                await tag_ops.seed_tag(session, name=name, facet=facet, example_texts=examples)
                n += 1
        await session.commit()
    print(f"✓ seeded {n} tags across {len(SEEDS)} facets.")


if __name__ == "__main__":
    asyncio.run(main())
