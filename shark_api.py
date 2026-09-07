"""
Wrapper around Shark Exchange's PUBLIC klines endpoint.
No api-key/signature is needed for paper trading -- we only read prices.

NOTE: docs.sharkexchange.in's exact klines request/response schema was not
fully visible when this was built (page cut off before the "Get Klines"
section body). This wrapper follows the conventions used everywhere else
in Shark's docs (camelCase params, ISO timestamps or epoch-ms, standard
OHLCV fields). If your real responses differ, only _parse_klines() below
needs adjusting -- everything downstream (cpr_engine.py, bot.py) just
consumes the clean list of dicts this function returns.
"""
import time
from typing import List, Dict, Optional

import requests

import config


class Candle:
    __slots__ = ("open_time", "open", "high", "low", "close", "close_time")

    def __init__(self, open_time, open_, high, low, close, close_time):
        self.open_time = int(open_time) if open_time is not None else None    # epoch ms
        self.open = float(open_)
        self.high = float(high)
        self.low = float(low)
        self.close = float(close)
        self.close_time = int(close_time) if close_time is not None else None  # epoch ms

    def __repr__(self):
        return (f"Candle(t={self.open_time}, O={self.open}, H={self.high}, "
                f"L={self.low}, C={self.close})")


def _parse_klines(raw) -> List[Candle]:
    """Confirmed via live debug output that Shark's candle fields are:
    startTime, open, high, low, close, endTime, volume (values as strings).
    Keeping the list-style fallback too in case a different endpoint/version
    ever returns array-style rows.
    """
    candles = []
    items = raw.get("data", raw) if isinstance(raw, dict) else raw

    for item in items:
        if isinstance(item, dict):
            candles.append(Candle(
                open_time=item.get("startTime") or item.get("openTime") or item.get("open_time"),
                open_=item.get("open"),
                high=item.get("high"),
                low=item.get("low"),
                close=item.get("close"),
                close_time=item.get("endTime") or item.get("closeTime") or item.get("close_time"),
            ))
        elif isinstance(item, (list, tuple)):
            candles.append(Candle(
                open_time=item[0],
                open_=item[1],
                high=item[2],
                low=item[3],
                close=item[4],
                close_time=item[6] if len(item) > 6 else None,
            ))
    return candles


def get_klines(symbol: str, interval: str, limit: int = 20) -> List[Candle]:
    """Fetch recent candles for a symbol. Raises RuntimeError with the
    exchange's own error message body on failure (not just 'Bad Request'),
    so the Telegram alert / Actions log tells us exactly what's wrong.

    NOTE: confirmed via a live 400 response that Shark's field name is
    `pair` (not `symbol`), and `priceType` is rejected by this endpoint's
    validation -- so it is NOT sent.
    """
    url = f"{config.BASE_URL}{config.KLINES_ENDPOINT}"
    payload = {
        "pair": symbol,
        "interval": interval,
        "limit": limit,
    }
    resp = requests.post(url, json=payload, timeout=15)
    if not resp.ok:
        raise RuntimeError(
            f"Shark klines request failed [{resp.status_code}] "
            f"payload={payload} response_body={resp.text[:500]}"
        )
    raw = resp.json()
    candles = _parse_klines(raw)
    if not candles:
        print(f"[shark_api] WARNING: 0 candles parsed for payload={payload}. "
              f"Raw response snippet: {str(raw)[:500]}")
    return candles


def get_last_price(symbol: str) -> Optional[float]:
    """Convenience: latest close from the smallest interval we use."""
    candles = get_klines(symbol, config.ENTRY_CANDLE_INTERVAL, limit=1)
    if not candles:
        return None
    return candles[-1].close


def get_klines_with_retry(symbol: str, interval: str, limit: int = 20,
                           retries: int = 3, backoff_seconds: int = 5) -> List[Candle]:
    last_err = None
    for attempt in range(retries):
        try:
            return get_klines(symbol, interval, limit)
        except Exception as e:
            last_err = e
            print(f"[shark_api] get_klines failed (attempt {attempt+1}/{retries}): {e}")
            time.sleep(backoff_seconds)
    raise last_err


def get_range_high_low(symbol: str, start_ms: int, end_ms: int,
                        interval: str = None, limit: int = 100):
    """Fetches small-interval candles and aggregates the highest high /
    lowest low / latest close across [start_ms, end_ms]. This lets the bot
    catch a price wick that touched a level BETWEEN two 5-minute polls --
    the exchange's own candle still records that high/low even though we
    weren't polling at that exact second.

    Returns (highest_high, lowest_low, last_close) or (None, None, None) if
    no candles fall in the requested range (e.g. the range is older than
    what `limit` candles at this interval cover).
    """
    interval = interval or config.TOUCH_CHECK_INTERVAL
    try:
        candles = get_klines_with_retry(symbol, interval, limit=limit)
    except Exception as e:
        print(f"[shark_api] get_range_high_low failed for {symbol}: {e}")
        return None, None, None

    in_range = [c for c in candles
                if c.open_time is not None and start_ms <= c.open_time <= end_ms]
    if not in_range:
        return None, None, None

    highest = max(c.high for c in in_range)
    lowest = min(c.low for c in in_range)
    last_close = in_range[-1].close
    return highest, lowest, last_close
