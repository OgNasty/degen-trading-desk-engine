"""SOL staking bucket: JitoSOL (stake1015, 2026-10-15).

A SEPARATE JitoSOL bucket for half of the bot's trading SOL. It is not the
savings-mode JitoSOL: the bucket quantity is recorded in sol-stake-state.json
(bucket_jitosol) and every wallet JitoSOL read subtracts it, so savings,
latches and the Savings panel only ever see savings JitoSOL.

 * Keep stake_share (0.5) of trading SOL (wallet SOL minus a reserve_sol fee
   reserve) as JitoSOL. Every balance reader adds bucket x JitoSOL->SOL rate
   to SOL, so portfolio, P&L vs funded, deposit detection, rebalances, Friday
   RSI, the daily rebalance, the minute check and the dashboard see one SOL
   number. The bot never trades the bucket as its own token.
 * A swap that spends more SOL than is liquid first swaps enough bucket
   JitoSOL back to SOL (best Jupiter route) to cover it and reset to 50/50.
   Runs in venue_quotes build_* inside the trading job (same lock).
 * Idle rule at the end of each edge check: liquid above 70% of the SOL total
   and excess >= 10% of the SOL total in USD (floor $10, cap $500) -> swap
   the excess SOL to JitoSOL (best Jupiter route).
 * enabled=false in sol-stake-config.json swaps the whole bucket back to SOL
   on the next check (or right away: `python sol_stake.py off`).
Bucket swaps are recorded (signatures) so the late-fill catcher ignores them.
Never prints keys.
"""
import json
import os
import sys
import time

import degen_home as _DH
from degen_lend import plan_trade  # same withdraw math as the USDC lend

HOME = _DH.HOME_STR
CFG_PATH = HOME + "/sol-stake-config.json"
STATE_PATH = HOME + "/sol-stake-state.json"
SOL_MINT = "So11111111111111111111111111111111111111112"
JITO_MINT = "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn"
DEFAULT_CFG = {
    "enabled": False, "stake_share": 0.5, "withdraw_buffer": 0.001, "reserve_sol": 0.05,
    "sell_slip_pad": 0.004,
    "idle_deposit": {"enabled": True, "liquid_above": 0.70, "min_usd_pct": 0.10,
                     "min_usd_floor": 10.0, "min_usd_cap": 500.0},
    "rate_max_age_s": 600,
}
LAST = {"read": False, "bucket": 0.0, "rate": None}
_BUSY = {"on": False}


def _log(m):
    print(m, flush=True)


def cfg(path=None):
    c = json.loads(json.dumps(DEFAULT_CFG))
    try:
        with open(path or CFG_PATH, encoding="utf-8") as f:
            for k, v in json.load(f).items():
                if isinstance(v, dict) and isinstance(c.get(k), dict):
                    c[k].update(v)
                else:
                    c[k] = v
    except FileNotFoundError:
        pass
    return c


def enabled(c=None):
    """Interest mode (cost-basis.json interest_mode.enabled) switches USDC lending and the JitoSOL
    bucket together; without that key the module config decides."""
    try:
        import interest_mode as _IM
        f = _IM.flag()
    except Exception:
        f = None
    return bool((c or cfg()).get("enabled")) if f is None else f


