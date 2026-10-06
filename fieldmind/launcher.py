"""Start, stop and reset the demo devices from one place, on any OS.

    python -m fieldmind start                  two devices (edge-a, edge-b) and the cloud
    python -m fieldmind start --pin 2468       with the PIN pre-set
    python -m fieldmind start --only edge-b --cloud-url http://192.168.1.20:6333
    python -m fieldmind start --lan            reachable from phones on the same network
    python -m fieldmind stop                   stop the devices started here
    python -m fieldmind reset-all              clean slate: stop, erase both devices and the cloud, start again

Each device runs as its own process; its log and pid live under data/logs.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import httpx

from .config import ROOT, Settings

DEVICES = (("edge-a", 8001, "technician-a"), ("edge-b", 8002, "technician-b"))


def _logs(settings: Settings) -> Path:
    path = settings.data_root / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pid_file(settings: Settings, device: str) -> Path:
    return _logs(settings) / f"{device}.pid"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _ready(port: int, seconds: float = 90) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2)
            return True
        except httpx.HTTPError:
            time.sleep(0.5)
    return False


def _setup_code(log: Path) -> str | None:
    """The one-time setup code a PIN-less device printed to its console, if any."""
    try:
        found = re.findall(r"setup code ([A-Z0-9]{6})", log.read_text(errors="replace"))
    except OSError:
        return None
    return found[-1] if found else None


def _local_cloud(url: str) -> bool:
    return any(host in url for host in ("//localhost", "//127.0.0.1"))


def start_cloud(settings: Settings) -> None:
    if not _local_cloud(settings.cloud_url):
        print(f"Using the cloud at {settings.cloud_url}")
        return
    if not shutil.which("docker"):
        print("Docker is not installed, so no cloud was started. The devices work offline and sync to peers if configured.")
        return
    result = subprocess.run(["docker", "compose", "-f", str(ROOT / "docker-compose.yml"), "up", "-d", "qdrant"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        print("The cloud did not start. Is Docker running? The devices still work offline.")


def start(settings: Settings, only: str | None = None, lan: bool = False, open_browser: bool = True,
          pin: str | None = None) -> int:
    start_cloud(settings)
    devices = [d for d in DEVICES if not only or d[0] == only] or [(only, 8001, "technician")]
    host = "0.0.0.0" if lan else "127.0.0.1"
    env = {**os.environ}
    if pin:
        env["FIELDMIND_PIN"] = pin
    logs = _logs(settings)
    started = []
    for name, port, author in devices:
        pid_file = _pid_file(settings, name)
        if pid_file.exists() and _alive(int(pid_file.read_text() or 0)):
            print(f"{name} is already running on port {port}.")
            continue
        out = open(logs / f"{name}.log", "ab")
        process = subprocess.Popen(
            [sys.executable, "-m", "fieldmind", "serve", "--device", name, "--port", str(port), "--author", author,
             "--host", host],
            cwd=str(ROOT), env=env, stdout=out, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        pid_file.write_text(str(process.pid))
        started.append((name, port))

    failed = 0
    for name, port in started:
        url = f"http://127.0.0.1:{port}"
        if _ready(port):
            print(f"{name} ready at {url}" + (f"  (PIN {pin})" if pin else ""))
            code = _setup_code(logs / f"{name}.log")
            if code:
                print(f"  {name} has no PIN yet. On this computer, just choose one on the lock screen. "
                      f"From a phone or another computer, enter setup code {code} first.")
            if open_browser:
                webbrowser.open(url)
        else:
            failed += 1
            print(f"{name} did not start. See {logs / f'{name}.log'}")
    if lan:
        print(f"From other devices on this network, open http://<this machine's address>:{devices[0][1]}")
    return 1 if failed else 0


def stop(settings: Settings) -> int:
    stopped = 0
    for name, _, _ in DEVICES:
        pid_file = _pid_file(settings, name)
        if not pid_file.exists():
            continue
        pid = int(pid_file.read_text() or 0)
        if pid and _alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(20):
                    if not _alive(pid):
                        break
                    time.sleep(0.25)
                else:
                    os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            print(f"Stopped {name}.")
            stopped += 1
        pid_file.unlink(missing_ok=True)
    if not stopped:
        print("No devices started from here are running.")
    return 0


def reset_all(settings: Settings, pin: str | None = None, open_browser: bool = True) -> int:
    from .cloud import Cloud

    stop(settings)
    time.sleep(0.5)
    for name, _, _ in DEVICES:
        shutil.rmtree(settings.data_root / name, ignore_errors=True)
        print(f"Erased local data for {name}.")
    cloud = Cloud(settings.cloud_url, settings.cloud_api_key, settings.collection)
    if cloud.reachable():
        cloud.reset()
        print("Emptied the cloud collection.")
    return start(settings, open_browser=open_browser, pin=pin)
