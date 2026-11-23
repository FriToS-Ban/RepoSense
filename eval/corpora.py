"""Benchmark corpora: pinned sources, deterministic loading, content fingerprints.

Two source kinds:
  * ``git``: a subtree of this repository at a pinned commit (read with ``git archive``,
    so the working tree, including uncommitted edits, never leaks into a benchmark).
  * ``package``: an installed Python package located with ``importlib``; pinned by
    distribution version and verified by a content fingerprint.

Files are returned in the order ``os.walk`` would visit them (files of a directory
before its sub-directories, both sorted).  The order matters: the v1 indexer's
edge heuristics are order-dependent ("last definition wins").
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

from eval.offline import REPO_ROOT

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "dist", "build", "venv"}
MAX_FILE_BYTES = 100_000  # same rule as backend.services.github.get_repo_contents
MANIFEST_PATH = Path(__file__).resolve().parent / "manifest" / "corpora.json"


@dataclass(frozen=True)
class CorpusSpec:
    name: str
    kind: str  # "git" | "package"
    target: str  # git: subtree path; package: import name
    pin: str  # git: commit sha; package: distribution==version
    fingerprint: str | None = None


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def walk_order_key(rel_path: str):
    parts = rel_path.split("/")
    return [(1, p) for p in parts[:-1]] + [(0, parts[-1])]


def _load_dir(root: Path, sub: str) -> list[dict]:
    out = []
    for dp, dn, fn in os.walk(root / sub):
        dn[:] = sorted(d for d in dn if d not in SKIP_DIRS)
        for f in sorted(fn):
            p = Path(dp) / f
            if p.stat().st_size >= MAX_FILE_BYTES:
                continue
            with open(p, encoding="utf-8", errors="ignore") as fh:
                out.append({"path": p.relative_to(root).as_posix(), "content": fh.read()})
    return out


def _load_git(sha: str, sub: str) -> list[dict]:
    raw = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "archive", "--format=tar", sha, sub], capture_output=True, check=True
    ).stdout
    out = []
    with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
        for m in tf.getmembers():
            if not m.isfile() or m.size >= MAX_FILE_BYTES:
                continue
            if any(part in SKIP_DIRS for part in m.name.split("/")[:-1]):
                continue
            data = tf.extractfile(m).read().decode("utf-8", errors="ignore")
            out.append({"path": m.name, "content": _normalize(data)})
    out.sort(key=lambda f: walk_order_key(f["path"]))
    return out


def load(spec: CorpusSpec) -> list[dict]:
    if spec.kind == "git":
        return _load_git(spec.pin, spec.target)
    if spec.kind == "package":
        dist, _, version = spec.pin.partition("==")
        installed = importlib.metadata.version(dist)
        if installed != version:
            raise RuntimeError(f"corpus {spec.name}: need {dist}=={version}, found {installed}")
        loc = importlib.util.find_spec(spec.target).submodule_search_locations[0]
        return _load_dir(Path(loc).parent, spec.target)
    raise ValueError(spec.kind)


def fingerprint(files: list[dict]) -> str:
    h = hashlib.sha256()
    for f in files:
        h.update(f["path"].encode())
        h.update(b"\0")
        h.update(hashlib.sha256(f["content"].encode("utf-8", errors="ignore")).digest())
    return h.hexdigest()


def manifest() -> list[CorpusSpec]:
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return [CorpusSpec(**c) for c in data["corpora"]]


def load_verified(spec: CorpusSpec, strict: bool = True) -> list[dict]:
    files = load(spec)
    fp = fingerprint(files)
    if spec.fingerprint and fp != spec.fingerprint:
        msg = f"corpus {spec.name}: fingerprint {fp[:12]} != manifest {spec.fingerprint[:12]}"
        if strict:
            raise RuntimeError(msg + " (pass strict=False / --allow-corpus-drift to continue)")
        print("WARNING:", msg)
    return files
