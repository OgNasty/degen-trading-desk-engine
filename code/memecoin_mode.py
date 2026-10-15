"""Memecoin mode for wallet. On when cost-basis memecoin_mode.enabled is true.

The buy latch is this mode's own combined portfolio USD (usd_high_water):
SOL, USDC, cbBTC, and WETH, plus held JitoSOL and Greedy Goats. It is not
gain versus cost, and it is not the savings latch. One $1 Greedy Goats buy
happens only when that total is another $10 above the latch. The latch then
rises by $10 and does not fall, so a dip and a recovery to the same dollar
level does not buy again. A deposit of SOL, USDC, cbBTC, or WETH that is not
a swap fill raises this latch and the savings latch by the deposit USD at
arrival, before the buy test. That deposit is not a gain and does not buy
Greedy Goats. A swap does not move either latch except the +$10 after a real
high-water buy. Greedy Goats is not edge-traded and does not change the
streak or pair multipliers.

The buy is paid only in JitoSOL. If wallet has none, buy enough JitoSOL first
from the heaviest SOL/USDC/cbBTC/WETH holding (leave 0.02 SOL), then swap that
JitoSOL for Greedy Goats. Jupiter manual platformFeeBps=0 versus Raydium, best
after network fees and any Jito tip.

The Greedy Goats leg is tagged memecoin, so the 0.3% price-impact cap does not
apply. The fee-spike skip still applies to both legs. The JitoSOL funding leg
is not tagged memecoin, so the price-impact cap applies there.

This module does not print keys. Importing it does not trade.
"""
import degen_home as _DH  # portable base dir (pcmove 2026-10-10)
import json

import savings_mode
import venue_quotes as VQ

CB_PATH = _DH.HOME_STR + "/cost-basis.json"
MIN_SOL_BUFFER_DEFAULT = 0.02


def _cfg(cb):
    raw = cb.get("memecoin_mode") if isinstance(cb, dict) else None
    return raw if isinstance(raw, dict) else {}


def enabled(cb):
    return _cfg(cb).get("enabled") is True


def symbol_of(cb):
    return str(_cfg(cb).get("symbol") or "GreedyGoats")


def mint_of(cb):
    return str(_cfg(cb).get("mint") or "")


def pay_symbol(cb):
    return str(_cfg(cb).get("pay_symbol") or "JitoSOL")


def is_held_asset(cb, sym):
    """Greedy Goats, by symbol or mint. Not an edge inventory token."""
    s = str(sym or "")
    if not s:
        return False
    if s == symbol_of(cb):
        return True
    mint = mint_of(cb)
    return bool(mint) and s == mint


def _skip(reason, cfg=None):
    cfg = cfg or {}
    return {
        "enabled": cfg.get("enabled") is True,
        "action": "skip",
        "reason": reason,
        "purchased": False,
        "quoted": False,
        "dirty": False,
        "counts_as_edge": False,
        "streak_changed": False,
        "multipliers_changed": False,
        "symbol": cfg.get("symbol") or "GreedyGoats",
        "gains_credited_usd": cfg.get("gains_credited_usd"),
        "usd_high_water": cfg.get("usd_high_water"),
    }


def _jitosol_qty(cb, balances):
    qty = float((balances or {}).get("JitoSOL") or (balances or {}).get(pay_symbol(cb)) or 0)
    return max(0.0, qty)


def _jitosol_px(prices):
    px = float((prices or {}).get("JitoSOL") or 0)
    if px <= 0:
        px = float((prices or {}).get("SOL") or 0)
    return px


