"""
Super CPR Strategy - Paper Trading Bot
=======================================
Runs two fully independent CPR engines (ETHUSDT for BUY, ETHINR for SELL),
each computing its own CPR from its own candles, generating one signal per
new 4-hour block, taking a paper entry (primary or secondary) only during
the first two 1-hour candles of that block, and managing every resulting
position completely independently (partial exit at 1R, breakeven, then
trailing SL every subsequent R level) until its own SL is hit.

No real orders are ever placed. This only reads public candle data and
simulates fills/exits, logging everything to Telegram + console + CSV.

USAGE:
    pip install -r requirements.txt
    python bot.py
"""
import csv
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import config
import cpr_engine
import shark_api
import state_store
import telegram_alert
from paper_broker import PaperBroker


def log_trade_csv(broker: PaperBroker):
    file_exists = os.path.exists(config.TRADE_LOG_FILE)
    with open(config.TRADE_LOG_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "id", "symbol", "side", "entry_type", "entry_price", "initial_sl",
            "current_sl", "size", "leverage", "sl_percent", "status",
            "partial_exit_done", "max_r_locked",
            "realized_pnl_points", "opened_at", "closed_at",
        ])
        for p in broker.positions:
            writer.writerow([
                p.id, p.symbol, p.side, p.entry_type, p.entry_price, p.initial_sl,
                p.current_sl, p.size, round(p.leverage, 2), round(p.sl_percent, 4),
                p.status, p.partial_exit_done, p.max_r_locked,
                round(p.realized_pnl_points, 4),
                datetime.fromtimestamp(p.opened_at, tz=timezone.utc).isoformat(),
                datetime.fromtimestamp(p.closed_at, tz=timezone.utc).isoformat() if p.closed_at else "",
            ])


def _is_930pm_ist_block(open_time_ms: int) -> bool:
    """9:30 PM IST = 16:00 UTC exactly, since blocks are UTC-aligned."""
    dt_utc = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc)
    return dt_utc.hour == 16 and dt_utc.minute == 0


def _format_position_line(idx: int, p) -> str:
    if p.partial_exit_done:
        status = f"1R hit, locked {p.realized_pnl_points:+.2f} | trailing {p.max_r_locked}R, SL {p.current_sl:.2f}"
    else:
        status = f"no partial yet, SL {p.current_sl:.2f}"
    return (f"{idx}. {p.side} {p.symbol} @ {p.entry_price:.2f} | {status} | "
            f"lev {p.leverage:.1f}x | id={p.id}")


def send_live_snapshot(broker: PaperBroker, block_label: str):
    open_positions = [p for p in broker.positions if p.status == "OPEN"]
    total_margin = sum(broker.used_margin_inr_by_symbol.values())
    if open_positions:
        lines = [_format_position_line(i + 1, p) for i, p in enumerate(open_positions)]
        body = "\n".join(lines)
    else:
        body = "(no open positions right now)"
    telegram_alert.send(
        f"📋 <b>Live Positions Snapshot</b> (new block: {block_label})\n"
        f"{body}\n"
        f"Total open: {len(open_positions)} | Margin used: ₹{total_margin:,.0f} / ₹{broker.capital_inr:,.0f}"
    )


def send_daily_summary(broker: PaperBroker, daily_stats: dict, day_start_s: float, day_end_s: float):
    day_positions = [p for p in broker.positions if day_start_s <= p.opened_at < day_end_s]
    total = len(day_positions)
    closed = [p for p in day_positions if p.status == "CLOSED"]
    profitable = [p for p in closed if p.realized_pnl_points > 0]
    losing = [p for p in closed if p.realized_pnl_points < 0]

    net_by_symbol: dict = {}
    trades_by_symbol: dict = {}
    for p in day_positions:
        net_by_symbol[p.symbol] = net_by_symbol.get(p.symbol, 0.0) + p.realized_pnl_points
        trades_by_symbol[p.symbol] = trades_by_symbol.get(p.symbol, 0) + 1

    net_lines = "\n".join(
        f"  {sym}: {net:+.2f} ({config.QUOTE_CURRENCY_BY_SYMBOL[sym]}), {trades_by_symbol[sym]} trade(s)"
        for sym, net in net_by_symbol.items()
    ) or "  (no trades today)"

    currently_open = [p for p in broker.positions if p.status == "OPEN"]
    no_trade_count = daily_stats.get("no_trade_count", 0)

    telegram_alert.send(
        f"📊 <b>DAILY SUMMARY</b> (Yesterday 9:30 PM → Today 9:30 PM)\n"
        f"Total trades taken: {total} | Blocks with NO TRADE: {no_trade_count}\n"
        f"✅ Profitable: {len(profitable)} | ❌ Losing: {len(losing)}\n"
        f"Net P&L by symbol:\n{net_lines}\n"
        f"Currently open (carried forward): {len(currently_open)}"
    )


