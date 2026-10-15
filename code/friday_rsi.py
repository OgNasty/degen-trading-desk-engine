"""Friday check: Wilder RSI(14) only. Old weight bands are retired.

Wallet is <YOUR_WALLET_ADDRESS> via the local signer.
Never Phantom. Never <NONE>.
This module does not print private keys.

Schedule: Friday 9:01 PM in the owner's time zone (shifted by this install's jitter).

RSI trades use USDC as the only quote (SOL-USDC, USDC-cbBTC, USDC-WETH).
Rank SOL, cbBTC, and WETH by daily RSI(14). USDC is cash. JitoSOL and Goats
are ignored. Exactly one case, no fall-through:
1. gap >= 15: sell 75% of the high coin into USDC and buy the low coin with
   those proceeds;
2. gap under 15 AND lowest RSI <= 30: spend 75% of USDC on that coin;
3. highest RSI >= 70 AND nothing <= 30: sell 75% of the high coin into USDC;
4. otherwise do nothing.
Never sell more than 75% of a coin, or spend more than 75% of USDC, on one
check. A 60 reading is not case 3.
Kind rsi_rebalance. Reverse is a flat 5% (not 2x, not leftover on top). Skip
normal edge, size caps, fade, impact, fee-spike; keep the 0.02 SOL buffer.
No streak/multiplier changes.
friday_band is retired (enabled false). bands_if_no_trade is false: case 4
and skipped RSI legs do not run the old band rules.

--dry-run prints the decision and does not sign or send.
"""
# --- auto-switch to the project venv (signing needs solders) ---
import tokens_book
import os as _os, sys as _sys
import degen_home as _DH
if __name__ == "__main__":
    _DH.maybe_reexec_venv()
# --- end auto-switch ---
import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo
from degen_home import TZ_NAME as _TZ_NAME

PT = ZoneInfo(_TZ_NAME)
ADDR = __import__("degen_home").wallet_address()
ADDR_OLD = ""  # template: no legacy wallet
import solana_rpc
CB_PATH = _DH.HOME_STR + "/cost-basis.json"

# Tie order and the only RSI-ranked coins. USDC is cash.
COINS = ("SOL", "cbBTC", "WETH")
BOOK = ("SOL", "USDC", "cbBTC", "WETH")
EXCLUDED = ("JitoSOL", "GreedyGoats", "Greedy Goats")
DEC = {"SOL": 9, "USDC": 6, "cbBTC": 8, "WETH": 8}
MINTS = {
    "SOL": "So11111111111111111111111111111111111111112",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "cbBTC": "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij",
    "WETH": "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
}
# RSI fills are USDC-quoted only. Band fills may use any of the six book pairs.
RSI_PAIRS = {
    ("SOL", "USDC"): "SOL-USDC",
    ("USDC", "SOL"): "SOL-USDC",
    ("cbBTC", "USDC"): "USDC-cbBTC",
    ("USDC", "cbBTC"): "USDC-cbBTC",
    ("WETH", "USDC"): "USDC-WETH",
    ("USDC", "WETH"): "USDC-WETH",
}
PAIR_BASE_QUOTE = {
    "SOL-USDC": ("SOL", "USDC"),
    "USDC-cbBTC": ("cbBTC", "USDC"),
    "USDC-WETH": ("WETH", "USDC"),
    "SOL-cbBTC": ("cbBTC", "SOL"),
    "SOL-WETH": ("WETH", "SOL"),
    "cbBTC-WETH": ("WETH", "cbBTC"),
}
BINANCE_SYMBOL = {"SOL": "SOLUSDT", "cbBTC": "BTCUSDT", "WETH": "ETHUSDT"}
COINBASE_PRODUCT = {"SOL": "SOL-USD", "cbBTC": "BTC-USD", "WETH": "ETH-USD"}

# Same isolation as band_rebalance / underweight_double: not a quiet check,
# not an edge fill, so skip-halving, the same-pair streak, and pair multipliers stay put.
ISOLATED_FILL_KINDS = (
    "band_rebalance",
    "underweight_double",
    "friday_rebalance",
    "friday_band_rebalance",
    "rsi_rebalance",
    "daily_v2",  # dailyv2c 2026-10-11: daily_rebalance_v2 fills (reverse capped at 50%)
)
QUOTE_STALE_SEC = 45.0
SOL_FEE_BUFFER = 0.02


def canonical_fill_kind(kind):
    """Map a stored or writer kind. rsi stays rsi. Old Friday band names stay band.

    Strings that contain 'rsi' are rsi_rebalance even if they also say friday
    or rebalance. friday_rebalance and friday_band_rebalance do not contain
    rsi and still become band_rebalance (the 2x reverse).
    """
    k = str(kind or "").strip()
    if not k:
        return ""
    low = k.lower().replace("-", "_").replace(" ", "_")
    if "rsi" in low:
        return "rsi_rebalance"
    if low in ("friday_rebalance", "friday_band_rebalance", "band_rebalance", "band"):
        return "band_rebalance"
    if "underweight" in low:
        return "underweight_double"
    if low == "edge":
        return "edge"
    return k


def kind_from_writer(fill_kind, counts_as_edge, venue):
    """Kind stored on last_fill_by_pair. Does not touch streak or multipliers.

    Explicit rsi_rebalance is kept. Explicit friday_rebalance / friday_band_rebalance
    become band_rebalance. A venue string is only a fallback: 'rsi' wins before
    'friday' or 'rebalance', so a friday RSI venue is not relabeled as a band fill.
    """
    if fill_kind:
        kind = canonical_fill_kind(fill_kind)
        return kind or None
    if counts_as_edge:
        return "edge"
    v = str(venue or "").lower()
    if "rsi" in v:
        return "rsi_rebalance"
    if "underweight" in v:
        return "underweight_double"
    if "band" in v or "friday" in v or "rebalance" in v:
        return "band_rebalance"
    return None


def reverse_eff(kind, composed, leftover_floor=0.15, clamp_floor=0.20, clamp_cap=5.0, reb_mult=2.0):
    """Required reverse profit for a last fill.

    rsi_rebalance: flat 5%. That replaces composed, the 2x multiplier, the 0.20
    floor, and the leftover floor. Not 5% plus leftover. Not doubled.
    band_rebalance and underweight_double (and legacy friday_rebalance names):
    eff% = min(5, max(0.20, 2 * composed)), leftover floor still on top.
    Anything else is a normal edge reverse: clamp(composed), leftover on top.
    """
    canon = canonical_fill_kind(kind)
    composed = float(composed)
    if canon == "rsi_rebalance":
        return {
            "kind": "rsi_rebalance",
            "eff": 5.0,
            "doubled": False,
            "reb_mult": 1.0,
            "leftover_floor": 0.0,
            "required_with_leftover": 5.0,
        }
    if canon in ("band_rebalance", "underweight_double"):
        eff = min(float(clamp_cap), max(float(clamp_floor), float(reb_mult) * composed))
        floor = float(leftover_floor)
        return {
            "kind": canon,
            "eff": eff,
            "doubled": True,
            "reb_mult": float(reb_mult),
            "leftover_floor": floor,
            "required_with_leftover": eff + floor,
        }
    eff = min(float(clamp_cap), max(float(clamp_floor), composed))
    floor = float(leftover_floor)
    return {
        "kind": canon or "edge",
        "eff": eff,
        "doubled": False,
        "reb_mult": 1.0,
        "leftover_floor": floor,
        "required_with_leftover": eff + floor,
    }


# Per-pair reverse window: newest-first, at most 3 fills. Each fill may reverse once.
LAST_FILLS_WINDOW = 3


def make_fill_record(side, rate, tx, time_pt, kind=None, sold_amount=None, bought_amount=None, reversed_flag=False):
    """One window entry. Size is the original fill; reverse sells bought_amount the other way."""
    rec = {
        "side": side,
        "rate": float(rate) if rate is not None else None,
        "tx": tx,
        "time_pt": time_pt,
        "reversed": bool(reversed_flag),
    }
    if kind:
        rec["kind"] = kind
    if sold_amount is not None:
        try:
            rec["sold_amount"] = float(sold_amount)
        except (TypeError, ValueError):
            pass
    if bought_amount is not None:
        try:
            rec["bought_amount"] = float(bought_amount)
        except (TypeError, ValueError):
            pass
    return rec


def _as_fill_list(raw):
    """Normalize legacy single-dict or list storage into a newest-first list."""
    if raw is None:
        return []
    if isinstance(raw, dict):
        # Legacy: one last fill per pair, not yet a window.
        entry = dict(raw)
        entry.setdefault("reversed", False)
        return [entry]
    if isinstance(raw, list):
        out = []
        for item in raw:
            if isinstance(item, dict):
                entry = dict(item)
                entry.setdefault("reversed", False)
                out.append(entry)
        return out
    return []


def pair_fill_window(last_fill_by_pair, pair):
    """Up to LAST_FILLS_WINDOW fills for pair, newest first."""
    store = last_fill_by_pair if isinstance(last_fill_by_pair, dict) else {}
    return _as_fill_list(store.get(pair))[:LAST_FILLS_WINDOW]