def plan(cb, balances, prices):
    """Decide only. No quote, no swap, no book writes.

    The latch is combined portfolio USD (usd_high_water), not gain versus cost.
    One buy is due only when that total is at least gain_step_usd above the latch.
    The latch itself buys nothing. A lower total does not move the latch.
    """
    cfg = _cfg(cb)
    if cfg.get("enabled") is not True:
        return _skip("memecoin_mode_disabled", cfg)
    step = float(cfg.get("gain_step_usd") or 0)
    buy = float(cfg.get("buy_usd") or 0)
    total = savings_mode.combined_portfolio_usd(cb, balances, prices)
    latch_raw = cfg.get("usd_high_water")
    sym = symbol_of(cb)
    pay = pay_symbol(cb)
    sold, sold_usd = savings_mode.heaviest_funding(balances, prices, pay)
    buffer = float(cfg.get("min_sol_buffer") or MIN_SOL_BUFFER_DEFAULT)
    jit_qty = _jitosol_qty(cb, balances)
    jit_px = _jitosol_px(prices)
    jit_usd = jit_qty * jit_px if jit_px > 0 else 0.0
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
        "pay_symbol": pay,
        "pay_mint": cfg.get("pay_mint"),
        "held": True,
        "memecoin": True,
        "portfolio_usd": total,
        "usd_high_water": latch_raw,
        "gains_credited_usd": cfg.get("gains_credited_usd"),
        "gain_step_usd": step,
        "buy_usd": buy,
        "steps": 0,
        "jitosol_qty": jit_qty,
        "jitosol_usd": jit_usd,
        "fund_symbol": sold,
        "fund_usd": sold_usd,
    }
    if latch_raw is None:
        base["action"] = "skip"
        base["reason"] = "high_water_missing"
        return base
    latch = float(latch_raw)
    base["usd_high_water"] = latch
    # One buy. Exactly one step above the latch qualifies; not a stack of buys.
    due = step > 0 and buy > 0 and (total + 1e-6 >= latch + step)
    if not due:
        base["action"] = "skip"
        base["reason"] = "total_below_next_latch"
        return base
    n = 1
    base["steps"] = n
    need = n * buy
    base["buy_usd_total"] = need
    have = jit_usd + 1e-6 >= need and jit_qty > 0
    base["needs_jitosol_buy"] = not have
    if have:
        base["jitosol_sell_amt"] = need / jit_px if jit_px > 0 else 0.0
        base["fund_sell_amt"] = 0.0
        base["action"] = "buy"
        base["reason"] = "high_water_step"
        return base
    short = max(0.0, need - jit_usd)
    room = 0.0 if not sold else savings_mode.sell_amt_for_usd(sold, balances, prices, short, buffer)
    px = float((prices or {}).get(sold) or 0) if sold else 0.0
    room_usd = room * px
    if room_usd + 1e-6 < short or short <= 0:
        # Whole steps that the heaviest holding can fund, if any.
        if not sold or px <= 0 or buy <= 0:
            base["action"] = "skip"
            base["reason"] = "funding_blocked_sol_buffer_or_balance"
            return base
        room_all = savings_mode.sell_amt_for_usd(sold, balances, prices, n * buy, buffer)
        affordable = int((room_all * px + jit_usd + 1e-6) // buy)
        affordable = max(0, min(n, affordable))
        if affordable < 1:
            base["action"] = "skip"
            base["reason"] = "funding_blocked_sol_buffer_or_balance"
            return base
        need = affordable * buy
        short = max(0.0, need - jit_usd)
        n = affordable
        base["steps"] = n
        base["buy_usd_total"] = need
    base["needs_jitosol_buy"] = short > 1e-9
    base["jitosol_buy_usd"] = short
    base["fund_sell_amt"] = 0.0 if short <= 1e-9 or not sold else savings_mode.sell_amt_for_usd(
        sold, balances, prices, short, buffer
    )
    base["jitosol_sell_amt"] = need / jit_px if jit_px > 0 and not base["needs_jitosol_buy"] else None
    base["action"] = "buy"
    base["reason"] = "high_water_step"
    return base


def _register(cfg):
    sym = str(cfg.get("symbol") or "GreedyGoats")
    mint = str(cfg.get("mint") or "")
    pay = str(cfg.get("pay_symbol") or "JitoSOL")
    pay_mint = str(cfg.get("pay_mint") or "")
    if not mint or not pay_mint:
        raise RuntimeError("memecoin_mint_missing")
    VQ.MINTS[sym] = mint
    VQ.MINTS[pay] = pay_mint
    prog = str(cfg.get("token_program") or "")
    if prog:
        VQ.TOKEN_PROGRAMS[mint] = prog
    return sym, pay


def _bad_cfg(cb):
    bcs = cb.get("bad_condition_skip") if isinstance(cb, dict) else None
    bcs = bcs if isinstance(bcs, dict) else {}
    fee = bcs.get("fee_spike") if isinstance(bcs.get("fee_spike"), dict) else {}
    return bcs, fee


def leg_block_reasons(cb, row, memecoin_leg):
    """Price-impact cap, waived only on the tagged Greedy Goats leg. Fee spike always."""
    bcs, fee = _bad_cfg(cb)
    if bcs.get("enabled") is False:
        return []
    reasons = []
    impact = VQ.quote_price_impact_pct(row)
    exempt = (
        bool(memecoin_leg)
        and bcs.get("memecoin_exempt", True) is not False
        and enabled(cb)
    )
    max_pct = bcs.get("price_impact_max_pct", 0.3)
    if (not exempt) and impact is not None and max_pct is not None:
        if float(impact) > float(max_pct):
            reasons.append(f"price_impact_pct={float(impact):.6g}>max_{float(max_pct)}")
    spike = VQ.fee_spike_reason(
        row,
        tip_multiple=float(fee.get("tip_multiple") or 2),
        base_lamports=int(fee.get("base_signature_lamports") or VQ.BASE_SIGNATURE_LAMPORTS),
    )
    if spike:
        reasons.append(spike)
    return reasons


def _pick(rows, sell_usd, sol_usd, out_decimals, cb, memecoin_leg):
    scored = []
    blocked = []
    for row in rows or []:
        reasons = leg_block_reasons(cb, row, memecoin_leg)
        if reasons:
            blocked.append({"venue": row.get("venue") if isinstance(row, dict) else None, "reasons": reasons})
            continue
        net = savings_mode.net_out_ui(row, sell_usd, sol_usd, out_decimals)
        if net is None:
            continue
        scored.append((net, savings_mode.VENUE_RANK.get(row.get("venue"), 0), row))
    if not scored:
        return None, blocked
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    net, _rank, row = scored[0]
    return {"net_out_ui": net, "row": row}, blocked


def _quote(sold, bought, sell_amt, dec_in):
    amount = int(round(float(sell_amt) * (10 ** int(dec_in))))
    if amount <= 0:
        return None, "zero_amount"
    rows = VQ.fetch_venue_quotes(sold, bought, amount)
    return rows, None


def execute_plan(cb, idea, balances, prices):
    """Quote and, only when the plan says buy, submit the JitoSOL then goats swaps.

    Does not touch pair multipliers, the streak, tokens_usd, or the savings latch.
    """
    cfg = _cfg(cb)
    if cfg.get("enabled") is not True:
        return _skip("memecoin_mode_disabled", cfg)
    if not idea or idea.get("action") != "buy":
        out = dict(idea or _skip("no_plan", cfg))
        out["purchased"] = False
        out["quoted"] = False
        return out
    if cfg.get("arm_guard") is True:
        idea = dict(idea)
        idea["action"] = "skip"
        idea["reason"] = "arm_guard_blocks_execute"
        idea["purchased"] = False
        idea["quoted"] = False
        return idea
    sym, pay = _register(cfg)
    dec_pay = int((cb.get("savings_mode") or {}).get("decimals") or 9)
    dec_out = int(cfg.get("decimals") or 6)
    sold = idea.get("fund_symbol")
    sol_usd = float((prices or {}).get("SOL") or 0)
    sigs = []
    quoted = False
    jit_in = _jitosol_qty(cb, balances)
    # Funding leg is not memecoin: 0.3% price impact applies, and so does fee spike.
    if idea.get("needs_jitosol_buy"):
        if sold not in savings_mode.EDGE_INVENTORY or savings_mode.is_savings_asset(cb, sold):
            idea = dict(idea)
            idea["action"] = "skip"
            idea["reason"] = "refusing_non_book_funding"
            idea["quoted"] = False
            return idea
        sell_amt = float(idea.get("fund_sell_amt") or 0)
        rows, err = _quote(sold, pay, sell_amt, savings_mode.DECIMALS[sold])
        quoted = True
        if err or not rows:
            idea = dict(idea)
            idea["action"] = "skip"
            idea["reason"] = "jitosol_quote_failed"
            idea["quoted"] = True
            return idea
        sell_usd = sell_amt * float((prices or {}).get(sold) or 0)
        chosen, blocked = _pick(rows, sell_usd, sol_usd, dec_pay, cb, memecoin_leg=False)
        if not chosen:
            idea = dict(idea)
            idea["action"] = "skip"
            idea["reason"] = "jitosol_leg_blocked" if blocked else "jitosol_no_ok_quote"
            idea["quoted"] = True
            idea["blocked"] = blocked
            return idea
        row = chosen["row"]
        txs, err_b = savings_mode._materialize(row, sold, pay)
        if err_b or not txs:
            idea = dict(idea)
            idea["action"] = "skip"
            idea["reason"] = f"jitosol_build_failed:{err_b}"
            idea["quoted"] = True
            return idea
        kp = savings_mode._load_keypair()

        def _requote_jit():
            rows_q, err_q = _quote(sold, pay, sell_amt, savings_mode.DECIMALS[sold])
            if err_q or not rows_q:
                return None
            chosen_q, _blocked_q = _pick(rows_q, sell_usd, sol_usd, dec_pay, cb, memecoin_leg=False)
            if not chosen_q:
                return None
            txs_q, err_bq = savings_mode._materialize(chosen_q["row"], sold, pay)
            if err_bq or not txs_q:
                return None
            return list(txs_q)

        sig, err_s, uncertain = savings_mode.send_built_txs(txs, kp, _requote_jit, accept_unconfirmed=False)
        if err_s or not sig:
            idea = dict(idea)
            idea["action"] = "skip"
            idea["reason"] = f"jitosol_send_failed:{err_s or 'no_sig'}"
            idea["quoted"] = True
            idea["purchased"] = False
            idea["send_uncertain"] = bool(uncertain)
            return idea
        sigs.append(sig)
        jit_in = int(row["out_amount"]) / (10 ** dec_pay)
        try:
            savings_mode.append_savings_fill_logs(
                cb, {"sold": sold, "sell_amt": sell_amt, "bought": pay, "out_ui": jit_in, "signature": sig,
                     "venue": row.get("venue"), "gas_sol": row.get("gas_sol")},
                savings_mode._project_after(savings_mode.wallet_balances(balances), sold, sell_amt),
                prices, "memecoin_funding", f"{pay} memecoin funding")
        except Exception as _le:
            idea["log_error"] = type(_le).__name__
    if jit_in <= 0:
        idea = dict(idea)
        idea["action"] = "skip"
        idea["reason"] = "no_jitosol"
        idea["quoted"] = quoted
        idea["purchased"] = False
        return idea
    # Spend the JitoSOL this buy needs, not an unrelated pile.
    need = float(idea.get("buy_usd_total") or 0)
    jit_px = _jitosol_px(prices)
    if idea.get("needs_jitosol_buy"):
        sell_jit = jit_in
    elif jit_px > 0:
        sell_jit = min(jit_in, need / jit_px)
    else:
        sell_jit = float(idea.get("jitosol_sell_amt") or 0)
    rows, err = _quote(pay, sym, sell_jit, dec_pay)
    quoted = True
    if err or not rows:
        idea = dict(idea)
        idea["action"] = "skip"
        idea["reason"] = "goats_quote_failed"
        idea["quoted"] = True
        idea["funding_signatures"] = sigs
        return idea
    sell_usd = sell_jit * jit_px if jit_px > 0 else need
    chosen, blocked = _pick(rows, sell_usd, sol_usd, dec_out, cb, memecoin_leg=True)
    if not chosen:
        idea = dict(idea)
        idea["action"] = "skip"
        idea["reason"] = "goats_leg_blocked" if blocked else "goats_no_ok_quote"
        idea["quoted"] = True
        idea["blocked"] = blocked
        idea["funding_signatures"] = sigs
        return idea
    row = chosen["row"]
    txs, err_b = savings_mode._materialize(row, pay, sym)
    if err_b or not txs:
        idea = dict(idea)
        idea["action"] = "skip"
        idea["reason"] = f"goats_build_failed:{err_b}"
        idea["quoted"] = True
        idea["funding_signatures"] = sigs
        return idea
    kp = savings_mode._load_keypair()

    def _requote_goats():
        rows_q, err_q = _quote(pay, sym, sell_jit, dec_pay)
        if err_q or not rows_q:
            return None
        chosen_q, _blocked_q = _pick(rows_q, sell_usd, sol_usd, dec_out, cb, memecoin_leg=True)
        if not chosen_q:
            return None
        txs_q, err_bq = savings_mode._materialize(chosen_q["row"], pay, sym)
        if err_bq or not txs_q:
            return None
        return list(txs_q)

    sig, err_s, uncertain = savings_mode.send_built_txs(txs, kp, _requote_goats)
    if err_s or not sig:
        idea = dict(idea)
        idea["action"] = "skip"
        idea["reason"] = f"goats_send_failed:{err_s or 'no_sig'}"
        idea["quoted"] = True
        idea["purchased"] = False
        idea["funding_signatures"] = sigs
        idea["send_uncertain"] = bool(uncertain)
        return idea
    sigs.append(sig)
    out_ui = int(row["out_amount"]) / (10 ** dec_out)
    try:
        savings_mode.append_savings_fill_logs(
            cb, {"sold": pay, "sell_amt": sell_jit, "bought": sym, "out_ui": out_ui, "signature": sig,
                 "venue": row.get("venue"), "gas_sol": row.get("gas_sol")},
            savings_mode.wallet_balances(balances), prices, "memecoin", f"{sym} memecoin")
    except Exception as _le:
        idea = dict(idea)
        idea["log_error"] = type(_le).__name__
    steps = int(idea.get("steps") or 0)
    step = float(cfg.get("gain_step_usd") or 0)
    # One buy raises this mode's USD latch by one step. It does not fall, and
    # it does not use gain versus cost. Savings keeps its own latch.
    cfg["usd_high_water"] = float(cfg.get("usd_high_water") or 0) + step
    cfg["cost_usd"] = float(cfg.get("cost_usd") or 0) + float(sell_usd or 0)
    cfg["qty"] = float(cfg.get("qty") or 0) + out_ui
    cfg["held"] = True
    cfg["edge_inventory"] = False
    cfg["last_buy"] = {
        "signature": sig,
        "signatures": sigs,
        "venue": row.get("venue"),
        "pay_symbol": pay,
        "bought": sym,
        "bought_amount": out_ui,
        "buy_usd": idea.get("buy_usd_total"),
        "steps": steps,
        "memecoin": True,
        "counts_as_edge": False,
        "usd_high_water": cfg.get("usd_high_water"),
    }
    idea = dict(idea)
    idea["action"] = "bought"
    idea["reason"] = "memecoin_buy"
    idea["purchased"] = True
    idea["quoted"] = True
    idea["dirty"] = True
    idea["signature"] = sig
    idea["signatures"] = sigs
    idea["venue"] = row.get("venue")
    idea["bought_amount"] = out_ui
    idea["usd_high_water"] = cfg["usd_high_water"]
    idea["gains_credited_usd"] = cfg.get("gains_credited_usd")
    idea["counts_as_edge"] = False
    idea["streak_changed"] = False
    idea["multipliers_changed"] = False
    return idea


def consider(cb, balances, prices, source, execute=False):
    """Shared hook. A missing latch, or an arm guard that would buy immediately, is set to the current total instead.

    Deposits are applied first (same helper as savings). The second hook in one
    check sees the updated baseline and does not add the deposit again.
    """
    dep = savings_mode.apply_external_deposits(cb, balances, prices)
    cfg = _cfg(cb)
    if cfg.get("enabled") is not True:
        status = _skip("memecoin_mode_disabled", cfg)
        status["source"] = source
        return savings_mode._note_deposit(status, dep)
    _blk = savings_mode.buy_block_reason(cb, dep)
    if _blk:
        status = _skip(_blk, cfg)
        status["source"] = source
        status["enabled"] = True
        status["dirty"] = bool(dep.get("dirty"))
        return savings_mode._note_deposit(status, dep)
    if cfg.get("usd_high_water") is None:
        total = savings_mode.combined_portfolio_usd(cb, balances, prices)
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
        return savings_mode._note_deposit(status, dep)
    idea = plan(cb, balances, prices)
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
        return savings_mode._note_deposit(idea, dep)
    if cfg.get("arm_guard") is True and idea.get("action") != "buy":
        cfg["arm_guard"] = False
        idea["dirty"] = True
        idea["arm_guard_cleared"] = True
    if idea.get("action") != "buy" or not execute:
        if idea.get("action") == "buy" and not execute:
            idea["reason"] = "plan_only_not_executed"
        return savings_mode._note_deposit(idea, dep)
    # Book-token funding sell on an edge check only. JitoSOL->goats is not one of
    # the four, and morning balance is not this rule. Impact-cap exemption does
    # not waive a fade of the funding token.
    import fade_trend
    if fade_trend.edge_funding_blocked(source, idea.get("fund_symbol"), idea.get("fund_sell_amt")):
        idea = dict(idea)
        idea["action"] = "skip"
        idea["reason"] = "fade_trend"
        idea["purchased"] = False
        idea["quoted"] = False
        idea["dirty"] = False
        idea["counts_as_edge"] = False
        idea["streak_changed"] = False
        idea["multipliers_changed"] = False
        print(f"FADE_TREND_SKIP memecoin fund={idea.get('fund_symbol')}", flush=True)
        return savings_mode._note_deposit(idea, dep)
    done = execute_plan(cb, idea, balances, prices)
    done["source"] = source
    _r = done.get("reason")
    if isinstance(_r, str) and "send_failed" in _r:
        savings_mode.pause_mode_uncertain(cfg, _r, "memecoin")
        done["dirty"] = True
        done["mode_paused"] = True
    return savings_mode._note_deposit(done, dep)


def edge_check_hook(cb, balances, prices, execute=True):
    return consider(cb, balances, prices, source="edge_check", execute=execute)


def morning_balance_hook(cb, balances, prices, execute=True):
    return consider(cb, balances, prices, source="morning_balance", execute=execute)


if __name__ == "__main__":
    book = json.load(open(CB_PATH))
    status = plan(book, {}, {})
    assert status.get("purchased") is False and status.get("quoted") is False
    print("memecoin_selfcheck", status.get("action"), status.get("reason"), "enabled", status.get("enabled"))
