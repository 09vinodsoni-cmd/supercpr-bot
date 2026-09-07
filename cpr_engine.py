"""
Pure CPR math + signal classification. No I/O here -- fully unit-testable.
Implements strategy rules #1 and #2 from Super_CPR_Final_Strategy.txt
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class CPRLevels:
    block_open_time: int   # epoch ms of the 4h block this CPR was computed FROM
    high: float
    low: float
    close: float
    pp: float
    bc: float
    tc: float
    upper_cpr: float
    lower_cpr: float
    r1: float
    s1: float


def compute_cpr(open_time: int, high: float, low: float, close: float) -> CPRLevels:
    """Rule #1 formulas, applied to one completed 4-hour block's H/L/C."""
    pp = (high + low + close) / 3
    bc = (high + low) / 2
    tc = 2 * pp - bc
    upper = max(tc, bc)
    lower = min(tc, bc)
    r1 = 2 * pp - low
    s1 = 2 * pp - high
    return CPRLevels(
        block_open_time=open_time,
        high=high, low=low, close=close,
        pp=pp, bc=bc, tc=tc,
        upper_cpr=upper, lower_cpr=lower,
        r1=r1, s1=s1,
    )


def classify_signal(first_candle_open: float, current_cpr: CPRLevels,
                     previous_cpr: Optional[CPRLevels]) -> str:
    """Rule #2. `current_cpr` = CPR computed from the block that JUST
    completed (i.e. the levels that apply to the new/current block).
    `previous_cpr` = the CPR that applied to the block before that
    (needed only for the INSIDE CPR case)."""
    if first_candle_open > current_cpr.upper_cpr:
        return "BUY"
    if first_candle_open < current_cpr.lower_cpr:
        return "SELL"

    # INSIDE CPR
    if previous_cpr is None:
        # Not enough history yet -- default conservatively to SELL per rule
        # text ("otherwise -> SELL"), but this should rarely trigger since
        # we require 2 completed blocks before trading starts.
        return "SELL"

    current_mid = (current_cpr.upper_cpr + current_cpr.lower_cpr) / 2
    previous_mid = (previous_cpr.upper_cpr + previous_cpr.lower_cpr) / 2
    if current_mid > previous_mid:
        return "BUY"
    return "SELL"
