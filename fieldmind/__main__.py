"""Command line entry point.

    python -m fieldmind start [--pin 2468] [--only edge-b] [--lan] [--no-browser]
    python -m fieldmind stop
    python -m fieldmind reset-all [--pin 2468]
    python -m fieldmind serve --device edge-a --port 8001
    python -m fieldmind seed-cloud
    python -m fieldmind reset --device edge-a
    python -m fieldmind doctor
"""

from __future__ import annotations

import argparse
import shutil
import sys

from .config import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fieldmind", description="Offline-first memory for field devices.")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run one edge device with its dashboard")
    serve.add_argument("--device", help="device name, for example edge-a")
    serve.add_argument("--port", type=int)
    serve.add_argument("--host")
    serve.add_argument("--site")
    serve.add_argument("--author")
    serve.add_argument("--cloud-url")
    serve.add_argument("--pin", help="pre-set the device PIN")

    seed = sub.add_parser("seed-cloud", help="publish reference knowledge to the cloud")
    seed.add_argument("--cloud-url")

    reset = sub.add_parser("reset", help="erase a device's local data")
    reset.add_argument("--device", required=True)
    reset.add_argument("--cloud", action="store_true", help="also empty the cloud collection")

    sub.add_parser("doctor", help="check that this machine is ready to run and to demo offline")

    start = sub.add_parser("start", help="start the cloud and the demo devices, then open the dashboards")
    start.add_argument("--only", help="start just this device, for example edge-b")
    start.add_argument("--cloud-url")
    start.add_argument("--pin", help="pre-set the device PIN")
    start.add_argument("--lan", action="store_true", help="let phones and other computers open the dashboards")
    start.add_argument("--no-browser", action="store_true")

    sub.add_parser("stop", help="stop the devices started with 'start'")

    reset_all = sub.add_parser("reset-all", help="stop, erase both devices and the cloud collection, start again")
    reset_all.add_argument("--pin")
    reset_all.add_argument("--no-browser", action="store_true")

    args = parser.parse_args(argv)
    settings = Settings()
    for name in ("device", "port", "host", "site", "author", "cloud_url", "pin"):
        value = getattr(args, name, None)
        if value is not None:
            setattr(settings, "device_id" if name == "device" else name, value)

    if args.command == "doctor":
        from .doctor import run

        return run(settings)

    if args.command in ("start", "stop", "reset-all"):
        from . import launcher

        if args.command == "start":
            return launcher.start(settings, only=args.only, lan=args.lan, open_browser=not args.no_browser, pin=args.pin)
        if args.command == "stop":
            return launcher.stop(settings)
        return launcher.reset_all(settings, pin=args.pin, open_browser=not args.no_browser)

    if args.command == "seed-cloud":
        from .cloud import Cloud
        from .demo import seed_cloud
        from .embedder import Embedder

        cloud = Cloud(settings.cloud_url, settings.cloud_api_key, settings.collection)
        if not cloud.reachable():
            print(f"Cloud not reachable at {settings.cloud_url}. Start it with: docker compose up -d")
            return 1
        print(f"Published {seed_cloud(cloud, Embedder(settings.models_dir))} reference documents.")
        return 0

    if args.command == "reset":
        shutil.rmtree(settings.data_dir, ignore_errors=True)
        print(f"Erased local data for {settings.device_id}.")
        if args.cloud:
            from .cloud import Cloud

            cloud = Cloud(settings.cloud_url, settings.cloud_api_key, settings.collection)
            if cloud.reachable():
                cloud.reset()
                print("Emptied the cloud collection.")
            else:
                print("Cloud not reachable, left untouched.")
        return 0

    import uvicorn

    from .api import create_app

    print(f"FieldMind device {settings.device_id} -> http://{settings.host}:{settings.port}")
    if settings.pin:
        print(f"  Dashboard PIN: {settings.pin} (preset by FIELDMIND_PIN)")
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
