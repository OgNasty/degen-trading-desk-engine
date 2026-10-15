"""mirrorguard1015: guard blocks on a mirror without fallback-active.json, allows with it.
Runs each script in a TEMP COPY, in a subprocess with networking disabled (unshare -rn),
with a throwaway unfunded key. Never imports the entry scripts here."""
import os, sys, tempfile
from pathlib import Path
_HERE = Path(__file__).resolve().parent
if _HERE.name == "tests":  # template layout: isolated paper-mode home; code lives one folder up
    _TMP = tempfile.mkdtemp(prefix="degen-test-")
    os.environ["DEGEN_HOME"] = _TMP
    os.environ.setdefault("PAPER_TRADING", "true")
    Path(_TMP, ".env").write_text("HELIUS_RPC_URL=https://rpc.invalid/test\nPAPER_TRADING=true\n")
    sys.path.insert(0, str(_HERE.parent)); os.chdir(str(_HERE.parent))
import os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent
if SRC.name == "tests":
    SRC = SRC.parent
PY = sys.executable
NONET = ["unshare", "-rn"] if shutil.which("unshare") and subprocess.call(["unshare", "-rn", "true"]) == 0 else None

def make_copy(mirror, active):
    d = Path(tempfile.mkdtemp(prefix="mg-"))
    for p in SRC.glob("*.py"):
        if not p.name.startswith(("test_", "deploy_", "patch_", "fix_")):
            shutil.copy2(p, d / p.name)
    for n in ("lend-config.json", "sol-stake-config.json", "allowed-pairs.json", "reporting.json"):
        if (SRC / n).exists():
            shutil.copy2(SRC / n, d / n)
    if mirror:
        (d / "MIRROR_ONLY").write_text("test")
    if active:
        (d / "fallback-active.json").write_text("{}")
    return d

def run(d, script, *args):
    env = {k: v for k, v in os.environ.items() if k not in ("DEGEN_WALLET_KEY",)}
    env["DEGEN_HOME"] = str(d)
    env["SOLANA_TRADING_PRIVATE_KEY"] = "x"   # junk, never a real key
    cmd = (NONET or []) + [PY, str(d / script), *args]
    r = subprocess.run(cmd, cwd=str(d), env=env, capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr

@unittest.skipIf(NONET is None, "needs unshare -rn (network-less subprocess); Windows uses guard unit tests only")
class SubprocGuard(unittest.TestCase):
    def tearDown(self):
        for d in getattr(self, "_dirs", []):
            shutil.rmtree(d, ignore_errors=True)
    def _d(self, mirror, active):
        d = make_copy(mirror, active); self._dirs = getattr(self, "_dirs", []) + [d]; return d
    def test_blocks_without_active(self):
        d = self._d(True, False)
        for script, args in (("_edge_check_run.py", ()), ("degen_lend.py", ("tick",)), ("sol_stake.py", ("tick",))):
            rc, out = run(d, script, *args)
            self.assertEqual(rc, 3, (script, out[-400:])); self.assertIn("MIRROR_GUARD_REFUSED", out)
        self.assertFalse((d / "last-check.json").exists())
        self.assertFalse((d / "lend-state.json").exists())
        self.assertFalse((d / "sol-stake-state.json").exists())
    def test_allows_with_active(self):
        d = self._d(True, True)
        rc, out = run(d, "_edge_check_run.py")
        self.assertNotIn("MIRROR_GUARD_REFUSED", out)  # gets past the guard (then fails offline)
        rc, out = run(d, "degen_lend.py", "tick")
        self.assertNotIn("MIRROR_GUARD_REFUSED", out)
    def test_pc_no_mirror_flag(self):
        d = self._d(False, False)
        rc, out = run(d, "sol_stake.py", "tick")
        self.assertNotIn("MIRROR_GUARD_REFUSED", out)

class GuardUnit(unittest.TestCase):
    def test_blocked_logic(self):
        sys.path.insert(0, str(SRC)); import mirror_guard as MG
        with tempfile.TemporaryDirectory() as t:
            t = Path(t); self.assertFalse(MG.blocked(t))
            (t / "MIRROR_ONLY").write_text("x"); self.assertTrue(MG.blocked(t))
            with self.assertRaises(SystemExit) as cm: MG.check("unit", t)
            self.assertEqual(cm.exception.code, 3)
            (t / "fallback-active.json").write_text("{}"); self.assertFalse(MG.blocked(t)); MG.check("unit", t)
    def test_guard_is_first_in_edge_check(self):
        s = open(SRC / "_edge_check_run.py", encoding="utf-8").read()
        code = [l for l in s.split('"""', 2)[2].splitlines() if l.strip() and not l.startswith("#")]
        self.assertTrue(code[0].startswith("import mirror_guard")); self.assertIn("_MG.check(", code[1])

if __name__ == "__main__":
    unittest.main()
