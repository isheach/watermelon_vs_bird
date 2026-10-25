from dataclasses import replace
from datetime import timedelta
import unittest

from watermelon.ranking import DataQualityError, eligible, float32, rank_universe, turn20
from support import bars, coverage, dates, state


class TurnoverTests(unittest.TestCase):
    def test_twenty_records_eighteen_valid_days(self):
        rows = list(bars(2))
        rows[3] = replace(rows[3], suspended=True, volume=0, turnover_pct=None)
        rows[8] = replace(rows[8], volume=0, turnover_pct=None)
        result = turn20(dates(), rows)
        self.assertEqual((result.score, result.valid_days), (2, 18))

    def test_missing_is_not_suspension(self):
        with self.assertRaises(DataQualityError):
            turn20(dates(), bars()[:-1])
        with self.assertRaises(DataQualityError):
            turn20(dates(), bars()[:-1] + (bars()[0],))

    def test_invalid_calendar(self):
        for window in (dates()[1:], tuple(reversed(dates())), dates()[:-1] + (dates()[0],)):
            with self.subTest(window=window), self.assertRaises(DataQualityError):
                turn20(window, bars())

    def test_active_anomalies_block(self):
        for changes in ({"turnover_pct": None}, {"turnover_pct": float("nan")},
                        {"turnover_pct": -1}, {"volume": None}, {"volume": -1},
                        {"suspended": None}, {"pit_verified": False},
                        {"suspended": True}):
            rows = list(bars())
            rows[4] = replace(rows[4], **changes)
            with self.subTest(changes=changes), self.assertRaises(DataQualityError):
                turn20(dates(), rows)

    def test_dated_float_shares_in_shares_and_percent(self):
        rows = tuple(replace(row, turnover_pct=None, float_shares=100000) for row in bars())
        self.assertEqual(turn20(dates(), rows).score, 1)
        rows = tuple(replace(row, float_shares=200000 if i >= 10 else 100000)
                     for i, row in enumerate(rows))
        self.assertEqual(turn20(dates(), rows).score, .75)

    def test_all_inactive_is_not_zero_score(self):
        rows = tuple(replace(row, suspended=True, volume=None, turnover_pct=None) for row in bars())
        result = turn20(dates(), rows)
        self.assertIsNone(result.score)
        self.assertEqual(result.valid_days, 0)

    def test_float32_score(self):
        self.assertEqual(turn20(dates(), bars(.123456789)).score, float32(.123456789))
        with self.assertRaises(DataQualityError):
            float32(1e100)


class RankingTests(unittest.TestCase):
    def test_pit_listing_and_delisting(self):
        day = dates()[-1]
        self.assertFalse(eligible(state(listing_date=day - timedelta(days=364)), day))
        self.assertTrue(eligible(state(listing_date=day - timedelta(days=365)), day))
        self.assertFalse(eligible(state(known_delisting_date=day), day))
        self.assertTrue(eligible(state(known_delisting_date=day + timedelta(days=1)), day))

    def test_exclusions(self):
        for change in ({"exchange": "BSE"}, {"is_a_share": False}, {"is_st": True},
                       {"suspended": True}, {"volume": 0}, {"close": 0}, {"total_mcap": 0}):
            self.assertFalse(eligible(state(**change), dates()[-1]))

    def test_uncertainty_is_not_eligible_or_harmless(self):
        for change in ({"is_st": None}, {"suspended": None}, {"listing_date": None},
                       {"pit_verified": False}, {"as_of": dates()[-2]}, {"volume": None},
                       {"close": float("nan")}, {"total_mcap": None}):
            with self.subTest(change=change), self.assertRaises(DataQualityError):
                eligible(state(**change), dates()[-1])

    def test_tie_after_float32_breaks_by_code(self):
        result = rank_universe(19, dates(), coverage("SYN002", "SYN001"),
                               [state("SYN002"), state("SYN001")],
                               {"SYN002": bars(1), "SYN001": bars(1.00000001)})
        self.assertEqual(result.codes, ("SYN001", "SYN002"))
        self.assertEqual(result.rows[0].score, result.rows[1].score)

    def test_incomplete_roster_blocks_full_ranking(self):
        for cover, states in ((coverage("SYN001", "SYN002"), [state()]),
                              (coverage("SYN001", full_pool_verified=False), [state()]),
                              (coverage("SYN001"), [state(), state()])):
            with self.assertRaises(DataQualityError):
                rank_universe(19, dates(), cover, states, {"SYN001": bars()})

    def test_missing_eligible_history_blocks(self):
        with self.assertRaises(DataQualityError):
            rank_universe(19, dates(), coverage("SYN001"), [state()], {})

    def test_known_ineligible_does_not_need_price_history(self):
        result = rank_universe(19, dates(), coverage("SYN001"), [state(is_st=True)], {})
        self.assertEqual(result.codes, ())

    def test_state_history_disagreement_blocks(self):
        with self.assertRaises(DataQualityError):
            rank_universe(19, dates(), coverage("SYN001"), [state(volume=2000)], {"SYN001": bars()})

    def test_small_pool_stays_small(self):
        result = rank_universe(19, dates(), coverage("SYN001"), [state()], {"SYN001": bars()})
        self.assertEqual(len(result.rows), 1)


if __name__ == "__main__":
    unittest.main()
