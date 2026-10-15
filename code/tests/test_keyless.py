#!/usr/bin/env python3
"""Offline tests for v1.0.0 keyless fallbacks + jitter. No network: urlopen is mocked.
  python tests/test_keyless.py      (run from DEGEN_HOME; uses a temp DEGEN_HOME)"""
import io, json, os, socket, sys, tempfile, time, urllib.error
from datetime import datetime
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="degen-test-")
os.environ["DEGEN_HOME"] = TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
Path(TMP, "config.json").write_text(json.dumps({"paper_trading": True, "jitter": {"edge_offset_frac": 0.37, "daily_offset_min": -4}}))
Path(TMP, ".env").write_text("HELIUS_RPC_URL=https://rpc.invalid/test\nPAPER_TRADING=true\nSOLANA_TRADING_PRIVATE_KEY=\nTRADING_ALERT_WEBHOOK_URL=\n")
import degen_home as DH, keyless as K, market_gates as MG, daily_rebalance_v2 as DV, friday_rsi as FR

sleeps = []
K._sleep = lambda s: sleeps.append(s)
ROUTES = {}  # substring -> list of responses (exception instance or dict)


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def fake_urlopen(req, timeout=None):
    url = req.full_url
    for key, seq in ROUTES.items():
        if key in url:
            r = seq.pop(0) if len(seq) > 1 else seq[0]
            if isinstance(r, Exception):
                raise r
            return _Resp(json.dumps(r).encode())
    raise urllib.error.URLError("no route in test")


K._urlopen = fake_urlopen
H429 = lambda: urllib.error.HTTPError("u", 429, "Too Many", {"Retry-After": "2"}, None)
H503 = lambda: urllib.error.HTTPError("u", 503, "busy", {}, None)
TO = lambda: urllib.error.URLError(socket.timeout("timed out"))
passed = 0


def ok(cond, name):
    global passed
    assert cond, name
    passed += 1
    print("PASS", name)


def reset():
    ROUTES.clear(); sleeps.clear(); K._warned.clear()
    for f in ("keyless-cache.json", "fear-greed-state.json", "keyless-warnings.log", "fear-greed-rule-state.json"):
        Path(TMP, f).unlink(missing_ok=True)


KR = {"error": [], "result": {"XXBTZUSD": {"c": ["100"], "p": ["0", "101"]}, "XETHZUSD": {"c": ["10"], "p": ["0", "9"]}, "SOLUSD": {"c": ["5"], "p": ["0", "6"]}}}
BN = [{"symbol": "BTCUSDT", "lastPrice": "100", "weightedAvgPrice": "102"}, {"symbol": "ETHUSDT", "lastPrice": "10", "weightedAvgPrice": "8"}, {"symbol": "SOLUSDT", "lastPrice": "5", "weightedAvgPrice": "7"}]

# 1 backoff: 429 then success, Retry-After honoured
reset(); ROUTES["kraken"] = [H429(), KR]
v = MG.fetch_vwap({}); ok(v.get("cbBTC", {}).get("vwap") == 101 and sleeps == [2.0], "429 -> backoff (Retry-After 2s) -> success")
# 2 rotation: Kraken 503 twice -> Binance
reset(); ROUTES["kraken"] = [H503()]; ROUTES["binance"] = [BN]
v = MG.fetch_vwap({}); ok(v.get("WETH", {}).get("vwap") == 8.0, "Kraken 5xx -> rotate to Binance")
# 3 all fail, fresh cache -> cached value
ROUTES["binance"] = [TO()]; ROUTES["kraken"] = [TO()]
v = MG.fetch_vwap({}); ok(v.get("SOL", {}).get("vwap") == 7.0, "all VWAP sources time out -> last-good cache (<10 min)")
# 4 all fail, cache too old -> unavailable -> gate blocks USDC routes, no crash
c = json.loads(Path(TMP, "keyless-cache.json").read_text()); c["vwap24h"]["at"] -= 3600; Path(TMP, "keyless-cache.json").write_text(json.dumps(c))
v = MG.fetch_vwap({}); g = MG.Gates({"fear_greed_rule": {"enabled": False}}, fng={}, vwap=v)
ok("error" in v and g.block("USDC", "SOL") == "vwap24h_unavailable", "stale VWAP cache -> gate skipped (USDC buy blocked)")
ok(Path(TMP, "keyless-warnings.log").read_text().count("KEYLESS_WARN") == 1, "single concise warning per key per run (repeat failures not re-logged)")
# 5 non-retryable 404: no backoff
reset(); ROUTES["kraken"] = [urllib.error.HTTPError("u", 404, "nf", {}, None)]; ROUTES["binance"] = [BN]
MG.fetch_vwap({}); ok(sleeps == [], "404 -> no retry/backoff, rotate at once")
# 6 F&G: timeout with cache <36h -> cached; >36h -> None -> USDC routes blocked
reset(); now = time.time()
Path(TMP, "fear-greed-state.json").write_text(json.dumps({"value": 40, "class": "Fear", "fetched_at": now - 30 * 3600, "last_attempt": 0}))
ROUTES["alternative.me"] = [TO()]
f = MG.fetch_fng({}, now=now); ok(f.get("value") == 40 and f.get("stale"), "F&G timeout -> cached value (30h old)")
Path(TMP, "fear-greed-state.json").write_text(json.dumps({"value": 41, "fetched_at": now - 6 * 86400, "last_attempt": 0}))
f = MG.fetch_fng({}, now=now); ok(f.get("value") == 41, "F&G cache 6 days old still used (7-day max age, as live)")
Path(TMP, "fear-greed-state.json").write_text(json.dumps({"value": 40, "fetched_at": now - 8 * 86400, "last_attempt": 0}))
f = MG.fetch_fng({}, now=now); g = MG.Gates({"vwap24h_rule": {"enabled": False}}, fng=f, vwap={})
ok(f.get("value") is None and g.block("SOL", "USDC") is None and g.block("USDC", "SOL") is None and g.size_mult() == 1.0,
   "F&G 8 days old -> F&G rule OFF, USDC routes proceed (template v1.0.0)")
