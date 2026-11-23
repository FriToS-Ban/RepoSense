"""T1 benchmark and v1-replay tests.  Need git (the reposense corpus and v1 sources are read at pinned commits)."""
import tempfile
import unittest
from pathlib import Path

from eval import corpora, offline
from eval.benchmarks import t1


def _reposense_spec():
    return next(s for s in corpora.manifest() if s.name == "reposense")


class QueryViewsTest(unittest.TestCase):
    def setUp(self):
        J = t1.Judgment
        self.q = t1.Query("q", "c", "deps", "a.py", "f", 1, 5, [
            J("b.py::g", 1, True), J("a.py::h", 1, False), J("c.py::k", 2, True)])

    def test_views(self):
        self.assertEqual(self.q.grades("sym_all"), {"b.py::g": 2, "a.py::h": 2})
        self.assertEqual(self.q.grades("sym_xfile"), {"b.py::g": 2})
        self.assertEqual(self.q.grades("2hop_only"), {"c.py::k": 1})
        self.assertEqual(self.q.grades("graded_1+2hop"), {"b.py::g": 2, "a.py::h": 2, "c.py::k": 1})
        self.assertEqual(self.q.grades("file_xfile"), {"b.py": 1})
        self.assertEqual(self.q.main_gt(), ["b.py::g", "a.py::h"])  # cross-file first

    def test_make_diff_shape(self):
        d = t1.make_diff("a.py", ["def f(x):", "    return g(x)", "", "x"], 1, 2, "impact")
        self.assertTrue(d.startswith("diff --git a/a.py b/a.py\n"))
        self.assertIn("\n-def f(x):\n+def f(x):  # signature changed\n     return g(x)\n", d)


class T1DeterminismTest(unittest.TestCase):
    def test_reposense_queries_match_frozen_data(self):
        """reposense is first in sampling order, so its queries depend only on the seed."""
        spec = _reposense_spec()
        files = corpora.load_verified(spec)
        fresh = t1.generate([(spec.name, files)])
        again = t1.generate([(spec.name, files)])
        self.assertEqual(fresh, again)
        frozen, meta = t1.load()
        self.assertEqual(meta["corpora"]["reposense"]["fingerprint"], spec.fingerprint)
        self.assertEqual([q for q in frozen if q.corpus == "reposense"], fresh)


class V1ReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        offline.bootstrap(Path(tempfile.mkdtemp(prefix="reposense-eval-test-")) / "t.sqlite")
        from eval.systems.v1 import V1

        cls.v1 = V1()

    def test_v1_chunker_emits_header_pseudo_hunks(self):
        d = t1.make_diff("pkg/module.py", ["def f():", "    a = 1", "    b = 2", "    return a + b"], 1, 4, "deps")
        chunks = self.v1.rag_fixed.extract_diff_chunks(d)
        self.assertEqual(len(chunks), 2)
        self.assertNotIn("@@", chunks[0][1])  # finding W7: the file header becomes a query

    def test_shipped_v1_swallows_unbound_local_error(self):
        from backend.core.database import SessionLocal

        offline.LOG_RECORDS.clear()
        d = t1.make_diff("x.py", ["def f():", "    return 1"], 1, 2, "deps")
        self.assertEqual(self.v1.rag_shipped.get_relevant_context("none", d, SessionLocal(), changed_file_paths=["x.py"]), "")
        self.assertTrue(any("all_matched_nodes" in r[2] for r in offline.LOG_RECORDS))


if __name__ == "__main__":
    unittest.main()