def newest_pair_fill(last_fill_by_pair, pair=None):
    """Newest fill for pair, or None.

    Pass (store, pair) for the last_fill_by_pair map, or omit pair and pass a
    raw window list / legacy single-fill dict.
    """
    if pair is None:
        window = _as_fill_list(last_fill_by_pair)
    else:
        window = pair_fill_window(last_fill_by_pair, pair)
    return window[0] if window else None


def reversed_tx_set(reverse_cfg, pair):
    """Durable set of fill txs already reversed on this pair (survives window drop)."""
    cfg = reverse_cfg if isinstance(reverse_cfg, dict) else {}
    by_pair = cfg.get("reversed_fill_txs_by_pair") or {}
    raw = by_pair.get(pair) or []
    return {str(t) for t in raw if t}


def fill_already_reversed(fill, reverse_cfg, pair):
    if not isinstance(fill, dict):
        return True
    if fill.get("reversed"):
        return True
    tx = fill.get("tx")
    if tx and str(tx) in reversed_tx_set(reverse_cfg, pair):
        return True
    return False


def unreversed_fills(last_fill_by_pair, pair, reverse_cfg=None):
    """Window fills still eligible for reverse (not marked, not in durable set)."""
    cfg = reverse_cfg if reverse_cfg is not None else {}
    out = []
    for fill in pair_fill_window(last_fill_by_pair, pair):
        if fill_already_reversed(fill, cfg, pair):
            continue
        out.append(fill)
    return out


def mark_fill_reversed(reverse_cfg, pair, fill_tx):
    """Set reversed on the window entry and record the tx durably."""
    if not fill_tx or not isinstance(reverse_cfg, dict):
        return
    store = reverse_cfg.setdefault("last_fill_by_pair", {})
    window = _as_fill_list(store.get(pair))
    for entry in window:
        if entry.get("tx") == fill_tx:
            entry["reversed"] = True
    store[pair] = window[:LAST_FILLS_WINDOW]
    by_pair = reverse_cfg.setdefault("reversed_fill_txs_by_pair", {})
    seen = list(by_pair.get(pair) or [])
    if fill_tx not in seen:
        seen.append(fill_tx)
    by_pair[pair] = seen


def append_pair_fill(reverse_cfg, pair, fill_rec, reverse_of_tx=None):
    """Append a new fill newest-first, drop beyond 3. Optionally mark reverse_of_tx.

    Does not mark prior fills reversed just because a newer fill happened.
    reverse_of_tx marks that prior fill reversed (once) and records it durably.
    """
    if not isinstance(reverse_cfg, dict):
        raise TypeError("reverse_cfg")
    if reverse_of_tx:
        mark_fill_reversed(reverse_cfg, pair, reverse_of_tx)
    store = reverse_cfg.setdefault("last_fill_by_pair", {})
    window = _as_fill_list(store.get(pair))
    rec = dict(fill_rec)
    rec.setdefault("reversed", False)
    window = [rec] + window
    store[pair] = window[:LAST_FILLS_WINDOW]
    return store[pair]


def fill_bought_amount(fill, trades=None):
    """Amount the reverse must sell: the fill's bought size. Prefer stored field."""
    if isinstance(fill, dict):
        for key in ("bought_amount", "bought_amt"):
            if fill.get(key) is not None:
                try:
                    return float(fill[key])
                except (TypeError, ValueError):
                    pass
        side = fill.get("side")
        tx = fill.get("tx")
        if trades and side:
            for t in reversed(list(trades)):
                if t.get("side") != side:
                    continue
                if tx and t.get("tx") and t.get("tx") != tx:
                    continue
                try:
                    return float(t.get("bought_amount"))
                except (TypeError, ValueError):
                    return None
                break
            # Same side, any tx (legacy seed fallback).
            for t in reversed(list(trades)):
                if t.get("side") == side:
                    try:
                        return float(t.get("bought_amount"))
                    except (TypeError, ValueError):
                        return None
    return None


def candidate_profit_key(c):
    """Sort key for most profitable fee-inclusive net (higher is better).

    Primary: gap_net_pct. Tie: reverse of the newest eligible fill, then reverse
    over basis, then route string for stability.
    """
    if not isinstance(c, dict):
        return (-9999.0, 0, "", "")
    try:
        gap = float(c.get("gap_net_pct"))
    except (TypeError, ValueError):
        gap = -9999.0
    is_rev = 1 if c.get("kind") == "reverse" else 0
    fill_time = str(c.get("fill_time_pt") or c.get("time_pt") or "")
    route = str(c.get("route") or "")
    return (gap, is_rev, fill_time, route)


def pick_best_per_pair(candidates):
    """One most-profitable clearing candidate per pair.

    Different pairs may all be returned (multi-fill check). Two routes on the
    same pair collapse to the single best fee-inclusive net (gap_net_pct). On a
    tie, prefer the reverse of the newest eligible fill. Result is ordered by
    that same profit key descending (best pair first).
    """
    best = {}
    for c in candidates or []:
        if not isinstance(c, dict):
            continue
        pair = c.get("pair")
        if not pair:
            continue
        cur = best.get(pair)
        if cur is None or candidate_profit_key(c) > candidate_profit_key(cur):
            best[pair] = c
    return sorted(best.values(), key=candidate_profit_key, reverse=True)


