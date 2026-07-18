"""Optional LLM refinement of a planned Session via the Anthropic SDK.

`shape_session` refines the coaching messages and framing of a workout,
grounded strictly in the numbers already produced by the pure-formula planner.
It never invents an FTP or alters segment power/duration targets. If no API key
is configured, the input Session is returned unchanged so the app is fully
functional without a key.
"""
from __future__ import annotations

import json
from typing import Optional

from ..config import load_config
from .planner import Session

MODEL = "claude-sonnet-5"


def _build_prompt(session: Session, state) -> str:
    payload = {
        "training_state": state.to_dict() if hasattr(state, "to_dict") else {},
        "workout": session.to_dict(),
    }
    return (
        "You are a cycling coach refining a workout that has ALREADY been "
        "prescribed by a training-science engine. Do NOT change any power "
        "targets, durations, or the FTP - those are fixed and correct. You may "
        "only improve the workout name, description, and the coaching text for "
        "each segment so they motivate the athlete and explain the purpose, "
        "grounded strictly in the numbers provided. Do not invent an FTP.\n\n"
        "Return ONLY JSON matching this shape: {\"name\": str, "
        "\"description\": str, \"segment_texts\": [str, ...]} where "
        "segment_texts has exactly one entry per segment in order.\n\n"
        "Here is the current workout and training state as JSON:\n"
        + json.dumps(payload, indent=2)
    )


def shape_session(session: Session, state) -> Session:
    """Refine the Session with the LLM if a key is present; else return unchanged."""
    cfg = load_config()
    api_key: Optional[str] = cfg.anthropic_api_key
    if not api_key:
        return session

    try:
        import anthropic
    except ImportError:
        return session

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            thinking={"type": "adaptive"},
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "segment_texts": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["name", "description", "segment_texts"],
                        "additionalProperties": False,
                    },
                }
            },
            messages=[{"role": "user", "content": _build_prompt(session, state)}],
        )
        text = next(
            (b.text for b in response.content if getattr(b, "type", None) == "text"),
            None,
        )
        if not text:
            return session
        data = json.loads(text)
    except Exception:
        # Any failure (network, parse, API) falls back to the pure-formula plan.
        return session

    # Apply refinements without touching numeric targets.
    if isinstance(data.get("name"), str) and data["name"].strip():
        session.name = data["name"].strip()
    if isinstance(data.get("description"), str) and data["description"].strip():
        session.description = data["description"].strip()
    texts = data.get("segment_texts")
    if isinstance(texts, list):
        for seg, txt in zip(session.segments, texts):
            if isinstance(txt, str) and txt.strip():
                seg.text = txt.strip()
    return session
