"""Tests for the learned reranker (backend/retrieval/ltr.py)."""
import random
import tempfile
import unittest
from pathlib import Path

from backend.kg.build import build_from_files
from backend.retrieval.diff import parse_unified_diff
from backend.retrieval.ltr import FEATURES, GROUPS, LinearRanker, candidate_features, fit
from backend.retrieval.pipeline import RepoRetriever, RetrievalConfig
from backend.retrieval.seeds import link
from backend.tests.test_retrieval import REPO, git_diff


class FitTest(unittest.TestCase):
    def test_irls_recovers_the_informative_feature_and_roundtrips(self):
        rng = random.Random(1)
        rows = []
        for _ in range(3000):
            a, b = rng.random(), rng.random()
            y = int(rng.random() < 1 / (1 + 2.718 ** -(6 * a - 3)))  # depends on a only
            rows.append(({"a": a, "b": b}, y))
        m = fit(rows, ("a", "b"), l2=0.1)
        self.assertGreater(m.weights[0], 1.0)
        self.assertLess(abs(m.weights[1]), 0.15)
        self.assertGreater(m.score({"a": 0.9, "b": 0.1}), m.score({"a": 0.1, "b": 0.9}))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.json"
            p.write_text(m.to_json(), encoding="utf-8")
            m2 = LinearRanker.load(p)
            self.assertAlmostEqual(m2.score({"a": 0.5, "b": 0.5}), m.score({"a": 0.5, "b": 0.5}))

    def test_negative_subsampling_is_seeded(self):
        rows = [({"a": float(i % 7)}, int(i % 10 == 0)) for i in range(300)]
        self.assertEqual(fit(rows, ("a",), neg_per_pos=3, seed=5).weights, fit(rows, ("a",), neg_per_pos=3, seed=5).weights)


class FeatureAndPipelineTest(unittest.TestCase):
    def setUp(self):
        self.g = build_from_files("r", [{"path": p, "content": c} for p, c in REPO.items()], confidence={})
        self.diff = git_diff("app/store.py", "@@ -6,1 +6,1 @@", ["-    return len(payload)", "+    return len(payload) + 1"])

    def test_features_cover_lexical_and_graph_candidates(self):
        retr = RepoRetriever(self.g)
        change = link(retr.snap, parse_unified_diff(self.diff), REPO.get)
        rows = candidate_features(retr, change, "", RetrievalConfig())
        by_key = {f"{retr.snap.symbols[i].path}::{retr.snap.symbols[i].qualname}": (f, lab) for i, f, lab in rows}
        f, lab = by_key["app/orders.py::place_order"]
        self.assertEqual(f["rel_caller"], 1.0)
        self.assertGreater(f["ppr_log"], 0.0)
        self.assertEqual(set(f), set(FEATURES))
        self.assertIn("app/api.py::handle", by_key)  # two hops: PPR only
        self.assertEqual(by_key["app/api.py::handle"][0]["two_hop"], 1.0)
        lex_only = candidate_features(retr, change, "", RetrievalConfig(), generators=("lexical",))
        self.assertTrue(all("lexical" in lab for _i, _f, lab in lex_only))

    def test_reranker_replaces_rrf(self):
        retr = RepoRetriever(self.g)
        prefer_graph = LinearRanker(("rel_caller", "two_hop"), [5.0, 1.0], 0.0, [0.0, 0.0], [1.0, 1.0])
        cfg = RetrievalConfig(channels=("lexical", "structural", "ppr"), reranker=prefer_graph)
        res = retr.build_context(self.diff, REPO.get, config=cfg)
        self.assertIn("reranked_pool", res.trace["channel_sizes"])
        keys = [c["key"] for c in res.trace["candidates"]]
        self.assertEqual(keys[0], "app/orders.py::place_order")
        self.assertEqual(set(GROUPS), {"lexical", "graph", "cochange", "node"})


if __name__ == "__main__":
    unittest.main()