def wilder_rsi(closes, period=14):
    """Wilder RSI. First average is the simple mean of the first `period` changes.

    Each later bar is (prev * (period-1) + current) / period.
    RSI = 100 - 100 / (1 + avg_gain/avg_loss). avg_loss == 0 -> 100.
    `closes` must be oldest first and must include the last completed day only.
    """
    period = int(period)
    if period < 1:
        raise ValueError("period")
    vals = [float(c) for c in closes]
    if len(vals) < period + 1:
        raise ValueError(f"need at least {period + 1} closes, got {len(vals)}")
    changes = [vals[i] - vals[i - 1] for i in range(1, len(vals))]
    gains = [c if c > 0 else 0.0 for c in changes]
    losses = [-c if c < 0 else 0.0 for c in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _usd(usd_balances, sym):
    try:
        return float((usd_balances or {}).get(sym) or 0)
    except (TypeError, ValueError):
        return 0.0


def _extreme(rsi_by_coin, want_high):
    """Coin at the extreme RSI. Ties use USD, then SOL, cbBTC, WETH."""
    ranked = []
    for sym in COINS:
        if sym not in rsi_by_coin:
            raise KeyError(sym)
        ranked.append((float(rsi_by_coin[sym]), sym))
    extreme = max(v for v, _ in ranked) if want_high else min(v for v, _ in ranked)
    group = [sym for v, sym in ranked if v == extreme]
    return extreme, group


def basis_unwinds_rsi(last_fill, sold, bought):
    """True when a normal (non-reverse) edge fill would undo an rsi_rebalance.

    While the newest last_fill_by_pair entry kind is rsi_rebalance, do not sell
    the coin that trade bought and do not buy back the coin it sold. Accepts a
    legacy single dict or a newest-first window list. The dedicated reverse is
    a different path and is not passed here. Other kinds, and other pairs,
    return False. band_rebalance is not this guard.
    """
    if isinstance(last_fill, list):
        last_fill = last_fill[0] if last_fill else None
    if not isinstance(last_fill, dict):
        return False
    if canonical_fill_kind(last_fill.get("kind")) != "rsi_rebalance":
        return False
    side = str(last_fill.get("side") or "")
    if "\u2192" in side or "→" in side:
        prev_sold, prev_bought = side.replace("\u2192", "→").split("→", 1)
    elif "->" in side:
        prev_sold, prev_bought = side.split("->", 1)
    else:
        return False
    prev_sold, prev_bought = prev_sold.strip(), prev_bought.strip()
    if not prev_sold or not prev_bought:
        return False
    return str(sold) == prev_bought or str(bought) == prev_sold


def pick_case(rsi_by_coin, usd_balances, gap=15.0, low=30.0, high=70.0, sell_fraction=0.75,
              usdc_fraction=0.75, dust_usd=1.0):
    """Pick exactly one RSI case. No fall-through.

    1. Gap (highest minus lowest) >= 15: sell 75% of the high coin into USDC
       and buy the low coin with those proceeds. Exactly 15 counts.
    2. Gap under 15 and lowest RSI <= 30: spend 75% of USDC on the low coin.
       Exactly 30 counts.
    3. Highest RSI >= 70 and no coin <= 30: sell 75% of the high coin into USDC.
       Exactly 70 counts. 60 is not enough. If something is <= 30, case 1 or 2
       already ran.
    4. Otherwise do nothing. Old friday_band rules do not run.

    A skipped leg does not select a later case. USDC is not ranked. A coin with
    USD <= 0 is not held and cannot be sold; the case stays, and case 1 then
    does not buy. Dust is a slice under dust_usd.
    Highest-RSI tie: larger USD balance, then SOL, cbBTC, WETH.
    Lowest-RSI tie: smaller USD balance, then the same order.
    """
    rsi = {}
    for sym in COINS:
        if sym not in (rsi_by_coin or {}):
            raise KeyError(f"missing rsi for {sym}")
        rsi[sym] = float(rsi_by_coin[sym])
    hi, hi_group = _extreme(rsi, True)
    lo, lo_group = _extreme(rsi, False)
    high_coin = min(hi_group, key=lambda s: (-_usd(usd_balances, s), COINS.index(s)))
    low_coin = min(lo_group, key=lambda s: (_usd(usd_balances, s), COINS.index(s)))
    gap_pts = hi - lo
    if gap_pts + 1e-12 >= float(gap):
        case = 1
        sell, buy = high_coin, low_coin
    elif lo - 1e-12 <= float(low):
        case = 2
        sell, buy = None, low_coin
    elif hi + 1e-12 >= float(high):
        case = 3
        sell, buy = high_coin, None
    else:
        case = 4
        sell, buy = None, None

    dust = float(dust_usd)
    sell_frac = float(sell_fraction)
    usdc_frac = float(usdc_fraction)
    sell_skip = None
    buy_skip = None
    sell_slice_usd = 0.0
    buy_slice_usd = 0.0
    if case in (1, 3) and sell:
        held_usd = _usd(usd_balances, sell)
        sell_slice_usd = held_usd * sell_frac
        if held_usd <= 0:
            sell_skip = "not_held"
            sell_slice_usd = 0.0
        elif sell_slice_usd < dust:
            sell_skip = "dust"
        if case == 1 and sell_skip:
            buy_skip = "no_proceeds"
            buy_slice_usd = 0.0
        elif case == 1:
            # Buy spends the USDC proceeds of this sell, not half the cash already held.
            buy_slice_usd = sell_slice_usd
    if case == 2 and buy:
        usdc = _usd(usd_balances, "USDC")
        buy_slice_usd = usdc * usdc_frac
        if buy_slice_usd < dust:
            buy_skip = "dust"
            buy_slice_usd = 0.0 if usdc <= 0 else buy_slice_usd
    would_trade = False
    if case in (1, 3) and sell and not sell_skip:
        would_trade = True
    elif case == 2 and buy and not buy_skip:
        would_trade = True
    return {
        "case": case,
        "sell_coin": sell,
        "buy_coin": buy,
        "high_rsi": hi,
        "low_rsi": lo,
        "gap": gap_pts,
        "high_coin": high_coin if case in (1, 3) else None,
        "low_coin": low_coin if case in (1, 2) else None,
        "sell_skip": sell_skip,
        "buy_skip": buy_skip,
        "sell_slice_usd": sell_slice_usd,
        "buy_slice_usd": buy_slice_usd,
        "buy_usd_is": "sell_proceeds" if case == 1 and not sell_skip else ("usdc_balance_fraction" if case == 2 else None),
        "would_trade": would_trade,
        "rsi": rsi,
    }


def sol_sell_qty(sol_qty, fraction=0.75, buffer=SOL_FEE_BUFFER):
    """min(fraction of SOL, SOL balance minus the fee buffer). Never below 0."""
    qty = float(sol_qty or 0)
    room = max(0.0, qty - float(buffer))
    return min(qty * float(fraction), room)


def apply_sol_buffer(pick, qty_balances, prices, fraction=0.75, buffer=SOL_FEE_BUFFER, dust_usd=1.0):
    """When the sold coin is SOL, cap the slice and dust-skip the remainder.

    Does not change the case. A dust skip after the buffer is still not a
    different case, and case 1 still does not buy.
    """
    out = dict(pick)
    sell = out.get("sell_coin")
    if out.get("case") not in (1, 3) or not sell or out.get("sell_skip"):
        out["sell_qty"] = 0.0
        out["buy_qty_est"] = None
        return out
    qty = float((qty_balances or {}).get(sell) or 0)
    px = float((prices or {}).get(sell) or 0)
    if qty <= 0:
        out["sell_skip"] = "not_held"
        out["sell_qty"] = 0.0
        out["sell_slice_usd"] = 0.0
        out["would_trade"] = False
        if out.get("case") == 1:
            out["buy_skip"] = "no_proceeds"
            out["buy_slice_usd"] = 0.0
        return out
    if sell == "SOL":
        sell_qty = sol_sell_qty(qty, fraction, buffer)
    else:
        sell_qty = qty * float(fraction)
    slice_usd = sell_qty * px if px > 0 else 0.0
    out["sell_qty"] = sell_qty
    out["sell_slice_usd"] = slice_usd
    if sell_qty <= 0 or (px > 0 and slice_usd < float(dust_usd)):
        out["sell_skip"] = "dust"
        out["would_trade"] = False
        if out.get("case") == 1:
            out["buy_skip"] = "no_proceeds"
            out["buy_slice_usd"] = 0.0
        return out
    if out.get("case") == 1:
        out["buy_slice_usd"] = slice_usd
        out["buy_usd_is"] = "sell_proceeds"
    out["would_trade"] = True
    return out


def band_status(usd_by_asset, two_smallest_min=0.05, third_min=0.10, largest_max=0.75):
    """Friday bands on the four-coin book. JitoSOL and Greedy Goats are ignored.

    Two smallest >= 5%, third-smallest >= 10%, largest <= 75%.
    """
    clean = {}
    for sym in BOOK:
        clean[sym] = _usd(usd_by_asset, sym)
    total = sum(clean.values())
    ranked = sorted(clean.items(), key=lambda kv: (kv[1], BOOK.index(kv[0])))
    violations = []
    if total <= 0:
        return {"ok": True, "violations": [], "ranked": ranked, "total": 0.0, "pct": {s: 0.0 for s in BOOK}}
    pct = {s: clean[s] / total for s in BOOK}
    floors = (
        (0, float(two_smallest_min), "smallest"),
        (1, float(two_smallest_min), "second_smallest"),
        (2, float(third_min), "third_smallest"),
    )
    for idx, floor, label in floors:
        sym, usd = ranked[idx]
        if pct[sym] + 1e-12 < floor:
            violations.append({
                "symbol": sym,
                "pct": pct[sym] * 100.0,
                "need_pct": floor * 100.0,
                "label": label,
                "text": f"{sym} {pct[sym] * 100.0:.4f}% < {floor * 100.0:.0f} {label}",
            })
    sym, usd = ranked[-1]
    if pct[sym] - 1e-12 > float(largest_max):
        violations.append({
            "symbol": sym,
            "pct": pct[sym] * 100.0,
            "need_pct": float(largest_max) * 100.0,
            "label": "largest",
            "text": f"{sym} {pct[sym] * 100.0:.4f}% > {float(largest_max) * 100.0:.0f} largest",
        })
    return {
        "ok": not violations,
        "violations": violations,
        "ranked": ranked,
        "total": total,
        "pct": pct,
    }


def plan_band_moves(usd_by_asset, qty_balances=None, prices=None, buffer=SOL_FEE_BUFFER,
                    two_smallest_min=0.05, third_min=0.10, largest_max=0.75):
    """USD to move from the largest holding into under-band coins. No quote, no send.

    Direct book pair only. Does not sell JitoSOL or Greedy Goats. SOL sales keep
    the fee buffer. Returns [] when the book is already inside the bands.
    """
    status = band_status(usd_by_asset, two_smallest_min, third_min, largest_max)
    if status["ok"] or status["total"] <= 0:
        return []
    total = status["total"]
    ranked = status["ranked"]
    floors = {
        ranked[0][0]: float(two_smallest_min) * total,
        ranked[1][0]: float(two_smallest_min) * total,
        ranked[2][0]: float(third_min) * total,
    }
    largest = ranked[-1][0]
    usd = {s: _usd(usd_by_asset, s) for s in BOOK}
    moves = []
    for sym, _ in ranked[:3]:
        need = floors[sym] - usd[sym]
        if need > 0.01:
            moves.append({"sold": largest, "bought": sym, "usd": need, "reason": "under_band"})
    spent = sum(m["usd"] for m in moves)
    excess = (usd[largest] - spent) - float(largest_max) * total
    if excess > 0.01:
        sink = ranked[0][0]
        if sink == largest:
            sink = ranked[1][0]
        moves.append({"sold": largest, "bought": sink, "usd": excess, "reason": "largest_over_cap"})
    # Cap SOL so the fee buffer stays. Shrink later legs first if needed? Cap the total.
    if largest == "SOL" and qty_balances is not None and prices is not None:
        px = float((prices or {}).get("SOL") or 0)
        qty = float((qty_balances or {}).get("SOL") or 0)
        room_qty = max(0.0, qty - float(buffer))
        room_usd = room_qty * px if px > 0 else 0.0
        left = room_usd
        capped = []
        for m in moves:
            take = min(m["usd"], left)
            left -= take
            if take <= 0.01:
                continue
            row = dict(m)
            row["usd"] = take
            if take + 0.01 < m["usd"]:
                row["capped"] = "sol_fee_buffer"
            capped.append(row)
        moves = capped
    out = []
    for m in moves:
        if m["sold"] in EXCLUDED or m["bought"] in EXCLUDED:
            continue
        if m["sold"] == m["bought"]:
            continue
        pair = _pair_of(m["sold"], m["bought"])
        row = dict(m)
        row["pair"] = pair
        row["fill_kind"] = "band_rebalance"
        out.append(row)
    return out


def _pair_of(sold, bought):
    key = frozenset((sold, bought))
    for pair, (base, quote) in PAIR_BASE_QUOTE.items():
        if frozenset((base, quote)) == key:
            return pair
    raise KeyError(f"no book pair for {sold}->{bought}")


def capped_transfer_usd(usd_book, sold, bought, requested, floor_pct=0.05, cap_pct=0.75):
    """Largest USD to move from sold to bought without breaking the RSI book rules.

    Not the 15% portfolio cap and not the 40% of-token cap. Those are not applied.
    After the move, the bought coin is at or under cap_pct (75%). Every coin other
    than the one sold is at or over floor_pct (5%). The sold coin itself may finish
    under 5%, except a USDC spend, which must leave USDC at or over 5%.
    Equal to 5% and equal to 75% are allowed. Returns 0 when no positive size works.
    Total USD is unchanged: this is pre-trade balances plus the slice.
    """
    book = {s: max(0.0, float((usd_book or {}).get(s) or 0)) for s in BOOK}
    total = sum(book.values())
    requested = float(requested or 0)
    if total <= 0 or requested <= 0 or sold not in BOOK or bought not in BOOK or sold == bought:
        return 0.0
    floor = float(floor_pct) * total
    for sym in BOOK:
        if sym == sold or sym == bought:
            continue
        if book[sym] + 1e-9 < floor:
            return 0.0
    room_cap = float(cap_pct) * total - book[bought]
    if sold == "USDC":
        room_floor = book["USDC"] - floor
    else:
        room_floor = book[sold]
    need = floor - book[bought]
    x_max = min(requested, book[sold], room_cap, room_floor)
    if x_max <= 1e-12:
        return 0.0
    if need > 1e-9 and x_max + 1e-9 < need:
        return 0.0
    return x_max


def apply_book_limits(pick, usd_book, prices=None, dust_usd=1.0, floor_pct=0.05, cap_pct=0.75):
    """Cut an RSI slice so the 5% floor and 75% cap hold. Does not change the case.

    A cut that leaves less than dust_usd skips that leg as dust and does not
    fall through. Case 1 with a skipped sell still does not buy.
    """
    out = dict(pick)
    out["book_cut"] = False
    prices = prices or {}
    dust = float(dust_usd)

    def _qty(sym, usd):
        if sym == "USDC":
            return float(usd)
        px = float(prices.get(sym) or 0)
        if px <= 0:
            return None
        return float(usd) / px

    if out.get("case") in (1, 3) and out.get("sell_coin") and not out.get("sell_skip"):
        sold = out["sell_coin"]
        requested = float(out.get("sell_slice_usd") or 0)
        allowed = capped_transfer_usd(usd_book, sold, "USDC", requested, floor_pct, cap_pct)
        if allowed + 1e-9 < requested:
            out["book_cut"] = True
        if allowed < dust:
            out["sell_skip"] = "dust"
            out["sell_slice_usd"] = 0.0
            out["sell_qty"] = 0.0
            out["would_trade"] = False
            if out.get("case") == 1:
                out["buy_skip"] = "no_proceeds"
                out["buy_slice_usd"] = 0.0
            return out
        out["sell_slice_usd"] = allowed
        qty = _qty(sold, allowed)
        if qty is not None:
            out["sell_qty"] = qty
        if out.get("case") == 1 and out.get("buy_coin"):
            post = {s: max(0.0, float((usd_book or {}).get(s) or 0)) for s in BOOK}
            post[sold] = post[sold] - allowed
            post["USDC"] = post["USDC"] + allowed
            buy_allowed = capped_transfer_usd(post, "USDC", out["buy_coin"], allowed, floor_pct, cap_pct)
            if buy_allowed + 1e-9 < allowed:
                out["book_cut"] = True
            if buy_allowed < dust:
                out["buy_skip"] = "dust"
                out["buy_slice_usd"] = 0.0
                out["would_trade"] = True
            else:
                out["buy_skip"] = None
                out["buy_slice_usd"] = buy_allowed
                out["buy_qty"] = buy_allowed
                out["buy_usd_is"] = "sell_proceeds"
                out["would_trade"] = True
        else:
            out["would_trade"] = True
        return out

    if out.get("case") == 2 and out.get("buy_coin") and not out.get("buy_skip"):
        requested = float(out.get("buy_slice_usd") or 0)
        allowed = capped_transfer_usd(usd_book, "USDC", out["buy_coin"], requested, floor_pct, cap_pct)
        if allowed + 1e-9 < requested:
            out["book_cut"] = True
        if allowed < dust:
            out["buy_skip"] = "dust"
            out["buy_slice_usd"] = 0.0
            out["buy_qty"] = 0.0
            out["would_trade"] = False
            return out
        out["buy_slice_usd"] = allowed
        out["buy_qty"] = allowed
        out["would_trade"] = True
        return out
    return out


def _band_fracs(band_cfg):
    """Config stores 5 / 10 / 75. A value already <= 1 is a fraction."""
    cfg = band_cfg or {}
    return (
        _as_frac(cfg.get("two_smallest_min_pct", 5)),
        _as_frac(cfg.get("third_smallest_min_pct", 10)),
        _as_frac(cfg.get("largest_max_pct", 75)),
    )


def _bands_allowed(band_cfg=None, rsi_cfg=None):
    """Bands run only when friday_band is enabled/not retired AND bands_if_no_trade is true.

    Locked default: retired. bands_if_no_trade false. Legacy band helpers stay for tests
    of reverse math and historical fills, but friday_path never selects them.
    """
    cfg = rsi_cfg or {}
    band = band_cfg or {}
    if band.get("retired") is True:
        return False
    if band.get("enabled") is False:
        return False
    bif = cfg.get("bands_if_no_trade", False)
    if isinstance(bif, str):
        # Legacy descriptive string was truthy; treat only explicit true/"true" as on.
        return bif.strip().lower() in ("true", "1", "yes")
    return bool(bif)


def friday_path(pick, usd_book, qty_balances=None, prices=None, band_cfg=None, rsi_cfg=None):
    """RSI trade path, or none. Old Friday bands are retired and do not run."""
    two, third, cap = _band_fracs(band_cfg)
    status = band_status(usd_book, two, third, cap)
    if pick.get("would_trade"):
        return {
            "path": "rsi",
            "bands_would_run": False,
            "reason": "rsi_trade",
            "bands": status,
            "moves": [],
        }
    if not _bands_allowed(band_cfg, rsi_cfg):
        return {
            "path": "none",
            "bands_would_run": False,
            "reason": "rsi_no_trade_bands_retired",
            "bands": status,
            "moves": [],
        }
    if status["ok"]:
        return {
            "path": "none",
            "bands_would_run": False,
            "reason": "rsi_no_trade_bands_ok",
            "bands": status,
            "moves": [],
        }
    moves = plan_band_moves(
        usd_book, qty_balances, prices,
        two_smallest_min=two, third_min=third, largest_max=cap,
    )
    return {
        "path": "band",
        "bands_would_run": True,
        "reason": "rsi_no_trade",
        "bands": status,
        "moves": moves,
    }


def _as_frac(value, default=0.0):
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = float(default)
    if v > 1.0:
        return v / 100.0
    return v


def _http_json(url, timeout=25):
    """v1.0.0: keyless.get_json = short backoff on 429 / 5xx / timeout, then the caller rotates sources."""
    import keyless
    return keyless.get_json(url, timeout=timeout, tries=2)


def _completed_binance(symbol, limit=120):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1d&limit={int(limit)}"
    rows = _http_json(url)
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"binance_empty:{symbol}")
    now_ms = int(time.time() * 1000)
    closes = []
    for row in rows:
        close_time = int(row[6])
        if close_time >= now_ms:
            continue
        closes.append(float(row[4]))
    if len(closes) < 15:
        raise RuntimeError(f"binance_short:{symbol}:{len(closes)}")
    return closes[-100:]


