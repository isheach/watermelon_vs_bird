"""测试专用虚构代码、市场日期和价格；不含行情样本。"""

from datetime import date, timedelta

from watermelon.config import FeeSchedule
from watermelon.ranking import Coverage, Rank, Ranking, SecurityState, TurnoverBar


def dates():
    start = date(2020, 1, 2)
    return tuple(start + timedelta(days=i) for i in range(28)
                 if (start + timedelta(days=i)).weekday() < 5)[:20]


def bars(score=1.0):
    return tuple(TurnoverBar(d, False, 1000, score, pit_verified=True) for d in dates())


def state(code="SYN001", **changes):
    values = dict(code=code, as_of=dates()[-1], exchange="SSE", is_a_share=True,
                  listing_date=date(2018, 1, 1), known_delisting_date=None,
                  is_st=False, suspended=False, volume=1000, close=10, total_mcap=1000000,
                  pit_verified=True)
    values.update(changes)
    return SecurityState(**values)


def coverage(*codes, **changes):
    values = dict(as_of=dates()[-1], expected_codes=tuple(codes), full_pool_verified=True)
    values.update(changes)
    return Coverage(**values)


def ranking(index=99, n=20):
    return Ranking(index, dates()[-1],
                   tuple(Rank(f"SYN{i:03}", i, float(i), 20) for i in range(1, n + 1)), True)


def fees():
    return FeeSchedule("0.0002", "5", "0.00001", "0.00001", "0.0005")
