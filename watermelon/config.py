"""标准库配置；金额用十进制，实验规则不进入默认策略。"""

from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path


def decimal(value) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("Boolean is not a monetary value")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError("Invalid decimal") from exc
    if not result.is_finite():
        raise ValueError("Non-finite decimal")
    return result


def integer(value, minimum=0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError("Expected integer >= " + str(minimum))
    return value


@dataclass(frozen=True)
class FeeSchedule:
    commission_rate: Decimal
    minimum_commission: Decimal
    buy_transfer_rate: Decimal
    sell_transfer_rate: Decimal
    sell_stamp_rate: Decimal

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            value = decimal(getattr(self, name))
            if value < 0 or (name != "minimum_commission" and value >= 1):
                raise ValueError("Invalid fee: " + name)
            object.__setattr__(self, name, value)


@dataclass(frozen=True)
class StrategyConfig:
    initial_cash: Decimal
    minimum_days: int
    buy_n: int
    exit_rank: int
    slippage_bps: Decimal
    participation_rate: Decimal
    fees: FeeSchedule
    fee_preset: str

    def __post_init__(self):
        integer(self.minimum_days)
        integer(self.buy_n, 1)
        integer(self.exit_rank, self.buy_n)
        for name in ("initial_cash", "slippage_bps", "participation_rate"):
            object.__setattr__(self, name, decimal(getattr(self, name)))
        if self.initial_cash <= 0 or not 0 <= self.slippage_bps < 10000:
            raise ValueError("Invalid cash/slippage")
        if not 0 < self.participation_rate <= 1:
            raise ValueError("Invalid participation")
        if not isinstance(self.fees, FeeSchedule) or not self.fee_preset:
            raise ValueError("Explicit fee schedule and description required")


def load_config(path) -> StrategyConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {"initial_cash", "minimum_days", "buy_n", "exit_rank",
                "slippage_bps", "participation_rate", "fees", "fee_preset",
                "turn_window", "exclude_bse", "experiments_enabled"}
    if set(raw) != expected:
        raise ValueError("Unexpected or missing config keys")
    if type(raw["turn_window"]) is not int or raw["turn_window"] != 20:
        raise ValueError("This kernel defines a 20-market-day window")
    if raw["exclude_bse"] is not True or raw["experiments_enabled"] is not False:
        raise ValueError("Default strategy excludes BSE and disables experiments")
    fields = {k: v for k, v in raw.items()
              if k not in {"turn_window", "exclude_bse", "experiments_enabled", "fees"}}
    return StrategyConfig(**fields, fees=FeeSchedule(**raw["fees"]))