def _completed_coinbase(product):
    url = f"https://api.exchange.coinbase.com/products/{product}/candles?granularity=86400"
    rows = _http_json(url)
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"coinbase_empty:{product}")
    now = time.time()
    # Coinbase: [time, low, high, open, close, volume], newest first. time is the open.
    done = []
    for row in rows:
        start = float(row[0])
        if start + 86400 > now:
            continue
        done.append((start, float(row[4])))
    done.sort(key=lambda item: item[0])
    closes = [c for _, c in done]
    if len(closes) < 15:
        raise RuntimeError(f"coinbase_short:{product}:{len(closes)}")
    return closes[-100:]


def fetch_rsi_closes():
    """Last completed UTC daily closes. Binance first, Coinbase exchange candles if it fails."""
    errors = []
    try:
        out = {}
        for sym, pair in BINANCE_SYMBOL.items():
            out[sym] = _completed_binance(pair)
        return out, "binance", errors
    except Exception as e:
        errors.append(f"binance:{type(e).__name__}:{e}")
    try:
        out = {}
        for sym, product in COINBASE_PRODUCT.items():
            out[sym] = _completed_coinbase(product)
        return out, "coinbase", errors
    except Exception as e:
        errors.append(f"coinbase:{type(e).__name__}:{e}")
    return None, None, errors


