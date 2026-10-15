"""Per-pair realized P&L (locked 2026-10-05, owner). Pure helpers, no network.

Feeds the per-pair size tilt (cost-basis.json pair_pnl_size_tilt). State file:
pair-pnl-state.json. Tests: test_edge_policies.py (PairPnlTests).

METHOD: FIFO round trips in the pair's own rate, valued in USD at fill time.
  * Each pair has a base and a quote (cost-basis.json pairs.*.base/quote) and a
    rate = quote per base taken from the fill's executed amounts
    (quote_amount / base_amount). Executed amounts are net of venue swap fees
    (LP/AMM fee is inside out_amount; Jupiter platformFeeBps=0), so swap fees
    are already in the rate.
  * A fill is "sell_base" (sold the base) or "buy_base" (bought the base).
    It first closes, FIFO, any open lots of the OPPOSITE direction on that pair:
      open sell_base @ r_open, closed by buy_base @ r_close:
          gain_quote = matched_base * (r_open - r_close)
      open buy_base  @ r_open, closed by sell_base @ r_close:
          gain_quote = matched_base * (r_close - r_open)
    gain_usd = gain_quote * USD(quote) at the CLOSING fill's time.
    Any unmatched remainder opens a new lot (same-direction fills queue up).
  * Fees: every fill's network fee (gas + priority + any Jito tip, the
    "gas X SOL" in trades.jsonl notes / gas_sol at fill) x USD(SOL) at that fill
    is a realized cost when paid, on that pair. A fill with no recorded gas uses
    DEFAULT_GAS_SOL (5000 lamports, one base signature fee).
  * realized_net_usd = sum(gain_usd) - sum(fee_usd).
  * traded_usd = sum over the pair's fills of the fill's quote leg x USD(quote)
    at fill time (total USD notional traded on the pair).
  * pnl_pct = realized_net_usd / traded_usd * 100. No fills -> None (tilt x1.0).
  Open (unmatched) lots are inventory, not realized; they do not move pnl_pct
  except for the fee already paid to open them.

USD at fill time: live check prices for a fill made by this process; for
reconstruction, USDC = 1, else the nearest-in-time sample from USDC-legged
fills in trades.jsonl and the per-check "- balances:" lines of sol-usdc-log.md
($ value / qty), else the current check prices.
"""
from __future__ import annotations

import bisect
import json
import os
import re
import tempfile
from datetime import datetime

STATE_FILE = "pair-pnl-state.json"
DEFAULT_GAS_SOL = 5e-06
EPS = 1e-15
METHOD = "fifo_round_trip_usd_at_fill_minus_network_fees"

_GAS_RE = re.compile(r"\bgas\s+~?([0-9.]+(?:e-?[0-9]+)?)\s*SOL", re.I)
_HDR_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?[+-]\d{2}:\d{2})")
_BAL_RE = re.compile(r"(SOL|USDC|cbBTC|WETH)=([0-9.]+(?:e-?[0-9]+)?)\s*\(\$([0-9.,]+)\)")
_BAL2_RE = re.compile(r"(SOL|USDC|cbBTC|WETH) ([0-9.]+(?:e-?[0-9]+)?) \$([0-9.,]+)")  # logdiet '- bal:' lines


def _ts(s):
    try:
        return datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


def pair_for(sold, bought, pairs_cfg):
    """Pair key whose base/quote are exactly {sold, bought}; None if not a book pair."""
    for key, p in (pairs_cfg or {}).items():
        if not isinstance(p, dict):
            continue
        if {p.get("base"), p.get("quote")} == {sold, bought} and sold != bought:
            return key
    return None


def parse_gas_sol(notes):
    m = _GAS_RE.search(str(notes or ""))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


class PriceBook:
    """USD samples per symbol over time; nearest-in-time lookup. USDC is 1."""

    def __init__(self, fallback=None):
        self.samples = {}
        self.fallback = dict(fallback or {})

    def add(self, ts, sym, usd):
        t = ts if isinstance(ts, datetime) else _ts(ts)
        try:
            v = float(usd)
        except (TypeError, ValueError):
            return
        if t is None or v <= 0:
            return
        arr = self.samples.setdefault(sym, [])
        bisect.insort(arr, (t.timestamp(), v))

    def usd(self, sym, ts=None):
        if sym == "USDC":
            return 1.0
        arr = self.samples.get(sym) or []
        t = ts if isinstance(ts, datetime) else _ts(ts)
        if arr and t is not None:
            x = t.timestamp()
            i = bisect.bisect_left(arr, (x, -1.0))
            best = None
            for j in (i - 1, i):
                if 0 <= j < len(arr):
                    if best is None or abs(arr[j][0] - x) < abs(best[0] - x):
                        best = arr[j]
            if best:
                return best[1]
        try:
            v = float(self.fallback.get(sym) or 0)
        except (TypeError, ValueError):
            v = 0.0
        return v if v > 0 else None


