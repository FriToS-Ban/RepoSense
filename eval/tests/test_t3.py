"""T3 helpers (no clones needed) and the frozen case set."""
import unittest

from eval.benchmarks import t3


def case(**kw):
    base = dict(cid="c", repo="r", pin="p", mutation="arg_swap", path="m.py", line=3, before="    f(a, b)",
                after="    f(b, a)", needed=["lib.py::f"], needed_rendering="any", description="")
    return t3.Case(**{**base, **kw})


class T3HelpersTest(unittest.TestCase):
    def test_make_diff_is_a_one_line_change(self):
        content = "def g(a, b):\n    x = 1\n    f(a, b)\n    return x\n"
        diff, head = t3.make_diff(case(), content)
        self.assertIn("-    f(a, b)\n+    f(b, a)\n", diff)
        self.assertIn("@@ -1,4 +1,4 @@", diff)
        self.assertEqual(head.split("\n")[2], "    f(b, a)")
        with self.assertRaises(AssertionError):
            t3.make_diff(case(before="    other()"), content)  # the pin drifted

    def test_sufficiency_respects_the_needed_rendering(self):
        from eval.run_t3 import sufficient

        self.assertTrue(sufficient(case(), {"lib.py::f": "compact"}))
        caller = case(mutation="add_param", needed=["api.py::h", "cli.py::k"], needed_rendering="callsite")
        self.assertFalse(sufficient(caller, {"api.py::h": "compact"}))  # a signature does not show the call
        self.assertTrue(sufficient(caller, {"cli.py::k": "callsite"}))

    def test_frozen_cases_are_consistent(self):
        cs = t3.load()
        self.assertEqual(len({c.cid for c in cs}), len(cs))
        self.assertTrue(all(c.mutation in t3.TYPES and c.needed and c.before != c.after for c in cs))


if __name__ == "__main__":
    unittest.main()
