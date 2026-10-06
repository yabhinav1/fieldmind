import shutil
import sys
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fieldmind.config import Settings  # noqa: E402
from fieldmind.embedder import Embedder  # noqa: E402
from fieldmind.ner import NameFinder  # noqa: E402
from fieldmind.reranker import Reranker  # noqa: E402
from fieldmind.runtime import build  # noqa: E402


@pytest.fixture(scope="session")
def embedder():
    return Embedder(Settings().models_dir)


@pytest.fixture(scope="session")
def names():
    finder = NameFinder(Settings().models_dir)
    assert finder.available, finder.error
    return finder


@pytest.fixture(scope="session")
def reranker():
    model = Reranker(Settings().models_dir)
    assert model.available, model.error
    return model


@pytest.fixture
def fleet(tmp_path, embedder, names, reranker):
    """Two devices on the same site, sharing one cloud."""
    cloud_client = QdrantClient(":memory:")
    devices = []

    def make(name: str, site: str = "plant-1", **overrides):
        settings = Settings(device_id=name, site=site, data_root=tmp_path, sync_batch=100, ollama_model="", **overrides)
        device = build(settings, embedder=embedder, cloud_client=cloud_client, names=names, reranker=reranker)
        devices.append(device)
        return device

    yield make
    for device in devices:
        try:
            device.close()
        except Exception:
            pass  # already closed by the test
    # Each shard preallocates about 215 MB, so leftovers fill a disk quickly.
    shutil.rmtree(tmp_path, ignore_errors=True)
