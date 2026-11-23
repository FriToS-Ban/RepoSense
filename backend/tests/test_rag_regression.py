"""Regression tests for backend.services.rag.get_relevant_context.

From commit 7ce9e6d (2026-05-27) until the M0 hotfix, ``all_matched_nodes`` was used
before assignment.  The resulting UnboundLocalError was swallowed by the function's
blanket ``except`` and every review got an empty RAG context
(docs/retrieval/01-phase1-audit.md, finding W1).

These tests run offline: Pinecone and the embedding API are replaced by fakes.
"""
import tempfile
import unittest
import uuid
from pathlib import Path

from eval import offline

offline.bootstrap(Path(tempfile.mkdtemp(prefix="reposense-test-")) / "test.sqlite")

from backend.core.database import SessionLocal  # noqa: E402
from backend.models.models import CodeNode, NodeType, Repository  # noqa: E402
from backend.services import rag  # noqa: E402

DIFF = (
    "diff --git a/app.py b/app.py\n"
    "index 3f2a1b0..9c8d7e6 100644\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,3 +1,3 @@\n"
    " def handler(request):\n"
    "-    return helper(request)\n"
    "+    return helper(request, strict=True)\n"
)


class GetRelevantContextTest(unittest.TestCase):
    def setUp(self):
        self.db = SessionLocal()
        self.repo_id = f"repo-{uuid.uuid4()}"
        self.db.add(Repository(id=self.repo_id, repo_name="r", repo_full_name="t/r", is_indexed=True, crawl_permission=True))
        self.changed = CodeNode(repo_id=self.repo_id, node_type=NodeType.function, name="handler", file_path="app.py",
                                content="def handler(request):\n    return helper(request)\n")
        self.helper = CodeNode(repo_id=self.repo_id, node_type=NodeType.function, name="helper", file_path="lib/util.py",
                               content="def helper(request, strict=False):\n    ...\n")
        self.db.add_all([self.changed, self.helper])
        self.db.commit()
        offline.STORE.query_hook = lambda text, top_k, flt, index: [(self.helper.id, 0.9)]
        offline.LOG_RECORDS.clear()

    def tearDown(self):
        offline.STORE.query_hook = None
        self.db.close()

    def _rag_errors(self):
        return [r for r in offline.LOG_RECORDS if r[0] == "backend.services.rag" and r[1] == "ERROR"]

    def test_context_contains_changed_file_and_retrieved_nodes(self):
        ctx = rag.get_relevant_context(self.repo_id, DIFF, self.db, changed_file_paths=["app.py"])
        self.assertEqual(self._rag_errors(), [])
        self.assertIn("=== app.py (function: handler) ===", ctx)
        self.assertIn("=== lib/util.py (function: helper) ===", ctx)

    def test_retrieval_only_when_no_changed_files_given(self):
        ctx = rag.get_relevant_context(self.repo_id, DIFF, self.db)
        self.assertEqual(self._rag_errors(), [])
        self.assertIn("=== lib/util.py (function: helper) ===", ctx)
        self.assertNotIn("app.py (function: handler)", ctx)

    def test_matches_the_audited_fix(self):
        """The shipped hotfix must behave exactly like the fix measured in the Phase-1 audit."""
        from eval.systems.v1 import V1

        v1 = V1()
        live = rag.get_relevant_context(self.repo_id, DIFF, self.db, changed_file_paths=["app.py"])
        replay = v1.rag_fixed.get_relevant_context(self.repo_id, DIFF, self.db, changed_file_paths=["app.py"])
        shipped = v1.rag_shipped.get_relevant_context(self.repo_id, DIFF, self.db, changed_file_paths=["app.py"])
        self.assertEqual(live, replay)
        self.assertEqual(shipped, "")  # the pre-hotfix behaviour, kept reproducible for baselines


if __name__ == "__main__":
    unittest.main()
