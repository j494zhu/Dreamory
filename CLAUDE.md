# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Dreamory — emotion-aware companion LLM app (FastAPI + vanilla HTML/CSS/JS in `static/`, DeepSeek v4, bge-m3 embeddings, PostgreSQL + pgvector). Comments, docs, prompts, and UI are mostly Chinese. `description.txt` is the as-built spec (kept in sync with each release); `CHANGELOG.md` follows Keep a Changelog + SemVer.

## Commands

```bash
pip install -r requirements.txt
docker compose up -d              # Postgres 16 + pgvector (user/pass/db: dreamory)
cp .env.example .env              # fill DEEPSEEK_API_KEY; other values have sane defaults
python -m scripts.init_db         # enable pgvector + create tables + HNSW/GIN indexes
python -m scripts.seed_tags       # seed the controlled tag vocabulary
uvicorn app.main:app --reload --port 8000
```

Tests (fully offline — no DB, no LLM, no API keys; pure-function tests run in <1s):

```bash
pytest -q                                     # all tests
pytest tests/test_dynamics.py -q              # one file
pytest tests/test_commitment.py::test_name    # one test
```

`pytest.ini` sets `asyncio_mode = auto` — async tests need no decorator.

Accelerated-aging simulation (requires Postgres up + real `DEEPSEEK_API_KEY`; runs the real pipeline with a fast-forwarded clock, writes CSV/health report to `sim_out/`):

```bash
python -m scripts.simulate --scenario scripts/scenarios/neglect_week.json --preset anxious --no-bg
```

`--no-bg` disables background LLM calls (life sim / Dream) to control cost. Scenario files are a JSON step DSL (`say` / `advance_hours` / `night` / `auto` / `repeat` / `tick_timers`).

## Iron laws (every module must comply)

- Content is stored **once**, in L3 `memories.content`. Every other layer (L2, L1, tags) holds **ids only**. Derived caches (L2, summaries) are disposable and rebuildable — never a source of truth.
- Text that goes into vectors is **pure content** (VectorDB_1) or **pure emotion+reasoning** (VectorDB_2). Tags / timestamps / speaker are side-car columns used as `WHERE` filters at retrieval, never concatenated into embedded text.
- **Hot path is zero-LLM**: tagging is deterministic kNN/centroid voting. The LLM only names already-formed clusters, in offline Dream (`DREAM_ENABLED=false` by default).
- **The LLM never decides numbers.** The extractor (flash) outputs discrete classifications only; `app/affect/dynamics.py` (pure code, heavily unit-tested) turns them into numeric state changes.
- **All time goes through `app/clock.py`** (injectable offset). Never call `time.time()` / `datetime.now()` directly — the simulation harness depends on this.
- Primary keys are UUIDv7; time queries use dedicated indexed `ts_ms` columns.

## Architecture

Two pillars: an **affect state machine** (`app/affect/`) whose hidden numeric state is compiled deterministically into the prompt, and a **MemGPT-style 3-tier memory** (`app/memory/`) with an orthogonal tag registry.

Per-turn orchestration lives in `app/conversation/pipeline.py::handle_message` — extraction and dynamics run **before** generation so her reply reflects how his current message moved her state:

1. time effects (arousal cooldown / session boundary / offline affection decay)
2. event extraction — LLM① flash, strict JSON, classification only
3. dynamics + mode transition — pure code
4. persist his message to L3 + zero-LLM tagging
5. assemble L1 (cherished + L2 hot + L3 retrieval, deduped + token-budgeted; plus time-sense, schedule, topic seeds, injector state blocks)
6. generation — LLM② pro, bounded agent loop (`search_memory` / `grep_memory` / `set_timer` / `write_note`, `TOOL_MAX_ROUNDS` then forced answer); output is `<thinking>` + 1–4 `<reply>` bursts
7. persist her replies to L3 + tagging; 8. save affect state (+ timer pings); 9. background maintenance (auto-dream, life sim)

A second entrypoint, `handle_timer_fire`, produces proactive messages (timer/commitment pings): no extraction/dynamics, result pushed to the frontend over SSE (`GET /api/chats/{id}/events`, in-process `conversation/bus.py`).

Memory tiers: `l3_store.py` (L3 cold store, sole truth, dual vectors) · `tags.py` (controlled vocab + centroids, kNN tagging) · `retrieval.py` (dual-axis recall + time filters) · `l2_hot.py` (id-only hot zone, time-decayed heat, background flush) · `l1_assembly.py` (dedup + elastic token budget) · `dream.py` (offline cluster/merge/rename) · `health.py` (six read-only drift/entropy metrics).

Affect: `state.py` (`AffectState`: 6 discrete modes with hysteresis, open_loops/grievances as first-class structures, arousal/security/patience scalars, affection 0–200 in 8 tiers, 3 hormone axes with distinct half-lives) · `persona.py` (fixed per-persona hyperparameters, 8 presets — one engine, different parameterizations) · `extractor.py` (LLM①) · `injector.py` (state → prompt blocks) · `narrative.py` (confabulation: her stated reason vs. real cause, zero-LLM).

Other subsystems in `app/conversation/`: timer scheduling (`FOR UPDATE SKIP LOCKED` claiming), guardrail (persona-break detection + one hidden regeneration), night agent (distill day → passage/diary/tomorrow's plan), notebook, persona evolution (append-only, snapshot-before-apply via `config_store.py`), life sim ("generated once = canon", written to L3), timeline snapshots. `app/db_locks.py` provides Postgres advisory locks so background singletons are multi-worker safe (the SSE bus is still per-process — acceptable because messages live in L3).

LLM access is centralized in `app/llm/client.py`: `MODEL_PRO` (generation / night reflection / Dream naming), `MODEL_FLASH` (extraction), OpenAI-compatible.

Embeddings (`app/llm/embeddings.py`) have three backends via `EMBEDDING_BACKEND`: `api` (bge-m3 over SiliconFlow HTTP, default, no torch), `bge_m3` (local FlagEmbedding), `fallback` (deterministic hash embedder — what the tests use).

Most subsystems are gated by `.env` feature flags (`TOOLS_ENABLED`, `TIMER_ENABLED`, `GUARDRAIL_ENABLED`, `NIGHT_AGENT_ENABLED`, `LIFE_SIM_ENABLED`, `COMMITMENT_PING_ENABLED`, …) and degrade gracefully when off — see `.env.example` for the full annotated list.

## Hard-won conventions (violating these regresses live behavior)

- `deepseek-v4-pro` has **native reasoning ON by default**. The "脑内剧场" `<thinking>` block is in-band roleplay, *not* model reasoning, so generation/extraction/summary all pass `thinking=False` (`{"thinking": {"type": "disabled"}}`). Keep it that way unless deliberately enabling reasoning.
- **Injector prompt-block structure is load-bearing.** e.g. the `set_timer` teaching must be its own salient block with stated consequences — demoting it to bullet points dropped timer compliance from ~8/9 to 1/3 in live tests. `tests/test_injector_blocks.py` pins the structure; update tests deliberately, not incidentally.
- Generation output parsing must **never leak bare `<reply>`/`<thinking>` tags** to the user — the model often emits unclosed tags after tool rounds; `_parse_generation` in pipeline.py is deliberately tolerant.
- Persona/core-identity changes are **append-only with snapshots** (`chat_revisions`); names never change; rollback must itself leave a snapshot.
- Proactive (timer-fired) generations must not schedule new timers (prevents self-trigger chains); commitment pings check the loop is still open before firing (never nag after he delivered).
