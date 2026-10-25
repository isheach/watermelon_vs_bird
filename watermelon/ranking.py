"""完整历史时点股票池上的 Turn20；缺失与不合格分开处理。"""

from dataclasses import dataclass
from datetime import date
from math import isfinite
import struct
from typing import Mapping, Sequence

from .config import integer


class DataQualityError(ValueError):
    """未知数据可能影响排名，调用方应暂停信号并补齐证据。"""


def finite(value, name: str) -> float:
    if value is None or isinstance(value, bool):
        raise DataQualityError("Unknown " + name)
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise DataQualityError("Invalid " + name) from exc
    if not isfinite(result):
        raise DataQualityError("Non-finite " + name)
    return result


def float32(value: float) -> float:
    """先取平均再转 binary32，随后按 (score, code) 排序。"""
    try:
        result = struct.unpack("!f", struct.pack("!f", value))[0]
    except (OverflowError, struct.error) as exc:
        raise DataQualityError("Score outside float32 range") from exc
    if not isfinite(result):
        raise DataQualityError("Non-finite score")
    return result


@dataclass(frozen=True)
class TurnoverBar:
    day: date
    suspended: bool | None
    volume: float | None  # 股；适配器须把手数等单位先转换为股
    turnover_pct: float | None = None
    float_shares: float | None = None  # 该日当时生效的流通股本
    pit_verified: bool = False


@dataclass(frozen=True)
class Turn20:
    score: float | None
    valid_days: int


def turn20(window: Sequence[date], bars: Sequence[TurnoverBar]) -> Turn20:
    days = tuple(window)
    if len(days) != 20 or tuple(sorted(set(days))) != days:
        raise DataQualityError("Need exactly 20 ordered market dates")
    by_day = {bar.day: bar for bar in bars}
    if len(by_day) != len(bars) or set(by_day) != set(days):
        raise DataQualityError("Missing/duplicate/unexpected market-day record")
    valid = []
    for day in days:
        bar = by_day[day]
        if bar.pit_verified is not True or type(bar.suspended) is not bool:
            raise DataQualityError("Unverified historical turnover state")
        if bar.suspended:
            if bar.volume is not None and finite(bar.volume, "volume") != 0:
                raise DataQualityError("Suspended record with nonzero volume")
            continue
        volume = finite(bar.volume, "volume")
        if volume < 0:
            raise DataQualityError("Negative volume")
        if volume == 0:  # 有明确记录的无成交日，不是缺记录
            continue
        if bar.turnover_pct is not None:
            turnover = finite(bar.turnover_pct, "turnover_pct")
        else:
            shares = finite(bar.float_shares, "dated float shares")
            if shares <= 0:
                raise DataQualityError("Non-positive float shares")
            turnover = volume / shares * 100
        if turnover < 0:
            raise DataQualityError("Negative active turnover")
        valid.append(turnover)
    return Turn20(float32(sum(valid) / len(valid)) if valid else None, len(valid))


@dataclass(frozen=True)
class SecurityState:
    code: str
    as_of: date
    exchange: str | None  # SSE / SZSE / BSE
    is_a_share: bool | None
    listing_date: date | None
    known_delisting_date: date | None
    is_st: bool | None
    suspended: bool | None
    volume: float | None
    close: float | None
    total_mcap: float | None
    pit_verified: bool = False  # 包括生命周期：None 退市日是核实后未到已知退市日


def eligible(state: SecurityState, signal_day: date) -> bool:
    if state.pit_verified is not True or state.as_of != signal_day or not state.code:
        raise DataQualityError("Unverified/mismatched security state")
    if state.exchange not in {"SSE", "SZSE", "BSE"} or type(state.is_a_share) is not bool:
        raise DataQualityError("Unknown exchange/share type")
    if state.exchange == "BSE" or not state.is_a_share:
        return False
    if state.listing_date is None or state.listing_date > signal_day:
        raise DataQualityError("Unknown/future listing date")
    if state.known_delisting_date is not None:
        if state.known_delisting_date < state.listing_date:
            raise DataQualityError("Invalid lifecycle")
        if signal_day >= state.known_delisting_date:
            return False
    if (signal_day - state.listing_date).days < 365:
        return False
    if type(state.is_st) is not bool or type(state.suspended) is not bool:
        raise DataQualityError("Unknown ST/suspension state")
    if state.is_st or state.suspended:
        return False
    volume = finite(state.volume, "current volume")
    close = finite(state.close, "close")
    cap = finite(state.total_mcap, "total_mcap")
    if min(volume, close, cap) < 0:
        raise DataQualityError("Negative current market field")
    return volume > 0 and close > 0 and cap > 0


@dataclass(frozen=True)
class Coverage:
    as_of: date
    expected_codes: tuple[str, ...]
    full_pool_verified: bool = False  # 由全池适配器出具；代码不能凭子集推断此声明


@dataclass(frozen=True)
class Rank:
    code: str
    rank: int
    score: float
    valid_days: int


@dataclass(frozen=True)
class Ranking:
    signal_index: int
    signal_date: date
    rows: tuple[Rank, ...]
    full_pool_verified: bool = False

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(row.code for row in self.rows)

    def validate(self):
        integer(self.signal_index)
        if self.full_pool_verified is not True:
            raise DataQualityError("Incomplete universe must not produce a trading plan")
        if len(set(self.codes)) != len(self.rows):
            raise DataQualityError("Duplicate ranked code")
        if tuple(row.rank for row in self.rows) != tuple(range(1, len(self.rows) + 1)):
            raise DataQualityError("Non-contiguous ranking")
        keys = [(finite(row.score, "score"), row.code) for row in self.rows]
        if keys != sorted(keys) or any(k[0] < 0 for k in keys):
            raise DataQualityError("Ranking is not sorted by score/code")


def rank_universe(signal_index: int, window: Sequence[date], coverage: Coverage,
                  states: Sequence[SecurityState],
                  histories: Mapping[str, Sequence[TurnoverBar]]) -> Ranking:
    integer(signal_index)
    days = tuple(window)
    if len(days) != 20 or tuple(sorted(set(days))) != days:
        raise DataQualityError("Invalid market calendar window")
    signal_day = days[-1]
    expected = set(coverage.expected_codes)
    actual = {state.code for state in states}
    if (coverage.full_pool_verified is not True or coverage.as_of != signal_day
            or not expected or len(expected) != len(coverage.expected_codes)
            or len(actual) != len(states) or actual != expected):
        raise DataQualityError("Full PIT roster and exact state coverage required")
    scored = []
    for state in states:
        if not eligible(state, signal_day):
            continue
        if state.code not in histories:
            raise DataQualityError("Missing history for eligible security")
        history = histories[state.code]
        score = turn20(days, history)
        current = next(bar for bar in history if bar.day == signal_day)
        if (current.suspended is not False or current.volume is None
                or finite(current.volume, "last volume") != finite(state.volume, "state volume")):
            raise DataQualityError("Current state/history disagree")
        if score.score is None:
            raise DataQualityError("Active eligible security without valid turnover")
        scored.append((score.score, state.code, score.valid_days))
    rows = tuple(Rank(code, i + 1, score, count)
                 for i, (score, code, count) in enumerate(sorted(scored)))
    result = Ranking(signal_index, signal_day, rows, True)
    result.validate()
    return result
