from math import exp, isfinite, log, prod
import random
import unittest

from watermelon.experiments import (EPS, PriceObservation, Trend, causal_features,
                                    entry_flat, entry_slow, extend_flat, extend_slow)


class PredicateTests(unittest.TestCase):
    def test_flat_epsilon_boundaries(self):
        self.assertTrue(entry_flat(Trend(.002 + EPS, None, .02 + EPS)))
        self.assertTrue(entry_flat(Trend(-.002 - EPS, None, .02)))
        self.assertFalse(entry_flat(Trend(.002 + 2 * EPS, None, .02)))
        self.assertFalse(entry_flat(Trend(0, None, .02 + 2 * EPS)))

    def test_d_is_declining_speed_entry(self):
        self.assertTrue(entry_slow(Trend(-.005 - EPS, -.005 - EPS)))
        self.assertTrue(entry_slow(Trend(-.004, -.02)))
        self.assertFalse(entry_slow(Trend(-.005 - 2 * EPS, -.001)))
        self.assertFalse(entry_slow(Trend(-.004, -.006)))

    def test_extensions_rising_speed_and_strict_boundary(self):
        self.assertFalse(extend_flat(Trend(.002 + EPS)))
        self.assertTrue(extend_flat(Trend(.002 + 2 * EPS)))
        self.assertTrue(extend_slow(Trend(.003, .002 + EPS)))
        self.assertTrue(extend_slow(Trend(.01, .02)))
        self.assertFalse(extend_slow(Trend(.009, .02)))
        self.assertFalse(extend_slow(Trend(-.004, -.02)))

    def test_missing_is_not_a_signal(self):
        for x in (None, float("nan"), float("inf")):
            self.assertFalse(entry_flat(Trend(x, 0, 0)))
            self.assertFalse(entry_flat(Trend(0, 0, x)))
            self.assertFalse(entry_slow(Trend(0, x, 0)))
            self.assertFalse(extend_flat(Trend(x)))
            self.assertFalse(extend_slow(Trend(.003, x)))


class FeatureTests(unittest.TestCase):
    def test_compound_rates_not_raw_price_change(self):
        rows = [PriceObservation(10.1, 10) for _ in range(9)]
        result = causal_features(rows)
        self.assertIsNone(result[2].r3)
        self.assertAlmostEqual(result[3].r3, .01, places=14)
        self.assertIsNone(result[7].p5)
        self.assertAlmostEqual(result[8].p5, .01, places=14)
        self.assertAlmostEqual(result[8].range4, 1.01 ** 3 - 1, places=14)

    def test_reference_chain_handles_ex_date_without_future_adjustment(self):
        # 人工除权例：原始 close 跳变，close/pre_close 连续收益为零。
        rows = [PriceObservation(10, 10)] * 4 + [PriceObservation(5, 5)] * 5
        self.assertEqual(causal_features(rows)[-1], Trend(0, 0, 0))

    def test_missing_and_invalid_values_break_segment(self):
        for bad in (PriceObservation(None, 10), PriceObservation(10, 0),
                    PriceObservation(float("nan"), 10), PriceObservation(10, 10, False)):
            result = causal_features([PriceObservation(10, 10)] * 9 + [bad]
                                      + [PriceObservation(10, 10)] * 9)
            self.assertIsNone(result[9].r3)
            self.assertIsNone(result[12].r3)
            self.assertEqual(result[13].r3, 0)
            self.assertIsNone(result[17].p5)
            self.assertEqual(result[18].p5, 0)

    def test_appending_or_perturbing_future_keeps_prefix(self):
        prefix = [PriceObservation(10 + i / 100, 10) for i in range(20)]
        suffix = [PriceObservation(999, 1), PriceObservation(None, None)] * 10
        self.assertEqual(causal_features(prefix), causal_features(prefix + suffix)[:len(prefix)])

    def test_independent_local_log_oracle_on_synthetic_paths(self):
        rng = random.Random(610125)
        ratios = [rng.uniform(.94, 1.06) for _ in range(180)]
        rows = [PriceObservation(r * 10, 10) if i % 31 != 15 else PriceObservation(None, None)
                for i, r in enumerate(ratios)]
        features = causal_features(rows)
        for i, feature in enumerate(features):
            good3 = i >= 3 and all(r.close is not None for r in rows[i - 3:i + 1])
            good8 = i >= 8 and all(r.close is not None for r in rows[i - 8:i + 1])
            if not good3:
                self.assertIsNone(feature.r3)
                self.assertIsNone(feature.range4)
            else:
                r3 = exp(sum(log(ratios[j]) for j in range(i - 2, i + 1)) / 3) - 1
                back = [1.0] + [1 / prod(ratios[i - n + 1:i + 1]) for n in range(1, 4)]
                self.assertAlmostEqual(feature.r3, r3, places=12)
                self.assertAlmostEqual(feature.range4, max(back) / min(back) - 1, places=12)
            if not good8:
                self.assertIsNone(feature.p5)
            else:
                p5 = exp(sum(log(ratios[j]) for j in range(i - 7, i - 2)) / 5) - 1
                self.assertAlmostEqual(feature.p5, p5, places=12)


if __name__ == "__main__":
    unittest.main()
