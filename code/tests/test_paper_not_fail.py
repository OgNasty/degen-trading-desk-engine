#!/usr/bin/env python3
"""Paper mode: a skipped send is a would-be fill, never a trade failure (no trade_fail alert, not in the
2-fails-in-a-row count), and the check-in still counts it once. Offline: python tests/test_paper_not_fail.py"""
import os, sys, tempfile, json, io, contextlib
from pathlib import Path
from datetime import datetime
TMP = tempfile.mkdtemp(prefix="degen-test-")
os.environ["DEGEN_HOME"] = TMP
os.environ["PAPER_TRADING"] = "true"
Path(TMP, ".env").write_text("HELIUS_RPC_URL=https://rpc.invalid/test\nPAPER_TRADING=true\n")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for n in ("allowed-pairs.json", "cost-basis.seed.json"):
    Path(TMP, n.replace(".seed", "")).write_text((ROOT / "seeds" / n).read_text())
Path(TMP, "config.json").write_text(json.dumps({"run_side": "box", "paper_trading": True}))
import solana_rpc as SR
import box_runner as BR
import checkin_report as CR
import seed_prices as SP


def _edge_fail_block():
    """The exact trade-fail code path of _edge_check_run.py, run on a paper-skipped send."""
    src = (ROOT / "_edge_check_run.py").read_text(encoding="utf-8")
    start = src.index("trade_fails = _trade_fail_lines()")
    end = src.index('print("\\nRESULT"', start)
    resp = SR.rpc_send("sendTransaction", ["AAAA", {}], "helius")       # paper guard answers, nothing is sent
    assert SR.is_paper_skip(resp), resp
    err = SR.error_line("helius", "sendTransaction", resp)
    fill = [{"where": "edge", "route": "SOL->USDC", "venue": "jupiter", "reason": err}]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(src[start:end], {"_trade_fail_lines": lambda: list(fill), "json": json, "solana_rpc": SR})
    return buf.getvalue()


def test_paper_skip_is_not_a_failure():
    out = _edge_fail_block()
    assert "TRADE_FAIL" not in out and "NOTIFY_USER" not in out, out
    assert out.startswith("PAPER_WOULD_FILL"), out
    parsed = BR.parse_markers(out + "\nRESULT SKIP\n")
    assert not parsed["has_trade_fail"], parsed
    assert "trade_fail" not in [a["kind"] for a in BR.decide_alerts(parsed, {})]
    # even an old-style TRADE_FAIL line carrying the paper message is not a failure
    old = 'TRADE_FAIL {"reason": "PAPER_TRADING: send skipped"}\nRESULT SKIP\n'
    assert not BR.parse_markers(old)["trade_fail_lines"]


def test_real_failure_still_alerts():
    src = (ROOT / "_edge_check_run.py").read_text(encoding="utf-8")
    start = src.index("trade_fails = _trade_fail_lines()"); end = src.index('print("\\nRESULT"', start)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(src[start:end], {"_trade_fail_lines": lambda: [{"where": "edge", "reason": "send_failed:blockhash"}],
                              "json": json, "solana_rpc": SR})
    parsed = BR.parse_markers(buf.getvalue() + "\nRESULT SKIP\n")
    assert parsed["has_trade_fail"] and "trade_fail" in [a["kind"] for a in BR.decide_alerts(parsed, {})]


def test_checkin_counts_paper_once():
    out = _edge_fail_block()
    t = datetime.now().astimezone().isoformat(timespec="seconds")
    d = Path(TMP, "runner-logs"); d.mkdir(exist_ok=True)
    (d / f"{datetime.now().astimezone():%Y-%m-%d}.log").write_text(
        f"===== {t} check start =====\n{out}LOG_SUMMARY {{\"result\": \"SKIP\"}}\nRESULT SKIP\n===== {t} check end rc=0 =====\n")
    SP.live_usd_prices = lambda A: (_ for _ in ()).throw(RuntimeError("offline"))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        CR.main()
    r = json.loads(buf.getvalue())
    assert (r["checks"], r["would_be_fills"]) == (1, 1), r


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"): v(); print("ok", k)
