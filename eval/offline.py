"""Run RepoSense backend code offline.

External services (Pinecone, NVIDIA/OpenAI embeddings, google.genai) are replaced by
in-memory fakes.  Every call to them is counted, so evaluations can report the real
network-call cost model of the code under test without making any network calls.

The fake Pinecone index does not embed anything: a fake "vector" is the text that
would have been embedded (``TextVec``).  Queries are answered by a pluggable
``query_hook`` (BM25, an oracle, ...), so the *pipeline* code runs unmodified while
the first-stage retriever is controlled by the experiment.
"""
from __future__ import annotations

import collections
import contextlib
import logging
import os
import random
import sys
import types
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COUNTERS: collections.Counter = collections.Counter()
LOG_RECORDS: list[tuple[str, str, str]] = []

_STATE: dict = {}


class TextVec(str):
    """Fake embedding: carries the text that would have been embedded."""


class FakeIndex:
    def __init__(self):
        self.vectors: dict[str, tuple[TextVec, dict]] = {}
        self.query_hook = None  # callable(text, top_k, filter, index) -> list[(node_id, score)]

    def upsert(self, vectors):
        COUNTERS["pinecone.upsert"] += 1
        for v in vectors:
            self.vectors[v["id"]] = (v["values"], v["metadata"])

    def query(self, vector, top_k, filter=None, include_metadata=True):
        COUNTERS["pinecone.query"] += 1
        hits = self.query_hook(str(vector), top_k, filter or {}, self) if self.query_hook else []
        return types.SimpleNamespace(
            matches=[
                types.SimpleNamespace(id=nid, score=s, metadata={"node_id": nid}) for nid, s in hits[:top_k]
            ]
        )

    def docs_for_repo(self, repo_id: str) -> list[tuple[str, str]]:
        """(node_id, embedded_text) for every vector of a repo, in upsert order."""
        return [(md["node_id"], str(v)) for v, md in self.vectors.values() if md.get("repo_id") == repo_id]


STORE = FakeIndex()


class FakePinecone:
    def __init__(self, api_key=None):
        COUNTERS["pinecone.client_init"] += 1

    def list_indexes(self):
        COUNTERS["pinecone.list_indexes"] += 1
        return [types.SimpleNamespace(name="reposense")]

    def create_index(self, **kw):
        COUNTERS["pinecone.create_index"] += 1

    def Index(self, name):
        return STORE


class _FakeEmbeddings:
    def create(self, model=None, input=None, encoding_format=None, extra_body=None):
        COUNTERS["embed.calls"] += 1
        COUNTERS["embed.chars"] += len(input)
        COUNTERS[f"embed.input_type={(extra_body or {}).get('input_type')}"] += 1
        return types.SimpleNamespace(data=[types.SimpleNamespace(embedding=TextVec(input))])


class FakeOpenAI:
    def __init__(self, base_url=None, api_key=None):
        COUNTERS["openai.client_init"] += 1
        self.embeddings = _FakeEmbeddings()


class _ListHandler(logging.Handler):
    def emit(self, record):
        LOG_RECORDS.append((record.name, record.levelname, record.getMessage()))


def _install_fakes():
    pine = types.ModuleType("pinecone")
    pine.Pinecone = FakePinecone
    sys.modules["pinecone"] = pine
    oa = types.ModuleType("openai")
    oa.OpenAI = FakeOpenAI
    sys.modules["openai"] = oa
    genai = types.ModuleType("google.genai")
    try:
        import google  # namespace package used by other google libraries; keep it intact
    except ImportError:
        google = types.ModuleType("google")
        google.__path__ = []
        sys.modules["google"] = google
    sys.modules["google.genai"] = genai
    google.genai = genai


def bootstrap(db_path: str | os.PathLike):
    """Idempotent.  Must run before any ``backend.*`` import.  Returns the SQLAlchemy engine."""
    if "engine" in _STATE:
        return _STATE["engine"]
    os.environ.setdefault("SECRET_KEY", "offline-eval")
    os.environ["DATABASE_URL"] = f"sqlite:///{Path(db_path).as_posix()}"
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    _install_fakes()
    handler = _ListHandler()
    handler.setLevel(logging.WARNING)
    logging.getLogger().addHandler(handler)

    import backend.models.models  # noqa: F401  (registers tables)
    from backend.core.database import Base, engine

    Base.metadata.create_all(engine)
    _STATE["engine"] = engine
    return engine


@contextlib.contextmanager
def deterministic_uuids(seed: int):
    """Make ``uuid.uuid4`` reproducible (v1 node ids are uuid4 and leak into tie-breaks)."""
    rng = random.Random(seed)
    original = uuid.uuid4
    uuid.uuid4 = lambda: uuid.UUID(int=rng.getrandbits(128), version=4)
    try:
        yield
    finally:
        uuid.uuid4 = original
