"""
Simulated (paper) position management. No real orders are ever sent.
Implements strategy rules #4 - #11. Every position is fully independent
(rule #11) -- nothing here ever nets or merges positions.
"""
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import config
import telegram_alert


@dataclass
class Position:
    id: str
    symbol: str
    side: str              # "BUY" or "SELL"
    entry_type: str        # "PRIMARY" or "SECONDARY"
    entry_price: float
    initial_sl: float
    size: float
    risk_distance: float   # abs(entry_price - initial_sl)
    current_sl: float
    status: str = "OPEN"   # OPEN -> CLOSED
    partial_exit_done: bool = False
    max_r_locked: int = 0
    remaining_fraction: float = 1.0   # 1.0 -> 0.5 after 1R partial exit
    realized_pnl_points: float = 0.0
    opened_at: float = field(default_factory=time.time)
    closed_at: Optional[float] = None

    @property
    def sign(self) -> int:
        return 1 if self.side == "BUY" else -1

    def price_at_r(self, r: float) -> float:
        return self.entry_price + self.sign * r * self.risk_distance

    def current_r(self, price: float) -> float:
        return self.sign * (price - self.entry_price) / self.risk_distance


class PaperBroker:
    def __init__(self):
        self.positions: list[Position] = []

    # ------------------------------------------------------------------
    # Opening
    # ------------------------------------------------------------------
    def open_position(self, symbol: str, side: str, entry_type: str,
                       entry_price: float, initial_sl: float) -> Position:
        risk_distance = abs(entry_price - initial_sl)
        if risk_distance <= 0:
            raise ValueError("SL distance must be > 0")
        size = round(config.MAX_RISK_POINTS / risk_distance, 6)  # rule #7

        pos = Position(
            id=str(uuid.uuid4())[:8],
            symbol=symbol, side=side, entry_type=entry_type,
            entry_price=entry_price, initial_sl=initial_sl,
            size=size, risk_distance=risk_distance, current_sl=initial_sl,
        )
        self.positions.append(pos)

        telegram_alert.send(
            f"🟢 <b>NEW {entry_type} {side}</b> {symbol}\n"
            f"Entry: {entry_price:.2f} | Initial SL: {initial_sl:.2f}\n"
            f"Size: {size} | Risk: {config.MAX_RISK_POINTS} pts | id={pos.id}"
        )
        return pos

    # ------------------------------------------------------------------
    # Per-tick management (rule #9 trailing, #11 independence)
    # ------------------------------------------------------------------
    def update_all(self, prices: dict):
        """prices: {symbol: last_price}. Call every poll."""
        for pos in self.positions:
            if pos.status != "OPEN":
                continue
            price = prices.get(pos.symbol)
            if price is None:
                continue
            self._update_position(pos, price)

    def _update_position(self, pos: Position, price: float):
        r = pos.current_r(price)

        # --- 1R: partial exit + move to breakeven, start trailing ---
        if not pos.partial_exit_done and r >= 1:
            pos.partial_exit_done = True
            pos.remaining_fraction = 0.5
            pos.current_sl = pos.entry_price  # breakeven
            pos.max_r_locked = 1
            realized = 0.5 * pos.size * pos.risk_distance * 1  # 1R profit on half size
            pos.realized_pnl_points += realized
            telegram_alert.send(
                f"🟡 <b>1R HIT</b> {pos.symbol} {pos.side} id={pos.id}\n"
                f"Closed 50% @ ~{price:.2f} | SL moved to Break Even ({pos.entry_price:.2f})"
            )

        # --- 2R, 3R, 4R...: trail SL to previous R level ---
        elif pos.partial_exit_done:
            r_level = math.floor(r)
            if r_level >= 2 and r_level > pos.max_r_locked:
                new_sl = pos.price_at_r(r_level - 1)
                pos.current_sl = new_sl
                pos.max_r_locked = r_level
                telegram_alert.send(
                    f"🔵 <b>{r_level}R HIT</b> {pos.symbol} {pos.side} id={pos.id}\n"
                    f"Trailing SL moved to {r_level - 1}R level = {new_sl:.2f}"
                )

        # --- check SL hit (closes remaining position) ---
        hit = (pos.sign == 1 and price <= pos.current_sl) or \
              (pos.sign == -1 and price >= pos.current_sl)
        if hit:
            self._close_position(pos, price)

    def _close_position(self, pos: Position, exit_price: float):
        move = pos.sign * (exit_price - pos.entry_price)
        realized = pos.remaining_fraction * pos.size * move
        pos.realized_pnl_points += realized
        pos.status = "CLOSED"
        pos.closed_at = time.time()

        emoji = "✅" if pos.realized_pnl_points >= 0 else "❌"
        telegram_alert.send(
            f"{emoji} <b>CLOSED</b> {pos.symbol} {pos.side} id={pos.id}\n"
            f"Exit: {exit_price:.2f} | Total realized: {pos.realized_pnl_points:.4f} pts"
        )

    # ------------------------------------------------------------------
    def open_positions_for(self, symbol: str):
        return [p for p in self.positions if p.symbol == symbol and p.status == "OPEN"]

    def summary(self) -> str:
        open_pos = [p for p in self.positions if p.status == "OPEN"]
        closed_pos = [p for p in self.positions if p.status == "CLOSED"]
        realized = sum(p.realized_pnl_points for p in closed_pos)
        return (f"Open: {len(open_pos)} | Closed: {len(closed_pos)} | "
                f"Realized pts (closed only): {realized:.4f}")
