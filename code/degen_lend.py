"""Jupiter Lend (Earn) for the bot's USDC (lend1015, 2026-10-15).

USDC only:
 * Keep lend_share (0.5) of the bot's USDC lent as jlUSDC, the rest liquid.
 * A swap that spends more USDC than is liquid first withdraws what is short
   plus enough to reset to 50/50 of what remains (+0.1% buffer). This runs in
   venue_quotes build_* (every swap path builds there), inside the job that is
   trading, so it shares that job's lock: no collisions, no double action.
 * Idle rule (end of each edge check): liquid above idle_above (70%) of the
   USDC total AND excess >= idle_min_usd -> deposit back to 50/50.
   idle_min_usd scales with the balance: 10% of the USDC total, floor $10, cap $500.
 * enabled=false in lend-config.json withdraws everything on the next check
   (or right away: `python degen_lend.py off`).

Valuation: every balance reader adds jlUSDC shares x share value into "USDC",
so portfolio value, P&L vs funded, deposit detection, rebalances, Friday RSI,
the daily rebalance, reports and the dashboard all see one USDC total and a
lend move is never read as a deposit, withdrawal or loss. jlUSDC is never a
trading token. Interest is credited as expected USDC in deposit detection.
Signing/sending reuse savings_mode (DPAPI/env key, Helius then Alchemy,
confirm). Never prints keys.
"""
import json
import os
import sys
import time
import urllib.request
from dataclasses import dataclass

import degen_home as _DH

HOME = _DH.HOME_STR
CFG_PATH = HOME + "/lend-config.json"
STATE_PATH = HOME + "/lend-state.json"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
JL_MINT = "9BEcn9aPEmhSPbPQeFGjidRiEKki46fVQDyPpSQXPA2D"
DEC = 6
TOKENKEG = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
DEFAULT_CFG = {
    "enabled": False,
    "lend_share": 0.5,
    "withdraw_buffer": 0.001,
    "idle_deposit": {"enabled": True, "liquid_above": 0.70, "min_usd_pct": 0.10,
                     "min_usd_floor": 10.0, "min_usd_cap": 500.0},
    "min_withdraw_usd": 0.05,
    "share_value_max_age_s": 600,
    "api_base": "https://api.jup.ag/lend/v1/earn",  # 1.1.2: Jupiter retires the old lite-api host on 2026-10-30; same paths, keyless
}
# Last hooked balance read in this process (set by lent_usdc_from_tok).
LAST = {"read": False, "shares": 0.0, "sv": None, "lent": 0.0}


def _log(msg):
    print(msg, flush=True)


def cfg(path=None):
    c = json.loads(json.dumps(DEFAULT_CFG))
    try:
        with open(path or CFG_PATH, encoding="utf-8") as f:
            u = json.load(f)
        for k, v in u.items():
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
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, p)


# ---------------- pure rebalance math (ported from fx_lend) ----------------
@dataclass
class Plan:
    action: str
    trade_amount: float = 0.0
    withdraw: float = 0.0
    reason: str = ""


def plan_trade(liquid, lent, need, *, share=0.5, buffer=0.001, pool_free=float("inf")):
    if need <= 0:
        return Plan("skip", reason="nothing to spend")
    if need <= liquid:
        return Plan("trade", need, 0.0, "covered by liquid")
    total = liquid + lent
    reason = ""
    if need > total:
        need, reason = total, "capped to total; "
    remain = total - need
    w = (need - liquid) + (1 - share) * remain
    w = min(w * (1 + buffer), lent, pool_free)
    if liquid + w + 1e-9 < need:
        return Plan("shrink", liquid + w, w, reason + "pool limited")
    return Plan("withdraw_then_trade", need, w, reason + "withdraw then reset to target")


def idle_min_usd(total_usd, c=None):
    i = (c or cfg())["idle_deposit"]
    return min(float(i["min_usd_cap"]), max(float(i["min_usd_floor"]), float(i["min_usd_pct"]) * total_usd))


def plan_deposit(liquid, lent, c=None):
    """Idle rule only. Returns USDC to deposit (0 if none)."""
    c = c or cfg()
    total = liquid + lent
    if total <= 0 or not c["idle_deposit"].get("enabled", True):
        return 0.0
    excess = liquid - (1 - float(c["lend_share"])) * total
    if excess <= 0:
        return 0.0
    if liquid / total > float(c["idle_deposit"]["liquid_above"]) and excess >= idle_min_usd(total, c):
        return excess
    return 0.0


# ---------------- Jupiter Lend API ----------------
def _req(path, body=None, c=None, timeout=15):
    base = (c or cfg())["api_base"].rstrip("/")
    h = {"Content-Type": "application/json", "User-Agent": "degen-lend/1"}
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, headers=h)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read())


