"""Tests for the LLM refinement: fallback + per-endpoint call paths."""
import json
import sys
import types

from wattracker.analysis.state import TrainingState
from wattracker.prescribe.planner import plan_workout
from wattracker.prescribe import llm


def _make_session():
    state = TrainingState(ftp=250.0, tsb=0.0)
    return state, plan_workout(state, 60)


def _reply_payload(session) -> str:
    return json.dumps(
        {
            "name": "Refined Name",
            "description": "a refined description",
            "segment_texts": [f"refined {i}" for i in range(len(session.segments))],
        }
    )


# ------------------------------------------------- fake openai SDK
class _BadRequest(Exception):
    def __init__(self, message):
        super().__init__(f"Error code: 400 - {message}")
        self.status_code = 400


class _Completion:
    def __init__(self, content):
        message = types.SimpleNamespace(content=content)
        self.choices = [types.SimpleNamespace(message=message)]


class _Completions:
    """Replays a script: each entry is a _Completion to return, or an
    Exception instance to raise, one per create() call."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._script:
            raise AssertionError("more create() calls than scripted")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _install_fake_openai(monkeypatch, script, client_record):
    """Stand in for the `import openai` inside llm._call_openai_compatible.

    `client_record` receives the kwargs openai.OpenAI(...) was constructed
    with; the returned _Completions records every create() call.
    """
    completions = _Completions(script)

    def _OpenAI(**kwargs):
        client_record.update(kwargs)
        return types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=completions)
        )

    module = types.ModuleType("openai")
    module.OpenAI = _OpenAI
    monkeypatch.setitem(sys.modules, "openai", module)
    return completions


# ------------------------------------------------------------ fallback
def test_shape_session_no_key_returns_unchanged(monkeypatch):
    # Ensure no API key is present.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    state = TrainingState(ftp=250.0, tsb=0.0)
    session = plan_workout(state, 60)

    before = session.to_dict()
    result = llm.shape_session(session, state)

    assert result is session
    assert result.to_dict() == before


# ------------------------------------------- openai-compatible path
def test_openai_happy_path(monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("LLM_ENDPOINT", "openai")
    state, session = _make_session()
    client_record = {}
    completions = _install_fake_openai(
        monkeypatch, [_Completion(_reply_payload(session))], client_record
    )

    result = llm.shape_session(session, state)

    assert result is session
    assert session.name == "Refined Name"
    assert session.description == "a refined description"
    assert session.segments[0].text == "refined 0"
    # openai: SDK-default URL (no base_url), the real key, 60 s timeout,
    # no SDK-level retries (one bounded attempt on the request thread).
    assert client_record == {
        "api_key": "k", "timeout": 60.0, "max_retries": 0
    }
    call = completions.calls[0]
    assert call["model"] == "gpt-5.6-luna"
    assert call["max_tokens"] == 2000
    assert call["response_format"] == {"type": "json_object"}
    assert len(completions.calls) == 1


def test_openrouter_uses_its_base_url(monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("LLM_ENDPOINT", "openrouter")
    monkeypatch.setenv("LLM_MODEL", "org/model")
    state, session = _make_session()
    client_record = {}
    completions = _install_fake_openai(
        monkeypatch, [_Completion(_reply_payload(session))], client_record
    )

    llm.shape_session(session, state)

    assert client_record["base_url"] == "https://openrouter.ai/api/v1"
    assert completions.calls[0]["model"] == "org/model"


def test_openai_rejects_structured_output_retries_plain(monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("LLM_ENDPOINT", "openai")
    state, session = _make_session()
    client_record = {}
    # First call: 400 naming structured output. Second: plain reply that
    # wraps the JSON in prose, to exercise the lenient parse on the retry.
    completions = _install_fake_openai(
        monkeypatch,
        [
            _BadRequest("The model does not support response_format"),
            _Completion(
                f"Sure, here you go:\n{_reply_payload(session)}\nRide well!"
            ),
        ],
        client_record,
    )

    result = llm.shape_session(session, state)

    assert result is session
    assert session.name == "Refined Name"
    assert len(completions.calls) == 2
    assert completions.calls[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in completions.calls[1]


def test_openai_unrelated_400_is_not_retried(monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("LLM_ENDPOINT", "openai")
    state, session = _make_session()
    before = session.to_dict()
    client_record = {}
    completions = _install_fake_openai(
        monkeypatch, [_BadRequest("The model 'nope' was not found")], client_record
    )

    result = llm.shape_session(session, state)

    assert result is session
    assert session.to_dict() == before
    assert len(completions.calls) == 1


def test_openai_network_error_returns_unchanged(monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("LLM_ENDPOINT", "openai")
    state, session = _make_session()
    before = session.to_dict()
    client_record = {}
    completions = _install_fake_openai(
        monkeypatch, [ConnectionError("connection refused")], client_record
    )

    result = llm.shape_session(session, state)

    assert result is session
    assert session.to_dict() == before
    assert len(completions.calls) == 1


def test_openai_prose_wrapped_reply_still_parsed(monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("LLM_ENDPOINT", "openai")
    state, session = _make_session()
    client_record = {}
    completions = _install_fake_openai(
        monkeypatch,
        [_Completion(f"Here is the plan: {_reply_payload(session)} done.")],
        client_record,
    )

    result = llm.shape_session(session, state)

    assert result is session
    assert session.name == "Refined Name"
    assert len(completions.calls) == 1


def test_custom_endpoint_without_key_uses_placeholder(monkeypatch):
    monkeypatch.setenv("LLM_ENDPOINT", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "local-model")
    state, session = _make_session()
    client_record = {}
    completions = _install_fake_openai(
        monkeypatch, [_Completion(_reply_payload(session))], client_record
    )

    result = llm.shape_session(session, state)

    assert result is session
    # The SDK rejects an empty api_key, so a keyless custom endpoint gets a
    # placeholder; the URL is used as the base_url, verbatim.
    assert client_record["api_key"] == "not-needed"
    assert client_record["base_url"] == "http://localhost:11434/v1"
    assert completions.calls[0]["model"] == "local-model"
    assert session.name == "Refined Name"


# ------------------------------------------- anthropic override path
def test_anthropic_override_model_uses_simple_call(monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("LLM_MODEL", "claude-haiku")
    state, session = _make_session()
    client_record = {}
    calls = []

    class _TextBlock:
        type = "text"
        # Prose-wrapped to prove the lenient parse is on this path too.
        text = (
            "Got it: " + _reply_payload(session) + " - enjoy the session."
        )

    def _Anthropic(**kwargs):
        client_record.update(kwargs)

        def create(**create_kwargs):
            calls.append(create_kwargs)
            return types.SimpleNamespace(content=[_TextBlock()])

        return types.SimpleNamespace(
            messages=types.SimpleNamespace(create=create)
        )

    module = types.ModuleType("anthropic")
    module.Anthropic = _Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", module)

    result = llm.shape_session(session, state)

    assert result is session
    assert client_record == {"api_key": "k"}
    call = calls[0]
    assert call["model"] == "claude-haiku"
    assert call["max_tokens"] == 2000
    # The simpler call: no thinking, no structured output - neither is
    # guaranteed to be accepted by every model.
    assert "thinking" not in call
    assert "output_config" not in call
    assert session.name == "Refined Name"


def test_anthropic_default_model_keeps_full_call(monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    state, session = _make_session()
    client_record = {}
    calls = []

    def _Anthropic(**kwargs):
        client_record.update(kwargs)

        def create(**create_kwargs):
            calls.append(create_kwargs)

            class _TextBlock:
                type = "text"
                text = _reply_payload(session)

            return types.SimpleNamespace(content=[_TextBlock()])

        return types.SimpleNamespace(
            messages=types.SimpleNamespace(create=create)
        )

    module = types.ModuleType("anthropic")
    module.Anthropic = _Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", module)

    result = llm.shape_session(session, state)

    assert result is session
    call = calls[0]
    assert call["model"] == "claude-sonnet-5"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"]["format"]["type"] == "json_schema"
