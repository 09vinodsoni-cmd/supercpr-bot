"""
Standalone diagnostic script for the LIVE trading API integration.
Run this manually on the VPS to test authentication, wallet fetch, and a
SAFE place+cancel order cycle -- all WITHOUT waiting for a real CPR signal.

Usage (on the VPS, same environment as the bot):
    python3 test_live_api.py

Needs the same env vars as bot.py: SHARK_API_KEY, SHARK_API_SECRET.

Test 4 places a tiny test order at a price 50% away from the current
market price specifically so it CANNOT realistically fill, then cancels
it immediately -- this validates the full place-order -> cancel-order
round trip without real trade risk.
"""
import shark_trading_api as api
import shark_api as candles

print("=" * 60)
print("TEST 1: Wallet balance fetch")
print("=" * 60)
try:
    wallet = api.get_futures_wallet()
    print("SUCCESS. Raw response:")
    print(wallet)
except Exception as e:
    print(f"FAILED: {e}")

print()
print("=" * 60)
print("TEST 2: Get open positions")
print("=" * 60)
try:
    positions = api.get_positions("OPEN")
    print(f"SUCCESS. {len(positions)} open position(s):")
    print(positions)
except Exception as e:
    print(f"FAILED: {e}")

print()
print("=" * 60)
print("TEST 3: Get open orders")
print("=" * 60)
try:
    orders = api.get_open_orders()
    print(f"SUCCESS. {len(orders)} open order(s):")
    print(orders)
except Exception as e:
    print(f"FAILED: {e}")

print()
print("=" * 60)
print("TEST 4a: Set leverage with marginMode=CROSS (diagnostic)")
print("=" * 60)
try:
    lev_resp = api.set_leverage_and_margin_mode("ETHUSDT", 5, "CROSS")
    print("SUCCESS. Response:")
    print(lev_resp)
except Exception as e:
    print(f"FAILED: {e}")

print()
print("=" * 60)
print("TEST 4b: Set leverage with marginMode=ISOLATED (diagnostic)")
print("=" * 60)
try:
    lev_resp = api.set_leverage_and_margin_mode("ETHUSDT", 5, "ISOLATED")
    print("SUCCESS. Response:")
    print(lev_resp)
except Exception as e:
    print(f"FAILED: {e}")

print()
print("=" * 60)
print("TEST 4: Place+cancel a tiny NEAR-MARKET test order (skips leverage-setting this run)")
print("(price is set 5% below market -- close enough to be accepted, far")
print(" enough that it won't fill in the couple of seconds this test runs)")
print("=" * 60)
try:
    current_price = candles.get_last_price("ETHUSDT")
    print(f"Current ETHUSDT price: {current_price}")
    test_price = round(current_price * 0.95, 2)
    print(f"Placing tiny test LIMIT BUY: 0.1 ETH @ {test_price} (far below market)...")
    resp = api.place_entry_order("ETHUSDT", "BUY", "LIMIT", 0.1, price=test_price)
    print("Placed. Response:")
    print(resp)

    client_order_id = resp.get("clientOrderId")
    if client_order_id:
        print(f"Cancelling order {client_order_id}...")
        cancel_resp = api.delete_order(client_order_id)
        print("Cancelled. Response:")
        print(cancel_resp)
    else:
        print("WARNING: no clientOrderId in response -- check the exchange "
              "app/site manually to make sure nothing is left resting!")
except Exception as e:
    print(f"FAILED: {e}")

print()
print("=" * 60)
print("All tests complete. Review each section above for FAILED lines.")
print("If TEST 4 placed an order but errored before cancelling, check")
print("your open orders on the exchange directly.")
print("=" * 60)
