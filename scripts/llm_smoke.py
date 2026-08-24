#!/usr/bin/env python3
"""One-off live smoke test for the LLM refinement path (manual, not in the
test suite).

Reads LLM_ENDPOINT / LLM_MODEL / API_KEY from a .env-style file (the legacy
ANTHROPIC_API_KEY works too), resolves the configuration exactly the way the
server does, plans a 60-minute workout for a synthetic rider, and calls
llm.shape_session() against the live endpoint with the real SDK. The
fake-SDK unit tests cannot verify the wire behaviour a real provider has to
accept (base_url joining, response_format support, the parse of the reply) -
this does.

Usage:  scripts/llm_smoke.py [path/to/.env]     (default: ./.env)

The file format is plain KEY=VALUE lines; # comments and blank lines are
ignored, surrounding quotes are stripped. Only the three LLM variables are
consumed; a key is not required for a custom (local) endpoint.

Run it from the repo root with the project venv, e.g.
    .venv/bin/python scripts/llm_smoke.py .env
It never reads or writes the real ~/.wattracker config: the data dir is
pointed at a fresh temp directory, so env values are the only source.
"""
import logging
import os
import sys
import tempfile
import time


def load_env_file(path: str) -> None:
    if not os.path.exists(path):
        sys.exit(
            f"no env file at {path} - create one with, e.g.\n"
            "  LLM_ENDPOINT=https://api.openai.com/v1\n"
            "  LLM_MODEL=gpt-5.6-luna\n"
            "  API_KEY=sk-..."
        )
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ[key.strip()] = value.strip().strip('"').strip("'")


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    load_env_file(sys.argv[1] if len(sys.argv) > 1 else ".env")
    # A fresh data dir: the real app config must never be read or written.
    os.environ["WATTRACKER_DATA_DIR"] = tempfile.mkdtemp(
        prefix="wattracker-llm-smoke-"
    )

    from wattracker import config
    from wattracker.analysis.state import TrainingState
    from wattracker.prescribe import llm
    from wattracker.prescribe.planner import plan_workout

    settings = config.llm_settings()
    if settings is None:
        sys.exit(
            "llm_settings() resolved to None - check the values above "
            "(a warning line should say why)."
        )
    print(
        f"resolved: endpoint={settings.endpoint} model={settings.model} "
        f"api_key={'yes' if settings.api_key else 'no (keyless custom)'}"
    )

    state = TrainingState(ftp=250.0, tsb=0.0)
    session = plan_workout(state, 60)
    before = session.to_dict()

    t0 = time.time()
    refined = llm.shape_session(session, state)
    elapsed = time.time() - t0
    after = session.to_dict()
    assert refined is session

    def show(label: str, s: dict) -> None:
        print(f"--- {label} ---")
        print(f"name:        {s['name']}")
        print(f"description: {s['description']}")
        for i, seg in enumerate(s["segments"]):
            print(f"segment {i}:   {seg.get('text')}")

    show("before", before)
    show("after", after)
    if before == after:
        print(
            f"UNCHANGED after {elapsed:.1f}s - the call failed or returned "
            "no refinements; the WARNING line above (if any) says why. "
            "If the error names response_format or the model, the provider "
            "rejected something the retry could not recover from. A timeout "
            "or an empty reply at ~60s smells like a reasoning model: its "
            "thinking consumes the 2000-token budget and/or overruns the "
            "60s window, so use a non-reasoning (instruct) model."
        )
        sys.exit(2)
    print(f"CHANGED after {elapsed:.1f}s - the live endpoint refined the plan.")


if __name__ == "__main__":
    main()
