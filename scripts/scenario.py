"""Walks two running devices through the full edge-to-cloud story and checks each step.

Start the cloud and both devices first (see README), then:

    python scripts/scenario.py [PIN]

Set FIELDMIND_A and FIELDMIND_B to test devices on other addresses.

Pass the devices' PIN as the first argument. A device that has no PIN yet is given it,
which works from the device's own machine; a device elsewhere must have its PIN set once
from the dashboard first (it asks for the setup code printed in its console).
"""

import os
import sys
import time

import httpx

A = os.environ.get("FIELDMIND_A", "http://127.0.0.1:8001")
B = os.environ.get("FIELDMIND_B", "http://127.0.0.1:8002")
PIN = sys.argv[1] if len(sys.argv) > 1 else "2468"
failures = []
sessions = {}


def session(base):
    """One logged-in HTTP session per device."""
    if base not in sessions:
        client = httpx.Client(base_url=base, timeout=120)
        state = client.get("/api/auth/state").json()
        response = client.post("/api/auth/login" if state["configured"] else "/api/auth/setup", json={"pin": PIN})
        if response.status_code == 403 and not state["configured"]:
            sys.exit(f"{base} has no PIN yet and is not on this machine. Open its dashboard once, enter the setup code "
                     "from its console and choose a PIN, then run this script with that PIN.")
        if response.status_code != 200:
            sys.exit(f"Could not unlock {base}: {response.json().get('detail')}. Pass the PIN as an argument.")
        sessions[base] = client
    return sessions[base]


def call(base, method, path, **body):
    response = session(base).request(method, path, json=body or None)
    response.raise_for_status()
    return response.json()


def check(label, ok, detail=""):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{' - ' + str(detail) if detail else ''}")
    if not ok:
        failures.append(label)


def sync(base):
    for _ in range(20):
        result = call(base, "POST", "/api/sync/run")
        if result["status"] != "busy":
            return result
        time.sleep(0.3)
    return result


def cloud_texts():
    return [item["text"] for item in call(A, "GET", "/api/cloud")["items"]]


print("0. Devices are locked")
check("API refuses requests without the PIN", httpx.get(A + "/api/status").status_code == 401)
session(A), session(B)

print("1. Headquarters publishes manuals; device A downloads them")
call(A, "POST", "/api/link/offline", value=False)
call(B, "POST", "/api/link/offline", value=False)
published = call(A, "POST", "/api/demo/seed-cloud")["published"]
sync(A)
status = call(A, "GET", "/api/status")
check("manuals are on the device", status["memory"]["replica"] >= published, status["memory"]["replica"])

print("2. Device A loses the network and keeps working")
call(A, "POST", "/api/link/offline", value=True)
created = call(A, "POST", "/api/demo/seed")["created"]
status = call(A, "GET", "/api/status")
check("link reported as offline", status["link"]["online"] is False)
check("notes saved while offline", created >= 6, created)
check("shareable notes are queued", status["outbox"]["pending"] >= 5, status["outbox"]["pending"])
check("private notes are not queued", status["memory"]["private"] >= 3, status["memory"]["private"])

found = call(A, "POST", "/api/search", query="which machine is running too hot")
top = found["results"][0]
check("search works offline", "M-9" in top["text"] or top["asset"] == "M-9", top["text"][:60])
check("the technician's own note is found", any("Anil" in r["text"] for r in found["results"][:3]))
check("search made no network calls", found["network_calls"] == 0, f"{found['timing_ms']['search']} ms")
found = call(A, "POST", "/api/search", query="what vibration level is unacceptable")
check("cloud manual is searchable offline", found["results"][0]["source"] == "replica", found["results"][0]["text"][:60])
check("sync refuses while offline", sync(A)["status"] == "offline")

masked = call(A, "POST", "/api/memories", text="Suresh cleaned the strainer on cooling pump P-210, flow back to 42 m3/h")
check("a bare name is masked for the cloud", "Suresh" not in (masked["memory"]["shared_text"] or "Suresh"),
      masked["memory"]["shared_text"])

