"""Savings mode for wallet.

JitoSOL bought here is held savings. It is not an edge-inventory token.
It is not sold on SOL/USDC/cbBTC/WETH routes, and a savings buy does not
change the same-pair streak or pair multipliers.

The buy latch is combined portfolio USD (usd_high_water). One $4 JitoSOL buy
happens only when that total is another $10 above the latch, and the latch
then rises by $10. Memecoin mode keeps its own portfolio USD latch.

A deposit of SOL, USDC, cbBTC, or WETH that is not a swap fill raises both
latches by the deposit's USD value at arrival, before the buy test, and adds
that USD to funding.funded_usd (net capital for wallet P&L reporting). The
deposit is not portfolio gain and does not buy JitoSOL. An external withdrawal
(outflow with no swap-leg increase) subtracts from funding.funded_usd (floor 0)
and does not move latches. A swap between existing holdings does not move
either latch or funded_usd except that +$10 after a real high-water buy.

Edge checks call edge_check_hook. Morning balance calls morning_balance_hook.
Both apply deposits/withdrawals before any quote or swap. Enabled false
returns before any quote or swap.

Extra savings buys (2026-10-05, owner): on each $10 latch step, in addition
to the $4 JitoSOL buy, every savings_mode.extra_buys entry (cbBTC, $1) is
bought and held. Saved cbBTC is tracked in savings_mode.savings_cbbtc_held.
trading_balances() subtracts it from the wallet cbBTC so every trading path
(edge sizing, caps, reverses, holdings bias, concentration, pair tilt, fade,
Friday RSI/bands, morning underweight) only sees trading cbBTC. The saved
amount is carried in the "cbBTC_savings" key so combined_portfolio_usd still
counts it in the savings latch total and deposit detection still sees the
whole wallet. Extra buys are funded from the heaviest holding that is not the
bought token and not JitoSOL (SOL/USDC/WETH for cbBTC), 0.02 SOL buffer. The
latch advances one step when at least one of the step's buys lands; a failed
extra buy is recorded and alerted (TRADE_FAIL) and not retried.

This module does not print keys. Importing it does not trade.
"""
import degen_home as _DH  # portable base dir (pcmove 2026-10-10)
import base64
import json
import os

import venue_quotes as VQ

EDGE_INVENTORY = ("SOL", "USDC", "cbBTC", "WETH")
# Rank, size, 5% floor, concentration cap, and Friday band weights use only these.
BOOK_ASSETS = EDGE_INVENTORY
DECIMALS = {"SOL": 9, "USDC": 6, "cbBTC": 8, "WETH": 8, "JitoSOL": 9}
VENUE_RANK = {"jupiter_manual": 2, "raydium": 1}
CB_PATH = _DH.HOME_STR + "/cost-basis.json"
MIN_SOL_BUFFER_DEFAULT = 0.02
# A SOL drop at or under this is a network fee, not a swap leg or a withdrawal.
SOL_FEE_QTY_MAX = 0.002
# A book-asset decrease worth at least this (USD) is a swap leg or an outflow.
SWAP_LEG_MIN_USD = 0.50
# bugfix1012: an external deposit/withdrawal must show on 2 separate runs (checks) before it counts.
RUN_ID = os.getpid()
QTY_EPS = {"SOL": 5e-9, "USDC": 5e-7, "cbBTC": 5e-10, "WETH": 5e-10}
SAVINGS_KEY_SUFFIX = "_savings"
# Max failure records kept in savings_mode.extra_buy_failures.
EXTRA_FAIL_KEEP = 25
LOG_ROOT = _DH.HOME_STR
DEPOSIT_RULE = (
    "depfix 2026-10-14: once per check, at the start, compare live SOL/USDC/cbBTC/WETH "
    "with deposit_watch.balances minus every swap logged in trades.jsonl since deposit_watch.log_lines. "
    "Any unexplained increase is a deposit (even if another coin fell); it raises both latches and "
    "funding.funded_usd. Any unexplained decrease is a withdrawal, except SOL fee dust and drops covered "
    "by a rise in the bot's own JitoSOL/Greedy Goats. A flow is confirmed on a second read; while pending, "
    "and in the check a deposit lands, savings and memecoin buys are blocked."
)



def _now_pt():
    from datetime import datetime
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def _book_px(prices, name):
    px = float((prices or {}).get(name) or 0)
    if px <= 0 and name == "USDC":
        return 1.0
    return px


def _funding(cb):
    """Return (and create if missing) the funding dict on the cost-basis book."""
    if not isinstance(cb, dict):
        return {}
    funding = cb.get("funding")
    if not isinstance(funding, dict):
        funding = {
            "funded_usd": 0.0,
            "updated_pt": _now_pt(),
            "rule": (
                "Cumulative net USD deposited into wallet (external inflows minus "
                "outflows of SOL/USDC/cbBTC/WETH; not swaps). Wallet performance "
                "reporting uses live portfolio USD vs this funded_usd."
            ),
            "history": [],
        }
        cb["funding"] = funding
    funding.setdefault("history", [])
    if funding.get("funded_usd") is None:
        funding["funded_usd"] = 0.0
    return funding


def funded_usd(cb):
    """Net wallet capital for wallet P&L display. 0 when missing."""
    if not isinstance(cb, dict):
        return 0.0
    funding = cb.get("funding")
    if not isinstance(funding, dict):
        return 0.0
    try:
        return float(funding.get("funded_usd") or 0)
    except (TypeError, ValueError):
        return 0.0


def legacy_funded_usd(cb):
    """Stranded legacy wallet deposit baseline when known; None if omitted."""
    if not isinstance(cb, dict):
        return None
    funding = cb.get("funding")
    if not isinstance(funding, dict):
        return None
    if "legacy_funded_usd" not in funding:
        return None
    try:
        v = float(funding.get("legacy_funded_usd") or 0)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def pnl_vs_funded_pct(live_usd, funded):
    """(live - funded) / funded * 100, or None when funded is not positive."""
    try:
        live = float(live_usd or 0)
        base = float(funded or 0)
    except (TypeError, ValueError):
        return None
    if base <= 0:
        return None
    return (live - base) / base * 100.0


def _apply_funding_delta(cb, usd, kind, assets, note=None):
    """Add (deposit) or subtract (withdrawal) USD from funding.funded_usd.

    Withdrawals floor at 0. Appends funding.history. Idempotent with the
    deposit_watch detection that calls this once per flow.
    """
    if not isinstance(cb, dict):
        return 0.0
    try:
        delta = float(usd or 0)
    except (TypeError, ValueError):
        return funded_usd(cb)
    if abs(delta) < 1e-12:
        return funded_usd(cb)
    funding = _funding(cb)
    prev = float(funding.get("funded_usd") or 0)
    if kind == "withdrawal":
        new = max(0.0, prev - abs(delta))
        hist_usd = -abs(delta)
    else:
        new = prev + abs(delta)
        hist_usd = abs(delta)
    funding["funded_usd"] = new
    funding["updated_pt"] = _now_pt()
    entry = {
        "pt": funding["updated_pt"],
        "usd": hist_usd,
        "kind": kind,
        "assets": assets or {},
        "funded_usd_after": new,
    }
    if note:
        entry["note"] = note
    hist = funding.get("history")
    if not isinstance(hist, list):
        hist = []
        funding["history"] = hist
    hist.append(entry)
    return new


def _deposit_result(reason, deposit_usd, deposits, dirty, cb, withdrawal_usd=0.0, withdrawals=None):
    sav = _cfg(cb)
    mem = cb.get("memecoin_mode") if isinstance(cb, dict) else None
    mem = mem if isinstance(mem, dict) else {}
    return {
        "reason": reason,
        "deposit_usd": float(deposit_usd or 0),
        "deposits": deposits or {},
        "withdrawal_usd": float(withdrawal_usd or 0),
        "withdrawals": withdrawals or {},
        "dirty": bool(dirty),
        "funded_usd": funded_usd(cb) if isinstance(cb, dict) else None,
        "savings_usd_high_water": sav.get("usd_high_water"),
        "memecoin_usd_high_water": mem.get("usd_high_water"),
    }


