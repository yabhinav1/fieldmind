"""Wires one device together: embedder, two shards, journal, policy, cloud and sync."""

from __future__ import annotations

from dataclasses import dataclass

from .cloud import Cloud
from .config import Settings
from .embedder import Embedder
from .journal import Journal
from .ner import NameFinder
from .peers import Fetch, PeerExchange
from .photos import PhotoStore
from .policy import PolicyEngine
from .reranker import Reranker
from .service import MemoryService
from .speech import Transcriber
from .store import Shard
from .sync import SyncEngine
from .vault import Vault
from .vision import ImageEmbedder


@dataclass
class Device:
    settings: Settings
    embedder: Embedder
    names: NameFinder
    journal: Journal
    service: MemoryService
    cloud: Cloud
    sync: SyncEngine
    reranker: Reranker | None = None
    vault: Vault | None = None
    peers: PeerExchange | None = None
    photos: PhotoStore | None = None
    speech: Transcriber | None = None

    closed: bool = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.sync.stop()
        self.service.local.close()
        self.service.replica.close()
        if self.photos:
            self.photos.close()
        self.journal.close()


def build(settings: Settings, embedder: Embedder | None = None, cloud_client=None,
          names: NameFinder | None = None, reranker: Reranker | None = None,
          peer_fetch: Fetch | None = None, vision: ImageEmbedder | None = None,
          speech: Transcriber | None = None) -> Device:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    embedder = embedder or Embedder(settings.models_dir)
    names = names or NameFinder(settings.models_dir)
    if reranker is None and settings.rerank:
        reranker = Reranker(settings.models_dir)
    vault = Vault.open(settings.vault_key_file)
    journal = Journal(settings.journal_path, vault=vault)
    local = Shard(settings.local_shard_dir, "local", vault)
    replica = Shard(settings.replica_shard_dir, "replica", vault)
    policy = PolicyEngine(embedder, names, store=journal)
    photos = PhotoStore(settings, vault, vision or ImageEmbedder(settings.models_dir)) if settings.photos else None
    service = MemoryService(settings, embedder, local, replica, journal, policy, reranker=reranker, photos=photos)
    cloud = Cloud(settings.cloud_url, settings.cloud_api_key, settings.collection, client=cloud_client)
    peers = PeerExchange(service, fetch=peer_fetch)
    sync = SyncEngine(service, journal, cloud, peers=peers)
    if speech is None and settings.speech:
        speech = Transcriber(settings.models_dir, settings.speech_model)
    return Device(settings, embedder, names, journal, service, cloud, sync, reranker, vault, peers, photos, speech)
