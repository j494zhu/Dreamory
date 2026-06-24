"""
Affect engine — emotion simulation via an Emotion-Bids + Attachment-Theory
state machine. A hidden parameter set (the "brain theatre") is maintained in
code and compiled into the prompt as natural-language behavioural directives,
so the LLM stops behaving like a perpetually full-battery, fixed-length responder.

  state.py     — the data (mode, open_loops/grievances, scalar drives)
  persona.py   — per-person hyper-parameters that modulate the dynamics
  dynamics.py  — pure, LLM-free coupling rules + mode transitions (unit-testable)
  extractor.py — LLM① : classify the latest message into discrete events
  injector.py  — deterministic state -> natural-language prompt compilation
"""
from . import dynamics, extractor, injector, persona, state  # noqa: F401
