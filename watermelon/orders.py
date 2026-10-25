"""开盘可观测输入上的保守委托估算；返回数量不是成交回报。"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Mapping, Sequence

from .config import FeeSchedule, decimal, integer
from .policy import Plan, Position, validate_positions
from .ranking import DataQualityError

ZERO = Decimal("0")
LIMIT_TOLERANCE = Decimal("0.004")  # 与研究开盘限价判定一致，价格单位元


@dataclass(frozen=True)
class OpenQuote:
    code: str
    execution_index: int
    board: str  # MAIN / STAR；适配器负责历史板块及单位映射
    open_price: object
    suspended: bool | None
    is_st: bool | None
    limit_up: object
    limit_down: object
    previous_amount: object  # 前一市场交易日成交额，元，不能取最近有成交日
    previous_amount_index: int | None
    known_at_open: bool = False


@dataclass(frozen=True)
class Costs:
    commission: Decimal
    stamp: Decimal
    transfer: Decimal

    @property
    def total(self) -> Decimal:
        return self.commission + self.stamp + self.transfer


def fees_for(gross, side: str, schedule: FeeSchedule) -> Costs:
    value = decimal(gross)
    if value < 0 or side not in {"BUY", "SELL"}:
        raise ValueError("Invalid gross/side")
    if value == 0:
        return Costs(ZERO, ZERO, ZERO)
    return Costs(max(value * schedule.commission_rate, schedule.minimum_commission),
                 value * schedule.sell_stamp_rate if side == "SELL" else ZERO,
                 value * (schedule.buy_transfer_rate if side == "BUY"
                          else schedule.sell_transfer_rate))


@dataclass(frozen=True)
class OrderEstimate:
    code: str
    side: str
    quantity: int = 0
    price: Decimal = ZERO
    gross: Decimal = ZERO
    commission: Decimal = ZERO
    stamp: Decimal = ZERO
    transfer: Decimal = ZERO
    cash_delta: Decimal = ZERO
    reason: str = "blocked"


def spendable_cash(cash, frozen_cash=0) -> Decimal:
    total, frozen = decimal(cash), decimal(frozen_cash)
    if total < 0 or frozen < 0 or frozen > total:
        raise ValueError("Invalid cash/frozen cash")
    return total - frozen


def opening_nav(cash, positions: Sequence[Position], marks: Mapping[str, object],
                receivables=0) -> Decimal:
    """mark 必须是已知开盘价或可核验前收盘标记；包括全部待卖实仓。"""
    total, due = decimal(cash), decimal(receivables)
    if total < 0 or due < 0 or len({p.code for p in positions}) != len(positions):
        raise ValueError("Invalid account view")
    value = total + due
    for position in positions:
        if position.code not in marks:
            raise DataQualityError("Missing opening mark for real holding")
        try:
            price = decimal(marks[position.code])
        except ValueError as exc:
            raise DataQualityError("Unknown opening mark") from exc
        if price <= 0:
            raise DataQualityError("Non-positive opening mark")
        value += price * position.quantity
    return value


def new_name_budget(cash, frozen_cash, nav, new_count: int, target_n: int = 10) -> Decimal:
    available = spendable_cash(cash, frozen_cash)
    equity = decimal(nav)
    integer(new_count)
    integer(target_n, 1)
    if equity < 0:
        raise ValueError("Negative opening NAV")
    return min(available / new_count, equity / target_n) if new_count else ZERO


def _floor(value: Decimal, unit: int) -> int:
    return int((value / unit).to_integral_value(rounding=ROUND_FLOOR)) * unit


def estimate_order(side: str, quote: OpenQuote, *, signal_index: int, cash,
                   fees: FeeSchedule, budget=None, position: Position | None = None,
                   requested_shares: int | None = None, slippage_bps=10,
                   participation_rate="0.01") -> OrderEstimate:
    """不看执行日收盘/全日成交量；未知开盘事实返回阻断，非法调用抛异常。"""
    if side not in {"BUY", "SELL"}:
        raise ValueError("Invalid side")
    integer(signal_index)
    integer(quote.execution_index)
    if side == "SELL":
        if position is None or position.code != quote.code:
            raise ValueError("SELL requires matching real position")
        validate_positions((position,), quote.execution_index)
    available = spendable_cash(cash)
    slip, participation = decimal(slippage_bps) / 10000, decimal(participation_rate)
    if not 0 <= slip < 1 or not 0 < participation <= 1:
        raise ValueError("Invalid slippage/capacity rate")
    blocked = lambda reason: OrderEstimate(quote.code, side, reason=reason)
    if quote.execution_index != signal_index + 1:
        return blocked("wrong_execution_day")
    if quote.known_at_open is not True or type(quote.suspended) is not bool:
        return blocked("unknown_open_state")
    if quote.board not in {"MAIN", "STAR"}:
        return blocked("unknown_board")
    if quote.suspended:
        return blocked("suspended")
    if side == "BUY":
        if type(quote.is_st) is not bool:
            return blocked("unknown_st")
        if quote.is_st:
            return blocked("new_st")
        if position is not None:
            return blocked("existing_position")
        if budget is None:
            raise ValueError("Explicit per-name budget required")
        allocation = decimal(budget)
        if allocation < 0:
            raise ValueError("Negative budget")
    else:
        if position.sellable(quote.execution_index) == 0:
            return blocked("t_plus_one")
        if requested_shares is not None:
            integer(requested_shares)
            if requested_shares > position.quantity:
                raise ValueError("Requested oversell")
    try:
        raw, up, down = map(decimal, (quote.open_price, quote.limit_up, quote.limit_down))
    except ValueError:
        return blocked("unknown_price_or_limits")
    if min(raw, up, down) <= 0 or down > up or raw < down - LIMIT_TOLERANCE or raw > up + LIMIT_TOLERANCE:
        return blocked("invalid_price_or_limits")
    if side == "BUY" and raw >= up - LIMIT_TOLERANCE:
        return blocked("limit_up")
    if side == "SELL" and raw <= down + LIMIT_TOLERANCE:
        return blocked("limit_down")
    if (type(quote.previous_amount_index) is not int
            or quote.previous_amount_index != signal_index):
        return blocked("unknown_previous_market_amount")
    try:
        amount = decimal(quote.previous_amount)
    except ValueError:
        return blocked("unknown_previous_market_amount")
    if amount < 0:
        return blocked("invalid_previous_market_amount")
    capacity = amount * participation
    if capacity <= 0:
        return blocked("zero_capacity")
    price = min(up, raw * (1 + slip)) if side == "BUY" else max(down, raw * (1 - slip))
    if side == "BUY":
        # 与研究引擎一致：容量上限也约束含费预算，略保守于仅约束成交额。
        spend = min(available, allocation, capacity)
        rate = fees.buy_transfer_rate
        shares = max(ZERO, min(spend / (price * (1 + fees.commission_rate + rate)),
                              (spend - fees.minimum_commission) / (price * (1 + rate))))
        minimum, unit = (200, 1) if quote.board == "STAR" else (100, 100)
        quantity = _floor(shares, unit)
        if quantity < minimum:
            return blocked("insufficient_cash_budget_or_lot")
        while quantity >= minimum:
            gross = price * quantity
            costs = fees_for(gross, side, fees)
            if gross + costs.total <= spend:
                break
            quantity -= unit
        if quantity < minimum:
            return blocked("insufficient_cash_budget_or_lot")
    else:
        wanted = position.quantity if requested_shares is None else requested_shares
        wanted = min(wanted, position.sellable(quote.execution_index))
        maximum = min(Decimal(wanted), capacity / price)
        if maximum >= position.quantity:
            quantity = position.quantity  # 完整余股清仓允许不足100股
        else:
            quantity = _floor(maximum, 1 if quote.board == "STAR" else 100)
        if quantity <= 0:
            return blocked("insufficient_capacity_or_lot")
        gross = price * quantity
        costs = fees_for(gross, side, fees)
        if gross - costs.total + available < 0:
            return blocked("insufficient_cash_for_sell_fees")
    gross = price * quantity
    costs = fees_for(gross, side, fees)
    delta = -(gross + costs.total) if side == "BUY" else gross - costs.total
    reason = "estimated_partial" if side == "SELL" and quantity < position.quantity else "estimated"
    return OrderEstimate(quote.code, side, quantity, price, gross, costs.commission,
                         costs.stamp, costs.transfer, delta, reason)


@dataclass(frozen=True)
class EntryBatch:
    opening_equity: Decimal
    budget_per_name: Decimal
    orders: tuple[OrderEstimate, ...]
    unreserved_available_cash: Decimal  # 估算预留后的余额，不是已成交后的现金账


def estimate_entries(plan: Plan, actual_positions: Sequence[Position],
                     quotes: Mapping[str, OpenQuote], *, cash, frozen_cash=0,
                     marks: Mapping[str, object], fees: FeeSchedule, receivables=0,
                     slippage_bps=10, participation_rate="0.01") -> EntryBatch:
    """传入已确认卖出后的账户；不得把卖出估算收入加入 cash。"""
    validate_positions(actual_positions, plan.execution_index)
    if len(actual_positions) > plan.target_n:
        raise ValueError("Position cap exceeded")
    held = {p.code for p in actual_positions}
    candidates = tuple(c for c in plan.entry_candidates if c not in held)
    nav = opening_nav(cash, actual_positions, marks, receivables)
    available = spendable_cash(cash, frozen_cash)
    budget = new_name_budget(cash, frozen_cash, nav, len(candidates), plan.target_n)
    slots = plan.target_n - len(actual_positions)
    orders = []
    for code in candidates:
        if slots == 0:
            orders.append(OrderEstimate(code, "BUY", reason="position_cap"))
            continue
        quote = quotes.get(code)
        if quote is None:
            orders.append(OrderEstimate(code, "BUY", reason="unknown_quote"))
            continue
        if quote.code != code or quote.execution_index != plan.execution_index:
            raise ValueError("Quote does not match plan")
        order = estimate_order("BUY", quote, signal_index=plan.signal_index, cash=available,
                               budget=budget, fees=fees, slippage_bps=slippage_bps,
                               participation_rate=participation_rate)
        orders.append(order)
        if order.quantity:
            slots -= 1
            available += order.cash_delta
    return EntryBatch(nav, budget, tuple(orders), available)
