"""Fear & Greed + Kraken 24h VWAP hard gates (owner 2026-10-10).

Rule 1 FNG (api.alternative.me/fng, data[0].value; fng9pm 2026-10-11: fetched once a day at the
  first check at/after 21:00 PT, cache persisted in fear-greed-state.json; a failed 9 PM fetch keeps
  the last value and retries at most hourly; no cache -> fetch now; cache older than 7 days -> fail closed (rules1012; was 48h)):
  value > 75 -> every fill size x0.5 (final, after caps/tilts/cuts) and block
                USDC -> SOL/cbBTC/WETH buys.
  value < 25 -> block SOL/cbBTC/WETH -> USDC sells.
Rule 3 VWAP (Kraken Ticker p[1] = 24h VWAP; current price = Kraken last c[0]):
  USDC -> token only if last < vwap; token -> USDC only if last > vwap.
  Crypto<->crypto routes unaffected.
Fail closed: fetch failure blocks the USDC routes that rule governs.
"""
import degen_home as _DH  # portable base dir (pcmove 2026-10-10)
from degen_home import TZ_NAME as _TZ_NAME
import json, time, urllib.request
from pathlib import Path

ROOT = Path(_DH.HOME_STR)
FNG_STATE = ROOT / "fear-greed-state.json"
FNG_URL = "https://api.alternative.me/fng/"
KRAKEN_URL = "https://api.kraken.com/0/public/Ticker?pair=XBTUSD,ETHUSD,SOLUSD"
TOKENS = ("SOL", "cbBTC", "WETH")


BINANCE_24H = "https://api.binance.com/api/v3/ticker/24hr?symbols=%5B%22BTCUSDT%22,%22ETHUSDT%22,%22SOLUSDT%22%5D"
VWAP_CACHE_MAX_SEC = 600  # v1.0.0: last-good VWAP usable for 10 min when every source fails


def _get(url, timeout):
    """v1.0.0: keyless.get_json = short backoff on 429 / 5xx / timeout."""
    import keyless
    return keyless.get_json(url, timeout=timeout, tries=2)


def cfgs(cb):
    f = cb.get("fear_greed_rule") or {}
    v = cb.get("vwap24h_rule") or {}
    return f, v


