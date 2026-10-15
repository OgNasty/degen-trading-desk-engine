"""jup429once1015: Jupiter 429 retries once per quote; non-Jupiter (Raydium) unchanged."""
import os, sys, tempfile
from pathlib import Path
_HERE = Path(__file__).resolve().parent
if _HERE.name == "tests":  # template layout: isolated paper-mode home; code lives one folder up
    _TMP = tempfile.mkdtemp(prefix="degen-test-")
    os.environ["DEGEN_HOME"] = _TMP
    os.environ.setdefault("PAPER_TRADING", "true")
    Path(_TMP, ".env").write_text("HELIUS_RPC_URL=https://rpc.invalid/test\nPAPER_TRADING=true\n")
    sys.path.insert(0, str(_HERE.parent)); os.chdir(str(_HERE.parent))
import unittest, types
from unittest import mock
import venue_quotes as VQ, re, subprocess, time, json
# Never import _edge_check_run (it runs a live check on import); load only curl_json from source.
_src = open("_edge_check_run.py", encoding="utf-8").read()
_fn = re.search(r"^def curl_json\(.*?(?=^def )", _src, re.S | re.M).group(0)
E = types.SimpleNamespace(subprocess=types.SimpleNamespace(run=None), time=types.SimpleNamespace(sleep=None))
_g = {"subprocess": E.subprocess, "time": E.time, "json": json}
exec(_fn, _g); E.curl_json = _g["curl_json"]

def fake(code, body="x"):
    calls = []
    def run(*a, **k):
        calls.append(1); return types.SimpleNamespace(returncode=0, stdout=body + "\n" + code, stderr="")
    return run, calls

class T(unittest.TestCase):
    def _go(self, mod, url, code="429", body="x", retries=4):
        run, calls = fake(code, body)
        with mock.patch.object(mod.subprocess, "run", run), mock.patch.object(mod.time, "sleep") as sl:
            r = mod.curl_json(url, retries=retries)
        return r, len(calls), sl
    def test_vq_jup_429_once(self):
        r, n, sl = self._go(VQ, VQ.JUP_MANUAL + "?a=1")
        self.assertEqual(n, 2); self.assertEqual(r["error"], "rate_limit_429"); sl.assert_called_once_with(4.0)
    def test_vq_jup_text_rate_limit(self):
        r, n, _ = self._go(VQ, "https://api.jup.ag/price/v3", "200", "Rate limit exceeded")
        self.assertEqual(n, 2)
    def test_vq_raydium_unchanged(self):
        r, n, _ = self._go(VQ, VQ.RAY_COMPUTE + "?a=1")
        self.assertEqual(n, 4)
    def test_vq_jup_non429_unchanged(self):
        r, n, _ = self._go(VQ, VQ.JUP_MANUAL, "500")
        self.assertEqual(n, 4)
    def test_edge_jup_429_once(self):
        r, n, sl = self._go(E, "https://api.jup.ag/swap/v2/build?x", retries=5)
        self.assertEqual(n, 2); sl.assert_called_once_with(8.0)
    def test_edge_raydium_unchanged(self):
        r, n, _ = self._go(E, "https://transaction-v1.raydium.io/x", retries=5)
        self.assertEqual(n, 5)

if __name__ == "__main__":
    unittest.main()
