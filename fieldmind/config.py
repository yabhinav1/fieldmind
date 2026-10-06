"""Runtime settings for one edge device."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:  # settings may live in a .env file next to the project; real environment variables win
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

DENSE = "dense"
SPARSE = "bm25"
DENSE_MODEL = "BAAI/bge-small-en-v1.5"
DENSE_DIM = 384


def _env(name: str, default: str) -> str:
    return os.environ.get(f"FIELDMIND_{name}", default)


@dataclass
class Settings:
    device_id: str = field(default_factory=lambda: _env("DEVICE", "edge-a"))
    site: str = field(default_factory=lambda: _env("SITE", "plant-1"))
    author: str = field(default_factory=lambda: _env("AUTHOR", "technician"))
    host: str = field(default_factory=lambda: _env("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("PORT", "8001")))

    data_root: Path = field(default_factory=lambda: Path(_env("DATA", str(ROOT / "data"))))
    models_dir: Path = field(default_factory=lambda: Path(_env("MODELS", str(ROOT / "models"))))

    cloud_url: str = field(default_factory=lambda: _env("CLOUD_URL", "http://localhost:6333"))
    cloud_api_key: str | None = field(default_factory=lambda: os.environ.get("FIELDMIND_CLOUD_API_KEY"))
    collection: str = field(default_factory=lambda: _env("COLLECTION", "fleet_memory"))

    # Seconds between background sync attempts, and how many queued changes one
    # attempt may send. The cap models a constrained uplink: when the link comes
    # back, high-priority memories go first.
    sync_interval: float = field(default_factory=lambda: float(_env("SYNC_INTERVAL", "4")))
    sync_batch: int = field(default_factory=lambda: int(_env("SYNC_BATCH", "25")))
    # A queued change that keeps failing for its own reasons (not because the link
    # is down) is parked after this many attempts so the rest of the queue drains.
    sync_max_attempts: int = field(default_factory=lambda: int(_env("SYNC_MAX_ATTEMPTS", "5")))
    # When a device is this many memories behind, it restores the replica from one
    # cloud snapshot instead of fetching memories one by one.
    snapshot_min_points: int = field(default_factory=lambda: int(_env("SNAPSHOT_MIN_POINTS", "200")))

    # Pre-set device PIN. Without it, the first person to open the dashboard on the
    # device itself chooses one.
    pin: str | None = field(default_factory=lambda: os.environ.get("FIELDMIND_PIN"))
    # Seal private and masked note text and the activity log on disk with a key
    # kept in the device's data directory (or FIELDMIND_KEY_FILE). 0 turns it off.
    encrypt: bool = field(default_factory=lambda: _env("ENCRYPT", "1") not in ("0", "false", "no", ""))
    key_file: Path | None = field(default_factory=lambda: Path(os.environ["FIELDMIND_KEY_FILE"])
                                  if os.environ.get("FIELDMIND_KEY_FILE") else None)
    # /metrics holds only counters and timings, no note text. Set a token to require
    # "Authorization: Bearer <token>" on it anyway.
    metrics_token: str | None = field(default_factory=lambda: os.environ.get("FIELDMIND_METRICS_TOKEN"))

    # Rerank search candidates with a small cross-encoder (about 23 MB). Answers
    # always use it when it is loaded; search uses it on request.
    rerank: bool = field(default_factory=lambda: _env("RERANK", "1") not in ("0", "false", "no", ""))

    # On-device language model for phrasing answers (scripts/llm.ps1 runs it).
    # Leave the model empty to always compose answers from the notes themselves.
    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_URL", "http://127.0.0.1:11435"))
    ollama_model: str = field(default_factory=lambda: _env("OLLAMA_MODEL", "llama3.2:3b"))

    @property
    def data_dir(self) -> Path:
        return self.data_root / self.device_id

    @property
    def local_shard_dir(self) -> Path:
        return self.data_dir / "shard-local"

    @property
    def replica_shard_dir(self) -> Path:
        return self.data_dir / "shard-replica"

    @property
    def journal_path(self) -> Path:
        return self.data_dir / "journal.sqlite"

    @property
    def vault_key_file(self) -> Path | None:
        if not self.encrypt:
            return None
        return self.key_file or self.data_dir / ".key"