def _last_boundary(now, hour=21, tz=None, minute=None):
    """Epoch of the most recent daily HH:00 (+ this install's jitter minutes, v1.0.0) at or before now."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    lt = datetime.fromtimestamp(now, ZoneInfo(tz or _TZ_NAME))
    b = lt.replace(hour=int(hour), minute=0, second=0, microsecond=0)
    b += timedelta(minutes=_DH.daily_offset_min() if minute is None else int(minute))
    if lt < b:
        b -= timedelta(days=1)
    return b.timestamp()


def _save_fng(st):
    try:
        tmp = FNG_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        tmp.replace(FNG_STATE)
    except Exception:
        pass


def fetch_fng(cfg, now=None, getter=_get):
    """fng9pm 2026-10-11: one fetch per day at the first check at/after fetch_hour_pt."""
    now = time.time() if now is None else now
    retry = float(cfg.get("retry_sec", 3600))
    max_age = float(cfg.get("max_age_sec", 7 * 86400))  # 7 days
    try:
        st = json.loads(FNG_STATE.read_text())
        if not isinstance(st, dict):
            st = {}
    except Exception:
        st = {}
    have = st.get("value") is not None
    age = now - float(st.get("fetched_at", 0) or 0)
    boundary = _last_boundary(now, cfg.get("fetch_hour_pt", 21), (cfg.get("tz") or _TZ_NAME))

    def cached(stale=False):
        if age > max_age:  # fail closed on a value older than max_age
            return {"value": None, "class": None, "error": "fng_cache_too_old", "age_h": round(age / 3600, 1)}
        out = {"value": int(st["value"]), "class": st.get("class"), "cached": True}
        if stale:
            out["stale"] = True
        return out

    due = (not have) or float(st.get("fetched_at", 0) or 0) < boundary
    if not due:
        return cached()
    last_try = float(st.get("last_attempt", 0) or 0)
    if have and last_try >= boundary and now - last_try < retry:
        return cached(stale=True)  # 9 PM fetch failed recently: keep last value, retry hourly
    st["last_attempt"] = now
    try:
        d = getter(FNG_URL, float(cfg.get("timeout_sec", 6)))["data"][0]
        out = {"value": int(d["value"]), "class": d.get("value_classification"), "cached": False}
        _save_fng({"value": out["value"], "class": out["class"], "fetched_at": now, "last_attempt": now})
        return out
    except Exception as e:
        _save_fng(st)
        import keyless
        keyless.warn("fear_greed", f"fear_greed: alternative.me failed ({getattr(e, 'kind', type(e).__name__)}); "
                     + ("using cached value" if have and age <= max_age else "no usable cache, USDC routes blocked"))
        if have:
            r = cached(stale=True)
            if r.get("value") is not None:
                r["fetch_error"] = type(e).__name__
            return r
        return {"value": None, "class": None, "error": type(e).__name__}


def _map_kraken(key):
    k = key.upper()
    if "XBT" in k or "BTC" in k:
        return "cbBTC"
    if "ETH" in k:
        return "WETH"
    if "SOL" in k:
        return "SOL"
    return None


def fetch_vwap(cfg, getter=_get):
    """v1.0.0: Kraken first, Binance 24h weightedAvgPrice next; last-good cache up to 10 min; else unavailable
    (the USDC routes stay blocked: no trade on stale data)."""
    import keyless
    t = float(cfg.get("timeout_sec", 6))

    def kraken():
        d = getter(KRAKEN_URL, t)
        if d.get("error"):
            raise ValueError("kraken_error")
        out = {}
        for k, q in (d.get("result") or {}).items():
            sym = _map_kraken(k)
            if sym:
                out[sym] = {"last": float(q["c"][0]), "vwap": float(q["p"][1])}
        if len(out) < 3:
            raise ValueError("kraken_incomplete")
        return out

    def binance():
        rows = getter(BINANCE_24H, t)
        m = {"BTCUSDT": "cbBTC", "ETHUSDT": "WETH", "SOLUSDT": "SOL"}
        out = {m[r["symbol"]]: {"last": float(r["lastPrice"]), "vwap": float(r["weightedAvgPrice"])}
               for r in rows if r.get("symbol") in m}
        if len(out) < 3:
            raise ValueError("binance_incomplete")
        return out

    try:
        v, meta = keyless.first_ok("vwap24h", [("kraken", kraken), ("binance", binance)],
                                   float(cfg.get("cache_max_age_sec", VWAP_CACHE_MAX_SEC)))
        return dict(v)
    except Exception as e:
        return {"error": type(e).__name__}


FNG_RULE_STATE = ROOT / "fear-greed-rule-state.json"


def fng_rule_transition(available, log=None):
    """Template v1.0.0: F&G data older than max age / unavailable turns the F&G rule OFF (USDC routes proceed
    without it); fresh data turns it back ON. One concise notice per switch. Returns True if the rule is active."""
    try:
        st = json.loads(FNG_RULE_STATE.read_text())
    except Exception:
        st = {"off": False}
    was_off = bool(st.get("off"))
    if was_off == (not available):
        return available
    msg = ("FNG_RULE_ON fresh Fear & Greed data; rule active again" if available
           else "FNG_RULE_OFF Fear & Greed data unavailable or older than max age; rule off, USDC routes proceed without it")
    try:
        FNG_RULE_STATE.write_text(json.dumps({"off": not available, "since": time.time()}))
    except Exception:
        pass
    import keyless
    keyless.warn("fng_rule_" + ("on" if available else "off"), msg) if log is None else log(msg)
    return available


class Gates:
    def __init__(self, cb, fng=None, vwap=None):
        self.fcfg, self.vcfg = cfgs(cb)
        self.f_on = bool(self.fcfg.get("enabled", True))
        self.v_on = bool(self.vcfg.get("enabled", True))
        # knife_hurdle_v1 (2026-10-12): VWAP gate off for buys and sells while the switch is on; F&G kept.
        _kh = (cb or {}).get("knife_hurdle_v1") if isinstance(cb, dict) else None
        if isinstance(_kh, dict) and _kh.get("enabled") and _kh.get("disable_vwap", True):
            self.v_on = False
        self.fng = fng if fng is not None else ({} if not self.f_on else fetch_fng(self.fcfg))
        self.vwap = vwap if vwap is not None else ({} if not self.v_on else fetch_vwap(self.vcfg))
        self.blocked = {}  # route -> rule
        # template v1.0.0: no usable F&G -> rule off (not fail-closed); notice on each switch
        self.f_active = self.f_on and fng_rule_transition((self.fng or {}).get("value") is not None) if self.f_on else False

    @property
    def fng_value(self):
        return (self.fng or {}).get("value")

    def size_mult(self):
        v = self.fng_value
        if self.f_active and v is not None and v > float(self.fcfg.get("greed_above", 75)):
            return float(self.fcfg.get("greed_size_mult", 0.5))
        return 1.0

    def block(self, sold, bought, skip_vwap=False):
        """Rule name that blocks sold->bought this check, else None."""
        buy = sold == "USDC" and bought in TOKENS
        sell = bought == "USDC" and sold in TOKENS
        if not (buy or sell):
            return None
        if self.f_active:  # template v1.0.0: rule is off while F&G is unavailable/too old
            v = self.fng_value
            if buy and v > float(self.fcfg.get("greed_above", 75)):
                return "fear_greed_gt75_no_usdc_buy"
            if sell and v < float(self.fcfg.get("fear_below", 25)):
                return "fear_greed_lt25_no_usdc_sell"
        if self.v_on and not skip_vwap:  # reversals exempt (owner 2026-10-10)
            tok = bought if buy else sold
            q = (self.vwap or {}).get(tok)
            if not q:
                return "vwap24h_unavailable"
            if buy and not q["last"] < q["vwap"]:
                return "vwap24h_not_below"
            if sell and not q["last"] > q["vwap"]:
                return "vwap24h_not_above"
        return None

    def note(self, route, rule):
        if rule:
            self.blocked[route] = rule

    def log_lines(self):
        f = self.fng or {}
        if not self.f_on:
            fl = "off"
        elif f.get("value") is None:
            fl = f"unavailable ({f.get('error')}) -> F&G rule off until fresh data"
        else:
            fl = f"{f['value']} ({f.get('class')}){' size x' + str(self.size_mult()) if self.size_mult() != 1 else ''}"
        if not self.v_on:
            vl = "off"
        elif (self.vwap or {}).get("error"):
            vl = f"unavailable ({self.vwap['error']}) -> USDC routes blocked"
        else:
            parts = []
            for t in TOKENS:
                q = self.vwap.get(t)
                if not q:
                    parts.append(f"{t} n/a")
                else:
                    parts.append(f"{t} {q['last']:g}/{q['vwap']:.6g} {'below' if q['last'] < q['vwap'] else 'above'}")
            vl = ", ".join(parts)
        bl = ", ".join(f"{r} ({why})" for r, why in sorted(self.blocked.items())) or "none"
        return f"- fear_greed: {fl}\n- vwap24h: {vl}\n- rule_blocked: {bl}\n"
