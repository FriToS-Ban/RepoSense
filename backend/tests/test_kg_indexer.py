"""M7: production acquisition and incremental indexing for knowledge graph v2 (offline, fake GitHub)."""
import io
import tarfile
import tempfile
import unittest
import uuid
from pathlib import Path

from eval import offline

offline.bootstrap(Path(tempfile.mkdtemp(prefix="reposense-test-")) / "test.sqlite")

from backend.api.routes.webhooks import kg_push_target  # noqa: E402
from backend.core.config import settings  # noqa: E402
from backend.core.database import SessionLocal  # noqa: E402
from backend.kg.build import build_from_files  # noqa: E402
from backend.kg.store import load_graph  # noqa: E402
from backend.models.models import Repository  # noqa: E402
from backend.services import kg_indexer, rag  # noqa: E402

A = {"pkg/core.py": "def helper(x):\n    return x\n\nclass Store:\n    def save(self, v):\n        return helper(v)\n",
     "pkg/api.py": "from pkg.core import Store\n\ndef handle(r):\n    return Store().save(r)\n",
     "docs/conf.py": "project = 'x'\n"}
B = {**A, "pkg/core.py": A["pkg/core.py"] + "\ndef audit(v):\n    return helper(v)\n",
     "pkg/new.py": "from pkg.core import audit\n\ndef run():\n    return audit(1)\n"}
C = {k: v for k, v in B.items() if k != "pkg/api.py"}
C["pkg/http.py"] = B["pkg/api.py"]  # renamed


