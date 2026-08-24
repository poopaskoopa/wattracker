"""Optional LLM refinement of a planned Session.

`shape_session` refines the coaching messages and framing of a workout,
grounded strictly in the numbers already produced by the pure-formula planner.
It never invents an FTP or alters segment power/duration targets. It dispatches
on whatever `config.llm_settings()` resolves: Anthropic's Messages API for the
`anthropic` endpoint, the OpenAI-compatible chat completions API for `openai`,
`openrouter`, or a custom base URL. When nothing usable resolves, or any call
fails (missing SDK, network, API, parse), the input Session is returned
unchanged, so the app is fully functional without an LLM.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from ..config import LLM_DEFAULT_MODELS, LlmSettings, llm_settings
from .planner import Session

_log = logging.getLogger(__name__)


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


def _extract_json_block(text: str) -> Optional[str]:
    """Return the first balanced {...} block in `text`, or None.

    A model that wraps the JSON in prose still yields the plan: brace depth is
    tracked, and braces inside JSON strings do not count. The scan stops after
    4096 candidate starts so a reply full of unbalanced braces cannot make it
    O(n^2) on the request thread (4096 starts is far past any real reply).
    """
    start = text.find("{")
    for _ in range(4096):
        if start == -1:
            return None
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}" and depth:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        start = text.find("{", start + 1)
    return None


def _parse_reply(text: Optional[str]) -> Optional[dict]:
    """Parse a model reply leniently: the JSON first, else the first {...}.

    Shared by every endpoint so a server that ignores structured output (or
    wraps the JSON in prose) still yields the plan.
    """
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        block = _extract_json_block(text)
        if block is None:
            return None
        try:
            data = json.loads(block)
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


def _apply_refinements(session: Session, data: dict) -> Session:
    """Apply refinements without touching numeric targets."""
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


def _call_anthropic(
    settings: LlmSettings, session: Session, state
) -> Optional[dict]:
    """Anthropic Messages API. The default model keeps the full call shape.

    `thinking` and structured output are not guaranteed to be accepted by
    every model, so any other model gets the simpler call (no thinking, no
    output_config) and the reply is parsed leniently.
    """
    import anthropic  # lazy: a missing SDK degrades, it never crashes

    client = anthropic.Anthropic(api_key=settings.api_key)
    prompt = _build_prompt(session, state)
    if (settings.model or "").lower() == LLM_DEFAULT_MODELS["anthropic"]:
        response = client.messages.create(
            model=LLM_DEFAULT_MODELS["anthropic"],
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
            messages=[{"role": "user", "content": prompt}],
        )
    else:
        response = client.messages.create(
            model=settings.model,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
    text = next(
        (b.text for b in response.content if getattr(b, "type", None) == "text"),
        None,
    )
    return _parse_reply(text)


def _is_structured_output_rejection(exc: Exception) -> bool:
    """Whether a 400 names structured output, so one plain retry is worth it.

    Substring check on the SDK error message; any other failure (a bad model
    name, say) would just spend a second slow call to fail again.
    """
    if getattr(exc, "status_code", None) != 400:
        return False
    message = str(exc).lower()
    return any(
        marker in message for marker in ("response_format", "json_object", "structured")
    )


def _call_openai_compatible(
    settings: LlmSettings, session: Session, state
) -> Optional[dict]:
    """OpenAI-compatible chat completions: openai, openrouter, or a custom URL.

    The key is optional for custom (local) endpoints; the SDK rejects an empty
    one, so a placeholder is passed. A server that rejects structured output
    outright gets exactly one retry without response_format.
    """
    import openai  # lazy: a missing SDK degrades, it never crashes

    client_kwargs = {
        "api_key": settings.api_key or "not-needed",
        "timeout": 60.0,
        # The SDK's default two retries would turn a dead/black-holed endpoint
        # into ~3 x 60 s of blocking on the /generate request thread; the
        # degrade-to-unrefined contract wants a single bounded attempt. The
        # one conditional retry this feature wants (a 400 naming
        # response_format) is the application-level one below.
        "max_retries": 0,
    }
    if settings.base_url:
        # Omitted for openai: the SDK default is already api.openai.com/v1.
        client_kwargs["base_url"] = settings.base_url
    client = openai.OpenAI(**client_kwargs)
    create_kwargs = {
        "model": settings.model,
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": _build_prompt(session, state)}],
    }
    try:
        response = client.chat.completions.create(
            **create_kwargs, response_format={"type": "json_object"}
        )
    except Exception as exc:
        if not _is_structured_output_rejection(exc):
            raise
        response = client.chat.completions.create(**create_kwargs)
    text = response.choices[0].message.content
    return _parse_reply(text)


def shape_session(session: Session, state) -> Session:
    """Refine the Session with the configured LLM if one resolves; else
    return it unchanged."""
    try:
        settings = llm_settings()
        if settings is None:
            return session
        if settings.endpoint == "anthropic":
            data = _call_anthropic(settings, session, state)
        else:
            data = _call_openai_compatible(settings, session, state)
    except Exception:
        # Any failure (bad stored config, missing SDK, network, parse, API)
        # falls back to the pure-formula plan; log it, because a config that
        # resolves fine but fails per-call (a typo'd port, a 401, a timeout)
        # would otherwise look identical to "refinement is off".
        _log.warning(
            "LLM refinement failed; returning the unrefined session",
            exc_info=True,
        )
        return session
    if not data:
        return session
    return _apply_refinements(session, data)
