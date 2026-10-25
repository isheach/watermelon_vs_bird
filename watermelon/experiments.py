"""未晋升研究条件；默认策略不调用本模块，不含资金分账模拟。"""

from collections import deque
from dataclasses import dataclass
from math import isfinite, prod
from typing import Sequence

EPS = 1e-12


def _known(value) -> bool:
    return value is not None and not isinstance(value, bool) and isfinite(value)


@dataclass(frozen=True)
class PriceObservation:
    close: float | None
    pre_close: float | None
    exists: bool = True


@dataclass(frozen=True)
class Trend:
    r3: float | None = None
    p5: float | None = None
    range4: float | None = None


def causal_features(observations: Sequence[PriceObservation]) -> tuple[Trend, ...]:
    """局部归一化连续链；缺失断段，当前及以后不能修改此前结果。

    每条输入对应一个市场日，包含 t-3 / t-8 端点在内必须连续可核验。
    仅保存最近9个比率，避免长链累计溢出；不使用后复权未来因子。
    """
    ratios = deque(maxlen=9)
    result = []
    for row in observations:
        if not (row.exists is True and _known(row.close) and _known(row.pre_close)
                and row.close > 0 and row.pre_close > 0):
            ratios.clear()
            result.append(Trend())
            continue
        ratio = row.close / row.pre_close
        if not isfinite(ratio) or ratio <= 0:
            ratios.clear()
            result.append(Trend())
            continue
        ratios.append(ratio)
        values = list(ratios)
        r3 = p5 = span = None
        if len(values) >= 4:
            recent = values[-3:]
            chain = [1.0]
            for value in recent:
                chain.append(chain[-1] * value)
            if all(isfinite(p) and p > 0 for p in chain):
                r3 = chain[-1] ** (1 / 3) - 1
                span = max(chain) / min(chain) - 1
                if not isfinite(span):
                    span = None
        if len(values) >= 9:
            product = prod(values[-8:-3])
            if isfinite(product) and product > 0:
                p5 = product ** (1 / 5) - 1
        result.append(Trend(r3, p5, span))
    return tuple(result)


def entry_flat(trend: Trend) -> bool:
    return bool(_known(trend.r3) and _known(trend.range4)
                and abs(trend.r3) <= .002 + EPS and trend.range4 <= .02 + EPS)


def entry_slow(trend: Trend) -> bool:
    """D：跌速减弱买入；需要足够数据，不用缺失 p5 猜条件。"""
    return bool(_known(trend.r3) and _known(trend.p5) and trend.r3 >= -.005 - EPS
                and (trend.p5 >= -.005 - EPS or trend.r3 >= .5 * trend.p5 - EPS))


def extend_flat(trend: Trend) -> bool:
    """晚卖 FLAT：仍在上涨才允许延长，不满足后触发下一开盘退出。"""
    return bool(_known(trend.r3) and trend.r3 > .002 + EPS)


def extend_slow(trend: Trend) -> bool:
    """晚卖 SLOW：涨速减弱前可延长，与 D 跌速减弱买入不同。"""
    return bool(extend_flat(trend) and _known(trend.p5)
                and (trend.p5 <= .002 + EPS or trend.r3 >= .5 * trend.p5 - EPS))
