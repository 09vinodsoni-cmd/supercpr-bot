"""
Minimal Telegram alert sender. Uses stdlib only (urllib) so no extra
dependency is required just for alerts.
"""
import json
import urllib.request
import urllib.error

import config


def send(message: str) -> None:
    """Send a message to the configured Telegram chat. Never raises --
    a failed alert should never crash the trading loop."""
    print(f"[ALERT] {message}")  # always echo to console too

    if not config.TELEGRAM_ENABLED:
        return
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }).encode("utf-8")

    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except urllib.error.URLError as e:
        print(f"[ALERT] Telegram send failed: {e}")
    except Exception as e:
        print(f"[ALERT] Telegram send failed: {e}")
