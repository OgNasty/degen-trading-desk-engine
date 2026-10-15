#!/usr/bin/env python3
"""Deposit/withdrawal edge cases (audit 2026-10-14 #2/#3). Offline."""
import os, sys, tempfile
from pathlib import Path
TMP = tempfile.mkdtemp(prefix="degen-test-")
os.environ["DEGEN_HOME"] = TMP
os.environ.setdefault("PAPER_TRADING", "true")
Path(TMP, ".env").write_text("HELIUS_RPC_URL=https://rpc.invalid/test\nPAPER_TRADING=true\n")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import json
import test_depfix as T
import savings_mode as SM

def test_deposit_while_trade_in_flight():
    """Trade landed (balances moved) but its row is written after the first read, plus a USDC deposit."""
    r, cb = T.Run(), T.book()
    b = dict(T.BASE, SOL=T.BASE["SOL"] - 0.1, WETH=T.BASE["WETH"] + 0.1 * 103 / 2370, USDC=T.BASE["USDC"] + 25)
    d1 = r.check(cb, b)
    assert d1["reason"] == "external_pending_confirm"
    r.add("SOL", 0.1, "WETH", 0.1 * 103 / 2370)          # row lands now
    d2 = r.check(cb, b)
    assert d2["reason"] == "external_pending_confirm", d2  # log changed: re-confirm, no booking yet
    d3 = r.check(cb, b)
    assert d3["reason"] == "deposit" and abs(d3["deposit_usd"] - 25) < 0.01 and d3["withdrawal_usd"] == 0, d3
    assert abs(cb["funding"]["funded_usd"] - 475.59) < 0.01
    assert abs(cb["savings_mode"]["usd_high_water"] - 475.21) < 0.01

def test_multi_token_deposit_one_check():
    r, cb = T.Run(), T.book()
    b = dict(T.BASE, USDC=T.BASE["USDC"] + 20, SOL=T.BASE["SOL"] + 0.2, WETH=T.BASE["WETH"] + 0.004)
    r.check(cb, b); d = r.check(cb, b)
    want = 20 + 0.2 * 103 + 0.004 * 2370
    assert d["reason"] == "deposit" and abs(d["deposit_usd"] - want) < 0.05, d
    assert set(d["deposits"]) == {"USDC", "SOL", "WETH"}

def test_deposit_and_withdrawal_same_check():
    r, cb = T.Run(), T.book()
    b = dict(T.BASE, USDC=T.BASE["USDC"] + 50, WETH=T.BASE["WETH"] - 0.01)   # +$50, -$23.70
    r.check(cb, b); d = r.check(cb, b)
    assert d["reason"] == "deposit_and_outflow", d
    assert abs(d["deposit_usd"] - 50) < 0.01 and abs(d["withdrawal_usd"] - 23.7) < 0.01
    assert abs(cb["funding"]["funded_usd"] - (450.59 + 50 - 23.7)) < 0.01
    assert abs(cb["savings_mode"]["usd_high_water"] - 500.21) < 0.01        # latches never fall

def test_withdrawal_only_and_sol_fee_dust():
    r, cb = T.Run(), T.book()
    b = dict(T.BASE, SOL=T.BASE["SOL"] - 0.00001)
    d = r.check(cb, b)
    assert d["reason"] in ("sol_fee", "unchanged") and cb["funding"]["funded_usd"] == 450.59
    b2 = dict(b, USDC=0.0)
    r.check(cb, b2); d2 = r.check(cb, b2)
    assert d2["reason"] == "outflow" and abs(d2["withdrawal_usd"] - T.BASE["USDC"]) < 0.01

def test_rpc_glitch_single_read_never_books():
    r, cb = T.Run(), T.book()
    d = r.check(cb, dict(T.BASE, USDC=0.0))            # one bad read
    assert d["reason"] == "external_pending_confirm"
    d2 = r.check(cb, dict(T.BASE))                     # back to normal
    assert d2["reason"] in ("unchanged", "sol_fee") and cb["funding"]["funded_usd"] == 450.59
    assert "pending_external" not in cb["deposit_watch"]

if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"): v(); print("ok", k)
