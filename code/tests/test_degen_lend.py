"""lend1015: Jupiter Lend for USDC (no network; fake rpc/sender)."""
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
import degen_lend as L

USDC, JL = L.USDC_MINT, L.JL_MINT


def tok(usdc=0.0, jl=0.0, extra=()):
    vals = []
    for m, a in ((USDC, usdc), (JL, jl)) + tuple(extra):
        vals.append({"account": {"data": {"parsed": {"info": {"mint": m, "tokenAmount": {"uiAmount": a}}}}}})
    return {"result": {"value": vals}}


def wcfg(d):
    with open(L.CFG_PATH, "w") as f:
        json.dump(d, f)


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        L.CFG_PATH = os.path.join(self.d, "lend-config.json")
        L.STATE_PATH = os.path.join(self.d, "lend-state.json")
        wcfg({"enabled": True})
        L.save_state({"sv": 1.0645, "sv_t": 9e12, "apr": 6.04, "pool_free": 1e9})
        L.LAST.clear(); L.LAST.update(read=False, shares=0.0, sv=None, lent=0.0)
        self.chain = {"liq": 0.0, "sh": 0.0}
        self.sent = []

    def reader(self):
        return self.chain["liq"], self.chain["sh"]

    def sender(self, kind, raw):
        self.sent.append((kind, raw))
        sv = 1.0645
        if kind == "deposit":
            a = raw / 1e6; self.chain["liq"] -= a; self.chain["sh"] += a / sv
        elif kind == "withdraw":
            a = raw / 1e6; self.chain["liq"] += a; self.chain["sh"] -= a / sv
        elif kind == "redeem":
            s = raw / 1e6; self.chain["liq"] += s * sv; self.chain["sh"] = max(0.0, self.chain["sh"] - s)
        return "SIG" + kind, None, False


class TestMath(Base):
    def test_withdraw_resets_to_half(self):
        p = L.plan_trade(10, 50, 30, share=0.5, buffer=0.001)
        self.assertEqual(p.action, "withdraw_then_trade")
        self.assertAlmostEqual(p.withdraw, (20 + 0.5 * 30) * 1.001, 6)

    def test_liquid_covers(self):
        self.assertEqual(L.plan_trade(40, 40, 30).action, "trade")

    def test_capped_to_total(self):
        p = L.plan_trade(10, 20, 100, share=0.5, buffer=0.0)
        self.assertAlmostEqual(p.withdraw, 20, 6)

    def test_pool_limited_shrinks(self):
        p = L.plan_trade(10, 50, 30, pool_free=5)
        self.assertEqual(p.action, "shrink"); self.assertAlmostEqual(p.trade_amount, 15)

    def test_idle_min_scales(self):
        self.assertEqual(L.idle_min_usd(64), 10.0)
        self.assertAlmostEqual(L.idle_min_usd(1000), 100.0)
        self.assertEqual(L.idle_min_usd(100000), 500.0)

    def test_idle_rule(self):
        self.assertAlmostEqual(L.plan_deposit(64, 0), 32)          # first lend: 100% liquid
        self.assertEqual(L.plan_deposit(40, 30), 0.0)              # 57% liquid: under 70%
        self.assertEqual(L.plan_deposit(8, 2), 0.0)                # 80% liquid but excess $3 < $10
        self.assertAlmostEqual(L.plan_deposit(80, 20), 30)         # 80% liquid, excess $30


class TestValuation(Base):
    def test_lent_added(self):
        v = L.lent_usdc_from_tok(tok(30, 10))
        self.assertAlmostEqual(v, 10.645, 6)
        self.assertTrue(L.LAST["read"]); self.assertEqual(L.LAST["shares"], 10)
        self.assertEqual(L.load_state()["shares"], 10)

    def test_no_position_no_network(self):
        L.save_state({})
        self.assertEqual(L.lent_usdc_from_tok(tok(30, 0)), 0.0)

    def test_accrual(self):
        L.lent_usdc_from_tok(tok(30, 10))
        self.assertAlmostEqual(L.accrual_usdc({"lend_sv": 1.0640}), 10 * 0.0005, 9)
        self.assertEqual(L.accrual_usdc({}), 0.0)


