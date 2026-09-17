"""
LIVE trading broker. Places REAL orders on Shark Exchange and manages them
through their full lifecycle. This mirrors paper_broker.py's per-position
independence (every trade has its own entry/SL/R-tracking, tracked in OUR
state even though the exchange nets same-symbol positions into one), but
every state transition here corresponds to a REAL signed API call.

WARNING: Never live-tested end to end (sandboxed dev environment cannot
reach api.sharkexchange.in). Built strictly from docs.sharkexchange.in.
The first real run IS the first real test -- watch Telegram for error
alerts, which have consistently included the exchange's own error body
throughout this project's development, making mismatches fixable.

Architecture (confirmed with you over this whole build):
  1. New block opens -> place BOTH Primary and Secondary entry orders
     immediately (resting on the exchange), each with stopLossPrice
     attached (protects instantly on fill, zero lag).
  2. Poll: whichever fills first, cancel the other.
  3. Once open (positionId known): watch for 1R (using the same
     range-based High/Low check as paper mode) -> place a reduce-only
     LIMIT order for 50% qty at the 1R price (the partial take-profit).
  4. When that reduce-only order fills -> edit the existing SL order:
     quantity -> 50%, price -> breakeven.
  5. 2R, 3R, ... -> edit the same SL order's price only.
  6. SL eventually fills on the exchange itself -> position closed.
  7. Leverage: set via update/preference BEFORE placing entries (safest
     leverage across everything stacked on that symbol, same math as
     paper mode) -- since it's a per-symbol exchange setting, not
     per-order.
"""
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import config
import shark_trading_api as api
import telegram_alert


@dataclass
class LivePosition:
    """Our own tracking record for ONE trade -- independent of whatever the
    exchange shows as the (possibly netted/averaged) position for this
    symbol. This is what rule #11 independence is built on."""
    id: str
    symbol: str
    side: str                      # "BUY" or "SELL"
    entry_type: str                # "PRIMARY" or "SECONDARY"
    status: str = "PENDING"        # PENDING -> OPEN -> CLOSED (or CANCELLED)

    entry_price: float = 0.0       # intended entry level (pre-set, per your rule)
    initial_sl: float = 0.0
    size: float = 0.0
    risk_distance: float = 0.0
    leverage: float = 0.0
    sl_percent: float = 0.0

    entry_client_order_id: Optional[str] = None
    other_leg_client_order_id: Optional[str] = None
    sl_client_order_id: Optional[str] = None
    tp_client_order_id: Optional[str] = None
    position_id: Optional[str] = None
    block_open_time_ms: int = 0    # for cancelling PENDING legs once the entry window expires

    partial_exit_done: bool = False
    max_r_locked: int = 0
    current_sl: float = 0.0
    remaining_fraction: float = 1.0
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


