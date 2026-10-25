from dataclasses import replace
import unittest

from watermelon.policy import Position, make_plan, ready_entries, record_fill
from watermelon.ranking import DataQualityError
from support import ranking


class PolicyTests(unittest.TestCase):
    def test_rank_and_age_boundaries(self):
        for rank, age, retain in ((10, 30, True), (15, 30, True), (16, 29, True),
                                   (16, 30, False), (16, 31, False)):
            pos = Position(f"SYN{rank:03}", 100, 100 - age)
            plan = make_plan(ranking(), [pos], 100)
            self.assertEqual(plan.decisions[0].retain, retain)

    def test_retained_not_rebought_or_rebalanced(self):
        pos = Position("SYN001", 700, 80)
        plan = make_plan(ranking(), [pos], 100)
        self.assertIn(pos.code, plan.retained)
        self.assertNotIn(pos.code, plan.entry_candidates + plan.exit_intents)
        self.assertEqual(pos.quantity, 700)

    def test_fill_only_from_top10(self):
        positions = [Position(f"SYN{i:03}", 100, 80) for i in range(1, 11)]
        plan = make_plan(ranking(), positions, 100)
        self.assertEqual(plan.entry_candidates, ())
        plan = make_plan(ranking(n=8), [], 100)
        self.assertEqual(len(plan.entry_candidates), 8)
        self.assertNotIn("SYN011", plan.entry_candidates)

    def test_unfilled_and_partial_exit_still_occupy_slot(self):
        positions = [Position(f"OLD{i:03}", 100, 60, "baseline") for i in range(10)]
        plan = make_plan(ranking(), positions, 100)
        self.assertEqual(plan.slots_before_fills, 0)
        self.assertEqual(len(plan.entry_candidates), 10)
        self.assertEqual(ready_entries(plan, positions), ())
        partial = record_fill(positions[0], code=positions[0].code, side="SELL",
                              quantity=50, execution_index=100)
        self.assertEqual(ready_entries(plan, [partial] + positions[1:]), ())
        self.assertEqual(ready_entries(plan, positions[1:]), ("SYN001",))

    def test_baseline_pending_exit_can_cancel(self):
        pos = Position("SYN015", 100, 0, "baseline")
        plan = make_plan(ranking(), [pos], 100)
        self.assertEqual(plan.cancel_pending, (pos.code,))
        self.assertEqual(plan.exit_intents, ())

    def test_weakening_pending_exit_cannot_cancel(self):
        pos = Position("SYN001", 100, 0, "weakening")
        plan = make_plan(ranking(), [pos], 100)
        self.assertEqual(plan.exit_intents, (pos.code,))
        self.assertEqual(plan.cancel_pending, ())
        self.assertNotIn(pos.code, plan.entry_candidates)

    def test_current_st_absent_rank_retained_by_minimum(self):
        # ST 已被全池资格筛掉，但持仓仍受原最短持有期冻结。
        pos = Position("KNOWN_ST", 100, 71)
        self.assertIn(pos.code, make_plan(ranking(), [pos], 100).retained)
        self.assertIn(pos.code, make_plan(ranking(), [replace(pos, entry_index=70)], 100).exit_intents)

    def test_exact_next_market_day(self):
        for execution in (99, 101):
            with self.assertRaises(ValueError):
                make_plan(ranking(), [], execution)
        self.assertEqual(make_plan(ranking(), [], 100).execution_index, 100)

    def test_incomplete_rank_cannot_trigger_exits(self):
        with self.assertRaises(DataQualityError):
            make_plan(replace(ranking(), full_pool_verified=False), [Position("OLD", 100, 0)], 100)

    def test_future_clock_duplicates_and_cap_rejected(self):
        pos = Position("SYN001", 100, 101)
        for positions in ([pos], [Position("A", 100, 0)] * 2,
                          [Position(str(i), 100, 0) for i in range(11)]):
            with self.assertRaises(ValueError):
                make_plan(ranking(), positions, 100)

    def test_failed_entry_never_starts_clock(self):
        self.assertIsNone(record_fill(None, code="SYN001", side="BUY", quantity=0, execution_index=100))
        pos = record_fill(None, code="SYN001", side="BUY", quantity=100, execution_index=105)
        self.assertEqual(pos.entry_index, 105)
        plan = make_plan(ranking(index=133), [replace(pos, code="OUTSIDE")], 134)
        self.assertTrue(plan.decisions[0].retain)

    def test_partial_fills_keep_clock_and_t_plus_one(self):
        pos = Position("A", 100, 80, "baseline")
        more = record_fill(pos, code="A", side="BUY", quantity=100, execution_index=100)
        self.assertEqual((more.quantity, more.entry_index, more.sellable(100)), (200, 80, 100))
        with self.assertRaises(ValueError):
            record_fill(more, code="A", side="SELL", quantity=200, execution_index=100)
        partial = record_fill(more, code="A", side="SELL", quantity=100, execution_index=100)
        self.assertEqual((partial.quantity, partial.entry_index, partial.pending_exit), (100, 80, "baseline"))
        self.assertEqual(partial.sellable(100), 0)
        self.assertEqual(partial.sellable(101), 100)
        self.assertIsNone(record_fill(partial, code="A", side="SELL", quantity=100, execution_index=101))

    def test_same_day_initial_partial_buys_remain_locked(self):
        pos = record_fill(None, code="A", side="BUY", quantity=100, execution_index=100)
        pos = record_fill(pos, code="A", side="BUY", quantity=100, execution_index=100)
        self.assertEqual(pos.sellable(100), 0)
        self.assertEqual(pos.sellable(101), 200)