def tarball(files: dict, prefix="owner-repo-abc123", extra=None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, content in sorted(files.items()):
            data = content.encode()
            info = tarfile.TarInfo(f"{prefix}/{path}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info, data in extra or []:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


class FakeGitHub:
    def __init__(self, commits: dict, head: str):
        self.commits, self.head, self.calls = commits, head, []

    def get_tarball(self, token, full, ref):
        self.calls.append(("tarball", ref))
        return tarball(self.commits[ref])

    def get_default_branch_head(self, token, full):
        return self.head

    def compare_commits(self, token, full, base, head):
        self.calls.append(("compare", base, head))
        a, b = self.commits[base], self.commits[head]
        out = []
        renamed = {"pkg/http.py": "pkg/api.py"} if base == "B" and head == "C" else {}
        for p in sorted(set(a) | set(b)):
            if p in renamed:
                out.append({"filename": p, "status": "renamed", "previous_filename": renamed[p]})
            elif p in renamed.values():
                continue
            elif p not in b:
                out.append({"filename": p, "status": "removed", "previous_filename": None})
            elif p not in a:
                out.append({"filename": p, "status": "added", "previous_filename": None})
            elif a[p] != b[p]:
                out.append({"filename": p, "status": "modified", "previous_filename": None})
        return out

    def get_file_content_at(self, token, full, path, ref):
        self.calls.append(("file", path, ref))
        return self.commits[ref].get(path)


def same_graph(test, stored, files):
    fresh = build_from_files(stored.repo_id, [{"path": p, "content": c} for p, c in files.items()])
    test.assertEqual(set(stored.symbols), set(fresh.symbols))
    test.assertEqual({k: (round(e.confidence, 6), e.resolution, e.weight) for k, e in stored.edges.items()},
                     {k: (round(e.confidence, 6), e.resolution, e.weight) for k, e in fresh.edges.items()})


class TarballTest(unittest.TestCase):
    def test_only_regular_code_files_inside_the_repo(self):
        link = tarfile.TarInfo("owner-repo-abc123/pkg/link.py")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        evil = tarfile.TarInfo("owner-repo-abc123/../evil.py")
        evil.size = 1
        big = tarfile.TarInfo("owner-repo-abc123/pkg/big.py")
        big.size = 50
        data = tarball({"pkg/a.py": "x = 1\r\n", "node_modules/m.py": "y = 2\n", "README.md": "hi\n"},
                       extra=[(link, None), (evil, b"x"), (big, b"z" * 50)])
        files = kg_indexer.files_from_tarball(data, max_bytes=40)
        self.assertEqual(files, [{"path": "pkg/a.py", "content": "x = 1\n"}])


class IndexAndUpdateTest(unittest.TestCase):
    def setUp(self):
        self.db = SessionLocal()
        self.rid = f"repo-{uuid.uuid4()}"
        self.db.add(Repository(id=self.rid, repo_name="r", repo_full_name="o/r", crawl_permission=True))
        self.db.commit()
        self.gh = FakeGitHub({"A": A, "B": B, "C": C, "D": A}, head="A")

    def tearDown(self):
        self.db.close()

    def test_full_index_then_incremental_updates(self):
        kg_indexer.index_default_branch(self.db, self.rid, "t", "o/r", gh=self.gh)
        self.assertEqual(kg_indexer.current_snapshot(self.db, self.rid), "A")
        self.assertTrue(self.db.get(Repository, self.rid).is_indexed)
        same_graph(self, load_graph(self.db, self.rid), A)

        self.gh.calls.clear()
        st = kg_indexer.update_on_push(self.db, self.rid, "t", "o/r", "A", "B", gh=self.gh)
        self.assertGreater(st.symbols_inserted, 0)
        self.assertNotIn(("tarball", "B"), self.gh.calls)  # incremental: no tarball
        self.assertEqual(sorted(c[1] for c in self.gh.calls if c[0] == "file"), ["pkg/core.py", "pkg/new.py"])
        same_graph(self, load_graph(self.db, self.rid), B)

        self.assertIsNone(kg_indexer.update_on_push(self.db, self.rid, "t", "o/r", "A", "B", gh=self.gh))  # redelivery
        kg_indexer.update_on_push(self.db, self.rid, "t", "o/r", "B", "C", gh=self.gh)  # rename
        same_graph(self, load_graph(self.db, self.rid), C)
        self.assertEqual(kg_indexer.current_snapshot(self.db, self.rid), "C")

    def test_snapshot_mismatch_falls_back_to_full_index(self):
        kg_indexer.index_full(self.db, self.rid, "t", "o/r", "A", gh=self.gh)
        self.gh.calls.clear()
        kg_indexer.update_on_push(self.db, self.rid, "t", "o/r", "B", "C", gh=self.gh)  # missed the A..B push
        self.assertIn(("tarball", "C"), self.gh.calls)
        same_graph(self, load_graph(self.db, self.rid), C)

    def test_truncated_compare_falls_back_to_full_index(self):
        kg_indexer.index_full(self.db, self.rid, "t", "o/r", "A", gh=self.gh)
        self.gh.compare_commits = lambda *a: [{"filename": f"f{i}.py", "status": "added"} for i in range(300)]
        self.gh.calls.clear()
        kg_indexer.update_on_push(self.db, self.rid, "t", "o/r", "A", "B", gh=self.gh)
        self.assertIn(("tarball", "B"), self.gh.calls)
        same_graph(self, load_graph(self.db, self.rid), B)


class WiringTest(unittest.TestCase):
    def test_push_target_is_the_default_branch_only(self):
        p = {"ref": "refs/heads/main", "before": "a" * 40, "after": "b" * 40,
             "repository": {"full_name": "o/r", "default_branch": "main"}}
        self.assertEqual(kg_push_target(p), ("o/r", "a" * 40, "b" * 40))
        self.assertIsNone(kg_push_target({**p, "ref": "refs/heads/feature"}))
        self.assertIsNone(kg_push_target({**p, "after": "0" * 40}))  # branch deleted
        self.assertIsNone(kg_push_target({**p, "before": ""}))

    def test_rag_facade_uses_v2_only_when_enabled(self):
        db = SessionLocal()
        rid = f"repo-{uuid.uuid4()}"
        db.add(Repository(id=rid, repo_name="r", repo_full_name="o/r", crawl_permission=True))
        db.commit()
        kg_indexer.index_full(db, rid, "t", "o/r", "A", gh=FakeGitHub({"A": A}, "A"))
        diff = ("diff --git a/pkg/api.py b/pkg/api.py\nindex 1..2 100644\n--- a/pkg/api.py\n+++ b/pkg/api.py\n"
                "@@ -4,1 +4,1 @@\n-    return Store().save(r)\n+    return Store().save(r) or None\n")
        head = {**A, "pkg/api.py": A["pkg/api.py"].replace("save(r)", "save(r) or None")}
        old = settings.RETRIEVAL_ENGINE
        try:
            settings.RETRIEVAL_ENGINE = "v2"
            ctx = rag.get_relevant_context(rid, diff, db, changed_file_paths=["pkg/api.py"], head_content=head.get,
                                           pr_text="Return None when saving fails")
            self.assertIn("### pkg/api.py::handle  [changed: body_changed]", ctx)
            self.assertIn("### pkg/core.py::Store  [used by handle]", ctx)  # the callee, from BM25F + the graph label
            from backend.services.context_v2 import get_context_v2

            self.assertIsNone(get_context_v2(db, f"repo-{uuid.uuid4()}", diff, head.get))  # no snapshot: caller uses v1
        finally:
            settings.RETRIEVAL_ENGINE = old
            db.close()


if __name__ == "__main__":
    unittest.main()
