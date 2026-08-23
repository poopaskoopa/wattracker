"""Unit tests for the LLM config resolver (config.llm_settings and friends)."""
import json
import os

from wattracker import config


def _write_config(data: dict) -> None:
    path = config.config_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _read_config() -> dict:
    path = config.config_path()
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f) or {}


# ------------------------------------------------------------- resolution
def test_api_key_alone_resolves_anthropic_default(monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    s = config.llm_settings()
    assert s is not None
    assert s.endpoint == "anthropic"
    assert s.model == "claude-sonnet-5"
    assert s.api_key == "k"
    assert s.base_url is None


def test_openai_endpoint_resolves_default_model(monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("LLM_ENDPOINT", "openai")
    s = config.llm_settings()
    assert s is not None
    assert s.endpoint == "openai"
    assert s.model == "gpt-5.6-luna"
    assert s.base_url is None


def test_openrouter_endpoint_resolves_default_model(monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("LLM_ENDPOINT", "openrouter")
    s = config.llm_settings()
    assert s is not None
    assert s.endpoint == "openrouter"
    assert s.model == "google/gemini-3.7-flash"
    assert s.base_url == "https://openrouter.ai/api/v1"


def test_endpoint_keyword_is_case_insensitive_and_trimmed(monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("LLM_ENDPOINT", "  OPENAI  ")
    s = config.llm_settings()
    assert s is not None
    assert s.endpoint == "openai"


def test_llm_model_env_overrides_default(monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("LLM_ENDPOINT", "openai")
    monkeypatch.setenv("LLM_MODEL", "my-model")
    s = config.llm_settings()
    assert s is not None
    assert s.model == "my-model"


def test_custom_url_with_path_kept_as_is(monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("LLM_ENDPOINT", "http://host:1234/custom/v1/")
    monkeypatch.setenv("LLM_MODEL", "m")
    s = config.llm_settings()
    assert s is not None
    assert s.endpoint == "http://host:1234/custom/v1"
    assert s.base_url == "http://host:1234/custom/v1"


def test_custom_bare_host_gets_v1_appended(monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("LLM_ENDPOINT", "http://localhost:11434")
    monkeypatch.setenv("LLM_MODEL", "some-model")
    s = config.llm_settings()
    assert s is not None
    assert s.endpoint == "http://localhost:11434/v1"
    assert s.base_url == "http://localhost:11434/v1"


def test_literal_custom_is_invalid(monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("LLM_ENDPOINT", "custom")
    assert config.llm_settings() is None


def test_invalid_endpoint_values_are_rejected(monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    for bad in ("ftp://x", "not a url", "https://", "http://user:pw@host/",
                "http://host/path#frag", "http://host with space/v1"):
        monkeypatch.setenv("LLM_ENDPOINT", bad)
        assert config.llm_settings() is None, bad


def test_custom_without_model_is_disabled(monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("LLM_ENDPOINT", "http://localhost:11434/v1")
    assert config.llm_settings() is None


def test_custom_url_may_be_keyless(monkeypatch):
    monkeypatch.setenv("LLM_ENDPOINT", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "some-model")
    s = config.llm_settings()
    assert s is not None
    assert s.api_key is None


def test_named_endpoint_without_key_is_disabled(monkeypatch):
    monkeypatch.setenv("LLM_ENDPOINT", "openai")
    assert config.llm_settings() is None


def test_legacy_anthropic_api_key_env_fallback(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "legacy-key")
    s = config.llm_settings()
    assert s is not None
    assert s.api_key == "legacy-key"
    assert s.endpoint == "anthropic"
    assert s.model == "claude-sonnet-5"


def test_new_key_beats_legacy_key(monkeypatch):
    monkeypatch.setenv("API_KEY", "new-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "legacy-key")
    s = config.llm_settings()
    assert s is not None
    assert s.api_key == "new-key"


def test_legacy_config_json_key_fallback():
    _write_config({"anthropic_api_key": "stored-legacy"})
    s = config.llm_settings()
    assert s is not None
    assert s.api_key == "stored-legacy"
    assert s.endpoint == "anthropic"


def test_non_string_config_values_are_ignored_without_crashing(monkeypatch):
    # A hand-edited config.json can carry a bare number or a list; the
    # resolver must treat that as absent (with a one-time warning), never
    # raise - llm_settings() runs on every /generate.
    _write_config(
        {"api_key": True, "llm_endpoint": 42, "llm_model": ["x"]}
    )
    assert config.llm_settings() is None  # no usable key -> disabled, no crash
    monkeypatch.setenv("ANTHROPIC_API_KEY", "legacy-key")
    s = config.llm_settings()
    assert s is not None
    assert s.api_key == "legacy-key"
    assert s.endpoint == "anthropic"
    assert s.model == "claude-sonnet-5"


def test_custom_url_port_range_is_validated():
    for bad in ("http://host:99999/v1", "http://host:0/v1"):
        assert config.normalize_llm_endpoint(bad) is None, bad
    assert config.normalize_llm_endpoint("http://host:65535/v1") == (
        "http://host:65535/v1"
    )
    assert config.normalize_llm_endpoint("http://host:1/v1") == "http://host:1/v1"


def test_config_json_endpoint_and_model():
    _write_config(
        {
            "api_key": "k",
            "llm_endpoint": "openrouter",
            "llm_model": "stored-model",
        }
    )
    s = config.llm_settings()
    assert s is not None
    assert s.endpoint == "openrouter"
    assert s.model == "stored-model"
    assert s.api_key == "k"


def test_env_overrides_config_json(monkeypatch):
    _write_config(
        {
            "api_key": "stored-key",
            "llm_endpoint": "openrouter",
            "llm_model": "stored-model",
        }
    )
    monkeypatch.setenv("API_KEY", "env-key")
    monkeypatch.setenv("LLM_ENDPOINT", "openai")
    monkeypatch.setenv("LLM_MODEL", "env-model")
    s = config.llm_settings()
    assert s is not None
    assert s.api_key == "env-key"
    assert s.endpoint == "openai"
    assert s.model == "env-model"


# --------------------------------------------------------------- persistence
def test_set_llm_settings_writes_new_keys():
    config.set_llm_settings(
        endpoint="openrouter", model="x", api_key="k"
    )
    data = _read_config()
    assert data["llm_endpoint"] == "openrouter"
    assert data["llm_model"] == "x"
    assert data["api_key"] == "k"
    assert "anthropic_api_key" not in data


def test_set_llm_settings_custom_url_stored_in_llm_endpoint():
    config.set_llm_settings(custom_url="http://localhost:11434/v1")
    data = _read_config()
    assert data["llm_endpoint"] == "http://localhost:11434/v1"
    assert "llm_model" not in data


def test_set_llm_settings_rejects_literal_custom():
    config.set_llm_settings(endpoint="custom")
    data = _read_config()
    assert "llm_endpoint" not in data


def test_set_llm_settings_api_key_removes_legacy_key():
    _write_config({"anthropic_api_key": "old"})
    config.set_llm_settings(api_key="new")
    data = _read_config()
    assert data["api_key"] == "new"
    assert "anthropic_api_key" not in data


def test_set_llm_settings_partial_save_does_not_clobber():
    _write_config({"llm_endpoint": "openrouter", "llm_model": "m", "api_key": "k"})
    config.set_llm_settings(api_key="k2")
    data = _read_config()
    assert data["llm_endpoint"] == "openrouter"
    assert data["llm_model"] == "m"
    assert data["api_key"] == "k2"


def test_set_llm_settings_clear_model():
    _write_config({"llm_endpoint": "openrouter", "llm_model": "m", "api_key": "k"})
    config.set_llm_settings(model="", clear_model=True)
    data = _read_config()
    assert "llm_model" not in data
    # The resolver now falls back to the provider default.
    s = config.llm_settings()
    assert s is not None
    assert s.model == "google/gemini-3.7-flash"


def test_set_llm_settings_empty_model_without_clear_keeps_stored():
    _write_config({"llm_endpoint": "openrouter", "llm_model": "m", "api_key": "k"})
    config.set_llm_settings(api_key="k2")
    data = _read_config()
    assert data["llm_model"] == "m"


def test_set_llm_settings_lowercases_endpoint_keyword():
    config.set_llm_settings(endpoint="OPENAI")
    data = _read_config()
    assert data["llm_endpoint"] == "openai"


def test_endpoint_change_is_logged(caplog):
    import logging

    config.set_llm_settings(endpoint="anthropic")
    with caplog.at_level(logging.WARNING, logger="wattracker.config"):
        config.set_llm_settings(endpoint="openrouter")
    assert any("LLM endpoint changed" in r.message for r in caplog.records)
    # Re-saving the same value does not re-log.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="wattracker.config"):
        config.set_llm_settings(endpoint="openrouter")
    assert not any("LLM endpoint changed" in r.message for r in caplog.records)


def test_deprecated_wrappers_still_work():
    config.set_anthropic_api_key("legacy")
    data = _read_config()
    assert data["api_key"] == "legacy"
    assert config.anthropic_api_key_set() is True