def pool_info(c=None, fetch=None):
    toks = (fetch or (lambda: _req("/tokens", c=c, timeout=8)))()
    for t in toks:
        if t.get("address") == JL_MINT or t.get("assetAddress") == USDC_MINT:
            L = t.get("liquiditySupplyData") or {}
            lim = int(L.get("withdrawalLimit") or 0) or 10 ** 30
            return {"sv": int(t["convertToAssets"]) / 10 ** DEC, "apr": int(t["totalRate"]) / 100.0,
                    "supply_apr": int(t.get("supplyRate") or 0) / 100.0,
                    "rewards_apr": int(t.get("rewardsRate") or 0) / 100.0,
                    "free": min(int(L.get("withdrawable") or 0), lim) / 10 ** DEC}
    raise RuntimeError("jlUSDC not in /tokens")


def share_value(max_age=None, fetch=None, st=None, save=True):
    """Cached jlUSDC share value (USDC per share). Refreshes when older than max_age.
    A failed refresh keeps the cached value (moves ~6%/yr, staleness is harmless)."""
    st = load_state() if st is None else st
    c = cfg()
    age = time.time() - float(st.get("sv_t") or 0)
    if st.get("sv") and age < (c["share_value_max_age_s"] if max_age is None else max_age):
        return float(st["sv"])
    try:
        p = pool_info(c, fetch)
        st.update(sv=p["sv"], sv_t=time.time(), apr=p["apr"], supply_apr=p["supply_apr"],
                  rewards_apr=p["rewards_apr"], pool_free=p["free"])
        if save:
            save_state(st)
        return p["sv"]
    except Exception as e:
        _log(f"LEND_SV_ERR {type(e).__name__}")
        if st.get("sv"):
            return float(st["sv"])
        raise


def jl_shares_from_tok(tok):
    """Sum of jlUSDC (ui units) in a getTokenAccountsByOwner jsonParsed result."""
    vals = ((tok or {}).get("result") or {}).get("value") or []
    n = 0.0
    for v in vals:
        info = v["account"]["data"]["parsed"]["info"]
        if info.get("mint") == JL_MINT:
            n += float(info["tokenAmount"].get("uiAmount") or 0)
    return n


def lent_usdc_from_tok(tok, record=True):
    """USDC value of the lent position in this read. Balance readers add it to USDC."""
    sh = jl_shares_from_tok(tok)
    sv = share_value() if sh > 0 else (LAST.get("sv") or 0.0)
    lent = sh * sv if sh > 0 else 0.0
    if record:
        LAST.update(read=True, shares=sh, sv=sv, lent=lent)
        if sh > 0 or LAST.get("_had"):
            LAST["_had"] = sh > 0
            try:
                st = load_state()
                if abs(float(st.get("shares") or 0) - sh) > 1e-9 or abs(float(st.get("lent_usdc") or 0) - lent) > 0.005:
                    st.update(shares=sh, lent_usdc=round(lent, 6), lent_t=time.time())
                    save_state(st)
            except Exception:
                pass
    return lent


def accrual_usdc(watch):
    """Interest since the deposit-watch snapshot (expected USDC rise, not a deposit)."""
    sv0 = (watch or {}).get("lend_sv")
    if not LAST.get("read") or not LAST.get("shares") or not sv0 or not LAST.get("sv"):
        return 0.0
    return max(0.0, float(LAST["shares"]) * (float(LAST["sv"]) - float(sv0)))


def snapshot_sv():
    return LAST.get("sv") if LAST.get("read") and LAST.get("shares") else None


# ---------------- on-chain read / move ----------------
def _addr():
    import venue_quotes as VQ
    return VQ.ADDR


def read_onchain(rpc=None, addr=None):
    """(liquid_usdc, jl_shares) at confirmed commitment, ui units."""
    import solana_rpc
    rpc = rpc or (lambda m, p: solana_rpc.rpc(m, p, retries=3, timeout=20))
    tok = rpc("getTokenAccountsByOwner", [addr or _addr(), {"programId": TOKENKEG},
                                          {"encoding": "jsonParsed", "commitment": "confirmed"}])
    liq = 0.0
    for v in tok["result"]["value"]:
        info = v["account"]["data"]["parsed"]["info"]
        if info.get("mint") == USDC_MINT:
            liq += float(info["tokenAmount"].get("uiAmount") or 0)
    return liq, jl_shares_from_tok(tok)


def _event(kind, amount, **kw):
    st = load_state()
    ev = {"t": int(time.time()), "kind": kind, "usdc": round(amount, 6), **kw}
    st["events"] = (st.get("events") or [])[-29:] + [ev]
    if kw.get("sig"):
        st["last_" + kind] = ev
    save_state(st)
    _log("LEND_" + kind.upper() + " " + json.dumps({k: v for k, v in ev.items() if k != "t"}))


