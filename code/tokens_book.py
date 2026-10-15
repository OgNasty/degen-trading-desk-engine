"""tokens_usd book: per-coin qty and USD avg cost (proposed 2026-10-12).

Before this module nothing wrote tokens_usd after the one-time seed, so loss tilt
and the profit-sale cut used a frozen SOL 0.717 @ $121.03.
- record_fill: every swap between book coins (edge, reverse, any-profit, daily_v2,
  RSI, band, trim, underweight). Sold coin: qty down at avg cost. Bought coin: qty up,
  cost += USD value given up (sold qty x sold-coin USD price).
- reconcile: once per check vs live trading balances (saved cbBTC/JitoSOL/memecoins
  excluded). Extra coins (deposits) are added at market price; missing coins
  (savings/memecoin buys, gas, withdrawals) are removed at avg cost. Avg cost of
  a removal never changes. Drift is logged in tokens_usd.drift_log.
"""
BOOK = ("SOL", "USDC", "cbBTC", "WETH")
TOL_QTY_PCT = 0.5     # drift tolerance: max(0.5% of qty, $0.25)
TOL_USD = 0.25


def _row(cb, sym):
    t = cb.setdefault("tokens_usd", {})
    r = t.get(sym)
    if not isinstance(r, dict):
        r = t[sym] = {"qty": 0.0, "avg_cost_usd": 1.0 if sym == "USDC" else None}
    return r


def _remove(r, qty):
    r["qty"] = max(0.0, float(r.get("qty") or 0) - float(qty))


def _add(r, sym, qty, usd_cost):
    q0 = float(r.get("qty") or 0)
    c0 = q0 * float(r.get("avg_cost_usd") or 0)
    q1 = q0 + float(qty)
    r["qty"] = q1
    if sym == "USDC":
        r["avg_cost_usd"] = 1.0
    elif q1 > 0:
        r["avg_cost_usd"] = (c0 + float(usd_cost)) / q1


def record_fill(cb, sold, bought, sold_amt, bought_amt, prices, when=None):
    if sold not in BOOK or bought not in BOOK:
        return False
    px = 1.0 if sold == "USDC" else float(prices.get(sold) or 0)
    if px <= 0:
        return False
    _remove(_row(cb, sold), sold_amt)
    _add(_row(cb, bought), bought, bought_amt, float(sold_amt) * px)
    if when:
        cb["tokens_usd"]["updated_pt"] = when
    return True


def reconcile(cb, live, prices, when=None, heal=True, confirm=2):
    """live: trading balances {sym: qty}. Returns list of drift dicts.

    bugfix1012: a drift must be seen on `confirm` consecutive checks (same wallet qty
    within tolerance) before it is healed; a one-off bad read never changes the book.
    """
    out = []
    pend = cb.setdefault("tokens_usd", {}).setdefault("drift_pending", {})
    for sym in BOOK:
        if sym not in live:
            continue
        r = _row(cb, sym)
        have = float(r.get("qty") or 0)
        want = float(live[sym] or 0)
        px = 1.0 if sym == "USDC" else float(prices.get(sym) or 0)
        d = want - have
        tol = max(abs(have) * TOL_QTY_PCT / 100, (TOL_USD / px) if px > 0 else 0)
        if abs(d) <= tol:
            pend.pop(sym, None)
            continue
        if confirm > 1:
            pv = pend.get(sym)
            if not pv or abs(float(pv.get("wallet_qty", -1)) - want) > tol:
                pend[sym] = {"wallet_qty": want, "seen": 1, "pt": when}
                continue
            pv["seen"] = int(pv.get("seen", 1)) + 1
            if pv["seen"] < confirm:
                continue
            pend.pop(sym, None)
        out.append({"sym": sym, "book_qty": have, "wallet_qty": want, "drift_qty": d,
                    "drift_usd": round(d * px, 4), "healed": bool(heal), "pt": when})
        if heal:
            if d > 0:
                _add(r, sym, d, d * px)
            else:
                _remove(r, -d)
    if out:
        log = cb["tokens_usd"].setdefault("drift_log", [])
        log.extend(out)
        del log[:-50]
        if when:
            cb["tokens_usd"]["updated_pt"] = when
    return out


def drift_lines(drift):
    """Log lines for corrections (only called when a correction happened)."""
    return [f"TOKENS_USD_DRIFT {d['pt']} {d['sym']} book={d['book_qty']:.8f} wallet={d['wallet_qty']:.8f} "
            f"usd={d['drift_usd']} healed={d['healed']}" for d in drift]


def set_qty_from_wallet(cb, live):
    """Seed helper: set book qty to live trading balances without touching avg cost."""
    for sym in BOOK:
        if sym in live:
            _row(cb, sym)["qty"] = float(live[sym] or 0)