def price_book_from(trades, log_path=None, fallback=None):
    pb = PriceBook(fallback)
    for t in trades or []:
        sold, bought = t.get("sold_asset"), t.get("bought_asset")
        try:
            sa, ba = float(t.get("sold_amount") or 0), float(t.get("bought_amount") or 0)
        except (TypeError, ValueError):
            continue
        if sa <= 0 or ba <= 0:
            continue
        if sold == "USDC" and bought != "USDC":
            pb.add(t.get("time_pt"), bought, sa / ba)
        elif bought == "USDC" and sold != "USDC":
            pb.add(t.get("time_pt"), sold, ba / sa)
    if log_path and os.path.exists(log_path):
        cur = None
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _HDR_RE.match(line)
                if m:
                    cur = m.group(1)
                    continue
                if cur and (line.startswith("- balances:") or line.startswith("- bal:")):
                    for sym, qty, usd in (_BAL_RE if line.startswith("- balances:") else _BAL2_RE).findall(line):
                        try:
                            q, u = float(qty), float(usd.replace(",", ""))
                        except ValueError:
                            continue
                        # Skip tiny $ values: 2-decimal rounding makes the price noisy.
                        if q > 0 and u >= 1.0 and sym != "USDC":
                            pb.add(cur, sym, u / q)
    return pb


def new_entry(pair, base, quote):
    return {
        "pair": pair, "base": base, "quote": quote,
        "fills": 0, "traded_usd": 0.0, "realized_gross_usd": 0.0,
        "fees_usd": 0.0, "realized_net_usd": 0.0, "pnl_pct": None,
        "matched_base_qty": 0.0, "open_lots": [],
        "last_fill_tx": None, "last_fill_time_pt": None,
    }


def new_state():
    return {
        "method": METHOD,
        "doc": "See pair_pnl.py docstring and README-cost-basis.md (pair_pnl_size_tilt).",
        "pairs": {}, "processed_txs": [], "updated_pt": None,
    }


def pnl_pct(entry):
    try:
        traded = float(entry.get("traded_usd") or 0)
    except (AttributeError, TypeError, ValueError):
        return None
    if int(entry.get("fills") or 0) <= 0 or traded <= 0:
        return None
    return float(entry.get("realized_net_usd") or 0) / traded * 100.0


