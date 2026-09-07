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
        return {"symbol_state": {}, "positions": []}
    with open(config.STATE_FILE, "r") as f:
        return json.load(f)


def save(symbol_state: Dict[str, Any], positions: list[Position]) -> None:
    data = {
        "symbol_state": symbol_state,
        "positions": [asdict(p) for p in positions],
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
