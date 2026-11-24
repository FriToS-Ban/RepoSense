"""T2 helpers that do not need the cloned repositories."""
import unittest

from eval.benchmarks import t2


class T2HelpersTest(unittest.TestCase):
    def test_code_file_filter(self):
        self.assertTrue(t2.is_code("src/flask/app.py"))
        self.assertTrue(t2.is_code("tests/test_app.py"))
        self.assertFalse(t2.is_code("docs/conf.py"))
        self.assertFalse(t2.is_code("examples/tutorial/app.py"))
        self.assertFalse(t2.is_code("README.md"))

    def test_chore_commits_are_skipped(self):
        for subject in ("Bump version to 2.0", "Merge pull request #1", "Revert \"x\"", "Apply black", "Release 1.2"):
            self.assertTrue(t2.CHORE_RE.match(subject), subject)
        self.assertFalse(t2.CHORE_RE.match("Fix redirect handling for relative URLs"))

    def test_views(self):
        q = t2.Query("q", "r", "s", "p", 0, "a.py", "msg", ["a.py", "b.py"],
                     [t2.Judgment("b.py::f", False), t2.Judgment("tests/test_b.py::test_f", True)])
        self.assertEqual(q.grades("sym_code"), {"b.py::f": 1})
        self.assertEqual(q.grades("sym_tests"), {"tests/test_b.py::test_f": 1})
        self.assertEqual(q.grades("file"), {"b.py": 1, "tests/test_b.py": 1})

    def test_analysis_graph_distance(self):
        from backend.kg.build import build_from_files
        from backend.kg.model import symbol_id
        from eval.t2_analysis import bfs, union_adjacency

        files = {"a.py": "def f():\n    return g()\n\ndef g():\n    return 1\n",
                 "b.py": "from a import f\n\ndef h():\n    return f()\n",
                 "c.py": "def lone():\n    return 0\n"}
        g = build_from_files("r", [{"path": p, "content": c} for p, c in files.items()], confidence={})
        sid = lambda p, q: symbol_id("r", p, q)  # noqa: E731
        dist = bfs(union_adjacency([g]), [sid("a.py", "g")])
        self.assertEqual(dist[sid("a.py", "f")], (1, "caller"))
        self.assertEqual(dist[sid("b.py", "h")], (2, "caller>caller"))
        self.assertNotIn(sid("c.py", "lone"), dist)  # unreachable: relation_missing

    def test_v1_key_mapping_is_lenient(self):
        from backend.kg.extract_python import extract
        from eval.run_t2_v1 import map_items

        facts = [extract("m.py", "class A:\n    def f(self):\n        pass\n\nclass B:\n    def f(self):\n        pass\n\n"
                                 "def g():\n    pass\n")]
        items = [("m.py::f", 10, "m.py"), ("m.py::A", 10, "m.py"), ("m.py", 10, "m.py"), ("m.py::g__chunk1", 5, "m.py")]
        self.assertEqual(map_items(items, facts), ["m.py::A.f", "m.py::B.f", "m.py::A", "m.py", "m.py::g"])

    def test_frozen_data_is_consistent(self):
        qs, meta = t2.load()
        self.assertEqual(len(qs), sum(r["queries"] for r in meta["repos"].values()))
        for q in qs:
            self.assertTrue(q.judgments)
            self.assertNotIn(q.anchor + "::", " ".join(j.key for j in q.judgments))  # anchor symbols are never judged


if __name__ == "__main__":
    unittest.main()
