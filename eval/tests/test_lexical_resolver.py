import unittest

from eval.lexical import BM25, tokenize
from eval.reference import RefGraph

FIXTURE = {
    "pkg/__init__.py": "from .core import Engine\n__all__ = ['Engine']\n",
    "pkg/base.py": (
        "class Base:\n"
        "    def run(self):\n"
        "        return self.step()\n"
        "    def step(self):\n"
        "        return 1\n"
    ),
    "pkg/util.py": "def helper():\n    return 2\n\ndef fmt(v):\n    return str(v)\n",
    "pkg/core.py": (
        "from .base import Base\n"
        "from . import util\n"
        "from .util import *\n"
        "\n"
        "class Engine(Base):\n"
        "    def step(self):\n"
        "        x = helper()\n"
        "        y = util.fmt(x)\n"
        "        return super().step() + len(y)\n"
        "    def go(self, data: Base):\n"
        "        return self.run()\n"
    ),
    "app.py": (
        "from pkg import Engine\n"
        "import pkg.util as u\n"
        "\n"
        "def main():\n"
        "    e = Engine()\n"
        "    u.fmt(1)\n"
        "    helper = lambda: 0\n"
        "    helper()\n"
        "    return e\n"
    ),
}


def refs(rg, sid):
    return {t for t, _ in rg.refs[sid]}


class ResolverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rg = RefGraph([{"path": p, "content": c} for p, c in FIXTURE.items()])

    def test_star_import_relative_module_and_super(self):
        r = refs(self.rg, ("pkg/core.py", "Engine.step"))
        self.assertIn(("pkg/util.py", "helper"), r)  # via `from .util import *`
        self.assertIn(("pkg/util.py", "fmt"), r)  # via `from . import util` + attribute
        self.assertIn(("pkg/base.py", "Base.step"), r)  # via super()

    def test_self_call_resolves_through_mro_and_annotations_are_references(self):
        r = refs(self.rg, ("pkg/core.py", "Engine.go"))
        self.assertIn(("pkg/base.py", "Base.run"), r)
        self.assertIn(("pkg/base.py", "Base"), r)
        self.assertEqual(self.rg.classes[("pkg/core.py", "Engine")]["bases"], [("pkg/base.py", "Base")])

    def test_reexport_alias_import_and_local_shadowing(self):
        r = refs(self.rg, ("app.py", "main"))
        self.assertIn(("pkg/core.py", "Engine"), r)  # re-exported by pkg/__init__.py
        self.assertIn(("pkg/util.py", "fmt"), r)  # `import pkg.util as u`
        self.assertNotIn(("pkg/util.py", "helper"), r)  # local lambda shadows the name

    def test_callers_and_edge_sets(self):
        self.assertEqual(self.rg.callers[("pkg/util.py", "helper")], {("pkg/core.py", "Engine.step")})
        cross, same = self.rg.call_pairs()
        self.assertIn(("app.py", "pkg/util.py", "fmt"), cross)
        self.assertIn(("pkg/base.py", "pkg/base.py", "step"), same)  # self.step() in Base.run
        self.assertIn(("pkg/core.py", "pkg/base.py"), self.rg.import_file_pairs())
        self.assertIn((("pkg/core.py", "Engine"), ("pkg/base.py", "Base")), self.rg.inherit_pairs())


class LexicalTest(unittest.TestCase):
    def test_tokenize_splits_identifiers_and_drops_keywords(self):
        toks = tokenize("def getHTTPResponse_code(self): return None")
        self.assertEqual(toks, ["gethttpresponse_code", "get", "http", "response", "code"])

    def test_bm25_ranks_matching_document_first(self):
        bm = BM25([("a", "parse the config file"), ("b", "send http request"), ("c", "config loader config")])
        self.assertEqual([d for d, _ in bm.search("load config", 3)], ["c", "a"])

    def test_bm25_ties_break_by_insertion_order(self):
        bm = BM25([("first", "alpha beta"), ("second", "alpha beta"), ("other", "gamma")])
        self.assertEqual([d for d, _ in bm.search("alpha", 2)], ["first", "second"])


if __name__ == "__main__":
    unittest.main()
