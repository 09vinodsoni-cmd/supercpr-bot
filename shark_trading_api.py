"""
Signed client for Shark Exchange's AUTHENTICATED (trading) endpoints.

Confirmed from docs.sharkexchange.in:
- Auth: HMAC-SHA256(secret, data_to_sign) where:
    * GET/DELETE : data_to_sign = query string (includes timestamp)
    * POST/PATCH : data_to_sign = json.dumps(params, separators=(',', ':'))
      (compact JSON -- no spaces. The signature MUST be computed over the
      exact same byte string that is sent as the request body, so we sign
      first and then send that exact string as the body -- not re-serialize.)
- Headers: 'api-key': <key>, 'signature': <hex signature>
- Every authenticated request needs a 'timestamp' (epoch ms) in its params.

⚠️ NEVER TESTED against the real API (sandboxed dev environment has no
network access to api.sharkexchange.in). Built strictly from the docs.
The very first live call is the real test -- watch Telegram/console for
the exact error body if something is rejected; error messages have been
very informative every time we hit this exchange (see shark_api.py's
history), so a mismatch here should be equally fixable.
"""
import hashlib
import hmac
import json
import time
import urllib.parse

import requests

import config


class SharkAPIError(Exception):
    def __init__(self, message, status_code=None, response_body=None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


def _get_credentials():
    api_key = config.SHARK_API_KEY
    api_secret = config.SHARK_API_SECRET
    if not api_key or not api_secret:
        raise SharkAPIError(
            "SHARK_API_KEY / SHARK_API_SECRET are not set. "
            "Live trading cannot proceed without them."
        )
    return api_key, api_secret


def _sign(secret: str, data_to_sign: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), data_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _raise_for_bad_response(resp):
    if not resp.ok:
        raise SharkAPIError(
            f"Shark API request failed [{resp.status_code}] "
            f"url={resp.url} response_body={resp.text[:800]}",
            status_code=resp.status_code,
            response_body=resp.text,
        )


def signed_get(endpoint: str, params: dict = None) -> dict:
    api_key, api_secret = _get_credentials()
    params = dict(params or {})
    params["timestamp"] = str(int(time.time() * 1000))

    query_string = urllib.parse.urlencode(params)
    signature = _sign(api_secret, query_string)

    url = f"{config.BASE_URL}{endpoint}"
    resp = requests.get(
        url, params=params,
        headers={"api-key": api_key, "signature": signature, "accept": "*/*"},
        timeout=15,
    )
    _raise_for_bad_response(resp)
    return resp.json()


def signed_post(endpoint: str, params: dict = None) -> dict:
    return _signed_body_request("POST", endpoint, params)


def signed_patch(endpoint: str, params: dict = None) -> dict:
    return _signed_body_request("PATCH", endpoint, params)


def signed_delete(endpoint: str, params: dict = None) -> dict:
    """Shark's own delete example signs a JSON body the same way POST/PATCH
    do (not a query string), and sends it as the DELETE request's body."""
    return _signed_body_request("DELETE", endpoint, params)


def _signed_body_request(method: str, endpoint: str, params: dict) -> dict:
    api_key, api_secret = _get_credentials()
    params = dict(params or {})
    params["timestamp"] = str(int(time.time() * 1000))

    # Compact JSON, matching Shark's own example exactly (separators=(',',':')).
    # We sign THIS exact string and send it verbatim as the raw body, rather
    # than letting requests re-serialize the dict (which could reorder keys
    # or add spaces and invalidate the signature).
    body_str = json.dumps(params, separators=(",", ":"))
    signature = _sign(api_secret, body_str)

    url = f"{config.BASE_URL}{endpoint}"
    resp = requests.request(
        method, url, data=body_str.encode("utf-8"),
        headers={
            "api-key": api_key,
            "signature": signature,
            "Content-Type": "application/json",
        },
        timeout=15,
    )
    _raise_for_bad_response(resp)
    return resp.json()


# ---------------------------------------------------------------------------
# High-level order / position / leverage functions
# ---------------------------------------------------------------------------

def set_leverage_and_margin_mode(symbol: str, leverage: int, margin_mode: str = "ISOLATED") -> dict:
    """MUST be called before placing an entry -- orders execute using
    whatever leverage/margin-mode was PREVIOUSLY set for the symbol (it is
    not a per-order parameter)."""
    return signed_post("/v1/exchange/update/preference", {
        "leverage": int(round(leverage)),
        "marginMode": margin_mode,
        "contractName": symbol,
    })


def place_entry_order(symbol: str, side: str, order_type: str, quantity: float,
                       price: float = None, stop_price: float = None,
                       stop_loss_price: float = None) -> dict:
    """order_type: 'LIMIT' or 'STOP_MARKET' (the two we use for entries --
    see bot's logic for choosing between them based on current price vs
    the entry level). stop_loss_price attaches a linked full-quantity SL
    immediately, protecting the position from the instant it fills."""
    params = {
        "placeType": "ORDER_FORM",
        "quantity": quantity,
        "side": side,
        "symbol": symbol,
        "reduceOnly": False,
        "marginAsset": "INR",  # Shark is INR-settled; margin is INR even for USDT-quoted pairs
        "type": order_type,
    }
    if order_type in ("LIMIT", "STOP_LIMIT"):
        if price is None:
            raise ValueError(f"{order_type} order requires a price")
        params["price"] = price
    if order_type in ("STOP_MARKET", "STOP_LIMIT"):
        if stop_price is None:
            raise ValueError(f"{order_type} order requires a stop_price")
        params["stopPrice"] = stop_price
    if stop_loss_price is not None:
        params["stopLossPrice"] = stop_loss_price
    return signed_post("/v1/order/place-order", params)


def place_reduce_only_order(symbol: str, side: str, position_id: str,
                             quantity: float, price: float) -> dict:
    """Used for the 1R partial take-profit -- a LIMIT order tied to an
    existing position via positionId, reduceOnly=True so it can only ever
    shrink the position (never flip or add to it)."""
    params = {
        "placeType": "POSITION",
        "positionId": position_id,
        "quantity": quantity,
        "side": side,
        "symbol": symbol,
        "reduceOnly": True,
        "marginAsset": "INR",
        "type": "LIMIT",
        "price": price,
    }
    return signed_post("/v1/order/place-order", params)


def edit_order(client_order_id: str, quantity: float = None, price: float = None,
                stop_price: float = None) -> dict:
    params = {"clientOrderId": client_order_id}
    if quantity is not None:
        params["quantity"] = quantity
    if price is not None:
        params["price"] = price
    if stop_price is not None:
        params["stopPrice"] = stop_price
    return signed_patch("/v1/order/edit-order", params)


def delete_order(client_order_id: str) -> dict:
    return signed_delete("/v1/order/delete-order", {"clientOrderId": client_order_id})


def get_open_orders(symbol: str = None) -> list:
    params = {"pageSize": 100, "sortOrder": "desc"}
    if symbol:
        params["symbol"] = symbol
    return signed_get("/v1/order/open-orders", params)


def get_linked_orders(link_id: str) -> list:
    return signed_get(f"/v1/order/linked-orders/{link_id}")


def get_positions(status: str, symbol: str = None) -> list:
    """status: 'OPEN', 'CLOSED', or 'LIQUIDATED'."""
    params = {"pageSize": 100, "sortOrder": "desc"}
    if symbol:
        params["symbol"] = symbol
    return signed_get(f"/v1/positions/{status.upper()}", params)


def close_all_positions(symbol: str = None) -> dict:
    params = {}
    if symbol:
        params["symbol"] = symbol
    return signed_post("/v1/positions/close-all-positions", params)


def cancel_all_orders(symbol: str = None) -> dict:
    params = {}
    if symbol:
        params["symbol"] = symbol
    return signed_post("/v1/order/cancel-all-orders", params)


def get_futures_wallet() -> dict:
    """Real account balance/margin -- source of truth for live capital
    tracking (so brokerage fees and any external credits are automatically
    reflected, since we're reading the exchange's own number, not
    re-deriving it ourselves).

    ⚠️ UNCONFIRMED PATH: the docs page was cut off before showing this
    endpoint's exact URL/params (we only saw the table-of-contents entry
    '#futures-wallet-details'). This guess follows the sibling endpoints'
    naming pattern. If it 404s, check docs.sharkexchange.in's own TOC
    entry directly and update ENDPOINT below -- nothing else depends on
    the exact path."""
    ENDPOINT = "/v1/order/futures-wallet-details"
    return signed_get(ENDPOINT)