def _raise_both_latches(cb, deposit_usd):
    """Add deposit USD to each latch that already exists. Does not create one."""
    if deposit_usd <= 0:
        return
    for key in ("savings_mode", "memecoin_mode"):
        cfg = cb.get(key)
        if isinstance(cfg, dict) and cfg.get("usd_high_water") is not None:
            cfg["usd_high_water"] = float(cfg.get("usd_high_water") or 0) + float(deposit_usd)


def _trade_log_path():
    return os.path.join(LOG_ROOT, "trades.jsonl")


def trade_log_count(path=None):
    """Number of lines in trades.jsonl (anchor for deposit detection)."""
    try:
        with open(path or _trade_log_path(), "rb") as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


def logged_flows_since(n, path=None):
    """Expected book-asset deltas from trades.jsonl rows after line n.

    Every logged swap (edge fill, rebalance, savings, memecoin) moves the
    sold asset down and the bought asset up. Non-book assets are ignored.
    """
    exp = {k: 0.0 for k in EDGE_INVENTORY}
    traded = {k: 0.0 for k in EDGE_INVENTORY}
    try:
        with open(path or _trade_log_path(), encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return exp, traded
    for ln in lines[int(n or 0):]:
        try:
            r = json.loads(ln)
        except Exception:
            continue
        so, bo = r.get("sold_asset"), r.get("bought_asset")
        try:
            sa, ba = float(r.get("sold_amount") or 0), float(r.get("bought_amount") or 0)
        except (TypeError, ValueError):
            continue
        if so in exp:
            exp[so] -= sa
            traded[so] += sa
        if bo in exp:
            exp[bo] += ba
            traded[bo] += ba
    return exp, traded


AUX_ASSETS = ("JitoSOL", "GreedyGoats")
_DETECT_CACHE = {}


def _rent_now(balances, aux):
    """Token-account rent (SOL) read this check, from aux["_rent_sol"] or balances["_rent_sol"]."""
    for src in (aux, balances):
        if isinstance(src, dict) and src.get("_rent_sol") is not None:
            try:
                return float(src["_rent_sol"])
            except (TypeError, ValueError):
                return None
    return None


def _aux_now(balances, aux):
    out = {}
    src = aux if isinstance(aux, dict) else (balances or {})
    for k in AUX_ASSETS:
        v = src.get(k)
        if v is not None:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
    return out


def apply_external_deposits(cb, balances, prices, aux=None, log_path=None):
    """Detect external deposits/withdrawals (depfix 2026-10-14).

    Runs once per check (cached by RUN_ID); the edge check calls it at the
    start, before tokens_usd reconcile and before any buy test, so later
    hooks reuse the result.

    unexplained = live qty - deposit_watch.balances - (swaps logged in
    trades.jsonl since the baseline). An unexplained increase is a deposit
    even when another coin fell in the same window. An unexplained decrease
    is a withdrawal, except SOL fee dust and any book drop covered by a rise
    in the bot's own JitoSOL/Greedy Goats (late-landing own buys are never
    booked as withdrawals). A flow is confirmed on a second read before it
    moves funded_usd/latches; while it is pending, savings/memecoin buys are
    blocked (see buy_block_reason). Does not quote, sign, or send.
    """
    if not isinstance(cb, dict):
        return _deposit_result("no_book", 0.0, {}, False, {})
    cached = _DETECT_CACHE.get(id(cb))
    if cached and cached[0] == RUN_ID and cached[2] is cb.get("deposit_watch"):
        res = dict(cached[1])
        res["cached"] = True
        return res
    res = _detect_flows(cb, balances, prices, aux, log_path)
    _DETECT_CACHE[id(cb)] = (RUN_ID, dict(res), cb.get("deposit_watch"))
    return res


def buy_block_reason(cb, dep):
    """Savings/memecoin buys must not run in a check that saw a deposit or a pending flow."""
    if not isinstance(dep, dict):
        return None
    if dep.get("reason") in ("external_pending_confirm", "deposit_unpriced", "withdrawal_unpriced"):
        return "deposit_check_pending"
    if float(dep.get("deposit_usd") or 0) > 0:
        return "deposit_this_check"
    w = (cb or {}).get("deposit_watch") or {}
    if isinstance(w.get("pending_external"), dict):
        return "deposit_check_pending"
    return None


def pause_mode_uncertain(cfg, reason, mode):
    """After one failed/uncertain send, pause the mode until the owner turns it back on.

    The next check's deposit detection sees any late landing (own JitoSOL/Goats
    rise) so nothing is retried blind.
    """
    if not isinstance(cfg, dict):
        return
    cfg["enabled"] = False
    cfg["paused_reason"] = f"auto-paused after uncertain/failed send: {reason}"
    cfg["paused_pt"] = _now_pt()
    print(f"MODE_PAUSED {mode} {reason}", flush=True)


def _detect_flows(cb, balances, prices, aux, log_path):
    watch = cb.get("deposit_watch")
    if not isinstance(watch, dict):
        watch = {}
        cb["deposit_watch"] = watch
    fee_cap = float(watch.get("sol_fee_qty_max") or SOL_FEE_QTY_MAX)
    leg_min = float(watch.get("swap_leg_min_usd") or SWAP_LEG_MIN_USD)
    watch.setdefault("assets", list(EDGE_INVENTORY))
    watch.setdefault("sol_fee_qty_max", fee_cap)
    watch.setdefault("swap_leg_min_usd", leg_min)
    watch["rule"] = DEPOSIT_RULE
    snap = watch.get("balances")
    curr = book_balances(wallet_balances(balances))
    n_now = trade_log_count(log_path)
    aux_now = _aux_now(balances, aux)
    # rent1014: SOL parked as rent in the wallet's token accounts (opening/closing an account moves
    # SOL between the wallet and its accounts; never a deposit or withdrawal). None = not read.
    rent_now = _rent_now(balances, aux)
    rent_prev = watch.get("rent_sol")
    rent_d = (rent_now - float(rent_prev)) if (rent_now is not None and rent_prev is not None) else 0.0
    if not isinstance(snap, dict) or any(name not in snap for name in EDGE_INVENTORY):
        watch["balances"] = curr
        watch["log_lines"] = n_now
        if aux_now:
            watch["aux_balances"] = aux_now
        if rent_now is not None:
            watch["rent_sol"] = rent_now
        watch["updated_pt"] = _now_pt()
        return _deposit_result("snapshot_initialized", 0.0, {}, True, cb)
    n0 = watch.get("log_lines")
    if n0 is None or int(n0) > n_now:
        n0 = n_now  # first run with the new anchor: treat log as already applied
    expected, traded = logged_flows_since(n0, log_path)
    # own non-book buys (JitoSOL / Goats) seen in the wallet
    aux_prev = watch.get("aux_balances") or {}
    own_aux_usd = 0.0
    for k, q in aux_now.items():
        dq = q - float(aux_prev.get(k) or 0) if k in aux_prev else 0.0
        if dq > 0:
            px = float((prices or {}).get(k) or 0)
            if px <= 0 and k == "JitoSOL":
                px = jitosol_px(prices) or _book_px(prices, "SOL") * 1.3
            own_aux_usd += dq * px
    logged_aux_usd = 0.0  # logged non-book buys already in `expected` via sold leg
    deposits, withdrawals = {}, {}
    inc, dec = {}, {}
    for name in EDGE_INVENTORY:
        px = _book_px(prices, name)
        delta = float(curr.get(name) or 0) - float(snap.get(name) or 0)
        if name == "SOL":
            delta += rent_d  # rent moved into accounts left the wallet SOL but is still the owner's
        unexp = delta - expected.get(name, 0.0)
        tol = max(QTY_EPS[name] * 10, (leg_min / px) if px > 0 else 0.0, 0.03 * traded.get(name, 0.0))
        if name == "SOL":
            tol = max(tol, fee_cap)
        if unexp > tol:
            inc[name] = unexp
        elif unexp < -tol:
            dec[name] = -unexp
    # book drops paid for the bot's own JitoSOL/Goats (logged or not) are not withdrawals
    if dec and own_aux_usd > 0:
        budget = own_aux_usd * 1.25 + 0.5
        for name in ("SOL", "USDC", "WETH", "cbBTC"):
            if name in dec and budget > 0:
                px = _book_px(prices, name)
                usd = dec[name] * px
                take = min(usd, budget)
                budget -= take
                rest = usd - take
                if rest * 1.0 <= leg_min:
                    dec.pop(name)
                else:
                    dec[name] = rest / px
    # An unexplained rise matched by an unexplained fall of about the same USD is an
    # unlogged swap between book coins (e.g. a job that logs after the hook), not a flow.
    if inc and dec:
        iu = sum(q * _book_px(prices, n) for n, q in inc.items())
        du = sum(q * _book_px(prices, n) for n, q in dec.items())
        if abs(iu - du) <= max(1.0, 0.05 * max(iu, du)):
            inc, dec = {}, {}
    external = bool(inc or dec)
    if external:
        pend = watch.get("pending_external")
        # Confirm only when balances AND the trade log are unchanged since the
        # first read: a swap row appended in between re-explains the change.
        same = isinstance(pend, dict) and int(pend.get("log_lines", n_now)) == n_now and all(
            abs(float(pend.get("balances", {}).get(n) or 0) - float(curr.get(n) or 0)) <= QTY_EPS[n] * 10
            for n in EDGE_INVENTORY)
        if not same or pend.get("run") == RUN_ID:
            if not same:
                watch["pending_external"] = {"balances": dict(curr), "run": RUN_ID, "pt": _now_pt(), "log_lines": n_now}
            return _deposit_result("external_pending_confirm", 0.0, {}, True, cb)
        watch.pop("pending_external", None)
    elif "pending_external" in watch:
        watch.pop("pending_external", None)
    deposit_usd = withdrawal_usd = 0.0
    for name, qty in inc.items():
        px = _book_px(prices, name)
        if px <= 0:
            return _deposit_result("deposit_unpriced", 0.0, {}, False, cb)
        deposits[name] = {"qty": qty, "price_usd": px, "usd": qty * px}
        deposit_usd += qty * px
    for name, qty in dec.items():
        px = _book_px(prices, name)
        if px <= 0:
            return _deposit_result("withdrawal_unpriced", 0.0, {}, False, cb)
        withdrawals[name] = {"qty": qty, "price_usd": px, "usd": qty * px}
        withdrawal_usd += qty * px
    reason = "unchanged"
    if deposit_usd > 0:
        _raise_both_latches(cb, deposit_usd)
        _apply_funding_delta(cb, deposit_usd, "deposit", deposits)
        watch["last_deposit"] = {"usd": deposit_usd, "assets": deposits, "at_pt": _now_pt(),
                                 "counts_as_gain": False, "triggered_buy": False}
        reason = "deposit"
    if withdrawal_usd > 0:
        _apply_funding_delta(cb, withdrawal_usd, "withdrawal", withdrawals)
        watch["last_withdrawal"] = {"usd": withdrawal_usd, "assets": withdrawals, "at_pt": _now_pt(),
                                    "latches_unchanged": True}
        reason = "deposit_and_outflow" if deposit_usd > 0 else "outflow"
    if reason == "unchanged" and any(abs(float(curr.get(n) or 0) - float(snap.get(n) or 0)) > QTY_EPS[n]
                                     for n in EDGE_INVENTORY):
        _only_sol = all(abs(float(curr.get(n) or 0) - float(snap.get(n) or 0)) <= QTY_EPS[n]
                        for n in EDGE_INVENTORY if n != "SOL")
        _sd = float(curr.get("SOL") or 0) - float(snap.get("SOL") or 0)
        reason = "sol_fee" if (_only_sol and -fee_cap - 1e-12 <= _sd < 0 and n_now == n0) else "swap_fill"
    if own_aux_usd > 0:
        watch["last_own_aux_rise"] = {"usd": own_aux_usd, "at_pt": _now_pt(),
                                      "logged": n_now != n0}
    dirty = reason != "unchanged" or n_now != n0 or aux_now != aux_prev or abs(rent_d) > 1e-12 \
        or (rent_now is not None and rent_prev is None)
    if dirty:
        if rent_now is not None:
            watch["rent_sol"] = rent_now
        watch["balances"] = curr
        watch["log_lines"] = n_now
        if aux_now:
            watch["aux_balances"] = aux_now
        watch["updated_pt"] = _now_pt()
    return _deposit_result(reason, deposit_usd, deposits, dirty, cb,
                           withdrawal_usd=withdrawal_usd, withdrawals=withdrawals)


def _note_deposit(status, dep):
    """Record the deposit/withdrawal result. Does not overwrite a latch the hook just set."""
    if not isinstance(status, dict) or not isinstance(dep, dict):
        return status
    status["deposit_usd"] = float(dep.get("deposit_usd") or 0)
    status["deposit_reason"] = dep.get("reason")
    status["withdrawal_usd"] = float(dep.get("withdrawal_usd") or 0)
    if dep.get("deposits"):
        status["deposits"] = dep["deposits"]
    if dep.get("withdrawals"):
        status["withdrawals"] = dep["withdrawals"]
    if dep.get("funded_usd") is not None:
        status["funded_usd"] = dep["funded_usd"]
    if dep.get("dirty"):
        status["dirty"] = True
    return status


def _cfg(cb):
    raw = cb.get("savings_mode") if isinstance(cb, dict) else None
    return raw if isinstance(raw, dict) else {}


def enabled(cb):
    """True only for JSON boolean true. Missing or false skips."""
    return _cfg(cb).get("enabled") is True


def symbol_of(cb):
    return str(_cfg(cb).get("symbol") or "JitoSOL")


def mint_of(cb):
    return str(_cfg(cb).get("mint") or "")


def is_savings_asset(cb, sym):
    s = str(sym or "")
    if not s:
        return False
    if s == symbol_of(cb):
        return True
    mint = mint_of(cb)
    return bool(mint) and s == mint


def extra_buys(cb):
    """Enabled extra savings buys (e.g. cbBTC $1) made on each latch step."""
    out = []
    for item in _cfg(cb).get("extra_buys") or []:
        if not isinstance(item, dict) or item.get("enabled", True) is False:
            continue
        sym = str(item.get("symbol") or "")
        try:
            usd = float(item.get("buy_usd") or 0)
        except (TypeError, ValueError):
            usd = 0.0
        if not sym or usd <= 0:
            continue
        out.append(item)
    return out


def held_key(sym):
    return f"savings_{str(sym).lower()}_held"


def saved_qty(cb, sym):
    """Saved (held, not traded) quantity of a book token, e.g. cbBTC."""
    rec = _cfg(cb).get(held_key(sym))
    if not isinstance(rec, dict):
        return 0.0
    try:
        return max(0.0, float(rec.get("amount") or 0))
    except (TypeError, ValueError):
        return 0.0


def reserved_symbols(cb):
    """Book tokens with a savings reserve (configured extra buys or a held ledger)."""
    syms = []
    for item in extra_buys(cb):
        sym = str(item.get("symbol"))
        if sym in EDGE_INVENTORY and sym not in syms:
            syms.append(sym)
    for sym in EDGE_INVENTORY:
        if sym not in syms and saved_qty(cb, sym) > 0:
            syms.append(sym)
    return syms


def is_trading_view(balances):
    return any(str(k).endswith(SAVINGS_KEY_SUFFIX) for k in (balances or {}))


def trading_balances(cb, balances):
    """Wallet balances minus saved book tokens (cbBTC). Idempotent.

    The saved quantity moves to "<sym>_savings" (e.g. cbBTC_savings) so the
    combined savings total and deposit detection still see it. Every trading
    path must size from this view. A dict that already has a *_savings key is
    returned unchanged.
    """
    src = balances if isinstance(balances, dict) else {}
    if is_trading_view(src):
        return src
    out = dict(src)
    for sym in reserved_symbols(cb):
        have = float(out.get(sym) or 0)
        res = min(saved_qty(cb, sym), max(0.0, have))
        out[sym] = max(0.0, have - res)
        out[sym + SAVINGS_KEY_SUFFIX] = res
    return out


def wallet_balances(balances):
    """Undo trading_balances: add *_savings back onto the book token."""
    src = balances if isinstance(balances, dict) else {}
    if not is_trading_view(src):
        return src
    out = {}
    for k, v in src.items():
        if str(k).endswith(SAVINGS_KEY_SUFFIX):
            continue
        out[k] = v
    for k, v in src.items():
        if str(k).endswith(SAVINGS_KEY_SUFFIX):
            base = str(k)[: -len(SAVINGS_KEY_SUFFIX)]
            out[base] = float(out.get(base) or 0) + float(v or 0)
    return out


def savings_reserved_usd(balances, prices):
    """USD of saved book tokens carried in a trading view (0 for a raw wallet dict)."""
    tot = 0.0
    for k, v in (balances or {}).items():
        if str(k).endswith(SAVINGS_KEY_SUFFIX):
            base = str(k)[: -len(SAVINGS_KEY_SUFFIX)]
            tot += float(v or 0) * _book_px(prices, base)
    return tot


def savings_held_summary(cb, prices=None):
    """{sym: {amount, raw, usd}} for saved book tokens. Display only."""
    out = {}
    for sym in reserved_symbols(cb):
        rec = _cfg(cb).get(held_key(sym)) or {}
        amt = saved_qty(cb, sym)
        out[sym] = {
            "amount": amt,
            "raw": int(rec.get("raw") or 0),
            "usd": None if prices is None else amt * _book_px(prices, sym),
            "cost_usd": float(rec.get("cost_usd") or 0),
        }
    return out


def portfolio_gain_usd(cb, balances, prices, jitosol_mark_usd=0.0):
    """Dollar gain of the book.

    Inventory mark minus inventory USD cost, plus JitoSOL mark minus USD
    already spent buying savings. A savings swap is not a new gain or loss.
    JitoSOL is not inside tokens_usd and is not an edge inventory token.
    """
    balances = balances or {}
    prices = prices or {}
    mark = 0.0
    cost = 0.0
    tokens = (cb.get("tokens_usd") or {}) if isinstance(cb, dict) else {}
    for name in EDGE_INVENTORY:
        qty = float(balances.get(name) or 0)
        mark += qty * float(prices.get(name) or 0)
        cost += qty * float((tokens.get(name) or {}).get("avg_cost_usd") or 0)
    spent = float(_cfg(cb).get("savings_cost_usd") or 0)
    return mark + float(jitosol_mark_usd or 0) - cost - spent


def steps_due(gain_usd, credited_usd, step_usd):
    """Whole $step gains above the watermark. The watermark itself buys nothing."""
    step = float(step_usd)
    if step <= 0:
        return 0
    extra = float(gain_usd) - float(credited_usd)
    if extra < step:
        return 0
    return int(extra // step)


def book_balances(balances):
    """SOL, USDC, cbBTC, WETH only. JitoSOL and Greedy Goats are not in the book."""
    src = balances or {}
    return {k: float(src.get(k) or 0) for k in BOOK_ASSETS}


def book_usd_vals(balances, prices):
    """Live USD of the four book assets. Held JitoSOL and Greedy Goats are omitted."""
    px = prices or {}
    bals = book_balances(balances)
    return {k: bals[k] * float(px.get(k) or 0) for k in BOOK_ASSETS}


def book_usd_total(balances, prices):
    return sum(book_usd_vals(balances, prices).values())


JITOSOL_USD = None  # jitototal1014: set once per check by the bot (Jupiter JitoSOL price or its fallback)
JITOSOL_SOL_RATE = 1.30


def jitosol_px(prices):
    """JitoSOL USD: prices["JitoSOL"], else the check's JITOSOL_USD, else SOL x 1.30. Never plain SOL."""
    px = float((prices or {}).get("JitoSOL") or 0)
    if px <= 0:
        px = float(JITOSOL_USD or 0)
    if px <= 0:
        px = float((prices or {}).get("SOL") or 0) * JITOSOL_SOL_RATE
    return px


def jitosol_mark_usd(cb, balances, prices):
    """Mark of held JitoSOL. Wallet qty wins; else the savings ledger qty.
    Price is JitoSOL if known, otherwise SOL. Not an edge-book balance.
    """
    qty = float((balances or {}).get("JitoSOL") or 0)
    if qty <= 0:
        qty = float(_cfg(cb).get("jitosol_qty") or 0)
    return qty * jitosol_px(prices)


def goats_mark_usd(cb, balances, prices):
    """Greedy Goats mark. At cost when no price is known, so a hold is not a fake loss."""
    mc = cb.get("memecoin_mode") if isinstance(cb, dict) else None
    mc = mc if isinstance(mc, dict) else {}
    sym = str(mc.get("symbol") or "GreedyGoats")
    qty = float((balances or {}).get(sym) or 0)
    if qty <= 0:
        qty = float(mc.get("qty") or 0)
    px = float((prices or {}).get(sym) or 0)
    if px > 0:
        return qty * px
    if qty > 0:
        return float(mc.get("cost_usd") or 0)
    return 0.0


def combined_portfolio_usd(cb, balances, prices):
    """Book USD plus saved cbBTC, held JitoSOL, and Greedy Goats.

    This is the savings high-water total. It is not the edge rank, the 5%
    floor, the concentration cap, Friday band weights, or holdings bias.
    Same answer for a raw wallet dict and its trading_balances view.
    """
    return (
        book_usd_total(balances, prices)
        + savings_reserved_usd(balances, prices)
        + jitosol_mark_usd(cb, balances, prices)
        + goats_mark_usd(cb, balances, prices)
    )


def heaviest_funding(balances, prices, savings_symbol, exclude=()):
    """Heaviest SOL/USDC/cbBTC/WETH holding. Never the savings token or a memecoin.

    Pass a trading_balances view so saved cbBTC is never counted or sold.
    """
    best = None
    best_usd = -1.0
    for name in EDGE_INVENTORY:
        if name == savings_symbol or name in (exclude or ()):
            continue
        usd = float((balances or {}).get(name) or 0) * float((prices or {}).get(name) or 0)
        if usd > best_usd:
            best, best_usd = name, usd
    return best, best_usd


def sell_amt_for_usd(sold, balances, prices, usd, buffer):
    """Size a funding sale. SOL keeps the fee buffer. Never sells savings."""
    px = float((prices or {}).get(sold) or 0)
    bal = float((balances or {}).get(sold) or 0)
    if px <= 0 or bal <= 0 or usd <= 0:
        return 0.0
    amt = float(usd) / px
    if sold == "SOL":
        amt = min(amt, max(0.0, bal - float(buffer)))
    else:
        amt = min(amt, bal)
    return max(0.0, amt)


def extra_fund_candidates(cb, balances, prices, bought):
    """Funding order for an extra buy: heaviest first, never the bought token or JitoSOL."""
    tb = trading_balances(cb, balances)
    names = [n for n in EDGE_INVENTORY if n != bought and not is_savings_asset(cb, n)]
    vals = {n: float(tb.get(n) or 0) * _book_px(prices, n) for n in names}
    return sorted(names, key=lambda n: (-vals[n], EDGE_INVENTORY.index(n)))


def jitosol_leg_usd(cb):
    """USD for the JitoSOL leg of the next savings step (2026-10-14).

    jitosol_skip_steps: steps that buy only the extras (cbBTC) while the
    JitoSOL leg sits out (it was over-bought by the missed deposit).
    jitosol_next_mult: one-time size for the next JitoSOL buy (0.5 = half).
    Extras keep their normal size and schedule.
    """
    cfg = _cfg(cb)
    base = float(cfg.get("buy_usd") or 0)
    try:
        if int(cfg.get("jitosol_skip_steps") or 0) > 0:
            return 0.0
        m = float(cfg.get("jitosol_next_mult") or 1.0)
    except (TypeError, ValueError):
        m = 1.0
    return base * (m if 0 < m <= 1 else 1.0)


def _advance_jitosol_leg(cfg, primary_ok):
    skip = int(cfg.get("jitosol_skip_steps") or 0)
    if skip > 0:
        if skip - 1 > 0:
            cfg["jitosol_skip_steps"] = skip - 1
        else:
            cfg.pop("jitosol_skip_steps", None)
    elif primary_ok and "jitosol_next_mult" in cfg:
        cfg.pop("jitosol_next_mult", None)


def plan_extra(cb, item, balances, prices, buffer):
    """Plan one extra savings buy against a trading view. No quote."""
    sym = str(item.get("symbol"))
    usd = float(item.get("buy_usd") or 0)
    tb = trading_balances(cb, balances)
    cands = []
    for name in extra_fund_candidates(cb, tb, prices, sym):
        amt = sell_amt_for_usd(name, tb, prices, usd, buffer)
        px = _book_px(prices, name)
        ok = px > 0 and amt * px + 1e-6 >= usd
        cands.append({"symbol": name, "sell_amt": amt, "usd": float(tb.get(name) or 0) * px, "affordable": ok})
    first = next((c for c in cands if c["affordable"]), None)
    return {
        "symbol": sym,
        "mint": str(item.get("mint") or ""),
        "decimals": int(item.get("decimals") or DECIMALS.get(sym) or 9),
        "buy_usd": usd,
        "fund_candidates": cands,
        "fund_symbol": None if not first else first["symbol"],
        "fund_sell_amt": None if not first else first["sell_amt"],
        "affordable": bool(first),
        "action": "buy" if first else "skip",
        "reason": "high_water_step" if first else "funding_blocked_sol_buffer_or_balance",
        "purchased": False,
        "quoted": False,
    }


def _project_after(balances, sold, sell_amt, bought=None, buy_amt=0.0, gas_sol=0.0):
    """Local balance projection after a swap (trading view keys kept)."""
    out = dict(balances or {})
    if sold:
        out[sold] = max(0.0, float(out.get(sold) or 0) - float(sell_amt or 0))
    if bought and bought in out:
        out[bought] = float(out.get(bought) or 0) + float(buy_amt or 0)
    if gas_sol:
        out["SOL"] = max(0.0, float(out.get("SOL") or 0) - float(gas_sol or 0))
    return out


def _skip(reason, cfg=None):
    cfg = cfg or {}
    return {
        "enabled": False,
        "action": "skip",
        "reason": reason,
        "purchased": False,
        "quoted": False,
        "dirty": False,
        "counts_as_edge": False,
        "streak_changed": False,
        "multipliers_changed": False,
        "symbol": cfg.get("symbol") or "JitoSOL",
        "gains_credited_usd": cfg.get("gains_credited_usd"),
        "usd_high_water": cfg.get("usd_high_water"),
    }


def plan(cb, balances, prices, jitosol_mark_usd=0.0):
    """Decide only. No quote, no swap, no book writes.

    The latch is combined portfolio USD (usd_high_water), not the gain watermark.
    One $buy is due only when the total is at least gain_step_usd above that latch.
    The latch itself buys nothing. Enabled false never buys.
    jitosol_mark_usd is accepted for callers and ignored: the mark is computed
    from the ledger so a held JitoSOL balance stays inside the combined total.
    """
    del jitosol_mark_usd
    cfg = _cfg(cb)
    if cfg.get("enabled") is not True:
        return _skip("savings_mode_disabled", cfg)
    step = float(cfg.get("gain_step_usd") or 0)
    buy = jitosol_leg_usd(cb)
    # Saved cbBTC is never sold: size and rank funding from the trading view.
    balances = trading_balances(cb, balances)
    total = combined_portfolio_usd(cb, balances, prices)
    latch_raw = cfg.get("usd_high_water")
    sym = symbol_of(cb)
    sold, sold_usd = heaviest_funding(balances, prices, sym)
    buffer = float(cfg.get("min_sol_buffer") or MIN_SOL_BUFFER_DEFAULT)
    base = {
        "enabled": True,
        "purchased": False,
        "quoted": False,
        "dirty": False,
        "counts_as_edge": False,
        "streak_changed": False,
        "multipliers_changed": False,
        "symbol": sym,
        "mint": mint_of(cb),
        "held": cfg.get("held", True) is not False,
        "portfolio_usd": total,
        "usd_high_water": latch_raw,
        "gain_step_usd": step,
        "buy_usd": buy,
        "steps": 0,
        "fund_symbol": sold,
        "fund_usd": sold_usd,
        "gains_credited_usd": cfg.get("gains_credited_usd"),
    }
    if latch_raw is None:
        base["action"] = "skip"
        base["reason"] = "high_water_missing"
        return base
    latch = float(latch_raw)
    base["usd_high_water"] = latch
    # One buy. Exactly the step above the latch qualifies; not a stack of buys.
    has_extra = any(float(e.get("buy_usd") or 0) > 0 for e in extra_buys(cb))
    due = step > 0 and (buy > 0 or has_extra) and (total + 1e-6 >= latch + step)
    if not due:
        base["action"] = "skip"
        base["reason"] = "total_below_next_latch"
        return base
    base["steps"] = 1
    room = 0.0 if not sold else sell_amt_for_usd(sold, balances, prices, buy, buffer)
    px = float((prices or {}).get(sold) or 0) if sold else 0.0
    affordable = 1 if buy > 0 and px > 0 and room * px + 1e-6 >= buy else 0
    if buy <= 0:
        base["jitosol_leg_skipped"] = True
    base["steps_affordable"] = affordable
    base["fund_sell_amt"] = None if not sold else sell_amt_for_usd(sold, balances, prices, buy, buffer)
    base["primary_affordable"] = affordable >= 1
    # Extra buys are planned against balances after the primary JitoSOL sale.
    after = _project_after(balances, sold, base["fund_sell_amt"]) if affordable >= 1 else balances
    extras = [plan_extra(cb, item, after, prices, buffer) for item in extra_buys(cb)]
    base["extra_buys"] = extras
    base["buy_usd_total"] = (buy if affordable >= 1 else 0.0) + sum(
        float(e["buy_usd"]) for e in extras if e.get("affordable")
    )
    if affordable < 1 and not any(e.get("affordable") for e in extras):
        base["action"] = "skip"
        base["reason"] = "funding_blocked_sol_buffer_or_balance"
        return base
    base["action"] = "buy"
    base["reason"] = "high_water_step"
    return base


def _register_mint(cfg):
    sym = str(cfg.get("symbol") or "JitoSOL")
    mint = str(cfg.get("mint") or "")
    if not mint:
        raise RuntimeError("savings_mint_missing")
    # Lookup only. Does not add an edge route or a pair.
    VQ.MINTS[sym] = mint
    return sym


def net_out_ui(row, sell_usd, sol_usd, out_decimals):
    """Output tokens after network fee and Jito tip, same cost those swaps use."""
    if not row or not row.get("ok") or row.get("out_amount") is None:
        return None
    out = int(row["out_amount"]) / (10 ** int(out_decimals))
    if out <= 0 or sell_usd <= 0:
        return None
    gas = float(row.get("gas_sol") or 0)
    gas_out = 0.0
    if gas > 0 and sol_usd > 0:
        gas_out = gas * float(sol_usd) * out / float(sell_usd)
    return out - gas_out


def best_quote(cb, sold, sell_amt):
    """Jupiter manual (platform fee 0) and Raydium. Best net after fees and tip.

    Caller must already know enabled is true. This does not sign or send.
    """
    cfg = _cfg(cb)
    if cfg.get("enabled") is not True:
        return None, _skip("savings_mode_disabled", cfg)
    sym = _register_mint(cfg)
    dec_in = DECIMALS[sold]
    dec_out = int(cfg.get("decimals") or DECIMALS.get(sym) or 9)
    amount = int(round(float(sell_amt) * (10 ** dec_in)))
    if amount <= 0:
        return None, {"ok": False, "reason": "zero_amount"}
    rows = VQ.fetch_venue_quotes(sold, sym, amount)
    return rows, {"symbol": sym, "decimals": dec_out, "amount": amount}


def _register_extra_mint(sym, mint):
    if not mint:
        raise RuntimeError("extra_mint_missing")
    known = VQ.MINTS.get(sym)
    if known and known != mint:
        raise RuntimeError(f"extra_mint_mismatch:{sym}")
    VQ.MINTS[sym] = mint
    return sym


def quote_rows(sold, bought, sell_amt):
    """Jupiter manual (platformFeeBps=0) + Raydium rows. No sign/send."""
    amount = int(round(float(sell_amt) * (10 ** DECIMALS[sold])))
    if amount <= 0:
        return None, amount
    return VQ.fetch_venue_quotes(sold, bought, amount), amount


def pick_best_row(rows, sell_usd, sol_usd, out_decimals):
    scored = []
    for row in rows or []:
        net = net_out_ui(row, sell_usd, sol_usd, out_decimals)
        if net is None:
            continue
        scored.append((net, VENUE_RANK.get(row.get("venue"), 0), row))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    net, _rank, row = scored[0]
    return {"net_out_ui": net, "row": row}


def _load_signer_keypair():
    """signerfix1014: wallet key from env or degen_home (DPAPI on Windows). Accepts a JSON
    byte array or base58; strips BOM/whitespace. Never logs the key. Fails closed."""
    from solders.keypair import Keypair
    raw = os.environ.get("SOLANA_TRADING_PRIVATE_KEY") or _DH.get_secret("SOLANA_TRADING_PRIVATE_KEY") or ""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    raw = str(raw).replace("\ufeff", "").strip().strip('"').strip()
    if not raw:
        raise RuntimeError("signer_key_missing")
    try:
        if raw.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(raw)))
        return Keypair.from_base58_string(raw)
    except Exception:
        raise RuntimeError("signer_env_unexpected_format") from None


