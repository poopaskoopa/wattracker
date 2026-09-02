from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from wattracker.cloud.budget_hook import create_budget_hook_app
from wattracker.cloud.limits import (
    clear_kill_switch,
    KILL_SWITCH_ENABLED,
    KillSwitchState,
    disable_public_api,
    read_kill_switch,
)
from wattracker.cloud.security import MemorySecurityStateBackend


TOKEN = "budget-hook-test-token"


class DurableMemoryBackend(MemorySecurityStateBackend):
    durable = True


def _client(backend):
    return TestClient(create_budget_hook_app(backend, expected_token=TOKEN))


def _functions_client(backend):
    return TestClient(
        create_budget_hook_app(
            backend, expected_token=TOKEN, platform_authenticated=True
        )
    )


def test_80_percent_disables_writes_and_persists_in_shared_backend():
    backend = DurableMemoryBackend()
    response = _client(backend).post(
        "/budget/disable-writes",
        headers={"X-Wattracker-Budget-Token": TOKEN},
        json={"action": "disable-public-api"},
    )
    assert response.status_code == 200
    assert read_kill_switch(backend).writes_enabled is False
    assert read_kill_switch(backend).public_enabled is True
    assert read_kill_switch(backend).reason == "budget 80%"


def test_100_percent_disables_both_and_survives_shared_backend_restart():
    backend = DurableMemoryBackend()
    assert _client(backend).post(
        "/budget/disable-public-api", headers={"X-Wattracker-Budget-Token": TOKEN}
    ).status_code == 200
    restarted_view = _client(backend)
    state = read_kill_switch(backend)
    assert state.writes_enabled is False
    assert state.public_enabled is False
    assert restarted_view.post(
        "/budget/disable-public-api", headers={"X-Wattracker-Budget-Token": TOKEN}
    ).status_code == 200


def test_clear_restores_service():
    backend = DurableMemoryBackend()
    disable_public_api(backend)
    clear_kill_switch(backend)
    state = read_kill_switch(backend)
    assert state.writes_enabled is KILL_SWITCH_ENABLED.writes_enabled
    assert state.public_enabled is KILL_SWITCH_ENABLED.public_enabled


def test_invalid_or_missing_auth_does_not_touch_backend():
    backend = DurableMemoryBackend()
    client = _client(backend)
    for kwargs in ({}, {"headers": {"X-Wattracker-Budget-Token": "wrong"}}):
        assert client.post("/budget/disable-public-api", **kwargs).status_code == 401
    assert read_kill_switch(backend) == KILL_SWITCH_ENABLED


def test_query_code_is_not_app_auth_and_valid_header_is_the_direct_host_seam():
    backend = DurableMemoryBackend()
    client = _client(backend)
    assert client.post("/budget/disable-public-api?code=" + TOKEN).status_code == 401
    assert client.post(
        "/budget/disable-public-api?code=function-host-key",
        headers={"X-Wattracker-Budget-Token": TOKEN},
    ).status_code == 200


def test_functions_host_authentication_allows_host_key_query_without_app_header():
    backend = DurableMemoryBackend()
    response = _functions_client(backend).post(
        "/budget/disable-public-api?code=function-host-key"
    )
    assert response.status_code == 200
    assert read_kill_switch(backend).public_enabled is False


@pytest.mark.parametrize(
    "path, headers",
    [
        ("/budget/disable-public-api?code=é", {}),
        (
            "/budget/disable-public-api",
            [(b"x-wattracker-budget-token", b"\xff")],
        ),
    ],
)
def test_non_ascii_query_or_header_tokens_return_401(path, headers):
    backend = DurableMemoryBackend()
    response = _client(backend).post(path, headers=headers)
    assert response.status_code == 401
    assert read_kill_switch(backend) == KILL_SWITCH_ENABLED


def test_clear_requires_operator_header_even_after_functions_host_auth():
    backend = DurableMemoryBackend()
    disable_public_api(backend)
    client = _functions_client(backend)

    assert client.post("/budget/clear?code=function-host-key").status_code == 401
    assert read_kill_switch(backend).public_enabled is False
    assert client.post(
        "/budget/clear?code=function-host-key",
        headers={"X-Wattracker-Budget-Token": TOKEN},
    ).status_code == 200
    assert read_kill_switch(backend) == KillSwitchState(
        writes_enabled=True,
        public_enabled=True,
        reason="operator clear",
        updated_at=read_kill_switch(backend).updated_at,
    )


def test_route_and_body_cannot_select_another_action():
    backend = DurableMemoryBackend()
    client = _client(backend)
    response = client.post(
        "/budget/disable-writes",
        headers={"X-Wattracker-Budget-Token": TOKEN},
        json={"action": "disable-public-api"},
    )
    assert response.status_code == 200
    assert read_kill_switch(backend).public_enabled is True
    assert client.post(
        "/budget/unknown", headers={"X-Wattracker-Budget-Token": TOKEN}
    ).status_code == 404
    assert client.get(
        "/budget/disable-public-api", headers={"X-Wattracker-Budget-Token": TOKEN}
    ).status_code == 405
    assert read_kill_switch(backend).public_enabled is True


def test_backend_failure_is_generic_503():
    class BrokenBackend:
        durable = True

        def write(self, *args, **kwargs):
            raise RuntimeError("secret backend details")

        def read(self, *args, **kwargs):
            raise RuntimeError("secret backend details")

    response = _client(BrokenBackend()).post(
        "/budget/disable-public-api", headers={"X-Wattracker-Budget-Token": TOKEN}
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "budget hook unavailable"}
    assert "secret backend" not in response.text
