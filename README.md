# Dreamory

A backend for a long-running companion chatbot. The problem it tries to solve: a stateless LLM behind a chat box has no memory beyond the context window and no internal state, so it is equally cheerful in every message and forgets everything after a few thousand tokens. Dreamory keeps both of those things *outside* the model, in code and in Postgres, and compiles them into the prompt every turn.

Two pieces do most of the work:

- **An emotion state machine** (`app/affect/`). A small set of scalars (arousal, security, patience, affection, three "hormone" decays on different time scales), a six-mode state machine with hysteresis, and a rule table that updates them from discrete events. The LLM only classifies what the user said; it never picks the numbers.
- **A three-tier memory** (`app/memory/`). Every message is stored once in Postgres with two pgvector embeddings (content axis and emotion axis). A time-decayed hot cache holds ids only, and a token-budgeted assembler decides what goes into the context window each turn.

Around those: a bounded tool-calling loop so the model can search its own memory, a regex-based guardrail that catches persona breaks and regenerates once, a scheduler that lets the character send messages on its own, and a per-turn audit log that records every classification, rule firing, retrieval hit and tool call so misjudgements can be reviewed after the fact.

Stack: **FastAPI** (async), **PostgreSQL 16 + pgvector**, **SQLAlchemy 2 async / asyncpg**, DeepSeek through the OpenAI-compatible SDK, **bge-m3** embeddings (via API, local, or a deterministic hash fallback for tests). Frontend is a single-page vanilla JS client. Chinese-language persona; the code comments are largely in Chinese.

**Status.** This repository is v0.6.2, which was shared with a small group of testers. The architecture held up technically, but the product direction had a design flaw: the emotional dynamics were tuned so that the *user* ended up doing the emotional work rather than receiving it, which made the experience tiring instead of pleasant. A v1 rewrite that keeps the memory and infrastructure ideas but restructures the affect layer is in progress and not yet published. This README describes what is here.

## How a turn works

`app/conversation/pipeline.py::handle_message` runs the following steps for every user message:

1. **Time effects** – decay arousal and hormones, reset per-session patience if the gap is over 6 h, age unresolved "open loops" so they can settle into grievances.
2. **Event extraction (LLM call 1)** – a JSON-mode call classifies the message: what kind of bid it is (venting, sharing, seeking comfort, testing…), how it responds to her last bid (turn toward / away / against), apology, commitment, persona attack. Output is schema-validated in code; on failure it degrades to a neutral event so the pipeline never stalls.
3. **Dynamics (no LLM)** – `affect/dynamics.py` applies the rule table: scalar updates, open-loop bookkeeping, mode transition with hysteresis. All constants live at the top of the file so they can be swept.
4. **Persist + tag** – the user's message is written to `memories` with both embeddings and tagged by kNN label propagation against a controlled vocabulary (no LLM on this path).
5. **Assemble L1** – `memory/l1_assembly.py` fills the context window's memory region from three slots (cherished, working-memory FIFO, retrieved) under a token budget, deduplicating by id across slots. `affect/injector.py` renders the persona, relationship stage, current affect and tool instructions.
6. **Generate (LLM call 2)** – `_generate_with_tools` runs at most `TOOL_MAX_ROUNDS` tool rounds (`search_memory`, `grep_memory`, `set_timer`, `write_note`), then forces a final answer with `tool_choice="none"`. If anything in the tool path fails, it falls back to a plain completion. Output uses `<thinking>` / `<reply>` tags; the parser handles unclosed and orphaned tags and strips anything that would otherwise leak to the user.
7. **Guardrail** – `conversation/guardrail.py` scans the visible replies with a conservative regex set (first-person "I am an AI", assistant-style refusals, system-prompt mentions, markdown fences) and checks for inner-monologue leakage. On a hit it regenerates once with a hidden corrective note. If the retry also fails it sends the original anyway; the user never sees a "content blocked" message.
8. **Persist replies, schedule timers, snapshot** – replies go to L3; a timer ping is queued if she said she would come back later; the affect state, an `affect_snapshots` row and a `turn_logs` row are written.

Background work (auto-dream, life simulator, night agent, persona evolution) is kicked off after the turn commits and runs under Postgres advisory locks (`app/db_locks.py`) so multiple workers do not run the same job twice. Timer pings are claimed with `SELECT … FOR UPDATE SKIP LOCKED` for the same reason.

## Memory design