def build_tx(kind, body, addr):
    """Unsigned v0 tx: CU limit/price, idempotent ATA create (USDC + jlUSDC; the API's
    /deposit expects the jlUSDC account to exist), then the Jupiter Lend instructions."""
    import base64
    import venue_quotes as VQ
    from solders.instruction import Instruction, AccountMeta
    from solders.message import MessageV0
    from solders.hash import Hash
    from solders.transaction import VersionedTransaction
    from solders.signature import Signature
    import solana_rpc
    r = _req("/" + kind + "-instructions", body)
    api_ixs = [VQ._api_ix(ix) for ix in (r.get("instructions") or [])] if isinstance(r, dict) else []
    if not api_ixs:
        raise RuntimeError("lend_build_no_instructions")
    pk = VQ._pk
    owner = pk(addr)

    def ata_ix(mint):
        return Instruction(pk(VQ.ATA_PROGRAM), bytes([1]), [
            AccountMeta(owner, True, True), AccountMeta(pk(VQ.ata(addr, mint, TOKENKEG)), False, True),
            AccountMeta(owner, False, False), AccountMeta(pk(mint), False, False),
            AccountMeta(pk(VQ.SYSTEM_PROGRAM), False, False), AccountMeta(pk(TOKENKEG), False, False)])
    bh = solana_rpc.rpc("getLatestBlockhash", [{"commitment": "confirmed"}], retries=3, timeout=20)["result"]["value"]["blockhash"]
    body_ixs = [ata_ix(USDC_MINT), ata_ix(JL_MINT)] + api_ixs
    micro = int(VQ.raydium_priority_micro())

    def compile_(cu):
        msg = MessageV0.try_compile(owner, [VQ._cu_limit_ix(cu), VQ._cu_price_ix(micro)] + body_ixs, [], Hash.from_string(bh))
        tx = VersionedTransaction.populate(msg, [Signature.default() for _ in range(int(msg.header.num_required_signatures))])
        return base64.b64encode(bytes(tx)).decode()
    cu = 400_000
    try:
        sim = solana_rpc.rpc("simulateTransaction", [compile_(1_400_000), {
            "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True,
            "commitment": "confirmed"}], retries=2, timeout=20)
        val = (sim.get("result") or {}).get("value") or {}
    except Exception as e:
        val = {}
        _log(f"LEND_SIM_RPC_ERR {type(e).__name__}")
    if val.get("err") is not None:
        raise RuntimeError(f"lend_sim_failed:{str(val.get('err'))[:80]}")  # never send a tx that would fail
    if val.get("unitsConsumed"):
        cu = int(int(val["unitsConsumed"]) * 1.3) + 10_000
    return compile_(cu)


def _send(kind, raw_amount, addr=None, sender=None):
    """Build via Jupiter Lend API, sign with the bot key, send Helius->Alchemy, confirm.
    kind: deposit | withdraw (amount = USDC base units) | redeem (amount = jlUSDC shares base units).
    Returns (sig, err, uncertain)."""
    if sender is not None:
        return sender(kind, raw_amount)
    import savings_mode as SM
    addr = addr or _addr()
    body = {"asset": USDC_MINT, "signer": addr}
    body["shares" if kind == "redeem" else "amount"] = str(int(raw_amount))

    def _build():
        return [build_tx(kind, body, addr)]

    txs = _build()
    kp = SM._load_keypair()
    return SM.send_built_txs(txs, kp, requote_fn=_build)


def _wait_liquid(target, tries=10, step=2.0, reader=None, sleep=time.sleep):
    liq = sh = None
    for _ in range(tries):
        liq, sh = (reader or read_onchain)()
        if liq + 1e-6 >= target:
            return liq, sh
        sleep(step)
    return liq, sh


def withdraw_usdc(amount, shares_all=None, reason="", sender=None, reader=None, sleep=time.sleep):
    """Withdraw amount USDC (or redeem all shares when shares_all is given)."""
    liq0, _ = (reader or read_onchain)()
    if shares_all:
        sig, err, unc = _send("redeem", round(shares_all * 10 ** DEC), sender=sender)
    else:
        sig, err, unc = _send("withdraw", int(amount * 10 ** DEC), sender=sender)
    _event("withdraw", amount, sig=sig, err=err, uncertain=bool(unc), reason=reason)
    if sig:
        _wait_liquid(liq0 + amount * 0.999, reader=reader, sleep=sleep)
    return sig, err


def deposit_usdc(amount, reason="", sender=None):
    sig, err, unc = _send("deposit", int(amount * 10 ** DEC), sender=sender)
    _event("deposit", amount, sig=sig, err=err, uncertain=bool(unc), reason=reason)
    return sig, err


