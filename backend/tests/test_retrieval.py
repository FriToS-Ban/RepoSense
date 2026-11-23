"""Tests for the v2 retrieval pipeline (backend/retrieval)."""
import unittest

from backend.kg.build import build_from_files
from backend.kg.snapshot import GraphSnapshot
from backend.retrieval.diff import parse_unified_diff
from backend.retrieval.lexical import BM25F, query_from_lines, tokenize
from backend.retrieval.packing import pack
from backend.retrieval.pipeline import RepoRetriever, RetrievalConfig
from backend.retrieval.seeds import link

REPO = {
    "app/store.py": (
        "class Store:\n"
        "    \"\"\"Persists orders.\"\"\"\n"
        "    def save(self, order):\n"
        "        return order\n"
        "def checksum(payload):\n"
        "    return len(payload)\n"
    ),
    "app/orders.py": (
        "from app.store import Store, checksum\n"
        "\n"
        "def place_order(payload, store: Store):\n"
        "    total = checksum(payload)\n"
        "    return store.save(total)\n"
        "\n"
        "def cancel_order(order_id):\n"
        "    return order_id\n"
    ),
    "app/api.py": (
        "from app.orders import place_order\n"
        "\n"
        "def handle(request):\n"
        "    return place_order(request, None)\n"
    ),
}


def git_diff(path, hunk_header, lines):
    return f"diff --git a/{path} b/{path}\nindex 1..2 100644\n--- a/{path}\n+++ b/{path}\n{hunk_header}\n" + "\n".join(lines) + "\n"


class DiffParserTest(unittest.TestCase):
    def test_modified_added_deleted_renamed_binary(self):
        text = (
            "diff --git a/x.py b/x.py\nindex 1..2 100644\n--- a/x.py\n+++ b/x.py\n"
            "@@ -2,3 +2,4 @@ def f():\n     a = 1\n-    b = 2\n+    b = 3\n+    c = 4\n\n\\ No newline at end of file\n"
            "diff --git a/new.py b/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,1 @@\n+print(1)\n"
            "diff --git a/old.py b/old.py\ndeleted file mode 100644\n--- a/old.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-print(0)\n"
            "diff --git a/a.py b/b.py\nsimilarity index 90%\nrename from a.py\nrename to b.py\n"
            "diff --git a/img.png b/img.png\nBinary files a/img.png and b/img.png differ\n"
        )
        files = parse_unified_diff(text)
        self.assertEqual([f.status for f in files], ["modified", "added", "deleted", "renamed", "modified"])
        x = files[0]
        self.assertEqual(x.removed_lines, {3})
        self.assertEqual(x.added_lines, {3, 4})
        self.assertEqual(x.hunks[0].lines[-1][0], " ")  # empty context line recovered
        self.assertEqual((files[3].old_path, files[3].new_path), ("a.py", "b.py"))
        self.assertTrue(files[4].binary)
        self.assertEqual(files[2].new_path, None)


class SeedLinkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = GraphSnapshot(build_from_files("r", [{"path": p, "content": c} for p, c in REPO.items()], confidence={}))

    def test_body_and_signature_changes(self):
        body = git_diff("app/orders.py", "@@ -4,1 +4,1 @@", ["-    total = checksum(payload)", "+    total = checksum(payload) + 1"])
        head = REPO["app/orders.py"].replace("checksum(payload)\n", "checksum(payload) + 1\n")
        ch = link(self.snap, parse_unified_diff(body), lambda p: head if p == "app/orders.py" else REPO.get(p))
        self.assertEqual([(s.qualname, s.change) for s in ch.seeds], [("place_order", "body_changed")])
        self.assertTrue(ch.structural_change)
        sig = git_diff("app/orders.py", "@@ -7,1 +7,1 @@", ["-def cancel_order(order_id):", "+def cancel_order(order_id, reason):"])
        head2 = REPO["app/orders.py"].replace("def cancel_order(order_id):", "def cancel_order(order_id, reason):")
        ch2 = link(self.snap, parse_unified_diff(sig), lambda p: head2 if p == "app/orders.py" else REPO.get(p))
        self.assertEqual([(s.qualname, s.change) for s in ch2.seeds], [("cancel_order", "signature_changed")])

    def test_added_and_removed_symbols(self):
        head = REPO["app/orders.py"].replace("def cancel_order(order_id):\n    return order_id\n", "def refund(order_id):\n    return 0\n")
        d = git_diff("app/orders.py", "@@ -7,2 +7,2 @@",
                     ["-def cancel_order(order_id):", "-    return order_id", "+def refund(order_id):", "+    return 0"])
        ch = link(self.snap, parse_unified_diff(d), lambda p: head if p == "app/orders.py" else REPO.get(p))
        self.assertEqual(sorted((s.qualname, s.change) for s in ch.seeds), [("cancel_order", "removed"), ("refund", "added")])


