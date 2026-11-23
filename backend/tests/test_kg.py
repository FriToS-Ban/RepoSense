"""Tests for the knowledge-graph v2 package (backend/kg)."""
import tempfile
import unittest
from pathlib import Path

from eval import offline

offline.bootstrap(Path(tempfile.mkdtemp(prefix="reposense-test-")) / "test.sqlite")

from backend.core.database import SessionLocal  # noqa: E402
from backend.kg.build import build_from_files, validate  # noqa: E402
from backend.kg.derive import c3_mro  # noqa: E402
from backend.kg.extract_python import extract  # noqa: E402
from backend.kg.model import EdgeType, Flag, Kind, Tier, symbol_id  # noqa: E402
from backend.kg.snapshot import GraphSnapshot  # noqa: E402
from backend.kg.store import KGInvariantError, index_files, load_graph  # noqa: E402
from backend.models.models import KGVectorOutbox  # noqa: E402

REPO = "fixture"
FIX = {
    "app/__init__.py": "from .models import User\nfrom .util import *\n",
    "app/models.py": (
        "from abc import ABC, abstractmethod\n"
        "class Base(ABC):\n"
        "    @abstractmethod\n"
        "    def save(self):\n"
        "        ...\n"
        "    def run(self):\n"
        "        return self.save()\n"
        "class User(Base):\n"
        "    repo: 'Repo'\n"
        "    def __init__(self, repo: 'Repo'):\n"
        "        self.repo = repo\n"
        "        self.cache = Cache()\n"
        "    def save(self):\n"
        "        self.cache.put(self)\n"
        "        return super().run()\n"
        "class Repo:\n"
        "    def put(self, x):\n"
        "        return x\n"
        "class Cache:\n"
        "    def put(self, x):\n"
        "        return None\n"
    ),
    "app/util.py": "def helper(x):\n    return x\n\ndef fmt(v):\n    return str(v).upper()\n",
    "app/service.py": (
        "import os\n"
        "from app.models import User, Repo\n"
        "from . import util\n"
        "from .util import helper, fmt\n"
        "from .util import helper as h\n"
        "\n"
        "def make(repo: Repo):\n"
        "    u = User(repo)\n"
        "    u.save()\n"
        "    repo.put(u)\n"
        "    util.fmt(1)\n"
        "    h(2)\n"
        "    os.path.join('a', 'b')\n"
        "    'x'.join([])\n"
        "    return u\n"
        "\n"
        "def shadow():\n"
        "    helper = lambda: 0\n"
        "    return helper()\n"
        "\n"
        "def local_import():\n"
        "    from json import dumps as fmt\n"
        "    return fmt({})\n"
        "\n"
        "def unknown(client):\n"
        "    return client.put(1)\n"
    ),
    "app/state.py": (
        "class _Unset:\n"
        "    def clean(self, d):\n"
        "        return d\n"
        "UNSET = _Unset()\n"
        "class Conn:\n"
        "    def fetch(self) -> 'Row':\n"
        "        return Row()\n"
        "class Row:\n"
        "    def value(self):\n"
        "        return 1\n"
        "def get_conn() -> Conn:\n"
        "    return Conn()\n"
        "class Factory:\n"
        "    @classmethod\n"
        "    def build(cls):\n"
        "        return cls.make()\n"
        "    @classmethod\n"
        "    def make(cls):\n"
        "        return cls()\n"
    ),
    "app/uses.py": (
        "from .state import UNSET, get_conn, Row\n"
        "def uses_state(d: dict, items: list['Row']):\n"
        "    UNSET.clean(d)\n"
        "    c = get_conn()\n"
        "    r = c.fetch()\n"
        "    r.value()\n"
        "    d.get('x')\n"
        "    return items\n"
    ),
    "app/api.py": "from app import User, helper\n\ndef get():\n    return User(None)\n\ndef h2():\n    return helper(1)\n",
    "app/generic.py": (
        "from typing import Generic, TypeVar\n"
        "T = TypeVar('T')\n"
        "class Box(Generic[T]):\n"
        "    def get(self):\n"
        "        return 1\n"
        "class IntBox(Box[int]):\n"
        "    def show(self):\n"
        "        return self.get()\n"
    ),
    "tests/test_service.py": (
        "from app.service import make\n"
        "def _setup():\n"
        "    return make(None)\n"
        "def test_make():\n"
        "    assert make(None)\n"
        "def test_indirect():\n"
        "    assert _setup()\n"
    ),
    "src/pkg/__init__.py": "",
    "src/pkg/core.py": "def core_fn():\n    return 1\n",
    "src/pkg/use.py": "from pkg.core import core_fn\n\ndef go():\n    return core_fn()\n",
    "nsroot/sub/mod.py": "from nsroot.sub.other import thing\n\ndef call():\n    return thing()\n",
    "nsroot/sub/other.py": "def thing():\n    return 1\n",
}


