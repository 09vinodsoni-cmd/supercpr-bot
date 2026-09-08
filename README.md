# Super CPR Strategy — Paper Trading Bot

Paper trading bot for your Super CPR strategy on Shark Exchange. **No real
orders are ever placed** — it only reads public candle data and simulates
trades, sending you a Telegram alert (and console log) at every step.

## What it does

- Runs **two fully independent CPR engines**, each on its own candles:
  - **ETHUSDT** → computes its own CPR, only takes **BUY** signals (paper long)
  - **ETHINR** → computes its own CPR, only takes **SELL** signals (paper short)
- 4-hour CPR blocks (these align exactly with your 5:30 AM–9:30 AM IST etc.
  blocks, since 5:30 AM IST = 00:00 UTC)
- One signal per new block, entry only within the first two 1-hour candles
- Primary entry (Upper/Lower CPR) → falls back to secondary (R1/S1) only if
  primary was never touched
- Position sizing: 10-point max risk per trade
- 1R → close 50%, SL to breakeven, then trailing SL at every subsequent R level
- Every position is 100% independent — multiple can be open at once, never merged
- Telegram alert on: new signal, entry fill, 1R/BE, each trailing SL move, final exit
- State saved to `state.json` (survives restarts) + trade log in `trade_log.csv`

## Setup — Option A: Run on GitHub Actions (no laptop/VPS needed)

1. Create a **private** GitHub repo and upload all these files (including
   the `.github/workflows/paper-bot.yml` file — make sure the `.github`
   folder comes through, GitHub's drag-and-drop upload UI sometimes hides
   folders starting with a dot, so if it doesn't appear, create it manually
   via "Add file → Create new file" and paste in `paper-bot.yml`'s content
   at path `.github/workflows/paper-bot.yml`).
2. In the repo, go to **Settings → Secrets and variables → Actions → New
   repository secret** and add two secrets:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   (This keeps your token out of the code, even though a fallback copy is
   also in `config.py` for convenience — for a private repo either is fine.)
3. Go to the **Actions** tab of your repo. You should see "Super CPR Paper
   Bot" listed. Click it, then **Run workflow** to trigger it manually once
   and confirm it works (check for a Telegram alert + a new commit updating
   `state.json`).
4. After that first manual run works, it will run automatically every ~5
   minutes via the schedule in the workflow file. No server needed.

**Known GitHub Actions quirks to expect:**
- Runs can be delayed a few (sometimes 10-15) minutes during GitHub-wide
  high load — this is a platform limitation, not a bug in the bot.
- If the repo goes fully inactive, GitHub *can* pause scheduled workflows —
  the bot's own state-committing keeps the repo "active", but keep an eye
  on the Actions tab occasionally to confirm runs are still happening.
- You can watch every run's logs under the **Actions** tab.

## Setup — Option B: Run continuously on your own laptop/VPS

```bash
pip install -r requirements.txt
python bot.py
```

Leave the script running (in a `screen`/`tmux` session, or as a systemd
service / background process) — it polls every 5 minutes in an infinite
loop. This gives more precise timing than GitHub Actions but needs a
machine that's always on.

## ⚠️ Before you trust this fully — please verify

1. **Klines API schema is an educated guess.** Shark Exchange's public docs
   page (docs.sharkexchange.in) got cut off right before the "Get Klines"
   section's exact parameters/response shape. `shark_api.py` follows the
   conventions used by every other endpoint in their docs (camelCase params,
   standard OHLC fields) and includes a flexible parser that handles both
   dict-style and array-style candles. **The very first time you run this,
   watch the console output closely** — if `get_klines` throws an error or
   returns obviously wrong prices, the fix is only in `shark_api.py`
   (`get_klines()` and `_parse_klines()`), nothing else needs to change.
2. **Symbol names** (`ETHUSDT`, `ETHINR`) — confirm these match exactly what
   Shark Exchange uses (case, no slash, etc.) via their `/v1/exchange`
   endpoint or app.
3. **Touch detection uses 5-minute candle High/Low ranges, not just a
   snapshot.** Every poll looks at the aggregated high/low across all
   5-minute candles covering the relevant window (the whole entry window
   for entry checks; since-last-poll for trailing SL/R-levels). This means
   a price wick that touches a level and reverts BETWEEN two 5-minute polls
   is still caught, because the exchange's own candle already recorded
   that high/low. The one edge case: if a single 5-min candle both tags a
   profit level AND reverts to hit the (now-moved) SL within that same
   candle, the bot conservatively assumes the SL was hit (can't know the
   exact order within one candle) -- this matches how most backtesting
   engines handle same-candle ambiguity.
4. This is a **paper bot only**. Before ever connecting real order placement
   (`/v1/order/place-order`, which needs your API key + HMAC signature),
   run this for at least a few weeks and compare its logged trades against
   what you'd have taken manually.

## Files

| File | Purpose |
|---|---|
| `config.py` | All settings — symbols, risk, timeframes, Telegram creds |
| `shark_api.py` | Public klines fetch (candle data only, no auth) |
| `cpr_engine.py` | Pure CPR math + BUY/SELL/INSIDE signal classification |
| `paper_broker.py` | Simulated positions: sizing, SL, 1R partial exit, trailing |
| `telegram_alert.py` | Sends alerts to your Telegram chat |
| `state_store.py` | Saves/restores state so restarts don't lose track |
| `bot.py` | Main loop — ties everything together |

## Adjusting risk / timeframes

Everything tunable lives in `config.py` — `MAX_RISK_POINTS_BY_SYMBOL`,
`SL_BUFFER_POINTS_BY_SYMBOL` (per-symbol dicts, since ETHINR points are
~100x the size of ETHUSDT points for the same real risk), `ENTRY_CANDLE_INTERVAL`,
`POLL_INTERVAL_SECONDS`, etc. No other file needs touching for those kinds
of changes.

## Simulated capital / leverage / margin (live-trading rehearsal)

This is preparation for eventual live trading, tested here in paper mode
first:

- `STARTING_CAPITAL_INR` (config.py) is the pretend account size.
- Each trade's leverage is calculated from its **own** SL distance:
  `leverage = min(MAX_LEVERAGE_CAP, 100 / (sl_percent * LEVERAGE_SAFETY_CUSHION))`
  — tighter SL → higher leverage (up to the cap); wider SL → lower leverage.
  This keeps liquidation always further away than our own SL, with a
  configurable safety cushion.
- If a symbol already has open position(s) and a new one stacks on top of
  it, **all** positions on that symbol switch to the SAFEST (lowest)
  leverage among them — mirroring the real exchange, where leverage is a
  single per-symbol setting, not per-position. Each trade's own SL order
  stays completely independent regardless.
- Before opening any trade, the bot checks whether the required margin
  (converted to INR for USDT-quoted ETHUSDT, using the live ETHINR/ETHUSDT
  rate) fits within remaining capital. If not, the trade is **skipped**
  with a Telegram alert — never force-opened.
- `trade_log.csv` now includes `leverage` and `sl_percent` columns, and the
  console summary line shows current margin usage, so you can watch how
  this behaves over several days of paper trading before any real money
  is involved.
