"""The HTTP layer: every route sits behind the device PIN."""

import pytest
from fastapi.testclient import TestClient

from fieldmind.api import create_app

ON_DEVICE = ("127.0.0.1", 50000)
ELSEWHERE = ("192.168.1.50", 50000)


@pytest.fixture
def app(fleet):
    return create_app(device=fleet("edge-a"))


def test_everything_is_locked_until_a_pin_is_entered(app):
    with TestClient(app, client=ON_DEVICE) as client:
        for method, path in (("GET", "/api/status"), ("GET", "/api/memories"), ("GET", "/api/events"),
                             ("POST", "/api/search"), ("POST", "/api/memories"), ("GET", "/api/cloud")):
            assert client.request(method, path, json={}).status_code == 401, path
        assert client.get("/").status_code == 200  # the page shell holds no data


def test_first_pin_is_chosen_on_the_device(app):
    with TestClient(app, client=ON_DEVICE) as client:
        remote = TestClient(app, client=ELSEWHERE)  # same running app, reached over the network
        state = remote.get("/api/auth/state").json()
        assert state == {**state, "configured": False, "authenticated": False, "can_set_up": False}
        assert remote.post("/api/auth/setup", json={"pin": "2468"}).status_code == 403

        assert client.post("/api/auth/setup", json={"pin": "12"}).status_code == 400
        assert client.post("/api/auth/setup", json={"pin": "2468"}).status_code == 200
        assert client.get("/api/status").json()["device"]["id"] == "edge-a"
        assert client.post("/api/auth/setup", json={"pin": "9999"}).status_code == 409


def test_login_logout_and_lockout(app):
    with TestClient(app, client=ON_DEVICE) as client:
        client.post("/api/auth/setup", json={"pin": "2468"})
        client.post("/api/auth/logout")
        assert client.get("/api/status").status_code == 401

        assert client.post("/api/auth/login", json={"pin": "0000"}).status_code == 401
        assert client.post("/api/auth/login", json={"pin": "2468"}).status_code == 200
        assert client.get("/api/status").status_code == 200

        client.post("/api/auth/logout")
        for _ in range(4):
            assert client.post("/api/auth/login", json={"pin": "0000"}).status_code == 401
        assert client.post("/api/auth/login", json={"pin": "0000"}).status_code == 429
        # Even the right PIN is refused while the lockout lasts.
        assert client.post("/api/auth/login", json={"pin": "2468"}).status_code == 429


def test_pin_is_not_stored_in_plain_text(fleet):
    device = fleet("edge-a")
    device.settings.pin = "2468"
    with TestClient(create_app(device=device), client=ON_DEVICE) as client:
        assert client.get("/api/auth/state").json()["configured"] is True
        assert client.post("/api/auth/login", json={"pin": "2468"}).status_code == 200
    # The journal is closed with the app; read the stored record from the file.
    import sqlite3

    raw = sqlite3.connect(device.settings.journal_path).execute("SELECT value FROM kv WHERE key = 'pin'").fetchone()[0]
    assert "2468" not in raw and "hash" in raw


def test_capture_and_search_over_http(app):
    with TestClient(app, client=ON_DEVICE) as client:
        client.post("/api/auth/setup", json={"pin": "2468"})
        saved = client.post("/api/memories", json={"text": "Suresh fixed pump P-102 seal yesterday"}).json()
        assert saved["memory"]["scope"] == "redacted" and "Suresh" not in saved["memory"]["shared_text"]
        found = client.post("/api/search", json={"query": "pump seal repair"}).json()
        assert found["results"][0]["id"] == saved["memory"]["id"]
