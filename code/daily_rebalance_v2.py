#!/usr/bin/env python3
"""Daily rebalance v2 (owner 2026-10-11) — BUILT DISABLED, planning + dry-run only.

Targets (share of the TOTAL trading book SOL+USDC+cbBTC+WETH, saved cbBTC excluded):
  1. USDC = Fear & Greed / 5 (saved daily value, market_gates.fetch_fng; no extra fetch).
  2. The rest is split across cbBTC/WETH/SOL by CoinGecko 24h volume share (usd_24h_vol of
     bitcoin/ethereum/solana — the same feed and math as the dashboard's GLOBAL VOLUME pie).
  3. Each of cbBTC/WETH/SOL clamped to [min_pct, max_pct] (20..55 since floor20 2026-10-12; was 10..55) of the total; unclamped
     crypto weights re-normalized, iterated until stable. If the bounds cannot be met with the
     F&G cash target (cash > 100 - 3*min or < 100 - 3*max) the cash target is moved to the
     nearest feasible value and reported as cash_adjusted.
  4. Swaps to reach the targets: greedy largest-overweight -> largest-underweight matching
     (at most 3 swaps for 4 assets, direct pairs; all 6 pairs are allowed). Only daily_fraction
     (1/10) of each swap's notional is executed per day; legs below min_trade_usd are skipped; no fee-loss skip
     (allow_negative_net: true, owner 2026-10-11).
Fear & Greed gates apply (block USDC buys >75 + size x0.5, block sells to USDC <25); the Kraken
VWAP rule does not (dailyv2d). Execution (dailyv2e): run_morning() from morning_balance_run at 6:59 AM PT (Degen Morning Balance task), once per PT day
(daily-rebalance-v2-state.json); each leg via execute() on the top-up send path, recorded as daily_v2.
Live sends only when daily_rebalance_v2.enabled is true.
Usage: .venv/bin/python daily_rebalance_v2.py --dry-run [--no-quotes]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import degen_home as _DH

ROOT = Path(_DH.HOME_STR)
CRYPTO = ("cbBTC", "WETH", "SOL")
BOOK = ("SOL", "USDC", "cbBTC", "WETH")
CG_IDS = {"cbBTC": "bitcoin", "WETH": "ethereum", "SOL": "solana"}  # dashboard SHARE_COINS
CG_URL = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana&vs_currencies=usd&include_24hr_vol=true"
DEFAULTS = {
    "enabled": False,
    "time_pt": "06:59",  # informational; the Windows task trigger decides
    "cash_divisor": 5.0,
    "min_pct": 20.0,  # floor20 2026-10-12: was 10%
    "max_pct": 55.0,
    "daily_fraction": 0.1,
    "min_trade_usd": 1.0,
    "apply_market_gates": False,   # superseded by the two keys below (dailyv2d)
    "apply_fear_greed": True,      # F&G >75: size x0.5 + no USDC->crypto buys; <25: no crypto->USDC sells
    "skip_vwap": True,             # v2 exempt from the Kraken 24h VWAP rule
    "respect_size_limits": True,
    "allow_negative_net": True,
    "fill_kind": "daily_v2",       # dailyv2c: recorded kind; edge reverses capped at reversal_cap_pct
    "reversal_cap_pct": 50.0,  # dailyv2b: small fee-inclusive losses allowed; no net skip
    "supersedes": ["underweight_double"],
}


def cfg_of(cb: dict) -> dict:
    c = dict(DEFAULTS)
    c.update((cb or {}).get("daily_rebalance_v2") or {})
    import h093 as _H093  # H093 preset: daily_fraction 0.15 when cost-basis h093.enabled
    c["daily_fraction"] = _H093.daily_fraction(cb, c["daily_fraction"])
    return c


def volume_shares(getter=None) -> dict:
    """{cbBTC, WETH, SOL: share 0..1} from CoinGecko usd_24h_vol (dashboard getShareData('vol'))."""
    if getter is None:
        import market_gates as MG
        getter = MG._get
    import keyless

    def coingecko():
        d = getter(CG_URL, 10)
        vols = {s: float((d.get(cid) or {}).get("usd_24h_vol") or 0) for s, cid in CG_IDS.items()}
        tot = sum(vols.values())
        if tot <= 0:
            raise ValueError("no_volume")
        return {s: v / tot for s, v in vols.items()}
    # v1.0.0: volume shares move slowly; last-good value usable up to 36 h, else the caller skips the run.
    v, _meta = keyless.first_ok("cg_volume_shares", [("coingecko", coingecko)], 36 * 3600)
    return v


def targets(fng: float, vol: dict, cfg: dict) -> dict:
    """Return dict with cash/ideal/clamped weights (percent of total) and notes."""
    lo, hi = float(cfg["min_pct"]), float(cfg["max_pct"])
    n = len(CRYPTO)
    cash = float(fng) / float(cfg["cash_divisor"])
    notes = []
    feas_hi, feas_lo = 100 - n * lo, max(0.0, 100 - n * hi)
    cash_adj = min(max(cash, feas_lo), feas_hi)
    if abs(cash_adj - cash) > 1e-9:
        notes.append(f"cash_adjusted {cash:.2f}% -> {cash_adj:.2f}% (bounds {lo}-{hi}% x{n} need cash in [{feas_lo:.0f},{feas_hi:.0f}])")
    rest = 100 - cash_adj
    ideal = {s: rest * vol[s] for s in CRYPTO}
    # floor20 2026-10-12: bounded proportional fill. w_s = clamp(k * vol_s, lo, hi), with k found by
    # bisection so the crypto weights sum to `rest`. Unclamped coins keep their volume ratios; works
    # when floors and caps both bind (the old one-direction pass could overshoot with a 20% floor).
    vs = {s: max(0.0, float(vol.get(s) or 0)) for s in CRYPTO}
    if sum(vs.values()) <= 0:
        vs = {s: 1.0 for s in CRYPTO}
    f = lambda k: {s: min(hi, max(lo, k * vs[s])) for s in CRYPTO}
    k_lo, k_hi = 0.0, 1.0
    while sum(f(k_hi).values()) < rest - 1e-12 and k_hi < 1e12:
        k_hi *= 2
    for _ in range(200):
        k = (k_lo + k_hi) / 2
        if sum(f(k).values()) < rest:
            k_lo = k
        else:
            k_hi = k
    w = f(k_hi)
    err = rest - sum(w.values())
    free = [s for s in CRYPTO if lo + 1e-9 < w[s] < hi - 1e-9] or list(CRYPTO)
    for s in free:
        w[s] += err / len(free)
    fixed = {s: w[s] for s in CRYPTO if abs(w[s] - lo) < 1e-6 or abs(w[s] - hi) < 1e-6}
    out = {"USDC": cash_adj, **{s: w[s] for s in CRYPTO}}
    return {"fng": fng, "cash_rule_pct": cash, "ideal": {"USDC": cash_adj, **ideal}, "target": out,
            "clamped": sorted(fixed), "notes": notes}


def swaps(cur_usd: dict, tgt_pct: dict) -> list[dict]:
    """Fewest-swaps greedy: largest surplus pays largest deficit, direct pair."""
    total = sum(cur_usd.values())
    diff = {s: cur_usd.get(s, 0.0) - total * tgt_pct[s] / 100 for s in BOOK}
    over = {s: d for s, d in diff.items() if d > 1e-9}
    under = {s: -d for s, d in diff.items() if d < -1e-9}
    out = []
    while over and under:
        so = max(over, key=over.get)
        su = max(under, key=under.get)
        amt = min(over[so], under[su])
        out.append({"sell": so, "buy": su, "usd": amt})
        over[so] -= amt
        under[su] -= amt
        if over[so] <= 1e-6:
            del over[so]
        if under[su] <= 1e-6:
            del under[su]
    return out


def daily_legs(full: list[dict], cfg: dict, bals: dict, prices: dict, cb: dict) -> list[dict]:
    frac, mn = float(cfg["daily_fraction"]), float(cfg["min_trade_usd"])
    sl = (cb or {}).get("size_limits") or {}
    max_frac = float(sl.get("max_sell_fraction_of_balance", 0.4))
    min_sol = float(sl.get("min_sol_fee_buffer", 0.02))
    legs = []
    work = dict(bals)
    for s in full:
        usd = s["usd"] * frac
        row = {"sell": s["sell"], "buy": s["buy"], "full_usd": round(s["usd"], 4), "usd": round(usd, 4)}
        px = 1.0 if s["sell"] == "USDC" else float(prices.get(s["sell"]) or 0)
        if cfg.get("respect_size_limits") and px > 0:
            bal = float(work.get(s["sell"]) or 0)
            cap = bal * max_frac
            if s["sell"] == "SOL":
                cap = min(cap, max(0.0, bal - min_sol))
            if usd > cap * px:
                row["capped_from_usd"] = row["usd"]
                usd = cap * px
                row["usd"] = round(usd, 4)
        row["sell_amt"] = usd / px if px > 0 else 0.0
        if usd < mn:
            row["skip"] = f"below_min_trade_usd({mn})"
        else:
            work[s["sell"]] = float(work.get(s["sell"]) or 0) - row["sell_amt"]
        legs.append(row)
    return legs


def apply_gates(legs: list[dict], cfg: dict, gates) -> list[dict]:
    """dailyv2d 2026-10-11: Fear & Greed rules apply (block + >75 halving), VWAP exempt."""
    if not cfg.get("apply_fear_greed") or gates is None:
        return legs
    mult = float(gates.size_mult())
    for leg in legs:
        if leg.get("skip"):
            continue
        rule = gates.block(leg["sell"], leg["buy"], skip_vwap=bool(cfg.get("skip_vwap", True)))
        if rule:
            leg["skip"] = f"market_gate:{rule}"
            continue
        if mult != 1:
            leg["greed_mult"] = mult
            leg["usd"] = round(leg["usd"] * mult, 4)
            leg["sell_amt"] *= mult
            if leg["usd"] < float(cfg["min_trade_usd"]):
                leg["skip"] = f"below_min_trade_usd({cfg['min_trade_usd']})"
    return legs


def plan(cb: dict, bals: dict, prices: dict, fng: float, vol: dict, gates=None) -> dict:
    cfg = cfg_of(cb)
    cur = {s: float(bals.get(s) or 0) * (1.0 if s == "USDC" else float(prices.get(s) or 0)) for s in BOOK}
    total = sum(cur.values())
    t = targets(fng, vol, cfg)
    full = swaps(cur, t["target"])
    return {"cfg": cfg, "total_usd": total, "current_pct": {s: cur[s] / total * 100 for s in BOOK},
            "current_usd": cur, **t, "volume_pct": {s: vol[s] * 100 for s in CRYPTO},
            "full_swaps": full, "today": apply_gates(daily_legs(full, cfg, bals, prices, cb), cfg, gates)}


STATE_PATH = ROOT / "daily-rebalance-v2-state.json"


def slot_key(now) -> str:
    """buyside1012: two runs per PT day (6:59 AM 'am', 4:59 PM 'pm'); each slot runs once."""
    return f"{now.date().isoformat()}-{'am' if now.hour < 12 else 'pm'}"


def _state(today: str) -> dict:
    try:
        st = json.loads(STATE_PATH.read_text())
    except Exception:
        st = {}
    return st if st.get("date") == today else {"date": today, "done": False, "legs": []}


def _save_state(st: dict) -> None:
    import fade_trend
    fade_trend.atomic_write_json(str(STATE_PATH), st)


def _quote(FR, MB, sold, bought, sell_amt, prices, cfg):
    """Best net of Jupiter manual (fee 0) and Raydium. allow_negative_net: no fee-loss skip."""
    sol_usd = float(prices.get("SOL") or 0)
    bpx = 1.0 if bought == "USDC" else float(prices.get(bought) or 0)
    best, _rows, started = FR.quote_winning_route(sold, bought, sell_amt, sol_usd, bpx)
    if not best:
        return None, None, started, "no_quote"
    buy_amt = int(best["out_amount"]) / (10 ** MB.DEC[bought])
    ok, net = MB.net_ok(sold, bought, sell_amt, buy_amt, best.get("gas_sol"), prices)
    if not ok and not cfg.get("allow_negative_net", True):
        return None, net, started, "net_loss_beyond_dust"
    return best, net, started, None


def execute(cb, leg, prices, dry_run=False):
    """One v2 leg on the morning top-up's send path (savings_mode.send_built_txs: dupsend guard,
    wait-then-retry, stale re-quote, rebuild on retry). Fill recorded as kind daily_v2."""
    import time
    import friday_rsi as FR
    import morning_balance_run as MB
    import savings_mode
    import venue_quotes as VQ
    cfg = cfg_of(cb)
    kind = cfg.get("fill_kind", "daily_v2")
    sold, bought, sell_amt = leg["sell"], leg["buy"], float(leg["sell_amt"])
    pair = FR._pair_of(sold, bought)
    best, net, started, why = _quote(FR, MB, sold, bought, sell_amt, prices, cfg)
    if not best:
        return {"sent": False, "error": why, "pair": pair, "net_usd": net}
    q = {"venue": best.get("venue"), "out": int(best["out_amount"]) / (10 ** MB.DEC[bought]), "net_usd": net}
    if dry_run:
        return {"sent": False, "error": "dry_run", "pair": pair, "quote": q}
    if not cfg.get("enabled"):
        return {"sent": False, "error": "disabled", "pair": pair}
    txs, err = MB._uw_build(VQ, best, sold, bought)
    if err or not txs:
        return {"sent": False, "error": err or "no_tx", "pair": pair}
    if time.monotonic() - started > FR.QUOTE_STALE_SEC:
        best, net, started, why = _quote(FR, MB, sold, bought, sell_amt, prices, cfg)
        if not best:
            return {"sent": False, "error": f"stale_requote:{why}", "pair": pair}
        txs, err = MB._uw_build(VQ, best, sold, bought)
        if err or not txs:
            return {"sent": False, "error": err or "no_tx", "pair": pair}
    holder = {"best": best}

    def _rebuild():
        b2, _n, _s, _w = _quote(FR, MB, sold, bought, sell_amt, prices, cfg)
        if not b2:
            return None
        built, e2 = MB._uw_build(VQ, b2, sold, bought)
        if e2 or not built:
            return None
        holder["best"] = b2
        return built

    kp = savings_mode._load_keypair()
    sig, err_s, uncertain = savings_mode.send_built_txs(txs, kp, _rebuild, accept_unconfirmed=False)
    if err_s or not sig:
        return {"sent": False, "error": err_s or "send_failed", "pair": pair, "submitted": bool(uncertain)}
    best = holder["best"]
    buy_amt = int(best["out_amount"]) / (10 ** MB.DEC[bought])
    base, quote = FR.PAIR_BASE_QUOTE[pair]
    amts = {sold: sell_amt, bought: float(buy_amt)}
    fill_rate = amts[quote] / amts[base]
    when = MB.now_pt().isoformat()
    FR.record_rebalance_fill(pair, sold, bought, sell_amt, buy_amt, fill_rate, sig, when, kind)
    return {"sent": True, "signature": sig, "pair": pair, "sold": sold, "bought": bought,
            "sell_amt": sell_amt, "buy_amt": buy_amt, "fill_rate": fill_rate, "kind": kind,
            "venue": best.get("venue"), "gas_sol": best.get("gas_sol"), "when_pt": when, "net_usd": net}


def run_morning(cb, bals, prices, dry_run=False, executor=None, fng=None, vol=None, gates=None):
    """Called by morning_balance_run.main() (Degen Morning Balance task, 6:59 AM and 4:59 PM PT). Once per slot (state file).
    Returns (results, note). Does nothing live unless daily_rebalance_v2.enabled."""
    import morning_balance_run as MB
    cfg = cfg_of(cb)
    if not cfg.get("enabled") and not dry_run:
        return [], "daily_rebalance_v2 disabled"
    today = slot_key(MB.now_pt())  # buyside1012: once per slot (am 6:59 / pm 4:59 PT), each 1/10 of the gap
    st = _state(today)
    if st.get("done") and not dry_run:
        return [], f"daily_rebalance_v2 already ran this slot ({today})"
    import market_gates as MG
    if fng is None:
        f = MG.fetch_fng(MG.cfgs(cb)[0])
        if f.get("value") is None:
            return [{"error": "fear_greed_unavailable", "fng": f}], "daily_rebalance_v2 skipped (no F&G)"
        fng = f["value"]
        gates = gates or MG.Gates(cb, fng=f, vwap={})
    if vol is None:
        try:
            vol = volume_shares()
        except Exception as e:
            return [{"error": f"volume_unavailable:{type(e).__name__}"}], "daily_rebalance_v2 skipped (no volume)"
    p = plan(cb, bals, prices, fng, vol, gates=gates)
    run = executor or execute
    results = []
    cur = dict(bals)  # after1012: running balances; each row's after = read after THAT fill
    for leg in p["today"]:
        if leg.get("skip"):
            results.append({"sent": False, "error": leg["skip"], "leg": leg})
            continue
        res = dict(run(cb, leg, prices, dry_run=dry_run) or {})
        res["leg"] = leg
        results.append(res)
        if res.get("sent"):
            st["legs"].append({k: res.get(k) for k in ("pair", "sold", "bought", "sell_amt", "buy_amt", "when_pt")})
            _save_state(st)
            try:
                import after_bals as _AB
                after, _src = _AB.read_after(MB.MAIN_WALLET, cur, res["sold"], res["bought"], res["sell_amt"], res["buy_amt"])
                cur = dict(after)
                res["after_source"] = _src
                total_after = round(sum(MB.usd_map(after, prices).values()), 4)
                MB.append_trade_logs(
                    res["when_pt"], f"{res['sold']}\u2192{res['bought']}", res["sold"], res["sell_amt"],
                    res["bought"], res["buy_amt"], res["fill_rate"], res["pair"], res["signature"], after, total_after,
                    f"DAILY_V2 target {p['target'][res['bought']]:.2f}% {res['bought']}; 1/10 of ${leg['full_usd']:.2f}; "
                    f"{res.get('venue')} local signer wallet"
                     + ("" if _src == "fresh_confirmed" else "; balances_after estimated (bought token not seen in reads)"), kind=res["kind"])
            except Exception as e:
                res["log_error"] = type(e).__name__
    if not dry_run:
        st["done"] = True
        st["plan"] = {"fng": fng, "target": p["target"], "total_usd": p["total_usd"]}
        _save_state(st)
    return results, ("daily_rebalance_v2 dry-run" if dry_run else "daily_rebalance_v2 ran")


def _dry_run(quotes=True):
    import market_gates as MG
    import morning_balance_run as MB
    import savings_mode
    cb = json.loads((ROOT / "cost-basis.json").read_text())
    f = MG.fetch_fng(MG.cfgs(cb)[0])  # saved daily value; fetches only if the 9 PM refetch is due
    if f.get("value") is None:
        print(json.dumps({"skip": "fear_greed_unavailable", "fng": f})); return 2
    raw, _held = MB.get_balances(MB.MAIN_WALLET)
    bals = savings_mode.trading_balances(cb, {t: float(raw.get(t) or 0) for t in BOOK})
    prices, src = MB.jupiter_prices()
    vol = volume_shares()
    gates = MG.Gates(cb, fng=f, vwap={})  # VWAP never consulted (skip_vwap); no Kraken fetch
    p = plan(cb, bals, prices, f["value"], vol, gates=gates)
    p["fng_detail"], p["price_source"], p["balances"], p["prices"] = f, src, bals, prices
    if quotes:
        res, note = run_morning(cb, bals, prices, dry_run=True, fng=f["value"], vol=vol, gates=gates)
        p["wired_dry_run"] = {"note": note, "results": res}
    print(json.dumps(p, indent=1, default=str))
    return 0


if __name__ == "__main__":
    if "--dry-run" not in sys.argv[1:]:
        print("daily_rebalance_v2: disabled build; only --dry-run is supported", flush=True)
        raise SystemExit(1)
    raise SystemExit(_dry_run(quotes="--no-quotes" not in sys.argv[1:]))
