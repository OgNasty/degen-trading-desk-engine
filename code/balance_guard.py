"""Bad balance read guard (bugfix1012, 2026-10-12, owner): fail closed.

A coin that was worth > DUST_USD in the last trusted snapshot (deposit_watch.balances)
and now reads missing or 0 is re-read once. If it is still 0 the check/job ends as an
error with no trades. Only after ACCEPT_AFTER consecutive runs that all read 0 is the
zero accepted (a real full sell-out), recorded in balance-guard.json.
"""
import json, os, time

COINS = ("SOL", "USDC", "cbBTC", "WETH")
DUST_USD = 0.50
ACCEPT_AFTER = 3          # consecutive runs reading 0 before a zero is believed
STREAK_GAP_SEC = 30 * 60  # runs further apart than this do not count as consecutive


class BadBalanceRead(RuntimeError):
    def __init__(self, coins):
        super().__init__("bad_balance_read " + ",".join(coins))
        self.coins = coins


def suspicious(prev, now, prices, dust_usd=DUST_USD):
    out = []
    for s in COINS:
        p = float((prev or {}).get(s) or 0)
        px = 1.0 if s == "USDC" else float((prices or {}).get(s) or 0)
        if p <= 0 or (px > 0 and p * px < dust_usd):
            continue
        v = (now or {}).get(s)
        if v is None or float(v) <= 0:
            out.append(s)
    return out


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(path, st):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, path)


def guarded_read(read_fn, prev, prices, state_path, log=print, now=time.time, sleep=time.sleep, retry_wait=2.0):
    """read_fn() -> balances dict (or (bals, extra) tuple). Returns read_fn's result or raises BadBalanceRead."""
    res = read_fn()
    bals = res[0] if isinstance(res, tuple) else res
    bad = suspicious(prev, bals, prices)
    if bad:
        log(f"BALANCE_GUARD zero read {bad}; retrying once")
        sleep(retry_wait)
        res = read_fn()
        bals = res[0] if isinstance(res, tuple) else res
        bad = suspicious(prev, bals, prices)
    st = _load(state_path)
    if not bad:
        if st:
            _save(state_path, {})
        return res
    t = now()
    streak = st.get("streak", 0) if (st.get("coins") == bad and t - float(st.get("t") or 0) <= STREAK_GAP_SEC) else 0
    streak += 1
    _save(state_path, {"coins": bad, "streak": streak, "t": t})
    if streak >= ACCEPT_AFTER:
        log(f"BALANCE_GUARD_ACCEPT {bad} read 0 on {streak} consecutive runs; treating as real")
        _save(state_path, {})
        return res
    raise BadBalanceRead(bad)
