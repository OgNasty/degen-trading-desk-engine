"""1.1.1: optional H093 preset (off by default). Pure functions plus the cost-basis switch."""
import os, sys, tempfile
from pathlib import Path
_HERE = Path(__file__).resolve().parent
if _HERE.name == "tests":  # template layout: isolated paper-mode home; code lives one folder up
    _TMP = tempfile.mkdtemp(prefix="degen-test-")
    os.environ["DEGEN_HOME"] = _TMP
    os.environ.setdefault("PAPER_TRADING", "true")
    Path(_TMP, ".env").write_text("HELIUS_RPC_URL=https://rpc.invalid/test\nPAPER_TRADING=true\n")
    sys.path.insert(0, str(_HERE.parent)); os.chdir(str(_HERE.parent))
import json, os, tempfile, unittest
import h093 as H


def series(f, n=100):
    return [f(i) for i in range(n)]


class TestSwitch(unittest.TestCase):
    OLD = {"reversal_close_cap_pct": 85, "same_pair_streak_cap": {"max_consecutive_fills": 3},
           "daily_rebalance_v2": {"daily_fraction": 0.1, "reversal_cap_pct": 50}}

    def test_off_gives_old_values(self):
        cb = dict(self.OLD, h093={"enabled": False})
        self.assertEqual(H.rev_cap_pct(cb), 85)
        self.assertEqual(H.quiet_to_halve(cb, 60), 60)
        self.assertEqual(H.streak_cap(cb, 3), 3)
        self.assertEqual(H.daily_fraction(cb, 0.1), 0.1)
        self.assertEqual(H.sell_mult(cb, "SOL", "USDC", {"coins": {"SOL": {"g20_50": True, "ret30": True}}}), 1.0)

    def test_missing_block_is_off(self):
        self.assertFalse(H.enabled(self.OLD)); self.assertEqual(H.rev_cap_pct(self.OLD), 85)

    def test_on_gives_h093_values(self):
        cb = dict(self.OLD, h093={"enabled": True})
        self.assertEqual(H.rev_cap_pct(cb), 60)
        self.assertEqual(H.quiet_to_halve(cb, 60), 30)
        self.assertEqual(H.streak_cap(cb, 3), 5)
        self.assertEqual(H.daily_fraction(cb, 0.1), 0.15)

    def test_daily_v2_fraction_and_caps_untouched(self):
        import daily_rebalance_v2 as D
        on = D.cfg_of(dict(self.OLD, h093={"enabled": True}))
        off = D.cfg_of(dict(self.OLD, h093={"enabled": False}))
        self.assertEqual((on["daily_fraction"], off["daily_fraction"]), (0.15, 0.1))
        self.assertEqual((on["reversal_cap_pct"], off["reversal_cap_pct"]), (50, 50))


class TestDCut(unittest.TestCase):
    ON = {"h093": {"enabled": True}}

    def st(self, g, r):
        return {"coins": {"SOL": {"g20_50": g, "ret30": r}}}

    def test_multipliers(self):
        self.assertEqual(H.sell_mult(self.ON, "SOL", "USDC", self.st(False, False)), 1.0)
        self.assertEqual(H.sell_mult(self.ON, "SOL", "USDC", self.st(True, False)), 0.5)
        self.assertEqual(H.sell_mult(self.ON, "SOL", "USDC", self.st(False, True)), 0.15)
        self.assertAlmostEqual(H.sell_mult(self.ON, "SOL", "USDC", self.st(True, True)), 0.075)

    def test_only_coin_to_usdc(self):
        s = self.st(True, True)
        self.assertEqual(H.sell_mult(self.ON, "SOL", "WETH", s), 1.0)
        self.assertEqual(H.sell_mult(self.ON, "USDC", "SOL", s), 1.0)

    def test_no_state_no_cut(self):
        self.assertEqual(H.sell_mult(self.ON, "SOL", "USDC", None), 1.0)
        self.assertEqual(H.sell_mult(self.ON, "SOL", "USDC", {"coins": {}}), 1.0)

    def test_g20_50_hysteresis(self):
        up = series(lambda i: 100 + i)            # rising: MA20 > MA50 after day 2
        self.assertTrue(H.states_from_closes(up)[0])
        one = series(lambda i: 100.0)             # flat: MA20 == MA50, never on
        self.assertFalse(H.states_from_closes(one)[0])
        # one false day does not switch off; two do
        g = H._hyst([True, True, True, False]); self.assertEqual(g[-1], True)
        g = H._hyst([True, True, True, False, False]); self.assertEqual(g[-1], False)
        g = H._hyst([False, True]); self.assertEqual(g[-1], False)

    def test_ret30_band(self):
        c = [100.0] * 60 + [120.0] * 5            # +20% vs 30d ago -> on
        self.assertTrue(H.states_from_closes(c)[1])
        c2 = c + [108.0] * 30                     # +8% vs 30d ago... (vs 120 it is -10%) -> off below 5%
        self.assertFalse(H.states_from_closes(c2)[1])
        b = H._band([0.2, 0.10, 0.06], 0.15, 0.05); self.assertEqual(b, [True, True, True])   # stays on 5-15%
        b = H._band([0.2, 0.04], 0.15, 0.05); self.assertEqual(b, [True, False])
        b = H._band([0.10], 0.15, 0.05); self.assertEqual(b, [False])

    def test_load_state_cache_and_fallback(self):
        d = tempfile.mkdtemp(); old = H.STATE; H.STATE = os.path.join(d, "s.json")
        try:
            calls = []
            def fetch():
                calls.append(1); return {a: [100.0 + i for i in range(80)] for a in H.COINS}, "t", []
            s1 = H.load_state(self.ON, fetch=fetch, now=86400 * 10 + 5)
            s2 = H.load_state(self.ON, fetch=fetch, now=86400 * 10 + 500)
            self.assertEqual(len(calls), 1); self.assertTrue(s1["coins"]["SOL"]["g20_50"]); self.assertEqual(s1, s2)
            def bad(): raise OSError("down")
            s3 = H.load_state(self.ON, fetch=bad, now=86400 * 11 + 5)      # next day, fetch fails -> cached (<=2d)
            self.assertTrue(s3["coins"]["SOL"]["g20_50"]); self.assertIn("stale_error", s3)
            s4 = H.load_state(self.ON, fetch=fetch, now=86400 * 11 + 60)   # within 30 min of failure: no refetch
            self.assertEqual(len(calls), 1)
            s5 = H.load_state(self.ON, fetch=bad, now=86400 * 20)          # too old -> no cut
            self.assertEqual(s5["coins"], {})
            short = lambda: ({a: [1.0] * 10 for a in H.COINS}, "t", [])
            H.STATE = os.path.join(d, "s2.json")
            self.assertEqual(H.load_state(self.ON, fetch=short, now=5)["coins"], {})
        finally:
            H.STATE = old

    def test_hooks_present(self):
        e = open("_edge_check_run.py", encoding="utf-8").read()
        self.assertIn("_H093.quiet_to_halve(globals().get(\"cb\")", e); self.assertIn("H093.streak_cap(cb", e)
        self.assertIn("_rcc = H093.rev_cap_pct(cb)", e)
        self.assertEqual(e.count("H093.sell_mult(cb"), 2)
        self.assertIn("H093.sell_mult(cb, opp_sold, opp_bought", e)
        # daily_v2 reversal cap path and Friday unchanged
        self.assertIn("_REV.reversal_cap(info.get(\"kind\"), REVERSAL_CAP, cb)", e)
        self.assertNotIn("H093", open("friday_rsi.py", encoding="utf-8").read())


if __name__ == "__main__":
    unittest.main()