def _load_keypair():
    """Build the wallet signer. Does not print the key."""
    from solders.keypair import Keypair
    kp = _load_signer_keypair()  # signerfix1014: env, else DPAPI via degen_home; JSON or base58
    if str(kp.pubkey()) != VQ.ADDR:
        raise RuntimeError("signer_pubkey_mismatch")
    return kp


def _sign(tx_b64, kp):
    from solders.transaction import VersionedTransaction
    vtx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    msg = vtx.message
    pub = kp.pubkey()
    keys = list(msg.account_keys)
    if pub not in keys:
        raise RuntimeError("signer_not_in_tx")
    idx = keys.index(pub)
    nreq = int(msg.header.num_required_signatures)
    if nreq == 1:
        signed = VersionedTransaction(msg, [kp])
    else:
        sigs = list(vtx.signatures)
        sigs[idx] = kp.sign_message(bytes(msg))
        signed = VersionedTransaction.populate(msg, sigs)
    checks = signed.verify_with_results()
    if idx >= len(checks) or not checks[idx]:
        raise RuntimeError("local_sig_verify_failed")
    return base64.b64encode(bytes(signed)).decode()


def _send(b64):
    """Already-signed bytes stay on Helius. Prefer send_built_txs.

    Does not forward these bytes to Alchemy or public mainnet. Logs a
    JSON-RPC error body and sets maxRetries to 0.
    """
    import solana_rpc
    try:
        resp = solana_rpc.rpc_send(
            "sendTransaction",
            [b64, solana_rpc.send_config()],
            "helius",
        )
    except RuntimeError as exc:
        return None, solana_rpc.redact(str(exc))[:1500]
    if solana_rpc.is_already_processed(resp):
        sig = solana_rpc.signature_from_signed_b64(b64)
        if sig:
            return sig, None
        return None, solana_rpc.error_line("helius", "sendTransaction", resp)
    if not isinstance(resp, dict) or resp.get("error") or "result" not in resp:
        if isinstance(resp, dict) and resp.get("error"):
            return None, solana_rpc.error_line("helius", "sendTransaction", resp)
        return None, "missing_result" if isinstance(resp, dict) else "send_failed"
    sig = resp.get("result")
    if not sig:
        return None, "no_signature"
    return sig, None


