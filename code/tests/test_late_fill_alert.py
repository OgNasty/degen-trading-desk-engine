"""LATE_FILL lines become one 'late_fill' webhook payload; not a fill or trade_fail alert."""
import os, sys, tempfile
from pathlib import Path
TMP = tempfile.mkdtemp(prefix="degen-test-")
os.environ["DEGEN_HOME"] = TMP
Path(TMP, ".env").write_text("HELIUS_RPC_URL=https://rpc.invalid/test\nPAPER_TRADING=true\n")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import box_runner as BR

out = "\n".join([
    "RESULT SKIP",
    "- LATE_FILL 2026-10-14T19:45:45-07:00 SOL\u2192JitoSOL 0.01266978 -> 0.007421068 tx 5DummyLateFi11Signaturexxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "LATE_FILL 2026-10-14T19:45:45-07:00 SOL\u2192JitoSOL 0.01266978 -> 0.007421068 tx 5DummyLateFi11Signaturexxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
])
p = BR.parse_markers(out)
assert len(p["late_fill_lines"]) == 1 and not p["has_trade_fail"] and not p["fill_lines"], p
a = BR.decide_alerts(p, {})
kinds = [x["kind"] for x in a]
assert kinds == ["late_fill"], kinds
x = a[0]
assert x["type"] == "late_fill" and x["counts_toward_fail_streak"] is False and "logged it" in x["summary"]
assert "SOL\u2192JitoSOL" in x["lines"][0] and "0.007421068" in x["lines"][0] and "5DummyLate" not in x["lines"][0]
assert BR.decide_alerts(BR.parse_markers("RESULT SKIP"), {}) == []
print("ok late_fill_alert")