class LiveBroker:
    def __init__(self):
        self.trades: list[LivePosition] = []

    def place_dual_entries(self, symbol: str, side: str, cpr, sl_buffer: float,
                            current_price: float, safe_leverage: float,
                            block_open_time_ms: int):
        if config.LEVERAGE_API_BLOCKED:
            print(f"[live_broker] Skipping update/preference call (blocked) -- "
                  f"relying on manually-set {config.FIXED_LEVERAGE_WHILE_BLOCKED}x for {symbol}.")
        else:
            try:
                api.set_leverage_and_margin_mode(symbol, safe_leverage, "ISOLATED")
            except Exception as e:
                telegram_alert.send(f"WARNING: Failed to set leverage for {symbol}: {e}")
                return

        if side == "BUY":
            primary_entry, primary_sl = cpr.upper_cpr, cpr.s1 - sl_buffer
            secondary_entry, secondary_sl = cpr.r1, cpr.lower_cpr - sl_buffer
        else:
            primary_entry, primary_sl = cpr.lower_cpr, cpr.r1 + sl_buffer
            secondary_entry, secondary_sl = cpr.s1, cpr.upper_cpr + sl_buffer

        max_risk = config.MAX_RISK_POINTS_BY_SYMBOL[symbol]

        primary = self._build_trade(symbol, side, "PRIMARY", primary_entry, primary_sl,
                                     max_risk, safe_leverage, block_open_time_ms)
        secondary = self._build_trade(symbol, side, "SECONDARY", secondary_entry, secondary_sl,
                                       max_risk, safe_leverage, block_open_time_ms)

        primary_order = self._place_one_leg(primary, current_price)
        secondary_order = self._place_one_leg(secondary, current_price)

        if primary_order is None or secondary_order is None:
            if primary_order is not None:
                self._cancel_trade(primary)
            if secondary_order is not None:
                self._cancel_trade(secondary)
            return

        primary.other_leg_client_order_id = secondary.entry_client_order_id
        secondary.other_leg_client_order_id = primary.entry_client_order_id
        self.trades.append(primary)
        self.trades.append(secondary)

    def _build_trade(self, symbol, side, entry_type, entry_price, initial_sl, max_risk,
                      leverage, block_open_time_ms) -> LivePosition:
        risk_distance = abs(entry_price - initial_sl)
        size = round(max_risk / risk_distance, 6)
        return LivePosition(
            id=str(uuid.uuid4())[:8], symbol=symbol, side=side, entry_type=entry_type,
            entry_price=entry_price, initial_sl=initial_sl, size=size,
            risk_distance=risk_distance, current_sl=initial_sl,
            leverage=leverage, sl_percent=(risk_distance / entry_price) * 100,
            block_open_time_ms=block_open_time_ms,
        )

    def _place_one_leg(self, trade: LivePosition, current_price: float):
        if trade.side == "BUY":
            needs_stop = trade.entry_price > current_price
        else:
            needs_stop = trade.entry_price < current_price

        try:
            if needs_stop:
                resp = api.place_entry_order(
                    trade.symbol, trade.side, "STOP_MARKET", trade.size,
                    stop_price=trade.entry_price, stop_loss_price=trade.initial_sl,
                )
            else:
                resp = api.place_entry_order(
                    trade.symbol, trade.side, "LIMIT", trade.size,
                    price=trade.entry_price, stop_loss_price=trade.initial_sl,
                )
        except Exception as e:
            telegram_alert.send(f"WARNING: Failed to place {trade.entry_type} {trade.side} {trade.symbol}: {e}")
            return None

        trade.entry_client_order_id = resp.get("clientOrderId")
        telegram_alert.send(
            f"LIVE {trade.entry_type} {trade.side} placed {trade.symbol}\n"
            f"Entry: {trade.entry_price:.2f} | SL: {trade.initial_sl:.2f} | "
            f"Size: {trade.size} | Leverage: {trade.leverage:.1f}x\n"
            f"clientOrderId={trade.entry_client_order_id}"
        )
        return resp

    def _cancel_trade(self, trade: LivePosition):
        if trade.entry_client_order_id:
            try:
                api.delete_order(trade.entry_client_order_id)
            except Exception as e:
                print(f"[live_broker] cancel failed for {trade.entry_client_order_id}: {e}")
        trade.status = "CANCELLED"

    def cancel_expired_pending(self, window_ms: int):
        """Cancel any PENDING trade whose entry window has closed without
        a fill -- the live-mode equivalent of NO TRADE."""
        now_ms = int(time.time() * 1000)
        for trade in [t for t in self.trades if t.status == "PENDING"]:
            if now_ms >= trade.block_open_time_ms + window_ms:
                self._cancel_trade(trade)
                telegram_alert.send(
                    f"NO TRADE (live) {trade.symbol} {trade.entry_type} {trade.side} id={trade.id} "
                    f"-- entry window closed without a fill, order cancelled."
                )

    def check_and_place_dual_entries(self, symbol: str, side: str, cpr, sl_buffer: float,
                                      current_price: float, block_open_time_ms: int):
        """Computes leverage from SL%, checks REAL account balance (so
        brokerage fees / referral credits / anything else are automatically
        reflected -- we never re-derive capital ourselves in live mode),
        and only places the dual entries if margin is available."""
        if side == "BUY":
            primary_sl_dist = abs(cpr.upper_cpr - (cpr.s1 - sl_buffer))
            secondary_sl_dist = abs(cpr.r1 - (cpr.lower_cpr - sl_buffer))
            entry_ref_price = cpr.upper_cpr
        else:
            primary_sl_dist = abs(cpr.lower_cpr - (cpr.r1 + sl_buffer))
            secondary_sl_dist = abs(cpr.s1 - (cpr.upper_cpr + sl_buffer))
            entry_ref_price = cpr.lower_cpr

        primary_sl_pct = (primary_sl_dist / entry_ref_price) * 100
        secondary_sl_pct = (secondary_sl_dist / entry_ref_price) * 100

        if config.LEVERAGE_API_BLOCKED:
            # Workaround: use the fixed leverage manually set in the Shark
            # app (see config.py comment) instead of computing dynamically,
            # since the update/preference API currently rejects all requests.
            #
            # Safety check: this fixed leverage is only safe if SL% stays
            # within the cushion's bounds. If a block's SL is ever wider
            # than that (unusual volatility spike), skip rather than risk
            # liquidation happening before our own SL would.
            max_safe_sl_pct = 100 / (config.FIXED_LEVERAGE_WHILE_BLOCKED * config.LEVERAGE_SAFETY_CUSHION)
            if primary_sl_pct > max_safe_sl_pct or secondary_sl_pct > max_safe_sl_pct:
                telegram_alert.send(
                    f"TRADE SKIPPED (SL too wide for fixed {config.FIXED_LEVERAGE_WHILE_BLOCKED}x leverage) "
                    f"{symbol} {side}\n"
                    f"Primary SL%={primary_sl_pct:.2f}, Secondary SL%={secondary_sl_pct:.2f}, "
                    f"max safe={max_safe_sl_pct:.2f}%. Waiting for next block."
                )
                return
            safe_leverage = config.FIXED_LEVERAGE_WHILE_BLOCKED
        else:
            safe_leverage = min(
                self._leverage_for_sl_percent(primary_sl_pct),
                self._leverage_for_sl_percent(secondary_sl_pct),
            )

        max_risk = config.MAX_RISK_POINTS_BY_SYMBOL[symbol]
        approx_size = max_risk / min(primary_sl_dist, secondary_sl_dist)
        needed_margin_native = (approx_size * entry_ref_price) / safe_leverage
        if config.QUOTE_CURRENCY_BY_SYMBOL[symbol] == "INR":
            needed_margin_inr = needed_margin_native
        else:
            rate = 95.0  # fallback if the live-rate fetch below fails
            try:
                import shark_api  # local import -- candle-data module, avoids a top-level circular concern
                usdt_price = shark_api.get_last_price("ETHUSDT")
                inr_price = shark_api.get_last_price("ETHINR")
                if usdt_price and inr_price:
                    rate = inr_price / usdt_price
            except Exception as e:
                print(f"[live_broker] rate fetch failed, using fallback {rate}: {e}")
            needed_margin_inr = needed_margin_native * rate

        try:
            wallet = api.get_futures_wallet()
            available = wallet.get("withdrawableBalance", wallet.get("availableBalance"))
            if available is None:
                print(f"[live_broker] wallet response missing expected balance field, raw: {wallet}")
        except Exception as e:
            telegram_alert.send(f"WARNING: Could not fetch wallet balance, skipping entry as a precaution: {e}")
            return

        if available is None or available < needed_margin_inr:
            telegram_alert.send(
                f"TRADE SKIPPED (insufficient real margin) {symbol} {side}\n"
                f"Need ~₹{needed_margin_inr:,.0f}, available ₹{available if available is not None else 'unknown'}."
            )
            return

        self.place_dual_entries(symbol, side, cpr, sl_buffer, current_price, safe_leverage, block_open_time_ms)

    @staticmethod
    def _leverage_for_sl_percent(sl_percent: float) -> float:
        if sl_percent <= 0:
            return config.MAX_LEVERAGE_CAP
        raw = 100 / (sl_percent * config.LEVERAGE_SAFETY_CUSHION)
        return max(1.0, min(config.MAX_LEVERAGE_CAP, raw))

    def poll_pending_entries(self):
        pending = [t for t in self.trades if t.status == "PENDING"]
        if not pending:
            return
        symbols = {t.symbol for t in pending}
        for symbol in symbols:
            try:
                open_orders = api.get_open_orders(symbol)
                open_positions = api.get_positions("OPEN", symbol)
            except Exception as e:
                print(f"[live_broker] poll_pending_entries fetch failed for {symbol}: {e}")
                continue
            open_order_ids = {o.get("clientOrderId") for o in open_orders}

            for trade in [t for t in pending if t.symbol == symbol]:
                still_open = trade.entry_client_order_id in open_order_ids
                if still_open:
                    continue

                matching_pos = self._match_position(open_positions, trade)
                if matching_pos is None:
                    trade.status = "CANCELLED"
                    telegram_alert.send(
                        f"{trade.entry_type} {trade.side} {trade.symbol} id={trade.id} "
                        f"no longer resting and no matching position -- treating as not filled."
                    )
                    continue

                trade.status = "OPEN"
                trade.position_id = matching_pos.get("positionId")
                self._fetch_sl_client_order_id(trade)
                telegram_alert.send(
                    f"FILLED {trade.entry_type} {trade.side} {trade.symbol} id={trade.id}\n"
                    f"positionId={trade.position_id}"
                )
                sibling = next((t for t in pending
                                 if t.other_leg_client_order_id == trade.entry_client_order_id
                                 and t.status == "PENDING"), None)
                if sibling:
                    self._cancel_trade(sibling)
                    telegram_alert.send(
                        f"Cancelled sibling {sibling.entry_type} {sibling.side} "
                        f"{sibling.symbol} id={sibling.id} (other leg filled)."
                    )

    @staticmethod
    def _match_position(open_positions: list, trade: LivePosition):
        want_type = "LONG" if trade.side == "BUY" else "SHORT"
        candidates = [p for p in open_positions if p.get("positionType") == want_type]
        if not candidates:
            return None
        return candidates[0]

    def _fetch_sl_client_order_id(self, trade: LivePosition):
        try:
            linked = api.get_linked_orders(trade.entry_client_order_id)
            sl_order = next((o for o in linked if o.get("linkType") == "ORDER_SL"), None)
            if sl_order:
                trade.sl_client_order_id = sl_order.get("clientOrderId")
        except Exception as e:
            print(f"[live_broker] could not fetch linked SL order for {trade.id}: {e}")

    def update_open_trades(self, price_ranges: dict):
        for trade in [t for t in self.trades if t.status == "OPEN"]:
            rng = price_ranges.get(trade.symbol)
            if rng is None or rng[0] is None:
                continue
            high, low, _ = rng
            try:
                self._update_one_trade(trade, high, low)
            except Exception as e:
                print(f"[live_broker] update failed for {trade.id}: {e}")
                telegram_alert.send(f"WARNING: Trailing-update error for {trade.symbol} id={trade.id}: {e}")

    def _update_one_trade(self, trade: LivePosition, high: float, low: float):
        favorable = high if trade.sign == 1 else low

        if not trade.partial_exit_done and trade.current_r(favorable) >= 1:
            self._place_partial_tp(trade)
            return

        if trade.partial_exit_done:
            r_level = math.floor(trade.current_r(favorable))
            if r_level >= 2 and r_level > trade.max_r_locked:
                new_sl = trade.price_at_r(r_level - 1)
                if trade.sl_client_order_id:
                    api.edit_order(trade.sl_client_order_id, price=new_sl)
                    trade.current_sl = new_sl
                    trade.max_r_locked = r_level
                    telegram_alert.send(
                        f"{r_level}R HIT {trade.symbol} {trade.side} id={trade.id}\n"
                        f"SL trailed to {new_sl:.2f}"
                    )

    def _place_partial_tp(self, trade: LivePosition):
        tp_side = "SELL" if trade.side == "BUY" else "BUY"
        tp_price = trade.price_at_r(1)
        tp_qty = round(trade.size * 0.5, 6)
        try:
            resp = api.place_reduce_only_order(trade.symbol, tp_side, trade.position_id, tp_qty, tp_price)
            trade.tp_client_order_id = resp.get("clientOrderId")
            telegram_alert.send(
                f"1R reached {trade.symbol} {trade.side} id={trade.id} -- "
                f"placed partial TP: {tp_qty} @ {tp_price:.2f}"
            )
        except Exception as e:
            telegram_alert.send(f"WARNING: Failed to place partial TP for {trade.id}: {e}")

    def poll_trade_closures(self):
        open_trades = [t for t in self.trades if t.status == "OPEN"]
        if not open_trades:
            return
        symbols = {t.symbol for t in open_trades}
        for symbol in symbols:
            try:
                open_orders = api.get_open_orders(symbol)
                open_positions = api.get_positions("OPEN", symbol)
            except Exception as e:
                print(f"[live_broker] poll_trade_closures fetch failed for {symbol}: {e}")
                continue
            open_order_ids = {o.get("clientOrderId") for o in open_orders}
            still_has_open_position = len(open_positions) > 0

            for trade in [t for t in open_trades if t.symbol == symbol]:
                if (trade.tp_client_order_id and not trade.partial_exit_done
                        and trade.tp_client_order_id not in open_order_ids):
                    self._on_partial_tp_filled(trade)

                if trade.partial_exit_done and not still_has_open_position:
                    self._on_final_close(trade)
                elif not trade.partial_exit_done and trade.sl_client_order_id and \
                        trade.sl_client_order_id not in open_order_ids and not still_has_open_position:
                    self._on_final_close(trade)

    def _on_partial_tp_filled(self, trade: LivePosition):
        trade.partial_exit_done = True
        trade.remaining_fraction = 0.5
        trade.max_r_locked = 1
        breakeven = trade.entry_price
        if trade.sl_client_order_id:
            try:
                api.edit_order(trade.sl_client_order_id, quantity=round(trade.size * 0.5, 6), price=breakeven)
                trade.current_sl = breakeven
            except Exception as e:
                telegram_alert.send(f"WARNING: Failed to move SL to breakeven for {trade.id}: {e}")
        realized = 0.5 * trade.size * trade.risk_distance * 1
        trade.realized_pnl_points += realized
        telegram_alert.send(
            f"1R CONFIRMED FILLED {trade.symbol} {trade.side} id={trade.id}\n"
            f"SL moved to breakeven ({breakeven:.2f})"
        )

    def _on_final_close(self, trade: LivePosition):
        trade.status = "CLOSED"
        trade.closed_at = time.time()
        telegram_alert.send(
            f"CLOSED (live) {trade.symbol} {trade.side} id={trade.id}\n"
            f"Final SL/exit -- check exchange trade history for exact fill price."
        )

    def emergency_stop_all(self):
        try:
            api.cancel_all_orders()
            api.close_all_positions()
            for t in self.trades:
                if t.status == "PENDING":
                    t.status = "CANCELLED"
                elif t.status == "OPEN":
                    t.status = "CLOSED"
                    t.closed_at = time.time()
            telegram_alert.send("EMERGENCY STOP (live) -- all orders cancelled, all positions closed.")
        except Exception as e:
            telegram_alert.send(f"WARNING: EMERGENCY STOP failed partway: {e}. Check the exchange directly.")

    def close_one(self, trade_id: str):
        trade = next((t for t in self.trades if t.id == trade_id and t.status == "OPEN"), None)
        if not trade:
            telegram_alert.send(f"No OPEN live trade found with id={trade_id}.")
            return
        try:
            if trade.sl_client_order_id:
                api.delete_order(trade.sl_client_order_id)
            if trade.tp_client_order_id:
                api.delete_order(trade.tp_client_order_id)
            api.close_all_positions(trade.symbol)
            trade.status = "CLOSED"
            trade.closed_at = time.time()
            telegram_alert.send(f"Manually closed {trade.symbol} {trade.side} id={trade.id}")
        except Exception as e:
            telegram_alert.send(f"WARNING: Failed to close {trade_id}: {e}")

    def summary(self) -> str:
        open_trades = [t for t in self.trades if t.status == "OPEN"]
        pending = [t for t in self.trades if t.status == "PENDING"]
        closed = [t for t in self.trades if t.status == "CLOSED"]
        return (f"LIVE -- Pending: {len(pending)} | Open: {len(open_trades)} | "
                f"Closed: {len(closed)}")
