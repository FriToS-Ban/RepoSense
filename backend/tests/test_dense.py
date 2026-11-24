"""Tests for the pluggable dense channel (backend/retrieval/dense.py), with the offline hashing backend."""
import os
import tempfile
import unittest
from pathlib import Path

from backend.kg.build import build_from_files
from backend.retrieval.dense import EmbeddingCache, HashingBackend, OpenAICompatibleBackend
from backend.retrieval.pipeline import RepoRetriever, RetrievalConfig
from backend.tests.test_retrieval import REPO, git_diff


class CountingBackend(HashingBackend):
    def __init__(self):
        super().__init__(64)
        self.texts = 0

    def embed(self, texts, kind):
        self.texts += len(texts)
        return super().embed(texts, kind)


class DenseTest(unittest.TestCase):
    def setUp(self):
        self.g = build_from_files("r", [{"path": p, "content": c} for p, c in REPO.items()], confidence={})

    def test_cache_embeds_each_symbol_version_once(self):
        be = CountingBackend()
        with tempfile.TemporaryDirectory() as d:
            cache = EmbeddingCache(Path(d) / "vec.jsonl")
            RepoRetriever(self.g).attach_dense(be, cache)
            first = be.texts
            RepoRetriever(self.g).attach_dense(be, cache)  # same snapshot again: no new passages
            self.assertEqual(be.texts, first)
            RepoRetriever(self.g).attach_dense(be, EmbeddingCache(Path(d) / "vec.jsonl"))  # reloaded from disk
            self.assertEqual(be.texts, first)

    def test_dense_channel_and_fusion(self):
        retr = RepoRetriever(self.g)
        retr.attach_dense(HashingBackend(256))
        d = git_diff("app/orders.py", "@@ -8,1 +8,1 @@", ["-    return order_id", "+    return str(order_id)"])
        keys = [it.key for it in retr.build_context(d, REPO.get, "cancel order",
                                                    config=RetrievalConfig(channels=("dense",))).items]
        self.assertTrue(keys and keys[0] == "app/orders.py::cancel_order")  # the seed
        self.assertNotIn("app/orders.py::place_order", keys)  # same-file symbols are excluded, as in lexical
        fused = retr.build_context(d, REPO.get, "cancel order", config=RetrievalConfig(channels=("lexical", "dense")))
        self.assertIn("dense", fused.trace["channel_sizes"])

    def test_channel_is_inert_without_a_backend(self):
        retr = RepoRetriever(self.g)
        d = git_diff("app/orders.py", "@@ -8,1 +8,1 @@", ["-    return order_id", "+    return str(order_id)"])
        res = retr.build_context(d, REPO.get, config=RetrievalConfig(channels=("dense",)))
        self.assertNotIn("dense", res.trace["channel_sizes"])

    def test_remote_backend_needs_explicit_environment(self):
        saved = {k: os.environ.pop(k, None) for k in ("DENSE_BASE_URL", "DENSE_MODEL", "DENSE_API_KEY")}
        try:
            self.assertIsNone(OpenAICompatibleBackend.from_env())
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
