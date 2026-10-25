"""从空白合成日历/股票状态生成计划与估算，不读取行情，不执行委托。"""

from dataclasses import asdict, replace
from datetime import date, timedelta
import json
from pathlib import Path

from watermelon.config import load_config
from watermelon.experiments import PriceObservation, causal_features, entry_flat, entry_slow
from watermelon.orders import OpenQuote, estimate_entries
from watermelon.policy import Position, make_plan
from watermelon.ranking import Coverage, SecurityState, TurnoverBar, rank_universe


def main():
    cfg = load_config(Path(__file__).resolve().parents[1] / "config" / "default.json")
    start = date(2020, 1, 2)
    days = tuple(start + timedelta(days=i) for i in range(28)
                 if (start + timedelta(days=i)).weekday() < 5)[:20]
    codes = tuple(f"SYN{i:03}" for i in range(1, 21))
    states, histories = [], {}
    for i, code in enumerate(codes, 1):
        states.append(SecurityState(code, days[-1], "SSE", True, date(2018, 1, 1),
                                    None, False, False, 1000, 10, 1000000, True))
        rows = [TurnoverBar(day, False, 1000, i / 10, pit_verified=True) for day in days]
        if i == 1:
            for j in (3, 8):
                rows[j] = replace(rows[j], suspended=True, volume=0, turnover_pct=None)
        histories[code] = rows
    ranked = rank_universe(99, days, Coverage(days[-1], codes, True), states, histories)
    positions = (Position("SYN015", 100, 70), Position("SYN016", 100, 71),
                 Position("SYN020", 100, 70, "baseline"))
    plan = make_plan(ranked, positions, 100, cfg.minimum_days, cfg.buy_n, cfg.exit_rank)
    quotes = {code: OpenQuote(code, 100, "STAR" if code == "SYN003" else "MAIN",
                              10, False, False, 11, 9, 10000000, 99, True)
              for code in plan.entry_candidates}
    batch = estimate_entries(plan, positions, quotes, cash=70000, frozen_cash=5000,
                             marks={p.code: 10 for p in positions}, fees=cfg.fees,
                             slippage_bps=cfg.slippage_bps, participation_rate=cfg.participation_rate)
    feature = causal_features([PriceObservation(10, 10)] * 9)[-1]
    result = dict(scope="SYNTHETIC ONLY: plans and estimates, no fills or historical returns",
                  turn20=asdict(ranked.rows[0]), plan=asdict(plan), estimates=asdict(batch),
                  separate_experimental_predicates=dict(F=entry_flat(feature), D=entry_slow(feature)))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str, allow_nan=False))


if __name__ == "__main__":
    main()