def send_built_txs(txs, kp, requote_fn=None, transport=None, error_log_path=None, accept_unconfirmed=False):
    """Helius blockhash, sign, send on Helius. One Alchemy attempt after requote.

    requote_fn returns a new unsigned tx only when the buy rules still pass.
    That tx gets a new Alchemy blockhash and one sendTransaction. The same
    signed bytes are not submitted again. Public mainnet is not used.
    """
    import solana_rpc

    def sign_fn(raw, blockhash):
        patched = VQ.replace_recent_blockhash(raw, blockhash)
        return _sign(patched, kp)

    sig, err, uncertain = solana_rpc.submit_bundle(
        list(txs),
        sign_fn,
        requote_fn,
        transport=transport,
        error_log_path=error_log_path,
        accept_unconfirmed=accept_unconfirmed,
    )
    return sig, err, uncertain


def _materialize(row, sold, bought):
    venue = row.get("venue")
    if venue == "jupiter_manual":
        tx_b64, err = VQ.build_jupiter_manual_tx(row.get("payload"))
        if err or not tx_b64:
            return None, err or "jupiter_manual_no_tx"
        return [tx_b64], None
    if venue == "raydium":
        txs, err = VQ.build_raydium_tx(row.get("swap_response"), sold, bought)
        if err or not txs:
            return None, err or "raydium_no_tx"
        return list(txs), None
    return None, f"unknown_venue:{venue}"


