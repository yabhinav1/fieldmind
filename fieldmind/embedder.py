"""On-device embeddings: a small ONNX dense model plus Qdrant Edge's built-in BM25.

Nothing here touches the network once the model files are in ``models_dir``.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from qdrant_edge import Bm25, SparseVector

from .config import DENSE_DIM, DENSE_MODEL

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


class Embedder:
    dim = DENSE_DIM
    model_name = DENSE_MODEL

    def __init__(self, models_dir: Path):
        from fastembed import TextEmbedding
        from loguru import logger

        logger.disable("fastembed")

        models_dir.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        try:
            self._dense = TextEmbedding(DENSE_MODEL, cache_dir=str(models_dir), local_files_only=True)
            self.loaded_from = "disk"
        except Exception:
            # First run only: fetch the model once, then every later start is offline.
            self._dense = TextEmbedding(DENSE_MODEL, cache_dir=str(models_dir))
            self.loaded_from = "download"
        self._bm25 = Bm25()
        self._lock = threading.Lock()
        self.load_seconds = round(time.perf_counter() - started, 2)

    def dense(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            return [v.tolist() for v in self._dense.embed(texts)]

    def dense_query(self, text: str) -> list[float]:
        with self._lock:
            return next(iter(self._dense.query_embed(text))).tolist()

    def sparse(self, text: str) -> SparseVector:
        return self._bm25.embed_document(text)

    def sparse_query(self, text: str) -> SparseVector:
        return self._bm25.embed_query(text)

    def document(self, text: str) -> tuple[list[float], SparseVector]:
        return self.dense([text])[0], self.sparse(text)
