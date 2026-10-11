"""Faces in photos, found and hidden on the device before a thumbnail leaves it.

A 1.3 MB detector (UltraFace RFB-320, ONNX) runs on the CPU in about 15 ms. The
photo the technician keeps is untouched; the copy that travels to the fleet has
every face pixelated, and the vector the fleet searches by is computed from that
copy, so neither the picture nor its embedding can identify a person.
"""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path

import numpy as np

REPO = "onnxmodelzoo/version-RFB-320"
FILE = "version-RFB-320.onnx"
INPUT_W, INPUT_H = 320, 240
MIN_CONFIDENCE = 0.7
NMS_IOU = 0.3
# A box is widened by this fraction on each side so hair, ears and chin go too.
MARGIN = 0.25


class FaceFinder:
    def __init__(self, models_dir: Path):
        self.available = False
        self.error: str | None = None
        self._lock = threading.Lock()
        started = time.perf_counter()
        try:
            path = models_dir / "face" / FILE
            if not path.exists():
                from huggingface_hub import hf_hub_download

                path = Path(hf_hub_download(REPO, FILE, local_dir=str(models_dir / "face")))
            import onnxruntime as ort

            options = ort.SessionOptions()
            options.log_severity_level = 3  # the model lists its weights as inputs; the warning is harmless
            self._session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
            self.available = True
        except Exception as error:  # no model and no network: photos still work, faces are not hidden
            self.error = str(error)
        self.load_seconds = round(time.perf_counter() - started, 2)

    def find(self, data: bytes) -> list[tuple[int, int, int, int]]:
        """Face boxes as (left, top, right, bottom) in the image's own pixels."""
        if not self.available:
            return []
        from PIL import Image

        image = Image.open(io.BytesIO(data)).convert("RGB")
        width, height = image.size
        small = np.asarray(image.resize((INPUT_W, INPUT_H)), dtype=np.float32)
        tensor = ((small - 127.0) / 128.0).transpose(2, 0, 1)[None]
        with self._lock:
            scores, boxes = self._session.run(None, {"input": tensor})
        confidence = scores[0][:, 1]
        keep = confidence > MIN_CONFIDENCE
        candidates, confidence = boxes[0][keep], confidence[keep]
        out = []
        for index in _nms(candidates, confidence):
            x1, y1, x2, y2 = candidates[index]
            out.append((int(max(0, x1 * width)), int(max(0, y1 * height)),
                        int(min(width, x2 * width)), int(min(height, y2 * height))))
        return out


def hide(data: bytes, boxes: list[tuple[int, int, int, int]]) -> bytes:
    """The same JPEG with every face pixelated beyond recognition."""
    from PIL import Image

    image = Image.open(io.BytesIO(data)).convert("RGB")
    width, height = image.size
    for left, top, right, bottom in boxes:
        dx, dy = int((right - left) * MARGIN), int((bottom - top) * MARGIN)
        box = (max(0, left - dx), max(0, top - dy), min(width, right + dx), min(height, bottom + dy))
        region = image.crop(box)
        cells = max(2, min(region.width, region.height) // 12)
        tiny = region.resize((max(1, region.width // cells), max(1, region.height // cells)), Image.BILINEAR)
        image.paste(tiny.resize(region.size, Image.NEAREST), box)
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=82, optimize=True)
    return out.getvalue()


def _nms(boxes: np.ndarray, scores: np.ndarray) -> list[int]:
    order = scores.argsort()[::-1]
    kept: list[int] = []
    while len(order):
        first = order[0]
        kept.append(int(first))
        if len(order) == 1:
            break
        rest = order[1:]
        x1 = np.maximum(boxes[first, 0], boxes[rest, 0])
        y1 = np.maximum(boxes[first, 1], boxes[rest, 1])
        x2 = np.minimum(boxes[first, 2], boxes[rest, 2])
        y2 = np.minimum(boxes[first, 3], boxes[rest, 3])
        inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
        area = lambda b: (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])  # noqa: E731
        iou = inter / (area(boxes[[first]]) + area(boxes[rest]) - inter + 1e-9)
        order = rest[iou <= NMS_IOU]
    return kept