def files(overrides=None, drop=()):
    d = {**FIX, **(overrides or {})}
    return [{"path": p, "content": c} for p, c in d.items() if p not in drop]


def sid(path, qual):
    return symbol_id(REPO, path, qual)


class ExtractTest(unittest.TestCase):
    def test_overloads_properties_and_typecheck_stub_merge(self):
        src = (
            "from typing import TYPE_CHECKING, overload\n"
            "if TYPE_CHECKING:\n"
            "    def Lazy(x): ...\n"
            "else:\n"
            "    class Lazy:\n"
            "        def get(self): return 1\n"
            "class P:\n"
            "    @property\n"
            "    def x(self): return 1\n"
            "    @x.setter\n"
            "    def x(self, v): pass\n"
            "    @overload\n"
            "    def f(self, a: int) -> int: ...\n"
            "    def f(self, a): return a\n"
            "    class Inner:\n"
            "        def m(self):\n"
            "            def closure(): return 1\n"
            "            return closure()\n"
        )
        ff = extract("m.py", src)
        by = {s.qualname: s for s in ff.symbols}
        self.assertEqual(by["Lazy"].kind, Kind.CLASS)
        self.assertEqual(by["Lazy.get"].kind, Kind.METHOD)
        self.assertEqual(len(by["P.x"].spans), 2)
        self.assertTrue(by["P.x"].flags & Flag.PROPERTY)
        self.assertEqual(by["P.f"].signature.strip(), "def f(self, a): return a")
        self.assertIn("P.Inner.m", by)
        self.assertNotIn("P.Inner.m.closure", by)  # closures are part of their parent

    def test_relative_and_scoped_imports(self):
        ff = extract("pkg/sub/mod.py", "from ..a import b\nfrom . import c\ndef f():\n    import json\n")
        mods = [(i.scope, i.module, i.name) for i in ff.imports]
        self.assertIn(("", "pkg.a", "b"), mods)
        self.assertIn(("", "pkg.sub", "c"), mods)
        self.assertIn(("f", "json", None), mods)

    def test_test_flags_and_type_env(self):
        ff = extract("tests/test_x.py", "def test_a(c: 'Client'):\n    s = Session()\n    return s\n")
        f = next(s for s in ff.symbols if s.qualname == "test_a")
        self.assertTrue(f.flags & Flag.IS_TEST and f.flags & Flag.IN_TEST_FILE)
        self.assertEqual(f.type_env["c"], [["Client", []]])
        self.assertEqual(f.type_env["s"], [["Session", []]])

    def test_syntax_error_yields_module_only(self):
        ff = extract("bad.py", "def (:\n")
        self.assertIsNotNone(ff.parse_error)
        self.assertEqual([s.kind for s in ff.symbols], [Kind.MODULE])


class ResolveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = build_from_files(REPO, files(), confidence={})

    def edge(self, sp, sq, dp, dq, etype):
        return self.g.edges.get((sid(sp, sq), sid(dp, dq), etype))

    def test_tiers(self):
        S, M, U = "app/service.py", "app/models.py", "app/util.py"
        self.assertEqual(self.edge(S, "make", M, "User", EdgeType.CALLS).role, "instantiate")
        self.assertEqual(self.edge(S, "make", M, "User.save", EdgeType.CALLS).resolution, Tier.TYPE_INFERRED)
        self.assertEqual(self.edge(S, "make", M, "Repo.put", EdgeType.CALLS).resolution, Tier.TYPE_INFERRED)
        self.assertEqual(self.edge(S, "make", U, "fmt", EdgeType.CALLS).resolution, Tier.IMPORT)
        self.assertEqual(self.edge(S, "make", U, "helper", EdgeType.CALLS).resolution, Tier.IMPORT)  # alias h
        self.assertEqual(self.edge(M, "Base.run", M, "Base.save", EdgeType.CALLS).resolution, Tier.MEMBER)
        e = self.edge(M, "User.save", M, "Base.run", EdgeType.CALLS)
        self.assertEqual((e.resolution, e.role), (Tier.MEMBER, "super_call"))
        self.assertEqual(self.edge(M, "User.save", M, "Cache.put", EdgeType.CALLS).resolution, Tier.TYPE_INFERRED)
        self.assertIsNone(self.edge(M, "User.save", M, "Repo.put", EdgeType.CALLS))

    def test_shadowing_external_and_literals(self):
        S, U = "app/service.py", "app/util.py"
        self.assertIsNone(self.edge(S, "shadow", U, "helper", EdgeType.CALLS))
        self.assertIsNone(self.edge(S, "local_import", U, "fmt", EdgeType.CALLS))
        targets = {self.g.symbols[e.dst].qualname for e in self.g.edges.values() if e.src == sid(S, "make")}
        self.assertNotIn("join", {t.split(".")[-1] for t in targets})  # os.path.join / 'x'.join are not guessed

    def test_r5_fallback_is_scoped_and_split(self):
        S, M = "app/service.py", "app/models.py"
        a = self.edge(S, "unknown", M, "Repo.put", EdgeType.CALLS)
        b = self.edge(S, "unknown", M, "Cache.put", EdgeType.CALLS)
        self.assertEqual((a.resolution, a.confidence), (Tier.FALLBACK, 0.25))
        self.assertEqual((b.resolution, b.confidence), (Tier.FALLBACK, 0.25))

    def test_reexport_star_src_layout_and_namespace_packages(self):
        self.assertIsNotNone(self.edge("app/api.py", "get", "app/models.py", "User", EdgeType.CALLS))
        self.assertIsNotNone(self.edge("app/api.py", "h2", "app/util.py", "helper", EdgeType.CALLS))
        self.assertIsNotNone(self.edge("src/pkg/use.py", "go", "src/pkg/core.py", "core_fn", EdgeType.CALLS))
        self.assertIsNotNone(self.edge("nsroot/sub/mod.py", "call", "nsroot/sub/other.py", "thing", EdgeType.CALLS))
        self.assertIsNotNone(self.edge("app/service.py", "", "app/util.py", "", EdgeType.IMPORTS))
        self.assertEqual(self.edge("app/service.py", "make", "app/models.py", "Repo", EdgeType.REFERENCES).role, "annotation")

    def test_r4_module_instances_and_return_annotations(self):
        U, ST = "app/uses.py", "app/state.py"
        for target in ("_Unset.clean", "Conn.fetch", "Row.value"):
            self.assertEqual(self.edge(U, "uses_state", ST, target, EdgeType.CALLS).resolution, Tier.TYPE_INFERRED)

    def test_no_fallback_on_builtin_method_names_and_no_self_class_edge(self):
        U, ST = "app/uses.py", "app/state.py"
        names = {self.g.symbols[e.dst].name for e in self.g.edges.values() if e.src == sid(U, "uses_state")}
        self.assertNotIn("get", names)
        self.assertIsNotNone(self.edge(ST, "Factory.build", ST, "Factory.make", EdgeType.CALLS))
        self.assertIsNone(self.edge(ST, "Factory.build", ST, "Factory", EdgeType.REFERENCES))
        self.assertEqual(self.edge(U, "uses_state", ST, "Row", EdgeType.REFERENCES).role, "annotation")  # list['Row']

    def test_generic_bases_are_inherited(self):
        G = "app/generic.py"
        self.assertIsNotNone(self.edge(G, "IntBox", G, "Box", EdgeType.INHERITS))
        self.assertEqual(self.edge(G, "IntBox.show", G, "Box.get", EdgeType.CALLS).resolution, Tier.MEMBER)

    def test_derived_edges(self):
        M = "app/models.py"
        self.assertIsNotNone(self.edge(M, "User", M, "Base", EdgeType.INHERITS))
        self.assertEqual(self.edge(M, "User.save", M, "Base.save", EdgeType.OVERRIDES).role, "implements_abstract")
        T, S = "tests/test_service.py", "app/service.py"
        self.assertEqual(self.edge(T, "test_make", S, "make", EdgeType.TESTS).confidence, 1.0)
        self.assertEqual(self.edge(T, "test_indirect", S, "make", EdgeType.TESTS).role, "via_helper")
        self.assertEqual(self.edge(T, "", S, "", EdgeType.TESTS).role, "naming")

    def test_invariants_hold_and_detect_tampering(self):
        self.assertEqual(validate(self.g), [])
        g2 = build_from_files(REPO, files(), confidence={})
        g2.symbols[sid("app/util.py", "fmt")].end_line = 1000
        g2.symbols[sid("app/util.py", "helper")].start_line = 3  # helper spans 1-2
        k = next(iter(g2.edges))
        g2.edges[(k[0], "nope", k[2])] = g2.edges[k].__class__(k[0], "nope", k[2], 1.0, Tier.LOCAL, "call", 1)
        v = validate(g2)
        self.assertTrue(any("dangling" in x for x in v))
        self.assertTrue(any("not inside its parent" in x for x in v))
        self.assertTrue(any("inverted span" in x for x in v))