def apply_fill(state, row, pairs_cfg, px, gas_sol=None):
    """Fold one fill (trades.jsonl-shaped row) into state. Idempotent by tx.

    px(sym, time_pt) -> USD at fill time. Returns the pair key, or None when the
    fill is not on a book pair / already applied / malformed.
    """
    tx = row.get("tx")
    done = state.setdefault("processed_txs", [])
    if row.get("savings") or row.get("kind") == "savings_buy":
        # Held savings buys (JitoSOL, saved cbBTC) are not pair trades.
        if tx and tx not in done:
            done.append(tx)
        return None
    sold, bought = row.get("sold_asset"), row.get("bought_asset")
    pair = pair_for(sold, bought, pairs_cfg)
    if pair is None:
        return None
    if tx and tx in done:
        return None
    try:
        sa, ba = float(row.get("sold_amount") or 0), float(row.get("bought_amount") or 0)
    except (TypeError, ValueError):
        return None
    if sa <= 0 or ba <= 0:
        return None
    base, quote = pairs_cfg[pair]["base"], pairs_cfg[pair]["quote"]
    e = state.setdefault("pairs", {}).setdefault(pair, new_entry(pair, base, quote))
    when = row.get("time_pt")
    if sold == base:
        direction, qb, qq = "sell_base", sa, ba
    else:
        direction, qb, qq = "buy_base", ba, sa
    rate = qq / qb
    q_usd = px(quote, when)
    sol_usd = px("SOL", when)
    if gas_sol is None:
        gas_sol = parse_gas_sol(row.get("notes"))
    if gas_sol is None:
        gas_sol = DEFAULT_GAS_SOL
    fee_usd = float(gas_sol) * float(sol_usd or 0)
    gain_usd = 0.0
    remaining = qb
    lots = e.setdefault("open_lots", [])
    while remaining > EPS and lots and lots[0]["dir"] != direction:
        lot = lots[0]
        m = min(remaining, float(lot["qty_base"]))
        if lot["dir"] == "sell_base":
            gq = m * (float(lot["rate"]) - rate)
        else:
            gq = m * (rate - float(lot["rate"]))
        gain_usd += gq * float(q_usd or 0)
        e["matched_base_qty"] = float(e.get("matched_base_qty") or 0) + m
        lot["qty_base"] = float(lot["qty_base"]) - m
        remaining -= m
        if lot["qty_base"] <= EPS * max(1.0, qb):
            lots.pop(0)
    if remaining > EPS * max(1.0, qb):
        lots.append({"dir": direction, "qty_base": remaining, "rate": rate, "tx": tx, "time_pt": when})
    e["fills"] = int(e.get("fills") or 0) + 1
    e["traded_usd"] = float(e.get("traded_usd") or 0) + qq * float(q_usd or 0)
    e["realized_gross_usd"] = float(e.get("realized_gross_usd") or 0) + gain_usd
    e["fees_usd"] = float(e.get("fees_usd") or 0) + fee_usd
    e["realized_net_usd"] = e["realized_gross_usd"] - e["fees_usd"]
    e["pnl_pct"] = pnl_pct(e)
    e["last_fill_tx"] = tx
    e["last_fill_time_pt"] = when
    if tx:
        done.append(tx)
    return pair


def sync(state, trades, pairs_cfg, px):
    """Apply every trades.jsonl row not yet in state (file order). Returns changed pairs."""
    changed = []
    for t in trades or []:
        p = apply_fill(state, t, pairs_cfg, px)
        if p:
            changed.append(p)
    return changed


def rebuild(trades, pairs_cfg, px):
    st = new_state()
    sync(st, trades, pairs_cfg, px)
    return st


def ensure_pairs(state, pairs_cfg):
    for key, p in (pairs_cfg or {}).items():
        if isinstance(p, dict) and p.get("base") and p.get("quote"):
            state.setdefault("pairs", {}).setdefault(key, new_entry(key, p["base"], p["quote"]))
    return state


def load(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else new_state()
    except (OSError, ValueError):
        return None


def save(path, state):
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".atomic-", suffix=".json", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def summary(state, tilt_fn=None):
    out = {}
    for k, e in sorted((state.get("pairs") or {}).items()):
        pct = e.get("pnl_pct")
        row = {
            "fills": e.get("fills"), "pnl_pct": None if pct is None else round(pct, 4),
            "realized_net_usd": round(float(e.get("realized_net_usd") or 0), 6),
            "traded_usd": round(float(e.get("traded_usd") or 0), 4),
        }
        if tilt_fn:
            row["mult"], row["label"] = tilt_fn(pct, int(e.get("fills") or 0))
        out[k] = row
    return out


if __name__ == "__main__":  # rebuild from trades.jsonl, print, write state
    import sys
    root = os.path.dirname(os.path.abspath(__file__))
    cb = json.load(open(os.path.join(root, "cost-basis.json")))
    _tp = os.path.join(root, "trades.jsonl")
    trades = [json.loads(l) for l in open(_tp, encoding="utf-8") if l.strip()] if os.path.exists(_tp) else []
    try:
        fb = json.load(open(os.path.join(root, "usd-price-snapshot.json"))).get("prices_usd")
    except Exception:
        fb = {}
    pb = price_book_from(trades, os.path.join(root, "sol-usdc-log.md"), fb)
    st = ensure_pairs(rebuild(trades, cb["pairs"], pb.usd), cb["pairs"])
    st["updated_pt"] = datetime.now().astimezone().isoformat(timespec="seconds")
    st["source"] = "rebuild trades.jsonl + sol-usdc-log.md price samples"
    import edge_policies as EP
    cfg = EP.pair_tilt_cfg(cb)
    print(json.dumps(summary(st, lambda p, n: EP.pair_tilt_multiplier(p, n, cfg)), indent=1))
    if "--write" in sys.argv:
        save(os.path.join(root, STATE_FILE), st)
        print("wrote", STATE_FILE)
