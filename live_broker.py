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


def round_price(symbol: str, price: float):
    """Round a price to the symbol's allowed precision before sending to
    Shark. When precision is 0 (e.g. ETHINR needs whole-rupee prices),
    returns a plain int -- a float like 249606.0 still serializes with a
    decimal point in JSON, which Shark's "precision should be less than 1"
    check can reject just like a real fractional price."""
    precision = config.PRICE_PRECISION_BY_SYMBOL.get(symbol, 2)
    rounded = round(price, precision)
    return int(rounded) if precision == 0 else rounded


def api_price(symbol: str, price):
    """Format an already-rounded price for the order-placement API call.
    Tried sending it as a JSON string for precision-0 symbols (ETHINR) to
    test whether that fixed "Signature mismatched" on STOP_MARKET entries --
    it didn't, AND it broke the previously-passing precision check on
    LIMIT entries (Shark's precision validator apparently can't handle a
    string price at all). Reverted to passing the value through unchanged;
    round_price() already returns a plain int for precision-0 symbols,
    which is what actually satisfied Shark's precision check before.
    The STOP_MARKET signature-mismatch issue remains unresolved -- it is
    NOT caused by int-vs-string price formatting."""
    return price


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
    breakeven_notified: bool = False
    breakeven_warned: bool = False
    last_r_notified: int = 0
    last_r_warned: int = 0

    opened_at: float = field(default_factory=time.time)
    closed_at: Optional[float] = None

    @property
    def sign(self) -> int:
        return 1 if self.side == "BUY" else -1

    def price_at_r(self, r: float) -> float:
        raw = self.entry_price + self.sign * r * self.risk_distance
        return round_price(self.symbol, raw)

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
        # Shark rejects order prices with more decimals than its tick size
        # allows (e.g. "Price precision should be less than 3") -- CPR-level
        # math can produce longer floats, so round here BEFORE anything
        # (size, risk_distance, alerts) is derived from these two values.
        entry_price = round_price(symbol, entry_price)
        initial_sl = round_price(symbol, initial_sl)
        risk_distance = abs(entry_price - initial_sl)
        size = round(max_risk / risk_distance, config.QUANTITY_PRECISION_BY_SYMBOL.get(symbol, 3))
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

        def _do_place():
            if needs_stop:
                return api.place_entry_order(
                    trade.symbol, trade.side, "STOP_MARKET", trade.size,
                    stop_price=api_price(trade.symbol, trade.entry_price),
                    stop_loss_price=api_price(trade.symbol, trade.initial_sl),
                )
            else:
                return api.place_entry_order(
                    trade.symbol, trade.side, "LIMIT", trade.size,
                    price=api_price(trade.symbol, trade.entry_price),
                    stop_loss_price=api_price(trade.symbol, trade.initial_sl),
                )

        try:
            resp = _do_place()
        except Exception as e:
            # A single retry after a brief pause -- some failures here have
            # turned out to be one-off network/timing glitches (e.g. a
            # signature mismatch on an order type that has otherwise placed
            # fine moments before/after) rather than a genuine, repeatable
            # bug, so it's worth one quick second attempt before giving up
            # and cancelling the sibling leg.
            print(f"[live_broker] place_entry_order failed for {trade.entry_type} "
                  f"{trade.symbol}, retrying once: {e}")
            time.sleep(1.0)
            try:
                resp = _do_place()
            except Exception as e2:
                telegram_alert.send(
                    f"WARNING: Failed to place {trade.entry_type} {trade.side} {trade.symbol} "
                    f"(after 1 retry): {e2}"
                )
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
            if available is not None:
                # Shark's wallet API returns this field as a string (e.g. "1234.56"),
                # not a number -- cast it so the comparison below works correctly.
                try:
                    available = float(available)
                except (TypeError, ValueError):
                    print(f"[live_broker] could not parse balance value as float: {available!r}")
                    available = None
            if available is None:
                print(f"[live_broker] wallet response missing/unparseable balance field, raw: {wallet}")
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
                # Place the 50%-at-1R take-profit as a resting order on the
                # exchange RIGHT NOW, rather than waiting for our own polling
                # to notice price touched 1R -- a poll is up to a minute
                # behind, and if price reverses in that gap the exchange
                # would never have had a live order to fill, turning a
                # would-be profit into a loss instead. Resting it immediately
                # means the exchange's own matching engine fills it the
                # instant 1R is touched, no matter how briefly.
                self._place_partial_tp(trade)
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

    def _refresh_sl_client_order_id(self, trade: LivePosition) -> bool:
        """Re-fetch the CURRENT SL order's clientOrderId before editing it.
        Shark replaces the SL order (new clientOrderId) whenever the
        underlying net position's size changes -- e.g. a partial TP fill, or
        another entry merging into the same netted position -- and the OLD
        clientOrderId we cached right after entry becomes stale, showing up
        only as a "linkId" reference on the new order. Editing with the
        stale id fails with "not found" (error 3060) even though a live SL
        order genuinely exists.

        IMPORTANT: when two sibling trades on the same symbol each have
        their OWN separate SL order open at once, there is more than one
        STOP_LOSS-subtype order to choose from -- picking "the first one"
        (the original behaviour) silently cross-wires siblings' SL orders
        together. Disambiguate using the order's "price" field, which
        Shark appears to keep FIXED at the order's original placement
        price forever (unlike "stopPrice", which changes every time the
        SL is trailed, and can therefore coincidentally collide between
        two siblings once either has been edited) -- match against
        trade.initial_sl, which is exactly that original value, and is
        unique per trade since two siblings' initial stops are normally
        far enough apart.

        Returns True if a current SL order was found (trade.sl_client_order_id
        updated in place)."""
        try:
            open_orders = api.get_open_orders(trade.symbol)
            candidates = [o for o in open_orders if o.get("subType") == "STOP_LOSS"]
            if not candidates:
                return False
            best = min(candidates, key=lambda o: abs((o.get("price") or 0) - trade.initial_sl))
            # "price" is supposed to stay fixed at the order's original
            # placement value forever, so a genuine match should be exact
            # (or a hair off from float/rounding) -- if even the CLOSEST
            # candidate is far from this trade's own initial_sl, none of
            # them is actually this trade's order (e.g. only a sibling's SL
            # remains after this trade's own SL genuinely closed), and
            # returning that mismatched order would wrongly report this
            # trade as still covered.
            if abs((best.get("price") or 0) - trade.initial_sl) > 0.01:
                return False
            trade.sl_client_order_id = best.get("clientOrderId")
            return True
        except Exception as e:
            print(f"[live_broker] could not refresh SL order id for {trade.id}: {e}")
            return False

    def _trade_still_covered(self, trade: LivePosition, open_orders: list) -> bool:
        """Whether THIS SPECIFIC trade's own SL order is still resting.
        Deliberately per-trade, not "is the whole symbol position flat" --
        once multiple sibling trades share one netted position, one
        sibling's SL closing does NOT make the shared position flat while
        others remain open, so checking the whole position was silently
        leaving closed siblings marked OPEN forever.

        IMPORTANT: even the CACHED sl_client_order_id must be verified by
        price, not just "does an order with this id currently exist" --
        once an id has ever been cross-wired to the wrong trade (from
        before this matching logic existed), it can keep pointing at a
        DIFFERENT trade's still-live order forever, since that id genuinely
        stays present in open_order_ids even though it was never really
        this trade's own order."""
        candidates = [o for o in open_orders if o.get("subType") == "STOP_LOSS"]
        cached = next((o for o in candidates if o.get("clientOrderId") == trade.sl_client_order_id), None)
        if cached and abs((cached.get("price") or 0) - trade.initial_sl) <= 0.01:
            return True
        if not candidates:
            return False
        best = min(candidates, key=lambda o: abs((o.get("price") or 0) - trade.initial_sl))
        if abs((best.get("price") or 0) - trade.initial_sl) > 0.01:
            return False
        trade.sl_client_order_id = best.get("clientOrderId")
        return True

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

        # Safety net: a trade whose SL move/trail failed this cycle already
        # had its max_r_locked advanced (so the correct NEW target keeps
        # getting computed), but its own r-level check above won't fire
        # again next poll unless price reaches a further R-level. Re-run
        # reconcile for every symbol with a partial-exit trade so a stuck
        # edit keeps getting retried every poll until it succeeds, without
        # needing a fresh price trigger.
        symbols_needing_retry = {t.symbol for t in self.trades
                                  if t.status == "OPEN" and t.partial_exit_done}
        for symbol in symbols_needing_retry:
            try:
                self._reconcile_sl_orders(symbol)
            except Exception as e:
                print(f"[live_broker] safety-net reconcile failed for {symbol}: {e}")

    def _update_one_trade(self, trade: LivePosition, high: float, low: float):
        favorable = high if trade.sign == 1 else low

        # The 50%-at-1R take-profit is now placed as a resting order the
        # moment the entry fills (see poll_pending_entries), not triggered
        # here from candle high/low -- this function only handles trailing
        # the SL further once that partial exit has actually happened.
        if trade.partial_exit_done:
            r_level = math.floor(trade.current_r(favorable))
            if r_level >= 2 and r_level > trade.max_r_locked:
                trade.max_r_locked = r_level
                # Route through the same reconciler used for the breakeven
                # move -- editing just the stop_price still gets rejected
                # with "quantity out of range" if a SIBLING trade's SL
                # quantity on this symbol is currently stale, since Shark
                # validates the combined SL/TP quantity budget on ANY edit
                # to the position, not just edits that change quantity.
                self._reconcile_sl_orders(trade.symbol)

    def _place_partial_tp(self, trade: LivePosition):
        tp_side = "SELL" if trade.side == "BUY" else "BUY"
        tp_price = trade.price_at_r(1)
        tp_qty = round(trade.size * 0.5, config.QUANTITY_PRECISION_BY_SYMBOL.get(trade.symbol, 3))
        try:
            resp = api.place_reduce_only_order(trade.symbol, tp_side, trade.position_id, tp_qty,
                                                api_price(trade.symbol, tp_price))
            trade.tp_client_order_id = resp.get("clientOrderId")
            telegram_alert.send(
                f"1R take-profit order placed (resting) {trade.symbol} {trade.side} id={trade.id} -- "
                f"{tp_qty} @ {tp_price:.2f}"
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
            except Exception as e:
                print(f"[live_broker] poll_trade_closures fetch failed for {symbol}: {e}")
                continue
            open_order_ids = {o.get("clientOrderId") for o in open_orders}

            for trade in [t for t in open_trades if t.symbol == symbol]:
                my_sl_covered = self._trade_still_covered(trade, open_orders)
                tp_gone = (trade.tp_client_order_id and not trade.partial_exit_done
                           and trade.tp_client_order_id not in open_order_ids)

                if tp_gone and my_sl_covered:
                    # MY OWN SL order is still resting (reduced) -- the TP
                    # genuinely filled and took 50% off. This is the only
                    # case where "TP order gone" safely means "TP filled".
                    self._on_partial_tp_filled(trade)
                elif tp_gone and not my_sl_covered:
                    # MY OWN SL closed the WHOLE size before the TP ever got
                    # a chance to fill, and the exchange auto-cancelled the
                    # now-dangling TP as a side effect. Treat this as a
                    # full SL loss, NOT a 1R partial fill -- crediting a
                    # partial-profit here would be reporting a profit that
                    # never actually happened.
                    self._on_sl_full_close(trade)
                    continue

                if trade.partial_exit_done and not my_sl_covered:
                    self._on_final_close(trade)
                elif not trade.partial_exit_done and trade.sl_client_order_id and not my_sl_covered:
                    self._on_final_close(trade)

            self._reconcile_sl_orders(symbol)

    def _on_partial_tp_filled(self, trade: LivePosition):
        trade.partial_exit_done = True
        trade.remaining_fraction = 0.5
        trade.max_r_locked = 1
        realized = 0.5 * trade.size * trade.risk_distance * 1
        trade.realized_pnl_points += realized
        telegram_alert.send(
            f"1R CONFIRMED FILLED {trade.symbol} {trade.side} id={trade.id}\n"
            f"Moving SL to breakeven..."
        )

    def _intended_sl_price(self, trade: LivePosition) -> float:
        if not trade.partial_exit_done:
            return trade.initial_sl
        if trade.max_r_locked >= 2:
            return trade.price_at_r(trade.max_r_locked - 1)
        return trade.entry_price

    def _intended_sl_qty(self, trade: LivePosition) -> float:
        frac = trade.remaining_fraction if trade.partial_exit_done else 1.0
        return round(frac * trade.size, config.QUANTITY_PRECISION_BY_SYMBOL.get(trade.symbol, 3))

    def _reconcile_sl_orders(self, symbol: str):
        """Drive every open trade's SL order on this symbol to its correct
        (quantity, price) within THIS call -- no waiting for a future poll
        cycle. Shark enforces total(SL quantity across ALL orders on a
        position) <= the position's actual size at every moment, so editing
        several siblings' SL quantities in the wrong order can transiently
        exceed that shared budget and get rejected ("quantity out of
        range", error 3010) even though everyone's FINAL target is
        individually valid. Fix: run up to 2 ordered passes in one call --
        pass 1 processes the sibling holding the largest CURRENT (most
        stale) quantity first, shrinking it only as far as necessary to
        make room for the others; pass 2 brings everyone the rest of the
        way to their true target now that room exists."""
        siblings = [t for t in self.trades if t.symbol == symbol and t.status == "OPEN"
                    and t.sl_client_order_id]
        if not siblings:
            return

        for t in siblings:
            self._refresh_sl_client_order_id(t)

        moved = {}
        for _pass in range(2):
            try:
                open_orders = api.get_open_orders(symbol)
                positions = api.get_positions("OPEN", symbol)
            except Exception as e:
                print(f"[live_broker] reconcile: could not fetch state for {symbol}: {e}")
                return
            # "positionAmount" has been observed to under-report the true
            # size (e.g. 0.31 when the real total was 0.347) -- "quantity"
            # is the field that actually matches the combined footprint of
            # every resting SL/TP order, so use that for the budget check.
            position_size = sum(p.get("quantity", p.get("positionAmount", 0)) for p in positions)
            current_qty = {}
            for t in siblings:
                o = next((o for o in open_orders if o.get("clientOrderId") == t.sl_client_order_id), None)
                current_qty[t.id] = o.get("orderAmount") if o else None

            order = sorted(siblings, key=lambda t: current_qty.get(t.id) or 0, reverse=True)
            for t in order:
                if moved.get(t.id):
                    continue
                cur = current_qty.get(t.id)
                if cur is None:
                    continue  # no live SL order found for this trade right now
                target_qty = self._intended_sl_qty(t)
                target_price = self._intended_sl_price(t)
                others_total = sum(
                    (current_qty.get(o.id) or 0) for o in siblings if o.id != t.id and not moved.get(o.id)
                ) + sum(
                    self._intended_sl_qty(o) for o in siblings if o.id != t.id and moved.get(o.id)
                )
                headroom = position_size - others_total
                # Round DOWN (never up) to the symbol's required precision.
                # headroom is computed from raw API floats and can come out
                # with extra decimal places (e.g. 0.17899999...), which
                # Shark rejects outright ("Quantity precision should be
                # less than 4", error 3006); flooring (rather than
                # round-to-nearest) also guarantees we never submit a
                # hair more than the true available headroom.
                precision = config.QUANTITY_PRECISION_BY_SYMBOL.get(t.symbol, 3)
                factor = 10 ** precision
                safe_qty = math.floor(min(target_qty, max(headroom, 0)) * factor) / factor
                if safe_qty <= 0:
                    continue
                already_correct = abs(cur - safe_qty) < 1e-9 and abs(t.current_sl - target_price) < 1e-9
                if already_correct:
                    if safe_qty == target_qty:
                        moved[t.id] = True
                    continue
                try:
                    api.edit_order(t.sl_client_order_id, quantity=safe_qty,
                                    stop_price=api_price(t.symbol, target_price))
                    current_qty[t.id] = safe_qty
                    if safe_qty == target_qty:
                        t.current_sl = target_price
                        moved[t.id] = True
                except Exception as e:
                    # Error 3066 ("Another Edit request is already ongoing")
                    # is a brief race from firing consecutive edits on the
                    # same position within the same call -- one short retry
                    # resolves it without waiting a full poll cycle.
                    if "3066" in str(e):
                        time.sleep(0.5)
                        try:
                            api.edit_order(t.sl_client_order_id, quantity=safe_qty,
                                            stop_price=api_price(t.symbol, target_price))
                            current_qty[t.id] = safe_qty
                            if safe_qty == target_qty:
                                t.current_sl = target_price
                                moved[t.id] = True
                        except Exception as e2:
                            print(f"[live_broker] reconcile: retry after 3066 failed for {t.id}: {e2}")
                    else:
                        print(f"[live_broker] reconcile: edit failed for {t.id}: {e}")

        # Report the outcome (success once per new R-level reached; a
        # failure is warned about once per attempted R-level, but does NOT
        # block a later success message once the conflict resolves).
        for t in siblings:
            if not t.partial_exit_done:
                continue
            if t.max_r_locked == 1:
                if not t.breakeven_notified and moved.get(t.id):
                    t.breakeven_notified = True
                    telegram_alert.send(
                        f"SL moved to breakeven ({t.current_sl:.2f}) for {t.symbol} {t.side} id={t.id}"
                    )
                elif not t.breakeven_warned and not moved.get(t.id):
                    t.breakeven_warned = True
                    telegram_alert.send(
                        f"WARNING: Could not fully move SL to breakeven for {t.symbol} {t.side} id={t.id} "
                        f"this cycle (shared quantity budget with a sibling order) -- will keep retrying."
                    )
            elif t.max_r_locked >= 2:
                if moved.get(t.id) and t.max_r_locked > t.last_r_notified:
                    t.last_r_notified = t.max_r_locked
                    telegram_alert.send(
                        f"{t.max_r_locked}R HIT {t.symbol} {t.side} id={t.id}\n"
                        f"SL trailed to {t.current_sl:.2f}"
                    )
                elif not moved.get(t.id) and t.max_r_locked > t.last_r_warned:
                    t.last_r_warned = t.max_r_locked
                    telegram_alert.send(
                        f"WARNING: Failed to trail SL to {t.max_r_locked}R for {t.symbol} {t.side} id={t.id} "
                        f"this cycle (shared quantity budget with a sibling order) -- will keep retrying."
                    )

    def _on_sl_full_close(self, trade: LivePosition):
        """Position went from OPEN straight to fully closed, with the 1R TP
        never having filled -- a genuine full-size SL loss. Cancel the now-
        dangling TP (the exchange may have already auto-cancelled it, but
        clean up defensively) and record the ACTUAL loss, instead of the old
        behaviour of blindly crediting a +1R partial profit that never
        happened just because the TP order disappeared from open orders."""
        if trade.tp_client_order_id:
            try:
                api.delete_order(trade.tp_client_order_id)
            except Exception as e:
                print(f"[live_broker] cancel of dangling TP failed for {trade.id}: {e}")
        trade.realized_pnl_points += -1 * trade.size * trade.risk_distance
        trade.status = "CLOSED"
        trade.closed_at = time.time()
        telegram_alert.send(
            f"SL HIT (full loss) {trade.symbol} {trade.side} id={trade.id}\n"
            f"1R take-profit never filled -- closed at original SL, full size."
        )

    def _on_final_close(self, trade: LivePosition):
        # If SL fired before the resting 1R take-profit ever filled, that TP
        # order is now dangling (reduce-only against a position that's gone
        # flat) -- cancel it so it can't sit around and unexpectedly interact
        # with a future position on this symbol.
        if not trade.partial_exit_done and trade.tp_client_order_id:
            try:
                api.delete_order(trade.tp_client_order_id)
            except Exception as e:
                print(f"[live_broker] cancel of dangling TP failed for {trade.id}: {e}")
        # Record the remaining leg's P&L too -- previously this was never
        # computed at all, silently leaving realized_pnl_points at whatever
        # the partial-fill credited (or 0), even though the position's
        # closing price (approximated by current_sl, since Shark's exact
        # fill price isn't fetched here) may differ from that.
        if trade.partial_exit_done:
            trade.realized_pnl_points += (
                trade.remaining_fraction * trade.size * trade.sign
                * (trade.current_sl - trade.entry_price)
            )
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
                try:
                    api.delete_order(trade.sl_client_order_id)
                except Exception as e:
                    print(f"[live_broker] SL delete failed (possibly stale id) for {trade.id}: {e}")
            if trade.tp_client_order_id:
                try:
                    api.delete_order(trade.tp_client_order_id)
                except Exception as e:
                    print(f"[live_broker] TP delete failed (possibly stale id) for {trade.id}: {e}")
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
