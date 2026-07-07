# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1] - 2026-07-03

### Fixed

- Injector rendered `open_loop.sessions_old` (a session count) as a number of
  days — now labelled as sessions (`隔了 N 次聊天还没下文`).
- Same-message double patience penalty: a `turn_away`/`turn_against` turn that
  also carried a `perfunctory`/`dismissive` tone was charged twice. The tone
  penalty is now deduplicated against the heavier event.
- Conflict "哄不动" deadlock: with low `security`, repair attempts were gated out
  forever. Added a pity floor — after `REPAIR_PITY_ATTEMPTS` sincere attempts the
  repair is accepted (with a reduced security gain); counter resets on acceptance
  or a fresh attack.
- Syntax error in `pipeline._salience_from_events` (stray char after a line
  continuation) that prevented the conversation pipeline from importing.

### Changed

- `_salience_from_events` now takes the strongest single emotional event
  (`max`) instead of summing them, removing the `turn_against` + `repair`
  stacking, and compares arousal against a named `DAILY_AROUSAL_BASELINE`.
- Persona 口癖 (`style`) is no longer pinned into the frozen core identity every
  turn; it is injected at low frequency (`STYLE_INJECT_PROB`, warm/neutral only).

### Added

- New personas: `tsundere`, `cool`, `playful`, `clingy`, each with its own style.
- `AffectState.repair_attempts` field (tracks consecutive repair attempts).
- Auto-dream: the pipeline fires `should_dream()` after each turn and runs a
  Dream cycle in the background (non-blocking, single-flight) when the pending
  backlog is high enough. `DREAM_ENABLED` now defaults to `true`.

## [0.1.0] - 2026-06-24

### Added

- Emotion state machine (affect engine) with attachment theory dynamics
- Three-tier hierarchical memory (L1 / L2 / L3 + orthogonal tag registry)
- Conversation pipeline with time effects, event extraction, and personality injection
- FastAPI backend with native HTML/CSS/JS frontend
- PostgreSQL + pgvector storage with HNSW/GIN indexes
- Dual-axis retrieval (content + emotion vectors)
- Dream offline maintenance phase (clustering + LLM naming)
- DeepSeek v4 pro/flash integration with OpenAI-compatible API
- bge-m3 embedding via SiliconFlow API (with local and fallback backends)
- 14 unit tests covering emotion dynamics + memory pure functions

[Unreleased]: https://github.com/{user}/{repo}/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/{user}/{repo}/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/{user}/{repo}/releases/tag/v0.1.0
