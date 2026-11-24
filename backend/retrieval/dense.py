"""Dense candidate channel (design §5.3, ADR-10): pluggable, cached, and off unless configured.

Backends implement ``embed(texts, kind) -> list[vector]`` with kind "passage" | "query":

  * ``OpenAICompatibleBackend``: any /v1/embeddings endpoint (NVIDIA NIM, OpenAI, a local
    server).  The key is read from an environment variable by the caller, never from files.
  * ``HashingBackend``: signed feature hashing of identifier tokens.  NOT a semantic model:
    it exists so the plumbing is testable offline, and it is never reported as "dense".

``DenseIndex`` embeds one passage per symbol (compact rendering + body prefix, design §5.3),
caches vectors by (model, content hash) so unchanged symbols are never re-embedded across
snapshots, and answers cosine top-k queries.  Pure Python: no new dependency.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Protocol

from backend.retrieval.lexical import tokenize

PASSAGE_CHARS = 1500


class DenseBackend(Protocol):
    name: str

    def embed(self, texts: list[str], kind: str) -> list[list[float]]: ...


class HashingBackend:
    """Deterministic stand-in for tests: signed hashing of identifier tokens into ``dim`` buckets."""

    def __init__(self, dim: int = 256):
        self.dim, self.name = dim, f"hashing-{dim}"

    def embed(self, texts: list[str], kind: str) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for tok in tokenize(t):
                h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "little")
                v[h % self.dim] += 1.0 if (h >> 32) & 1 else -1.0
            out.append(v)
        return out


class OpenAICompatibleBackend:
    """POST {base_url}/embeddings in batches.  ``input_type`` is sent for asymmetric models (NVIDIA)."""

    def __init__(self, base_url: str, api_key: str, model: str, batch: int = 32, input_type: bool = True,
                 timeout: float = 120.0):
        self.base_url, self.api_key, self.model = base_url.rstrip("/"), api_key, model
        self.batch, self.input_type, self.timeout = batch, input_type, timeout
        self.name = model
        self.calls = 0

    @classmethod
    def from_env(cls) -> "OpenAICompatibleBackend | None":
        """DENSE_BASE_URL, DENSE_MODEL and DENSE_API_KEY (environment only), else None."""
        url, model, key = (os.environ.get(k) for k in ("DENSE_BASE_URL", "DENSE_MODEL", "DENSE_API_KEY"))
        return cls(url, key, model) if url and model and key else None

    def embed(self, texts: list[str], kind: str) -> list[list[float]]:
        import requests

        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            body = {"model": self.model, "input": texts[i: i + self.batch], "encoding_format": "float"}
            if self.input_type:
                body["input_type"] = kind
            r = requests.post(f"{self.base_url}/embeddings", json=body, timeout=self.timeout,
                              headers={"Authorization": f"Bearer {self.api_key}"})
            r.raise_for_status()
            self.calls += 1
            out += [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]
        return out


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def passage(sym) -> str:
    head = f"{sym.path} :: {sym.qualname}\n{sym.signature or sym.name}\n"
    doc = (sym.docstring or "").strip()
    return (head + (doc + "\n" if doc else "") + (sym.body or ""))[:PASSAGE_CHARS]


class EmbeddingCache:
    """(model, content hash) -> unit vector; optionally persisted as JSON lines."""

    def __init__(self, path: Path | None = None):
        self.path, self.vec = path, {}
        if path and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                k, v = json.loads(line)
                self.vec[k] = v

    def get_many(self, backend: DenseBackend, items: list[tuple[str, str]]) -> list[list[float]]:
        """items: [(content key, text)] -> unit vectors, embedding only the missing ones."""
        missing = [(k, t) for k, t in dict(items).items() if f"{backend.name}|{k}" not in self.vec]
        if missing:
            vecs = backend.embed([t for _, t in missing], "passage")
            new = {f"{backend.name}|{k}": _unit(v) for (k, _), v in zip(missing, vecs)}
            self.vec.update(new)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    for k, v in new.items():
                        fh.write(json.dumps([k, [round(x, 6) for x in v]]) + "\n")
        return [self.vec[f"{backend.name}|{k}"] for k, _ in items]


class DenseIndex:
    def __init__(self, snap, backend: DenseBackend, cache: EmbeddingCache | None = None):
        from backend.kg.model import Kind

        self.backend, self.cache = backend, cache or EmbeddingCache()
        self.ids = [i for i, s in enumerate(snap.symbols) if s.kind != Kind.MODULE]
        self.vecs = self.cache.get_many(backend, [(snap.symbols[i].content_hash, passage(snap.symbols[i])) for i in self.ids])

    def search(self, text: str, k: int, exclude: set[int]) -> list[tuple[int, float]]:
        q = _unit(self.backend.embed([text], "query")[0])
        scored = [(i, sum(a * b for a, b in zip(q, v))) for i, v in zip(self.ids, self.vecs) if i not in exclude]
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[:k]
