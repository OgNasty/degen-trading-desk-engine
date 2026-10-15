#!/usr/bin/env python3
"""depfix offline replay. python tests/test_depfix.py (uses a temp DEGEN_HOME)"""
import os, sys, tempfile, json
from pathlib import Path
TMP = tempfile.mkdtemp(prefix="degen-test-")
os.environ["DEGEN_HOME"] = TMP
os.environ.setdefault("PAPER_TRADING", "true")
Path(TMP, ".env").write_text("HELIUS_RPC_URL=https://rpc.invalid/test\nPAPER_TRADING=true\n")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import copy
import savings_mode as SM

PX = {"SOL": 103.0, "USDC": 1.0, "cbBTC": 81000.0, "WETH": 2370.0, "JitoSOL": 134.0}
BASE = {"SOL": 2.545115556, "USDC": 14.131266, "cbBTC": 0.00061063, "WETH": 0.03663901}

def book():
    return {"funding": {"funded_usd": 450.59, "history": []},
            "savings_mode": {"enabled": True, "usd_high_water": 450.21},
            "memecoin_mode": {"enabled": True, "usd_high_water": 450.13},
            "deposit_watch": {"balances": dict(BASE), "log_lines": 1, "aux_balances": {"JitoSOL": 0.0, "GreedyGoats": 0.0}}}

class Run:
    def __init__(self):
        self.d = tempfile.mkdtemp(); self.log = os.path.join(self.d, "trades.jsonl"); self.rid = 1000
        open(self.log, "w").write(json.dumps({"sold_asset": "cbBTC", "bought_asset": "WETH", "sold_amount": 1e-5, "bought_amount": 3e-4}) + "\n")
    def add(self, sold, sa, bought, ba):
        open(self.log, "a").write(json.dumps({"sold_asset": sold, "sold_amount": sa, "bought_asset": bought, "bought_amount": ba}) + "\n")
    def check(self, cb, bals, jit=0.0):
        self.rid += 1; SM.RUN_ID = self.rid; SM._DETECT_CACHE.clear()
        return SM.apply_external_deposits(cb, bals, PX, aux={"JitoSOL": jit, "GreedyGoats": 0.0}, log_path=self.log)

def test_deposit_alongside_savings_swap():
    r, cb = Run(), book()
    b = dict(BASE, USDC=64.131266)
    d1 = r.check(cb, b)
    assert d1["reason"] == "external_pending_confirm" and SM.buy_block_reason(cb, d1)
    # the old bug: a JitoSOL buy also happened between reads (logged)
    r.add("SOL", 0.0387, "JitoSOL", 0.02967776)
    b2 = dict(b, SOL=BASE["SOL"] - 0.0417)
    d2 = r.check(cb, b2, jit=0.02967776)
    assert d2["reason"] == "external_pending_confirm"  # balances changed: confirm again
    d3 = r.check(cb, b2, jit=0.02967776)
    assert d3["reason"] == "deposit", d3
    assert abs(d3["deposit_usd"] - 50.0) < 0.01 and d3["withdrawal_usd"] == 0
    assert abs(cb["funding"]["funded_usd"] - 500.59) < 0.01
    assert abs(cb["savings_mode"]["usd_high_water"] - 500.21) < 0.01
    assert abs(cb["memecoin_mode"]["usd_high_water"] - 500.13) < 0.01
    assert SM.buy_block_reason(cb, d3) == "deposit_this_check"

def test_unlogged_own_jitosol_buys_not_withdrawal():
    r, cb = Run(), book()
    b = dict(BASE, SOL=BASE["SOL"] - 0.01267)
    for _ in range(2):
        d = r.check(cb, b, jit=0.007421)
    assert d["withdrawal_usd"] == 0 and d["deposit_usd"] == 0, d
    assert cb["funding"]["funded_usd"] == 450.59

