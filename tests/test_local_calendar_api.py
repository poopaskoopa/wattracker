"""The read-only calendar feed used by the local iOS backend."""

import json

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from wattracker.server import create_app


@pytest.fixture()
def client():
    with TestClient(create_app()) as value:
        yield value


def _register(client):
    response = client.post(
        "/register",
        data={"username": "rider", "password": "correct horse battery staple"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)


def test_api_calendar_returns_the_same_month_model_as_html(client):
    _register(client)

    response = client.get("/api/calendar?year=2026&month=8")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["weeks"]) == 6
    assert payload["weeks"][0][0]["date"] == "2026-07-27"
    assert payload["weeks"][0][0]["in_month"] is False
    assert payload["weeks"][0][2]["date"] == "2026-07-29"


def test_api_calendar_requires_authentication(client):
    response = client.get("/api/calendar", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/welcome"

    followed = client.get("/api/calendar", follow_redirects=True)

    assert followed.status_code == 200
    assert followed.url.path == "/welcome"
    assert followed.headers["content-type"].startswith("text/html")
    with pytest.raises(json.JSONDecodeError):
        followed.json()