def swap_buy(sold, sell_amt, bought, out_decimals, prices, build_only=False):
    """Quote, build, and (unless build_only) sign+send one savings swap.

    Returns a dict: ok, reason, quoted, venue, signature, out_ui, sell_usd,
    gas_sol, send_uncertain, txs_built. Never touches the book.
    build_only builds the unsigned tx(s) and stops (dry run): no key, no send.
    """
    res = {"ok": False, "quoted": False, "sold": sold, "sell_amt": float(sell_amt), "bought": bought}
    if sold not in EDGE_INVENTORY or sold == bought:
        res["reason"] = "refusing_savings_or_non_inventory_funding"
        return res
    sell_usd = float(sell_amt) * _book_px(prices, sold)
    sol_usd = float((prices or {}).get("SOL") or 0)
    res["sell_usd"] = sell_usd
    rows, _amount = quote_rows(sold, bought, sell_amt)
    if not rows:
        res["reason"] = "quote_failed"
        return res
    res["quoted"] = True
    chosen = pick_best_row(rows, sell_usd, sol_usd, out_decimals)
    if not chosen:
        res["reason"] = "no_ok_quote"
        res["venue_notes"] = [f"{r.get('venue')}:{r.get('note')}" for r in rows if not r.get("ok")]
        return res
    row = chosen["row"]
    res["venue"] = row.get("venue")
    res["gas_sol"] = float(row.get("gas_sol") or 0)
    res["out_ui"] = int(row["out_amount"]) / (10 ** int(out_decimals))
    res["out_raw"] = int(row["out_amount"])
    res["net_out_ui"] = chosen.get("net_out_ui")
    txs, err = _materialize(row, sold, bought)
    if err or not txs:
        res["reason"] = f"build_failed:{err}"
        return res
    res["txs_built"] = len(txs)
    if build_only:
        res["reason"] = "built_not_sent_dry_run"
        return res
    kp = _load_keypair()

    def _requote():
        rows2, _a2 = quote_rows(sold, bought, sell_amt)
        if not rows2:
            return None
        chosen2 = pick_best_row(rows2, sell_usd, sol_usd, out_decimals)
        if not chosen2:
            return None
        txs2, err2 = _materialize(chosen2["row"], sold, bought)
        if err2 or not txs2:
            return None
        return list(txs2)

    sig, err_s, uncertain = send_built_txs(txs, kp, _requote)
    if err_s or not sig:
        res["reason"] = f"send_failed:{err_s or 'no_sig'}"
        res["send_uncertain"] = bool(uncertain)
        return res
    res["ok"] = True
    res["reason"] = "savings_buy"
    res["signature"] = sig
    return res


