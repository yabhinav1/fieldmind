"""The audit export: the whole activity log as JSON lines, for the device's owner."""

import json

from fastapi.testclient import TestClient

from fieldmind.api import create_app

FAULT = "Pump P-102 bearing vibration high at 7.2 mm/s, recommend replacement"
HEALTH = "Technician Ravi Kumar reported chest pain during night shift, sent to clinic"


def test_the_audit_log_exports_every_event_oldest_first(fleet):
    device = fleet("edge-a")
    device.service.capture(FAULT)
    device.service.capture(HEALTH)
    with TestClient(create_app(device=device), client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/audit").status_code == 401, "locked like everything else"
        client.post("/api/auth/setup", json={"pin": "2468"})
        response = client.get("/api/audit")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        assert response.headers["content-disposition"].startswith('attachment; filename="fieldmind-edge-a-audit-')
        lines = [json.loads(line) for line in response.text.splitlines()]
        assert len(lines) >= 3
        assert [line["ts"] for line in lines] == sorted(line["ts"] for line in lines)
        assert all(line["device"] == "edge-a" and line["site"] == "plant-1" and line["at"] for line in lines)
        decisions = [line for line in lines if line["type"] == "capture"]
        assert len(decisions) == 2, [line["type"] for line in lines]
        assert any("chest pain" in line["message"] for line in lines), "the owner's export is complete, private text included"
        assert lines[-1]["type"] == "audit", "the export is itself recorded"
