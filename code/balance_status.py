"""status1012: wallet balance status for the dashboard (desk.json `c` b/T/u/f + bt), independent of log_diet.

Written by the minute check from values it already has (no extra RPC/price calls), at most once per
reporting.json balance_status_every_sec (default 90; 0 = off). Checks run every 60 s, so with 90 s the status
lands on every other check (~120 s apart). Never writes sol-usdc-log.md or the trades files.
"""
import json, os
from datetime import datetime

import degen_home as _DH

PATH = os.path.join(_DH.HOME_STR, "live-balance.json")
REPORTING = os.path.join(_DH.HOME_STR, "reporting.json")
SYMS = ("SOL", "USDC", "cbBTC", "WETH")
JITTER_S = 2.0


def every_sec(path=REPORTING):
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return 90.0
    v = d.get("balance_status_every_sec")
    if v is None and d.get("balance_status_every_min") is not None:
        v = float(d["balance_status_every_min"]) * 60
    return float(90 if v is None else v)


def snapshot(result, now_iso):
    fills = result.get("fills") or []
    prices = result.get("prices_usd") or {}
    if fills and isinstance(fills[-1], dict) and fills[-1].get("balances_after"):
        bals = fills[-1]["balances_after"]
        total = fills[-1].get("total_usd_after")
        usd = {s: float(bals.get(s) or 0) * float(prices.get(s) or 0) for s in SYMS}
    else:
        bals = result.get("balances") or {}
        usd = result.get("balances_usd") or {}
        total = result.get("total_usd")
    if not bals or total is None:
        return None
    return {"t": now_iso, "b": {s: [bals.get(s), round(float(usd.get(s) or 0), 2)] for s in SYMS},
            "T": round(float(total), 2), "u": result.get("unrealized_pnl_pct"), "f": result.get("funded_usd")}


def maybe_write(result, now_iso, path=PATH, reporting=REPORTING):
    """Returns True when written."""
    ev = every_sec(reporting)
    if ev <= 0:
        return False
    try:
        prev = json.load(open(path, encoding="utf-8"))
        age = (datetime.fromisoformat(now_iso) - datetime.fromisoformat(prev["t"])).total_seconds()
    except Exception:
        age = None
    if age is not None and 0 <= age < ev - JITTER_S and not (result.get("fills") if isinstance(result, dict) else None):  # a fill always refreshes
        return False
    snap = snapshot(result, now_iso)
    if not snap:
        return False
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, separators=(",", ":"))
    os.replace(tmp, path)
    return True