class BM25FTest(unittest.TestCase):
    def test_name_field_outweighs_body(self):
        docs = [(0, {"name": "parse_config", "body": "x"}), (1, {"name": "other", "body": "parse config parse config"})]
        hits = BM25F(docs).search(query_from_lines([("parse_config()", 1.0)]), 2)
        self.assertEqual(hits[0][0], 0)

    def test_exclusion_and_determinism(self):
        docs = [(i, {"name": "same", "body": "token"}) for i in range(4)]
        bm = BM25F(docs)
        self.assertEqual([d for d, _ in bm.search({"token": 1.0}, 3, exclude={0})], [1, 2, 3])
        self.assertEqual(tokenize("HTTPResponse"), ["httpresponse", "http", "response"])


class PackingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        g = build_from_files("r", [{"path": p, "content": c} for p, c in REPO.items()], confidence={})
        cls.snap = GraphSnapshot(g)

    def _seed(self, path, qual):
        d = git_diff(path, "@@ -3,1 +3,1 @@", ["-def place_order(payload, store: Store):", "+def place_order(payload, store: Store, retry=False):"])
        return link(self.snap, parse_unified_diff(d), REPO.get).seeds

    def test_callers_get_callsite_and_budget_is_respected(self):
        seeds = self._seed("app/orders.py", "place_order")
        handle = self.snap.by_qualname("app/api.py", "handle")
        store = self.snap.by_qualname("app/store.py", "Store")
        res = pack(self.snap, seeds, [(handle, 2.0, "lexical"), (store, 1.0, "lexical")], budget=10_000)
        by_key = {it.key: it for it in res.items}
        self.assertEqual(res.items[0].rendering, "seed")
        self.assertIn(by_key["app/api.py::handle"].rendering, ("callsite", "full"))
        self.assertIn("place_order(request, None)", by_key["app/api.py::handle"].text)
        tiny = pack(self.snap, seeds, [(handle, 2.0, "lexical"), (store, 1.0, "lexical")], budget=250)
        self.assertLessEqual(tiny.chars, 250)

    def test_full_class_contains_its_methods(self):
        store = self.snap.by_qualname("app/store.py", "Store")
        save = self.snap.by_qualname("app/store.py", "Store.save")
        res = pack(self.snap, [], [(store, 2.0, "lexical"), (save, 1.0, "lexical")], budget=10_000)
        self.assertEqual([it.key for it in res.items], ["app/store.py::Store"])
        self.assertEqual(res.items[0].rendering, "full")


class PipelineTest(unittest.TestCase):
    def test_end_to_end_excludes_changed_file_and_traces(self):
        g = build_from_files("r", [{"path": p, "content": c} for p, c in REPO.items()], confidence={})
        retr = RepoRetriever(g, RetrievalConfig(budget=4000))
        d = git_diff("app/orders.py", "@@ -4,1 +4,1 @@", ["-    total = checksum(payload)", "+    total = checksum(payload) * 2"])
        res = retr.build_context(d, REPO.get, pr_text="store checksum")
        keys = [it.key for it in res.items]
        self.assertEqual(keys[0], "app/orders.py::place_order")
        self.assertNotIn("app/orders.py::cancel_order", keys)  # changed file is never a lexical candidate
        self.assertIn("app/store.py::checksum", keys)
        self.assertLessEqual(len(res.text), 4000 + 2 * len(res.items))
        self.assertEqual(res.trace["seeds"][0]["change"], "body_changed")
        self.assertIn("total_ms", res.trace["timings_ms"])


if __name__ == "__main__":
    unittest.main()
