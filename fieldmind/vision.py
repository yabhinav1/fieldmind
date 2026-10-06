"""On-device image understanding: CLIP ViT-B/32 through ONNX Runtime.

The vision side turns a photo into a 512-dimensional vector; the text side turns
a question into a vector in the same space, so "show me the corroded flange"
finds the photo of it. Both halves are fetched once (about 590 MB together) and
then loaded from disk. Loading takes a few seconds, so it happens in the
background after the device starts; the first photo waits for it if needed.
"""

from __future__ import annotations

import io
import os
import threading
import time
from pathlib import Path

VISION_MODEL = "Qdrant/clip-ViT-B-32-vision"
TEXT_MODEL = "Qdrant/clip-ViT-B-32-text"
IMAGE_DIM = 512
THUMBNAIL_PX = 768
JPEG_QUALITY = 82

os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def prepare(data: bytes) -> tuple[bytes, int, int]:
    """Re-encode a photo as a plain JPEG thumbnail: rotated the right way up, no
    EXIF (which carries GPS position, time and camera serial), at most
    ``THUMBNAIL_PX`` on the long side. Returns the bytes and the final size."""
    from PIL import Image, ImageOps

    image = Image.open(io.BytesIO(data))
    image = ImageOps.exif_transpose(image).convert("RGB")
    image.thumbnail((THUMBNAIL_PX, THUMBNAIL_PX))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue(), image.width, image.height


class ImageEmbedder:
    dim = IMAGE_DIM

    def __init__(self, models_dir: Path, background: bool = True):
        self.models_dir = models_dir
        self.available = False
        self.error: str | None = None
        self.load_seconds = 0.0
        self._lock = threading.Lock()
        self._vision = self._text = None
        self._thread = threading.Thread(target=self._load, name="fieldmind-vision", daemon=True)
        if background:
            self._thread.start()
        else:
            self._load()

    def _load(self) -> None:
        started = time.perf_counter()
        try:
            from fastembed import ImageEmbedding, TextEmbedding
            from loguru import logger

            logger.disable("fastembed")
            cache = str(self.models_dir)
            try:
                vision = ImageEmbedding(VISION_MODEL, cache_dir=cache, local_files_only=True)
                text = TextEmbedding(TEXT_MODEL, cache_dir=cache, local_files_only=True)
            except Exception:  # first run only: fetch the models, then every later start is offline
                vision = ImageEmbedding(VISION_MODEL, cache_dir=cache)
                text = TextEmbedding(TEXT_MODEL, cache_dir=cache)
            self._vision, self._text = vision, text
            self.available = True
        except Exception as error:  # no model and no network: photos are off, the device still runs
            self.error = str(error)
        self.load_seconds = round(time.perf_counter() - started, 2)

    def ready(self, wait: float | None = None) -> bool:
        """Whether the models are loaded, optionally waiting up to ``wait`` seconds for them."""
        if self._thread.is_alive() and wait:
            self._thread.join(wait)
        return self.available

    def image(self, data: bytes) -> list[float]:
        from PIL import Image

        with self._lock:
            return next(iter(self._vision.embed([Image.open(io.BytesIO(data))]))).tolist()

    def text(self, query: str) -> list[float]:
        with self._lock:
            return next(iter(self._text.embed([query]))).tolist()
