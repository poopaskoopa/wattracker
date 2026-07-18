"""Tests for the LLM refinement fallback."""
from wattracker.analysis.state import TrainingState
from wattracker.prescribe.planner import plan_workout
from wattracker.prescribe import llm


def test_shape_session_no_key_returns_unchanged(monkeypatch):
    # Ensure no API key is present.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    state = TrainingState(ftp=250.0, tsb=0.0)
    session = plan_workout(state, 60)

    before = session.to_dict()
    result = llm.shape_session(session, state)

    assert result is session
    assert result.to_dict() == before
