"""
Three-tier memory:
  l3_store    — L3 cold storage: relational main table + pgvector content/emotion axes.
  tags        — orthogonal Tag registry + zero-LLM kNN tagging on the hot path.
  retrieval   — dual-axis (content / emotion) semantic recall + tag WHERE filtering.
  l2_hot      — Hot Zone: in-memory time-decayed heat counter + batch write-back.
  l1_assembly — L1 context assembly: slot filling, global dedup, token budgeting.
  dream       — offline maintenance (clustering / naming / merge / split / remap).
"""
