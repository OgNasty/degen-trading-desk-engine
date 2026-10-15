import os, sys, tempfile as _tf
from pathlib import Path as _P
_T = _tf.mkdtemp(prefix="degen-test-")
os.environ["DEGEN_HOME"] = _T
os.environ.setdefault("PAPER_TRADING", "true")
_P(_T, ".env").write_text("HELIUS_RPC_URL=https://rpc.invalid/test\nPAPER_TRADING=true\n")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
"""Offline (jitopx1014 / chartdip1014): late_fill/backfilled rows without a wallet total make no chart dip;
JitoSOL is never priced as plain SOL when its price fetch fails."""
import ast, json, os, tempfile, types
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import desk_json as DJ

def _row(t, total, **kw):
    r = {"time_pt": t, "side": "SOL→USDC", "sold_asset": "SOL", "sold_amount": 0.1, "bought_asset": "USDC",
         "bought_amount": 10, "tx_url": "", "total_usd_after": total}
    r.update(kw); return r

def test_late_fill_row_makes_no_dip():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "trades.jsonl"
        rows = [_row("2026-10-05T10:00:00-07:00", 300.0), _row("2026-10-07T10:00:00-07:00", 310.0),
                _row("2026-10-14T19:46:45-07:00", 456.11),
                # appended last, dated in the past, no total: what late_fills.py / audits write
                _row("2026-10-06T10:26:59-07:00", None, kind="late_fill", backfilled=True),
                _row("2026-10-06T11:00:00-07:00", 0, kind="late_fill", backfilled=True)]
        p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        d2 = DJ.build(Path(d) / "nolog.md", p)
        assert d2["cs"] == [300.0, 310.0, 456.11], d2["cs"]
        assert d2["n"] == 5  # still counted as trades
        assert [r[0] for r in d2["r"]] == sorted(r[0] for r in d2["r"])

def _price_fn():
    src = open("_edge_check_run.py", encoding="utf-8").read()
    tree = ast.parse(src)
    keep = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name == "jitosol_usd_price") or
            (isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") in ("JITOSOL_PX_FILE", "JITOSOL_SOL_RATE_DEFAULT"))]
    g = {"json": json, "datetime": datetime, "PT": ZoneInfo("America/Phoenix"), "_DH": types.SimpleNamespace(HOME_STR=".")}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "x", "exec"), g)
    return g["jitosol_usd_price"]

def test_jitosol_never_sol_price():
    f = _price_fn()
    with tempfile.TemporaryDirectory() as d:
        assert abs(f(None, 100.0, home=d) - 130.0) < 1e-9          # no history: SOL x 1.30, not SOL
        assert abs(f(133.79, 102.38, home=d) - 133.79) < 1e-9       # live wins and is saved
        px = f(None, 110.0, home=d)                                  # fetch failed: saved rate
        assert abs(px - 110.0 * 133.79 / 102.38) < 1e-6 and px != 110.0
        assert abs(f(None, 0, home=d) - 133.79) < 1e-9              # no SOL price: last good

def test_wallet_totals_use_jitosol_price():
    import savings_mode as SM
    old = SM.JITOSOL_USD
    try:
        cb = {"savings_mode": {"jitosol_qty": 0.044509886}}
        p = {"SOL": 102.38, "USDC": 1.0, "cbBTC": 80000.0, "WETH": 2380.0}
        SM.JITOSOL_USD = None
        assert abs(SM.jitosol_px(p) - 102.38 * 1.30) < 1e-9        # never plain SOL
        SM.JITOSOL_USD = 133.79
        assert abs(SM.jitosol_mark_usd(cb, {}, p) - 0.044509886 * 133.79) < 1e-9
        assert abs(SM.jitosol_mark_usd(cb, {}, dict(p, JitoSOL=140.0)) - 0.044509886 * 140.0) < 1e-9
    finally:
        SM.JITOSOL_USD = old

if __name__ == "__main__":
    test_late_fill_row_makes_no_dip(); test_jitosol_never_sol_price(); test_wallet_totals_use_jitosol_price(); print("OK chartdip/jitopx")
