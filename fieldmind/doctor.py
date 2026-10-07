"""Preflight check: is this machine ready to run, and to demo without internet?

    python -m fieldmind doctor
"""

from __future__ import annotations

import shutil
import sys

import httpx

from .config import DENSE_MODEL, Settings

OK, WARN, FAIL = "ok", "warn", "FAIL"


def _report(level: str, label: str, detail: str = "") -> str:
    print(f"  [{level:>4}] {label}{' - ' + detail if detail else ''}")
    return level


def _embedding_model(settings: Settings) -> bool:
    try:
        from fastembed import TextEmbedding
        from loguru import logger

        logger.disable("fastembed")
        TextEmbedding(DENSE_MODEL, cache_dir=str(settings.models_dir), local_files_only=True)
        return True
    except Exception:
        return False


def _name_model(settings: Settings) -> bool:
    from .ner import FILES

    return all((settings.models_dir / "ner" / name).exists() for name in FILES)


def _reranker_model(settings: Settings) -> bool:
    from .reranker import FILES

    return all((settings.models_dir / "reranker" / name).exists() for name in FILES)


def _vision_models(settings: Settings) -> bool:
    try:
        from fastembed import ImageEmbedding, TextEmbedding
        from loguru import logger

        from .vision import TEXT_MODEL, VISION_MODEL

        logger.disable("fastembed")
        ImageEmbedding(VISION_MODEL, cache_dir=str(settings.models_dir), local_files_only=True)
        TextEmbedding(TEXT_MODEL, cache_dir=str(settings.models_dir), local_files_only=True)
        return True
    except Exception:
        return False


def run(settings: Settings) -> int:
    results = []
    print("FieldMind preflight\n")

    print("This machine")
    version = sys.version_info
    results.append(_report(OK if version >= (3, 10) else FAIL, "Python", f"{version.major}.{version.minor}.{version.micro}"))
    try:
        import fastembed  # noqa: F401
        import onnxruntime  # noqa: F401
        import qdrant_edge  # noqa: F401

        results.append(_report(OK, "Qdrant Edge and model runtimes installed"))
    except ImportError as error:
        results.append(_report(FAIL, "Packages missing", f"{error.name}; run: pip install -r requirements.txt"))
        return 1

    settings.data_root.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(settings.data_root).free / 1e9
    devices_present = [d for d in ("edge-a", "edge-b") if (settings.data_root / d).exists()]
    needed = 0.5 * (2 - len(devices_present)) + 0.3
    results.append(_report(OK if free_gb >= needed else FAIL, "Free disk",
                           f"{free_gb:.1f} GB free, about {needed:.1f} GB needed (each new device reserves 0.43 GB)"))

    print("\nModels on disk (needed to run with no internet)")
    embedding, names = _embedding_model(settings), _name_model(settings)
    results.append(_report(OK if embedding else WARN, "Embedding model",
                           "" if embedding else "will download about 130 MB on first start"))
    results.append(_report(OK if names else WARN, "Name recognition model",
                           "" if names else "will download about 105 MB on first start"))
    reranker = _reranker_model(settings) or not settings.rerank
    results.append(_report(OK if reranker else WARN, "Reranking model",
                           "turned off" if not settings.rerank else "" if reranker else "will download about 23 MB on first start"))
    speech_dir = settings.models_dir / "whisper"
    speech = (not settings.speech) or (speech_dir.exists() and any(speech_dir.rglob("model.bin")))
    results.append(_report(OK if speech else WARN, "Speech model (Whisper)",
                           "turned off" if not settings.speech else "" if speech else "will download about 140 MB on first start"))
    vision = _vision_models(settings) or not settings.photos
    results.append(_report(OK if vision else WARN, "Photo models (CLIP)",
                           "turned off; FIELDMIND_PHOTOS=1 turns photos on" if not settings.photos
                           else "" if vision else "will download about 590 MB on first start"))

    print("\nCloud")
    from .cloud import Cloud

    cloud = Cloud(settings.cloud_url, settings.cloud_api_key, settings.collection)
    if cloud.reachable():
        try:
            cloud.ensure()
            results.append(_report(OK, "Qdrant Server", f"{settings.cloud_url}, {cloud.count()} memories in {settings.collection}"))
        except Exception as error:
            results.append(_report(FAIL, "Qdrant Server reachable but unusable", str(error).splitlines()[0]))
    else:
        results.append(_report(WARN, "Qdrant Server not reachable", f"{settings.cloud_url}; devices will run offline. "
                                                                   "Start Docker Desktop, then: docker compose up -d qdrant"))

    print("\nOn-device language model (optional)")
    from .llm import LocalModel

    model = LocalModel(settings.ollama_url, settings.ollama_model)
    if not settings.ollama_model:
        results.append(_report(OK, "Turned off", "answers are composed from notes"))
    elif model.available():
        results.append(_report(OK, settings.ollama_model, settings.ollama_url))
    else:
        results.append(_report(WARN, f"{settings.ollama_model} not running",
                               "answers will be composed from notes. Start it with: scripts\\llm.ps1"))

    print("\nDevices")
    for name, port in (("edge-a", 8001), ("edge-b", 8002)):
        try:
            state = httpx.get(f"http://127.0.0.1:{port}/api/auth/state", timeout=1.5).json()
            detail = "PIN set" if state.get("configured") else "no PIN yet, the first visitor chooses one"
            results.append(_report(OK, f"{name} running on port {port}", detail))
        except (httpx.HTTPError, ValueError):
            results.append(_report(WARN, f"{name} not running", "start with: scripts\\start.ps1"))

    print()
    if FAIL in results:
        print("Not ready. Fix the FAIL lines above.")
        return 1
    offline_ready = embedding and names and reranker and vision and speech
    print("Ready." + ("" if offline_ready else " Internet is needed once, to download the models.")
          + (" Some optional parts are off; see the warn lines." if WARN in results else ""))
    return 0
