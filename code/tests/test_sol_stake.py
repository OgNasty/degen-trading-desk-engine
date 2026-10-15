"""stake1015: separate JitoSOL bucket for trading SOL (no network)."""
import os, sys, tempfile
from pathlib import Path
_HERE = Path(__file__).resolve().parent
if _HERE.name == "tests":  # template layout: isolated paper-mode home; code lives one folder up
    _TMP = tempfile.mkdtemp(prefix="degen-test-")
    os.environ["DEGEN_HOME"] = _TMP
    os.environ.setdefault("PAPER_TRADING", "true")
    Path(_TMP, ".env").write_text("HELIUS_RPC_URL=https://rpc.invalid/test\nPAPER_TRADING=true\n")
    sys.path.insert(0, str(_HERE.parent)); os.chdir(str(_HERE.parent))
import json, os, sys, tempfile, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import sol_stake as S

J = S.JITO_MINT
MK = {"rate": 1.25, "sol_usd": 100.0, "apy": 4.57}


def wcfg(d):
    with open(S.CFG_PATH, "w") as f:
        json.dump(d, f)


def tok(j):
    return {"result": {"value": [{"account": {"data": {"parsed": {"info": {"mint": J, "tokenAmount": {"uiAmount": j}}}}}}]}}


class Base(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        S.CFG_PATH = os.path.join(d, "c.json"); S.STATE_PATH = os.path.join(d, "s.json")
        wcfg({"enabled": True})
        S.save_state({"rate": 1.25, "sol_usd": 100.0, "apy": 4.57, "rate_t": 9e12})
        S.LAST.clear(); S.LAST.update(read=False, bucket=0.0, rate=None)
        self.ch = {"sol": 0.0, "j": 0.0}; self.swaps = []

    def reader(self):
        return self.ch["sol"], self.ch["j"]

    def swapper(self, sell, buy, raw):
        self.swaps.append((sell, buy, raw))
        if sell == "SOL":
            a = raw / 1e9; self.ch["sol"] -= a; self.ch["j"] += a / 1.25
        else:
            q = raw / 1e9; self.ch["j"] -= q; self.ch["sol"] += q * 1.25
        return "SIG" + sell, None, False


class TestStake(Base):
    def test_first_stake_half_of_trading_sol(self):
        self.ch.update(sol=2.05, j=0.3)          # 0.3 JitoSOL is savings, not bucket
        out = S.tick(reader=self.reader, swapper=self.swapper, sleep=lambda s: None)
        self.assertEqual(out["action"], "stake"); self.assertAlmostEqual(out["amount_sol"], 1.0, 6)
        self.assertAlmostEqual(S.bucket_qty(), 0.8, 6)      # only the new JitoSOL is bucket
        self.assertEqual(S.load_state()["sigs"], ["SIGSOL"])

    def test_savings_jitosol_not_bucket_in_reads(self):
        S.save_state(dict(S.load_state(), bucket_jitosol=0.8))
        v, q = S.bucket_sol_for_read(1.1)                   # wallet 1.1 = 0.8 bucket + 0.3 savings
        self.assertAlmostEqual(q, 0.8); self.assertAlmostEqual(v, 1.0)

    def test_bucket_clamped_to_wallet(self):
        S.save_state(dict(S.load_state(), bucket_jitosol=0.8))
        v, q = S.bucket_sol_for_read(0.5); self.assertAlmostEqual(q, 0.5)

    def test_quiet_at_target(self):
        S.save_state(dict(S.load_state(), bucket_jitosol=0.8)); self.ch.update(sol=1.05, j=0.8)
        out = S.tick(reader=self.reader, swapper=self.swapper)
        self.assertNotIn("action", out); self.assertEqual(self.swaps, [])

    def test_idle_min(self):
        self.assertEqual(S.idle_min_usd(50), 10.0); self.assertEqual(S.idle_min_usd(1e6), 500.0)
        self.assertEqual(S.plan_deposit(0.12, 0.0, 100.0), 0.0)   # excess $6 < $10

    def test_unstake_when_short(self):
        S.save_state(dict(S.load_state(), bucket_jitosol=0.8)); self.ch.update(sol=1.05, j=0.8)
        S.LAST.update(read=True, bucket=0.8, rate=1.25)
        S.ensure_liquid(S.SOL_MINT, int(1.5e9), reader=self.reader, swapper=self.swapper, sleep=lambda s: None)
        self.assertEqual(self.swaps[0][:2], ("JitoSOL", "SOL"))
        self.assertGreaterEqual(self.ch["sol"] - 0.05, 1.5)
        self.assertLess(S.bucket_qty(), 0.8)

    def test_covered_no_unstake(self):
        S.save_state(dict(S.load_state(), bucket_jitosol=0.8)); self.ch.update(sol=1.05, j=0.8)
        S.LAST.update(read=True, bucket=0.8, rate=1.25)
        S.ensure_liquid(S.SOL_MINT, int(0.5e9), reader=self.reader, swapper=self.swapper)
        self.assertEqual(self.swaps, [])

    def test_ensure_noop_without_read_or_busy(self):
        S.save_state(dict(S.load_state(), bucket_jitosol=0.8)); self.ch.update(sol=0.1, j=0.8)
        S.ensure_liquid(S.SOL_MINT, int(1e9), reader=self.reader, swapper=self.swapper)
        S.LAST.update(read=True, bucket=0.8, rate=1.25); S._BUSY["on"] = True
        S.ensure_liquid(S.SOL_MINT, int(1e9), reader=self.reader, swapper=self.swapper)
        S._BUSY["on"] = False
        self.assertEqual(self.swaps, [])

    def test_switch_off_unstakes_bucket_only(self):
        S.save_state(dict(S.load_state(), bucket_jitosol=0.8)); self.ch.update(sol=1.05, j=1.1)
        wcfg({"enabled": False})
        out = S.tick(reader=self.reader, swapper=self.swapper, sleep=lambda s: None)
        self.assertEqual(out["action"], "unstake_all"); self.assertEqual(self.swaps[0], ("JitoSOL", "SOL", 800000000))
        self.assertAlmostEqual(self.ch["j"], 0.3, 6)       # savings JitoSOL untouched
        self.assertAlmostEqual(S.bucket_qty(), 0.0, 9)

    def test_off_empty_noop(self):
        wcfg({"enabled": False}); S.save_state({})
        self.assertEqual(S.tick(reader=lambda: 1 / 0), {"enabled": False})

    def test_accrual(self):
        S.LAST.update(read=True, bucket=0.8, rate=1.26)
        self.assertAlmostEqual(S.accrual_sol({"jito_rate": 1.25}), 0.008, 9)

    def test_late_fill_skips_bucket_sig(self):
        import late_fills as LF
        d = tempfile.mkdtemp(); st = os.path.join(d, "lf.json"); tr = os.path.join(d, "t.jsonl"); open(tr, "w").close()
        with open(st, "w") as f: json.dump({"seen": []}, f)
        S.save_state(dict(S.load_state(), sigs=["BUCKETSIG"]))
        old = LF._DH.HOME_STR; LF._DH.HOME_STR = os.path.dirname(S.STATE_PATH)
        os.replace(S.STATE_PATH, os.path.join(LF._DH.HOME_STR, "sol-stake-state.json"))
        calls = []
        def rpc(m, p):
            calls.append(m)
            return {"result": [{"signature": "BUCKETSIG", "err": None}]} if m == "getSignaturesForAddress" else {"result": None}
        try:
            self.assertEqual(LF.scan("W", rpc, state_path=st, trades_path=tr), [])
            self.assertNotIn("getTransaction", calls)
        finally:
            LF._DH.HOME_STR = old



class TrackerTests(unittest.TestCase):
    def test_tracker_cost_vs_gain(self):
        st = {"rate": 1.30, "bucket_jitosol": 0.0}
        S._track(st, "stake", {"sol": 1.31, "jitosol": 1.0})          # paid 0.01 SOL over fair value
        st["bucket_jitosol"] = 1.0
        tk = S.tracker(st, rate=1.30, sol_usd=100)
        self.assertAlmostEqual(tk["swap_cost_sol"], 0.01, 9); self.assertAlmostEqual(tk["staking_gain_sol"], 0.0, 9)
        self.assertFalse(tk["paying_off"])
        tk = S.tracker(st, rate=1.32, sol_usd=100)                     # JitoSOL rose 0.02 SOL each
        self.assertAlmostEqual(tk["staking_gain_sol"], 0.02, 9); self.assertTrue(tk["paying_off"])
        self.assertAlmostEqual(tk["net_usd"], 1.0, 4)
        st["rate"] = 1.32
        S._track(st, "unstake", {"jitosol": 0.5, "sol_got": 0.655})    # fair 0.66, got 0.655 -> cost 0.005
        st["bucket_jitosol"] = 0.5
        tk = S.tracker(st, rate=1.32)
        self.assertAlmostEqual(tk["swap_cost_sol"], 0.015, 9)
        self.assertAlmostEqual(tk["staking_gain_sol"], 0.02, 9)        # 0.01 realized + 0.01 unrealized
        self.assertEqual(tk["swaps"], 2)

    def test_failed_swaps_not_tracked(self):
        st = {"rate": 1.3}
        S._track(st, "stake", {"sol": 1.0, "jitosol": 0.0, "err": "x"})
        self.assertIsNone(S.tracker(st))

if __name__ == "__main__":
    unittest.main(verbosity=1)