def fetch_spot_prices():
    """Live USD marks for slice sizing. Binance ticker, else Coinbase ticker. Not a trade."""
    prices = {"USDC": 1.0}
    try:
        for sym, pair in BINANCE_SYMBOL.items():
            row = _http_json(f"https://api.binance.com/api/v3/ticker/price?symbol={pair}")
            prices[sym] = float(row["price"])
        return prices, "binance_ticker"
    except Exception as e:
        err = f"binance_ticker_failed:{type(e).__name__}:{e}"
    try:
        for sym, product in COINBASE_PRODUCT.items():
            row = _http_json(f"https://api.exchange.coinbase.com/products/{product}/ticker")
            prices[sym] = float(row["price"])
        return prices, "coinbase_ticker_after_" + err
    except Exception as e2:
        return prices, err + f"|coinbase_ticker_failed:{type(e2).__name__}:{e2}"


def _rpc(method, params):
    """Shared order: helius, alchemy, public. Falls through on failure. Does not query legacy wallet."""
    return solana_rpc.rpc(method, params, retries=6, timeout=30)


def fetch_main_wallet_balances():
    """SOL/USDC/cbBTC/WETH for wallet only. Does not query legacy wallet."""
    if ADDR == ADDR_OLD:
        raise RuntimeError("refusing_legacy")
    sol = _rpc("getBalance", [ADDR])["result"]["value"] / 1e9
    tok = _rpc("getTokenAccountsByOwner", [
        ADDR,
        {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
        {"encoding": "jsonParsed"},
    ])
    bals = {"SOL": float(sol), "USDC": 0.0, "cbBTC": 0.0, "WETH": 0.0}
    want = {MINTS["USDC"]: "USDC", MINTS["cbBTC"]: "cbBTC", MINTS["WETH"]: "WETH"}
    for v in tok["result"]["value"]:
        info = v["account"]["data"]["parsed"]["info"]
        mint = info["mint"]
        if mint in want:
            bals[want[mint]] = float(info["tokenAmount"]["uiAmount"] or 0)
    return bals


def load_friday_config(path=CB_PATH):
    with open(path) as f:
        cb = json.load(f)
    rsi = cb.get("friday_rsi") or {}
    band = cb.get("friday_band") or {}
    return cb, rsi, band


def decide(rsi_by_coin, qty_balances, prices, rsi_cfg=None, band_cfg=None):
    """Pure decision from already-fetched RSI, quantities, and USD prices."""
    cfg = rsi_cfg or {}
    gap = float(cfg.get("gap_rsi", 15))
    low = float(cfg.get("low_rsi", 30))
    high = float(cfg.get("high_rsi", 70))
    sell_frac = float(cfg.get("sell_fraction", 0.75))
    usdc_frac = float(cfg.get("usdc_fraction", 0.75))
    max_sell = float(cfg.get("max_sell_fraction_one_check", 0.75))
    max_usdc = float(cfg.get("max_usdc_fraction_one_check", 0.75))
    sell_frac = min(sell_frac, max_sell)
    usdc_frac = min(usdc_frac, max_usdc)
    dust = float(cfg.get("dust_usd", 1))
    buffer = float(cfg.get("sol_fee_buffer", SOL_FEE_BUFFER))
    floor_pct = _as_frac(cfg.get("book_floor_pct", 5))
    cap_pct = _as_frac(cfg.get("book_cap_pct", 75))
    usd = {}
    for sym in BOOK:
        px = 1.0 if sym == "USDC" else float((prices or {}).get(sym) or 0)
        usd[sym] = float((qty_balances or {}).get(sym) or 0) * px
    pick = pick_case(
        rsi_by_coin, usd, gap=gap, low=low, high=high,
        sell_fraction=sell_frac, usdc_fraction=usdc_frac, dust_usd=dust,
    )
    pick = apply_sol_buffer(
        pick, qty_balances, prices, fraction=sell_frac, buffer=buffer, dust_usd=dust,
    )
    pick = apply_book_limits(pick, usd, prices, dust_usd=dust, floor_pct=floor_pct, cap_pct=cap_pct)
    path = friday_path(pick, usd, qty_balances, prices, band_cfg, cfg)
    return {"pick": pick, "usd": usd, "path": path, "prices": prices}


def format_report(decision, source, errors=None):
    pick = decision["pick"]
    path = decision["path"]
    rsi = pick.get("rsi") or {}
    lines = [
        "FRIDAY_RSI dry_run",
        f"wallet {ADDR}",
        f"candle_source {source}",
        f"rsi SOL={rsi.get('SOL')} cbBTC={rsi.get('cbBTC')} WETH={rsi.get('WETH')}",
        f"case {pick.get('case')} gap {pick.get('gap')} sell {pick.get('sell_coin')} buy {pick.get('buy_coin')}",
        f"book_cut {pick.get('book_cut')} sell_usd_after_cap {pick.get('sell_slice_usd')} buy_usd_after_cap {pick.get('buy_slice_usd')}",
        f"sell_skip {pick.get('sell_skip')} buy_skip {pick.get('buy_skip')} would_trade {pick.get('would_trade')}",
        (
            f"slices sell_qty {pick.get('sell_qty')} sell_usd {pick.get('sell_slice_usd')} "
            f"buy_usd {pick.get('buy_slice_usd')} buy_usd_is {pick.get('buy_usd_is')}"
        ),
        f"bands_would_run {path.get('bands_would_run')} path {path.get('path')} reason {path.get('reason')}",
    ]
    bands = path.get("bands") or {}
    if bands.get("pct"):
        order = ">".join(sym for sym, _ in bands.get("ranked") or [])
        bits = " ".join(f"{sym}={bands['pct'][sym] * 100:.4f}%" for sym in BOOK)
        lines.append(f"band_book {bits} order_small_to_large {order} bands_ok {bands.get('ok')}")
    for v in bands.get("violations") or []:
        lines.append(f"band_violation {v.get('text')}")
    for m in path.get("moves") or []:
        lines.append(
            f"band_move {m.get('sold')}->{m.get('bought')} usd {m.get('usd')} pair {m.get('pair')} kind band_rebalance"
        )
    if path.get("path") == "rsi":
        lines.append("bands_suppressed_by_rsi_trade")
    if errors:
        lines.append("candle_errors " + " | ".join(errors))
    lines.append("NO_TX")
    return "\n".join(lines)


def build_decision():
    closes, source, errors = fetch_rsi_closes()
    if not closes:
        raise RuntimeError("candle_fetch_failed:" + " | ".join(errors))
    rsi = {sym: wilder_rsi(series) for sym, series in closes.items()}
    prices, price_src = fetch_spot_prices()
    if any(sym not in prices for sym in COINS):
        # Fall back to the last completed daily close (closes are required above;
        # if they were unavailable build_decision already raised and the run skips with NO_TX).
        import keyless
        keyless.warn("friday_spot", f"friday_rsi: live prices unavailable ({price_src}); using last completed daily close")
        for sym, series in closes.items():
            prices.setdefault(sym, series[-1])
        price_src = price_src + "+last_completed_close"
    _cb, rsi_cfg, band_cfg = load_friday_config()
    # Saved cbBTC (savings_mode.savings_cbbtc_held) is not RSI/band inventory.
    import savings_mode
    bals = savings_mode.trading_balances(_cb, fetch_main_wallet_balances())
    decision = decide(rsi, bals, prices, rsi_cfg, band_cfg)
    decision["candle_source"] = source
    decision["price_source"] = price_src
    decision["errors"] = errors
    decision["balances"] = bals
    decision["closes_used"] = {sym: len(series) for sym, series in closes.items()}
    return decision


def _to_base(amt, sym):
    return int(round(float(amt) * (10 ** DEC[sym])))


def _best_row(rows, bought, sol_usd, bought_px):
    import venue_quotes as VQ
    scored = []
    for row in rows:
        if not row.get("ok"):
            continue
        if VQ.is_ultra_labeled(row):
            continue
        if str(row.get("venue")) not in ("jupiter_manual", "raydium"):
            continue
        fee = row.get("platform_fee_bps")
        if fee not in (None, 0):
            continue
        out = int(row["out_amount"]) / (10 ** DEC[bought])
        gas = float(row.get("gas_sol") or 0)
        px = float(bought_px or 0)
        if px > 0 and sol_usd > 0:
            net = out - (gas * float(sol_usd) / px)
        else:
            net = out
        rank = 2 if row.get("venue") == "jupiter_manual" else 1
        scored.append((net, rank, row))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return scored[0][2]


def quote_winning_route(sold, bought, sell_amt, sol_usd, bought_px):
    """Jupiter manual and Raydium. Re-quote this route only if the quote is stale. No send."""
    import venue_quotes as VQ
    amount = _to_base(sell_amt, sold)
    started = time.monotonic()
    rows = VQ.fetch_venue_quotes(sold, bought, amount)
    best = _best_row(rows, bought, sol_usd, bought_px)
    if best is None:
        return None, rows, started
    if time.monotonic() - started > QUOTE_STALE_SEC:
        started = time.monotonic()
        rows = VQ.fetch_venue_quotes(sold, bought, amount)
        best = _best_row(rows, bought, sol_usd, bought_px)
    return best, rows, started


def _tokens_prices(cb, sold, bought, sell_amt, buy_amt):
    """USD prices for tokens_book: the fill itself when one leg is USDC, else last check's prices."""
    px = {}
    try:
        with open(_DH.HOME_STR + "/last-check.json") as _f:
            px = dict(json.load(_f).get("prices_usd") or {})
    except Exception:
        px = {}
    if bought == "USDC" and float(sell_amt) > 0:
        px[sold] = float(buy_amt) / float(sell_amt)
    if sold == "USDC" and float(buy_amt) > 0:
        px[bought] = float(sell_amt) / float(buy_amt)
    return px


PAIR_DIRECT_RE = r"direct ([0-9]+(?:\.[0-9]+)?(?:e-?[0-9]+)?) base @([0-9]+(?:\.[0-9]+)?)"


def pair_book_update(pair, pinfo, base_qty, quote_qty, fill_rate):
    """Average every fill into pinfo['book_base'/'book_quote'] (proposed 2026-10-12).

    Old code: when fills_note had no 'X@r' parts (SOL-WETH, cbBTC-WETH: notes are
    'direct X base @r', which the old regex never matched) the rate was
    overwritten by the last fill. Seeds the totals once from the note, then adds.
    """
    import re as _re
    note = pinfo.get("fills_note") or ""
    if "book_base" not in pinfo:
        parts = _re.findall(r"(?<![a-z] )(?<!base )\b([0-9]+(?:\.[0-9]+)?)@([0-9]+(?:\.[0-9]+)?)", note)
        direct = _re.findall(PAIR_DIRECT_RE, note)
        qw = pair in ("USDC-cbBTC", "USDC-WETH")
        b = q = 0.0
        for a, r in parts:
            a, r = float(a), float(r)
            if r <= 0:
                continue
            if qw:
                q += a; b += a / r
            else:
                b += a; q += a * r
        for a, r in direct:
            b += float(a); q += float(a) * float(r)
        if b <= 0:
            b = 0.0; q = 0.0
        pinfo["book_base"], pinfo["book_quote"] = b, q
    pinfo["book_base"] = float(pinfo["book_base"]) + float(base_qty)
    pinfo["book_quote"] = float(pinfo["book_quote"]) + float(quote_qty)
    new_rate = pinfo["book_quote"] / pinfo["book_base"] if pinfo["book_base"] > 0 else fill_rate
    pinfo["fills_note"] = (note + " | " if note else "") + f"direct {base_qty} base @{fill_rate:.6f}"
    return new_rate


def record_rebalance_fill(pair, sold, bought, sell_amt, buy_amt, fill_rate, sig, when, fill_kind):
    """Update the rate book and last_fill_by_pair the way other wallet fills do.

    Does not change the same-pair streak, pair multipliers, or skip-halving.
    fill_kind rsi_rebalance stays rsi_rebalance. band_rebalance stays band.
    """
    kind = kind_from_writer(fill_kind, False, None)
    if kind not in ("rsi_rebalance", "band_rebalance", "underweight_double", "friday_trim75", "daily_v2"):
        raise RuntimeError(f"refusing_kind:{kind}")
    with open(CB_PATH) as f:
        cb = json.load(f)
    pairs = cb["pairs"]
    base, quote = PAIR_BASE_QUOTE[pair]
    # Prefer the file's base/quote when present.
    base = pairs[pair]["base"]
    quote = pairs[pair]["quote"]
    amts = {sold: float(sell_amt), bought: float(buy_amt)}
    base_qty = amts[base]
    quote_qty = amts[quote]
    import re
    pinfo = pairs[pair]
    new_rate = pair_book_update(pair, pinfo, base_qty, quote_qty, fill_rate)
    pinfo["rate"] = new_rate
    tokens_book.record_fill(cb, sold, bought, sell_amt, buy_amt,
                            _tokens_prices(cb, sold, bought, sell_amt, buy_amt), when)
    pinfo["source"] = f"fill_{kind}_local_signer"
    side_str = f"{sold}\u2192{bought}"
    rev_cfg = cb.setdefault("reverse_last_trade", {})
    append_pair_fill(
        rev_cfg,
        pair,
        make_fill_record(
            side_str, fill_rate, sig, when, kind=kind,
            sold_amount=sell_amt, bought_amount=buy_amt, reversed_flag=False,
        ),
    )
    cb["updated_pt"] = when
    import fade_trend as _FT  # bugfix1012: atomic write (no truncated file for a concurrent reader)
    _FT.atomic_write_json(CB_PATH, cb)
    return new_rate


def execute_leg(sold, bought, sell_amt, prices, fill_kind, dry_run):
    """Quote the best net and, only when not dry-run, sign and send one swap."""
    if dry_run:
        raise RuntimeError("dry_run_refuses_sign")
    if sold in EXCLUDED or bought in EXCLUDED:
        raise RuntimeError("refusing_excluded_asset")
    if fill_kind == "rsi_rebalance" and (sold, bought) not in RSI_PAIRS:
        raise RuntimeError(f"rsi_route_not_usdc:{sold}->{bought}")
    pair = _pair_of(sold, bought)
    sol_usd = float(prices.get("SOL") or 0)
    bought_px = 1.0 if bought == "USDC" else float(prices.get(bought) or 0)
    best, _rows, started = quote_winning_route(sold, bought, sell_amt, sol_usd, bought_px)
    if not best:
        return {"sent": False, "error": "no_quote", "pair": pair}
    import venue_quotes as VQ
    import savings_mode
    if best["venue"] == "jupiter_manual":
        tx_b64, err = VQ.build_jupiter_manual_tx(best.get("payload"))
        txs = [tx_b64] if tx_b64 else []
    elif best["venue"] == "raydium":
        txs, err = VQ.build_raydium_tx(best.get("swap_response"), sold, bought)
        txs = list(txs or [])
    else:
        return {"sent": False, "error": f"venue:{best.get('venue')}", "pair": pair}
    if err or not txs:
        return {"sent": False, "error": err or "no_tx", "pair": pair}
    # Same stale rule as the edge checker: re-quote only this route, not the others.
    if time.monotonic() - started > QUOTE_STALE_SEC:
        best, _rows, started = quote_winning_route(sold, bought, sell_amt, sol_usd, bought_px)
        if not best:
            return {"sent": False, "error": "stale_requote_failed", "pair": pair}
    kp = savings_mode._load_keypair()

    def _rebuild():
        best2, _rows2, _started2 = quote_winning_route(sold, bought, sell_amt, sol_usd, bought_px)
        if not best2:
            return None
        if best2["venue"] == "jupiter_manual":
            tx2, err2 = VQ.build_jupiter_manual_tx(best2.get("payload"))
            built = [tx2] if tx2 else []
        elif best2["venue"] == "raydium":
            built, err2 = VQ.build_raydium_tx(best2.get("swap_response"), sold, bought)
            built = list(built or [])
        else:
            return None
        if err2 or not built:
            return None
        return built

    sig, err_s, uncertain = savings_mode.send_built_txs(txs, kp, _rebuild, accept_unconfirmed=False)
    if err_s or not sig:
        return {
            "sent": False,
            "error": err_s or "send_failed",
            "pair": pair,
            "submitted": bool(uncertain),
        }
    buy_amt = int(best["out_amount"]) / (10 ** DEC[bought])
    base, quote = PAIR_BASE_QUOTE[pair]
    amts = {sold: float(sell_amt), bought: float(buy_amt)}
    fill_rate = amts[quote] / amts[base]
    when = datetime.now(PT).replace(microsecond=0).isoformat()
    record_rebalance_fill(pair, sold, bought, sell_amt, buy_amt, fill_rate, sig, when, fill_kind)
    return {
        "sent": True,
        "signature": sig,
        "pair": pair,
        "sold": sold,
        "bought": bought,
        "sell_amt": sell_amt,
        "buy_amt": buy_amt,
        "kind": canonical_fill_kind(fill_kind),
        "venue": best.get("venue"),
    }


def run_live(decision):
    """Execute the chosen path. Not used by --dry-run."""
    path = decision["path"]["path"]
    prices = decision["prices"]
    pick = decision["pick"]
    if path == "none":
        return []
    if path == "rsi":
        results = []
        if pick["case"] in (1, 3) and pick.get("sell_coin") and not pick.get("sell_skip"):
            sold = pick["sell_coin"]
            results.append(execute_leg(sold, "USDC", pick["sell_qty"], prices, "rsi_rebalance", False))
            if pick["case"] == 1 and results[-1].get("sent") and not pick.get("buy_skip"):
                proceeds = float(results[-1]["buy_amt"])
                spend = min(proceeds, float(pick.get("buy_slice_usd") or 0))
                dust = float((decision.get("rsi_cfg") or {}).get("dust_usd", 1) or 1)
                if spend >= dust:
                    results.append(execute_leg("USDC", pick["buy_coin"], spend, prices, "rsi_rebalance", False))
        elif pick["case"] == 2 and not pick.get("buy_skip"):
            results.append(execute_leg("USDC", pick["buy_coin"], pick["buy_qty"], prices, "rsi_rebalance", False))
        return results
    results = []
    qty = dict(decision.get("balances") or {})
    for move in decision["path"].get("moves") or []:
        px = 1.0 if move["sold"] == "USDC" else float(prices.get(move["sold"]) or 0)
        if px <= 0:
            continue
        sell_amt = float(move["usd"]) / px
        have = float(qty.get(move["sold"]) or 0)
        if move["sold"] == "SOL":
            sell_amt = min(sell_amt, max(0.0, have - SOL_FEE_BUFFER))
        else:
            sell_amt = min(sell_amt, have)
        if sell_amt * px < 1.0:
            continue
        row = execute_leg(move["sold"], move["bought"], sell_amt, prices, "band_rebalance", False)
        results.append(row)
        if row.get("sent"):
            qty[move["sold"]] = have - sell_amt
    return results



# ---------------------------------------------------------------------------
# Over-75% trim (friday_trim75, owner 2026-10-10). Runs on the live Friday
# run after the RSI decision/trades. Non-USDC on both sides. Losing fills are
# allowed (no edge %, no dust check); the least-bad candidate is chosen.
# ---------------------------------------------------------------------------
TRIM_KIND = "friday_trim75"
TRIM_DEFAULTS = {"enabled": True, "threshold_pct": 75.0, "target_pct": 74.9,
                 "require_above_vwap": True, "max_trade_usd": 20.0,
                 "max_sell_fraction_of_balance": 0.4, "min_sol_fee_buffer": 0.02,
                 "min_trade_usd": 1.0}
TRIM_ROOT = _DH.HOME_STR


def trim_cfg(cb):
    out = dict(TRIM_DEFAULTS)
    out.update((cb or {}).get("friday_trim75") or {})
    return out


def _px(prices, sym):
    return 1.0 if sym == "USDC" else float(prices.get(sym) or 0)


def plan_trim75(cfg, bals, prices, vwap):
    """Pure. Returns {"trim": bool, "reason", ...sizing}. Fail closed on VWAP."""
    vals = {s: float(bals.get(s) or 0) * _px(prices, s) for s in BOOK}
    total = sum(vals.values())
    out = {"trim": False, "total_usd": round(total, 4),
           "pct": {s: round(vals[s] / total * 100, 4) if total else 0 for s in BOOK}}
    if not cfg.get("enabled"):
        out["reason"] = "disabled"; return out
    if total <= 0:
        out["reason"] = "empty_book"; return out
    over = [s for s in COINS if vals[s] / total * 100 > float(cfg["threshold_pct"])]
    if not over:
        out["reason"] = "none_over_threshold"; return out
    coin = over[0]
    out["coin"] = coin
    if cfg.get("require_above_vwap", True):
        q = (vwap or {}).get(coin) if isinstance(vwap, dict) else None
        if not q or q.get("last") is None or q.get("vwap") is None:
            out["reason"] = "vwap_unavailable"; return out
        out["vwap"] = {"last": q["last"], "vwap": q["vwap"]}
        if not float(q["last"]) > float(q["vwap"]):
            out["reason"] = "not_above_vwap"; return out
    px = _px(prices, coin)
    need_usd = vals[coin] - float(cfg["target_pct"]) / 100.0 * total
    bal = float(bals.get(coin) or 0)
    cap_qty = bal * float(cfg["max_sell_fraction_of_balance"])
    if coin == "SOL":
        cap_qty = min(cap_qty, max(0.0, bal - float(cfg["min_sol_fee_buffer"])))
    qty = min(need_usd / px, cap_qty, float(cfg["max_trade_usd"]) / px)
    sell_usd = qty * px
    out.update({"need_usd": round(need_usd, 4), "sell_qty": qty, "sell_usd": round(sell_usd, 4),
                "remainder_usd": round(max(0.0, need_usd - sell_usd), 4),
                "candidates": [c for c in COINS if c != coin]})
    if sell_usd < float(cfg["min_trade_usd"]):
        out["reason"] = "below_min_size"; return out
    out["trim"] = True
    out["reason"] = "over_threshold_above_vwap"
    return out


def _row_net_usd(row, sold, bought, sell_qty, prices):
    out = int(row["out_amount"]) / (10 ** DEC[bought])
    gas_usd = float(row.get("gas_sol") or 0) * float(prices.get("SOL") or 0)
    return out * _px(prices, bought) - sell_qty * _px(prices, sold) - gas_usd


def choose_trim_route(sold, candidates, sell_qty, prices, quoter=None):
    """Quote both venues for each candidate; return best fee-inclusive USD net (least bad)."""
    import venue_quotes as VQ
    quoter = quoter or VQ.fetch_venue_quotes
    amount = _to_base(sell_qty, sold)
    scored, seen = [], []
    for bought in candidates:
        if bought == "USDC" or sold == "USDC":
            continue
        for row in quoter(sold, bought, amount) or []:
            if not row.get("ok") or str(row.get("venue")) not in ("jupiter_manual", "raydium"):
                continue
            if VQ.is_ultra_labeled(row) or row.get("platform_fee_bps") not in (None, 0):
                continue
            net = _row_net_usd(row, sold, bought, sell_qty, prices)
            seen.append({"bought": bought, "venue": row.get("venue"), "net_usd": round(net, 4)})
            scored.append((net, 1 if row.get("venue") == "jupiter_manual" else 0, bought, row))
    if not scored:
        return None, seen
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    net, _r, bought, row = scored[0]
    return {"bought": bought, "row": row, "net_usd": net}, seen


def _build_row(row, sold, bought):
    import venue_quotes as VQ
    if row["venue"] == "jupiter_manual":
        tx, err = VQ.build_jupiter_manual_tx(row.get("payload"))
        return ([tx] if tx else []), err
    txs, err = VQ.build_raydium_tx(row.get("swap_response"), sold, bought)
    return list(txs or []), err


def log_trim_fill(res, prices, bals_after=None):
    """trades.jsonl + trades.csv + sol-usdc-log.md (signature only in the trade files)."""
    import csv, os
    sig = res["signature"]
    url = f"https://solscan.io/tx/{sig}"
    b = bals_after or {}
    total_after = round(sum(float(b.get(s) or 0) * _px(prices, s) for s in BOOK), 4) if b else None
    note = (f"FRIDAY_TRIM75 {res['sold']} was {res['pct_before']:.2f}% of book; {res['venue']} local signer wallet; "
            f"net_usd {res['net_usd']:.4f}; remainder_usd {res['remainder_usd']}")
    row = {"time_pt": res["when_pt"], "side": f"{res['sold']}\u2192{res['bought']}", "sold_asset": res["sold"],
           "sold_amount": res["sell_amt"], "bought_asset": res["bought"], "bought_amount": res["buy_amt"],
           "rate": res["fill_rate"], "rate_unit": res.get("rate_unit"), "tx": sig, "tx_url": url,
           "sol_after": b.get("SOL"), "usdc_after": b.get("USDC"), "cbbtc_after": b.get("cbBTC"),
           "weth_after": b.get("WETH"), "total_usd_after": total_after, "notes": note,
           "kind": TRIM_KIND, "counts_as_edge": False}
    with open(os.path.join(TRIM_ROOT, "trades.jsonl"), "a", encoding="utf-8", newline="") as f:
        f.write(json.dumps(row) + "\n")
    with open(_DH.trades_csv_path(), "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([row["time_pt"], row["side"], row["sold_asset"], row["sold_amount"],
                                row["bought_asset"], row["bought_amount"], row["rate"], row["rate_unit"],
                                sig, url, row["sol_after"], row["usdc_after"], row["cbbtc_after"],
                                row["weth_after"], total_after, note])
    with open(os.path.join(TRIM_ROOT, "sol-usdc-log.md"), "a", encoding="utf-8", newline="") as f:
        f.write(f"\n## {res['when_pt']} Friday over-75% trim — {TRIM_KIND}\n"
                f"- {res['sell_amt']} {res['sold']} -> {res['buy_amt']} {res['bought']} via {res['venue']} "
                f"(sig omitted per fill-report rule)\n- {note}\n")


