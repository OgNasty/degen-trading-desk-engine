#!/usr/bin/env python3
"""Daily morning wallet check. Morning 5% top-up re-enabled 2026-10-10.

No fees-vs-edge line, field, or file (retired 2026-10-05, owner). Do not re-add.
"""
from __future__ import annotations
# --- auto-switch to the project venv (signing needs solders) ---
import os as _os, sys as _sys
import degen_home as _DH
_DH.maybe_reexec_venv()
# --- end auto-switch ---

import json
import os
import time
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from degen_home import TZ_NAME as _TZ_NAME

PT = ZoneInfo(_TZ_NAME)
ROOT = Path(_DH.HOME_STR)
sys.path.insert(0, str(ROOT))

import memecoin_mode
import savings_mode
import fade_trend
import venue_quotes as VQ

MAIN_WALLET = __import__("degen_home").wallet_address()
import solana_rpc
MINTS = VQ.MINTS
DEC = {"SOL": 9, "USDC": 6, "cbBTC": 8, "WETH": 8}
BOOK = ("SOL", "USDC", "cbBTC", "WETH")
DUST_USD = 0.05  # must not lose after fees beyond dust


def now_pt():
    return datetime.now(PT).replace(microsecond=0)


def rpc(method, params):
    """Shared order: helius, alchemy, public. Falls through on failure. Does not print URLs."""
    return solana_rpc.rpc(method, params, retries=5, timeout=25)


def _guard_main_wallet(cb, prices=None):
    """bugfix1012: fail closed on a bad wallet balance read (same rule as the edge check)."""
    import balance_guard as _BG
    try:
        px = prices or dict((json.loads((ROOT / "last-check.json").read_text()) or {}).get("prices_usd") or {})
    except Exception:
        px = {}
    try:
        return _BG.guarded_read(lambda: get_balances(MAIN_WALLET), (cb.get("deposit_watch") or {}).get("balances") or {},
                                px, str(ROOT / "balance-guard.json"), log=lambda x: print(x, flush=True))
    except _BG.BadBalanceRead as e:
        print(f"ERROR bad_balance_read {e.coins}: wallet read 0 twice; morning job ends with no trades", flush=True)
        raise SystemExit(2)


