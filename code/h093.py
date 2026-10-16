"""H093 preset (optional, OFF by default). One switch: cost-basis.json h093.enabled.

enabled true  -> reversal cap 60%, skip-halving after 30 quiet checks, same-pair streak cap 5,
                 daily_rebalance_v2 daily_fraction 0.15, and the D sell cuts below.
enabled false -> every value comes from the stored settings: 85 / 60 / 3 / 0.10, no D cuts.
The stored settings in cost-basis.json are never overwritten, so turning it off restores them.
The daily-rebalance reversal cap (50%) and the Friday caps are not part of H093.

D sell cuts (trend filters on completed UTC daily closes; a state decided at day d's close applies on day d+1):
  g20_50 : MA20(close) > MA50(close) (min_periods=1), hysteresis: on after 2 straight true days,
           off after 2 straight false days.  -> x0.5
  ret30  : close/close[30 days earlier]-1; on when > 0.15, off when < 0.05.  -> x0.15
  both   : multiply (x0.075). Applies only to edge (basis) and reversal sells of SOL/cbBTC/WETH
           into USDC. Not daily rebalance, not Friday, not cross-pair trades.
If daily closes can't be fetched and no state from the last 2 days is cached, no cut is applied.
Turn on: set "h093": {"enabled": true} in cost-basis.json.
"""
import json, os, time

import degen_home as _DH

STATE = os.path.join(_DH.HOME_STR, "h093-bull-state.json")
COINS = ("SOL", "cbBTC", "WETH")
DEFAULTS = {"enabled": False, "reversal_close_cap_pct": 60, "quiet_checks_to_halve": 30,
            "max_consecutive_fills": 5, "daily_fraction": 0.15,
            "d_cut": {"g20_50_mult": 0.5, "ret30_mult": 0.15, "ret30_on": 0.15, "ret30_off": 0.05}}


def cfg(cb):
    c = json.loads(json.dumps(DEFAULTS))
    c.update({k: v for k, v in ((cb or {}).get("h093") or {}).items() if k != "d_cut"})
    c["d_cut"].update(((cb or {}).get("h093") or {}).get("d_cut") or {})
    return c


def enabled(cb):
    return bool(((cb or {}).get("h093") or {}).get("enabled", False))


def rev_cap_pct(cb):
    return cfg(cb)["reversal_close_cap_pct"] if enabled(cb) else (cb or {}).get("reversal_close_cap_pct")


def quiet_to_halve(cb, stored):
    return int(cfg(cb)["quiet_checks_to_halve"]) if enabled(cb) else int(stored)


def streak_cap(cb, stored):
    return int(cfg(cb)["max_consecutive_fills"]) if enabled(cb) else int(stored)


def daily_fraction(cb, stored):
    return float(cfg(cb)["daily_fraction"]) if enabled(cb) else float(stored)


# ---- D cut state (pure functions) ----
def _ma(c, n, j):
    w = c[max(0, j - n + 1): j + 1]
    return sum(w) / len(w)


def _hyst(cond, kon=2, koff=2):
    st = False; on = off = 0; out = []
    for x in cond:
        if x: on += 1; off = 0
        else: off += 1; on = 0
        if not st and on >= kon: st = True
        elif st and off >= koff: st = False
        out.append(st)
    return out


def _band(vals, on, off):
    st = False; out = []
    for v in vals:
        if v is not None and not st and v > on: st = True
        elif v is not None and st and v < off: st = False
        out.append(st)
    return out


def states_from_closes(closes, ret_on=0.15, ret_off=0.05):
    """closes: completed UTC daily closes, oldest first. Returns (g20_50, ret30) state for the next day."""
    if not closes:
        return False, False
    g = _hyst([_ma(closes, 20, j) > _ma(closes, 50, j) for j in range(len(closes))])
    r = _band([(closes[j] / closes[j - 30] - 1) if j >= 30 else None for j in range(len(closes))], ret_on, ret_off)
    return bool(g[-1]), bool(r[-1])


def _utc_day(t=None):
    return int((time.time() if t is None else t) // 86400)


def _get(url, timeout=6):
    import urllib.request
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "degen-h093"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_closes():
    """Completed UTC daily closes (oldest first), Binance USDT then Coinbase USD. Short timeouts."""
    errs = []
    now_ms = int(time.time() * 1000)
    try:
        out = {}
        for a, sym in (("SOL", "SOLUSDT"), ("cbBTC", "BTCUSDT"), ("WETH", "ETHUSDT")):
            rows = _get(f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=1d&limit=120")
            out[a] = [float(r[4]) for r in rows if int(r[6]) < now_ms]
        return out, "binance", errs
    except Exception as e:
        errs.append(f"binance:{type(e).__name__}")
    try:
        out = {}
        for a, prod in (("SOL", "SOL-USD"), ("cbBTC", "BTC-USD"), ("WETH", "ETH-USD")):
            rows = _get(f"https://api.exchange.coinbase.com/products/{prod}/candles?granularity=86400")
            done = sorted((float(r[0]), float(r[4])) for r in rows if float(r[0]) + 86400 <= time.time())
            out[a] = [c for _, c in done]
        return out, "coinbase", errs
    except Exception as e:
        errs.append(f"coinbase:{type(e).__name__}")
    return None, None, errs


def load_state(cb, fetch=None, now=None):
    """Cached per UTC day. fetch() -> ({sym: closes}, source, errors)."""
    day = _utc_day(now)
    try:
        with open(STATE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = None
    if st and st.get("utc_day") == day:
        return st
    c = cfg(cb)["d_cut"]
    try:
        if st and st.get("fail_day") == day and (time.time() if now is None else now) - float(st.get("fail_at") or 0) < 1800:
            raise RuntimeError("retry_wait")  # failed fetch: retry at most every 30 min
        if fetch is None:
            fetch = fetch_closes
        closes, src, errs = fetch()
        if not closes or any(len(closes.get(a) or []) < 50 for a in COINS):
            raise RuntimeError(f"closes_short src={src} errs={errs}")
        coins = {}
        for a in COINS:
            g, r = states_from_closes(closes[a], c["ret30_on"], c["ret30_off"])
            coins[a] = {"g20_50": g, "ret30": r, "n": len(closes[a]), "last_close": closes[a][-1]}
        new = {"utc_day": day, "source": src, "coins": coins, "fetched": int(time.time() if now is None else now)}
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(new, f, indent=1)
        os.replace(tmp, STATE)
        return new
    except Exception as e:
        if str(e) != "retry_wait":
            try:
                keep = dict(st or {}); keep.update(fail_day=day, fail_at=int(time.time() if now is None else now))
                with open(STATE + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(keep, f, indent=1)
                os.replace(STATE + ".tmp", STATE)
            except Exception:
                pass
        if st and day - int(st.get("utc_day", -99)) <= 2:
            st = dict(st); st["stale_error"] = f"{type(e).__name__}: {e}"[:200]
            return st
        return {"utc_day": day, "coins": {}, "error": f"{type(e).__name__}: {e}"[:200]}


def sell_mult(cb, sold, bought, state):
    """Multiplier for an edge/reversal sell of `sold` into `bought`. 1.0 when off or not applicable."""
    if not enabled(cb) or bought != "USDC" or sold not in COINS:
        return 1.0
    s = ((state or {}).get("coins") or {}).get(sold) or {}
    c = cfg(cb)["d_cut"]
    m = 1.0
    if s.get("g20_50"): m *= float(c["g20_50_mult"])
    if s.get("ret30"): m *= float(c["ret30_mult"])
    return m
