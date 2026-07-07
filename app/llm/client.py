"""
DeepSeek client wrapper. DeepSeek speaks the OpenAI protocol, so we drive it
through the async `openai` SDK pointed at the DeepSeek base URL.

Two model tiers (per spec):
  PRO   (deepseek v4 pro)   — generation (LLM②, the "brain theatre") + Dream naming
  FLASH (deepseek v4 flash) — event extraction (LLM①), cheap & deterministic
"""
from __future__ import annotations

import json
from functools import lru_cache

from openai import AsyncOpenAI

from app.config import settings

MODEL_PRO = settings.deepseek_model_pro
MODEL_FLASH = settings.deepseek_model_flash


@lru_cache
def get_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=settings.deepseek_api_key or "missing-key",
        base_url=settings.deepseek_base_url,
    )


def _thinking_body(thinking: bool | None, effort: str | None) -> dict:
    """DeepSeek v4 thinking controls (passed via extra_body):
       {"thinking": {"type": "enabled"|"disabled"}}, {"reasoning_effort": "high"|"max"}.
    `thinking=None` leaves the provider default untouched."""
    body: dict = {}
    if thinking is not None:
        body["thinking"] = {"type": "enabled" if thinking else "disabled"}
    if thinking and effort:
        body["reasoning_effort"] = effort
    return body


async def chat(
    messages: list[dict],
    *,
    model: str | None = None,
    temperature: float = 0.7,
    json_mode: bool = False,
    max_tokens: int | None = None,
    thinking: bool | None = None,
    reasoning_effort: str | None = None,
) -> str:
    """Single completion. Returns the assistant text (or "" on failure).

    Note: deepseek-v4-pro reasons by default; pass thinking=False for tasks where
    the chain-of-thought is unwanted (e.g. our in-band roleplay <thinking> block,
    deterministic JSON extraction, short summaries) to save tokens/latency."""
    client = get_client()
    kwargs: dict = {
        "model": model or MODEL_PRO,
        "messages": messages,
        "temperature": temperature,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    body = _thinking_body(thinking, reasoning_effort)
    if body:
        kwargs["extra_body"] = body

    resp = await client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""


async def chat_tools(
    messages: list[dict],
    *,
    tools: list[dict],
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    thinking: bool | None = None,
    tool_choice: str = "auto",
):
    """One completion round with function-calling enabled.

    Returns the raw assistant message object (`.content`, `.tool_calls`) so the
    caller can run a bounded agent loop: append the message, execute the tool
    calls, append `role=tool` results, call again. `tool_choice="none"` forces
    a final text answer (used when the loop hits its round cap)."""
    client = get_client()
    kwargs: dict = {
        "model": model or MODEL_PRO,
        "messages": messages,
        "temperature": temperature,
        "tools": tools,
        "tool_choice": tool_choice,
    }
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    body = _thinking_body(thinking, None)
    if body:
        kwargs["extra_body"] = body

    resp = await client.chat.completions.create(**kwargs)
    return resp.choices[0].message


def tool_message_to_dict(msg) -> dict:
    """Assistant message with tool_calls → plain dict for the next request."""
    out: dict = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in msg.tool_calls
        ]
    return out


async def chat_json(
    messages: list[dict],
    *,
    model: str | None = None,
    temperature: float = 0.0,
    retries: int = 1,
    default: dict | None = None,
    thinking: bool | None = False,
) -> dict:
    """Completion constrained to JSON, parsed and validated as a dict.
    Degrades to `default` (or {}) instead of raising — keeps hot paths alive.
    Defaults thinking=False: extraction is deterministic classification."""
    for attempt in range(retries + 1):
        try:
            raw = await chat(
                messages, model=model, temperature=temperature, json_mode=True,
                thinking=thinking,
            )
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception as e:  # noqa: BLE001
            if attempt >= retries:
                print(f"[llm.chat_json] failed after {retries} retries: {e}")
    return dict(default) if default else {}
