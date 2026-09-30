"""Wires one device together: embedder, two shards, journal, policy, cloud and sync."""

from __future__ import annotations

from dataclasses import dataclass

from .cloud import Cloud
from .config import Settings
from .embedder import Embedder
from .journal import Journal
from .policy import PolicyEngine
from .service import MemoryService
from .store import Shard
from .sync import SyncEngine


@dataclass
class Device:
    settings: Settings
    embedder: Embedder
    journal: Journal
    service: MemoryService
    cloud: Cloud
    sync: SyncEngine

    closed: bool = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.sync.stop()
        self.service.local.close()
        self.service.replica.close()
        self.journal.close()


def build(settings: Settings, embedder: Embedder | None = None, cloud_client=None) -> Device:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    embedder = embedder or Embedder(settings.models_dir)
    journal = Journal(settings.journal_path)
    local = Shard(settings.local_shard_dir, "local")
    replica = Shard(settings.replica_shard_dir, "replica")
    service = MemoryService(settings, embedder, local, replica, journal, PolicyEngine(embedder))
    cloud = Cloud(settings.cloud_url, settings.cloud_api_key, settings.collection, client=cloud_client)
    sync = SyncEngine(service, journal, cloud)
    return Device(settings, embedder, journal, service, cloud, sync)
