from dataclasses import replace
from decimal import Decimal
import unittest

from watermelon.config import FeeSchedule
from watermelon.orders import (OpenQuote, estimate_entries, estimate_order, fees_for,
                               new_name_budget, opening_nav, spendable_cash)
from watermelon.policy import Position, make_plan, record_fill
from watermelon.ranking import DataQualityError
from support import fees, ranking


def quote(**changes):
    values = dict(code="SYN001", execution_index=100, board="MAIN", open_price="10",
                  suspended=False, is_st=False, limit_up="11", limit_down="9",
                  previous_amount="10000000", previous_amount_index=99, known_at_open=True)
    values.update(changes)
    return OpenQuote(**values)


FREE = FeeSchedule(0, 0, 0, 0, 0)


def buy(row=None, **changes):
    args = dict(signal_index=99, cash="10000", budget="10000", fees=fees())
    args.update(changes)
    return estimate_order("BUY", row or quote(), **args)


def sell(row=None, **changes):
    args = dict(signal_index=99, cash="100", position=Position("SYN001", 500, 60), fees=fees())
    args.update(changes)
    return estimate_order("SELL", row or quote(), **args)


class OrderTests(unittest.TestCase):
    def test_commission_five_yuan_boundary_and_each_order(self):
        self.assertEqual(fees_for("24999.99", "BUY", fees()).commission, Decimal(5))
        self.assertEqual(fees_for("25000", "BUY", fees()).commission, Decimal(5))
        self.assertGreater(fees_for("25000.01", "BUY", fees()).commission, Decimal(5))
        self.assertEqual(fees_for(0, "BUY", fees()).total, 0)
        self.assertEqual(sum(fees_for(1000, "BUY", fees()).commission for _ in range(2)), 10)

    def test_explicit_stamp_transfer_and_net_cash(self):
        result = sell(slippage_bps=0)
        self.assertEqual(result.gross, 5000)
        self.assertEqual(result.commission, 5)
        self.assertEqual(result.stamp, Decimal("2.5"))
        self.assertEqual(result.transfer, Decimal(".05"))
        self.assertEqual(result.cash_delta, Decimal("4992.45"))
        self.assertEqual(buy().stamp, 0)

    def test_main_lots_and_no_overspend(self):
        result = buy()
        self.assertEqual(result.quantity, 900)
        self.assertEqual(result.price, Decimal("10.01"))
        self.assertLessEqual(-result.cash_delta, 10000)
        self.assertEqual(buy(cash=1000, budget=1000).quantity, 0)
        self.assertEqual(buy(budget=0).quantity, 0)

    def test_star_minimum_200_then_unit_one(self):
        star = quote(board="STAR")
        self.assertEqual(buy(star, cash=1990, budget=1990, fees=FREE, slippage_bps=0).quantity, 0)
        self.assertEqual(buy(star, cash=2000, budget=2000, fees=FREE, slippage_bps=0).quantity, 200)
        self.assertEqual(buy(star, cash=2010, budget=2010, fees=FREE, slippage_bps=0).quantity, 201)

    def test_partial_main_sell_and_full_odd_lot(self):
        pos = Position("SYN001", 151, 60)
        partial = sell(quote(previous_amount=150000), position=pos, slippage_bps=0)
        self.assertEqual((partial.quantity, partial.reason), (100, "estimated_partial"))
        complete = sell(quote(previous_amount=151000), position=pos, slippage_bps=0)
        self.assertEqual(complete.quantity, 151)
        self.assertEqual(sell(position=pos, requested_shares=51).quantity, 0)
        self.assertEqual(sell(position=Position("SYN001", 51, 60)).quantity, 51)

    def test_partial_star_sell_is_one_share_unit(self):
        result = sell(quote(board="STAR", previous_amount=151000),
                      position=Position("SYN001", 201, 60), slippage_bps=0)
        self.assertEqual(result.quantity, 151)

    def test_limit_halt_t_plus_one_and_st(self):
        cases = ((buy, quote(suspended=True), "suspended"),
                 (buy, quote(open_price=11), "limit_up"),
                 (sell, quote(open_price=9), "limit_down"),
                 (buy, quote(is_st=True), "new_st"))
        for estimate, row, reason in cases:
            result = estimate(row)
            self.assertEqual((result.quantity, result.reason), (0, reason))
        self.assertEqual(sell(position=Position("SYN001", 100, 100)).reason, "t_plus_one")
        self.assertGreater(sell(quote(is_st=True)).quantity, 0)

    def test_today_added_lot_cannot_be_sold(self):
        pos = record_fill(Position("SYN001", 100, 60), code="SYN001", side="BUY",
                          quantity=100, execution_index=100)
        self.assertEqual(sell(position=pos, slippage_bps=0).quantity, 100)

    def test_unknown_inputs_are_blocked(self):
        for change in ({"known_at_open": False}, {"suspended": None}, {"is_st": None},
                       {"open_price": None}, {"limit_up": None}, {"limit_down": None},
                       {"previous_amount": None}, {"previous_amount_index": 98},
                       {"board": "UNKNOWN"}, {"previous_amount": float("nan")}):
            with self.subTest(change=change):
                self.assertEqual(buy(quote(**change)).quantity, 0)

    def test_zero_previous_day_does_not_seek_earlier_volume(self):
        self.assertEqual(buy(quote(previous_amount=0)).reason, "zero_capacity")
        self.assertEqual(buy(quote(previous_amount=-1)).quantity, 0)

    def test_wrong_market_day_blocked(self):
        self.assertEqual(buy(quote(execution_index=101)).reason, "wrong_execution_day")

    def test_capacity_is_previous_amount_one_percent(self):
        result = buy(quote(previous_amount=200000), fees=FREE, slippage_bps=0)
        self.assertEqual(result.quantity, 200)
        self.assertEqual(result.gross / Decimal(200000), Decimal(".01"))
        self.assertEqual(buy(quote(previous_amount=100000), slippage_bps=0).quantity, 0)

    def test_slippage_capped_at_price_limit(self):
        result = buy(quote(open_price="10.99"), fees=FREE, slippage_bps=100)
        self.assertEqual(result.price, 11)
        result = sell(quote(open_price="9.01"), fees=FREE, slippage_bps=100)
        self.assertEqual(result.price, 9)

    def test_cannot_rebuy_existing_or_pay_unfunded_sell_fees(self):
        self.assertEqual(buy(position=Position("SYN001", 100, 60)).reason, "existing_position")
        small = quote(open_price="1", limit_up="1.1", limit_down=".9")
        self.assertEqual(sell(small, cash=0, position=Position("SYN001", 1, 60)).quantity, 0)

    def test_buy_sweep_budget_and_capacity_invariants(self):
        for board in ("MAIN", "STAR"):
            for budget in range(0, 5001, 37):
                result = buy(quote(board=board, previous_amount=350000), cash=budget,
                             budget=budget)
                if result.quantity:
                    self.assertLessEqual(-result.cash_delta, min(budget, 3500))
                    self.assertLessEqual(result.gross, 3500)
                    self.assertGreaterEqual(result.quantity, 200 if board == "STAR" else 100)
                    if board == "MAIN":
                        self.assertEqual(result.quantity % 100, 0)