class MroTest(unittest.TestCase):
    def test_c3_diamond_and_inconsistent(self):
        bases = {"D": ["B", "C"], "B": ["A"], "C": ["A"], "A": []}
        self.assertEqual(c3_mro("D", bases), ["D", "B", "C", "A"])
        bad = {"X": ["A", "B"], "Y": ["B", "A"], "Z": ["X", "Y"], "A": [], "B": []}
        self.assertEqual(c3_mro("Z", bad)[0], "Z")  # falls back instead of failing


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.db = SessionLocal()
        global REPO
        self._repo = REPO

    def tearDown(self):
        self.db.close()

    def _index(self, repo, fs, sha):
        return index_files(self.db, repo, fs, sha)

    def _same(self, a, b):
        self.assertEqual(set(a.symbols), set(b.symbols))
        self.assertEqual({k: (e.confidence, e.resolution, e.weight) for k, e in a.edges.items()},
                         {k: (round(e.confidence, 6), e.resolution, e.weight) for k, e in b.edges.items()})

    def test_idempotent_and_update_equivalent(self):
        repo = "store-a"
        st = self._index(repo, [dict(f) for f in files()], "c1")
        self.assertGreater(st.symbols_inserted, 0)
        again = self._index(repo, [dict(f) for f in files()], "c1")
        self.assertFalse(again.changed)
        self.assertEqual(again.extra["files_reparsed"], 0)
        changed = {"app/util.py": "def helper(x, y=0):\n    return x\n"}  # fmt removed, helper changed
        st2 = self._index(repo, [dict(f) for f in files(changed, drop=("app/api.py",))], "c2")
        self.assertEqual(st2.extra["files_reparsed"], 1)
        from backend.kg.build import build_from_files as fresh_build

        self._same(load_graph(self.db, repo), fresh_build(repo, files(changed, drop=("app/api.py",))))
        ops = {(r.symbol_id, r.op) for r in self.db.query(KGVectorOutbox).filter(KGVectorOutbox.repo_id == repo)}
        self.assertIn((symbol_id(repo, "app/util.py", "fmt"), "delete"), ops)
        self.assertIn((symbol_id(repo, "app/api.py", "get"), "delete"), ops)
        self.assertIn((symbol_id(repo, "app/util.py", "helper"), "upsert"), ops)

    def test_invariant_failure_writes_nothing(self):
        import backend.kg.store as store

        original = store.validate
        store.validate = lambda g: ["forced violation"]
        try:
            with self.assertRaises(KGInvariantError):
                self._index("store-b", files(), "c1")
        finally:
            store.validate = original
        self.db.rollback()
        self.assertEqual(load_graph(self.db, "store-b").symbols, {})


class SnapshotTest(unittest.TestCase):
    def test_neighbors_and_line_lookup(self):
        g = build_from_files(REPO, files(), confidence={})
        snap = GraphSnapshot(g)
        make = snap.index[sid("app/service.py", "make")]
        callees = {snap.symbols[j].qualname for j, _, _ in snap.neighbors(make, EdgeType.CALLS)}
        self.assertTrue({"User", "User.save", "Repo.put", "fmt", "helper"} <= callees)
        callers = {snap.symbols[j].qualname for j, _, _ in snap.neighbors(snap.index[sid("app/service.py", "make")], EdgeType.CALLS, "in")}
        self.assertIn("test_make", callers)
        self.assertEqual(snap.symbols[snap.innermost("app/models.py", 14)].qualname, "User.save")
        self.assertEqual(snap.symbols[snap.innermost("app/models.py", 1)].kind, Kind.MODULE)
        self.assertEqual(snap.mro[snap.index[sid("app/models.py", "User")]][1], snap.index[sid("app/models.py", "Base")])


if __name__ == "__main__":
    unittest.main()