def run_trim75(prices, dry_run, bals=None, vwap=None, cb=None, quoter=None, sender=None, recorder=None, logger=None):
    """Plan, quote both candidates/venues, and (live only) send. Returns a report dict."""
    if cb is None:
        with open(CB_PATH) as f:
            cb = json.load(f)
    cfg = trim_cfg(cb)
    if bals is None:
        import savings_mode
        bals = savings_mode.trading_balances(cb, fetch_main_wallet_balances())
    if vwap is None and cfg.get("enabled"):
        import market_gates as MG
        _f, vcfg = MG.cfgs(cb)
        vwap = MG.fetch_vwap(vcfg)
    plan = plan_trim75(cfg, bals, prices, vwap)
    rep = {"plan": plan, "sent": False}
    if not plan["trim"]:
        return rep
    sold, qty = plan["coin"], plan["sell_qty"]
    pick, seen = choose_trim_route(sold, plan["candidates"], qty, prices, quoter)
    rep["quotes"] = seen
    if not pick:
        rep["error"] = "no_quote"; return rep
    rep["chosen"] = {"bought": pick["bought"], "venue": pick["row"].get("venue"), "net_usd": round(pick["net_usd"], 4)}
    if dry_run:
        rep["error"] = "dry_run_no_send"; return rep
    bought = pick["bought"]
    started = time.monotonic()
    txs, err = _build_row(pick["row"], sold, bought)
    if err or not txs:
        rep["error"] = err or "no_tx"; return rep
    holder = {"pick": pick}

    def _rebuild():
        p2, _s = choose_trim_route(sold, [bought], qty, prices, quoter)
        if not p2:
            return None
        built, e2 = _build_row(p2["row"], sold, bought)
        if e2 or not built:
            return None
        holder["pick"] = p2
        return built

    if time.monotonic() - started > QUOTE_STALE_SEC:
        txs = _rebuild()
        if not txs:
            rep["error"] = "stale_requote_failed"; return rep
    if sender is None:
        import savings_mode
        kp = savings_mode._load_keypair()
        sig, err_s, uncertain = savings_mode.send_built_txs(txs, kp, _rebuild, accept_unconfirmed=False)
    else:
        sig, err_s, uncertain = sender(txs, _rebuild)
    if err_s or not sig:
        rep.update({"error": err_s or "send_failed", "submitted": bool(uncertain)}); return rep
    pick = holder["pick"]
    pair = _pair_of(sold, bought)
    buy_amt = int(pick["row"]["out_amount"]) / (10 ** DEC[bought])
    base, quote = PAIR_BASE_QUOTE[pair]
    amts = {sold: float(qty), bought: float(buy_amt)}
    fill_rate = amts[quote] / amts[base]
    when = datetime.now(PT).replace(microsecond=0).isoformat()
    (recorder or record_rebalance_fill)(pair, sold, bought, qty, buy_amt, fill_rate, sig, when, TRIM_KIND)
    res = {"signature": sig, "pair": pair, "sold": sold, "bought": bought, "sell_amt": qty, "buy_amt": buy_amt,
           "fill_rate": fill_rate, "rate_unit": f"{quote}/{base}", "venue": pick["row"].get("venue"),
           "net_usd": pick["net_usd"], "when_pt": when, "pct_before": plan["pct"][sold],
           "remainder_usd": plan["remainder_usd"]}
    try:
        after = None
        if logger is None:
            import savings_mode
            import after_bals as _AB  # after1012: read after the fill confirms, else computed
            _pre = {s: float((bals or {}).get(s) or 0) for s in BOOK} if isinstance(bals, dict) else None
            _raw, _src = _AB.read_after(ADDR, _pre, sold, bought, qty, buy_amt,
                                        fetch=lambda a: savings_mode.trading_balances(cb, fetch_main_wallet_balances()))
            after = _raw
            res["after_source"] = _src
        (logger or log_trim_fill)(res, prices, after)
    except Exception as e:
        rep["log_error"] = type(e).__name__
    rep.update({"sent": True, "fill": {k: v for k, v in res.items() if k != "signature"}})
    return rep