class SymbolEngine:
    """Tracks CPR block state + entry window for ONE symbol."""

    def __init__(self, symbol: str, take_side: str, broker: PaperBroker, daily_stats: dict):
        self.symbol = symbol
        self.take_side = take_side  # "BUY" or "SELL" -- only this side is ever traded
        self.broker = broker
        self.daily_stats = daily_stats  # shared dict across engines, for NO_TRADE counting

        self.current_cpr = None
        self.previous_cpr = None
        self.active_block_open_time = None
        self.active_signal = None
        self.entered_this_block = False
        self.primary_touched = False
        self.no_trade_alerted = False
        self.last_trailing_check_ms = None  # for since-last-poll range fetches
        self.just_opened_new_block = False  # transient: true only during the poll a new block starts

    def restore(self, saved: dict):
        # (Minimal restore -- CPR objects are cheap to recompute on next poll
        # if not present; we mainly restore the "already entered" flags so we
        # never double-enter the same block after a restart.)
        self.active_block_open_time = saved.get("active_block_open_time")
        self.active_signal = saved.get("active_signal")
        self.entered_this_block = saved.get("entered_this_block", False)
        self.primary_touched = saved.get("primary_touched", False)
        self.no_trade_alerted = saved.get("no_trade_alerted", False)
        self.last_trailing_check_ms = saved.get("last_trailing_check_ms")

    def to_dict(self):
        return {
            "active_block_open_time": self.active_block_open_time,
            "active_signal": self.active_signal,
            "entered_this_block": self.entered_this_block,
            "primary_touched": self.primary_touched,
            "no_trade_alerted": self.no_trade_alerted,
            "last_trailing_check_ms": self.last_trailing_check_ms,
        }

    # ------------------------------------------------------------------
    def poll(self):
        self.just_opened_new_block = False
        try:
            self._check_new_block()
            if self.active_block_open_time is not None:
                self._check_entry_window()
        except Exception as e:
            print(f"[{self.symbol}] poll error: {e}")
            traceback.print_exc()

    def _check_new_block(self):
        blocks = shark_api.get_klines_with_retry(
            self.symbol, config.CPR_BLOCK_INTERVAL, limit=3
        )
        print(f"[{self.symbol}] interval={config.CPR_BLOCK_INTERVAL} -> "
              f"{len(blocks)} candle(s) received")
        if blocks:
            b = blocks[-1]
            print(f"  latest candle: open_time={b.open_time} O={b.open} "
                  f"H={b.high} L={b.low} C={b.close}")
        if len(blocks) < 2:
            print(f"[{self.symbol}] not enough candles yet (need >=2), skipping this poll")
            return  # not enough data yet

        # Last item = currently forming block. The one before it = the most
        # recently COMPLETED block, whose H/L/C becomes the CPR for the
        # forming (new/current) block -- standard CPR methodology.
        forming = blocks[-1]
        just_completed = blocks[-2]

        if self.active_block_open_time == forming.open_time:
            return  # nothing new

        # New block started.
        self.just_opened_new_block = True
        new_cpr = cpr_engine.compute_cpr(
            just_completed.open_time, just_completed.high,
            just_completed.low, just_completed.close,
        )
        self.previous_cpr = self.current_cpr
        self.current_cpr = new_cpr
        self.active_block_open_time = forming.open_time
        self.entered_this_block = False
        self.primary_touched = False
        self.no_trade_alerted = False

        self.active_signal = cpr_engine.classify_signal(
            forming.open, self.current_cpr, self.previous_cpr
        )

        traded_note = "WILL TRADE" if self.active_signal == self.take_side else "logged only (other side)"
        telegram_alert.send(
            f"📊 <b>New CPR block</b> {self.symbol}\n"
            f"Signal: <b>{self.active_signal}</b> ({traded_note})\n"
            f"Upper CPR: {self.current_cpr.upper_cpr:.2f} | Lower CPR: {self.current_cpr.lower_cpr:.2f}\n"
            f"R1: {self.current_cpr.r1:.2f} | S1: {self.current_cpr.s1:.2f}\n"
            f"Block open: {forming.open:.2f}"
        )

    def _hours_since_block_start(self) -> float:
        now_ms = time.time() * 1000
        return (now_ms - self.active_block_open_time) / (1000 * 3600)

    def _check_entry_window(self):
        if self.entered_this_block:
            return
        if self.active_signal != self.take_side:
            return  # this engine only ever trades its configured side

        now_ms = int(time.time() * 1000)
        window_end_ms = self.active_block_open_time + config.ENTRY_WINDOW_CANDLES * 3600 * 1000
        effective_end_ms = min(now_ms, window_end_ms)

        # Look at the FULL window (block start -> now/window-end) each time,
        # not just the current instant -- this catches a wick that touched a
        # level between two polls, and also means a delayed/skipped poll
        # can't cause us to falsely miss a touch that happened earlier in
        # the window.
        high, low, _ = shark_api.get_range_high_low(
            self.symbol, self.active_block_open_time, effective_end_ms
        )
        if high is None:
            print(f"[{self.symbol}] no {config.TOUCH_CHECK_INTERVAL} candle data yet for entry window check")
            return

        cpr = self.current_cpr
        side = self.take_side
        sl_buffer = config.SL_BUFFER_POINTS_BY_SYMBOL[self.symbol]

        # --- Primary entry check (rule #4 / #6) ---
        primary_hit = (side == "BUY" and high >= cpr.upper_cpr) or \
                      (side == "SELL" and low <= cpr.lower_cpr)
        if primary_hit:
            entry_price = cpr.upper_cpr if side == "BUY" else cpr.lower_cpr
            if side == "BUY":
                initial_sl = cpr.s1 - sl_buffer
            else:
                initial_sl = cpr.r1 + sl_buffer
            self.broker.open_position(self.symbol, side, "PRIMARY", entry_price, initial_sl)
            self.entered_this_block = True
            return

        # --- Secondary entry check (rule #5 / #6) -- only if primary NEVER touched ---
        secondary_hit = (side == "BUY" and high >= cpr.r1) or \
                        (side == "SELL" and low <= cpr.s1)
        if secondary_hit:
            entry_price = cpr.r1 if side == "BUY" else cpr.s1
            if side == "BUY":
                initial_sl = cpr.lower_cpr - sl_buffer
            else:
                initial_sl = cpr.upper_cpr + sl_buffer
            self.broker.open_position(self.symbol, side, "SECONDARY", entry_price, initial_sl)
            self.entered_this_block = True
            return

        # --- Neither level touched anywhere in the window -> NO TRADE ---
        if now_ms >= window_end_ms and not self.no_trade_alerted:
            self.no_trade_alerted = True
            self.daily_stats["no_trade_count"] = self.daily_stats.get("no_trade_count", 0) + 1
            telegram_alert.send(
                f"⚪ <b>NO TRADE</b> {self.symbol} -- entry window (first "
                f"{config.ENTRY_WINDOW_CANDLES}h) closed without a fill."
            )


