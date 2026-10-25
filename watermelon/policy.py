"""日级持仓意图；退出意图不会释放真实持仓、时钟或现金。"""

from dataclasses import dataclass, replace
from typing import Sequence

from .config import integer
from .ranking import Ranking


@dataclass(frozen=True)
class Position:
    code: str
    quantity: int
    entry_index: int  # 第一次实际成交所在的市场交易日序号
    pending_exit: str = "none"  # none / baseline / weakening
    last_buy_index: int | None = None
    last_buy_quantity: int = 0  # 支持外部部分买入回报，避免同日新增股被卖出

    def __post_init__(self):
        integer(self.quantity, 1)
        integer(self.entry_index)
        integer(self.last_buy_quantity)
        if not self.code or self.pending_exit not in {"none", "baseline", "weakening"}:
            raise ValueError("Invalid position")
        if self.last_buy_index is not None:
            integer(self.last_buy_index, self.entry_index)
        if ((self.last_buy_index is None) != (self.last_buy_quantity == 0)
                or self.last_buy_quantity > self.quantity):
            raise ValueError("Invalid recent buy lot")

    def sellable(self, execution_index: int) -> int:
        if execution_index <= self.entry_index:
            return 0
        if self.last_buy_index is not None and execution_index <= self.last_buy_index:
            return self.quantity - self.last_buy_quantity
        return self.quantity


def validate_positions(positions: Sequence[Position], execution_index: int):
    integer(execution_index)
    if len({p.code for p in positions}) != len(positions):
        raise ValueError("One aggregated real position per code required")
    if any(p.entry_index > execution_index or
           (p.last_buy_index is not None and p.last_buy_index > execution_index)
           for p in positions):
        raise ValueError("Future entry clock")


@dataclass(frozen=True)
class Decision:
    code: str
    rank: int | None
    completed_days: int
    retain: bool
    reason: str


@dataclass(frozen=True)
class Plan:
    signal_index: int
    execution_index: int
    target_n: int
    retained: tuple[str, ...]
    exit_intents: tuple[str, ...]
    cancel_pending: tuple[str, ...]
    entry_candidates: tuple[str, ...]  # 条件候选，执行前仍须检查真实仓位数
    slots_before_fills: int
    decisions: tuple[Decision, ...]


def make_plan(ranking: Ranking, positions: Sequence[Position], execution_index: int,
              minimum_days: int = 30, buy_n: int = 10, exit_rank: int = 15) -> Plan:
    ranking.validate()
    integer(minimum_days)
    integer(buy_n, 1)
    integer(exit_rank, buy_n)
    validate_positions(positions, execution_index)
    if execution_index != ranking.signal_index + 1:
        raise ValueError("Close signal is executable only at the next market open")
    if len(positions) > buy_n:
        raise ValueError("Existing positions exceed strategy capacity")
    ranks = {row.code: row.rank for row in ranking.rows}
    retained, exits, cancelled, decisions = [], [], [], []
    for position in positions:
        rank = ranks.get(position.code)
        age = execution_index - position.entry_index
        keep = (rank is not None and rank <= exit_rank) or age < minimum_days
        reason = ("top10" if rank is not None and rank <= buy_n else
                  "buffer" if rank is not None and rank <= exit_rank else
                  "minimum_holding" if age < minimum_days else "exit")
        if position.pending_exit == "weakening":
            keep, reason = False, "irreversible_weakening_exit"
        if keep:
            retained.append(position.code)
            if position.pending_exit == "baseline":
                cancelled.append(position.code)
        else:
            exits.append(position.code)
        decisions.append(Decision(position.code, rank, age, keep, reason))
    held = {p.code for p in positions}
    candidates = tuple(code for code in ranking.codes[:buy_n] if code not in held)
    candidates = candidates[:buy_n - len(retained)]
    return Plan(ranking.signal_index, execution_index, buy_n, tuple(retained),
                tuple(exits), tuple(cancelled), candidates,
                buy_n - len(positions), tuple(decisions))


def ready_entries(plan: Plan, actual_positions: Sequence[Position]) -> tuple[str, ...]:
    """只依据已确认成交后的实仓；卖出估算或退出意图不算成交。"""
    validate_positions(actual_positions, plan.execution_index)
    if len(actual_positions) > plan.target_n:
        raise ValueError("Position cap exceeded")
    held = {p.code for p in actual_positions}
    candidates = tuple(code for code in plan.entry_candidates if code not in held)
    return candidates[:plan.target_n - len(actual_positions)]


def record_fill(position: Position | None, *, code: str, side: str,
                quantity: int, execution_index: int) -> Position | None:
    """接收外部确认成交；零成交不创建仓位，部分成交保留原入场时钟。"""
    integer(quantity)
    integer(execution_index)
    if side not in {"BUY", "SELL"} or not code:
        raise ValueError("Invalid fill")
    if position is not None and (position.code != code or position.entry_index > execution_index
                                or (position.last_buy_index is not None
                                    and position.last_buy_index > execution_index)):
        raise ValueError("Mismatched position/fill")
    if quantity == 0:
        return position
    if side == "BUY":
        if position is None:
            return Position(code, quantity, execution_index)
        recent = position.last_buy_quantity if position.last_buy_index == execution_index else 0
        if execution_index == position.entry_index and position.last_buy_index is None:
            recent = position.quantity
        return replace(position, quantity=position.quantity + quantity,
                       last_buy_index=execution_index, last_buy_quantity=recent + quantity)
    if position is None or quantity > position.quantity:
        raise ValueError("No position/oversell")
    if quantity > position.sellable(execution_index):
        raise ValueError("T+1 violation")
    if quantity == position.quantity:
        return None
    # 只清理已跨日解锁的跟踪；同日新买股数仍须留在 T+1 冻结中。
    unlocked = position.last_buy_index is not None and execution_index > position.last_buy_index
    return replace(position, quantity=position.quantity - quantity,
                   last_buy_index=None if unlocked else position.last_buy_index,
                   last_buy_quantity=0 if unlocked else position.last_buy_quantity)