def test_logged_edge_fill_is_not_deposit():
    r, cb = Run(), book()
    r.add("SOL", 0.05, "USDC", 5.1)
    b = dict(BASE, SOL=BASE["SOL"] - 0.05001, USDC=BASE["USDC"] + 5.1)
    d = r.check(cb, b)
    assert d["reason"] in ("swap_fill", "sol_fee") and d["deposit_usd"] == 0, d

def test_real_withdrawal():
    r, cb = Run(), book()
    b = dict(BASE, SOL=BASE["SOL"] - 0.5)
    r.check(cb, b); d = r.check(cb, b)
    assert d["reason"] == "outflow" and abs(d["withdrawal_usd"] - 51.5) < 0.1

def test_cached_once_per_check():
    r, cb = Run(), book()
    b = dict(BASE, USDC=64.131266)
    r.check(cb, b); d = r.check(cb, b)
    again = SM.apply_external_deposits(cb, b, PX, log_path=r.log)
    assert again.get("cached") and cb["funding"]["funded_usd"] - 500.59 < 0.01

def test_pause_on_failed_send():
    cfg = {"enabled": True}
    SM.pause_mode_uncertain(cfg, "send_failed:long_wait_used_no_new_sends", "savings")
    assert cfg["enabled"] is False and "paused_reason" in cfg


def _sav_cb(latch):
    cb = book()
    cb["savings_mode"].update({"usd_high_water": latch, "gain_step_usd": 10, "buy_usd": 4, "symbol": "JitoSOL",
                               "mint": "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn", "decimals": 9, "min_sol_buffer": 0.02,
                               "jitosol_skip_steps": 1, "jitosol_next_mult": 0.5,
                               "extra_buys": [{"symbol": "cbBTC", "mint": "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij", "decimals": 8, "buy_usd": 1, "enabled": True}]})
    return cb

def _bals(total):
    b = dict(BASE); b["JitoSOL"] = 0.044509886
    rest = total - sum(b[k] * PX[k] for k in ("USDC", "cbBTC", "WETH")) - 0.044509886 * 134
    b["SOL"] = rest / PX["SOL"]; return b

def _step(cb, primary_ok):
    c = cb["savings_mode"]; c["usd_high_water"] += 10; SM._advance_jitosol_leg(c, primary_ok)

def test_split_legs_schedule():
    cb = _sav_cb(500.21)
    assert SM.plan(cb, _bals(509.9), PX)["action"] != "buy"
    p = SM.plan(cb, _bals(510.5), PX)          # $510.21: cbBTC $1 only
    assert p["action"] == "buy" and p["buy_usd"] == 0 and p.get("jitosol_leg_skipped"), p
    assert abs(SM.plan_extra(cb, SM.extra_buys(cb)[0], _bals(510.5), PX, 0.02)["buy_usd"] - 1.0) < 1e-9
    _step(cb, False)
    p = SM.plan(cb, _bals(520.5), PX)          # $520.21: JitoSOL half $2 + cbBTC $1
    assert p["action"] == "buy" and abs(p["buy_usd"] - 2.0) < 1e-9, p
    assert abs(SM.plan_extra(cb, SM.extra_buys(cb)[0], _bals(520.5), PX, 0.02)["buy_usd"] - 1.0) < 1e-9
    _step(cb, True)
    assert "jitosol_skip_steps" not in cb["savings_mode"] and "jitosol_next_mult" not in cb["savings_mode"]
    assert SM.plan(cb, _bals(529.9), PX)["action"] != "buy"
    p = SM.plan(cb, _bals(530.5), PX)          # $530.21: normal $4 + $1
    assert p["action"] == "buy" and abs(p["buy_usd"] - 4.0) < 1e-9

def test_half_mult_waits_for_real_jitosol_buy():
    cb = _sav_cb(510.21); cb["savings_mode"].pop("jitosol_skip_steps")
    _step(cb, False)  # step advanced by cbBTC only (JitoSOL failed): half stays pending
    assert cb["savings_mode"].get("jitosol_next_mult") == 0.5

if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"): v(); print("ok", k)
