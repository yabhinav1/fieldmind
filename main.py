"""Start one FieldMind device from a .env file, on the port the host assigns.

For hosting panels and platforms that run "python main.py" (Pterodactyl's
Python egg, Railway, Render and the like). The device's settings come from .env
next to this file (copy .env.example and fill in the device name and the cloud
URL and key); the port comes from SERVER_PORT or PORT, falling back to 8001.
Packages installed by the host into ./.local are picked up; if none are there,
they are installed on the first start. Data and models stay in ./data and
./models, which a git pull never touches.
"""

import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

# Packages the egg installed with "pip install --prefix .local" live here.
for path in glob.glob(os.path.join(HERE, ".local", "lib", "python3*", "site-packages")):
    if path not in sys.path:
        sys.path.insert(0, path)


def deps_ready() -> bool:
    try:
        import fastapi  # noqa: F401
        import fastembed  # noqa: F401
        import qdrant_edge  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        return False
    return True


if not deps_ready():
    print("Installing FieldMind's dependencies into ./.local (first start only, a few minutes)...", flush=True)
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--prefix", ".local", "-r", "requirements.txt"])
    for path in glob.glob(os.path.join(HERE, ".local", "lib", "python3*", "site-packages")):
        if path not in sys.path:
            sys.path.insert(0, path)
    if not deps_ready():
        sys.exit("The packages were installed but cannot be imported. Check the Python version (3.11 to 3.13).")

os.environ.setdefault("FIELDMIND_HOST", "0.0.0.0")
os.environ["FIELDMIND_PORT"] = os.environ.get("SERVER_PORT") or os.environ.get("PORT") or os.environ.get("FIELDMIND_PORT", "8001")
os.environ.setdefault("FIELDMIND_DATA", os.path.join(HERE, "data"))
os.environ.setdefault("FIELDMIND_MODELS", os.path.join(HERE, "models"))
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

device = "edge-a"
if not os.path.exists(os.path.join(HERE, ".env")):
    print("\n  No .env file yet. Copy .env.example to .env and fill in FIELDMIND_DEVICE, FIELDMIND_CLOUD_URL and\n"
          "  FIELDMIND_CLOUD_API_KEY, then restart. Starting with defaults for now (no cloud).\n", flush=True)
else:
    for line in open(os.path.join(HERE, ".env"), encoding="utf-8"):
        if line.startswith("FIELDMIND_DEVICE="):
            device = line.split("=", 1)[1].strip()
print(f"FieldMind {device} starting on port {os.environ['FIELDMIND_PORT']}", flush=True)
print("The first start downloads the models. Open the device's address and enter the PIN, or the setup code printed below.",
      flush=True)

from fieldmind.__main__ import main  # noqa: E402

sys.exit(main(["serve"]))