def get_balances(addr):
    sol_lamports = rpc("getBalance", [addr])["result"]["value"]
    bals = {"SOL": sol_lamports / 1e9}
    tok = rpc("getTokenAccountsByOwner", [
        addr,
        {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
        {"encoding": "jsonParsed"},
    ])
    want = {MINTS["USDC"]: "USDC", MINTS["cbBTC"]: "cbBTC", MINTS["WETH"]: "WETH"}
    for v in tok["result"]["value"]:
        info = v["account"]["data"]["parsed"]["info"]
        mint = info["mint"]
        if mint in want:
            bals[want[mint]] = float(info["tokenAmount"]["uiAmount"] or 0)
    for k in ("USDC", "cbBTC", "WETH"):
        bals.setdefault(k, 0.0)
    try:  # lend1015: lent USDC (jlUSDC x share value) is USDC the bot owns
        import degen_lend as _LEND
        bals["USDC"] = float(bals.get("USDC") or 0) + _LEND.lent_usdc_from_tok(tok)
    except Exception as _le:
        print(f"LEND_READ_ERR {type(_le).__name__}", flush=True)
        raise
    # held outside book (Tokenkeg + Token-2022 for Greedy if needed)
    held = {"JitoSOL": 0.0, "GreedyGoats": 0.0}
    jito = "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn"
    try:  # template: the memecoin chosen at setup (default Greedy Goats), the memecoin mint
        goats = str((json.loads((ROOT / "cost-basis.json").read_text()).get("memecoin_mode") or {}).get("mint") or "")
    except Exception:
        goats = ""
    for v in tok["result"]["value"]:
        info = v["account"]["data"]["parsed"]["info"]
        mint = info["mint"]
        amt = float(info["tokenAmount"]["uiAmount"] or 0)
        if mint == jito:
            held["JitoSOL"] = amt
        elif mint == goats:
            held["GreedyGoats"] = amt
    # Token-2022 accounts
    try:
        tok2 = rpc("getTokenAccountsByOwner", [
            addr,
            {"programId": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"},
            {"encoding": "jsonParsed"},
        ])
        for v in tok2["result"]["value"]:
            info = v["account"]["data"]["parsed"]["info"]
            mint = info["mint"]
            amt = float(info["tokenAmount"]["uiAmount"] or 0)
            if mint == goats:
                held["GreedyGoats"] = amt
            elif mint == jito:
                held["JitoSOL"] = amt
    except Exception:
        pass
    try:  # stake1015: the separate JitoSOL bucket is SOL (x JitoSOL->SOL), never savings JitoSOL
        import sol_stake as _STK
        _bsol, _bq = _STK.bucket_sol_for_read(_STK.jitosol_in_tok(tok))
        if _bq > 0:
            bals["SOL"] = float(bals.get("SOL") or 0) + _bsol
            held["JitoSOL"] = max(0.0, float(held.get("JitoSOL") or 0) - _bq)
    except Exception as _se:
        print(f"STAKE_READ_ERR {type(_se).__name__}", flush=True)
        raise
    return bals, held


def jupiter_prices():
    """USD marks for the book. Jupiter lite price/v3 first; CoinGecko fallback.

    Shared with the edge check (fade_trend.fetch_usd_prices). Never invents
    prices. Logs PRICE_FALLBACK source=coingecko when Jupiter fails/429s.
    """
    prices = {"USDC": 1.0, "SOL": 0.0, "cbBTC": 0.0, "WETH": 0.0}
    try:
        parsed, source = fade_trend.fetch_usd_prices()
    except Exception as exc:
        print(f"PRICE_ERR {type(exc).__name__}:{exc}", flush=True)
        return prices, "price_unavailable"
    for sym in BOOK:
        usd = (parsed.get(sym) or {}).get("usd")
        try:
            if usd is not None and float(usd) > 0:
                prices[sym] = float(usd)
        except (TypeError, ValueError):
            pass
    prices["USDC"] = 1.0
    return prices, source


def usd_map(bals, prices):
    return {t: float(bals.get(t) or 0) * float(prices.get(t) or (1.0 if t == "USDC" else 0)) for t in BOOK}


def cost_and_pnl(bals, prices, cb, funded=None):
    """Live portfolio USD and unrealized % vs funded capital (deposits).

    funded: explicit net-funded USD for this wallet. When None, falls back to
    wallet funding.funded_usd. tokens_usd avg costs are not used for wallet
    performance (they remain for trading rules only).
    """
    live = sum(usd_map(bals, prices).values())
    if funded is None:
        funded = savings_mode.funded_usd(cb)
    try:
        base = float(funded or 0)
    except (TypeError, ValueError):
        base = 0.0
    pnl = savings_mode.pnl_vs_funded_pct(live, base)
    return live, base, pnl


def day_n(today: date, days: dict | None = None) -> int:
    """Day 1 is this install's first morning check (stored in daily-balance-days.json)."""
    days = days if isinstance(days, dict) else {}
    try:
        d1 = date.fromisoformat(str(days.get("day1_date")))
    except Exception:
        d1 = today
        days["day1_date"] = today.isoformat()
    return (today - d1).days + 1


def size_caps(cb, bals, prices, sold, heaviest_usd_budget):
    size_lim = cb.get("size_limits") or {}
    max_frac = float(size_lim.get("max_sell_fraction_of_balance", 0.4))
    min_sol = float(size_lim.get("min_sol_fee_buffer", 0.02))
    pct = float(size_lim.get("max_trade_portfolio_pct", 15))
    combined = float(savings_mode.combined_portfolio_usd(cb, bals, prices) or 0)
    if combined <= 0:
        combined = sum(usd_map(bals, prices).values())
    max_usd = combined * pct / 100.0
    bal = float(bals.get(sold) or 0)
    px = 1.0 if sold == "USDC" else float(prices.get(sold) or 0)
    if px <= 0 or bal <= 0:
        return 0.0, 0.0
    by_frac = bal * max_frac
    if sold == "SOL":
        by_frac = min(by_frac, max(0.0, bal - min_sol))
    by_usd = max_usd / px
    by_budget = heaviest_usd_budget / px if heaviest_usd_budget > 0 else by_usd
    sell_amt = min(by_frac, by_usd, by_budget, bal)
    return sell_amt, sell_amt * px


def net_ok(sold, bought, sell_amt, buy_amt, gas_sol, prices):
    """Must not lose after fees beyond dust. No edge % required."""
    sold_px = 1.0 if sold == "USDC" else float(prices.get(sold) or 0)
    bought_px = 1.0 if bought == "USDC" else float(prices.get(bought) or 0)
    sol_px = float(prices.get("SOL") or 0)
    if sold_px <= 0 or bought_px <= 0:
        return False, None
    in_usd = sell_amt * sold_px
    out_usd = buy_amt * bought_px
    gas_usd = float(gas_sol or 0) * sol_px
    net = out_usd - in_usd - gas_usd
    return net >= -DUST_USD, net


def underweight_targets(bals, prices):
    vals = usd_map(bals, prices)
    total = sum(vals.values())
    if total <= 0:
        return [], vals, total
    out = []
    for t in ("SOL", "cbBTC", "WETH"):
        pct = vals[t] / total * 100.0
        if pct < 5.0 - 1e-9:
            out.append((t, pct, vals[t]))
    return out, vals, total


def heaviest_symbol(bals, prices, exclude=None):
    exclude = set(exclude or [])
    vals = usd_map(bals, prices)
    cand = [(t, vals[t]) for t in BOOK if t not in exclude and vals[t] > 0]
    if not cand:
        return None, 0.0
    cand.sort(key=lambda x: x[1], reverse=True)
    return cand[0]


def append_trade_logs(when, side, sold, sell_amt, bought, buy_amt, rate, pair, sig, bals_after, total_after, note, kind=None):
    import csv
    url = f"https://solscan.io/tx/{sig}"
    row = {
        "time_pt": when, "side": side, "sold_asset": sold, "sold_amount": sell_amt,
        "bought_asset": bought, "bought_amount": buy_amt, "rate": rate,
        "rate_unit": f"{bought}/{sold}" if sold != "USDC" else f"USDC/{bought}",
        "tx": sig, "tx_url": url,
        "sol_after": bals_after.get("SOL"), "usdc_after": bals_after.get("USDC"),
        "cbbtc_after": bals_after.get("cbBTC"), "weth_after": bals_after.get("WETH"),
        "total_usd_after": total_after, "notes": note,
    }
    if kind:
        row["kind"] = kind
        row["counts_as_edge"] = False
    with open(ROOT / "trades.jsonl", "a", encoding="utf-8", newline="") as f:
        f.write(json.dumps(row) + "\n")
    with open(__import__("degen_home").trades_csv_path(), "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            when, side, sold, sell_amt, bought, buy_amt, rate, row["rate_unit"], sig, url,
            bals_after.get("SOL"), bals_after.get("USDC"), bals_after.get("cbBTC"), bals_after.get("WETH"),
            total_after, note,
        ])


def fmt_amt(sym, qty):
    if sym == "USDC":
        return f"{qty:.6f}".rstrip("0").rstrip(".")
    if sym == "SOL":
        return f"{qty:.9f}".rstrip("0").rstrip(".")
    if sym == "cbBTC":
        return f"{qty:.8f}".rstrip("0").rstrip(".")
    return f"{qty:.8f}".rstrip("0").rstrip(".")


def fmt_usd(x):
    return f"${x:,.2f}"


UW_STATE_PATH = ROOT / "morning-underweight-state.json"
UW_MAX_USD = 20.0
UW_MIN_USD = 1.0


def _uw_cfg(cb):
    return (cb or {}).get("underweight_double") or {}


def _uw_state(today_iso):
    try:
        st = json.loads(UW_STATE_PATH.read_text())
    except Exception:
        st = {}
    if st.get("date") != today_iso:
        st = {"date": today_iso, "filled": []}
    return st


def _uw_save_state(st):
    fade_trend.atomic_write_json(str(UW_STATE_PATH), st)


def _uw_gates(cb):
    """Same Fear & Greed + Kraken VWAP gates the edge check uses. Fail closed."""
    import market_gates as _MG
    try:
        return _MG.Gates(cb)
    except Exception as e:
        return _MG.Gates(cb, fng={"value": None, "error": type(e).__name__},
                         vwap={"error": type(e).__name__})


def _uw_quote_net(FR, sold, bought, sell_amt, prices):
    """Best net of Jupiter manual (fee 0) and Raydium, with the fee-inclusive net test."""
    sol_usd = float(prices.get("SOL") or 0)
    bought_px = 1.0 if bought == "USDC" else float(prices.get(bought) or 0)
    best, _rows, started = FR.quote_winning_route(sold, bought, sell_amt, sol_usd, bought_px)
    if not best:
        return None, None, started, "no_quote"
    buy_amt = int(best["out_amount"]) / (10 ** DEC[bought])
    ok, net = net_ok(sold, bought, sell_amt, buy_amt, best.get("gas_sol"), prices)
    if not ok:
        return None, net, started, "net_loss_beyond_dust"
    return best, net, started, None


def _uw_build(VQ, best, sold, bought):
    if best["venue"] == "jupiter_manual":
        tx_b64, err = VQ.build_jupiter_manual_tx(best.get("payload"))
        return ([tx_b64] if tx_b64 else []), err
    if best["venue"] == "raydium":
        txs, err = VQ.build_raydium_tx(best.get("swap_response"), sold, bought)
        return list(txs or []), err
    return [], f"venue:{best.get('venue')}"


def _uw_execute(cb, sold, bought, sell_amt, prices, dry_run=False):
    """One underweight_double leg. Same send path as Friday RSI / edge
    (savings_mode.send_built_txs -> solana_rpc.submit_bundle: dupsend guard,
    wait-then-retry, Raydium CU fee floor via venue_quotes). Net re-checked on
    every quote, including the stale re-quote and the retry rebuild."""
    import friday_rsi as FR
    pair = FR._pair_of(sold, bought)
    best, net, started, why = _uw_quote_net(FR, sold, bought, sell_amt, prices)
    if not best:
        return {"sent": False, "error": why, "pair": pair, "net_usd": net}
    if dry_run:
        return {"sent": False, "error": "dry_run", "pair": pair, "venue": best.get("venue"), "net_usd": net}
    txs, err = _uw_build(VQ, best, sold, bought)
    if err or not txs:
        return {"sent": False, "error": err or "no_tx", "pair": pair}
    if time.monotonic() - started > FR.QUOTE_STALE_SEC:
        best, net, started, why = _uw_quote_net(FR, sold, bought, sell_amt, prices)
        if not best:
            return {"sent": False, "error": f"stale_requote:{why}", "pair": pair}
        txs, err = _uw_build(VQ, best, sold, bought)
        if err or not txs:
            return {"sent": False, "error": err or "no_tx", "pair": pair}
    holder = {"best": best}

    def _rebuild():
        b2, _n2, _s2, _w2 = _uw_quote_net(FR, sold, bought, sell_amt, prices)
        if not b2:
            return None
        built, e2 = _uw_build(VQ, b2, sold, bought)
        if e2 or not built:
            return None
        holder["best"] = b2
        return built

    kp = savings_mode._load_keypair()
    sig, err_s, uncertain = savings_mode.send_built_txs(txs, kp, _rebuild, accept_unconfirmed=False)
    if err_s or not sig:
        return {"sent": False, "error": err_s or "send_failed", "pair": pair, "submitted": bool(uncertain)}
    best = holder["best"]
    buy_amt = int(best["out_amount"]) / (10 ** DEC[bought])
    base, quote = FR.PAIR_BASE_QUOTE[pair]
    amts = {sold: float(sell_amt), bought: float(buy_amt)}
    fill_rate = amts[quote] / amts[base]
    when = now_pt().isoformat()
    FR.record_rebalance_fill(pair, sold, bought, sell_amt, buy_amt, fill_rate, sig, when, "underweight_double")
    return {
        "sent": True, "signature": sig, "pair": pair, "sold": sold, "bought": bought,
        "sell_amt": sell_amt, "buy_amt": buy_amt, "fill_rate": fill_rate,
        "kind": "underweight_double", "venue": best.get("venue"), "gas_sol": best.get("gas_sol"),
        "when_pt": when, "net_usd": net,
    }


def plan_underweight(cb, bals, prices, gates=None, filled_today=()):
    """Pure planning (no network unless gates is None and a target exists)."""
    plan, notes = [], []
    targets, vals, total = underweight_targets(bals, prices)
    targets = [t for t in targets if t[0] not in set(filled_today)]
    if not targets:
        return plan, notes, gates
    work = dict(bals)
    for target, pct, tusd in targets:
        sold, sold_usd = heaviest_symbol(work, prices, exclude={target})
        row = {"target": target, "pct": round(pct, 4), "sold": sold}
        if not sold:
            row["error"] = "no_funding_holding"; notes.append(row); continue
        if sold == "USDC" or target == "USDC":
            if gates is None:
                gates = _uw_gates(cb)
            rule = gates.block(sold, target)
            if rule:
                row["error"] = f"market_gate:{rule}"; notes.append(row); continue
        need_usd = float(tusd)  # doubling quantity = buying its current USD value
        budget = min(need_usd, UW_MAX_USD)
        sell_amt, sell_usd = size_caps(cb, work, prices, sold, budget)
        px = 1.0 if sold == "USDC" else float(prices.get(sold) or 0)
        if px > 0 and sell_amt * px > UW_MAX_USD:
            sell_amt = UW_MAX_USD / px
        if gates is None:
            gates = _uw_gates(cb)
        mult = gates.size_mult()  # FNG > 75 halving, as in the edge check
        sell_amt *= mult
        sell_usd = sell_amt * px
        row.update({"sell_amt": sell_amt, "sell_usd": round(sell_usd, 4), "need_usd": round(need_usd, 4),
                    "greed_mult": mult, "remainder_usd": round(max(0.0, need_usd - sell_usd), 4)})
        if sell_usd < UW_MIN_USD:
            row["error"] = "below_min_size"; notes.append(row); continue
        plan.append(row)
        work[sold] = float(work.get(sold) or 0) - sell_amt
    return plan, notes, gates


def do_underweight(cb, bals, prices, dry_run=False, executor=None):
    """Morning 5% top-up (re-enabled 2026-10-10, owner). Daily morning check only.

    For SOL/cbBTC/WETH under 5% of the SOL+USDC+cbBTC+WETH book, buy enough to
    double its quantity, funded by the heaviest holding. <= $20/trade, <= 40% of
    sold balance, 0.02 SOL buffer, fee-inclusive net not a loss beyond dust, no
    edge %. One fill per asset per PT morning. Fear & Greed / VWAP gates only on
    USDC legs (plus the >75 size halving). Fills are kind underweight_double.
    """
    cfg = _uw_cfg(cb)
    if not cfg.get("enabled"):
        return [], "morning 5% top-up disabled"
    if ((cb or {}).get("daily_rebalance_v2") or {}).get("enabled"):
        # dailyv2b 2026-10-11: retired while daily_rebalance_v2 is live (its 10% floor covers it)
        return [], "morning 5% top-up superseded by daily_rebalance_v2"
    today = now_pt().date().isoformat()
    st = _uw_state(today)
    plan, notes, _g = plan_underweight(cb, bals, prices, filled_today=st["filled"])
    results = list(notes)
    if not plan:
        return results, ("no asset under 5%" if not notes else "underweight skipped")
    run = executor or _uw_execute
    cur = dict(bals)  # after1012
    for row in plan:
        res = run(cb, row["sold"], row["target"], row["sell_amt"], prices, dry_run=dry_run)
        res = dict(res or {})
        res.setdefault("target", row["target"])
        res["plan"] = row
        results.append(res)
        if res.get("sent"):
            st["filled"].append(row["target"])
            _uw_save_state(st)
            try:
                import after_bals as _AB
                after, _src = _AB.read_after(MAIN_WALLET, cur, res["sold"], res["bought"], res["sell_amt"], res["buy_amt"])
                cur = dict(after)
                res["after_source"] = _src
                total_after = round(sum(usd_map(after, prices).values()), 4)
                append_trade_logs(
                    res["when_pt"], f"{res['sold']}\u2192{res['bought']}", res["sold"], res["sell_amt"],
                    res["bought"], res["buy_amt"], res["fill_rate"], res["pair"], res["signature"],
                    after, total_after,
                    f"UNDERWEIGHT_DOUBLE {row['target']} was {row['pct']:.2f}% of book; "
                    f"{res.get('venue')} local signer wallet; remainder_usd {row['remainder_usd']}",
                    kind="underweight_double",
                )
            except Exception as e:
                res["log_error"] = type(e).__name__
    return results, "underweight ran"


def main():
    when = now_pt()
    today = when.date()
    cb = json.loads((ROOT / "cost-basis.json").read_text())
    try:  # fresh install: no day file yet
        days = json.loads((ROOT / "daily-balance-days.json").read_text())
    except (FileNotFoundError, ValueError):
        days = {}
    n = day_n(today, days)

    main_wallet, main_wallet_held = _guard_main_wallet(cb)
    legacy, legacy_held = {t: 0.0 for t in BOOK}, {}  # template: one wallet only
    prices, px_source = jupiter_prices()

    # Merge held into cb-aware combined before underweight/hooks
    for sym, qty in main_wallet_held.items():
        if qty:
            main_wallet[sym] = qty  # savings_mode may read these

    r_funded = savings_mode.funded_usd(cb)
    c_funded = savings_mode.legacy_funded_usd(cb)
    r_live, r_cost, r_pnl = cost_and_pnl(main_wallet, prices, cb, funded=r_funded)
    c_live, c_cost, c_pnl = cost_and_pnl(legacy, prices, cb, funded=c_funded)
    both_bals = {t: float(main_wallet.get(t) or 0) + float(legacy.get(t) or 0) for t in BOOK}
    # Combined: live sum only. 
    b_live = sum(usd_map(both_bals, prices).values())
    b_cost = None
    b_pnl = None

    # daily_rebalance_v2 (dailyv2e): once per slot (6:59 AM / 4:59 PM PT, buyside1012); live only when enabled. Saved cbBTC excluded.
    try:
        import daily_rebalance_v2 as _DV2
        v2_results, v2_note = _DV2.run_morning(
            cb, savings_mode.trading_balances(cb, {t: float(main_wallet.get(t) or 0) for t in BOOK}), prices)
    except Exception as e:
        v2_results, v2_note = [{"error": f"{type(e).__name__}: {e}"}], "daily_rebalance_v2 error"
    if any(r.get("sent") for r in v2_results):
        main_wallet, main_wallet_held = _guard_main_wallet(cb)
        for sym, qty in main_wallet_held.items():
            if qty:
                main_wallet[sym] = qty
        cb = json.loads((ROOT / "cost-basis.json").read_text())

    # Underweight on wallet trading book only (pre-hook). Saved cbBTC excluded.
    uw_results, uw_note = do_underweight(
        cb, savings_mode.trading_balances(cb, {t: float(main_wallet.get(t) or 0) for t in BOOK}), prices
    )

    # Re-read wallet after fills
    if any(r.get("sent") for r in uw_results) or any(r.get("sent") for r in v2_results):
        main_wallet, main_wallet_held = _guard_main_wallet(cb)
        for sym, qty in main_wallet_held.items():
            if qty:
                main_wallet[sym] = qty
        r_funded = savings_mode.funded_usd(cb)
        r_live, r_cost, r_pnl = cost_and_pnl(main_wallet, prices, cb, funded=r_funded)
        both_bals = {t: float(main_wallet.get(t) or 0) + float(legacy.get(t) or 0) for t in BOOK}
        b_live = sum(usd_map(both_bals, prices).values())
        b_cost, b_pnl = None, None
        cb = json.loads((ROOT / "cost-basis.json").read_text())

    # Savings / memecoin morning hooks (do not change streak/multipliers)
    hook_notes = []
    try:
        h1 = savings_mode.morning_balance_hook(
            cb, savings_mode.trading_balances(cb, {t: float(main_wallet.get(t) or 0) for t in BOOK}), prices, execute=True
        )
        hook_notes.append({"savings": h1})
        cb = json.loads((ROOT / "cost-basis.json").read_text())
    except Exception as e:
        hook_notes.append({"savings_error": f"{type(e).__name__}: {e}"})
    try:
        h2 = memecoin_mode.morning_balance_hook(
            cb, savings_mode.trading_balances(cb, {t: float(main_wallet.get(t) or 0) for t in BOOK}), prices, execute=True
        )
        hook_notes.append({"memecoin": h2})
        cb = json.loads((ROOT / "cost-basis.json").read_text())
    except Exception as e:
        hook_notes.append({"memecoin_error": f"{type(e).__name__}: {e}"})

    # Persist deposit_watch / funding.funded_usd when a hook marked dirty
    # (same path edge check uses when savings/memecoin hooks dirty the book).
    hook_dirty = False
    for note in hook_notes:
        if not isinstance(note, dict):
            continue
        for status in note.values():
            if isinstance(status, dict) and status.get("dirty"):
                hook_dirty = True
    if hook_dirty:
        fade_trend.atomic_write_json(str(ROOT / "cost-basis.json"), cb)

    if any("sent" in str(h) and True for h in hook_notes):
        # refresh if a savings/memecoin buy happened
        main_wallet, main_wallet_held = _guard_main_wallet(cb)
        r_funded = savings_mode.funded_usd(cb)
        c_funded = savings_mode.legacy_funded_usd(cb)
        r_live, r_cost, r_pnl = cost_and_pnl(main_wallet, prices, cb, funded=r_funded)
        c_live, c_cost, c_pnl = cost_and_pnl(legacy, prices, cb, funded=c_funded)
        both_bals = {t: float(main_wallet.get(t) or 0) + float(legacy.get(t) or 0) for t in BOOK}
        b_live = sum(usd_map(both_bals, prices).values())
        b_cost, b_pnl = None, None

    r_vals = usd_map(main_wallet, prices)
    c_vals = usd_map(legacy, prices)

    # Underweight status lines (post-fill trading book %; saved cbBTC excluded)
    uw_status = []
    r_trade_vals = usd_map(savings_mode.trading_balances(cb, {t: float(main_wallet.get(t) or 0) for t in BOOK}), prices)
    tot_r = sum(r_trade_vals.values()) or 1.0
    for t in ("SOL", "cbBTC", "WETH"):
        pct = r_trade_vals[t] / tot_r * 100.0
        if pct < 5.0:
            uw_status.append(f"{t} {pct:.2f}%")
    fills_sent = [r for r in uw_results if r.get("sent")]

    trade_fails = []
    fail_prefixes = ("send_failed", "build_failed", "quote_failed", "no_ok_quote")
    for note in hook_notes:
        if not isinstance(note, dict):
            continue
        for label, status in note.items():
            if label.endswith("_error"):
                trade_fails.append({"where": label, "reason": status})
                continue
            if not isinstance(status, dict):
                continue
            reason = str(status.get("reason") or "")
            if reason.startswith(fail_prefixes):
                trade_fails.append({
                    "where": label,
                    "route": status.get("route") or status.get("fund_symbol"),
                    "venue": status.get("venue"),
                    "reason": reason,
                })
            for ex in status.get("extra_failures") or []:
                if isinstance(ex, dict):
                    trade_fails.append({
                        "where": f"{label}:{ex.get('symbol')}",
                        "route": f"{ex.get('fund_symbol')}->{ex.get('symbol')}",
                        "venue": ex.get("venue"),
                        "reason": ex.get("reason"),
                        "latch_advanced": bool(status.get("latch_advanced")),
                    })
    for fail in trade_fails:
        print("TRADE_FAIL", json.dumps(fail, default=str), flush=True)
    if trade_fails:
        print(f"NOTIFY_USER trade_failed count={len(trade_fails)}", flush=True)

    report = {
        "day": n,
        "when_pt": when.isoformat(),
        "prices": prices,
        "price_source": px_source,
        "main_wallet": {
            "address": MAIN_WALLET,
            "balances": {t: float(main_wallet.get(t) or 0) for t in BOOK},
            "usd": r_vals,
            "total_usd": r_live,
            "funded_usd": r_cost,
            "unrealized_pnl_pct": r_pnl,
            "held": main_wallet_held,
            "savings_held": savings_mode.savings_held_summary(cb, prices),
        },
        "combined": {
            "total_usd": b_live,
            "note": "trading wallet",
            "funded_usd": None,
            "unrealized_pnl_pct": None,
        },
        "underweight_remaining": uw_status,
        "underweight_results": [{k: v for k, v in r.items() if k != "signature"} for r in uw_results],
        "daily_rebalance_v2": {"note": v2_note, "results": [{k: v for k, v in r.items() if k != "signature"} for r in v2_results]},
        "hooks": hook_notes,
        "trade_fails": trade_fails,
    }

    bt_note = None  # template: backtest-data recording removed

    # Persist day counter
    days.update({
        "last_sent_day": n,
        "last_sent_pt_date": today.isoformat(),
        "this_morning_was_day": n,
        "updated_pt": when.isoformat(),
        "note_sent": (
            f"{today.isoformat()} morning Day {n} delivered. "
            f"wallet ${r_live:.2f} pnl={None if r_pnl is None else round(r_pnl, 2)}%; "
            f"total ${b_live:.2f}. "
            f"underweight_fills={len(fills_sent)} remaining={uw_status or 'none'}"
        ),
    })
    (ROOT / "daily-balance-days.json").write_text(json.dumps(days, indent=2) + "\n")

    # Log snippet
    r_line = (
        f"- wallet `{MAIN_WALLET}`: "
        + ", ".join(f"{t}={fmt_amt(t, main_wallet.get(t) or 0)} ({fmt_usd(r_vals[t])})" for t in BOOK)
        + f" | total {fmt_usd(r_live)}"
        + (f" | unrealized {r_pnl:+.2f}% vs {fmt_usd(r_cost)} funded" if r_pnl is not None else "")
    )
    lines = [
        f"## {when.strftime('%Y-%m-%dT%H:%M:%S%z')} Day {n} morning wallet check",
        r_line,
        f"- marks {px_source}",
    ]
    if fills_sent:
        for fr in fills_sent:
            lines.append(
                f"- underweight FILL: {fr.get('venue')} {fr.get('sold')}\u2192{fr.get('bought')} "
                f"sold {fr.get('sell_amt')} bought {fr.get('buy_amt')} rate {fr.get('fill_rate')} "
                f"(sig omitted per fill-report rule)"
            )
    elif uw_results:
        for fr in uw_results:
            lines.append(f"- underweight: {fr.get('target')} — SKIP ({fr.get('error')})")
    else:
        lines.append(f"- underweight: {uw_note or 'ok'}")
    lines.append(f"- daily_rebalance_v2: {v2_note}" + "".join(
        f" | {r.get('sold')}->{r.get('bought')} {r.get('sell_amt')}" for r in v2_results if r.get("sent")))
    if uw_status:
        lines.append(f"- still under 5% after top-up: {', '.join(uw_status)}")
    if bt_note:
        lines.append(f"- BACKTEST_DATA_OVER_50MB: backtest-data is {bt_note['mb']} MB (limit 50 MB); archive or delete old day files")
    elif report.get("backtest_data"):
        lines.append(f"- backtest-data: {report['backtest_data']['mb']} MB of 50 MB")
    with open(ROOT / "sol-usdc-log.md", "a", encoding="utf-8", newline="") as f:
        f.write("\n" + "\n".join(lines) + "\n")

    out_path = ROOT / "morning-balance-last.json"
    out_path.write_text(json.dumps(report, indent=2, default=str) + "\n")

    # Dashboard snapshot every morning (after writing morning-balance-last.json). Desktop sync CANCELLED.
    try:
        import dashboard_snapshot as _dash
        _r = "morning_underweight_fill" if (fills_sent or any(r.get("sent") for r in v2_results)) else "morning_balance"
        _dash.refresh(reason=_r)
        print(f"DASHBOARD_SNAPSHOT reason={_r}", flush=True)
    except Exception as _dash_err:
        print(f"DASHBOARD_SNAPSHOT_ERR {type(_dash_err).__name__}", flush=True)

    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    if _DH.MIRROR_FLAG.exists() and "--dry-run" not in _sys.argv[1:]:
        # Box mirror guard (pcmove 2026-10-10): live trading runs on the owner's PC only.
        print("MIRROR_ONLY present: this copy is a mirror and must not trade; refusing live run (live bot runs on the PC)", flush=True)
        raise SystemExit(3)
    _rc = 1
    import jobs_lock as _JL  # bugfix1012: edge check skips while this job runs
    _jl = None if "--dry-run" in _sys.argv[1:] else _JL.acquire("morning_balance")
    try:
        _rc = main()
    finally:
        if _jl:
            _JL.release(_jl)
        # deskjson 2026-10-11: daily 2-day log trim removed; size valve in box_runner.
        pass
    raise SystemExit(_rc)
