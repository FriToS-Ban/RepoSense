import math
import unittest

from eval.metrics import dcg, ir_metrics, mean_metrics
from eval.stats import bootstrap_ci, holm, paired_randomization_test


class MetricsTest(unittest.TestCase):
    def test_hand_computed_example(self):
        # duplicates count once: effective ranking a, x, b, y  -> relevance 2, 0, 2, 0
        m = ir_metrics(["a", "x", "b", "a", "y"], {"a": 2, "b": 2, "c": 1}, ks=(1, 5))
        self.assertAlmostEqual(m["P@1"], 1.0)
        self.assertAlmostEqual(m["P@5"], 2 / 5)
        self.assertAlmostEqual(m["R@5"], 2 / 3)
        self.assertEqual(m["Hit@5"], 1.0)
        self.assertEqual(m["MRR"], 1.0)
        dcg5 = 3 / math.log2(2) + 3 / math.log2(4)
        idcg5 = 3 / math.log2(2) + 3 / math.log2(3) + 1 / math.log2(4)
        self.assertAlmostEqual(m["nDCG@5"], dcg5 / idcg5)
        self.assertAlmostEqual(m["P@all"], 0.5)
        self.assertAlmostEqual(m["R@all"], 2 / 3)
        self.assertEqual(m["n"], 4)

    def test_first_relevant_late(self):
        m = ir_metrics(["x", "y", "a"], {"a": 1}, ks=(2,))
        self.assertEqual(m["MRR"], 1 / 3)
        self.assertEqual(m["R@2"], 0.0)
        self.assertEqual(m["nDCG@2"], 0.0)
        self.assertEqual(m["R@all"], 1.0)

    def test_no_judgments_means_skip(self):
        self.assertIsNone(ir_metrics(["a"], {}))

    def test_empty_ranking(self):
        m = ir_metrics([], {"a": 2}, ks=(5,))
        self.assertEqual((m["R@5"], m["MRR"], m["P@all"], m["n"]), (0.0, 0.0, 0.0, 0))

    def test_dcg_and_mean(self):
        self.assertAlmostEqual(dcg([1, 1]), 1 + 1 / math.log2(3))
        self.assertEqual(mean_metrics([{"R": 1.0}, {"R": 0.0}, {"R": 0.5}]), {"R": 0.5})


class StatsTest(unittest.TestCase):
    def test_bootstrap_constant(self):
        self.assertEqual(bootstrap_ci([0.3] * 20), (0.3, 0.3, 0.3))

    def test_bootstrap_brackets_mean_and_is_deterministic(self):
        vals = [i / 10 for i in range(11)]
        mean, lo, hi = bootstrap_ci(vals, seed=1)
        self.assertLessEqual(lo, mean)
        self.assertLessEqual(mean, hi)
        self.assertEqual(bootstrap_ci(vals, seed=1), (mean, lo, hi))

    def test_randomization_identical_and_separated(self):
        self.assertEqual(paired_randomization_test([0.2, 0.4], [0.2, 0.4]), (0.0, 1.0))
        delta, p = paired_randomization_test([1.0] * 30, [0.0] * 30, n_permutations=2000)
        self.assertEqual(delta, 1.0)
        self.assertLess(p, 0.001)

    def test_randomization_noise_is_not_significant(self):
        a = [0.1, 0.9, 0.4, 0.6, 0.5, 0.5]
        b = [0.9, 0.1, 0.6, 0.4, 0.5, 0.5]
        _, p = paired_randomization_test(a, b, n_permutations=2000)
        self.assertGreater(p, 0.5)

    def test_holm(self):
        adjusted = holm([0.01, 0.04, 0.03])
        for got, want in zip(adjusted, [0.03, 0.06, 0.06]):
            self.assertAlmostEqual(got, want)


if __name__ == "__main__":
    unittest.main()