class AccountBoundaryTests(unittest.TestCase):
    def test_budget_uses_cash_nav_and_no_recharge(self):
        self.assertEqual(new_name_budget(100000, 0, 100000, 10), 10000)
        self.assertEqual(new_name_budget(5000, 1000, 120000, 2), 2000)
        self.assertEqual(new_name_budget(200000, 0, 200000, 10), 20000)
        self.assertEqual(new_name_budget(100, 0, 100, 0), 0)
        with self.assertRaises(ValueError):
            spendable_cash(10, 11)

    def test_pending_holdings_and_receivables_in_nav(self):
        pos = Position("OLD", 100, 60, "baseline")
        self.assertEqual(opening_nav(1000, [pos], {"OLD": 10}, 500), 2500)
        self.assertEqual(spendable_cash(1000, 200), 800)
        with self.assertRaises(DataQualityError):
            opening_nav(1000, [pos], {})

    def test_estimated_exit_releases_neither_slot_nor_cash(self):
        positions = [Position(f"OLD{i}", 100, 60, "baseline") for i in range(10)]
        plan = make_plan(ranking(), positions, 100)
        marks = {pos.code: 50 for pos in positions}
        estimated_sale = sell(quote(code="OLD0"), position=positions[0])
        self.assertGreater(estimated_sale.cash_delta, 0)
        batch = estimate_entries(plan, positions, {}, cash=50000, frozen_cash=10000,
                                 marks=marks, fees=fees())
        self.assertEqual(batch.opening_equity, 100000)
        self.assertTrue(all(order.reason == "position_cap" for order in batch.orders))
        self.assertEqual(batch.unreserved_available_cash, 40000)

    def test_partial_exit_still_counts_and_confirmed_full_exit_frees_one(self):
        positions = [Position(f"OLD{i}", 200, 60, "baseline") for i in range(10)]
        plan = make_plan(ranking(), positions, 100)
        quotes = {code: quote(code=code) for code in plan.entry_candidates}
        partial = [replace(positions[0], quantity=100)] + positions[1:]
        kwargs = dict(cash=50000, frozen_cash=10000,
                      marks={p.code: 25 for p in positions}, fees=fees())
        blocked = estimate_entries(plan, partial, quotes, **kwargs)
        self.assertFalse(any(order.quantity for order in blocked.orders))
        ready = estimate_entries(plan, positions[1:], quotes, **kwargs)
        self.assertEqual(ready.budget_per_name, 4000)
        self.assertEqual(sum(order.quantity > 0 for order in ready.orders), 1)
        self.assertGreaterEqual(ready.unreserved_available_cash, 36000)


