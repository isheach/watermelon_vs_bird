import json
from pathlib import Path
import unittest

from watermelon.config import FeeSchedule, decimal, integer, load_config


class ConfigTests(unittest.TestCase):
    def test_default(self):
        config = load_config(Path(__file__).resolve().parents[1] / "config" / "default.json")
        self.assertEqual((config.minimum_days, config.buy_n, config.exit_rank), (30, 10, 15))
        self.assertEqual(config.initial_cash, 100000)
        self.assertEqual(config.fees.minimum_commission, 5)
        self.assertEqual(str(config.participation_rate), "0.01")

    def test_invalid_numbers(self):
        for value in (True, None, "NaN", "Infinity"):
            with self.assertRaises(ValueError):
                decimal(value)
        for value in (True, 1.5, -1):
            with self.assertRaises(ValueError):
                integer(value)
        with self.assertRaises(ValueError):
            FeeSchedule(-1, 5, 0, 0, 0)


if __name__ == "__main__":
    unittest.main()