class FillReviewRegressionTests(unittest.TestCase):
    def test_parent_cross_day_partial_sell_keeps_original_clock_and_pending(self):
        for pending in ("none", "baseline", "weakening"):
            with self.subTest(pending=pending):
                before = record_fill(Position("A", 100, 80, pending), code="A", side="BUY",
                                     quantity=300, execution_index=100)
                self.assertEqual((before.quantity, before.last_buy_quantity), (400, 300))
                after = record_fill(before, code="A", side="SELL", quantity=200, execution_index=101)
                self.assertEqual((after.quantity, after.entry_index, after.pending_exit), (200, 80, pending))
                self.assertEqual((after.last_buy_index, after.last_buy_quantity), (None, 0))
                self.assertEqual(after.sellable(101), 200)
                self.assertEqual((before.quantity, before.last_buy_quantity), (400, 300))

    def test_repeated_partial_sells_after_unlock(self):
        position = record_fill(Position("A", 100, 80, "baseline"), code="A", side="BUY",
                               quantity=300, execution_index=100)
        for remaining in (300, 200, 100):
            position = record_fill(position, code="A", side="SELL", quantity=100, execution_index=101)
            self.assertEqual((position.quantity, position.entry_index, position.pending_exit),
                             (remaining, 80, "baseline"))
            self.assertEqual((position.last_buy_index, position.last_buy_quantity), (None, 0))
        self.assertIsNone(record_fill(position, code="A", side="SELL", quantity=100, execution_index=101))

    def test_same_day_partial_sells_keep_new_lot_locked(self):
        position = record_fill(Position("A", 300, 80, "weakening"), code="A", side="BUY",
                               quantity=300, execution_index=100)
        for remaining in (500, 400, 300):
            position = record_fill(position, code="A", side="SELL", quantity=100, execution_index=100)
            self.assertEqual((position.quantity, position.last_buy_index, position.last_buy_quantity),
                             (remaining, 100, 300))
            self.assertEqual((position.entry_index, position.pending_exit), (80, "weakening"))
        self.assertEqual(position.sellable(100), 0)
        with self.assertRaisesRegex(ValueError, "T\\+1"):
            record_fill(position, code="A", side="SELL", quantity=1, execution_index=100)

    def test_cross_day_partial_then_rebuy_relocks_only_today_quantity(self):
        position = record_fill(Position("A", 100, 80), code="A", side="BUY", quantity=300, execution_index=100)
        position = record_fill(position, code="A", side="SELL", quantity=200, execution_index=101)
        position = record_fill(position, code="A", side="BUY", quantity=100, execution_index=101)
        self.assertEqual((position.quantity, position.sellable(101)), (300, 200))
        for _ in range(2):
            position = record_fill(position, code="A", side="SELL", quantity=100, execution_index=101)
            self.assertEqual((position.entry_index, position.last_buy_index, position.last_buy_quantity), (80, 101, 100))
        with self.assertRaisesRegex(ValueError, "T\\+1"):
            record_fill(position, code="A", side="SELL", quantity=1, execution_index=101)
        position = record_fill(position, code="A", side="SELL", quantity=50, execution_index=102)
        self.assertEqual((position.quantity, position.last_buy_index, position.last_buy_quantity), (50, None, 0))
        self.assertIsNone(record_fill(position, code="A", side="SELL", quantity=50, execution_index=102))

    def test_cross_day_rebuy_before_partial_sell_tracks_only_new_day(self):
        position = record_fill(Position("A", 100, 80, "baseline"), code="A", side="BUY",
                               quantity=300, execution_index=100)
        position = record_fill(position, code="A", side="BUY", quantity=200, execution_index=101)
        self.assertEqual((position.quantity, position.sellable(101), position.last_buy_quantity), (600, 400, 200))
        position = record_fill(position, code="A", side="SELL", quantity=400, execution_index=101)
        self.assertEqual((position.quantity, position.sellable(101)), (200, 0))
        position = record_fill(position, code="A", side="SELL", quantity=100, execution_index=102)
        self.assertEqual((position.quantity, position.entry_index, position.pending_exit), (100, 80, "baseline"))
        self.assertEqual((position.last_buy_index, position.last_buy_quantity), (None, 0))

    def test_partial_sell_normalization_does_not_restart_minimum_holding(self):
        position = record_fill(Position("OUTSIDE", 100, 80, "baseline"), code="OUTSIDE", side="BUY",
                               quantity=300, execution_index=100)
        position = record_fill(position, code="OUTSIDE", side="SELL", quantity=200, execution_index=101)
        at_29 = make_plan(ranking(index=108), [position], 109)
        at_30 = make_plan(ranking(index=109), [position], 110)
        self.assertEqual((at_29.decisions[0].completed_days, at_29.decisions[0].retain), (29, True))
        self.assertEqual((at_30.decisions[0].completed_days, at_30.decisions[0].retain), (30, False))


if __name__ == "__main__":
    unittest.main()