| Tier | What it holds | Where |
|---|---|---|
| L3 (source of truth) | every message and derived passage, once; `content_vec` and `emotion_vec` (1024-d, HNSW indexes); tags as a GIN-indexed array | `memory/l3_store.py`, `models.py` |
| L2 (hot cache) | ids only, fixed capacity, ordered by time-decayed heat; hits are counted in memory and flushed in batches | `memory/l2_hot.py` |
| L1 (context) | the assembled prompt region for this turn | `memory/l1_assembly.py` |

Retrieval (`memory/retrieval.py`) searches either axis or both, applies a hard relevance floor on the raw cosine score (kNN always returns *k* rows, so without a floor irrelevant noise would enter the prompt and accumulate heat), uses tags as a `WHERE` filter, and can bias by the chat's current goal. When the best automatic hit is below a confidence threshold, the prompt tells the model its memory is hazy and it may search explicitly.

Tags are assigned on the hot path by kNN vote plus centroid similarity; the vocabulary itself is only changed by the offline **Dream** job (`memory/dream.py`: cluster → LLM names the cluster → merge/split → remap), which is built and callable but disabled in the example configuration.

## Reliability and observability

- **Every LLM output is validated in code.** Extraction is schema-checked; generation is tag-parsed with fallbacks; night-agent payloads are validated before being applied.
- **Tools are an enhancement, not a dependency.** Tool-loop failure, guardrail failure and extractor failure each degrade to a defined behaviour rather than an error.
- **`turn_logs`** (`conversation/turnlog.py`) records, per turn: the full extraction result with a confidence flag, which dynamics rules fired, the list of injected prompt blocks, retrieval hits with scores, tool calls, and guardrail interventions. Message text is stored by id only. `GET /api/chats/{id}/turns?flagged=true` and a one-click "this felt wrong" button in the UI make it possible to audit misclassifications from real conversations.
- **`affect_snapshots`** gives a per-turn time series of the hidden state; the frontend plots it. `memory/health.py` computes six drift/redundancy/oscillation metrics over a chat's memory.
- **Accelerated ageing** (`scripts/simulate.py`). All time reads go through `app/clock.py`, which can be offset. A JSON scenario DSL (say / advance hours / run the night agent / let an LLM play the user for *n* turns) drives the *real* pipeline against a real database, so a week of interaction runs in minutes and produces a CSV of the state trajectory plus a health report. Four reference scenarios are included (a warm week, a week of neglect, and two commitment-tracking cases).
- **Test-period access control** (`app/auth.py`): with `ADMIN_TOKEN` set, each chat gets its own access token and testers receive an isolated link; global endpoints require the admin token. A rolling 24 h per-chat message cap returns 429.

## Tests

183 tests, `pytest -q`, about one second. They cover the pure-function parts — dynamics rules, affection curve, hormone half-lives, tag parsing, generation parsing, L1 budgeting and dedup, guardrail patterns, schedule matching, night-agent payload validation, health metrics, clock and commitment semantics — and use the hash-embedding backend so no database, API key or model download is needed.

## Running

```bash
docker compose up -d                 # Postgres 16 with pgvector
cp .env.example .env                 # set DEEPSEEK_API_KEY; EMBEDDING_BACKEND=api needs a SiliconFlow key,
                                     # or use EMBEDDING_BACKEND=fallback to run without one
pip install -r requirements.txt
python -m scripts.init_db            # extension, tables, HNSW/GIN indexes
python -m scripts.seed_tags
uvicorn app.main:app --reload --port 8000
```

`GET /healthz` reports backend, embedding and dream status. The chat router exposes 19 endpoints under `/api/chats` (create/list/get/patch/delete, messages, an SSE stream for proactive messages, timers, schedule, life events, notes, affect history, health, turn logs, config revisions with rollback) and the memory router 4 (retrieval introspection, tags, seed, dream run).

## Layout

```
app/
  affect/        state, dynamics (rule table), extractor (LLM 1), injector (prompt), persona, narrative
  memory/        l3_store, l2_hot, l1_assembly, retrieval, tags, dream, health
  conversation/  pipeline, tools, guardrail, timer, schedule, life_sim, night_agent,
                 notebook, evolution, timeline, turnlog, config_store, bus (SSE)
  llm/           DeepSeek client wrapper, embedding backends
  routers/       chat, memory
  auth.py  clock.py  db_locks.py  config.py  models.py  schemas.py  main.py
scripts/         init_db, seed_tags, simulate (+ scenarios/)
static/          single-page client
tests/           19 files
```

## Known limitations

- No user accounts; access control is per-chat tokens intended for a closed test.
- No public deployment; `docker-compose.yml` only provides the database.
- No retry/backoff around LLM calls beyond the extractor's single retry.
- The frontend is a debugging surface, not a product UI.
- Persona and prompts are Chinese-only.
