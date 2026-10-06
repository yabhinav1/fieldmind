"""The shift report: what this device recorded, as Markdown, without private notes."""

from fastapi.testclient import TestClient

from fieldmind.api import create_app

FAULT = "Pump P-102 bearing vibration high at 7.2 mm/s, recommend replacement"
GAS = "Smell of gas near compressor K-4, isolated the line and cleared the bay"
HEALTH = "Technician Ravi Kumar reported chest pain during night shift, sent to clinic"
MASKED = "Motor M-9 overheating, call Operator Anil Sharma on 9876543210 before restarting"


def test_the_report_groups_notes_hazards_first_and_leaves_private_ones_out(fleet):
    a = fleet("edge-a")
    for text in (FAULT, GAS, HEALTH, MASKED):
        a.service.capture(text)
    report = a.service.report(hours=24)
    assert report.startswith("# Shift report: edge-a (plant-1)")
    assert report.index("## Safety hazards") < report.index("## Equipment faults")
    assert "**K-4** · Smell of gas" in report and "_(urgent, not yet synced)_" in report
    assert "chest pain" not in report and "Ravi" not in report, "private notes stay on the device"
    assert "[person removed]" in report and "9876543210" not in report, "masked notes appear as the fleet sees them"
    assert "private notes are not included" in report

    full = a.service.report(hours=24, include_private=True)
    assert "chest pain" in full and "9876543210" in full
    assert "_Nothing recorded in this period._" in a.service.report(hours=0)


def test_the_report_downloads_as_a_file(fleet):
    device = fleet("edge-a")
    device.service.capture(FAULT)
    with TestClient(create_app(device=device), client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/report").status_code == 401
        client.post("/api/auth/setup", json={"pin": "2468"})
        response = client.get("/api/report?hours=24")
        assert response.status_code == 200 and response.headers["content-type"].startswith("text/markdown")
        assert response.headers["content-disposition"].startswith("attachment; filename=\"fieldmind-edge-a-")
        assert "P-102" in response.text
