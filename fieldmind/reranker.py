"""On-device reranking of search candidates with a small cross-encoder.

Fusion of cosine and BM25 scores is fast and good at recall. A cross-encoder
reads the question and each candidate together and is better at precision, at
the cost of a few tens of milliseconds for twenty candidates on a CPU. It is
used for answers, and for search when the person asks for it.

The model (about 23 MB, quantised ONNX) is fetched once, like the other models,
and then loaded from disk.
"""

from __future__ import annotations

import math
import threading
import time
from pathlib import Path

import numpy as np

REPO = "Xenova/ms-marco-MiniLM-L-6-v2"
FILES = ("onnx/model_quantized.onnx", "tokenizer.json", "config.json")
MAX_TOKENS = 256


class Reranker:
    """``available`` is False when the model could not be loaded; search then
    keeps the fused order."""

    model_name = REPO

    def __init__(self, models_dir: Path):
        self.available = False
        self.error: str | None = None
        self._lock = threading.Lock()
        started = time.perf_counter()
        try:
            paths = self._files(models_dir / "reranker")
            import onnxruntime as ort
            from tokenizers import Tokenizer

            self._tokenizer = Tokenizer.from_file(str(paths["tokenizer.json"]))
            self._tokenizer.enable_truncation(max_length=MAX_TOKENS)
            self._tokenizer.enable_padding()
            self._session = ort.InferenceSession(str(paths["onnx/model_quantized.onnx"]),
                                                 providers=["CPUExecutionProvider"])
            self._inputs = {i.name for i in self._session.get_inputs()}
            self.available = True
        except Exception as error:  # no model and no network: degrade, do not fail the device
            self.error = str(error)
        self.load_seconds = round(time.perf_counter() - started, 2)

    @staticmethod
    def _files(target: Path) -> dict[str, Path]:
        paths = {name: target / name for name in FILES}
        if all(p.exists() for p in paths.values()):
            return paths
        from huggingface_hub import hf_hub_download

        return {name: Path(hf_hub_download(REPO, name, local_dir=str(target))) for name in FILES}

    def score(self, query: str, texts: list[str]) -> list[float]:
        """Relevance of each text to the query, 0..1 (a sigmoid over the model's logit)."""
        if not self.available or not texts:
            return [0.0] * len(texts)
        encodings = self._tokenizer.encode_batch([(query, text) for text in texts])
        feeds = {"input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
                 "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64)}
        if "token_type_ids" in self._inputs:
            feeds["token_type_ids"] = np.array([e.type_ids for e in encodings], dtype=np.int64)
        with self._lock:
            logits = self._session.run(None, feeds)[0]
        return [1.0 / (1.0 + math.exp(-float(row[0]))) for row in logits]