ok("FNG_RULE_OFF" in Path(TMP, "keyless-warnings.log").read_text(), "one notice when the rule switches off")
n_off = Path(TMP, "keyless-warnings.log").read_text().count("FNG_RULE_OFF")
MG.Gates({"vwap24h_rule": {"enabled": False}}, fng={"value": None}, vwap={})
ok(Path(TMP, "keyless-warnings.log").read_text().count("FNG_RULE_OFF") == n_off, "no repeat notice while it stays off")
g = MG.Gates({"vwap24h_rule": {"enabled": False}}, fng={"value": 80, "class": "Extreme Greed"}, vwap={})
ok(g.block("USDC", "SOL") == "fear_greed_gt75_no_usdc_buy" and g.size_mult() == 0.5, "fresh F&G -> rule back ON automatically (>75 blocks USDC buys, x0.5)")
ok("FNG_RULE_ON" in Path(TMP, "keyless-warnings.log").read_text(), "one notice when the rule switches back on")
g = MG.Gates({"vwap24h_rule": {"enabled": False}}, fng={"value": 20}, vwap={})
ok(g.block("SOL", "USDC") == "fear_greed_lt25_no_usdc_sell", "rule stays on with fresh data (<25 blocks USDC sells)")
Path(TMP, "fear-greed-state.json").unlink()  # (a failed try waits an hour before retrying; start clean)
ROUTES["alternative.me"] = [H429(), {"data": [{"value": "55", "value_classification": "Greed"}]}]
f = MG.fetch_fng({}, now=now); ok(f.get("value") == 55, "F&G 429 -> backoff -> fresh value")
# 7 CoinGecko volume: 429 x2 -> cached shares; no cache -> exception -> run skipped (caller)
reset(); ROUTES["coingecko"] = [{"bitcoin": {"usd_24h_vol": 3}, "ethereum": {"usd_24h_vol": 1}, "solana": {"usd_24h_vol": 0}}]
DV.volume_shares(); ROUTES["coingecko"] = [H429()]
ok(abs(DV.volume_shares()["cbBTC"] - 0.75) < 1e-9, "CoinGecko 429 -> cached volume shares")
Path(TMP, "keyless-cache.json").unlink()
try:
    DV.volume_shares(); ok(False, "x")
except K.AllSourcesFailed:
    ok(True, "CoinGecko down + no cache -> AllSourcesFailed (daily run skips)")
# 8 Friday spot: Binance 429 -> Coinbase; both down -> no live price (build_decision then uses last close, as live)
reset(); ROUTES["binance"] = [H429()]; ROUTES["coinbase"] = [{"price": "5"}]
p, src = FR.fetch_spot_prices(); ok(p.get("SOL") == 5.0 and "coinbase" in src and sleeps, "Friday spot: Binance 429 (backoff) -> Coinbase")
ROUTES["coinbase"] = [TO()]
p, src = FR.fetch_spot_prices(); ok("SOL" not in p, "Friday spot: Binance + Coinbase down -> no live price")
closes = {s: [float(i) for i in range(1, 31)] for s in FR.COINS}
FR.fetch_rsi_closes = lambda: (closes, "binance", [])
FR.load_friday_config = lambda: (_ for _ in ()).throw(SystemExit("stop-after-prices"))
_orig = FR.fetch_spot_prices
captured = {}
def _spy():
    r = _orig(); captured["r"] = r; return r
FR.fetch_spot_prices = _spy
try:
    FR.build_decision()
except SystemExit:
    pass
ok(captured["r"][0].get("SOL") == 30.0, "build_decision filled SOL from the last completed close (30.0), as live")
FR.fetch_rsi_closes = lambda: (None, None, ["binance:timeout", "coinbase:timeout"])
try:
    FR.build_decision(); ok(False, "x")
except RuntimeError as e:
    ok("candle_fetch_failed" in str(e), "closes also unavailable -> run skipped (NO_TX)")
# 9 jitter
s = DH.next_jittered(datetime.fromtimestamp(1_700_000_000).astimezone(), 300)
ok(int(s.timestamp()) % 300 == DH.edge_offset_sec(300) == 111, "edge loop lands on this install's offset (111 s of 300)")
ok(DH.jittered_hm("06:59") == "06:55" and DH.jittered_hm("21:01") == "20:57", "daily times shift by -4 min, odd minutes stay odd")
from zoneinfo import ZoneInfo
b = MG._last_boundary(datetime(2026, 10, 13, 22, 0, tzinfo=ZoneInfo("America/Phoenix")).timestamp(), 21, "America/Phoenix")
ok(datetime.fromtimestamp(b, ZoneInfo("America/Phoenix")).strftime("%H:%M") == "20:56", "F&G daily fetch boundary 21:00 -> 20:56 (jitter)")
print(f"ALL {passed} TESTS PASSED")