class SellReviewRegressionTests(unittest.TestCase):
    def test_standalone_sell_rejects_future_recent_buy_snapshot(self):
        position = Position("SYN001", 400, 80, last_buy_index=101, last_buy_quantity=300)
        self.assertEqual(position.sellable(100), 100)  # 只检查可卖数会错误接受这份未来快照
        for board in ("MAIN", "STAR"):
            with self.subTest(board=board), self.assertRaisesRegex(ValueError, "Future"):
                sell(quote(board=board), position=position)

    def test_standalone_sell_rejects_future_first_entry_snapshot(self):
        with self.assertRaisesRegex(ValueError, "Future"):
            sell(position=Position("SYN001", 100, 101))

    def test_main_cross_day_partial_estimate_and_fill_roundtrip(self):
        position = record_fill(Position("SYN001", 100, 80, "baseline"), code="SYN001", side="BUY",
                               quantity=300, execution_index=100)
        for amount, quantity, remaining in ((200000, 200, 200), (100000, 100, 100)):
            row = quote(execution_index=101, previous_amount_index=100, previous_amount=amount)
            estimate = sell(row, position=position, signal_index=100, fees=FREE, slippage_bps=0)
            self.assertEqual((estimate.quantity, estimate.reason), (quantity, "estimated_partial"))
            position = record_fill(position, code="SYN001", side="SELL", quantity=estimate.quantity, execution_index=101)
            self.assertEqual((position.quantity, position.entry_index, position.pending_exit), (remaining, 80, "baseline"))
            self.assertEqual((position.last_buy_index, position.last_buy_quantity), (None, 0))
        estimate = sell(row, position=position, signal_index=100, fees=FREE, slippage_bps=0)
        self.assertEqual(estimate.quantity, 100)
        self.assertIsNone(record_fill(position, code="SYN001", side="SELL", quantity=100, execution_index=101))

    def test_star_cross_day_partial_estimate_and_fill_roundtrip(self):
        position = record_fill(Position("SYN001", 200, 80, "weakening"), code="SYN001", side="BUY",
                               quantity=301, execution_index=100)
        for amount, quantity, remaining in ((201000, 201, 300), (150000, 150, 150)):
            row = quote(board="STAR", execution_index=101, previous_amount_index=100, previous_amount=amount)
            estimate = sell(row, position=position, signal_index=100, fees=FREE, slippage_bps=0)
            self.assertEqual(estimate.quantity, quantity)
            position = record_fill(position, code="SYN001", side="SELL", quantity=quantity, execution_index=101)
            self.assertEqual((position.quantity, position.entry_index, position.pending_exit), (remaining, 80, "weakening"))
            self.assertEqual((position.last_buy_index, position.last_buy_quantity), (None, 0))
        estimate = sell(row, position=position, signal_index=100, fees=FREE, slippage_bps=0)
        self.assertEqual(estimate.quantity, 150)
        self.assertIsNone(record_fill(position, code="SYN001", side="SELL", quantity=150, execution_index=101))

    def test_star_today_lot_locked_then_tomorrow_partial_sale(self):
        position = record_fill(Position("SYN001", 300, 80), code="SYN001", side="BUY",
                               quantity=301, execution_index=100)
        estimate = sell(quote(board="STAR"), position=position, fees=FREE, slippage_bps=0)
        self.assertEqual(estimate.quantity, 300)
        position = record_fill(position, code="SYN001", side="SELL", quantity=300, execution_index=100)
        self.assertEqual((position.quantity, position.last_buy_index, position.last_buy_quantity), (301, 100, 301))
        self.assertEqual(sell(quote(board="STAR"), position=position).reason, "t_plus_one")
        row = quote(board="STAR", execution_index=101, previous_amount_index=100, previous_amount=200000)
        estimate = sell(row, position=position, signal_index=100, fees=FREE, slippage_bps=0)
        self.assertEqual(estimate.quantity, 200)
        position = record_fill(position, code="SYN001", side="SELL", quantity=200, execution_index=101)
        self.assertEqual((position.quantity, position.entry_index, position.sellable(101)), (101, 80, 101))


if __name__ == "__main__":
    unittest.main()
