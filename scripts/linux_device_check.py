"""Checks the Linux container device (edge-c) through a real network cut.

    docker compose --profile linux-device up -d --build
    python scripts/linux_device_check.py [PIN]

Unlike the Network switch in the dashboard, this disconnects the container from the
network it uses to reach the cloud. The device has to notice on its own.
"""

import subprocess
import sys
import time

import httpx

BASE = "http://127.0.0.1:8003"
PIN = sys.argv[1] if len(sys.argv) > 1 else "2468"
NETWORK, CONTAINER = "fieldmind_uplink", "fieldmind-edge-c"
failures = []


def check(label, ok, detail=""):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{' - ' + str(detail) if detail else ''}")
    if not ok:
        failures.append(label)


def docker(*args):
    subprocess.run(["docker", *args], check=True, capture_output=True)


def wait_for(predicate, seconds=40):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(1)
    return None


client = httpx.Client(base_url=BASE, timeout=60)
client.post("/api/auth/login", json={"pin": PIN}).raise_for_status()


def status():
    return client.get("/api/status").json()


print("1. The device runs on Linux and reaches the cloud")
first = wait_for(lambda: status() if status()["link"]["online"] else None)
check("online", bool(first), first["device"]["id"] if first else "")
check("name masking model loaded", bool(first and first["engine"]["name_model"]))

print("2. The uplink is physically disconnected")
docker("network", "disconnect", NETWORK, CONTAINER)
try:
    lost = wait_for(lambda: status() if not status()["link"]["online"] else None)
    check("device noticed by itself", bool(lost) and lost["link"]["forced_offline"] is False)
    note = f"Chiller CH-7 condenser pressure high at {time.strftime('%H:%M:%S')}, fan belt slipping"
    saved = client.post("/api/memories", json={"text": note}).json()
    check("note saved while cut off", saved["created"] and saved["memory"]["sync_state"] == "pending")
    found = client.post("/api/search", json={"query": "condenser pressure problem"}).json()
    check("search still works", found["results"][0]["id"] == saved["memory"]["id"], f"{found['timing_ms']['search']} ms")
    check("change is queued", status()["outbox"]["pending"] >= 1)
finally:
    print("3. The uplink is reconnected")
    docker("network", "connect", NETWORK, CONTAINER)

back = wait_for(lambda: status() if status()["link"]["online"] and status()["outbox"]["pending"] == 0 else None, 60)
check("device reconnected and drained its queue without help", bool(back))
cloud = client.get("/api/cloud").json()
check("note reached the cloud", any(item["id"] == saved["memory"]["id"] for item in cloud["items"]))

print()
print("All steps passed." if not failures else f"{len(failures)} step(s) failed: {failures}")
sys.exit(1 if failures else 0)
