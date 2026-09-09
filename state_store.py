"""
Simple JSON persistence for per-symbol block-tracking state and open/closed
paper positions, so restarting the bot doesn't lose track of anything.
"""
import json
import os
from dataclasses import asdict
from typing import Dict, Any

import config
from paper_broker import Position


def load() -> Dict[str, Any]:
    if not os.path.exists(config.STATE_FILE):
        return {"symbol_state": {}, "positions": [], "daily_stats": {}, "bot_control": {}}
    with open(config.STATE_FILE, "r") as f:
        data = json.load(f)
        data.setdefault("daily_stats", {})
        data.setdefault("bot_control", {})
        return data


def save(symbol_state: Dict[str, Any], positions: list[Position],
          daily_stats: Dict[str, Any], bot_control: Dict[str, Any]) -> None:
    data = {
        "symbol_state": symbol_state,
        "positions": [asdict(p) for p in positions],
        "daily_stats": daily_stats,
        "bot_control": bot_control,
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
