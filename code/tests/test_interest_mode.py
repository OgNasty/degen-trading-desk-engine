#!/usr/bin/env python3
"""v1.1.0 interest mode: one switch for USDC lending + the JitoSOL bucket; off by default; apply_features
sets it. Offline, paper mode, temp home. python tests/test_interest_mode.py"""
import os, sys, json, tempfile, shutil, unittest
from pathlib import Path
_HERE = Path(__file__).resolve().parent
TMP = tempfile.mkdtemp(prefix="degen-test-")
os.environ["DEGEN_HOME"] = TMP
os.environ.setdefault("PAPER_TRADING", "true")
Path(TMP, ".env").write_text("HELIUS_RPC_URL=https://rpc.invalid/test\nPAPER_TRADING=true\n")
shutil.copy2(_HERE.parent / "seeds" / "cost-basis.seed.json", Path(TMP, "cost-basis.json"))
shutil.copy2(_HERE.parent / "config.example.json", Path(TMP, "config.json"))
sys.path.insert(0, str(_HERE.parent))
import interest_mode as IM, degen_lend as L, sol_stake as S, apply_features as AF


class T(unittest.TestCase):
    def test_default_off(self):
        self.assertFalse(IM.flag()); self.assertFalse(L.enabled()); self.assertFalse(S.enabled())

    def test_one_switch_both_sides(self):
        IM.set_flag(True)
        self.assertTrue(L.enabled()); self.assertTrue(S.enabled())
        IM.set_flag(False)
        self.assertFalse(L.enabled()); self.assertFalse(S.enabled())

    def test_module_switch_flips_both(self):
        L._set_enabled(True)
        self.assertTrue(IM.flag()); self.assertTrue(S.enabled())
        S._set_enabled(False)
        self.assertFalse(IM.flag()); self.assertFalse(L.enabled())

    def test_apply_features(self):
        AF.main(["--interest", "on"])
        self.assertTrue(json.loads(Path(TMP, "cost-basis.json").read_text())["interest_mode"]["enabled"])
        self.assertTrue(json.loads(Path(TMP, "config.json").read_text())["features"]["interest_mode"])
        AF.main(["--interest", "off"])
        self.assertFalse(IM.flag())

    def test_off_and_empty_tick_does_nothing(self):
        IM.set_flag(False)
        self.assertEqual(L.tick(reader=lambda: 1 / 0), {"enabled": False})
        self.assertEqual(S.tick(reader=lambda: 1 / 0), {"enabled": False})


if __name__ == "__main__":
    unittest.main()
