"""
Embedding layer. ONE model per axis, fixed for the lifetime of the store
(swapping mid-flight makes old vectors incomparable — hard rule from the spec).

Two backends behind one interface:
  - "bge_m3":   real BAAI/bge-m3 dense vectors via FlagEmbedding (needs torch).
  - "fallback": deterministic hashed bag-of-tokens, zero heavy deps. Stable
                cosine similarity (shared tokens -> closer) so the whole
                pipeline runs end-to-end on a laptop / in CI.

CRITICAL: callers must pass PURE CONTENT here. Never concatenate tags, timestamps,
speaker, or emotion labels into the text being embedded — that smears the vector
and wrecks cosine separation. Metadata stays in side-car columns.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import re
from functools import lru_cache

from app.config import settings

DIM = settings.embedding_dim


# ── Backend: fallback (deterministic hash embedder) ──────────────────
_TOKEN_RE = re.compile(r"[\w一-鿿]+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    # words for latin scripts; per-character for CJK (cheap n-gram-ish signal)
    toks: list[str] = []
    for m in _TOKEN_RE.findall(text.lower()):
        if m.isascii():
            toks.append(m)
        else:
            toks.extend(m)            # split CJK run into characters
            toks.extend(m[i:i + 2] for i in range(len(m) - 1))  # + bigrams
    return toks


def _hash_embed(text: str) -> list[float]:
    vec = [0.0] * DIM
    for tok in _tokenize(text):
        h = hashlib.md5(tok.encode("utf-8")).digest()
        idx = int.from_bytes(h[:4], "little") % DIM
        sign = 1.0 if h[4] & 1 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


# ── Backend: api (bge-m3 over an OpenAI-compatible endpoint, e.g. SiliconFlow) ─
@lru_cache
def _api_client():
    from openai import AsyncOpenAI

    return AsyncOpenAI(
        api_key=settings.embedding_api_key or "missing-key",
        base_url=settings.embedding_base_url,
    )


async def _api_embed_batch(texts: list[str], chunk: int = 32) -> list[list[float]]:
    client = _api_client()
    out: list[list[float]] = []
    for i in range(0, len(texts), chunk):  # respect provider batch limits
        resp = await client.embeddings.create(
            model=settings.embedding_model, input=texts[i : i + chunk]
        )
        out.extend(d.embedding for d in resp.data)
    return out


# ── Backend: bge-m3 (local, lazy, threadpool-offloaded) ──────────────
@lru_cache
def _bge_model():
    from FlagEmbedding import BGEM3FlagModel  # imported only when used

    return BGEM3FlagModel(settings.embedding_model, use_fp16=True)


def _bge_embed_batch(texts: list[str]) -> list[list[float]]:
    model = _bge_model()
    out = model.encode(texts, batch_size=16, max_length=2048)["dense_vecs"]
    return [v.tolist() for v in out]


# ── Public async interface ───────────────────────────────────────────
async def embed(texts: list[str]) -> list[list[float]]:
    """Embed a batch of PURE-CONTENT strings -> list of dim-D vectors."""
    if not texts:
        return []
    backend = settings.embedding_backend
    if backend == "api":
        return await _api_embed_batch(texts)
    if backend == "bge_m3":
        return await asyncio.to_thread(_bge_embed_batch, texts)
    return [_hash_embed(t) for t in texts]


async def embed_one(text: str) -> list[float]:
    return (await embed([text]))[0]
