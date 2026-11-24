"""Tests for multi-hop structural candidates (backend/retrieval/ppr.py)."""
import unittest

from backend.kg.build import build_from_files
from backend.kg.snapshot import GraphSnapshot
from backend.retrieval.ppr import Transitions, multihop_candidates, ppr_push, sources_from_seeds, typed_bfs
from backend.retrieval.pipeline import RepoRetriever, RetrievalConfig
from backend.tests.test_retrieval import REPO, git_diff


def _snap(files=REPO):
    return GraphSnapshot(build_from_files("r", [{"path": p, "content": c} for p, c in files.items()], confidence={}))


def _idx(snap, key):
    path, qual = key.split("::")
    return snap.by_qualname(path, qual)


def exact_ppr(trans, sources, alpha, steps=400):
    """Reference: alpha * sum_t ((1 - alpha) P)^t s, with P row-normalised (dangling rows absorb)."""
    pi, v = {}, dict(sources)
    for _ in range(steps):
        nxt = {}
        for u, m in v.items():
            pi[u] = pi.get(u, 0.0) + alpha * m
            nbrs, total = trans.row(u)
            for w_node, w, _r in nbrs:
                nxt[w_node] = nxt.get(w_node, 0.0) + (1 - alpha) * m * w / total
        v = nxt
    return pi


class PPRTest(unittest.TestCase):
    def test_local_push_matches_power_iteration(self):
        snap = _snap()
        trans = Transitions(snap)
        src = {_idx(snap, "app/store.py::checksum"): 1.0}
        approx = ppr_push(trans, src, alpha=0.3, eps=1e-9)
        exact = exact_ppr(trans, src, 0.3)
        self.assertLessEqual(sum(approx.values()), 1.0 + 1e-9)
        for k in exact:
            self.assertAlmostEqual(approx.get(k, 0.0), exact[k], places=6)

    def test_ppr_ranks_direct_neighbours_first_and_reaches_two_hops(self):
        snap = _snap()
        trans = Transitions(snap)
        p = ppr_push(trans, {_idx(snap, "app/store.py::checksum"): 1.0}, alpha=0.3, eps=1e-6)
        place, handle = _idx(snap, "app/orders.py::place_order"), _idx(snap, "app/api.py::handle")
        self.assertGreater(p[place], p[handle])
        self.assertIn(handle, p)  # caller of a caller

    def test_coarse_tolerance_does_less_work(self):
        snap = _snap()
        trans = Transitions(snap)
        src = {_idx(snap, "app/store.py::checksum"): 1.0}
        self.assertLessEqual(len(ppr_push(trans, src, 0.3, eps=0.2)), len(ppr_push(trans, src, 0.3, eps=1e-6)))

    def test_typed_bfs_paths(self):
        snap = _snap()
        b = typed_bfs(Transitions(snap), {_idx(snap, "app/store.py::checksum"): 1.0}, depth=2)
        self.assertEqual(b[_idx(snap, "app/orders.py::place_order")][1:], (1, "caller"))
        self.assertEqual(b[_idx(snap, "app/api.py::handle")][1:], (2, "caller>caller"))
        self.assertEqual(b[_idx(snap, "app/store.py::Store.save")][1:], (2, "caller>callee"))

    def test_candidates_exclude_seeds_and_modules_and_respect_min_hops(self):
        snap = _snap()

        class S:
            index = _idx(snap, "app/store.py::checksum")
            key = "app/store.py::checksum"

        for method in ("ppr", "bfs"):
            cands = multihop_candidates(snap, [S()], method, limit=50)
            idx = [c[0] for c in cands]
            self.assertNotIn(S.index, idx)
            self.assertTrue(all(snap.symbols[i].kind != "module" for i in idx))
            two = [c[0] for c in multihop_candidates(snap, [S()], method, limit=50, min_hops=2)]
            self.assertNotIn(_idx(snap, "app/orders.py::place_order"), two)
            self.assertIn(_idx(snap, "app/api.py::handle"), two)

    def test_added_seed_restarts_from_its_head_dependencies(self):
        snap = _snap()

        class Added:
            index = None
            key = "app/orders.py::new_fn"

        dep = _idx(snap, "app/store.py::checksum")
        self.assertEqual(sources_from_seeds(snap, [Added()], {Added.key: [(dep, "CALLS", 1.0, [3])]}), {dep: 1.0})
        self.assertEqual(sources_from_seeds(snap, [Added()], None), {})

    def test_pipeline_multihop_tail(self):
        g = build_from_files("r", [{"path": p, "content": c} for p, c in REPO.items()], confidence={})
        retr = RepoRetriever(g, RetrievalConfig(channels=("structural",)))
        d = git_diff("app/store.py", "@@ -6,1 +6,1 @@", ["-    return len(payload)", "+    return len(payload) + 1"])
        one_hop = [it.key for it in retr.build_context(d, REPO.get).items]
        self.assertNotIn("app/api.py::handle", one_hop)
        for method in ("ppr", "bfs"):
            cfg = RetrievalConfig(channels=("structural",), multihop=method)
            keys = [it.key for it in retr.build_context(d, REPO.get, config=cfg).items]
            self.assertIn("app/api.py::handle", keys, method)
            self.assertLess(keys.index("app/orders.py::place_order"), keys.index("app/api.py::handle"))
        keys = [it.key for it in retr.build_context(d, REPO.get, config=RetrievalConfig(channels=("ppr", "lexical"))).items]
        self.assertIn("app/api.py::handle", keys)


if __name__ == "__main__":
    unittest.main()
