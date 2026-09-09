"""
Minimal Telegram alert sender + inbound command fetcher. Uses stdlib only
(urllib) so no extra dependency is required just for alerts.
"""
import json
import urllib.request
import urllib.error
import urllib.parse

import config

# Static reply-keyboard buttons -- attached to every outgoing message by
# default, so the control panel is always visible/refreshed in the chat.
# Tapping any of these sends a REAL text message (visible + saved in chat
# history), unlike Telegram's silent inline "callback" buttons.
STATIC_KEYBOARD_ROWS = [
    ["ON", "OFF"],
    ["MANUAL", "AUTO"],
    ["EMERGENCY STOP"],
    ["SNAPSHOT"],
    ["PAUSE ETHUSDT", "RESUME ETHUSDT"],
    ["PAUSE ETHINR", "RESUME ETHINR"],
]


def _build_reply_markup(rows):
    return {
        "keyboard": [[{"text": label} for label in row] for row in rows],
        "resize_keyboard": True,
    }


def send(message: str, keyboard_rows=None) -> None:
    """Send a message to the configured Telegram chat. Never raises --
    a failed alert should never crash the trading loop.

    keyboard_rows: list of lists of button labels. Defaults to the static
    command keyboard so it stays visible/refreshed after every message.
    Pass [] explicitly to send with no keyboard at all."""
    print(f"[ALERT] {message}")  # always echo to console too

    if keyboard_rows is None:
        keyboard_rows = STATIC_KEYBOARD_ROWS

    if not config.TELEGRAM_ENABLED:
        return
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload_dict = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }
    if keyboard_rows:
        payload_dict["reply_markup"] = _build_reply_markup(keyboard_rows)
    payload = json.dumps(payload_dict).encode("utf-8")

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


def get_updates(offset: int = None, timeout: int = 0) -> list:
    """Fetch pending incoming messages (button presses / typed commands)
    since `offset`. Returns [] on any failure -- never raises, so a
    Telegram hiccup never crashes the trading loop."""
    if not config.TELEGRAM_ENABLED or not config.TELEGRAM_BOT_TOKEN:
        return []

    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates?{urllib.parse.urlencode(params)}"

    try:
        with urllib.request.urlopen(url, timeout=timeout + 10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not data.get("ok"):
            print(f"[telegram] getUpdates failed: {data}")
            return []
        return data.get("result", [])
    except Exception as e:
        print(f"[telegram] getUpdates error: {e}")
        return []
