"""Late-landing swap catcher (audit 2026-10-14).

A send reported as failed/uncertain can still land after the check ends, and
nothing logged it. Once per check,
before deposit detection, read the wallet's newest signatures; any successful
wallet-signed swap whose signature is not in trades.jsonl is appended as a
kind=late_fill row (from on-chain amounts) and reported with LATE_FILL so the
owner hears about it. Book coins and the bot's JitoSOL/Greedy Goats count.
Plain transfers (one side only) are left to deposit detection. Read-only RPC.
"""
import json, os
from datetime import datetime
from zoneinfo import ZoneInfo
from degen_home import TZ_NAME as _TZ_NAME

import degen_home as _DH

PT = ZoneInfo(_TZ_NAME)
STATE = os.path.join(_DH.HOME_STR, "late-fills-state.json")
TRADES = os.path.join(_DH.HOME_STR, "trades.jsonl")
MINTS = {
    "So11111111111111111111111111111111111111112": "SOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij": "cbBTC",
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs": "WETH",
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn": "JitoSOL",
    "GGiveYrqhfJc5pgBnjuY3NP5cW3ni1nFHeJXYZGFappF": "GreedyGoats",
}
SOL_FEE_MAX = 0.003  # SOL change at or below this (after fee/rent are taken out) is tip dust, not a swap leg


def _logged_sigs(path=TRADES):
    out = set()
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                for k in ("tx", "signature"):
                    if r.get(k):
                        out.add(r[k])
                for s in r.get("signatures") or []:
                    out.add(s)
    except FileNotFoundError:
        pass
    return out


WSOL = "So11111111111111111111111111111111111111112"


def token_rent_delta(meta, pre, post):
    """SOL moved into (+) or out of (-) the wallet's token accounts as rent in one tx.

    Lamport change of each wallet-owned token account, minus the wrapped-SOL amount change for
    wSOL accounts (that part is SOL being swapped, not rent). Temporary accounts opened and
    closed in the same tx net to zero.
    """
    pb, qb = meta.get("preBalances") or [], meta.get("postBalances") or []
    out = 0.0
    for idx in set(pre) | set(post):
        if idx >= len(pb) or idx >= len(qb):
            continue
        lam = (qb[idx] - pb[idx]) / 1e9
        b = post.get(idx) or pre.get(idx)
        if b.get("mint") == WSOL:
            a1 = float(((post.get(idx) or {}).get("uiTokenAmount") or {}).get("uiAmountString") or 0)
            a0 = float(((pre.get(idx) or {}).get("uiTokenAmount") or {}).get("uiAmountString") or 0)
            lam -= (a1 - a0)
        out += lam
    return out


def deltas(tx, wallet):
    """Wallet coin changes in one parsed transaction: {sym: delta}, signer flag."""
    meta = tx.get("meta") or {}
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
    ch = {}
    if wallet in keys:
        i = keys.index(wallet)
        ch["SOL"] = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9
    pre = {b["accountIndex"]: b for b in meta.get("preTokenBalances") or [] if b.get("owner") == wallet}
    post = {b["accountIndex"]: b for b in meta.get("postTokenBalances") or [] if b.get("owner") == wallet}
    if "SOL" in ch:
        # rent1014: record the swap amount only, like normal buys. Add back the network fee the wallet
        # paid and the rent moved into (or back out of) the wallet's own token accounts.
        if keys and keys[0] == wallet:
            ch["SOL"] += float(meta.get("fee") or 0) / 1e9
        ch["SOL"] += token_rent_delta(meta, pre, post)
    for idx in set(pre) | set(post):
        b = post.get(idx) or pre.get(idx)
        sym = MINTS.get(b["mint"])
        if not sym:
            continue
        a1 = float(((post.get(idx) or {}).get("uiTokenAmount") or {}).get("uiAmountString") or 0)
        a0 = float(((pre.get(idx) or {}).get("uiTokenAmount") or {}).get("uiAmountString") or 0)
        if a1 != a0:
            # wrapped SOL folds into SOL
            ch[sym] = ch.get(sym, 0.0) + (a1 - a0)
    return ch, bool(keys) and keys[0] == wallet


def swap_row(sig, block_time, ch):
    """trades.jsonl row for a wallet swap, or None when it is not a swap."""
    legs = {k: v for k, v in ch.items() if abs(v) > 0 and not (k == "SOL" and abs(v) <= SOL_FEE_MAX)}
    downs = {k: -v for k, v in legs.items() if v < 0}
    ups = {k: v for k, v in legs.items() if v > 0}
    if len(downs) != 1 or len(ups) != 1:
        return None
    (sold, sa), = downs.items()
    (bought, ba), = ups.items()
    when = datetime.fromtimestamp(block_time, PT).replace(microsecond=0).isoformat() if block_time else None
    return {
        "time_pt": when, "side": f"{sold}\u2192{bought}", "sold_asset": sold, "sold_amount": sa,
        "bought_asset": bought, "bought_amount": ba, "rate": (sa / ba) if ba else None,
        "rate_unit": f"{sold}/{bought}", "tx": sig, "tx_url": f"https://solscan.io/tx/{sig}",
        "total_usd_after": None,
        "notes": "LATE_FILL: wallet swap that landed on-chain but was never logged (send reported failed/uncertain); "
                 "amounts from on-chain balance changes; not counted as an edge fill",
        "kind": "late_fill", "savings": bought in ("JitoSOL", "GreedyGoats"), "counts_as_edge": False,
        "backfilled": True,
    }


def scan(wallet, rpc, limit=15, state_path=STATE, trades_path=TRADES, append=True):
    """Return the late-fill rows found this call (and append them unless append=False).

    rpc(method, params) -> JSON-RPC body dict. First run seeds the seen set
    from the newest signatures and backfills nothing.
    """
    try:
        st = json.load(open(state_path, encoding="utf-8"))
    except Exception:
        st = {}
    seen = set(st.get("seen") or [])
    body = rpc("getSignaturesForAddress", [wallet, {"limit": int(limit)}]) or {}
    sigs = body.get("result") or []
    if not st:
        st = {"seen": [s["signature"] for s in sigs], "seeded_pt": datetime.now(PT).isoformat()}
        json.dump(st, open(state_path, "w", encoding="utf-8"))
        return []
    logged = _logged_sigs(trades_path)
    found = []
    for s in reversed(sigs):
        sig = s.get("signature")
        if not sig or sig in seen:
            continue
        if s.get("err") is None and sig not in logged:
            tx = (rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]) or {}).get("result")
            if not tx:
                continue  # not readable yet: try again next check (not marked seen)
            ch, signer = deltas(tx, wallet)
            row = swap_row(sig, s.get("blockTime") or tx.get("blockTime"), ch) if signer else None
            if row:
                found.append(row)
        seen.add(sig)
    if found and append:
        with open(trades_path, "a", encoding="utf-8", newline="") as f:
            for r in found:
                f.write(json.dumps(r) + "\n")
    st["seen"] = list(seen)[-500:] if len(seen) > 500 else list(seen)
    tmp = state_path + ".tmp"
    json.dump(st, open(tmp, "w", encoding="utf-8"))
    os.replace(tmp, state_path)
    return found
