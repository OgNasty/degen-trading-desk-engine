#!/usr/bin/env python3
"""Check-in counts (quiet one-line SKIPs, one would-be fill per check) and the single awaiting-deposit notice
for a new empty wallet (seed gate). Offline: python tests/test_checkin_awaiting.py"""
import os, sys, tempfile, json, io, types, subprocess, contextlib
from pathlib import Path
from datetime import datetime, timedelta
TMP = tempfile.mkdtemp(prefix="degen-test-")
os.environ["DEGEN_HOME"] = TMP
os.environ.setdefault("PAPER_TRADING", "true")
Path(TMP, ".env").write_text("HELIUS_RPC_URL=https://rpc.invalid/test\nPAPER_TRADING=true\n")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
Path(TMP, "cost-basis.json").write_text((ROOT / "seeds" / "cost-basis.seed.json").read_text())
Path(TMP, "allowed-pairs.json").write_text((ROOT / "seeds" / "allowed-pairs.json").read_text())
Path(TMP, "config.json").write_text(json.dumps({"seed_mode": "deposit", "run_side": "box", "paper_trading": True}))
import box_runner as BR
import checkin_report as CR
import seed_prices as SP


def test_checkin_counts():
    d = Path(TMP, "runner-logs"); d.mkdir(exist_ok=True)
    now = datetime.now().astimezone(); out = []
    run = {"ok": True, "returncode": 0, "timed_out": False, "elapsed_sec": 3,
           "combined": 'LOG_SUMMARY {"result":"SKIP","send_attempt":0,"closest":"SOL-USDC","gap_pct":0.4,"gap_net_pct":0.3,"quiet":1,"quiet_thr":100,"blocked":0}\nRESULT SKIP'}
    out.append(BR.compact_skip_line((now - timedelta(hours=30)).isoformat(timespec="seconds"), run))  # outside 24h
    for i in range(10):
        out.append(BR.compact_skip_line((now - timedelta(minutes=60 - i)).isoformat(timespec="seconds"), run))
    t = (now - timedelta(minutes=2)).isoformat(timespec="seconds")
    out += [f"===== {t} check start =====", "send error: PAPER_TRADING: send skipped",
            'TRADE_FAIL {"reason": "PAPER_TRADING: send skipped"}', 'LOG_SUMMARY {"result": "FILL_FAILED"}', "RESULT FILL_FAILED",
            f"===== {t} check end rc=0 ====="]
    (d / f"{now:%Y-%m-%d}.log").write_text("\n".join(out) + "\n")
    SP.live_usd_prices = lambda A: {"USDC": 1.0, "SOL": 100.0, "cbBTC": 80000.0, "WETH": 2000.0}
    SP.balances = lambda A: {"SOL": 1.0, "USDC": 10.0, "cbBTC": 0.0, "WETH": 0.0}
    cb = json.loads(Path(TMP, "cost-basis.json").read_text()); cb.setdefault("funding", {})["funded_usd"] = 100.0
    Path(TMP, "cost-basis.json").write_text(json.dumps(cb))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        CR.main()
    r = json.loads(buf.getvalue())
    assert (r["checks"], r["skips"], r["would_be_fills"]) == (11, 10, 1), r
    assert r["wallet_usd_now"] == 110.0 and r["paper_pnl_usd"] == 10.0 and r["paper_pnl_pct"] == 10.0, r


def test_awaiting_deposit_once_from_seed_gate():
    cb = json.loads((ROOT / "seeds" / "cost-basis.seed.json").read_text())
    Path(TMP, "cost-basis.json").write_text(json.dumps(cb))
    Path(TMP, "awaiting-deposit.json").unlink(missing_ok=True)
    sent = []; BR.post_webhook = lambda p: sent.append(p) or 200
    rcs = [2, 2, 2, 0]; real = subprocess.run
    subprocess.run = lambda *a, **k: types.SimpleNamespace(returncode=rcs.pop(0), stdout="", stderr="")
    try:
        assert [BR._template_seed_gate() for _ in range(3)] == [False] * 3
        assert [p["kind"] for p in sent] == ["awaiting_deposit"], sent
        BR._template_seed_gate()
        assert json.loads(Path(TMP, "awaiting-deposit.json").read_text()) == {"sent": False}
    finally:
        subprocess.run = real


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"): v(); print("ok", k)