def _main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    dry = "--dry-run" in argv
    try:
        decision = build_decision()
    except Exception as e:
        print(f"FRIDAY_RSI candle_or_balance_error {type(e).__name__}: {e}", flush=True)
        print("NO_TX", flush=True)
        return 1
    if dry:
        print(format_report(decision, decision.get("candle_source"), decision.get("errors")), flush=True)
        print(f"price_source {decision.get('price_source')}", flush=True)
        print(f"balances {decision.get('balances')}", flush=True)
        print(f"closes {decision.get('closes_used')}", flush=True)
        try:
            _t = run_trim75(decision["prices"], True, bals=decision.get("balances"))
            print("TRIM75 " + json.dumps(_t, default=str), flush=True)
        except Exception as e:
            print(f"TRIM75 error {type(e).__name__}: {e}", flush=True)
        return 0
    # Live path is for the Friday routine.
    results = run_live(decision)
    print("FRIDAY_RSI live", flush=True)
    _rsi = (decision.get("pick") or {}).get("rsi") or {}
    print(
        f"rsi SOL={float(_rsi.get('SOL') or 0):.4f} cbBTC={float(_rsi.get('cbBTC') or 0):.4f} WETH={float(_rsi.get('WETH') or 0):.4f}",
        flush=True,
    )
    print(
        f"case {decision['pick'].get('case')} path {decision['path'].get('path')} reason {decision['path'].get('reason')}",
        flush=True,
    )
    if not results:
        print("fills none", flush=True)
    for row in results:
        print("fill " + json.dumps({k: v for k, v in row.items() if k != "signature"}, default=str), flush=True)
    # Over-75% trim after the RSI decision/trades (fresh balances if RSI traded).
    try:
        _bals = None if any(r.get("sent") for r in results) else decision.get("balances")
        _t = run_trim75(decision["prices"], False, bals=_bals)
        print("TRIM75 " + json.dumps(_t, default=str), flush=True)
    except Exception as e:
        print(f"TRIM75 error {type(e).__name__}: {e}", flush=True)
    # rent1014: after the trades, close empty token accounts to claim back their rent (logged, no alert)
    try:
        reclaim_rent()
    except Exception as e:
        print(f"RENT_RECLAIM error {type(e).__name__}", flush=True)
    return 0