def ensure_liquid(input_mint, raw_amount, reason="swap", reader=None, sender=None, sleep=time.sleep):
    """Called right before a swap is built. Withdraws from Lend when the swap spends more
    USDC than is liquid. No-op unless this process has read a lent position."""
    try:
        if input_mint != USDC_MINT or not LAST.get("read") or not LAST.get("shares"):
            return None
        c = cfg()
        need = int(raw_amount) / 10 ** DEC
        liq, sh = (reader or read_onchain)()
        LAST["shares"] = sh
        if need <= liq + 1e-6 or sh <= 0:
            return None
        st = load_state()
        sv = share_value(max_age=120)
        lent = sh * sv
        share = float(c["lend_share"]) if enabled(c) else 0.0
        p = plan_trade(liq, lent, need, share=share, buffer=float(c["withdraw_buffer"]),
                       pool_free=float(st.get("pool_free") or float("inf")))
        if p.withdraw <= 0:
            return None
        w = max(p.withdraw, float(c["min_withdraw_usd"]))
        _log(f"LEND_PLAN need={need:.6f} liquid={liq:.6f} lent={lent:.6f} -> {p.action} withdraw={w:.6f}")
        if w >= lent * 0.999:
            return withdraw_usdc(lent, shares_all=sh, reason=reason, sender=sender, reader=reader, sleep=sleep)
        return withdraw_usdc(w, reason=reason, sender=sender, reader=reader, sleep=sleep)
    except Exception as e:
        _log(f"LEND_ERR ensure_liquid {type(e).__name__}: {str(e)[:160]}")
        return None


def tick(reader=None, sender=None, source="edge_check"):
    """End-of-check housekeeping (caller holds the trading context/lock).
    Disabled with a position -> withdraw all. Enabled -> idle rule deposit.
    Returns a small status dict for last-check.json / the dashboard."""
    import mirror_guard as _MG  # mirrorguard1015
    if _MG.blocked():
        print(_MG.line('degen_lend.tick'), flush=True)
        return {"skipped": "mirror_guard"}
    c = cfg()
    out = {"enabled": enabled(c)}
    if not enabled(c) and not LAST.get("shares") and not float(load_state().get("shares") or 0):
        return out
    try:
        liq, sh = (reader or read_onchain)()
        sv = share_value()
        lent = sh * sv
        st = load_state()
        if abs(float(st.get("shares") or 0) - sh) > 1e-9:
            st.update(shares=sh, lent_usdc=round(lent, 6), lent_t=time.time())
            save_state(st)
        out.update(liquid=round(liq, 6), lent=round(lent, 6), apr=st.get("apr"))
        if not enabled(c):
            if sh > 0:
                sig, err = withdraw_usdc(lent, shares_all=sh, reason="switched off", sender=sender, reader=reader)
                out.update(action="withdraw_all", sig=sig, err=err)
            return out
        dep = plan_deposit(liq, lent, c)
        if dep > 0:
            sig, err = deposit_usdc(dep, reason=f"idle ({source})", sender=sender)
            out.update(action="deposit", amount=round(dep, 6), sig=sig, err=err)
        return out
    except Exception as e:
        _log(f"LEND_ERR tick {type(e).__name__}: {str(e)[:160]}")
        out["err"] = type(e).__name__
        return out


def status():
    liq, sh = read_onchain()
    sv = share_value(max_age=0)
    st = load_state()
    tot = liq + sh * sv
    return {"enabled": enabled(), "liquid_usdc": round(liq, 6), "jl_shares": sh, "share_value": sv,
            "lent_usdc": round(sh * sv, 6), "lent_pct": round(100 * sh * sv / tot, 2) if tot else 0,
            "apr_pct": st.get("apr"), "idle_min_usd": round(idle_min_usd(tot), 2),
            "last_deposit": st.get("last_deposit"), "last_withdraw": st.get("last_withdraw")}


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
    tmp = CFG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f, indent=1)
    os.replace(tmp, CFG_PATH)


def main(argv):
    cmd = (argv[1:] or ["status"])[0]
    if cmd == "status":
        print(json.dumps(status(), indent=1))
        return 0
    if cmd not in ("on", "off", "tick"):
        print("usage: degen_lend.py status|on|off|tick")
        return 2
    import jobs_lock
    with jobs_lock.hold("lend_" + cmd):
        if cmd in ("on", "off"):
            _set_enabled(cmd == "on")
        print(json.dumps(tick(source="manual_" + cmd), indent=1))
    return 0


if __name__ == "__main__":
    if (sys.argv[1:] or ["status"])[0] != "status":
        import mirror_guard as _MG  # mirrorguard1015: on/off/tick refused on the box mirror
        _MG.check('degen_lend.py')
    sys.exit(main(sys.argv))
