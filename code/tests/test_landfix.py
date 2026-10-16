"""1.1.1: post-fill balances wait for the BOUGHT token to land before logging the total."""
import os, sys, tempfile
from pathlib import Path
_HERE = Path(__file__).resolve().parent
if _HERE.name == "tests":  # template layout: isolated paper-mode home; code lives one folder up
    _TMP = tempfile.mkdtemp(prefix="degen-test-")
    os.environ["DEGEN_HOME"] = _TMP
    os.environ.setdefault("PAPER_TRADING", "true")
    Path(_TMP, ".env").write_text("HELIUS_RPC_URL=https://rpc.invalid/test\nPAPER_TRADING=true\n")
    sys.path.insert(0, str(_HERE.parent)); os.chdir(str(_HERE.parent))
import unittest
import after_bals as AB

PRE = {"SOL": 3.0, "USDC": 10.0, "cbBTC": 0.0005, "WETH": 0.017}


class LandFix(unittest.TestCase):
    def test_sold_only_drop_waits_for_bought(self):
        # sold leg dropped but bought token not credited yet -> keep reading
        reads = iter([dict(PRE, SOL=2.8), dict(PRE, SOL=2.8, cbBTC=0.00074)])
        b, src = AB.read_after("W", PRE, "SOL", "cbBTC", 0.2, 0.00024, fetch=lambda a: next(reads), sleep=lambda s: None)
        self.assertEqual(src, "fresh_confirmed"); self.assertAlmostEqual(b["cbBTC"], 0.00074)

    def test_never_lands_uses_expected(self):
        b, src = AB.read_after("W", PRE, "SOL", "cbBTC", 0.2, 0.00024, fetch=lambda a: dict(PRE, SOL=2.8), wait_s=0, sleep=lambda s: None)
        self.assertEqual(src, "computed"); self.assertAlmostEqual(b["cbBTC"], 0.00074); self.assertAlmostEqual(b["SOL"], 2.8)

    def test_edge_post_fill_moved_needs_bought(self):
        import importlib, sys
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_edge_check_run.py"), encoding="utf-8").read()
        i = src.index("def _post_fill_moved"); j = src.index("\ndef ", i + 5)
        ns = {}; exec(src[i:j], ns); f = ns["_post_fill_moved"]
        info = {"sold": "SOL", "bought": "cbBTC", "sell_amt": 0.2, "buy_amt": 0.00024}
        self.assertFalse(f(PRE, dict(PRE, SOL=2.8), info))
        self.assertTrue(f(PRE, dict(PRE, SOL=2.8, cbBTC=0.00074), info))


if __name__ == "__main__":
    unittest.main()