def _fill_rate(cb, sold, bought, sell_amt, buy_amt):
    """Rate in the book pair's convention when it is a book pair, else sold per bought."""
    pairs = (cb.get("pairs") or {}) if isinstance(cb, dict) else {}
    for p in pairs.values():
        if isinstance(p, dict) and {p.get("base"), p.get("quote")} == {sold, bought}:
            amts = {sold: float(sell_amt), bought: float(buy_amt)}
            if amts[p["base"]] > 0:
                return amts[p["quote"]] / amts[p["base"]], p.get("rate_unit") or f"{p['quote']}/{p['base']}"
    if float(buy_amt) > 0:
        return float(sell_amt) / float(buy_amt), f"{sold}/{bought}"
    return None, f"{sold}/{bought}"


def append_savings_fill_logs(cb, res, wallet_after, prices, source, label):
    """trades.csv + trades.jsonl + sol-usdc-log.md for a landed savings buy.

    Rows carry kind=savings_buy / savings=true so pair P&L, reverses, and
    route-hit timers skip them (held savings, not an edge fill).
    """
    import csv
    when = _now_pt()
    sold, bought = res["sold"], res["bought"]
    sell_amt, buy_amt = float(res["sell_amt"]), float(res.get("out_ui") or 0)
    rate, unit = _fill_rate(cb, sold, bought, sell_amt, buy_amt)
    sig = res.get("signature")
    url = f"https://solscan.io/tx/{sig}"
    total_after = sum(float(wallet_after.get(k) or 0) * _book_px(prices, k) for k in BOOK_ASSETS)
    note = (
        f"SAVINGS_BUY {label} held savings (not an edge fill; not sold by trading; "
        f"excluded from pair P&L, reverses, streak, multipliers, tokens_usd); "
        f"source {source}; {res.get('venue')} local signer wallet; "
        f"gas {res.get('gas_sol')} SOL; balances_after are estimates"
    )
    row = {
        "time_pt": when, "side": f"{sold}\u2192{bought}", "sold_asset": sold, "sold_amount": sell_amt,
        "bought_asset": bought, "bought_amount": buy_amt, "rate": rate, "rate_unit": unit,
        "tx": sig, "tx_url": url,
        "sol_after": wallet_after.get("SOL"), "usdc_after": wallet_after.get("USDC"),
        "cbbtc_after": wallet_after.get("cbBTC"), "weth_after": wallet_after.get("WETH"),
        "total_usd_after": round(total_after, 4), "notes": note,
        "kind": "savings_buy", "savings": True, "counts_as_edge": False,
    }
    root = LOG_ROOT
    with open(os.path.join(root, "trades.jsonl"), "a", encoding="utf-8", newline="") as f:
        f.write(json.dumps(row) + "\n")
    with open(__import__("degen_home").trades_csv_path(), "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            when, row["side"], sold, sell_amt, bought, buy_amt, rate, unit, sig, url,
            row["sol_after"], row["usdc_after"], row["cbbtc_after"], row["weth_after"],
            row["total_usd_after"], note,
        ])
    with open(os.path.join(root, "sol-usdc-log.md"), "a", encoding="utf-8", newline="") as f:
        f.write(
            f"\n## {when} Savings buy ({label}) — SAVINGS_FILL\n"
            f"- {sell_amt} {sold} -> {buy_amt} {bought} via {res.get('venue')} tx `{sig}`\n"
            f"- held savings, not an edge fill; source={source}; gas {res.get('gas_sol')} SOL\n"
        )
    return row