def load_state(path=None):
    try:
        with open(path or STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(st, path=None):
    p = path or STATE_PATH
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    os.replace(p + ".tmp", p)


def bucket_qty(st=None):
    return float((load_state() if st is None else st).get("bucket_jitosol") or 0)


def fetch_market(fetch=None):
    """{'rate': JitoSOL->SOL, 'sol_usd', 'jito_usd', 'apy' (%)}."""
    if fetch:
        return fetch()
    import jupkey as _JK
    d = _JK.urlopen_json("https://api.jup.ag/price/v3?ids=" + SOL_MINT + "," + JITO_MINT, 8)
    sol, jito = float(d[SOL_MINT]["usdPrice"]), float(d[JITO_MINT]["usdPrice"])
    out = {"rate": jito / sol, "sol_usd": sol, "jito_usd": jito}
    try:
        import urllib.request
        r = urllib.request.Request("https://kobe.mainnet.jito.network/api/v1/stake_pool_stats",
                                   headers={"User-Agent": "degen-stake/1"})
        with urllib.request.urlopen(r, timeout=8) as resp:
            a = json.loads(resp.read()).get("apy") or []
        out["apy"] = round(float(a[-1]["data"]) * 100, 2) if a else None
    except Exception:
        out["apy"] = None
    return out


def market(max_age=None, fetch=None, save=True):
    st = load_state()
    age = time.time() - float(st.get("rate_t") or 0)
    if st.get("rate") and age < (cfg()["rate_max_age_s"] if max_age is None else max_age):
        return st
    try:
        m = fetch_market(fetch)
        st.update(rate=m["rate"], sol_usd=m["sol_usd"], rate_t=time.time())
        if m.get("apy"):
            st["apy"] = m["apy"]
        if save:
            save_state(st)
    except Exception as e:
        _log(f"STAKE_RATE_ERR {type(e).__name__}")
        if not st.get("rate"):
            raise
    return st


def bucket_sol_for_read(wallet_jitosol):
    """(bucket_sol_value, bucket_qty_used). Bucket is clamped to what the wallet holds."""
    st = load_state()
    b = min(bucket_qty(st), max(0.0, float(wallet_jitosol or 0)))
    if b <= 0:
        LAST.update(read=True, bucket=0.0, rate=st.get("rate"))
        return 0.0, 0.0
    rate = float(market().get("rate"))
    LAST.update(read=True, bucket=b, rate=rate)
    return b * rate, b


def jitosol_in_tok(tok):
    n = 0.0
    for v in ((tok or {}).get("result") or {}).get("value") or []:
        info = v["account"]["data"]["parsed"]["info"]
        if info.get("mint") == JITO_MINT:
            n += float(info["tokenAmount"].get("uiAmount") or 0)
    return n


def accrual_sol(watch):
    r0 = (watch or {}).get("jito_rate")
    if not LAST.get("read") or not LAST.get("bucket") or not r0 or not LAST.get("rate"):
        return 0.0
    return float(LAST["bucket"]) * (float(LAST["rate"]) - float(r0))


def snapshot_rate():
    return LAST.get("rate") if LAST.get("read") and LAST.get("bucket") else None


def idle_min_usd(total_usd, c=None):
    i = (c or cfg())["idle_deposit"]
    return min(float(i["min_usd_cap"]), max(float(i["min_usd_floor"]), float(i["min_usd_pct"]) * total_usd))


def plan_deposit(liquid_avail, bucket_sol, sol_usd, c=None):
    c = c or cfg()
    total = liquid_avail + bucket_sol
    if total <= 0 or not c["idle_deposit"].get("enabled", True):
        return 0.0
    excess = liquid_avail - (1 - float(c["stake_share"])) * total
    if excess <= 0:
        return 0.0
    if liquid_avail / total > float(c["idle_deposit"]["liquid_above"]) and excess * sol_usd >= idle_min_usd(total * sol_usd, c):
        return excess
    return 0.0


# ---------------- chain ----------------
def _addr():
    import venue_quotes as VQ
    return VQ.ADDR


def read_onchain(rpc=None, addr=None):
    """(wallet_sol, wallet_jitosol) at confirmed."""
    import solana_rpc
    rpc = rpc or (lambda m, p: solana_rpc.rpc(m, p, retries=3, timeout=20))
    a = addr or _addr()
    sol = rpc("getBalance", [a, {"commitment": "confirmed"}])["result"]["value"] / 1e9
    tok = rpc("getTokenAccountsByOwner", [a, {"mint": JITO_MINT}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
    return sol, jitosol_in_tok(tok)


def _track(st, kind, kw):
    """tracker1015 (report only): swap cost paid into/out of the bucket vs staking gain.
    Costs are measured against the JitoSOL->SOL market rate cached at the time of the swap."""
    r = float(st.get("rate") or 0)
    j = float(kw.get("jitosol") or 0)
    if r <= 0 or j <= 0 or kw.get("err"):
        return
    t = st.setdefault("track", {"swap_cost_sol": 0.0, "entry_value_sol": 0.0, "entry_jitosol": 0.0,
                                "realized_gain_sol": 0.0, "swaps": 0, "since": int(time.time())})
    if kind == "stake" and kw.get("sol"):
        t["swap_cost_sol"] += float(kw["sol"]) - j * r
        t["entry_value_sol"] += j * r
        t["entry_jitosol"] += j
    elif kind == "unstake":
        avg = t["entry_value_sol"] / t["entry_jitosol"] if t["entry_jitosol"] > 0 else r
        if kw.get("sol_got") is not None:
            t["swap_cost_sol"] += j * r - float(kw["sol_got"])
        t["realized_gain_sol"] += j * (r - avg)
        q = min(j, t["entry_jitosol"])
        t["entry_value_sol"] -= q * avg
        t["entry_jitosol"] -= q
    else:
        return
    t["swaps"] = int(t.get("swaps") or 0) + 1
    for k in ("swap_cost_sol", "entry_value_sol", "entry_jitosol", "realized_gain_sol"):
        t[k] = round(t[k], 9)


def tracker(st=None, rate=None, sol_usd=None):
    """Swap cost vs staking gain so far (SOL and USD). Report only; never used for trading."""
    st = st or load_state()
    t = st.get("track") or {}
    if not t:
        return None
    r = float(rate or st.get("rate") or 0)
    px = float(sol_usd or st.get("sol_usd") or 0)
    b = bucket_qty(st)
    q = min(b, float(t.get("entry_jitosol") or 0))
    avg = (float(t["entry_value_sol"]) / float(t["entry_jitosol"])) if float(t.get("entry_jitosol") or 0) > 0 else r
    gain = float(t.get("realized_gain_sol") or 0) + q * (r - avg)
    cost = float(t.get("swap_cost_sol") or 0)
    out = {"swap_cost_sol": round(cost, 9), "staking_gain_sol": round(gain, 9), "net_sol": round(gain - cost, 9),
           "swaps": int(t.get("swaps") or 0), "since": t.get("since"), "paying_off": gain > cost}
    if px:
        out.update(swap_cost_usd=round(cost * px, 4), staking_gain_usd=round(gain * px, 4), net_usd=round((gain - cost) * px, 4))
    return out


def _event(kind, **kw):
    st = load_state()
    try:
        _track(st, kind, kw)
    except Exception as _te:
        _log(f"STAKE_TRACK_ERR {type(_te).__name__}")
    ev = {"t": int(time.time()), "kind": kind, **kw}
    st["events"] = (st.get("events") or [])[-29:] + [ev]
    if kw.get("sig"):
        st["sigs"] = (st.get("sigs") or [])[-199:] + [kw["sig"]]
        st["last_" + kind] = ev
    save_state(st)
    _log("STAKE_" + kind.upper() + " " + json.dumps({k: v for k, v in ev.items() if k != "t"}))


def _swap(sell, buy, raw_amount):
    """Best Jupiter route swap, signed with the bot key, Helius then Alchemy, confirm.
    Returns (sig, err, uncertain)."""
    import venue_quotes as VQ
    import savings_mode as SM
    VQ.MINTS.setdefault("JitoSOL", JITO_MINT)
    VQ.MINTS.setdefault("SOL", SOL_MINT)

    def _build():
        row = VQ.quote_jupiter_manual(sell, buy, int(raw_amount))
        if not row or not row.get("ok"):
            raise RuntimeError("stake_quote_failed:" + str((row or {}).get("note"))[:80])
        tx, err = VQ.build_jupiter_manual_tx(row["payload"])
        if err:
            raise RuntimeError("stake_build_failed:" + str(err)[:80])
        return [tx]
    _BUSY["on"] = True
    try:
        txs = _build()
        kp = SM._load_keypair()
        return SM.send_built_txs(txs, kp, requote_fn=_build)
    finally:
        _BUSY["on"] = False


def _settle(j0, sig, uncertain, reader, sleep, expect_up):
    """Poll the wallet until the JitoSOL change shows; return the JitoSOL delta (signed)."""
    d = 0.0
    for _ in range(12 if (sig or uncertain) else 0):
        _sol, j1 = reader()
        d = j1 - j0
        if (expect_up and d > 0) or (not expect_up and d < 0):
            return d
        sleep(2.5)
    return d


def stake(sol_amount, reason="", reader=None, swapper=None, sleep=time.sleep):
    reader = reader or read_onchain
    _s0, j0 = reader()
    sig, err, unc = (swapper or _swap)("SOL", "JitoSOL", round(sol_amount * 1e9))
    d = _settle(j0, sig, unc, reader, sleep, True)
    st = load_state()
    if d > 0:
        st["bucket_jitosol"] = round(bucket_qty(st) + d, 9)
        save_state(st)
    _event("stake", sol=round(sol_amount, 9), jitosol=round(d, 9), sig=sig, err=err, uncertain=bool(unc), reason=reason)
    return sig, err


def unstake(jito_qty, reason="", reader=None, swapper=None, sleep=time.sleep):
    reader = reader or read_onchain
    s0, j0 = reader()
    sig, err, unc = (swapper or _swap)("JitoSOL", "SOL", int(jito_qty * 1e9))
    d = _settle(j0, sig, unc, reader, sleep, False)
    st = load_state()
    sol_got = None
    if d < 0:
        st["bucket_jitosol"] = round(max(0.0, bucket_qty(st) + d), 9)
        save_state(st)
        try:  # tracker1015: SOL received (net of the network fee), report only
            sol_got = round(reader()[0] - s0, 9)
        except Exception:
            sol_got = None
    _event("unstake", jitosol=round(-d, 9), sol_got=sol_got, sig=sig, err=err, uncertain=bool(unc), reason=reason)
    return sig, err


def ensure_liquid(input_mint, raw_amount, reason="swap", reader=None, swapper=None, sleep=time.sleep):
    """Before a swap that spends SOL is built: swap bucket JitoSOL back to SOL when liquid
    SOL (minus the fee reserve) is short. No-op unless this process read a bucket."""
    try:
        if _BUSY["on"] or input_mint != SOL_MINT or not LAST.get("read") or not LAST.get("bucket"):
            return None
        c = cfg()
        need = int(raw_amount) / 1e9
        reader = reader or read_onchain
        sol, wj = reader()
        st = load_state()
        b = min(bucket_qty(st), wj)
        liq = max(0.0, sol - float(c["reserve_sol"]))
        if need <= liq or b <= 0:
            return None
        rate = float(market(max_age=120).get("rate"))
        share = float(c["stake_share"]) if enabled(c) else 0.0
        p = plan_trade(liq, b * rate, need, share=share, buffer=float(c["withdraw_buffer"]))
        if p.withdraw <= 0:
            return None
        q = p.withdraw / rate * (1 + float(c["sell_slip_pad"]))
        q = b if q >= b * 0.995 else q
        _log(f"STAKE_PLAN need={need:.9f} liquid={liq:.9f} bucket={b:.9f} -> unstake {q:.9f} JitoSOL")
        return unstake(q, reason=reason, reader=reader, swapper=swapper, sleep=sleep)
    except Exception as e:
        _log(f"STAKE_ERR ensure_liquid {type(e).__name__}: {str(e)[:160]}")
        return None


def tick(reader=None, swapper=None, source="edge_check", sleep=time.sleep):
    import mirror_guard as _MG  # mirrorguard1015
    if _MG.blocked():
        print(_MG.line('sol_stake.tick'), flush=True)
        return {"skipped": "mirror_guard"}
    c = cfg()
    out = {"enabled": enabled(c)}
    st = load_state()
    if not enabled(c) and bucket_qty(st) <= 0:
        return out
    try:
        reader = reader or read_onchain
        sol, wj = reader()
        b = min(bucket_qty(st), wj)
        m = market()
        rate, px = float(m["rate"]), float(m.get("sol_usd") or 0)
        liq = max(0.0, sol - float(c["reserve_sol"]))
        out.update(liquid_sol=round(liq, 9), bucket_jitosol=round(b, 9), bucket_sol=round(b * rate, 9), apy=m.get("apy"))
        try:  # tracker1015: report only
            _tk = tracker(None, rate, px)
            if _tk:
                out["track"] = _tk
        except Exception:
            pass
        if not enabled(c):
            if b > 0:
                sig, err = unstake(b, reason="switched off", reader=reader, swapper=swapper, sleep=sleep)
                out.update(action="unstake_all", sig=sig, err=err)
            return out
        dep = plan_deposit(liq, b * rate, px, c) if px > 0 else 0.0
        if dep > 0:
            sig, err = stake(dep, reason=f"idle ({source})", reader=reader, swapper=swapper, sleep=sleep)
            out.update(action="stake", amount_sol=round(dep, 9), sig=sig, err=err)
        return out
    except Exception as e:
        _log(f"STAKE_ERR tick {type(e).__name__}: {str(e)[:160]}")
        out["err"] = type(e).__name__
        return out


def status():
    sol, wj = read_onchain()
    st = market(max_age=0)
    b = min(bucket_qty(st), wj)
    rate = float(st["rate"])
    c = cfg()
    liq = max(0.0, sol - float(c["reserve_sol"]))
    tot = liq + b * rate
    return {"enabled": enabled(c), "wallet_sol": round(sol, 9), "reserve_sol": c["reserve_sol"],
            "wallet_jitosol_all": round(wj, 9), "bucket_jitosol": round(b, 9),
            "savings_jitosol": round(wj - b, 9), "rate_jitosol_sol": rate,
            "bucket_sol": round(b * rate, 9), "staked_pct": round(100 * b * rate / tot, 2) if tot else 0,
            "apy_pct": st.get("apy"), "idle_min_usd": round(idle_min_usd(tot * float(st.get("sol_usd") or 0), c), 2),
            "last_stake": st.get("last_stake"), "last_unstake": st.get("last_unstake"),
            "tracker": tracker(st, rate, st.get("sol_usd"))}


def _set_enabled(v):
    c = {}
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            c = json.load(f)
    except FileNotFoundError:
        pass
    c["enabled"] = bool(v)
    try:  # template: on/off here flips interest mode (both sides together)
        import interest_mode as _IM
        if _IM.flag() is not None:
            _IM.set_flag(bool(v))
    except Exception:
        pass
    with open(CFG_PATH + ".tmp", "w", encoding="utf-8") as f:
        json.dump(c, f, indent=1)
    os.replace(CFG_PATH + ".tmp", CFG_PATH)


def main(argv):
    cmd = (argv[1:] or ["status"])[0]
    if cmd == "status":
        print(json.dumps(status(), indent=1))
        return 0
    if cmd not in ("on", "off", "tick"):
        print("usage: sol_stake.py status|on|off|tick")
        return 2
    import jobs_lock
    with jobs_lock.hold("stake_" + cmd):
        if cmd in ("on", "off"):
            _set_enabled(cmd == "on")
        print(json.dumps(tick(source="manual_" + cmd), indent=1))
    return 0


if __name__ == "__main__":
    if (sys.argv[1:] or ["status"])[0] != "status":
        import mirror_guard as _MG  # mirrorguard1015: on/off/tick refused on the box mirror
        _MG.check('sol_stake.py')
    sys.exit(main(sys.argv))