class TestMoves(Base):
    def test_ensure_noop_without_read(self):
        self.assertIsNone(L.ensure_liquid(USDC, 50_000_000, reader=self.reader, sender=self.sender))
        self.assertEqual(self.sent, [])

    def test_ensure_noop_other_mint(self):
        L.lent_usdc_from_tok(tok(10, 20))
        L.ensure_liquid("So11111111111111111111111111111111111111112", 9e9, reader=self.reader, sender=self.sender)
        self.assertEqual(self.sent, [])

    def test_ensure_withdraws_short_plus_reset(self):
        self.chain.update(liq=10.0, sh=50 / 1.0645)
        L.lent_usdc_from_tok(tok(10, self.chain["sh"]))
        L.ensure_liquid(USDC, 30_000_000, reader=self.reader, sender=self.sender, sleep=lambda s: None)
        self.assertEqual(self.sent[0][0], "withdraw")
        self.assertAlmostEqual(self.sent[0][1] / 1e6, (20 + 15) * 1.001, 4)
        self.assertGreaterEqual(self.chain["liq"], 30)
        st = L.load_state(); self.assertEqual(st["last_withdraw"]["sig"], "SIGwithdraw")

    def test_ensure_covered_no_withdraw(self):
        self.chain.update(liq=40.0, sh=20)
        L.lent_usdc_from_tok(tok(40, 20))
        L.ensure_liquid(USDC, 30_000_000, reader=self.reader, sender=self.sender)
        self.assertEqual(self.sent, [])

    def test_ensure_full_uses_redeem(self):
        self.chain.update(liq=1.0, sh=10)
        L.lent_usdc_from_tok(tok(1, 10))
        L.ensure_liquid(USDC, 50_000_000, reader=self.reader, sender=self.sender, sleep=lambda s: None)
        self.assertEqual(self.sent[0], ("redeem", 10_000_000))

    def test_ensure_never_raises(self):
        L.lent_usdc_from_tok(tok(1, 10))
        def bad(): raise RuntimeError("rpc down")
        self.assertIsNone(L.ensure_liquid(USDC, 5_000_000, reader=bad, sender=self.sender))

    def test_tick_first_deposit_half(self):
        self.chain.update(liq=64.0, sh=0)
        out = L.tick(reader=self.reader, sender=self.sender)
        self.assertEqual(out["action"], "deposit"); self.assertAlmostEqual(out["amount"], 32, 4)
        self.assertEqual(self.sent, [("deposit", 32_000_000)])

    def test_tick_idle_quiet_at_target(self):
        self.chain.update(liq=32.0, sh=30)
        out = L.tick(reader=self.reader, sender=self.sender)
        self.assertNotIn("action", out); self.assertEqual(self.sent, [])

    def test_switch_off_withdraws_all(self):
        self.chain.update(liq=32.0, sh=30)
        L.save_state(dict(L.load_state(), shares=30))
        wcfg({"enabled": False})
        out = L.tick(reader=self.reader, sender=self.sender, )
        self.assertEqual(out["action"], "withdraw_all"); self.assertEqual(self.sent, [("redeem", 30_000_000)])

    def test_off_and_empty_is_noop(self):
        wcfg({"enabled": False}); L.save_state({})
        self.assertEqual(L.tick(reader=lambda: 1 / 0, sender=self.sender), {"enabled": False})

    def test_disabled_ensure_withdraws_all_needed(self):
        wcfg({"enabled": False})
        self.chain.update(liq=10.0, sh=20)
        L.lent_usdc_from_tok(tok(10, 20))
        L.ensure_liquid(USDC, 15_000_000, reader=self.reader, sender=self.sender, sleep=lambda s: None)
        self.assertEqual(self.sent[0][0], "redeem")

    def test_failed_send_recorded(self):
        self.chain.update(liq=64.0, sh=0)
        out = L.tick(reader=self.reader, sender=lambda k, r: (None, "sim_fail", False))
        self.assertEqual(out["err"], "sim_fail")
        self.assertEqual(L.load_state()["events"][-1]["err"], "sim_fail")


class TestHooks(unittest.TestCase):
    def test_late_fill_ignores_lend_tx(self):
        import late_fills as LF
        W = "W" * 32
        tx = {"meta": {"preBalances": [10, 0], "postBalances": [5, 0], "fee": 5,
                       "preTokenBalances": [{"accountIndex": 1, "owner": W, "mint": USDC, "uiTokenAmount": {"uiAmountString": "64"}}],
                       "postTokenBalances": [{"accountIndex": 1, "owner": W, "mint": USDC, "uiTokenAmount": {"uiAmountString": "32"}},
                                             {"accountIndex": 2, "owner": W, "mint": JL, "uiTokenAmount": {"uiAmountString": "30"}}]},
              "transaction": {"message": {"accountKeys": [W, "x", "y"]}}}
        ch, signer = LF.deltas(tx, W)
        self.assertEqual(ch, {}); self.assertIsNone(LF.swap_row("s", 0, ch))

    def test_rent_reclaim_keeps_lend_and_jitosol(self):
        import friday_rsi as FR
        keep = FR.rent_keep_mints({})
        self.assertIn(JL, keep); self.assertIn("J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn", keep)

    def test_detect_flows_lend_move_is_not_flow(self):
        import savings_mode as SM
        L.LAST.update(read=True, shares=0.0, sv=None)
        d = tempfile.mkdtemp(); log = os.path.join(d, "t.jsonl"); open(log, "w").close()  # empty trade log
        cb = {"deposit_watch": {"balances": {"SOL": 1, "USDC": 64, "cbBTC": 0, "WETH": 0}, "log_lines": 0}}
        px = {"SOL": 100, "USDC": 1, "cbBTC": 60000, "WETH": 3000}
        # the USDC total is unchanged by a lend deposit (32 liquid + 32 lent)
        r = SM._detect_flows(cb, {"SOL": 1, "USDC": 64.0, "cbBTC": 0, "WETH": 0}, px, None, log)
        self.assertEqual(r["reason"] if isinstance(r, dict) and "reason" in r else r[0] if isinstance(r, tuple) else r.get("status"), "unchanged")


if __name__ == "__main__":
    unittest.main(verbosity=1)
