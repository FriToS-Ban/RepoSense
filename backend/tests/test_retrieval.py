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

    def test_query_time_parameters_equal_reindexing(self):
        docs = [(0, {"name": "parse_config", "doc": "read settings", "body": "x"}),
                (1, {"name": "other", "doc": "parse config settings", "body": "parse config"}),
                (2, {"name": "settings", "sig": "def settings(path)", "body": "config"})]
        q = {"parse": 1.0, "config": 1.0, "settings": 0.5}
        w = {"name": 1.0, "qual": 1.5, "sig": 3.0, "doc": 2.0, "body": 0.5}
        self.assertEqual(BM25F(docs).search(q, 3, weights=w, k1=2.0, b=0.3), BM25F(docs, w, k1=2.0, b=0.3).search(q, 3))

    def test_query_weighting_options(self):
        g = build_from_files("r", [{"path": p, "content": c} for p, c in REPO.items()], confidence={})
        retr = RepoRetriever(g)
        snap = retr.snap
        files = parse_unified_diff(git_diff("app/orders.py", "@@ -4,2 +4,2 @@",
                                            ["-    total = checksum(payload)", "+    total = checksum(payload) + 1",
                                             "     return store.save(total)"]))
        sd = next(s for s in link(snap, files, REPO.get).seeds if s.qualname == "place_order")
        base = retr._seed_query(sd, "fix totals", RetrievalConfig())
        self.assertEqual((base["checksum"], base["fix"], base["store"]), (1.0, 1.0, 0.5))  # changed, PR text, context
        self.assertEqual(retr._seed_query(sd, "fix totals", RetrievalConfig(pr_text_weight=0.0)).get("fix", 0.0), 0.0)
        self.assertNotIn("store", retr._seed_query(sd, "", RetrievalConfig(context_weight=0.0)))


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


class StructuralTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = build_from_files("r", [{"path": p, "content": c} for p, c in REPO.items()], confidence={})
        cls.snap = GraphSnapshot(cls.g)

    def test_plan_driven_relations(self):
        from backend.retrieval.structural import expand

        sig = git_diff("app/orders.py", "@@ -3,1 +3,1 @@", ["-def place_order(payload, store: Store):",
                                                            "+def place_order(payload, store: Store, retry=False):"])
        seeds = link(self.snap, parse_unified_diff(sig), REPO.get).seeds
        out = {self.snap.symbols[i].qualname: ch for i, _s, ch in expand(self.snap, seeds)}
        self.assertEqual(out["handle"], "structural:caller")  # signature change -> callers
        self.assertEqual(out["Store"], "structural:callee_on_changed_line")  # annotation on the changed def line
        self.assertEqual(out["checksum"], "structural:callee")
        ranked = [self.snap.symbols[i].qualname for i, _s, _c in expand(self.snap, seeds)]
        self.assertLess(ranked.index("handle"), ranked.index("checksum"))  # unplanned callees come after callers

    def test_rrf(self):
        from backend.retrieval.structural import rrf

        fused = rrf({"a": [(1, 9.0, "a"), (2, 5.0, "a")], "b": [(2, 1.0, "b"), (3, 0.5, "b")]}, k=60)
        self.assertEqual([i for i, _s, _l in fused], [2, 1, 3])
        self.assertEqual(fused[0][2], "a+b")

    def test_structural_pipeline_includes_same_file_and_cross_file_callees(self):
        files = dict(REPO)
        files["app/orders.py"] += "\ndef audit(order):\n    return cancel_order(order)\n"
        g = build_from_files("r", [{"path": p, "content": c} for p, c in files.items()], confidence={})
        retr = RepoRetriever(g, RetrievalConfig(channels=("structural", "lexical")))
        d = git_diff("app/orders.py", "@@ -11,1 +11,1 @@", ["-    return cancel_order(order)", "+    return cancel_order(order) or None"])
        keys = [it.key for it in retr.build_context(d, files.get).items]
        self.assertEqual(keys[0], "app/orders.py::audit")
        self.assertIn("app/orders.py::cancel_order", keys)  # same-file callee: eligible for the structural channel
        xfile = RetrievalConfig(channels=("structural",), structural_same_file=False)
        keys = [it.key for it in retr.build_context(d, files.get, config=xfile).items]
        self.assertNotIn("app/orders.py::cancel_order", keys)  # ablation: other files only
        self.assertEqual(keys[0], "app/orders.py::audit")  # the seed itself is still packed


class OverlayAndCoChangeTest(unittest.TestCase):
    def test_overlay_sees_calls_added_by_the_change(self):
        from backend.kg.extract_python import extract

        files = dict(REPO)
        files["app/store.py"] += "def audit_log(event):\n    return event\n"
        facts = [extract(p, c) for p, c in files.items()]
        from backend.kg.build import build_graph

        retr = RepoRetriever(build_graph("r", facts, confidence={}), RetrievalConfig(channels=("structural",)), facts=facts)
        head = files["app/orders.py"].replace("from app.store import Store, checksum",
                                              "from app.store import Store, audit_log, checksum")
        head = head.replace("    total = checksum(payload)\n", "    total = checksum(payload)\n    audit_log(total)\n")
        d = (git_diff("app/orders.py", "@@ -1,1 +1,1 @@", ["-from app.store import Store, checksum",
                                                         "+from app.store import Store, audit_log, checksum"])
             + "@@ -4,1 +4,2 @@\n     total = checksum(payload)\n+    audit_log(total)\n")
        provider = lambda p: head if p == "app/orders.py" else files.get(p)  # noqa: E731
        with_overlay = [it.key for it in retr.build_context(d, provider).items]
        self.assertIn("app/store.py::audit_log", with_overlay)  # a call that exists only at HEAD
        without = [it.key for it in retr.build_context(d, provider, config=RetrievalConfig(channels=("structural",), overlay=False)).items]
        self.assertNotIn("app/store.py::audit_log", without)

    def test_cochange_index(self):
        from backend.retrieval.cochange import CoChangeIndex

        cc = CoChangeIndex(max_files=3, min_support=2)
        for files in (["a.py", "b.py"], ["a.py", "b.py", "c.py"], ["a.py", "c.py"], ["a.py", "b.py", "c.py", "d.py"]):
            cc.add(files)  # the 4-file commit is ignored (> max_files)
        self.assertEqual(cc.related("a.py"), [("b.py", round(2 / 3, 4)), ("c.py", round(2 / 3, 4))])
        self.assertEqual(cc.related("d.py"), [])

    def test_cochange_channel_offers_coupled_files(self):
        from backend.retrieval.cochange import CoChangeIndex

        g = build_from_files("r", [{"path": p, "content": c} for p, c in REPO.items()], confidence={})
        retr = RepoRetriever(g, RetrievalConfig(channels=("cochange",)))
        cc = CoChangeIndex()
        for _ in range(3):
            cc.add(["app/orders.py", "app/api.py"])
        d = git_diff("app/orders.py", "@@ -8,1 +8,1 @@", ["-    return order_id", "+    return str(order_id)"])
        keys = [it.key for it in retr.build_context(d, REPO.get, cochange=cc.related).items]
        self.assertIn("app/api.py::handle", keys)


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
