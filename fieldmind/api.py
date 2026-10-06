"""HTTP API and dashboard for one edge device."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, metrics
from .auth import Auth, Locked
from .config import DENSE_MODEL, Settings
from .demo import DEFAULT_NOTES, DEVICE_NOTES, seed_cloud
from .peers import HEADER as PEER_HEADER
from .runtime import Device, build
from .service import NotFound

WEB = Path(__file__).parent / "web"


class CaptureBody(BaseModel):
    text: str
    kind: str = "observation"
    asset: str | None = None
    tags: list[str] = Field(default_factory=list)
    scope: str | None = None
    allow_duplicate: bool = False
    supersede: bool = True


class PreviewBody(BaseModel):
    text: str
    asset: str | None = None
    scope: str | None = None


class EditBody(BaseModel):
    text: str | None = None
    kind: str | None = None
    asset: str | None = None
    tags: list[str] | None = None
    scope: str | None = None


class SearchBody(BaseModel):
    query: str
    mode: str = "hybrid"
    limit: int = 8
    scope: str | None = None
    kind: str | None = None
    asset: str | None = None
    source: str | None = None
    include_superseded: bool = False
    rerank: bool = False


class AskBody(BaseModel):
    question: str


class ToggleBody(BaseModel):
    value: bool


class PinBody(BaseModel):
    pin: str


class ChangePinBody(BaseModel):
    current: str
    new: str


class ResolveBody(BaseModel):
    choice: str
    text: str | None = None


class PeerIdsBody(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=256)


def create_app(settings: Settings | None = None, device: Device | None = None) -> FastAPI:
    settings = settings or (device.settings if device else Settings())
    state: dict = {}
    cookie = f"fieldmind_{settings.device_id}"  # cookies ignore ports, so name it per device

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state["device"] = device or build(settings)
        dev = state["device"]
        state["auth"] = Auth(dev.journal, settings.pin)
        dev.journal.log("system", f"Device {settings.device_id} started. Embedding model loaded from {dev.embedder.loaded_from} "
                                  f"in {dev.embedder.load_seconds}s.")
        dev.sync.start()
        dev.service.llm.warm()
        yield
        dev.close()

    app = FastAPI(title="FieldMind", version=__version__, lifespan=lifespan)

    def dev() -> Device:
        return state["device"]

    def auth() -> Auth:
        return state["auth"]

    # -- device lock ------------------------------------------------------

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/peer/"):
            # Other devices, not people: one shared fleet token instead of the PIN.
            token = settings.peer_token
            if not token or request.headers.get(PEER_HEADER) != token:
                return JSONResponse({"detail": "Peer exchange is off or the fleet token is wrong."}, status_code=403)
        elif path.startswith("/api/") and not path.startswith("/api/auth/"):
            if not auth().valid(request.cookies.get(cookie)):
                return JSONResponse({"detail": "This device is locked."}, status_code=401)
        return await call_next(request)

    def open_session(response: Response) -> dict:
        response.set_cookie(cookie, auth().start_session(), httponly=True, samesite="strict", max_age=12 * 3600)
        return {"authenticated": True}

    def on_device(request: Request) -> bool:
        return request.client is not None and request.client.host in ("127.0.0.1", "::1")

    @app.get("/api/auth/state")
    def auth_state(request: Request):
        return {
            "device": settings.device_id,
            "configured": auth().configured,
            "authenticated": auth().valid(request.cookies.get(cookie)),
            "can_set_up": on_device(request),
            "locked_for": auth().locked_for(),
        }

    @app.post("/api/auth/setup")
    def auth_setup(body: PinBody, request: Request, response: Response):
        if auth().configured:
            raise HTTPException(409, "This device already has a PIN.")
        if not on_device(request):
            raise HTTPException(403, "The first PIN must be set on the device itself.")
        try:
            auth().set_pin(body.pin)
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        dev().journal.log("security", "A device PIN was set.")
        return open_session(response)

    @app.post("/api/auth/login")
    def auth_login(body: PinBody, response: Response):
        try:
            auth().login(body.pin)
        except Locked as error:
            raise HTTPException(429, str(error)) from None
        except PermissionError as error:
            raise HTTPException(401, str(error)) from None
        return open_session(response)

    @app.post("/api/auth/logout")
    def auth_logout(request: Request, response: Response):
        auth().logout(request.cookies.get(cookie))
        response.delete_cookie(cookie)
        return {"authenticated": False}

    @app.post("/api/auth/pin")
    def auth_change_pin(body: ChangePinBody, request: Request):
        """Change the PIN. Needs an open session and the current PIN; other sessions are closed."""
        if not auth().valid(request.cookies.get(cookie)):
            raise HTTPException(401, "This device is locked.")
        try:
            auth().login(body.current)
            auth().set_pin(body.new)
        except Locked as error:
            raise HTTPException(429, str(error)) from None
        except PermissionError:
            raise HTTPException(401, "The current PIN is wrong.") from None
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        auth().logout_all(except_token=request.cookies.get(cookie))
        dev().journal.log("security", "The device PIN was changed.")
        return {"changed": True}

    # -- probes (no PIN, no note text) --------------------------------------

    @app.get("/healthz")
    def healthz():
        d = state.get("device")
        if d is None or d.closed:
            return JSONResponse({"status": "starting"}, status_code=503)
        return {"status": "ok", "device": settings.device_id, "version": __version__}

    @app.get("/metrics")
    def prometheus(request: Request):
        if settings.metrics_token and request.headers.get("authorization") != f"Bearer {settings.metrics_token}":
            raise HTTPException(401, "A metrics token is required.")
        return PlainTextResponse(metrics.render(dev()), media_type="text/plain; version=0.0.4; charset=utf-8")

    # -- status -----------------------------------------------------------

    @app.get("/api/status")
    def status():
        d = dev()
        local, replica = d.service.local.info(), d.service.replica.info()
        return {
            "device": {"id": settings.device_id, "site": settings.site, "author": settings.author,
                       "version": __version__},
            "link": d.sync.link(),
            "memory": d.service.stats(),
            "outbox": d.journal.counts(),
            "open_conflicts": len(d.journal.conflicts("open")),
            "shards": {"local": local, "replica": replica},
            "engine": {"vector_store": "Qdrant Edge (in-process)", "dense_model": DENSE_MODEL,
                       "sparse_model": "BM25 (Qdrant Edge)", "dimensions": d.embedder.dim,
                       "answer_model": settings.ollama_model if d.service.llm.available() else None,
                       "name_model": "BERT NER (ONNX)" if d.names.available else None,
                       "reranker": "MiniLM cross-encoder (ONNX)" if d.service.reranker else None,
                       "learned_examples": d.service.policy.learned_count,
                       "sealed": bool(d.vault and d.vault.active)},
            "sync": {"last_sync_at": d.journal.get("last_sync_at"), "totals": d.journal.totals(), "interval": settings.sync_interval,
                     "batch": settings.sync_batch},
            "now": time.time(),
        }

    @app.post("/api/link/offline")
    def set_offline(body: ToggleBody):
        dev().sync.set_forced_offline(body.value)
        return dev().sync.link()

    @app.post("/api/sync/auto")
    def set_auto(body: ToggleBody):
        dev().sync.set_auto_sync(body.value)
        return dev().sync.link()

    # -- memory -----------------------------------------------------------

    @app.post("/api/preview")
    def preview(body: PreviewBody):
        if not body.text.strip():
            return {"decision": None, "asset": None, "related": []}
        return dev().service.preview(body.text, body.asset, body.scope)

    @app.post("/api/memories")
    def capture(body: CaptureBody):
        try:
            return dev().service.capture(body.text, body.kind, body.asset, body.tags, body.scope,
                                         allow_duplicate=body.allow_duplicate, supersede=body.supersede)
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @app.get("/api/memories")
    def memories(scope: str | None = None, sync_state: str | None = None, kind: str | None = None,
                 asset: str | None = None, source: str | None = None, status: str | None = None,
                 text: str | None = None):
        items = dev().service.list(scope, sync_state, kind, asset, source, status, text)
        return {"items": items, "total": len(items)}

    @app.get("/api/memories/{memory_id}")
    def memory(memory_id: str):
        try:
            return {"memory": dev().service.get(memory_id), "history": dev().service.history(memory_id),
                    "queued": dev().journal.open_op(memory_id)}
        except NotFound:
            raise HTTPException(404, "No such memory on this device.") from None

    @app.patch("/api/memories/{memory_id}")
    def edit(memory_id: str, body: EditBody):
        try:
            return dev().service.edit(memory_id, body.text, body.kind, body.asset, body.tags, body.scope)
        except NotFound:
            raise HTTPException(404, "No such memory on this device.") from None

    @app.delete("/api/memories/{memory_id}")
    def delete(memory_id: str):
        try:
            dev().service.delete(memory_id)
        except NotFound:
            raise HTTPException(404, "No such memory on this device.") from None
        return {"deleted": memory_id}

    @app.post("/api/search")
    def search(body: SearchBody):
        if not body.query.strip():
            return {"query": "", "results": [], "timing_ms": {}, "candidates": 0}
        return dev().service.search(body.query, body.mode, body.limit, body.scope, body.kind, body.asset,
                                    body.source, body.include_superseded, body.rerank)

    @app.post("/api/ask")
    def ask(body: AskBody):
        return dev().service.ask(body.question)

    # -- sync -------------------------------------------------------------

    @app.get("/api/sync")
    def sync_state():
        d = dev()
        outbox = []
        for op in d.journal.outbox():
            record = d.service.find(op["memory_id"])
            text = record["payload"].get("text") if record else (op["base_payload"] or {}).get("text", "")
            outbox.append({**op, "text": text, "base_payload": None})
        return {"link": d.sync.link(), "outbox": outbox, "counts": d.journal.counts(),
                "conflicts": d.journal.conflicts("open"), "runs": d.journal.runs(12), "totals": d.journal.totals()}

    @app.post("/api/sync/run")
    def sync_run():
        return dev().sync.run_once("manual")

    @app.post("/api/sync/retry")
    def sync_retry():
        return dev().sync.retry_failed()

    @app.post("/api/conflicts/{conflict_id}/resolve")
    def resolve(conflict_id: int, body: ResolveBody):
        try:
            return dev().sync.resolve(conflict_id, body.choice, body.text)
        except KeyError:
            raise HTTPException(404, "No open conflict with that id.") from None
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @app.post("/api/replica/rebuild")
    def rebuild():
        return dev().sync.rebuild_replica()

    @app.get("/api/events")
    def events(after: int = 0, limit: int = 200):
        return {"events": dev().journal.events(after, limit)}

    # -- peers (other devices on the local network) -------------------------

    @app.get("/api/peer/manifest")
    def peer_manifest():
        return dev().peers.manifest()

    @app.post("/api/peer/memories")
    def peer_memories(body: PeerIdsBody):
        return dev().peers.records(body.ids)

    @app.post("/api/peers/exchange")
    def peers_exchange():
        d = dev()
        if not d.peers.enabled:
            raise HTTPException(409, "No peers are configured on this device (FIELDMIND_PEERS and FIELDMIND_PEER_TOKEN).")
        return {"peers": d.peers.exchange()}

    # -- cloud ------------------------------------------------------------

    @app.get("/api/cloud")
    def cloud():
        d = dev()
        if not d.sync.link()["online"]:
            return {"online": False, "items": [], "total": 0}
        try:
            d.cloud.ensure()
            rows = d.cloud.browse()
        except Exception as error:
            return {"online": False, "items": [], "total": 0, "error": str(error)}
        items = [{"id": r["id"], **r["payload"]} for r in rows]
        live = [i for i in items if i.get("status") != "deleted"]
        return {"online": True, "items": live, "total": len(live),
                "tombstones": len(items) - len(live)}

    # -- demo -------------------------------------------------------------

    @app.post("/api/demo/seed")
    def seed():
        d = dev()
        created = 0
        for text in DEVICE_NOTES.get(settings.device_id, DEFAULT_NOTES):
            created += bool(d.service.capture(text).get("created"))
        return {"created": created}

    @app.post("/api/demo/seed-cloud")
    def seed_cloud_knowledge():
        d = dev()
        if not d.sync.link()["online"]:
            raise HTTPException(409, "The cloud is not reachable from this device.")
        count = seed_cloud(d.cloud, d.embedder)
        d.journal.log("system", f"Headquarters published {count} reference documents to the cloud.")
        return {"published": count}

    # -- dashboard --------------------------------------------------------

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-store"})

    # The service worker and manifest must sit at the root for the whole dashboard
    # to be installable and to keep its shell available offline.
    @app.get("/sw.js")
    def service_worker():
        return FileResponse(WEB / "sw.js", media_type="application/javascript",
                            headers={"Cache-Control": "no-store", "Service-Worker-Allowed": "/"})

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(WEB / "manifest.webmanifest", media_type="application/manifest+json")

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app
