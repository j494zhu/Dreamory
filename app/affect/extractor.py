"""
事件抽取器:LLM 调用①(走 DeepSeek flash,便宜、确定性)。
只做离散分类,绝不输出数值 delta —— 数值变化全部由 dynamics.py 的规则表决定。

输入:她的上一条消息 + 他的新消息 + 当前 open_loops
输出:严格 JSON 的事件分类
"""
from __future__ import annotations

from app.llm import client
from app.llm.client import MODEL_FLASH

# 输出 schema(写进 prompt,同时用代码校验)
_SCHEMA_DOC = """{
  "bid_in_her_last_msg": "none | venting | sharing | seeking_comfort | asking | testing",
  "his_response_type": "turn_toward | turn_away | turn_against | not_applicable",
  "addresses_loop_id": "<open_loop的id> 或 null",
  "is_repair_attempt": true/false,
  "new_commitment": "他这条消息里做出的具体承诺原文,没有则 null",
  "new_bid_from_him": true/false,
  "tone_flags": ["从这个列表多选: perfunctory, warm, defensive, dismissive, affectionate, apologetic, excited, demanding"],
  "topic_relates_to_grievance_id": "<grievance的id> 或 null"
}"""

_VALID_BIDS = {"none", "venting", "sharing", "seeking_comfort", "asking", "testing"}  # 好像是可以在请求参数里指定json的某个字段必须从一个列表中选取的. 
_VALID_RESP = {"turn_toward", "turn_away", "turn_against", "not_applicable"}
_VALID_TONES = {"perfunctory", "warm", "defensive", "dismissive",
                "affectionate", "apologetic", "excited", "demanding"}
 

def _build_messages(her_last_msg: str | None, his_msg: str,
                    recent_context: str, state) -> list[dict]:
    loops_desc = "\n".join(
        f'- id={l.id} [{l.type}, 重要度{l.weight}] {l.content}'
        for l in state.open_loops
    ) or "(无)"
    grievances_desc = "\n".join(
        f'- id={g.id} {g.content}' for g in state.grievances if not g.resolved
    ) or "(无)"

    system = (
        "你是对话事件分类器。场景:一对情侣在线上聊天,你站在女方视角分析男方刚发来的消息。\n\n"
        "概念定义:\n"
        "- bid(情感投标):她上一条消息里隐含的'接住我'的期待。抱怨=venting,分享喜悦=sharing,"
        "求安慰=seeking_comfort,提问=asking,表面平常实则试探=testing。\n"
        "- turn_toward:他回应了投标(追问、共情、给出实质回应)。\n"
        "- turn_away:他忽略/敷衍了投标(转移话题、'哦''嗯'式回复、只顾说自己的)。\n"
        "- turn_against:他攻击性回应(不耐烦、指责、嘲讽)。\n"
        "- repair_attempt:冲突或冷淡氛围中他主动缓和(道歉、自嘲、示好、认错)。\n\n"
        "判断他的消息是否回应了某个挂起回路(open_loop),是否触碰了某个旧账(grievance)。\n\n"
        f"当前挂起回路:\n{loops_desc}\n\n"
        f"当前旧账:\n{grievances_desc}\n\n"
        f"严格只输出符合此 schema 的 JSON,不要任何其他内容:\n{_SCHEMA_DOC}"
    )
    user = (
        f"【最近几轮对话(供理解上下文)】\n{recent_context}\n\n"
        f"【她的上一条消息】\n{her_last_msg or '(这是对话开头,她还没说过话)'}\n\n"
        f"【他刚发来的消息】\n{his_msg}\n\n"
        "输出分类 JSON。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _validate(data: dict, state) -> dict: 
    """代码侧校验 + 清洗,LLM 输出不可信。"""
    out = {}
    out["bid_in_her_last_msg"] = data.get("bid_in_her_last_msg") \
        if data.get("bid_in_her_last_msg") in _VALID_BIDS else "none"
    out["his_response_type"] = data.get("his_response_type") \
        if data.get("his_response_type") in _VALID_RESP else "not_applicable"

    loop_id = data.get("addresses_loop_id")
    out["addresses_loop_id"] = loop_id if loop_id and state.find_loop(loop_id) else None

    out["is_repair_attempt"] = bool(data.get("is_repair_attempt"))
    out["new_bid_from_him"] = bool(data.get("new_bid_from_him"))

    nc = data.get("new_commitment")
    out["new_commitment"] = nc if isinstance(nc, str) and nc.strip() else None

    tones = data.get("tone_flags") or []
    out["tone_flags"] = [t for t in tones if t in _VALID_TONES]

    gid = data.get("topic_relates_to_grievance_id")
    valid_gids = {g.id for g in state.grievances if not g.resolved}
    out["topic_relates_to_grievance_id"] = gid if gid in valid_gids else None
    return out


_NEUTRAL = {
    "bid_in_her_last_msg": "none", "his_response_type": "not_applicable",
    "addresses_loop_id": None, "is_repair_attempt": False, "new_bid_from_him": False,
    "new_commitment": None, "tone_flags": [], "topic_relates_to_grievance_id": None,
}


async def extract(her_last_msg: str | None, his_msg: str,
                  recent_context: str, state, retries: int = 1) -> dict:
    messages = _build_messages(her_last_msg, his_msg, recent_context, state)
    data = await client.chat_json(
        messages, model=MODEL_FLASH, temperature=0.0,
        retries=retries, default=dict(_NEUTRAL),
    )
    if not data:
        return dict(_NEUTRAL)  # 失败降级:按中性事件处理,不让管线崩
    return _validate(data, state) 