def rent_keep_mints(cb):
    """Mints never closed: the book coins trading uses every run (incl. wrapped SOL) and savings/memecoin coins."""
    import savings_mode, memecoin_mode
    keep = set(MINTS.values())
    keep |= {savings_mode.mint_of(cb), memecoin_mode.mint_of(cb), "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn"}
    for k in ("savings_mode", "memecoin_mode"):
        for ex in ((cb.get(k) or {}).get("extra_buys") or []):
            if isinstance(ex, dict) and ex.get("mint"):
                keep.add(ex["mint"])
    return {m for m in keep if m}


def reclaim_rent(dry_run=False, rpc=None, send_fn=None, cb_path=None, log_path=None):
    """Close empty token accounts (not rent_keep_mints) and log the SOL recovered.

    Recovered rent shifts the deposit-watch baseline, so it is never counted as a deposit.
    """
    import token_rent as TR, savings_mode, venue_quotes as VQ
    cb_path = cb_path or CB_PATH
    cb = json.load(open(cb_path, encoding="utf-8"))
    if send_fn is None:
        kp = savings_mode._load_keypair()
        send_fn = lambda txs: savings_mode.send_built_txs(txs, kp, None)
    res = TR.close_empty(rpc or _rpc, VQ.ADDR, send_fn, keep_mints=rent_keep_mints(cb), dry_run=dry_run)
    if res["sol_recovered"] > 0 and not dry_run:
        cb = json.load(open(cb_path, encoding="utf-8"))  # fresh read: other writers ran before us
        if TR.note_recovered(cb, res["sol_recovered"]):
            tmp = cb_path + ".rent.tmp"
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                json.dump(cb, f, indent=2)
            _os.replace(tmp, cb_path)
        line = (f"- RENT_RECLAIM {datetime.now(PT).replace(microsecond=0).isoformat()} closed {len(res['closed'])} empty "
                f"token account(s), recovered {res['sol_recovered']:.9f} SOL (rent, not a deposit) "
                + " ".join("tx " + s for s in res["signatures"]))
        with open(log_path or (_DH.HOME_STR + "/sol-usdc-log.md"), "a", encoding="utf-8", newline="") as f:
            f.write(line + "\n")
        print(line, flush=True)
    return res


def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        rc = _main(argv)
    finally:
        # deskjson 2026-10-11: Friday log trim removed; size valve in box_runner.
        pass
    return rc


if __name__ == "__main__":
    if _DH.MIRROR_FLAG.exists() and "--dry-run" not in _sys.argv[1:]:
        # Box mirror guard (pcmove 2026-10-10): live trading runs on the owner's PC only.
        print("MIRROR_ONLY present: this copy is a mirror and must not trade; refusing live run (live bot runs on the PC)", flush=True)
        raise SystemExit(3)
    import jobs_lock as _JL  # bugfix1012: edge check skips while this job runs
    _jl = None if "--dry-run" in _sys.argv[1:] else _JL.acquire("friday_rsi")
    try:
        _rc = main()
    finally:
        if _jl:
            _JL.release(_jl)
    raise SystemExit(_rc)
