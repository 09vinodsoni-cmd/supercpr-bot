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
    leverage: float = 0.0        # this trade's own SL-implied leverage
    sl_percent: float = 0.0      # abs(entry-sl)/entry * 100

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
        self.capital_inr = config.STARTING_CAPITAL_INR
        # Margin currently locked per symbol, always in INR (converted for
        # USDT-quoted symbols using the live rate) -- mirrors what the real
        # exchange account's per-symbol isolated margin would show.
        self.used_margin_inr_by_symbol: dict = {}
        self.inr_per_usdt: float = 0.0  # set every poll from live ETHINR/ETHUSDT prices

    def set_rate(self, inr_per_usdt: float):
        self.inr_per_usdt = inr_per_usdt

    def _to_inr(self, amount_native: float, symbol: str) -> float:
        if config.QUOTE_CURRENCY_BY_SYMBOL[symbol] == "INR":
            return amount_native
        # USDT-quoted -- convert using the live rate; if we don't have one
        # yet (very first poll), fall back to a rough placeholder so we
        # never divide/multiply by zero.
        rate = self.inr_per_usdt if self.inr_per_usdt > 0 else 95.0
        return amount_native * rate

    @staticmethod
    def _leverage_for_sl_percent(sl_percent: float) -> float:
        if sl_percent <= 0:
            return config.MAX_LEVERAGE_CAP
        raw = 100 / (sl_percent * config.LEVERAGE_SAFETY_CUSHION)
        return max(1.0, min(config.MAX_LEVERAGE_CAP, raw))

    def _other_symbols_margin_inr(self, symbol: str) -> float:
        return sum(m for sym, m in self.used_margin_inr_by_symbol.items() if sym != symbol)

    def recompute_margins_from_positions(self):
        """Rebuild used_margin_inr_by_symbol from currently-OPEN positions.
        Needed after loading state from disk, since each --once run starts
        a fresh process -- the margin dict itself isn't persisted, but it's
        fully derivable from each position's stored size/entry_price/leverage."""
        self.used_margin_inr_by_symbol = {}
        by_symbol: dict = {}
        for p in self.positions:
            if p.status == "OPEN":
                by_symbol.setdefault(p.symbol, []).append(p)
        for symbol, positions in by_symbol.items():
            if not positions:
                continue
            leverage = positions[0].leverage or config.MAX_LEVERAGE_CAP
            total_value_native = sum(p.size * p.entry_price for p in positions)
            margin_native = total_value_native / leverage
            self.used_margin_inr_by_symbol[symbol] = self._to_inr(margin_native, symbol)

    # ------------------------------------------------------------------
    # Opening
    # ------------------------------------------------------------------
    def open_position(self, symbol: str, side: str, entry_type: str,
                       entry_price: float, initial_sl: float) -> Optional[Position]:
        risk_distance = abs(entry_price - initial_sl)
        if risk_distance <= 0:
            raise ValueError("SL distance must be > 0")
        max_risk = config.MAX_RISK_POINTS_BY_SYMBOL[symbol]
        size = round(max_risk / risk_distance, 6)  # rule #7
        sl_percent = (risk_distance / entry_price) * 100

        candidate = Position(
            id=str(uuid.uuid4())[:8],
            symbol=symbol, side=side, entry_type=entry_type,
            entry_price=entry_price, initial_sl=initial_sl,
            size=size, risk_distance=risk_distance, current_sl=initial_sl,
            sl_percent=sl_percent,
        )

        # Rule (live-trading design, tested here first): if this symbol
        # already has open position(s), the exchange only has ONE net
        # position per symbol -- so leverage must be the SAFEST (lowest)
        # across ALL positions stacked on it (existing + this new one).
        existing = self.open_positions_for(symbol)
        stack = existing + [candidate]
        safe_leverage = min(self._leverage_for_sl_percent(p.sl_percent) for p in stack)

        total_position_value_native = sum(p.size * p.entry_price for p in stack)
        new_total_margin_native = total_position_value_native / safe_leverage
        new_total_margin_inr = self._to_inr(new_total_margin_native, symbol)

        other_margin_inr = self._other_symbols_margin_inr(symbol)
        if other_margin_inr + new_total_margin_inr > self.capital_inr:
            telegram_alert.send(
                f"🚫 <b>TRADE SKIPPED (insufficient margin)</b> {symbol} {side}\n"
                f"Would need ₹{new_total_margin_inr:,.0f} for this symbol "
                f"(₹{other_margin_inr:,.0f} already used elsewhere), "
                f"capital is ₹{self.capital_inr:,.0f}."
            )
            return None

        # Apply the (possibly revised) safe leverage to every position in
        # the stack, since the exchange's single per-symbol leverage
        # setting now covers all of them.
        for p in existing:
            p.leverage = safe_leverage
        candidate.leverage = safe_leverage
        self.used_margin_inr_by_symbol[symbol] = new_total_margin_inr

        self.positions.append(candidate)

        telegram_alert.send(
            f"🟢 <b>NEW {entry_type} {side}</b> {symbol}\n"
            f"Entry: {entry_price:.2f} | Initial SL: {initial_sl:.2f}\n"
            f"Size: {size} | Risk: {max_risk} pts | Leverage: {safe_leverage:.1f}x\n"
            f"Symbol margin now: ₹{new_total_margin_inr:,.0f} | id={candidate.id}"
        )
        return candidate

    # ------------------------------------------------------------------
    # Per-tick management (rule #9 trailing, #11 independence)
    # ------------------------------------------------------------------
    def update_all(self, price_ranges: dict):
        """price_ranges: {symbol: (highest_high, lowest_low, last_close)}
        covering everything since the last poll. Using a range (not a
        single snapshot) means a wick that touched a level between two
        polls is still caught, since the exchange's own candle recorded
        that high/low even if we weren't watching at that exact second."""
        for pos in self.positions:
            if pos.status != "OPEN":
                continue
            rng = price_ranges.get(pos.symbol)
            if rng is None or rng[0] is None:
                continue
            high, low, last_close = rng
            self._update_position(pos, high, low)

    def _update_position(self, pos: Position, high: float, low: float):
        # The extreme that moves the trade favorably (for R-level tracking)
        # vs. the extreme that would have hit its stop.
        favorable_extreme = high if pos.sign == 1 else low
        adverse_extreme = low if pos.sign == 1 else high

        r_fav = pos.current_r(favorable_extreme)

        # --- 1R: partial exit + move to breakeven, start trailing ---
        if not pos.partial_exit_done and r_fav >= 1:
            pos.partial_exit_done = True
            pos.remaining_fraction = 0.5
            pos.current_sl = pos.entry_price  # breakeven
            pos.max_r_locked = 1
            realized = 0.5 * pos.size * pos.risk_distance * 1  # 1R profit on half size
            pos.realized_pnl_points += realized
            telegram_alert.send(
                f"🟡 <b>1R HIT</b> {pos.symbol} {pos.side} id={pos.id}\n"
                f"Closed 50% (high/low touched {favorable_extreme:.2f}) | "
                f"SL moved to Break Even ({pos.entry_price:.2f})"
            )

        # --- 2R, 3R, 4R...: trail SL to previous R level ---
        elif pos.partial_exit_done:
            r_level = math.floor(r_fav)
            if r_level >= 2 and r_level > pos.max_r_locked:
                new_sl = pos.price_at_r(r_level - 1)
                pos.current_sl = new_sl
                pos.max_r_locked = r_level
                telegram_alert.send(
                    f"🔵 <b>{r_level}R HIT</b> {pos.symbol} {pos.side} id={pos.id}\n"
                    f"Trailing SL moved to {r_level - 1}R level = {new_sl:.2f}"
                )

        # --- check SL hit using the adverse extreme (catches wicks too) ---
        hit = (pos.sign == 1 and adverse_extreme <= pos.current_sl) or \
              (pos.sign == -1 and adverse_extreme >= pos.current_sl)
        if hit:
            # Assume the fill happened at the SL price itself (a stop order
            # fills at/near its trigger level), not at the raw wick tip.
            self._close_position(pos, exit_price=pos.current_sl)

    def _close_position(self, pos: Position, exit_price: float):
        move = pos.sign * (exit_price - pos.entry_price)
        realized = pos.remaining_fraction * pos.size * move
        pos.realized_pnl_points += realized
        pos.status = "CLOSED"
        pos.closed_at = time.time()

        # Recompute this symbol's margin/leverage now that one fewer
        # position is stacked on it (frees margin for future trades).
        remaining = self.open_positions_for(pos.symbol)
        if remaining:
            safe_leverage = min(self._leverage_for_sl_percent(p.sl_percent) for p in remaining)
            total_value_native = sum(p.size * p.entry_price for p in remaining)
            new_margin_native = total_value_native / safe_leverage
            for p in remaining:
                p.leverage = safe_leverage
            self.used_margin_inr_by_symbol[pos.symbol] = self._to_inr(new_margin_native, pos.symbol)
        else:
            self.used_margin_inr_by_symbol.pop(pos.symbol, None)

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
        total_margin_used = sum(self.used_margin_inr_by_symbol.values())
        return (f"Open: {len(open_pos)} | Closed: {len(closed_pos)} | "
                f"Realized pts (closed only): {realized:.4f} | "
                f"Margin used: ₹{total_margin_used:,.0f} / ₹{self.capital_inr:,.0f}")