print("3. The network returns")
call(A, "POST", "/api/link/offline", value=False)
result = sync(A)
check("queued notes were sent", result["status"] == "ok", result)
texts = cloud_texts()
joined = " ".join(texts)
check("cloud has the pump note", any("P-102" in t for t in texts))
check("health note never left the device", "chest pain" not in joined)
check("password never left the device", "Plant@2026" not in joined)
check("phone number was masked", "9876543210" not in joined and any("[phone removed]" in t for t in texts))
check("bare name never left the device", "Suresh" not in joined and any("P-210" in t for t in texts))
check("nothing left waiting", call(A, "GET", "/api/status")["outbox"]["pending"] == 0)

print("4. Device B receives what A shared")
sync(B)
found = call(B, "POST", "/api/search", query="pump bearing vibration", asset="P-102", source="replica")
pump = next(r for r in found["results"] if r["origin_device"] == "edge-a")
check("B can find A's note", pump["source"] == "replica", pump["text"][:60])

print("5. Both devices go offline and change the same note")
call(A, "POST", "/api/link/offline", value=True)
call(B, "POST", "/api/link/offline", value=True)
call(A, "PATCH", f"/api/memories/{pump['id']}", text="Pump P-102 bearing vibration 7.2 mm/s, replace at next shutdown")
call(B, "PATCH", f"/api/memories/{pump['id']}", text="Pump P-102 bearing vibration 7.2 mm/s, stop the pump immediately")
call(A, "POST", "/api/link/offline", value=False)
sync(A)
call(B, "POST", "/api/link/offline", value=False)
sync(B)
conflicts = call(B, "GET", "/api/sync")["conflicts"]
check("B raises a conflict instead of overwriting", len(conflicts) == 1, conflicts[0]["fields"] if conflicts else "")
check("cloud still holds A's version", any("next shutdown" in t for t in cloud_texts()))

print("6. The technician on B decides")
call(B, "POST", f"/api/conflicts/{conflicts[0]['id']}/resolve", choice="mine")
sync(B)
sync(A)
check("A now shows B's decision", "stop the pump immediately" in call(A, "GET", f"/api/memories/{pump['id']}")["memory"]["text"])

manual = call(A, "POST", "/api/search", query="boiler normal operating pressure", source="replica")["results"][0]
check("a field note did not replace the manual", manual["kind"] == "reference" and manual["status"] == "active", manual["text"][:50])

print("7. A follow-up replaces what the fleet believed")
saved = call(B, "POST", "/api/memories", text="Pump P-102 bearing replaced, vibration now 1.8 mm/s, normal")
check("follow-up linked to the earlier note", saved["memory"]["supersedes"] == pump["id"], saved["memory"].get("relation"))
sync(B)
sync(A)
answer = call(A, "POST", "/api/ask", question="what is the condition of pump P-102")
check("A answers with the latest state", "1.8 mm/s" in answer["answer"], answer["answer"][:80])

print("8. The replica is rebuilt from a cloud snapshot")
before = call(A, "GET", "/api/status")["memory"]
rebuilt = call(A, "POST", "/api/replica/rebuild")
after = call(A, "GET", "/api/status")["memory"]
check("rebuilt from a Qdrant Server snapshot", rebuilt["method"] == "snapshot", f"{rebuilt.get('bytes_down', 0)} bytes")
check("replica holds the same memories as before", after["replica"] == before["replica"], after["replica"])
check("device-written memories untouched", (after["local"], after["private"]) == (before["local"], before["private"]))
found = call(A, "POST", "/api/search", query="what vibration level is unacceptable")
check("restored replica is searchable", found["results"][0]["source"] == "replica", f"{found['timing_ms']['search']} ms")

answer = call(A, "POST", "/api/ask", question="is a vibration of 7.2 mm/s acceptable")
print(f"   answer engine: {answer['engine']}")
print(f"   answer: {answer['answer'][:160]}")

print()
print("All steps passed." if not failures else f"{len(failures)} step(s) failed: {failures}")
sys.exit(1 if failures else 0)
