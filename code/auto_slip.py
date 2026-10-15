"""auto_slip (ultra1012): capped automatic slippage for Jupiter manual, Raydium and Orca quotes/fills.

cost-basis.json auto_slippage: {enabled, max_bps (29; hard cap 30), min_bps (2)}.
Jupiter swap/v2/build ignores dynamicSlippage (omitting slippageBps gives its default 50), so the bot sends an explicit
slippageBps to every venue: bps = clamp(min_bps, max_bps, ceil(3 * sigma)), sigma = stdev (bps) of the pair's
check-to-check price-ratio change over the last <=30 checks (prices_usd). Fewer than 6 samples -> max_bps.
Slippage failures (Jupiter 0x1771 SlippageToleranceExceeded, Raydium/Orca too-little-output/slippage errors) are
appended to slip-fails.jsonl; more than 3 in 24h -> fixed max_bps until the 24h count drops back to <=3.
Never raises into a check; any error -> max_bps (30 if disabled). Fallback (>3 fails/24h) = max_bps."""
import json, math, os, re, time
from datetime import datetime, timedelta, timezone

import degen_home as _DH

HOME = _DH.HOME_STR
STATE = os.path.join(HOME, "slip-state.json")
FAILS = os.path.join(HOME, "slip-fails.jsonl")
FIXED = 30
LAST = {}  # (sell, buy) -> bps chosen this check
_CFG = None
_SLIP_RE = re.compile(r"0x1771\b|slippage|TooLittleOutput|too little output|ExceededSlippage|0x1786\b|amount_out_below_minimum|minimum.?amount.?out", re.I)


def cfg():
    global _CFG
    if _CFG is None:
        try:
            cb = json.load(open(os.path.join(HOME, "cost-basis.json"), encoding="utf-8"))
            c = cb.get("auto_slippage") or {}
        except Exception:
            c = {}
        _CFG = {"enabled": bool(c.get("enabled", False)), "max_bps": int(c.get("max_bps", FIXED)), "min_bps": int(c.get("min_bps", 5))}
        _CFG["max_bps"] = max(1, min(FIXED, _CFG["max_bps"]))  # never above 30
        _CFG["min_bps"] = max(1, min(_CFG["max_bps"], _CFG["min_bps"]))
    return _CFG


def _load(p, d):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return d


def _save(obj):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, STATE)


def note_prices(prices, now=None):
    """Called once per check with prices_usd {SYM: usd}."""
    try:
        st = _load(STATE, {})
        s = st.get("samples") or []
        s.append([int(now or time.time()), {k: float(v) for k, v in (prices or {}).items() if v}])
        st["samples"] = s[-30:]
        _save(st)
    except Exception:
        pass


def sigma_bps(sell, buy):
    s = (_load(STATE, {}).get("samples") or [])
    r = [x[1][sell] / x[1][buy] for x in s if sell in x[1] and buy in x[1] and x[1][buy]]
    ch = [(b / a - 1) * 1e4 for a, b in zip(r, r[1:]) if a]
    if len(ch) < 5:
        return None
    m = sum(ch) / len(ch)
    return math.sqrt(sum((c - m) ** 2 for c in ch) / (len(ch) - 1))


def fails_24h(now=None):
    now = now or time.time()
    n = 0
    try:
        for l in open(FAILS, encoding="utf-8"):
            try:
                if now - json.loads(l)["ts"] <= 86400:
                    n += 1
            except Exception:
                pass
    except Exception:
        pass
    return n


def fallback_active(now=None):
    return fails_24h(now) > 3


def bps(sell, buy, venue=None):
    try:
        c = cfg()
        if not c["enabled"]:
            b = FIXED
        elif fallback_active():
            b = c["max_bps"]
            st = _load(STATE, {})
            if not st.get("fallback_logged"):
                print(f"SLIPPAGE_FALLBACK fixed={c['max_bps']}bps fails_24h={fails_24h()} (>3)", flush=True)
                st["fallback_logged"] = int(time.time()); _save(st)
        else:
            sg = sigma_bps(sell, buy)
            b = c["max_bps"] if sg is None else int(min(c["max_bps"], max(c["min_bps"], math.ceil(3 * sg))))
            st = _load(STATE, {})
            if st.get("fallback_logged"):
                st.pop("fallback_logged"); _save(st)
                print("SLIPPAGE_FALLBACK cleared (fails_24h<=3); auto slippage resumed", flush=True)
    except Exception:
        b = (_CFG or {}).get("max_bps", FIXED) if (_CFG or {}).get("enabled") else FIXED  # slip2-29: error -> configured cap
    b = int(max(1, min(FIXED, b)))
    LAST[(sell, buy)] = b
    return b


def is_slip_error(msg):
    return bool(msg) and bool(_SLIP_RE.search(str(msg)))


def note_fail(msg, venue=None, route=None, chosen=None):
    """Record a slippage failure (only if msg looks like one). Returns True when recorded."""
    try:
        if not is_slip_error(msg):
            return False
        try:  # one send attempt is logged once per RPC provider: count it once
            last = open(FAILS, encoding="utf-8").read().strip().splitlines()[-1]
            if time.time() - json.loads(last)["ts"] <= 5:
                return False
        except Exception:
            pass
        with open(FAILS, "a", encoding="utf-8", newline="") as f:
            f.write(json.dumps({"ts": int(time.time()), "venue": venue, "route": route, "bps": chosen,
                                "msg": str(msg)[:160]}) + "\n")
        print(f"SLIPPAGE_FAIL venue={venue} route={route} bps={chosen} fails_24h={fails_24h()}", flush=True)
        return True
    except Exception:
        return False


def status():
    c = cfg()
    return {"enabled": c["enabled"], "max_bps": c["max_bps"], "min_bps": c["min_bps"], "fails_24h": fails_24h(),
            "fallback_fixed": fallback_active(), "chosen": {f"{a}>{b}": v for (a, b), v in LAST.items()}}