def load_engines_and_broker():
    broker = PaperBroker()
    saved = state_store.load()
    broker.positions = state_store.restore_positions(saved.get("positions", []))
    daily_stats = saved.get("daily_stats") or {"day_start_ms": None, "no_trade_count": 0}

    engines = {}
    for symbol, cfg in config.SYMBOL_ENGINES.items():
        eng = SymbolEngine(symbol, cfg["take_side"], broker, daily_stats)
        if symbol in saved.get("symbol_state", {}):
            eng.restore(saved["symbol_state"][symbol])
        engines[symbol] = eng
    return engines, broker, daily_stats


def run_one_cycle(engines: dict, broker: PaperBroker, daily_stats: dict):
    """One full poll cycle: check for new blocks, check entry windows,
    update trailing SL on open positions, persist state, log trades."""
    # Refresh the INR/USDT conversion rate BEFORE polling, since an entry
    # opened during eng.poll() needs it for margin-in-INR calculation.
    usdt_price = shark_api.get_last_price("ETHUSDT")
    inr_price = shark_api.get_last_price("ETHINR")
    if usdt_price and inr_price:
        broker.set_rate(inr_price / usdt_price)
    broker.recompute_margins_from_positions()

    for eng in engines.values():
        eng.poll()

    # All symbols share the same 4h block boundaries, so if ANY engine just
    # opened a new block, they all did this cycle -- fire the snapshot once.
    new_block_engines = [eng for eng in engines.values() if eng.just_opened_new_block]
    if new_block_engines:
        block_open_ms = new_block_engines[0].active_block_open_time
        block_label = datetime.fromtimestamp(block_open_ms / 1000, tz=timezone.utc).strftime("%H:%M UTC")
        send_live_snapshot(broker, block_label)

        if _is_930pm_ist_block(block_open_ms):
            day_start_ms = daily_stats.get("day_start_ms")
            day_start_s = (day_start_ms / 1000) if day_start_ms else 0.0
            day_end_s = block_open_ms / 1000
            send_daily_summary(broker, daily_stats, day_start_s, day_end_s)
            # Reset for the new day starting now.
            daily_stats["day_start_ms"] = block_open_ms
            daily_stats["no_trade_count"] = 0

    # Build {symbol: (high, low, last_close)} covering everything since the
    # last successful poll for that symbol -- not just an instantaneous
    # snapshot -- so a wick that touched an R-level or SL between two polls
    # is still caught (the exchange's own candle already recorded it).
    now_ms = int(time.time() * 1000)
    price_ranges = {}
    for symbol, eng in engines.items():
        since_ms = eng.last_trailing_check_ms
        if since_ms is None:
            since_ms = now_ms - config.POLL_INTERVAL_SECONDS * 1000 * 3  # safety margin on first run
        high, low, last_close = shark_api.get_range_high_low(symbol, since_ms, now_ms)
        if high is not None:
            price_ranges[symbol] = (high, low, last_close)
        eng.last_trailing_check_ms = now_ms

    broker.update_all(price_ranges)

    symbol_state = {sym: eng.to_dict() for sym, eng in engines.items()}
    state_store.save(symbol_state, broker.positions, daily_stats)
    log_trade_csv(broker)

    print(f"[{datetime.now().isoformat()}] {broker.summary()}")


def main():
    run_once = "--once" in sys.argv  # used by the GitHub Actions workflow

    engines, broker, daily_stats = load_engines_and_broker()

    if run_once:
        # Single cycle then exit -- GitHub Actions runs this on a cron
        # schedule, checking out fresh state.json each time and committing
        # the updated one back at the end of the workflow.
        try:
            run_one_cycle(engines, broker, daily_stats)
        except Exception as e:
            print(f"[bot] single-cycle error: {e}")
            traceback.print_exc()
            telegram_alert.send(f"⚠️ Bot error: {e}")
        return

    # Continuous mode -- for running on your own laptop/VPS.
    telegram_alert.send(
        f"🚀 Super CPR paper bot started. Symbols: {list(config.SYMBOL_ENGINES.keys())}"
    )
    while True:
        try:
            run_one_cycle(engines, broker, daily_stats)
        except Exception as e:
            print(f"[bot] main loop error: {e}")
            traceback.print_exc()
            telegram_alert.send(f"⚠️ Bot error: {e}")
        time.sleep(config.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
