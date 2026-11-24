"""Knowledge-graph v2 indexing for production (design §4.1, §4.6; M7).

* Acquisition: one tarball of the repository at a commit (one API call) instead of v1's
  recursive contents crawl (one call per file and directory, 100 KB cap per file).
* Full index: tarball -> FileFacts (extraction cache) -> global resolution -> diff-apply.
* Push update: the compare API between the indexed commit and the pushed one gives the
  changed paths; only those are fetched and re-extracted (kg.store.update_files).  If the
  stored snapshot is not the push's ``before`` commit (a missed event, a force-push) or the
  compare result is truncated, a full index runs instead.
* One writer per repository: an in-process lock, plus a transaction-scoped advisory lock
  on PostgreSQL so that several workers cannot interleave index/update of the same repo.

Nothing here is used unless RETRIEVAL_ENGINE=v2 (design §8).
"""
from __future__ import annotations

import io
import logging
import tarfile
import threading
import zlib
from contextlib import contextmanager

from sqlalchemy import select, text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 1_000_000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "site-packages", ".tox"}
CODE_EXTENSIONS = (".py",)  # the v2 extractor is Python-only (JS/TS via tree-sitter is a follow-on, design §9 M11)
COMPARE_FILE_LIMIT = 300  # the GitHub compare API lists at most 300 files

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def files_from_tarball(data: bytes, max_bytes: int = MAX_FILE_BYTES) -> list[dict]:
    """Code files of a GitHub tarball (``owner-repo-sha/`` prefix stripped), read in memory.

    Only regular files are read: links, devices and paths that are absolute or contain '..'
    are skipped, and nothing is written to disk."""
    out = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        for m in tar:
            if not m.isreg() or m.size > max_bytes:
                continue
            parts = m.name.split("/")
            if len(parts) < 2 or m.name.startswith("/") or ".." in parts:
                continue
            path = "/".join(parts[1:])
            if not path.endswith(CODE_EXTENSIONS) or any(p in SKIP_DIRS for p in parts[1:-1]):
                continue
            fh = tar.extractfile(m)
            if fh is None:
                continue
            out.append({"path": path, "content": fh.read().decode("utf-8", "replace").replace("\r\n", "\n")})
    out.sort(key=lambda f: f["path"])
    return out


@contextmanager
def repo_lock(db: Session, repo_id: str):
    with _locks_guard:
        lock = _locks.setdefault(repo_id, threading.Lock())
    with lock:
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": zlib.crc32(repo_id.encode()) & 0x7FFFFFFF})
        yield


def current_snapshot(db: Session, repo_id: str) -> str | None:
    from backend.models.models import KGSnapshot

    row = db.execute(select(KGSnapshot.commit_sha).where(KGSnapshot.repo_id == repo_id, KGSnapshot.is_current)).first()
    return row[0] if row else None


def index_full(db: Session, repo_id: str, access_token: str, repo_full_name: str, sha: str, gh=None, locked=False):
    from backend.kg.store import index_files

    gh = gh or _github()
    files = files_from_tarball(gh.get_tarball(access_token, repo_full_name, sha))
    if locked:
        st = index_files(db, repo_id, files, sha)
    else:
        with repo_lock(db, repo_id):
            st = index_files(db, repo_id, files, sha)
    _mark_indexed(db, repo_id)
    logger.info("kg v2: indexed %s@%s: %d files, %s", repo_full_name, sha[:10], len(files), st)
    return st


def _mark_indexed(db: Session, repo_id: str) -> None:
    from backend.models.models import Repository

    repo = db.get(Repository, repo_id)
    if repo is not None and not repo.is_indexed:
        repo.is_indexed = True
        db.commit()


def index_default_branch(db: Session, repo_id: str, access_token: str, repo_full_name: str, gh=None):
    gh = gh or _github()
    return index_full(db, repo_id, access_token, repo_full_name, gh.get_default_branch_head(access_token, repo_full_name), gh)


def update_on_push(db: Session, repo_id: str, access_token: str, repo_full_name: str, before: str, after: str, gh=None):
    """Incremental update from ``before`` to ``after``; falls back to a full index when the stored
    snapshot is not ``before`` or the compare result is truncated.  The snapshot is checked under
    the repository lock, so concurrent pushes cannot both apply a diff to the same base.
    (Out-of-order webhook delivery still ends at the last *processed* push; GitHub delivers
    pushes to one ref in order.)"""
    from backend.kg.store import update_files

    gh = gh or _github()
    with repo_lock(db, repo_id):
        snap = current_snapshot(db, repo_id)
        if snap == after:
            return None  # already indexed (a redelivered event)
        if snap != before:
            logger.info("kg v2: %s snapshot is %s, not %s; full index at %s", repo_full_name, (snap or "-")[:10],
                        before[:10], after[:10])
            return index_full(db, repo_id, access_token, repo_full_name, after, gh, locked=True)
        changed = gh.compare_commits(access_token, repo_full_name, before, after)
        if changed is None or len(changed) >= COMPARE_FILE_LIMIT:
            return index_full(db, repo_id, access_token, repo_full_name, after, gh, locked=True)
        changes: dict[str, str | None] = {}
        for f in changed:
            path, status = f["filename"], f["status"]
            if status == "renamed" and f.get("previous_filename"):
                changes[f["previous_filename"]] = None
            if not path.endswith(CODE_EXTENSIONS) or any(p in SKIP_DIRS for p in path.split("/")[:-1]):
                continue
            if status == "removed":
                changes[path] = None
            else:
                content = gh.get_file_content_at(access_token, repo_full_name, path, after)
                changes[path] = None if content is None else content.replace("\r\n", "\n")
        st = update_files(db, repo_id, changes, after)
    logger.info("kg v2: updated %s %s..%s: %d changed paths, %s", repo_full_name, before[:10], after[:10], len(changes), st)
    return st


def _github():
    from backend.services import github

    return github


def _run_in_thread(fn, *args):
    def target():
        from backend.core.database import SessionLocal

        db = SessionLocal()
        try:
            fn(db, *args)
        except Exception:  # noqa: BLE001 - background job: log and keep the server alive
            logger.exception("kg v2 background job failed")
            db.rollback()
        finally:
            db.close()

    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t


def index_full_background(repo_id: str, access_token: str, repo_full_name: str, sha: str):
    return _run_in_thread(index_full, repo_id, access_token, repo_full_name, sha)


def index_default_branch_background(repo_id: str, access_token: str, repo_full_name: str):
    return _run_in_thread(index_default_branch, repo_id, access_token, repo_full_name)


def update_on_push_background(repo_id: str, access_token: str, repo_full_name: str, before: str, after: str):
    return _run_in_thread(update_on_push, repo_id, access_token, repo_full_name, before, after)
