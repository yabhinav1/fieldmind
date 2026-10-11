"""On-device speech to text, so a technician can dictate a note with no vendor
service involved and no network.

Whisper runs on the CPU through CTranslate2: ``base`` (int8, about 140 MB) for
every phrase, which also detects the language, and ``small`` (about 460 MB) to
re-read phrases in languages other than English, which ``base`` garbles. The
browser records phrase by phrase and uploads each one; the device transcribes a
phrase in well under a second and the words land in the note box. The models
are fetched once and loaded in the background after start.
"""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path

MAX_SECONDS = 60


class Transcriber:
    """Two tiers: a fast model for every phrase, which also detects the language,
    and a larger one that re-reads phrases in other languages, because the fast
    model's Hindi, Tamil or Arabic comes out garbled. The larger model loads in
    the background after the fast one; until it is ready, the fast result is
    returned and marked provisional."""

    def __init__(self, models_dir: Path, model: str = "base", multilingual_model: str | None = "small",
                 background: bool = True):
        self.models_dir = models_dir
        self.model_name = model
        self.multilingual_name = multilingual_model if multilingual_model and multilingual_model != model else None
        self.available = False
        self.multilingual_available = False
        self.error: str | None = None
        self.load_seconds = 0.0
        self._model = None
        self._multilingual = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._load, name="fieldmind-speech", daemon=True)
        if background:
            self._thread.start()
        else:
            self._load()

    def _open(self, name: str):
        from faster_whisper import WhisperModel

        root = str(self.models_dir / "whisper")
        try:
            return WhisperModel(name, device="cpu", compute_type="int8", download_root=root, local_files_only=True)
        except Exception:  # first run only: fetch the model, then every later start is offline
            return WhisperModel(name, device="cpu", compute_type="int8", download_root=root)

    def _load(self) -> None:
        started = time.perf_counter()
        try:
            self._model = self._open(self.model_name)
            self.available = True
        except Exception as error:  # no model and no network, or the package is missing: dictation is off
            self.error = str(error)
        self.load_seconds = round(time.perf_counter() - started, 2)
        if self.available and self.multilingual_name:
            try:
                self._multilingual = self._open(self.multilingual_name)
                self.multilingual_available = True
            except Exception as error:
                self.multilingual_error = str(error)

    def ready(self, wait: float | None = None) -> bool:
        if self._thread.is_alive() and wait and not self.available:
            # Wait for the fast model only; the larger one keeps loading behind it.
            deadline = time.time() + wait
            while self._thread.is_alive() and not self.available and self.error is None and time.time() < deadline:
                time.sleep(0.2)
        return self.available

    def transcribe(self, audio: bytes, language: str | None = None) -> dict:
        """Words from a short recording (any format the browser produces: webm, ogg, mp4, wav).

        With no ``language`` the model detects it, so a technician can speak Hindi,
        Tamil, Spanish or English and the words come back in that language."""
        if not self.ready(wait=60):
            raise RuntimeError(f"The speech model is not loaded: {self.error or 'still loading'}")
        started = time.perf_counter()
        data = io.BytesIO(audio)
        with self._lock:
            # Greedy, no temperature fallback: a phrase must come back fast, and the
            # fallback retries are what make odd audio take thirty seconds.
            segments, info = self._model.transcribe(data, language=language or None, task="transcribe", beam_size=1,
                                                    temperature=0.0, max_new_tokens=96, vad_filter=True,
                                                    condition_on_previous_text=False)
            text = " ".join(s.text.strip() for s in segments).strip()
        if info.duration and info.duration > MAX_SECONDS:
            raise ValueError(f"Keep a dictation under {MAX_SECONDS} seconds.")
        model, provisional = self.model_name, False
        if text and info.language != "en" and self.multilingual_name:
            if self.multilingual_available:
                data.seek(0)
                with self._lock:
                    # A small beam with a repetition penalty keeps the larger model from
                    # looping on a syllable, which greedy decoding does in other scripts.
                    segments, _ = self._multilingual.transcribe(data, language=info.language, task="transcribe",
                                                                beam_size=3, temperature=0.0, repetition_penalty=1.2,
                                                                no_repeat_ngram_size=3, max_new_tokens=96, vad_filter=True,
                                                                condition_on_previous_text=False)
                    better = " ".join(s.text.strip() for s in segments).strip()
                if better:
                    text, model = better, self.multilingual_name
            else:
                provisional = True  # the larger model is still loading; this is the fast model's reading
        return {"text": text, "seconds": round(info.duration or 0, 1), "took_ms": round((time.perf_counter() - started) * 1000),
                "language": info.language, "language_probability": round(info.language_probability or 0, 2),
                "model": model, "provisional": provisional}
