"""after1012: wallet balances AFTER a fill, for trade-log rows.

Reads at "confirmed" (finalized reads lag a just-confirmed send and returned the pre-run balances),
retrying until the fill shows. If no fresh read reflects the fill, returns computed values
(pre - sold + bought) so the row is never the pre-trade balance.
"""
import time

import solana_rpc

MINTS = {"USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "cbBTC": "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij",
         "WETH": "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"}
BOOK = ("SOL", "USDC", "cbBTC", "WETH")


def fetch_confirmed(addr):
    sol = solana_rpc.rpc("getBalance", [addr, {"commitment": "confirmed"}], retries=2, timeout=15)["result"]["value"] / 1e9
    tok = solana_rpc.rpc("getTokenAccountsByOwner", [addr, {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                                                     {"encoding": "jsonParsed", "commitment": "confirmed"}], retries=2, timeout=15)
    out = {"SOL": float(sol), "USDC": 0.0, "cbBTC": 0.0, "WETH": 0.0}
    want = {v: k for k, v in MINTS.items()}
    for v in tok["result"]["value"]:
        info = v["account"]["data"]["parsed"]["info"]
        if info["mint"] in want:
            out[want[info["mint"]]] += float(info["tokenAmount"]["uiAmount"] or 0)
    return out


def computed(pre, sold, bought, sell_amt, buy_amt):
    c = {k: float((pre or {}).get(k) or 0) for k in BOOK}
    c[sold] = c.get(sold, 0.0) - float(sell_amt or 0)
    c[bought] = c.get(bought, 0.0) + float(buy_amt or 0)
    return c


def shows_fill(b, pre, sold, bought, sell_amt, buy_amt):
    try:
        dn = float(pre.get(sold) or 0) - float(b.get(sold) or 0)
        up = float(b.get(bought) or 0) - float(pre.get(bought) or 0)
        return dn >= 0.5 * float(sell_amt or 0) or up >= 0.5 * float(buy_amt or 0)
    except Exception:
        return False


def read_after(addr, pre, sold, bought, sell_amt, buy_amt, fetch=None, wait_s=24, step_s=3, sleep=time.sleep):
    """(balances, source) with source 'fresh_confirmed' or 'computed'."""
    fetch = fetch or fetch_confirmed
    t0 = time.monotonic()
    while True:
        try:
            b = fetch(addr)
            if b and pre and shows_fill(b, pre, sold, bought, sell_amt, buy_amt):
                return {k: float(b.get(k) or 0) for k in BOOK}, "fresh_confirmed"
        except Exception:
            pass
        if time.monotonic() - t0 >= wait_s:
            break
        sleep(step_s)
    return computed(pre, sold, bought, sell_amt, buy_amt), "computed"
