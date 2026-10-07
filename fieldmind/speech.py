"""On-device speech to text, so a technician can dictate a note with no vendor
service involved and no network.

Whisper (``base``, int8, about 140 MB) runs on the CPU through CTranslate2. The
browser records a few seconds of audio and uploads it; the device transcribes it
in well under a second and the words land in the note box. The model is fetched
once and loaded in the background after start; the first dictation waits for it.
"""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path

MAX_SECONDS = 60


class Transcriber:
    def __init__(self, models_dir: Path, model: str = "base", background: bool = True):
        self.models_dir = models_dir
        self.model_name = model
        self.available = False
        self.error: str | None = None
        self.load_seconds = 0.0
        self._model = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._load, name="fieldmind-speech", daemon=True)
        if background:
            self._thread.start()
        else:
            self._load()

    def _load(self) -> None:
        started = time.perf_counter()
        try:
            from faster_whisper import WhisperModel

            root = str(self.models_dir / "whisper")
            try:
                self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8", download_root=root,
                                           local_files_only=True)
            except Exception:  # first run only: fetch the model, then every later start is offline
                self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8", download_root=root)
            self.available = True
        except Exception as error:  # no model and no network, or the package is missing: dictation is off
            self.error = str(error)
        self.load_seconds = round(time.perf_counter() - started, 2)

    def ready(self, wait: float | None = None) -> bool:
        if self._thread.is_alive() and wait:
            self._thread.join(wait)
        return self.available

    def transcribe(self, audio: bytes, language: str | None = "en") -> dict:
        """Words from a short recording (any format the browser produces: webm, ogg, mp4, wav)."""
        if not self.ready(wait=60):
            raise RuntimeError(f"The speech model is not loaded: {self.error or 'still loading'}")
        started = time.perf_counter()
        with self._lock:
            segments, info = self._model.transcribe(io.BytesIO(audio), language=language or None, beam_size=1,
                                                    vad_filter=True, condition_on_previous_text=False)
            text = " ".join(s.text.strip() for s in segments).strip()
        if info.duration and info.duration > MAX_SECONDS:
            raise ValueError(f"Keep a dictation under {MAX_SECONDS} seconds.")
        return {"text": text, "seconds": round(info.duration or 0, 1), "took_ms": round((time.perf_counter() - started) * 1000),
                "language": info.language}
