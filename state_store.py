"""
Simple JSON persistence for per-symbol block-tracking state, open/closed
paper positions, and (when TRADING_MODE=LIVE) real live-trade tracking --
so restarting the bot (every GitHub Actions run is a fresh process) never
loses track of anything.
"""
import json
import os
from dataclasses import asdict
from typing import Dict, Any

import config
from paper_broker import Position
from live_broker import LivePosition


def load() -> Dict[str, Any]:
    if not os.path.exists(config.STATE_FILE):
        return {"symbol_state": {}, "positions": [], "daily_stats": {},
                "bot_control": {}, "live_trades": []}
    with open(config.STATE_FILE, "r") as f:
        data = json.load(f)
        data.setdefault("daily_stats", {})
        data.setdefault("bot_control", {})
        data.setdefault("live_trades", [])
        return data


def save(symbol_state: Dict[str, Any], positions: list[Position],
          daily_stats: Dict[str, Any], bot_control: Dict[str, Any],
          live_trades: list[LivePosition] = None) -> None:
    data = {
        "symbol_state": symbol_state,
        "positions": [asdict(p) for p in positions],
        "daily_stats": daily_stats,
        "bot_control": bot_control,
        "live_trades": [asdict(t) for t in (live_trades or [])],
    }
    tmp_path = config.STATE_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp_path, config.STATE_FILE)


def restore_positions(raw_positions: list[dict]) -> list[Position]:
    positions = []
    for d in raw_positions:
        positions.append(Position(**d))
    return positions


def restore_live_trades(raw_trades: list[dict]) -> list[LivePosition]:
    trades = []
    for d in raw_trades:
        trades.append(LivePosition(**d))
    return trades
