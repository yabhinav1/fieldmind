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
        assert state == {**state, "configured": False, "authenticated": False, "needs_setup_code": True}
        assert remote.post("/api/auth/setup", json={"pin": "2468"}).status_code == 403
        assert client.get("/api/auth/state").json()["needs_setup_code"] is False

        assert client.post("/api/auth/setup", json={"pin": "12"}).status_code == 400
        assert client.post("/api/auth/setup", json={"pin": "2468"}).status_code == 200
        assert client.get("/api/status").json()["device"]["id"] == "edge-a"
        assert client.post("/api/auth/setup", json={"pin": "9999"}).status_code == 409


def test_first_pin_over_the_network_needs_the_console_setup_code(fleet, capsys):
    app = create_app(device=fleet("edge-a"))
    with TestClient(app, client=ELSEWHERE) as remote:
        code = capsys.readouterr().out.split("setup code ")[1].split()[0]
        assert len(code) == 6
        assert remote.post("/api/auth/setup", json={"pin": "2468", "setup_code": "NOPE00"}).status_code == 403
        assert remote.post("/api/auth/setup", json={"pin": "2468", "setup_code": code}).status_code == 403, \
            "a wrong guess replaces the code, so the old one is useless"
        fresh = capsys.readouterr().out.split("setup code ")[-1].split()[0]
        assert fresh != code
        assert remote.post("/api/auth/setup", json={"pin": "2468", "setup_code": fresh.lower()}).status_code == 200
        assert remote.get("/api/status").status_code == 200
        assert remote.post("/api/auth/setup", json={"pin": "1111", "setup_code": fresh}).status_code == 409


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


def test_sessions_and_lockout_survive_a_restart(fleet):
    app = create_app(device=fleet("edge-a"))
    with TestClient(app, client=ON_DEVICE) as client:
        client.post("/api/auth/setup", json={"pin": "2468"})
        cookie = dict(client.cookies)
        remote = TestClient(app, client=ELSEWHERE)
        for _ in range(4):
            remote.post("/api/auth/login", json={"pin": "0000"})

    app = create_app(device=fleet("edge-a"))  # same data directory, as after a reboot
    with TestClient(app, client=ON_DEVICE) as client:
        client.cookies.update(cookie)
        assert client.get("/api/status").status_code == 200, "the session should outlive the process"
        # Four wrong guesses were made before the restart; the fifth still locks the device.
        remote = TestClient(app, client=ELSEWHERE)
        assert remote.post("/api/auth/login", json={"pin": "0000"}).status_code == 429


def test_changing_the_pin_keeps_the_new_one_over_a_preset(fleet):
    device = fleet("edge-a")
    device.settings.pin = "2468"
    app = create_app(device=device)
    with TestClient(app, client=ON_DEVICE) as client:
        client.post("/api/auth/login", json={"pin": "2468"})
        other = TestClient(app, client=ON_DEVICE)
        other.post("/api/auth/login", json={"pin": "2468"})
        assert client.post("/api/auth/pin", json={"current": "0000", "new": "1357"}).status_code == 401
        assert client.post("/api/auth/pin", json={"current": "2468", "new": "13"}).status_code == 400
        assert client.post("/api/auth/pin", json={"current": "2468", "new": "1357"}).status_code == 200
        assert client.get("/api/status").status_code == 200, "the session that changed the PIN stays open"
        assert other.get("/api/status").status_code == 401, "every other session is closed"

    again = fleet("edge-a")
    again.settings.pin = "2468"  # the preset is still in the environment after a restart
    with TestClient(create_app(device=again), client=ON_DEVICE) as client:
        assert client.post("/api/auth/login", json={"pin": "2468"}).status_code == 401
        assert client.post("/api/auth/login", json={"pin": "1357"}).status_code == 200


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


def test_probes_need_no_pin_and_carry_no_note_text(fleet):
    device = fleet("edge-a")
    device.service.capture("Technician Ravi Kumar reported chest pain during night shift, sent to clinic")
    device.service.search("chest pain")
    with TestClient(create_app(device=device), client=ELSEWHERE) as client:
        assert client.get("/healthz").json()["status"] == "ok"
        body = client.get("/metrics").text
        assert 'fieldmind_memories_private{device="edge-a",site="plant-1"} 1' in body
        assert "fieldmind_search_ms" in body and "fieldmind_outbox_pending" in body
        assert "Ravi" not in body and "chest" not in body

    device = fleet("edge-b")
    device.settings.metrics_token = "s3cret"
    with TestClient(create_app(device=device), client=ELSEWHERE) as client:
        assert client.get("/metrics").status_code == 401
        assert client.get("/metrics", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_capture_and_search_over_http(app):
    with TestClient(app, client=ON_DEVICE) as client:
        client.post("/api/auth/setup", json={"pin": "2468"})
        saved = client.post("/api/memories", json={"text": "Suresh fixed pump P-102 seal yesterday"}).json()
        assert saved["memory"]["scope"] == "redacted" and "Suresh" not in saved["memory"]["shared_text"]
        found = client.post("/api/search", json={"query": "pump seal repair"}).json()
        assert found["results"][0]["id"] == saved["memory"]["id"]


def test_the_dashboard_is_installable_and_its_shell_needs_no_pin(app):
    with TestClient(app, client=ELSEWHERE) as client:
        manifest = client.get("/manifest.webmanifest")
        assert manifest.status_code == 200 and manifest.json()["display"] == "standalone"
        worker = client.get("/sw.js")
        assert worker.status_code == 200 and "javascript" in worker.headers["content-type"]
        assert worker.headers["service-worker-allowed"] == "/"
        assert "/api/" in worker.text, "the worker names the API prefix it must never cache"
        icon = client.get("/static/icon.svg")
        assert icon.status_code == 200 and icon.headers["cache-control"] == "no-cache"
        assert client.get("/static/app.js").headers["cache-control"] == "no-cache", "updates show on the next load"
