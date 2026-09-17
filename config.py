"""
Super CPR Strategy - Configuration
Edit the values below to match your setup. No code-logic changes needed here.
"""

# ---------------------------------------------------------------------------
# Shark Exchange API
# ---------------------------------------------------------------------------
BASE_URL = "https://api.sharkexchange.in"

import os  # noqa: E402

# ---------------------------------------------------------------------------
# LIVE trading (real orders, real money) -- OFF by default.
# Set to "LIVE" only when ready; keep "PAPER" for continued simulation.
# ---------------------------------------------------------------------------
TRADING_MODE = os.environ.get("TRADING_MODE", "PAPER")  # "PAPER" or "LIVE"

SHARK_API_KEY = os.environ.get("SHARK_API_KEY", "")
SHARK_API_SECRET = os.environ.get("SHARK_API_SECRET", "")

# Klines are a PUBLIC endpoint (no api-key/signature needed for paper trading).
# We only need candle data, since this is a PAPER bot -- no real orders are sent.
KLINES_ENDPOINT = "/v1/market/klines"

# ---------------------------------------------------------------------------
# Symbols
#
# Two fully independent CPR engines run in parallel, each on its own candles
# (per strategy rule #10/#11 -- separate broker/account, own everything):
#
#   ETHUSDT -> computes its own CPR from ETHUSDT candles.
#              Only BUY signals from THIS engine are paper-traded (long only).
#   ETHINR  -> computes its own CPR from ETHINR candles.
#              Only SELL signals from THIS engine are paper-traded (short only).
#
# A BUY signal appearing on the ETHINR engine is logged but NOT traded (and
# vice versa for a SELL signal on the ETHUSDT engine), since each account
# only takes one side per your setup.
# ---------------------------------------------------------------------------
SYMBOL_ENGINES = {
    "ETHUSDT": {"take_side": "BUY"},
    "ETHINR": {"take_side": "SELL"},
}

# ---------------------------------------------------------------------------
# Timeframes
# ---------------------------------------------------------------------------
# 4-hour CPR block interval as understood by the exchange's klines API.
# NOTE: 5:30 AM IST == 00:00 UTC, so your 6 IST-aligned 4-hour blocks
# (5:30-9:30, 9:30-13:30, ... ) are EXACTLY the same as standard UTC-aligned
# 4-hour candles (00:00-04:00, 04:00-08:00, ...). No custom offset math needed.
CPR_BLOCK_INTERVAL = "4h"

# Candle size used to check the "first two candles" entry window rule.
ENTRY_CANDLE_INTERVAL = "1h"

# Fine-grained interval used to detect touches/wicks that happen BETWEEN
# polls (e.g. a price spike at 11:32 that reverts before the 11:35 poll).
# The exchange's own 5m candle still records that high/low even if we
# weren't polling at that exact second, so this closes most of the gap
# without needing to poll more often.
TOUCH_CHECK_INTERVAL = "5m"
RANGE_FETCH_LIMIT = 100  # ~8 hours of 5m candles -- comfortably covers a 4h block

# How many entry-timeframe candles are valid for entry after a new CPR block starts.
ENTRY_WINDOW_CANDLES = 2

# ---------------------------------------------------------------------------
# Risk / position sizing
# ---------------------------------------------------------------------------
# Max risk points differ per symbol because ETHINR points are ~100x the size
# of ETHUSDT points (INR vs USD quoting) for the same real-world risk.
MAX_RISK_POINTS_BY_SYMBOL = {
    "ETHUSDT": 10,
    "ETHINR": 1000,
}

SL_BUFFER_POINTS_BY_SYMBOL = {
    "ETHUSDT": 2,
    "ETHINR": 200,  # same ~100x scaling as the risk buffer above
}

# ---------------------------------------------------------------------------
# Simulated capital / leverage / margin (mirrors what real live trading will
# do -- tested here in paper mode first before any real money is involved)
# ---------------------------------------------------------------------------
STARTING_CAPITAL_INR = 70000

# Leverage is calculated PER TRADE from its own SL distance:
#   leverage = min(MAX_LEVERAGE_CAP, 100 / (sl_percent * LEVERAGE_SAFETY_CUSHION))
# Cushion=1.5 means liquidation stays 50% further away than our own SL.
LEVERAGE_SAFETY_CUSHION = 1.5
MAX_LEVERAGE_CAP = 30

# ---------------------------------------------------------------------------
# TEMPORARY WORKAROUND: Shark Exchange's leverage/margin-mode-setting API
# currently rejects every request ("Cross margin is not enabled" for both
# CROSS and ISOLATED regardless of parameters). A support ticket is open;
# until Shark fixes it, live_broker.py cannot set leverage dynamically per
# trade via the API, so it falls back to a fixed leverage that was set
# ONCE manually through the Shark app's UI for ETHUSDT.
#
#   LEVERAGE_API_BLOCKED = True  -> use FIXED_LEVERAGE_WHILE_BLOCKED instead
#                                   of computing leverage dynamically.
#   FIXED_LEVERAGE_WHILE_BLOCKED  -> must match whatever leverage is actually
#                                    set on Shark's app right now (25x).
#
# live_broker.py also checks that each trade's SL% stays within what this
# fixed leverage can safely support (via LEVERAGE_SAFETY_CUSHION above) and
# skips the trade instead of risking liquidation before our own SL if a
# block's SL is ever unusually wide.
#
# IMPORTANT: once Shark support resolves the API issue, set
# LEVERAGE_API_BLOCKED back to False to resume dynamic per-trade leverage.
# ---------------------------------------------------------------------------
LEVERAGE_API_BLOCKED = True
FIXED_LEVERAGE_WHILE_BLOCKED = 25

# What currency each symbol's price/margin is denominated in. ETHUSDT margin
# is in USDT and gets converted to INR (using the live ETHINR/ETHUSDT ratio)
# so it can be checked against one unified INR capital pool alongside ETHINR.
QUOTE_CURRENCY_BY_SYMBOL = {
    "ETHUSDT": "USDT",
    "ETHINR": "INR",
}

# ---------------------------------------------------------------------------
# Bot behaviour
# ---------------------------------------------------------------------------
POLL_INTERVAL_SECONDS = 5 * 60  # check every 5 minutes (paper trading)
STATE_FILE = "state.json"
TRADE_LOG_FILE = "trade_log.csv"

# ---------------------------------------------------------------------------
# Telegram alerts (fires on: new signal, entry filled, partial exit /
# breakeven, trailing SL move, final exit)
# ---------------------------------------------------------------------------
TELEGRAM_ENABLED = True

# Read from environment first (this is how GitHub Actions passes in your
# repo Secrets). Falls back to the hardcoded value only for local runs where
# you haven't set the env var -- for GitHub Actions, set these as repo
# Secrets named TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID instead of editing this
# file, so the token never sits in your (possibly public-ish) git history.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
# IMPORTANT NOTE ON THE KLINES API
# ---------------------------------------------------------------------------
# Shark Exchange's public docs (docs.sharkexchange.in) confirm:
#   POST /v1/market/klines  <- public, no auth required
#   Optional param added 22-01-2026: priceType = MARK_PRICE | LAST_PRICE
#
# The exact request/response schema (param names for symbol/interval/limit,
# and the shape of each candle) was cut off in the fetched documentation.
# shark_api.py below uses the common convention seen across the rest of
# Shark's docs (snake-less camelCase params, ISO or epoch-ms timestamps).
# If your real API key reveals a different schema, only shark_api.py's
# get_klines() / _parse_klines() need adjusting -- nothing else in this
# project depends on the raw wire format.