def _record_extra_failure(cfg, ex):
    fails = cfg.get("extra_buy_failures")
    if not isinstance(fails, list):
        fails = []
    fails.append({
        "pt": _now_pt(), "symbol": ex.get("symbol"), "buy_usd": ex.get("buy_usd"),
        "fund_symbol": ex.get("fund_symbol"), "venue": ex.get("venue"),
        "reason": ex.get("reason"), "send_uncertain": bool(ex.get("send_uncertain")),
        "retried": False,
    })
    cfg["extra_buy_failures"] = fails[-EXTRA_FAIL_KEEP:]
    cfg["last_extra_failure"] = fails[-1]


def execute_extra(cb, ex, balances, prices, source, build_only=False):
    """One extra savings buy (e.g. $1 cbBTC). One attempt, no retry loop.

    Funding: first affordable candidate (heaviest non-bought, non-JitoSOL);
    on edge checks a fade-blocked candidate is skipped for the next one.
    On success updates savings_<sym>_held. Never touches tokens_usd.
    """
    out = dict(ex)
    sym = out["symbol"]
    try:
        _register_extra_mint(sym, out.get("mint"))
    except RuntimeError as e:
        out.update(action="skip", reason=str(e), purchased=False)
        return out
    cands = [c for c in out.get("fund_candidates") or [] if c.get("affordable")]
    if not cands:
        out.update(action="skip", reason="funding_blocked_sol_buffer_or_balance", purchased=False)
        return out
    pick = None
    fade_blocked = []
    for c in cands:
        if source == "edge_check" and not build_only:
            import fade_trend
            if fade_trend.edge_funding_blocked(source, c["symbol"], c["sell_amt"]):
                fade_blocked.append(c["symbol"])
                print(f"FADE_TREND_SKIP savings_extra {sym} fund={c['symbol']}", flush=True)
                continue
        pick = c
        break
    if fade_blocked:
        out["fade_blocked"] = fade_blocked
    if not pick:
        out.update(action="skip", reason="fade_trend_all_funding", purchased=False)
        return out
    out["fund_symbol"], out["fund_sell_amt"] = pick["symbol"], pick["sell_amt"]
    try:
        res = swap_buy(pick["symbol"], pick["sell_amt"], sym, out["decimals"], prices, build_only=build_only)
    except Exception as e:  # never let one extra break the step
        res = {"ok": False, "reason": f"send_failed:exception:{type(e).__name__}", "quoted": False}
    for k in ("venue", "quoted", "signature", "out_ui", "out_raw", "gas_sol", "send_uncertain", "txs_built", "sell_usd", "venue_notes"):
        if k in res:
            out[k] = res[k]
    out["reason"] = res.get("reason")
    if not res.get("ok"):
        out["action"] = "built" if build_only and res.get("txs_built") else "skip"
        out["purchased"] = False
        return out
    cfg = cb["savings_mode"]
    key = held_key(sym)
    rec = cfg.get(key) if isinstance(cfg.get(key), dict) else {}
    rec["raw"] = int(rec.get("raw") or 0) + int(res.get("out_raw") or 0)
    rec["amount"] = rec["raw"] / (10 ** int(out["decimals"]))
    rec["decimals"] = int(out["decimals"])
    rec["cost_usd"] = float(rec.get("cost_usd") or 0) + float(res.get("sell_usd") or 0)
    rec["buys"] = int(rec.get("buys") or 0) + 1
    rec["updated_pt"] = _now_pt()
    rec["last_signature"] = res.get("signature")
    rec["rule"] = (
        "Held savings, not trading inventory. Subtracted from wallet cbBTC by "
        "savings_mode.trading_balances on every trading path. Counts in the "
        "wallet total and the savings latch total. Not in tokens_usd. Never sold."
    )
    cfg[key] = rec
    out.update(action="bought", purchased=True)
    out["held_after"] = rec["amount"]
    return out


