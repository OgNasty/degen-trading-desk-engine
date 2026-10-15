#!/usr/bin/env python3
"""Seed starting prices (each pair's blank basis rate) for a new Degen wallet. Template 2026-10-13.

Three choices, picked by the owner at setup:
  --mode deposit     Wait for the first deposit to land in WALLET_ADDRESS, then seed every pair from
                     live Jupiter quotes at that moment (also sets token qty/avg cost and funded_usd).
  --mode test-trade  Make ONE small real swap (default ~$1 of SOL -> USDC) to prove the setup is live,
                     then seed from live quotes. REAL TRADE: requires --approve-live-test-trade, which the
                     agent may pass only after the owner explicitly says yes. Works even in paper mode
                     (only this one swap is allowed through).
  --mode manual      Use the owner's own USD prices: --price SOL=150 --price cbBTC=60000 --price WETH=2500
                     (USDC=1 assumed), or prompts for any missing ones.
Common: --force (overwrite rates that are already set), --dry-run (print, write nothing).
Writes cost-basis.json in DEGEN_HOME (backup cost-basis.json.bak-seed-<time> first). Never prints secrets.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import degen_home as _DH

HOME = _DH.HOME
CB = HOME / "cost-basis.json"
AP = HOME / "allowed-pairs.json"
WSOL = "So11111111111111111111111111111111111111112"
JUP_KEYED = "https://api.jup.ag/swap/v1"


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def assets() -> dict:
    a = json.loads(AP.read_text(encoding="utf-8"))["assets"]
    return {x["symbol"]: {"mint": WSOL if x["mint"] == "native" else x["mint"], "dec": int(x["decimals"]), "native": x["mint"] == "native"} for x in a}


def _jup(path: str, params=None, body=None):
    key = (_DH.get_secret("JUPITER_API_KEY") or "").strip()
    for use_key in ([True] if key else []) + [False]:
        url = JUP_KEYED + path + ("?" + urllib.parse.urlencode(params) if params else "")
        hdr = {"User-Agent": "degen-seed/1", "Accept": "application/json"}
        if use_key:
            hdr["x-api-key"] = key
        data = None
        if body is not None:
            data = json.dumps(body).encode(); hdr["Content-Type"] = "application/json"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=hdr), timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if use_key and e.code in (401, 403):
                continue
            raise RuntimeError(f"jupiter http {e.code} {path}")
    raise RuntimeError("jupiter unavailable")


def quote(sell: str, buy: str, ui_amount: float, A: dict, slip_bps=50) -> dict:
    amt = int(ui_amount * 10 ** A[sell]["dec"])
    return _jup("/quote", {"inputMint": A[sell]["mint"], "outputMint": A[buy]["mint"], "amount": str(amt), "slippageBps": str(slip_bps)})


def live_usd_prices(A: dict) -> dict:
    """USD price per token from a ~$20 USDC -> token quote (USDC = 1)."""
    px = {"USDC": 1.0}
    for s in A:
        if s == "USDC":
            continue
        q = quote("USDC", s, 20.0, A)
        out = int(q["outAmount"]) / 10 ** A[s]["dec"]
        px[s] = 20.0 / out
    return px


def balances(A: dict) -> dict:
    import solana_rpc
    addr = _DH.wallet_address()
    if not addr:
        raise SystemExit("WALLET_ADDRESS is not set (.env or config.json wallet.address)")
    try:
        import base58
        ok = len(base58.b58decode(addr)) == 32
    except Exception:
        ok = False
    if not ok:
        raise SystemExit("WALLET_ADDRESS is not a valid Solana address yet (still the placeholder?); set it, then run again")
    out = {}
    for s, a in A.items():
        if a["native"]:
            r = solana_rpc.rpc("getBalance", [addr, {"commitment": "confirmed"}])
            out[s] = int(r["result"]["value"]) / 1e9
        else:
            r = solana_rpc.rpc("getTokenAccountsByOwner", [addr, {"mint": a["mint"]}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
            out[s] = sum(float(v["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0) for v in r["result"]["value"])
    return out


def seed(px: dict, bals: dict | None, source: str, force: bool, dry: bool) -> dict:
    cb = json.loads(CB.read_text(encoding="utf-8"))
    changed = {}
    for pair, p in cb.get("pairs", {}).items():
        if p.get("rate") not in (None, 0) and not force:
            continue
        b, q = p["base"], p["quote"]
        if b in px and q in px:
            p["rate"] = px[b] / px[q]
            p["source"] = source
            p["seeded_pt"] = now()
            changed[pair] = round(p["rate"], 10)
    if bals is not None:
        tu = cb.setdefault("tokens_usd", {})
        total = 0.0
        for s, qty in bals.items():
            row = tu.setdefault(s, {})
            if force or not row.get("avg_cost_usd"):
                row.update(qty=qty, avg_cost_usd=px.get(s), cost_source=source)
            total += qty * px.get(s, 0)
        f = cb.setdefault("funding", {})
        if force or not f.get("funded_usd"):
            f["funded_usd"] = round(total, 2)
            f.setdefault("history", []).append({"at_pt": now(), "usd": round(total, 2), "source": source})
    print(json.dumps({"seeded_pairs": changed, "usd_prices": {k: round(v, 6) for k, v in px.items()},
                      "funded_usd": (cb.get("funding") or {}).get("funded_usd")}, indent=2))
    if not dry and (changed or bals is not None):
        shutil.copy2(CB, CB.with_name(f"cost-basis.json.bak-seed-{datetime.now():%Y%m%d-%H%M%S}"))
        tmp = CB.with_suffix(".tmp"); tmp.write_text(json.dumps(cb, indent=2), encoding="utf-8"); tmp.replace(CB)
        print("cost-basis.json updated")
    return changed


def mode_deposit(A, args):
    deadline = time.time() + args.wait_min * 60
    while True:
        b = balances(A)
        px = live_usd_prices(A)
        usd = sum(b[s] * px[s] for s in b)
        if usd >= args.min_usd:
            print(f"deposit detected: ~${usd:.2f} in wallet")
            return seed(px, b, "seed_first_deposit_live_quotes", args.force, args.dry_run)
        if time.time() > deadline:
            print(f"no deposit >= ${args.min_usd} yet (wallet ~${usd:.2f}); run again later")
            return 2
        time.sleep(args.poll_sec)


def mode_test_trade(A, args):
    if not args.approve_live_test_trade:
        raise SystemExit("test-trade is a REAL swap. Re-run with --approve-live-test-trade only after the owner explicitly approves.")
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction
    import solana_rpc
    px = live_usd_prices(A)
    b = balances(A)
    sol_amt = round(args.usd / px["SOL"], 6)
    if b.get("SOL", 0) < sol_amt + 0.02:
        raise SystemExit(f"need at least {sol_amt + 0.02:.4f} SOL (test amount + 0.02 SOL fee buffer)")
    q = quote("SOL", "USDC", sol_amt, A)
    if args.dry_run:
        print(f"dry-run: would swap {sol_amt} SOL -> ~{int(q['outAmount'])/1e6:.4f} USDC"); return seed(px, b, "seed_test_trade_dry", args.force, True)
    sw = _jup("/swap", body={"quoteResponse": q, "userPublicKey": _DH.wallet_address(), "wrapAndUnwrapSol": True,
                             "dynamicComputeUnitLimit": True, "prioritizationFeeLamports": "auto"})
    kp = Keypair.from_base58_string((_DH.get_secret("SOLANA_TRADING_PRIVATE_KEY") or "").strip())
    if str(kp.pubkey()) != _DH.wallet_address():
        raise SystemExit("private key does not match WALLET_ADDRESS")
    tx = VersionedTransaction.from_bytes(base64.b64decode(sw["swapTransaction"]))
    signed = base64.b64encode(bytes(VersionedTransaction(tx.message, [kp]))).decode()
    os.environ["DEGEN_SEED_TEST_TRADE"] = "approved"  # lets exactly this send past paper mode
    try:
        resp = solana_rpc.rpc_send("sendTransaction", [signed, solana_rpc.send_config()], "helius")
    finally:
        os.environ.pop("DEGEN_SEED_TEST_TRADE", None)
    sig = (resp or {}).get("result")
    if not sig:
        raise SystemExit("test swap was not accepted: " + solana_rpc.redact(json.dumps((resp or {}).get("error")))[:300])
    print("test swap sent:", sig)
    print("confirm:", solana_rpc.confirm_signature(sig))
    time.sleep(3)
    px = live_usd_prices(A)
    return seed(px, balances(A), "seed_test_trade_live_quotes", args.force, args.dry_run)


def mode_manual(A, args):
    px = {"USDC": 1.0}
    for kv in args.price or []:
        k, v = kv.split("=", 1); px[k.strip()] = float(v)
    for s in A:
        if s not in px:
            px[s] = float(input(f"Starting USD price for {s}: ").strip().replace("$", "").replace(",", ""))
    try:
        b = balances(A)
    except (Exception, SystemExit):
        b = None  # no RPC yet: rates only
    return seed(px, b, "seed_owner_prices", args.force, args.dry_run)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", required=True, choices=["deposit", "test-trade", "manual"])
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--wait-min", type=float, default=0, help="deposit: keep watching this many minutes (0 = check once)")
    ap.add_argument("--poll-sec", type=float, default=30)
    ap.add_argument("--min-usd", type=float, default=5.0, help="deposit: smallest wallet value that counts as the first deposit")
    ap.add_argument("--usd", type=float, default=1.0, help="test-trade: size in USD")
    ap.add_argument("--approve-live-test-trade", action="store_true")
    ap.add_argument("--price", action="append", help="manual: SYMBOL=USD (repeatable)")
    a = ap.parse_args(argv)
    A = assets()
    r = {"deposit": mode_deposit, "test-trade": mode_test_trade, "manual": mode_manual}[a.mode](A, a)
    return r if isinstance(r, int) else 0


if __name__ == "__main__":
    sys.exit(main())
