#!/usr/bin/env python3

import os, sys, tempfile
from pathlib import Path
TMP = tempfile.mkdtemp(prefix="degen-test-")
os.environ["DEGEN_HOME"] = TMP
os.environ.setdefault("PAPER_TRADING", "true")
Path(TMP, ".env").write_text("HELIUS_RPC_URL=https://rpc.invalid/test\nPAPER_TRADING=true\n")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
"""Uncertain sends on any path (edge, 6:59 rebalance, Friday RSI, reversal): when the swap lands late,
late_fills logs it before deposit detection, so nothing is booked as a deposit/withdrawal (audit #8)."""
import json, os, tempfile
import late_fills as LF
import savings_mode as SM
import test_late_fills as TL
import test_depfix as T

U = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

def _flow(label, sold_sol, usdc_got):
    d = tempfile.mkdtemp(); st = os.path.join(d, "s.json"); tr = os.path.join(d, "t.jsonl")
    open(tr, "w").write(json.dumps({"sold_asset": "cbBTC", "bought_asset": "WETH", "sold_amount": 1e-5, "bought_amount": 3e-4}) + "\n")
    chain = {"sigs": [{"signature": "OLD", "err": None, "blockTime": 1}], "tx": {}}
    rpc = lambda m, p: ({"result": list(reversed(chain["sigs"]))} if m == "getSignaturesForAddress" else {"result": chain["tx"].get(p[0])})
    LF.scan(TL.W, rpc, state_path=st, trades_path=tr)
    cb = T.book(); r = T.Run(); r.log = tr
    r.check(cb, dict(T.BASE))                                    # baseline at this log length
    chain["sigs"].append({"signature": label, "err": None, "blockTime": 1760496345})
    chain["tx"][label] = TL.tx(-sold_sol, (U, T.BASE["USDC"], T.BASE["USDC"] + usdc_got))
    rows = LF.scan(TL.W, rpc, state_path=st, trades_path=tr)
    assert len(rows) == 1 and rows[0]["sold_asset"] == "SOL" and rows[0]["bought_asset"] == "USDC"
    b = dict(T.BASE, SOL=T.BASE["SOL"] - sold_sol, USDC=T.BASE["USDC"] + usdc_got)
    d1 = r.check(cb, b); d2 = r.check(cb, b)
    assert d1["deposit_usd"] == 0 and d1["withdrawal_usd"] == 0 and d2["deposit_usd"] == 0 and d2["withdrawal_usd"] == 0, (d1, d2)
    assert cb["funding"]["funded_usd"] == 450.59
    return rows[0]

def test_edge_late():      _flow("EDGE", 0.05, 5.1)
def test_rebalance_late(): _flow("DAILYV2", 0.35, 36.0)
def test_friday_late():    _flow("FRIDAY", 1.2, 123.0)
def test_reversal_late():  _flow("REV", 0.02, 2.05)

def test_alert_routing():
    import box_runner as BR
    a = BR.decide_alerts(BR.parse_markers('TRADE_FAIL {"where": "edge"}'), {})
    assert [x["kind"] for x in a] == ["trade_fail"]
    a = BR.decide_alerts(BR.parse_markers("- LATE_FILL t SOL\u2192USDC 1 -> 2 tx abc"), {})
    assert [x["kind"] for x in a] == ["late_fill"] and a[0]["counts_toward_fail_streak"] is False
    a = BR.decide_alerts(BR.parse_markers("RESULT SKIP"), {"runner_error_alert": True, "runner_error_summary": "2 consecutive checker failures", "runner_error_lines": ["x"]})
    assert [x["kind"] for x in a] == ["runner_error"]

if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"): v(); print("ok", k)