def execute_plan(cb, idea, balances, prices, source="edge_check", build_only=False):
    """Run one latch step: the JitoSOL buy plus every extra buy (cbBTC).

    Each buy is one attempt (no retry loop). The latch rises by one step
    when at least one buy lands. Failures are recorded; a failed extra buy
    is not retried on a later check. Does not touch pair multipliers, the
    streak, or tokens_usd. Refuses unless enabled and the plan action is buy.
    build_only (dry run): quote and build only, never sign/send/write.
    """
    if _cfg(cb).get("enabled") is not True:
        return _skip("savings_mode_disabled", _cfg(cb))
    if not idea or idea.get("action") != "buy":
        out = dict(idea or _skip("no_plan", _cfg(cb)))
        out["purchased"] = False
        out["quoted"] = False
        return out
    sym = symbol_of(cb)
    tb = trading_balances(cb, balances)
    wallet = dict(wallet_balances(tb))
    idea = dict(idea)
    primary_ok = False
    if idea.get("primary_affordable", True) and idea.get("fund_symbol"):
        sold = idea["fund_symbol"]
        sell_amt = float(idea["fund_sell_amt"])
        if is_savings_asset(cb, sold) or sold not in EDGE_INVENTORY:
            idea["reason"] = "refusing_savings_or_non_inventory_funding"
            res = {"ok": False, "reason": idea["reason"], "quoted": False}
        else:
            _register_mint(_cfg(cb))
            dec = int(_cfg(cb).get("decimals") or DECIMALS.get(sym) or 9)
            try:
                res = swap_buy(sold, sell_amt, sym, dec, prices, build_only=build_only)
            except Exception as e:
                res = {"ok": False, "reason": f"send_failed:exception:{type(e).__name__}", "quoted": False}
        idea["quoted"] = bool(res.get("quoted"))
        for k in ("venue", "send_uncertain", "txs_built", "gas_sol", "out_ui"):
            if k in res:
                idea[k] = res[k]
        if res.get("ok"):
            primary_ok = True
            cfg = cb["savings_mode"]
            cfg["savings_cost_usd"] = float(cfg.get("savings_cost_usd") or 0) + float(res.get("sell_usd") or 0)
            cfg["jitosol_qty"] = float(cfg.get("jitosol_qty") or 0) + float(res["out_ui"])
            cfg["held"] = True
            cfg["last_buy"] = {
                "signature": res["signature"], "venue": res.get("venue"), "sold": sold,
                "sold_amount": sell_amt, "bought": sym, "bought_amount": res["out_ui"],
                "buy_usd": jitosol_leg_usd(cb), "steps": 1, "counts_as_edge": False,
            }
            idea.update(action="bought", reason="savings_buy", purchased=True, dirty=True,
                        signature=res["signature"], bought_amount=res["out_ui"])
            tb = _project_after(tb, sold, sell_amt, None, 0.0, res.get("gas_sol"))
            wallet = _project_after(wallet, sold, sell_amt, None, 0.0, res.get("gas_sol"))
            try:
                append_savings_fill_logs(cb, res, wallet, prices, source, f"{sym} ${jitosol_leg_usd(cb):g}")
            except Exception as log_err:
                idea["log_error"] = type(log_err).__name__
        else:
            idea["action"] = "built" if build_only and res.get("txs_built") else "skip"
            idea["reason"] = res.get("reason")
            idea["purchased"] = False
    else:
        idea["action"] = "skip"
        idea["reason"] = "jitosol_leg_skipped" if idea.get("jitosol_leg_skipped") else "funding_blocked_sol_buffer_or_balance"
        idea["purchased"] = False
    # Extra buys (cbBTC $1): re-plan from balances after the JitoSOL sale.
    buffer = float(_cfg(cb).get("min_sol_buffer") or MIN_SOL_BUFFER_DEFAULT)
    extras_out = []
    any_extra = False
    if not primary_ok and idea.get("fund_symbol") and idea.get("primary_affordable", True) and build_only:
        # Dry run: plan extras as if the JitoSOL sale happened.
        tb = _project_after(tb, idea["fund_symbol"], idea.get("fund_sell_amt"))
    for item in extra_buys(cb):
        ex = plan_extra(cb, item, tb, prices, buffer)
        done = execute_extra(cb, ex, tb, prices, source, build_only=build_only)
        if done.get("purchased"):
            any_extra = True
            res = {
                "sold": done["fund_symbol"], "sell_amt": done["fund_sell_amt"], "bought": done["symbol"],
                "out_ui": done.get("out_ui"), "signature": done.get("signature"),
                "venue": done.get("venue"), "gas_sol": done.get("gas_sol"),
            }
            tb = _project_after(tb, done["fund_symbol"], done["fund_sell_amt"], None, 0.0, done.get("gas_sol"))
            wallet = _project_after(wallet, done["fund_symbol"], done["fund_sell_amt"],
                                    done["symbol"], done.get("out_ui") or 0.0, done.get("gas_sol"))
            try:
                append_savings_fill_logs(cb, res, wallet, prices, source, f"{done['symbol']} ${done['buy_usd']}")
            except Exception as log_err:
                done["log_error"] = type(log_err).__name__
        elif not build_only:
            _record_extra_failure(cb["savings_mode"], done)
            idea["dirty"] = True
        extras_out.append(done)
    idea["extra_buys"] = extras_out
    idea["extra_failures"] = [
        e for e in extras_out if not e.get("purchased") and not build_only
    ]
    advanced = (primary_ok or any_extra) and not build_only
    if advanced:
        cfg = cb["savings_mode"]
        step = float(cfg.get("gain_step_usd") or 0)
        # One step per latch crossing, even if one of the buys failed.
        cfg["usd_high_water"] = float(cfg.get("usd_high_water") or 0) + step
        _advance_jitosol_leg(cfg, primary_ok)
        cfg["last_step"] = {
            "pt": _now_pt(), "usd_high_water": cfg["usd_high_water"],
            "jitosol": "bought" if primary_ok else idea.get("reason"),
            "extras": {e.get("symbol"): ("bought" if e.get("purchased") else e.get("reason")) for e in extras_out},
        }
        if isinstance(cfg.get("last_buy"), dict) and primary_ok:
            cfg["last_buy"]["usd_high_water"] = cfg["usd_high_water"]
        idea["dirty"] = True
        if not primary_ok:
            idea["purchased_extra_only"] = True
    idea["latch_advanced"] = bool(advanced)
    idea["usd_high_water"] = _cfg(cb).get("usd_high_water")
    idea["gains_credited_usd"] = _cfg(cb).get("gains_credited_usd")
    idea["counts_as_edge"] = False
    idea["streak_changed"] = False
    idea["multipliers_changed"] = False
    return idea


def consider(cb, balances, prices, source, execute=False, jitosol_mark_usd=0.0):
    """Shared hook. Enabled false skips with no quote and no book change.

    source is edge_check or morning_balance. Neither path sells JitoSOL or
    saved cbBTC (funding is sized from trading_balances).
    Deposits are applied to both USD latches before the buy test.
    A missing latch is set to the current combined total and does not buy.
    arm_guard: if this hook would buy immediately, fix the latch instead.
    """
    dep = apply_external_deposits(cb, balances, prices)
    cfg = _cfg(cb)
    if cfg.get("enabled") is not True:
        status = _skip("savings_mode_disabled", cfg)
        status["source"] = source
        return _note_deposit(status, dep)
    _blk = buy_block_reason(cb, dep)
    if _blk:
        status = _skip(_blk, cfg)
        status["source"] = source
        status["enabled"] = True
        status["dirty"] = bool(dep.get("dirty"))
        return _note_deposit(status, dep)
    if cfg.get("usd_high_water") is None:
        total = combined_portfolio_usd(cb, balances, prices)
        cfg["usd_high_water"] = total
        cfg["arm_guard"] = False
        status = _skip("high_water_initialized", cfg)
        status["enabled"] = True
        status["source"] = source
        status["dirty"] = True
        status["quoted"] = False
        status["purchased"] = False
        status["portfolio_usd"] = total
        status["usd_high_water"] = total
        return _note_deposit(status, dep)
    idea = plan(cb, balances, prices, jitosol_mark_usd)
    idea["source"] = source
    if idea.get("action") == "buy" and cfg.get("arm_guard") is True:
        total = float(idea.get("portfolio_usd") or 0)
        cfg["usd_high_water"] = total
        cfg["arm_guard"] = False
        idea["action"] = "skip"
        idea["reason"] = "high_water_corrected"
        idea["purchased"] = False
        idea["quoted"] = False
        idea["dirty"] = True
        idea["usd_high_water"] = total
        idea["steps"] = 0
        return _note_deposit(idea, dep)
    if cfg.get("arm_guard") is True and idea.get("action") != "buy":
        cfg["arm_guard"] = False
        idea["dirty"] = True
        idea["arm_guard_cleared"] = True
    if idea.get("action") != "buy" or not execute:
        if idea.get("action") == "buy" and not execute:
            idea["reason"] = "plan_only_not_executed"
        return _note_deposit(idea, dep)
    # Edge-check funding sell only. Morning balance is not this rule.
    import fade_trend
    # The JitoSOL sale is fade-checked as before; a fade block defers the whole
    # step (nothing bought, latch unchanged). Extra buys fade-check their own funding.
    if idea.get("primary_affordable", True) and fade_trend.edge_funding_blocked(
        source, idea.get("fund_symbol"), idea.get("fund_sell_amt")
    ):
        idea = dict(idea)
        idea["action"] = "skip"
        idea["reason"] = "fade_trend"
        idea["purchased"] = False
        idea["quoted"] = False
        idea["dirty"] = False
        idea["counts_as_edge"] = False
        idea["streak_changed"] = False
        idea["multipliers_changed"] = False
        print(f"FADE_TREND_SKIP savings fund={idea.get('fund_symbol')}", flush=True)
        return _note_deposit(idea, dep)
    done = execute_plan(cb, idea, balances, prices, source=source)
    done["source"] = source
    _fails = [r for r in [done.get("reason")] + [e.get("reason") for e in done.get("extra_failures") or []]
              if isinstance(r, str) and r.startswith("send_failed")]
    if _fails:
        pause_mode_uncertain(cb["savings_mode"], _fails[0], "savings")
        done["dirty"] = True
        done["mode_paused"] = True
    return _note_deposit(done, dep)


def edge_check_hook(cb, balances, prices, execute=True):
    return consider(cb, balances, prices, source="edge_check", execute=execute)


def morning_balance_hook(cb, balances, prices, execute=True):
    return consider(cb, balances, prices, source="morning_balance", execute=execute)


if __name__ == "__main__":
    # Plan only. Does not quote, sign, or send.
    book = json.load(open(CB_PATH))
    status = plan(book, {}, {})
    assert status.get("purchased") is False and status.get("quoted") is False
    print("savings_selfcheck", status.get("action"), status.get("reason"), "enabled", status.get("enabled"))
