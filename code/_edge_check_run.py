#!/usr/bin/env python3
"""Primary Solana edge check — best net of Jupiter manual, Raydium, and Orca (top-1).

Quote equals execution: Jupiter swap/v2/build with platformFeeBps=0 (no Jupiter
commission) and Raydium. The winning venue is the one that is signed and submitted.
A stale winning route is re-quoted on those two venues together. The other
pairs are not re-quoted. A quote that is still fresh is reused. Orca Whirlpool is a third venue on
the single best-ranked candidate only. Never Phantom.

Locked 2026-10-05 (edge_policies.py):
- Quote cap: every basis route and unreversed-window reverse is ranked by an
  ESTIMATED fee-inclusive gap_net (USD marks, est gas, no venue call). Only the
  top 5 (quote5 2026-10-11, all concurrent) get a Jupiter+Raydium quote (allowed-pairs.json quote_cap.top_n);
  the top-1 of those also gets an Orca Whirlpool quote.
- Reverse candidates expire after reverse_last_trade.max_age_hours (96).
- Flat-market skip when SOL/cbBTC/WETH all moved < flat_market_skip.threshold_pct
  vs the last NON-skipped check (Jupiter lite prices; no venue quotes).
- Route hit-rate cooldown: no fill in 7d drops that directed route from the top-N quote slots
  for 24h (route-hit-state.json); fill remaining slots with best cooled routes.
- Per-pair P&L size tilt on basis sale size (cost-basis.json pair_pnl_size_tilt,
  pair_pnl.py, pair-pnl-state.json): pair realized P&L% > +0.25% -> x1.25 and
  that trade's caps rise to 18.75% portfolio / 50% balance; < -0.25% -> x0.75;
  else x1.0. Clamp to the pair's caps (0.02 SOL buffer always), then
  profit_sale_size_cut. Reverses keep mirrored size. The old portfolio-wide
  funded-P&L tilt is retired from sizing (wallet funded P&L display unchanged).
- Raydium first on a pair after repeated Jupiter "Program failed to complete"
  (jupiter-ix6-state.json).
- Alchemy fallback cap: 1 per directed route attempt, 2 per check.
- No fees-vs-edge tracking (retired).
Wallet is wallet via SOLANA_TRADING_PRIVATE_KEY. This file does not print keys.

Fills: at most ONE trade per trading pair per check (the best fee-inclusive
gap_net on that pair among basis and unreversed last-3 reverses). Different
pairs that each clear may ALL fill in the same check, sequentially, with
balances refreshed between fills so the next does not oversell.

Run with .venv python so solders loads from that
environment. The signer is imported once per process and the keypair is built
once, on the first fill this process actually signs. There is no resident
signer process: this process signs only the swap it just chose, then exits.
A long-lived helper holding the key could sign a trade it was not asked for.

Bad conditions skip a winning quote instead of forcing a fill. This file only.
Not the Friday RSI check, not the Friday band fallback, and not the morning
underweight check. Friday RSI runs on its own schedule. The old Friday bands (two smallest
>=5%, third >=10%, largest <=75%) run only when that RSI check sends no trade.
This edge check does not run either of them. A skipped quote is
not a fill and does not change the same-pair streak or pair multipliers. If no
other pair clears, the check is a normal skip. Skip-halving counts that quiet check: after 2 quiet checks in a row the multiplier is halved once, and further quiet checks in that streak do not halve. There is no multiplier floor and no once-per-hour timer.
A fade-trend skip is separate from price impact and the priority-fee spike.
It refuses an edge-check sell of SOL, USDC, cbBTC, or WETH, including a sell
that funds a savings or memecoin buy, when that token is strictly above both
its USD price at the previous check and its USD price 24 hours ago. The
memecoin impact-cap exemption does not waive it. Friday RSI, the Friday band
fallback, and the morning underweight trade are other paths and are not checked here.
"""
import mirror_guard as _MG  # mirrorguard1015: box mirror w/o fallback-active.json -> exit now
_MG.check("_edge_check_run")
import tokens_book
import degen_home as _DH  # portable base dir (pcmove 2026-10-10)
open(_DH.HOME_STR + "/trades.jsonl", "a", encoding="utf-8", newline="").close()  # fresh install: start with an empty trade log
import base64, json, math, sys, time, subprocess, random, os
# bugfix1012 (owner 2026-10-12): never run alongside a scheduled job (morning/daily v2,
# Friday RSI/trim, their savings writers). They hold scheduled-job.lock for their whole run.
import atexit as _atexit
import jobs_lock as _JL
FILLED_THIS_CHECK = [False]


def _job_skip_exit(why):
    print(f"SKIP scheduled job running ({why})", flush=True)
    print("LOG_SUMMARY " + json.dumps({"result": "SKIP", "log": "scheduled_job", "send_attempt": 0,
                                       "closest": None, "gap_pct": None, "gap_net_pct": None,
                                       "quiet": None, "quiet_thr": None, "blocked": 0}), flush=True)
    print("RESULT SKIP", flush=True)
    sys.exit(0)


_jl0 = _JL.held()
if _jl0:
    _job_skip_exit(f"{_jl0.get('job')} at start")
_JL.edge_mark_set()
_atexit.register(_JL.edge_mark_clear)


from concurrent.futures import ThreadPoolExecutor
import venue_quotes as VQ
import savings_mode
import memecoin_mode
import fade_trend
import rev_record as _REV  # revflag 2026-10-11 (record-only)
import friday_rsi
import h093 as H093  # optional H093 preset: cost-basis h093.enabled (off by default)
import jupiter_raydium_fallback as JRF
import edge_policies as EP
import pair_pnl as PP
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from degen_home import TZ_NAME as _TZ_NAME
# Once per process, before any quote, so a later fill does not pay the import
# while the quote ages. Python caches these; do not import them again per check.
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

PT = ZoneInfo(_TZ_NAME)

# Rebalance fills are not quiet checks and are not edge fills.
# rsi_rebalance is isolated the same way as band / underweight.
# friday_rebalance aliases stay in the set so an old kind cannot reset skip-halving.
_REBALANCE_FILL_KINDS = friday_rsi.ISOLATED_FILL_KINDS

def apply_skip_edge_halving(seh, outcome, fill_info=None, local_tradable=True):
    """Mutate skip_edge_halving for one check outcome.

    Quiet check (outcome SKIP, nothing filled), including a fade, price-impact,
    or fee skip: increment consecutive_quiet_checks. When that count reaches quiet_checks_to_halve (60)
    and this streak has not already halved, set current_multiplier to half of
    its stored value exactly once and set streak_halved. Later quiet checks in
    the streak do not halve. No multiplier floor and no once-per-hour timer.

    Edge fill: current_multiplier 1.0, consecutive_quiet_checks 0, streak_halved
    false, skips_since_fill 0.

    Rebalance fill: do not change the multiplier, the quiet count, or the flag.

    Returns quiet_halved, quiet_unchanged, edge_reset, rebalance_unchanged, or unchanged.
    """
    if not isinstance(seh, dict):
        return "unchanged"
    if outcome == "SKIP":
        quiet = int(seh.get("consecutive_quiet_checks", 0)) + 1
        skips = int(seh.get("skips_since_fill", 0)) + 1
        mult = float(seh.get("current_multiplier", 1.0))
        already = bool(seh.get("streak_halved", False))
        halved = False
        # rules1012 2026-10-11: halve after quiet_checks_to_halve (60) quiet checks, was 2.
        import h093 as _H093  # H093 preset: 30 when cost-basis h093.enabled
        _need = _H093.quiet_to_halve(globals().get("cb"), int(seh.get("quiet_checks_to_halve", 60) or 60))
        if quiet >= _need and not already:
            mult = mult * 0.5
            already = True
            halved = True
        seh["consecutive_quiet_checks"] = quiet
        seh["streak_halved"] = already
        seh["skips_since_fill"] = skips
        seh["current_multiplier"] = mult
        # Stale hourly gate and multiplier floor must not come back.
        seh.pop("last_halve_pt", None)
        seh.pop("min_multiplier", None)
        seh.pop("halve_interval_seconds", None)
        return "quiet_halved" if halved else "quiet_unchanged"
    if (
        outcome == "FILL"
        and local_tradable
        and isinstance(fill_info, dict)
        and fill_info.get("signature")
    ):
        kind = fill_info.get("fill_kind")
        edge = (
            fill_info.get("counts_as_edge", True) is not False
            and kind not in _REBALANCE_FILL_KINDS
        )
        if edge:
            seh["current_multiplier"] = 1.0
            seh["skips_since_fill"] = 0
            seh["consecutive_quiet_checks"] = 0
            seh["streak_halved"] = False
            return "edge_reset"
        return "rebalance_unchanged"
    return "unchanged"


def maybe_any_profit_bypass(sc, route=None):
    """Promote clears/clears_net when 100-quiet (or oneshot) + edge_net > 0."""
    _h = sc.get("hurdle_pct") if isinstance(sc, dict) else None
    if _h is not None:  # knife_hurdle_v1: the unlock must also clear the $ hurdle
        _t = dict(sc)
        try:
            _t["edge_net"] = float(sc.get("edge_net")) - float(_h)
        except (TypeError, ValueError):
            return False
        if not EP.apply_any_profit_bypass(
                _t, ANY_PROFIT_CFG, ANY_PROFIT_QUIET, route=route, oneshot_route=ONESHOT_ANY_PROFIT_ROUTE,
                min_edge_pct=(0.0 if sc.get("reverse_of_rsi") else float(MIN_FLOOR.get("any_profit_min_edge_pct", 0.25) if MIN_FLOOR.get("enabled", True) else 0)),
                min_usd=float(MIN_FLOOR.get("any_profit_min_usd", 0) if MIN_FLOOR.get("enabled", True) else 0)):
            return False
    fired = EP.apply_any_profit_bypass(
        sc, ANY_PROFIT_CFG, ANY_PROFIT_QUIET,
        route=route, oneshot_route=ONESHOT_ANY_PROFIT_ROUTE,
        min_edge_pct=(0.0 if sc.get("reverse_of_rsi") else float(MIN_FLOOR.get("any_profit_min_edge_pct", 0.25) if MIN_FLOOR.get("enabled", True) else 0)),
        min_usd=float(MIN_FLOOR.get("any_profit_min_usd", 0) if MIN_FLOOR.get("enabled", True) else 0),
    )
    if fired:
        print(
            f"ANY_PROFIT_BYPASS route={route} edge_net={sc.get('edge_net')} "
            f"quiet={ANY_PROFIT_QUIET} thr={ANY_PROFIT_CFG.get('quiet_checks_threshold')} "
            f"oneshot={ONESHOT_ANY_PROFIT_ROUTE!r}",
            flush=True,
        )
    return fired

ADDR_OLD = ""  # template: no legacy wallet  # stranded, no key — never quote, balance, or fill
ADDR_LOCAL = __import__("degen_home").wallet_address()
ADDR = ADDR_LOCAL  # live quotes, balances, and fills: wallet only
MINTS = {
    "SOL": "So11111111111111111111111111111111111111112",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "cbBTC": "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij",
    "WETH": "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
}
DEC = {"SOL": 9, "USDC": 6, "cbBTC": 8, "WETH": 8}
import solana_rpc
# Alchemy fallback cap for this whole check process (edge fills + hooks).
ALCHEMY = EP.AlchemyBudget(EP.ALCHEMY_PER_ROUTE_MAX, EP.ALCHEMY_PER_CHECK_MAX)
solana_rpc.ALCHEMY_GATE = ALCHEMY
RAY_PREF_PATH = _DH.HOME_STR + "/jupiter-ix6-state.json"
RAY_PREF = EP.RaydiumPreferState.load(RAY_PREF_PATH)
# Manual market quote lives in venue_quotes.JUP_MANUAL. Ultra is not requested.
QUOTE_GAP = 7.0
# Blockhash window is about a minute. Re-quote the winner only after this.
QUOTE_STALE_SEC = 45.0
RAYDIUM_QUOTE_CU = 250_000

def pair_tagged_memecoin(pair_info):
    """True only when the pair is tagged memecoin. Major pairs are not."""
    if not isinstance(pair_info, dict):
        return False
    if pair_info.get("memecoin") is True:
        return True
    return str(pair_info.get("category") or "").strip().lower() == "memecoin"

def _median(vals):
    s = sorted(float(v) for v in vals)
    n = len(s)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0

def bad_condition_config(cb):
    """Frozen thresholds. Spike test is attached tip versus the signature fee, not a median."""
    bcs = cb.get("bad_condition_skip") if isinstance(cb, dict) else None
    bcs = bcs if isinstance(bcs, dict) else {}
    fee = bcs.get("fee_spike") if isinstance(bcs.get("fee_spike"), dict) else {}
    return {
        "enabled": bool(bcs.get("enabled", True)),
        "price_impact_max_pct": float(bcs.get("price_impact_max_pct", 0.3)),
        "memecoin_exempt": bool(bcs.get("memecoin_exempt", True)),
        "memecoin_mode_enabled": bool((cb.get("memecoin_mode") or {}).get("enabled")) if isinstance(cb, dict) else False,
        "tip_multiple": float(fee.get("tip_multiple", 2)),
        "base_signature_lamports": int(fee.get("base_signature_lamports") or 5000),
    }

def bad_condition_reasons(cfg, pair_info, impact_pct, row):
    """Why the winning quote must not fill. Empty means it may fill.

    Price impact: strict > price_impact_max_pct. Exempt only when memecoin_mode
    is on, memecoin_exempt is on, and the pair is tagged memecoin.
    A missing impact does not skip. Major pairs are never exempt.
    Fee spike: attached tip (priority fee plus any Jito tip, in SOL) is strictly
    more than tip_multiple (about 2) times the base signature fee (5000 lamports
    times signatures on the quote). A missing priority fee and a missing Jito tip
    do not spike. The 3x median and the micro-lamport ceiling are not used.
    """
    if not isinstance(cfg, dict) or not cfg.get("enabled", True):
        return []
    reasons = []
    exempt = (
        bool(cfg.get("memecoin_exempt", True))
        and bool(cfg.get("memecoin_mode_enabled"))
        and pair_tagged_memecoin(pair_info)
    )
    max_pct = cfg.get("price_impact_max_pct")
    if (not exempt) and impact_pct is not None and max_pct is not None:
        if float(impact_pct) > float(max_pct):
            reasons.append(
                f"price_impact_pct={float(impact_pct):.6g}>max_{float(max_pct)}"
            )
    spike = VQ.fee_spike_reason(
        row if isinstance(row, dict) else {},
        tip_multiple=float(cfg.get("tip_multiple") or 2),
        base_lamports=int(cfg.get("base_signature_lamports") or 5000),
    )
    if spike:
        reasons.append(spike)
    return reasons

def priority_fee_sol(row, micro):
    """Priority fee in SOL from micro-lamports per CU. Not the signature or Jito tip."""
    if micro is None or not isinstance(row, dict):
        return None
    venue = str(row.get("venue") or "")
    if venue.startswith("jupiter"):
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        ixs = list(payload.get("computeBudgetInstructions") or [])
        cu = VQ._cu_limit(ixs) or VQ.JUP_CU_EST
    else:
        cu = RAYDIUM_QUOTE_CU
    lamports = (int(round(float(micro))) * int(cu)) // 1_000_000
    return lamports / 1e9

def curl_json(url, timeout=25, retries=5):
    last = None
    _jup429 = 0
    for i in range(retries):
        r = subprocess.run(
            ["curl", "-sS", "-w", "\n%{http_code}", "--max-time", str(timeout),
             "-H", "Accept: application/json", url],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            last = r.stderr.strip() or f"curl_exit_{r.returncode}"
            time.sleep(2.0 * (i + 1))
            continue
        body, _, code = r.stdout.rpartition("\n")
        code = code.strip()
        if code == "429" or "Too many requests" in body or "Rate limit" in body:
            last = "rate_limit_429"
            if "jup.ag" in url:  # jup429once1015: Jupiter 429 -> one retry per quote, then give up
                _jup429 += 1
                if _jup429 > 1:
                    break
            time.sleep(8.0 * (i + 1))
            continue
        if code != "200":
            last = f"http_{code}:{body[:120]}"
            time.sleep(2.0 * (i + 1))
            continue
        try:
            return json.loads(body)
        except Exception as e:
            last = str(e)
            time.sleep(1.5)
    return {"error": last}

def rpc(method, params, retries=5):
    """Reads only. Fall through helius, alchemy, then public. Sends use solana_rpc.rpc_send."""
    return solana_rpc.rpc(method, params, retries=retries, timeout=30)

HELD_WALLET = {"JitoSOL": 0.0, "GreedyGoats": 0.0, "_read": False}

def held_goats_wallet_qty(addr=None):
    """Greedy Goats wallet qty (Token-2022 mint). One mint-filtered RPC. Log only."""
    addr = addr or ADDR
    mint = memecoin_mode.mint_of(cb)
    try:
        res = rpc("getTokenAccountsByOwner", [addr, {"mint": mint}, {"encoding": "jsonParsed"}])
        qty = 0.0
        for v in (res.get("result") or {}).get("value") or []:
            qty += float(v["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0)
        return qty
    except Exception as _ge:
        print(f"HELD_READ_ERR goats {type(_ge).__name__}", flush=True)
        return None

JITOSOL_PX_FILE = "jitosol-price.json"
JITOSOL_SOL_RATE_DEFAULT = 1.30

def jitosol_usd_price(live_px, sol_px, home=None):
    """jitopx1014: JitoSOL USD price, never the plain SOL price.

    Live Jupiter price wins and is saved (with the JitoSOL/SOL rate). If the
    fetch failed: SOL price x the last saved JitoSOL/SOL rate (default 1.30),
    else the last saved JitoSOL price. Logs HELD_PRICE_WARN when not live.
    """
    import os as _os
    path = _os.path.join(home or _DH.HOME_STR, JITOSOL_PX_FILE)
    try:
        sol = float(sol_px or 0)
    except (TypeError, ValueError):
        sol = 0.0
    try:
        live = float(live_px or 0)
    except (TypeError, ValueError):
        live = 0.0
    if live > 0:
        rec = {"usd": live, "at_pt": datetime.now(PT).replace(microsecond=0).isoformat()}
        if sol > 0:
            rec["sol_rate"] = live / sol
        try:
            with open(path, "w", encoding="utf-8") as _f:
                json.dump(rec, _f)
        except Exception:
            pass
        return live
    last = {}
    try:
        with open(path, encoding="utf-8") as _f:
            last = json.load(_f) or {}
    except Exception:
        last = {}
    rate = float(last.get("sol_rate") or 0)
    if sol > 0:
        if not (1.0 <= rate <= 2.0):
            rate = JITOSOL_SOL_RATE_DEFAULT
        px, how = sol * rate, f"SOL x {rate:.4f}"
    elif float(last.get("usd") or 0) > 0:
        px, how = float(last["usd"]), f"last good {last.get('at_pt')}"
    else:
        px, how = 0.0, "none"
    print(f"HELD_PRICE_WARN JitoSOL price fetch failed; using {how} = ${px:.4f}", flush=True)
    return px

def held_usd_prices(syms_mints):
    """Jupiter lite price/v3 USD for held mints with a balance. {} on failure."""
    ids = [m for _, m in syms_mints if m]
    if not ids:
        return {}
    try:
        import urllib.request
        import jupkey as _JK  # jupkey1012: keyed api.jup.ag price/v3, keyless fallback
        url = "https://api.jup.ag/price/v3?ids=" + ",".join(ids)
        data = _JK.urlopen_json(url, 8)
        out = {}
        for sym, m in syms_mints:
            px = (data.get(m) or {}).get("usdPrice")
            if px:
                out[sym] = float(px)
        return out
    except Exception as _pe:
        print(f"HELD_PRICE_ERR {type(_pe).__name__} {getattr(_pe, 'code', '')}".rstrip(), flush=True)  # e.g. 403 / 429
        return {}

def get_balances(addr=None, commitment=None):
    """Wallet read. commitment None keeps the RPC default (finalized).

    Post-fill reads pass "confirmed": sends confirm at "confirmed", so a
    finalized read right after a fill still returns pre-trade balances.
    """
    addr = addr or ADDR
    _bal_cfg = [{"commitment": commitment}] if commitment else []
    _tok_cfg = {"encoding": "jsonParsed"}
    if commitment:
        _tok_cfg["commitment"] = commitment
    sol_lamports = rpc("getBalance", [addr] + _bal_cfg)["result"]["value"]
    bals = {"SOL": sol_lamports / 1e9}
    tok = rpc("getTokenAccountsByOwner", [
        addr,
        {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
        _tok_cfg,
    ])
    want = {MINTS["USDC"]:"USDC", MINTS["cbBTC"]:"cbBTC", MINTS["WETH"]:"WETH"}
    for v in tok["result"]["value"]:
        info = v["account"]["data"]["parsed"]["info"]
        mint = info["mint"]
        if mint in want:
            bals[want[mint]] = float(info["tokenAmount"]["uiAmount"] or 0)
    for k in ("USDC","cbBTC","WETH"):
        bals.setdefault(k, 0.0)
    try:  # lend1015: lent USDC (jlUSDC x share value) is USDC the bot owns
        import degen_lend as _LEND
        bals["USDC"] = float(bals.get("USDC") or 0) + _LEND.lent_usdc_from_tok(tok)
    except Exception as _le:
        print(f"LEND_READ_ERR {type(_le).__name__}", flush=True)
        raise
    # Held savings (JitoSOL) / memecoin (Greedy Goats) wallet quantities for the
    # log only. Kept out of bals so no trading path sees them.
    try:
        _jito_mint = savings_mode.mint_of(cb)
        HELD_WALLET["JitoSOL"] = 0.0
        for v in tok["result"]["value"]:
            info = v["account"]["data"]["parsed"]["info"]
            if info.get("mint") == _jito_mint:
                HELD_WALLET["JitoSOL"] += float(info["tokenAmount"]["uiAmount"] or 0)
                HELD_WALLET["_read"] = True
    except Exception as _he:
        print(f"HELD_READ_ERR jitosol {type(_he).__name__}", flush=True)
    try:  # stake1015: the separate JitoSOL bucket is SOL (x JitoSOL->SOL), never savings JitoSOL
        import sol_stake as _STK
        _bsol, _bq = _STK.bucket_sol_for_read(_STK.jitosol_in_tok(tok))
        if _bq > 0:
            bals["SOL"] = float(bals.get("SOL") or 0) + _bsol
            if HELD_WALLET.get("_read"):
                HELD_WALLET["JitoSOL"] = max(0.0, float(HELD_WALLET.get("JitoSOL") or 0) - _bq)
    except Exception as _se:
        print(f"STAKE_READ_ERR {type(_se).__name__}", flush=True)
        raise
    # Saved cbBTC (savings_mode.savings_cbbtc_held) is never trading inventory.
    # Every edge path sizes from this trading view; the saved amount rides in
    # bals["cbBTC_savings"] so wallet/latch totals still include it.
    return savings_mode.trading_balances(cb, bals)

POST_FILL_BAL_WAIT_S = 30.0  # 1.1.1: up to ~30s for the bought token to land
POST_FILL_BAL_STEP_S = 2.5

def _estimate_post_fill(pre, info):
    est = dict(pre)
    est[info["sold"]] = float(est.get(info["sold"]) or 0) - float(info["sell_amt"])
    est[info["bought"]] = float(est.get(info["bought"]) or 0) + float(info["buy_amt"])
    if info["sold"] != "SOL" and info["bought"] != "SOL":
        est["SOL"] = max(0.0, float(est.get("SOL") or 0) - float(info["gas_sol"] or 0))
    return est

def _post_fill_moved(pre, new, info):
    """True once the BOUGHT leg shows at least half the fill amount (1.1.1).

    A sold-only drop means the bought token is not credited yet; logging then undercounts the total."""
    sold, bought = info["sold"], info["bought"]
    d_bought = float(new.get(bought) or 0) - float(pre.get(bought) or 0)
    need = 0.5 * float(info["buy_amt"] or 0)
    if need > 0:
        return d_bought >= need
    d_sold = float(pre.get(sold) or 0) - float(new.get(sold) or 0)
    return d_sold >= 0.5 * float(info["sell_amt"] or 0) > 0

def post_fill_balances(pre, info, addr=None):
    """Re-read after a confirmed send until the fill shows up.

    Reads at "confirmed" every POST_FILL_BAL_STEP_S up to POST_FILL_BAL_WAIT_S.
    Returns (bals, estimated). If the read never reflects the fill (or errors),
    falls back to pre minus sold plus bought and estimated=True.
    """
    deadline = time.time() + POST_FILL_BAL_WAIT_S
    tries = 0
    while True:
        tries += 1
        try:
            new = get_balances(addr or ADDR, commitment="confirmed")
            if _post_fill_moved(pre, new, info):
                if tries > 1:
                    print(f"POST_FILL_BAL_OK tries={tries}", flush=True)
                return new, False
            print(f"POST_FILL_BAL_STALE try={tries}", flush=True)
        except Exception as bal_err:
            print(f"POST_FILL_BAL_ERR try={tries} {type(bal_err).__name__}", flush=True)
        if time.time() + POST_FILL_BAL_STEP_S > deadline:
            break
        time.sleep(POST_FILL_BAL_STEP_S)
    print("POST_FILL_BAL_ESTIMATED", flush=True)
    return _estimate_post_fill(pre, info), True

def wallet_total_usd(bals_in, prices_in):
    """Displayed wallet total: trading book USD plus saved cbBTC USD plus held JitoSOL
    (jitototal1014, JitoSOL price, wallet qty when read this check, else the savings ledger)."""
    return (savings_mode.book_usd_total(bals_in, prices_in) + savings_mode.savings_reserved_usd(bals_in, prices_in)
            + _held_jitosol_usd(bals_in, prices_in))

def _held_jitosol_usd(bals_in, prices_in):
    try:
        b = dict(bals_in or {})
        if HELD_WALLET.get("_read") and not b.get("JitoSOL"):
            b["JitoSOL"] = float(HELD_WALLET.get("JitoSOL") or 0)
        return float(savings_mode.jitosol_mark_usd(cb, b, prices_in) or 0)
    except Exception:
        return 0.0

_BALANCE_SNAPSHOT = None

def load_balance_snapshot():
    """One wallet snapshot per check, reused for sizing and the fill.

    A second read happens only when the first read never succeeded.
    Do not call this again before submit. Post-fill balances are a
    different read, after the swap has already been confirmed.
    """
    global _BALANCE_SNAPSHOT
    if _BALANCE_SNAPSHOT is not None:
        return _BALANCE_SNAPSHOT
    _BALANCE_SNAPSHOT = get_balances(ADDR)
    return _BALANCE_SNAPSHOT

VENUE_RANK = {"jupiter_manual": 3, "orca": 2, "raydium": 1, "jupiter_ultra": 0}

_QUOTE_BOOK = {}
# Quote-reuse fast path (cost-basis.json quote_reuse_fast_path). Missing = off.
SENDS_ATTEMPTED = [0]  # edge-fill send attempts this check (fast path is first-send only)

def remember_route_quotes(key, sell_amt, rows, at=None):
    """Keep this route's venue rows in memory. Not written to disk.

    at = monotonic time the rows came back (parallel prefetch); default now.
    """
    _QUOTE_BOOK[key] = {
        "at": time.monotonic() if at is None else float(at),
        "sell_amt": float(sell_amt),
        "rows": rows,
    }

def fresh_rows_for_route(key, sell_amt):
    """Rows if this exact size was quoted recently. None if missing or stale."""
    rec = _QUOTE_BOOK.get(key)
    if not rec:
        return None
    if abs(float(rec["sell_amt"]) - float(sell_amt)) > 1e-9:
        return None
    age = time.monotonic() - float(rec["at"])
    if age > QUOTE_STALE_SEC:
        return None
    return rec["rows"], age

def fetch_route_rows(sell, buy, amount_base_units, include_orca=False):
    """Network half of fetch_route_quotes. No prints; safe in a worker thread."""
    rows = VQ.fetch_venue_quotes(sell, buy, amount_base_units, include_orca=include_orca)
    return [VQ.adjust_for_ultra_fee(r) for r in rows]

def route_rows_notes(sell, buy, rows):
    """Print/notes half of fetch_route_quotes (main thread, ranked order)."""
    notes = []
    for row in rows:
        if not row.get("ok"):
            notes.append(f"{row.get('venue')}: {row.get('note')}")
            if row.get("venue") == "orca":
                print(f"ORCA_QUOTE_SKIP {sell}->{buy} {row.get('note')}", flush=True)
    return rows, notes

def fetch_route_quotes(sell, buy, amount_base_units, include_orca=False):
    """Jupiter manual + Raydium; Orca Whirlpool when include_orca (top-1 only).

    A down venue is a note, not a hard fail. Orca failures are logged by the
    caller and ignored (other venues still compete).
    """
    rows = fetch_route_rows(sell, buy, amount_base_units, include_orca=include_orca)
    return route_rows_notes(sell, buy, rows)

def peer_summary(rows, scored_by_venue):
    out = []
    for row in rows:
        venue = row.get("venue")
        sc = scored_by_venue.get(venue)
        item = {
            "venue": venue,
            "ok": bool(row.get("ok")),
            "note": None if row.get("ok") else row.get("note"),
            "out_amount": row.get("out_amount"),
            "gas_sol": row.get("gas_sol"),
            "jito_tip_sol": row.get("jito_tip_sol"),
            "platform_fee_bps": row.get("platform_fee_bps"),
            "labeled_ultra": bool(row.get("labeled_ultra")),
            "price_impact_pct": None if VQ.quote_price_impact_pct(row) is None else round(VQ.quote_price_impact_pct(row), 6),
            "priority_fee_micro_lamports_per_cu": None if VQ.quote_priority_fee_micro(row) is None else round(VQ.quote_priority_fee_micro(row), 3),
        }
        if sc:
            item["buy_amt"] = sc["buy_amt"]
            item["gap_net_pct"] = round(sc["gap_net"], 3)
            item["exec_net"] = sc["exec_net"]
        out.append(item)
    return out


def gas_sol_from_quote(q):
    """Network signature+priority, plus a Jito tip when the quote carries one."""
    if isinstance(q, dict) and q.get("_gas_sol") is not None:
        return float(q["_gas_sol"])
    if not isinstance(q, dict):
        return GAS_SOL_EST
    sig = int(q.get("signatureFeeLamports") or 0)
    pri = int(q.get("prioritizationFeeLamports") or 0)
    # rent is ATA creation; only charge if we pay it and it's not already held — count it as real cost when present & we are payer
    rent = int(q.get("rentFeeLamports") or 0)
    payer = q.get("signatureFeePayer") or q.get("prioritizationFeePayer")
    total = sig + pri
    # Include rent only if non-zero and looks like we pay (conservative for gas_net)
    if rent and (q.get("rentFeePayer") == ADDR or rent > 0):
        # ATA rent is reclaimable; for hard gas_net gate use sig+priority only (ongoing burn)
        pass
    if total <= 0:
        net = GAS_SOL_EST
    else:
        net = total / 1e9
    tip = 0.0
    if isinstance(q, dict):
        tip = float(q.get("_jito_tip_sol") or 0)
        if tip <= 0 and isinstance(q.get("payload"), dict):
            tip = VQ.jito_tip_lamports(q["payload"].get("tipInstruction")) / 1e9
    return net + tip

def to_base(amt, sym):
    return int(round(float(amt) * (10 ** DEC[sym])))

def from_base(amt, sym):
    return int(amt) / (10 ** DEC[sym])

def load_json(path):
    if not os.path.exists(path) and not path.endswith(("/cost-basis.json", "/allowed-pairs.json")):
        return {}  # template: first run has no state files yet
    with open(path) as f:
        return json.load(f)

def _cb_write(cb_obj, force=False):
    """Write cost-basis.json unless a scheduled job started mid-check (then abort, no write).

    force=True: a live fill must always persist (the job waits for edge-check.running to clear).
    """
    if not force and not FILLED_THIS_CHECK[0] and _JL.held():
        _job_skip_exit("started mid-check; cost-basis not written")
    fade_trend.atomic_write_json(_DH.HOME_STR + "/cost-basis.json", cb_obj)


cb = load_json(_DH.HOME_STR + "/cost-basis.json")
QR_CFG = EP.quote_reuse_cfg(cb)
pairs = cb["pairs"]
mults = dict(cb["pair_edge_escalation"]["multipliers"])
usd_pairs = set(cb["buy_sell_pct"]["usd_pairs"])
hb = cb["holdings_bias"]
rl = cb["reverse_last_trade"]["last_fill_by_pair"]
size_lim = cb["size_limits"]
seh = cb.get("skip_edge_halving") or {}
SEH_ENABLED = bool(seh.get("enabled", True))
# After 2 consecutive quiet SKIPs, halve current_multiplier once for that streak.
# Further quiet checks do not halve. No multiplier floor. No once-per-hour timer.
# last_halve_pt / halve_interval / min_multiplier are ignored. Do not re-enable a 0.5 floor.
# current_multiplier is used as stored.
SEH_MULT = float(seh.get("current_multiplier", 1.0))
SEH_SKIPS = int(seh.get("skips_since_fill", 0))
SEH_QUIET = int(seh.get("consecutive_quiet_checks", 0))
SEH_STREAK_HALVED = bool(seh.get("streak_halved", False))
clamp_cfg = cb.get("effective_pct_clamp") or {}
CLAMP_ENABLED = bool(clamp_cfg.get("enabled", True))
CLAMP_FLOOR = float(clamp_cfg.get("all_in_floor_pct", 0.20))
CLAMP_CAP = float(clamp_cfg.get("cap_pct", 5.0))
print(
    f"skip_edge_halving enabled={SEH_ENABLED} mult={SEH_MULT} skips={SEH_SKIPS} "
    f"quiet={SEH_QUIET} streak_halved={SEH_STREAK_HALVED}",
    flush=True,
)
print(f"effective_pct_clamp enabled={CLAMP_ENABLED} floor={CLAMP_FLOOR} cap={CLAMP_CAP}", flush=True)
# any_profit_after_100_quiet_checks (owner 2026-10-06): after N quiet checks,
# edge_net > 0 may fill (bypass required_pct / leftover). Seeded from skips_since_fill.
ANY_PROFIT_CFG = EP.any_profit_after_quiet_cfg(cb)
MIN_FLOOR = EP.min_floor_cfg(cb)  # min1pct 1% required floor; no size min; unlock 0.25% + $2 (buyside1012)
KH_CFG = EP.knife_hurdle_cfg(cb)  # knife_hurdle_v1: 24h falling-knife on edge fills + $0.10/$0.01 hurdle; off = old behavior
KH_STATS = {"knife": 0, "knife_price_missing": 0, "hurdle": 0}


def kh_hurdle(sc, is_reverse):
    if EP.apply_profit_hurdle(sc, KH_CFG, is_reverse):
        KH_STATS["hurdle"] += 1
    return sc
# Keep dedicated counter seeded; do not drop existing quiet streak.
if (cb.get("any_profit_after_quiet_checks") or {}).get("quiet_checks_since_fill") is None:
    cb.setdefault("any_profit_after_quiet_checks", {})["quiet_checks_since_fill"] = SEH_SKIPS
ANY_PROFIT_QUIET = EP.quiet_checks_since_fill(cb)
ONESHOT_ANY_PROFIT_ROUTE = (os.environ.get("DEGEN_ONESHOT_ANY_PROFIT_ROUTE") or "").strip() or None
BYPASS_FLAT_ENV = (os.environ.get("DEGEN_BYPASS_FLAT_MARKET") or "").strip() in ("1", "true", "True", "yes")
print(
    f"any_profit_after_quiet name={ANY_PROFIT_CFG.get('name')} enabled={ANY_PROFIT_CFG.get('enabled')} "
    f"quiet={ANY_PROFIT_QUIET} thr={ANY_PROFIT_CFG.get('quiet_checks_threshold')} "
    f"armed={EP.any_profit_armed(ANY_PROFIT_CFG, ANY_PROFIT_QUIET)} "
    f"oneshot={ONESHOT_ANY_PROFIT_ROUTE!r} bypass_flat={BYPASS_FLAT_ENV}",
    flush=True,
)
BAD_CFG = bad_condition_config(cb)
fee_history_dirty = False
bad_blocks = []
print(
    f"bad_condition_skip enabled={BAD_CFG['enabled']} price_impact_max_pct={BAD_CFG['price_impact_max_pct']} "
    f"memecoin_mode={BAD_CFG.get('memecoin_mode_enabled')} memecoin_exempt={BAD_CFG['memecoin_exempt']} "
    f"fee_spike={BAD_CFG['tip_multiple']}x_signature_fee lamports={BAD_CFG['base_signature_lamports']}",
    flush=True,
)

def remember_priority_fee(micro):
    """Retired. The spike test is tip versus the signature fee, not a median history."""
    return None

def held_outside_book(sym):
    """JitoSOL and Greedy Goats are held. They are not edge-traded."""
    return savings_mode.is_savings_asset(cb, sym) or memecoin_mode.is_held_asset(cb, sym)

def book_usd_map(bals_in, prices_in):
    """USD rank inputs: SOL, USDC, cbBTC, WETH only."""
    return savings_mode.book_usd_vals(bals_in, prices_in)

def winning_quote_state(pair, row, economically_clear):
    """Read impact and the attached tip off the winning quote."""
    impact = VQ.quote_price_impact_pct(row)
    micro = VQ.quote_priority_fee_micro(row)
    reasons = bad_condition_reasons(BAD_CFG, pairs.get(pair) or {}, impact, row)
    tip = VQ.attached_tip_sol(row)
    normal = VQ.normal_signature_fee_sol(row, BAD_CFG.get("base_signature_lamports") or 5000)
    blocked = bool(economically_clear and reasons)
    if blocked:
        bad_blocks.append({
            "pair": pair,
            "venue": None if not isinstance(row, dict) else row.get("venue"),
            "reasons": reasons,
        })
    return {
        "price_impact_pct": None if impact is None else round(float(impact), 6),
        "priority_fee_micro_lamports_per_cu": None if micro is None else round(float(micro), 3),
        "priority_fee_sol": None if micro is None else priority_fee_sol(row, micro),
        "attached_tip_sol": None if tip is None else tip,
        "signature_fee_sol": normal,
        "bad_condition_skip": blocked,
        "bad_condition_reasons": reasons,
        "memecoin": pair_tagged_memecoin(pairs.get(pair) or {}),
    }


assert ADDR == ADDR_LOCAL
# bugfix1012: fail closed on a bad balance read (coin >$0.50 last time now 0/missing).
import balance_guard as _BG
def _bg_read():
    global _BALANCE_SNAPSHOT
    _BALANCE_SNAPSHOT = None
    return load_balance_snapshot()
try:
    _bg_prev_px = dict((load_json(_DH.HOME_STR + "/last-check.json") or {}).get("prices_usd") or {})
except Exception:
    _bg_prev_px = {}
try:
    bals = _BG.guarded_read(_bg_read, (cb.get("deposit_watch") or {}).get("balances") or {}, _bg_prev_px,
                            _DH.HOME_STR + "/balance-guard.json", log=lambda m: print(m, flush=True))
except _BG.BadBalanceRead as _bge:
    print(f"ERROR bad_balance_read {_bge.coins}: was >$0.50 last check, read 0 twice; no trades this check", flush=True)
    print("RESULT ERROR", flush=True)
    sys.exit(2)
print("BALANCE_SNAPSHOT once", "interpreter", sys.executable, flush=True)
bals_local = bals
bals_old = {"queried": False, "reason": "stranded_no_key_do_not_touch", "address": ADDR_OLD}
local_tradable = any(float(bals.get(k) or 0) > 0 for k in ("SOL", "USDC", "cbBTC", "WETH"))
MIGRATION_BLOCKER = not local_tradable
print("BALANCES_LOCAL", bals_local, flush=True)
print("BALANCES_OLD", "not_queried", flush=True)
print("ACTIVE_READ_ADDR", ADDR, "MIGRATION_BLOCKER", MIGRATION_BLOCKER, flush=True)
print("BALANCES", bals, flush=True)

# Seed prices from last-check then refine from first successful USD quotes
prev = load_json(_DH.HOME_STR + "/last-check.json")
prices = dict(prev.get("prices_usd") or {"SOL":119.49,"USDC":1.0,"cbBTC":83631.4,"WETH":2679.52})
prices["USDC"] = 1.0

def max_trade_usd_cap(pct=None):
    """15% of the combined portfolio (book plus held JitoSOL and Greedy Goats).

    pct overrides for an up pair's raised cap (18.75%) on that trade only.
    """
    pct = float(size_lim.get("max_trade_portfolio_pct", 15)) if pct is None else float(pct)
    total = float(savings_mode.combined_portfolio_usd(cb, bals, prices) or 0)
    if total <= 0:
        total = float(savings_mode.book_usd_total(bals, prices) or 0)
    return max(0.0, total * pct / 100.0)

def inventory_skip_reason(sold, sell_amt, dust_usd, caps=None):
    """Why this sell must not be quoted. None means it has size to sell.

    Uses the check's balance snapshot. Respects the 15% portfolio cap, the 40% cap,
    and the 0.02 SOL buffer. Zero or dust inventory is not quoted.
    Does not call the network.
    """
    bal = float(bals.get(sold) or 0)
    px = float(prices.get(sold) or 0)
    amt = float(sell_amt or 0)
    if bal <= 0 or amt <= 0 or amt > bal + 1e-12:
        return "zero_inventory"
    if sold == "SOL" and bal - amt < float(size_lim["min_sol_fee_buffer"]) - 1e-12:
        return "sol_buffer"
    _pct, _frac = (None, float(size_lim["max_sell_fraction_of_balance"])) if caps is None else (caps[0], caps[1])
    if amt > bal * _frac + 1e-9:
        return "over_max_fraction"
    if px > 0 and amt * px - 1e-9 > max_trade_usd_cap(_pct):
        return "over_max_usd"
    if px <= 0 or amt * px < float(dust_usd):
        return "dust"
    return None

def capped_probe_amt(sold, requested):
    """Price-probe size clipped to the same caps as a trade. Not a new rule."""
    bal = float(bals.get(sold) or 0)
    px = float(prices.get(sold) or 0)
    amt = min(float(requested), bal * float(size_lim["max_sell_fraction_of_balance"]))
    if px > 0:
        amt = min(amt, max_trade_usd_cap() / px)
    if sold == "SOL":
        amt = min(amt, max(0.0, bal - float(size_lim["min_sol_fee_buffer"])))
    return max(0.0, amt)

def refresh_price_from_quote(sold, bought, sell_ui, price_key_num, price_key_den):
    """Update prices[num] from the best available venue quote."""
    global prices
    probe = capped_probe_amt(sold, sell_ui)
    reason = inventory_skip_reason(sold, probe, 0.05)
    if reason:
        print(f"price quote skipped {reason} sold={sold} probe={probe}", flush=True)
        return False
    rows, notes = fetch_route_quotes(sold, bought, to_base(probe, sold))
    time.sleep(QUOTE_GAP)
    oks = [r for r in rows if r.get("ok") and r.get("out_amount")]
    if not oks:
        print("price quote skipped", "; ".join(notes), flush=True)
        return False
    # Highest out is the favorable executable for this exact-in size.
    best = max(oks, key=lambda r: (int(r["out_amount"]), VENUE_RANK.get(r["venue"], 0)))
    if notes:
        print("price quote venue notes", "; ".join(notes), flush=True)
    out = from_base(best["out_amount"], bought)
    if bought == "USDC":
        prices[sold] = out / probe
    elif sold == "USDC":
        prices[bought] = probe / out
    return True

# Cheap Jupiter lite price/v3 first (same fetch fade-trend uses). Flat-market
# skip compares SOL/cbBTC/WETH to the last NON-skipped check before any venue
# quote (including the SOL probe). Missing reference -> run normally.
FLAT_MARKET_CFG = EP.flat_market_cfg(cb)
FLAT_REF_PATH = _DH.HOME_STR + "/flat-market-ref.json"
FLAT_MARKET = False
FLAT_MARKET_INFO = {"reason": "not_checked"}
_sol_probe_ok = False
try:
    fade_trend.begin_check()
    _psrc = getattr(fade_trend.current(), "price_source", None) or "unknown"
    for _sym in ("SOL", "cbBTC", "WETH", "USDC"):
        _usd = (fade_trend.current().quote_by_symbol.get(_sym) or {}).get("usd")
        if _usd and float(_usd) > 0:
            prices[_sym] = float(_usd)
    print(f"PRICE_SOURCE {_psrc}", flush=True)
except Exception as _mark_err:
    print(f"MARKS_ERR {type(_mark_err).__name__}", flush=True)
print("prices", prices, flush=True)
try:
    _flat_ref = load_json(FLAT_REF_PATH)
    _flat_prev = (_flat_ref.get("prices_usd") if isinstance(_flat_ref, dict) else None) or {}
except Exception:
    _flat_prev = {}
if FLAT_MARKET_CFG.get("enabled", True):
    FLAT_MARKET, FLAT_MARKET_INFO = EP.flat_market_skip(
        prices, _flat_prev,
        threshold_pct=FLAT_MARKET_CFG.get("threshold_pct", 0.1),
        symbols=FLAT_MARKET_CFG.get("symbols"),
    )
print(
    f"FLAT_MARKET skip={FLAT_MARKET} reason={FLAT_MARKET_INFO.get('reason')} "
    f"thr={FLAT_MARKET_CFG.get('threshold_pct')}% moves={FLAT_MARKET_INFO.get('moves_pct')}",
    flush=True,
)
if FLAT_MARKET and (BYPASS_FLAT_ENV or ONESHOT_ANY_PROFIT_ROUTE):
    print(
        f"FLAT_MARKET_BYPASS env={BYPASS_FLAT_ENV} oneshot={ONESHOT_ANY_PROFIT_ROUTE!r} "
        f"(was reason={FLAT_MARKET_INFO.get('reason')})",
        flush=True,
    )
    FLAT_MARKET = False
    FLAT_MARKET_INFO = dict(FLAT_MARKET_INFO or {})
    FLAT_MARKET_INFO["bypassed"] = True
    FLAT_MARKET_INFO["bypass_reason"] = "oneshot_or_env"
if not FLAT_MARKET:
    # Reuse the start-of-check Jupiter lite price v3 read (the same cached values
    # fade-trend and flat-market use). The SOL->USDC venue probe (+7s gap) runs
    # only when that read has no SOL price.
    _lite_sol = None
    try:
        _lite_sol = (fade_trend.current().quote_by_symbol.get("SOL") or {}).get("usd")
    except Exception:
        _lite_sol = None
    if _lite_sol and float(_lite_sol) > 0:
        prices["SOL"] = float(_lite_sol)
        _ps = getattr(fade_trend.current(), "price_source", None) or "jupiter_lite_price_v3"
        print(f"price SOL reuse {_ps} (no venue probe)", flush=True)
    else:
        print("price SOL...", flush=True)
        _sol_probe_ok = refresh_price_from_quote("SOL", "USDC", 0.01, "SOL", "USDC")
    try:
        for _sym in ("cbBTC", "WETH", "SOL"):
            if _sym == "SOL" and _sol_probe_ok:
                continue
            _usd = (fade_trend.current().quote_by_symbol.get(_sym) or {}).get("usd")
            if _usd and float(_usd) > 0:
                prices[_sym] = float(_usd)
    except Exception as _mark_err2:
        print(f"MARKS_ERR {type(_mark_err2).__name__}", flush=True)
    print("prices", prices, flush=True)

# jitototal1014: one JitoSOL USD price per check for every wallet total
# (savings/memecoin lines, caps, P&L, deposit watch, check-in, dashboard).
# Never the plain SOL price; kept out of `prices` so no trading path sees it.
try:
    _jx = held_usd_prices([("JitoSOL", savings_mode.mint_of(cb))]).get("JitoSOL")
    savings_mode.JITOSOL_USD = jitosol_usd_price(_jx, prices.get("SOL")) or None
except Exception as _jxe:
    print(f"HELD_PRICE_ERR jitosol_total {type(_jxe).__name__} {getattr(_jxe, 'code', '')}".rstrip(), flush=True)

usd_vals = book_usd_map(bals, prices)
total_usd = wallet_total_usd(bals, prices)
rank = sorted(usd_vals.items(), key=lambda x: x[1])
least, most = rank[0][0], rank[-1][0]
print("USD", {k: round(v,4) for k,v in usd_vals.items()}, "total", round(total_usd,4), "least", least, "most", most, flush=True)
print(
    "SAVINGS_RESERVED cbBTC", float(bals.get("cbBTC_savings") or 0),
    "usd", round(savings_mode.savings_reserved_usd(bals, prices), 4),
    "trading_cbBTC", float(bals.get("cbBTC") or 0),
    flush=True,
)

funded_usd = float(savings_mode.funded_usd(cb) or 0)
cost_basis_usd = funded_usd  # alias: wallet P&L vs net deposits (not tokens_usd)
unrealized = (total_usd / funded_usd - 1) * 100 if funded_usd else 0
# Wallet funded P&L% (display only; no longer sizes trades).
FUNDED_PNL_PCT = savings_mode.pnl_vs_funded_pct(total_usd, funded_usd)
# Per-pair P&L size tilt (2026-10-05, owner). Pair P&L% is that pair's own
# realized FIFO round-trip USD net of network fees / USD traded (pair_pnl.py).
# pair-pnl-state.json is synced with every trades.jsonl row not yet applied
# (morning underweight etc.) and updated on every fill this process makes.
PAIR_TILT_CFG = EP.pair_tilt_cfg(cb)
PAIR_PNL_PATH = _DH.HOME_STR + "/" + str((cb.get("pair_pnl_size_tilt") or {}).get("state_file") or PP.STATE_FILE)
PAIR_PNL_STATE = None
PAIR_TILT = {}

def _pair_pnl_trades():
    out = []
    try:
        with open(_DH.HOME_STR + "/trades.jsonl", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    out.append(json.loads(line))
    except (OSError, ValueError):
        pass
    return out

def refresh_pair_tilts():
    PAIR_TILT.clear()
    for _pk in pairs:
        _e = ((PAIR_PNL_STATE or {}).get("pairs") or {}).get(_pk) or {}
        _pct = PP.pnl_pct(_e) if _e else None
        _m, _lbl = EP.pair_tilt_multiplier(_pct, int(_e.get("fills") or 0), PAIR_TILT_CFG)
        PAIR_TILT[_pk] = {"mult": _m, "label": _lbl, "pnl_pct": _pct, "fills": int(_e.get("fills") or 0),
                          "realized_net_usd": _e.get("realized_net_usd"), "traded_usd": _e.get("traded_usd"),
                          "caps": EP.pair_size_caps(_lbl, size_lim, PAIR_TILT_CFG)}

try:
    _pp_trades = _pair_pnl_trades()
    PAIR_PNL_STATE = PP.load(PAIR_PNL_PATH)
    _pp_known = set((PAIR_PNL_STATE or {}).get("processed_txs") or [])
    _pp_new = [t for t in _pp_trades if t.get("tx") not in _pp_known]
    if PAIR_PNL_STATE is None or _pp_new:
        _pp_book = PP.price_book_from(_pp_trades, _DH.HOME_STR + "/sol-usdc-log.md", prices)
        if PAIR_PNL_STATE is None:
            PAIR_PNL_STATE = PP.rebuild(_pp_trades, pairs, _pp_book.usd)
            PAIR_PNL_STATE["source"] = "rebuild trades.jsonl + sol-usdc-log.md price samples"
        else:
            PP.sync(PAIR_PNL_STATE, _pp_new, pairs, _pp_book.usd)
        PP.ensure_pairs(PAIR_PNL_STATE, pairs)
        PAIR_PNL_STATE["updated_pt"] = datetime.now(PT).replace(microsecond=0).isoformat()
        PP.save(PAIR_PNL_PATH, PAIR_PNL_STATE)
        print(f"PAIR_PNL_SYNC applied={len(_pp_new)}", flush=True)
except Exception as _pp_err:
    print(f"PAIR_PNL_ERR {type(_pp_err).__name__}: {_pp_err}", flush=True)
    PAIR_PNL_STATE = PAIR_PNL_STATE or {"pairs": {}}
refresh_pair_tilts()
print(
    "PAIR_PNL_SIZE_TILT " + json.dumps({
        k: {"pnl_pct": None if v["pnl_pct"] is None else round(v["pnl_pct"], 3), "x": v["mult"], "label": v["label"],
            "caps": [v["caps"][0], v["caps"][1]]}
        for k, v in PAIR_TILT.items()
    }) + f" flat_band={PAIR_TILT_CFG['flat_band_pct']}% wallet_funded_pnl(display)="
    + f"{None if FUNDED_PNL_PCT is None else round(FUNDED_PNL_PCT, 3)}%",
    flush=True,
)

def pair_tilt(pair):
    return PAIR_TILT.get(pair) or {"mult": 1.0, "label": "no_fills", "pnl_pct": None, "fills": 0,
                                   "caps": EP.pair_size_caps("no_fills", size_lim, PAIR_TILT_CFG)}

def exec_caps(c, pair):
    """Caps that gate this candidate. Reverses keep normal caps (mirrored size)."""
    if c.get("kind") == "reverse" or c.get("opp_sold"):
        return EP.pair_size_caps("normal", size_lim, PAIR_TILT_CFG)
    return pair_tilt(pair)["caps"]
_qc = {}
try:
    _qc = (load_json(_DH.HOME_STR + "/allowed-pairs.json").get("quote_cap") or {})
except Exception:
    _qc = {}
QUOTE_TOP_N = int(_qc.get("top_n", EP.QUOTE_TOP_N) or EP.QUOTE_TOP_N)
GAS_SOL_EST = 0.000085
GAS_SOL = GAS_SOL_EST  # per-quote gas_sol replaces this (network fee + any Jito tip)
REVERSE_MAX_AGE_H = EP.reverse_max_age_hours(cb.get("reverse_last_trade") or cb)
ROUTE_HIT_CFG = EP.route_hit_cfg(cb)
ROUTE_HIT_PATH = _DH.HOME_STR + "/" + str(ROUTE_HIT_CFG.get("state_file") or "route-hit-state.json")
ROUTE_HIT = EP.RouteHitState.load(ROUTE_HIT_PATH, ROUTE_HIT_CFG)
COOLED_ROUTES = set()
print(f"REVERSE_MAX_AGE_H={REVERSE_MAX_AGE_H} ROUTE_HIT file={ROUTE_HIT_PATH}", flush=True)

def pair_base_pcts(pair, bought, sold):
    is_usd = pair in usd_pairs
    buy_b, sell_b = (1.0, 1.0) if is_usd else (0.5, 0.5)
    if bought == "WETH":
        buy_b = 1.0
    if bought == least:
        buy_b = hb["least_buy_pct"]
    elif bought == most:
        buy_b = hb["most_buy_pct"]
    if sold == least:
        sell_b = hb["least_sell_pct"]
    elif sold == most:
        sell_b = hb["most_sell_pct"]
    return buy_b, sell_b

def apply_skip_halving(raw_pct):
    """Multiply by skip-halving factor only (no floor here)."""
    if not SEH_ENABLED:
        return float(raw_pct)
    return float(raw_pct) * SEH_MULT

def clamp_eff(pct):
    """eff% = min(cap, max(all_in_floor, composed_base))"""
    if not CLAMP_ENABLED:
        return float(pct)
    return min(CLAMP_CAP, max(CLAMP_FLOOR, float(pct)))

# Reverse of a band rebalance (including legacy friday_rebalance / friday_band_rebalance)
# or a morning underweight double is 2x composed, leftover floor still on top.
# rsi_rebalance is not in that set: flat 5%, no 2x, no leftover floor.
# A reverse of an edge-check fill (kind edge, or untagged) is not doubled.
_rev_cfg = cb.get("reverse_last_trade") or {}
REBALANCE_REVERSE_KINDS = tuple(_rev_cfg.get("rebalance_kinds") or ("band_rebalance", "underweight_double"))
REBALANCE_REVERSE_MULT = float(_rev_cfg.get("rebalance_reverse_mult", 2.0))

def last_fill_rebalance_kind(info):
    """band_rebalance or underweight_double when this last fill uses the 2x reverse, else None.

    Explicit last_fill_by_pair.kind only. Do not infer from pairs.*.source: that
    source can stay fill_band_rebalance after a later edge fill on the same pair.
    friday_rebalance and friday_band_rebalance are the old Friday band and map to
    band_rebalance. rsi_rebalance does not. It returns None here so it is not doubled.
    The flat 5% path is reverse_of_rsi in score_leg.
    """
    if not isinstance(info, dict):
        return None
    kind = friday_rsi.canonical_fill_kind(info.get("kind"))
    if kind == "rsi_rebalance":
        return None
    if kind == "band_rebalance":
        return "band_rebalance"
    if kind == "underweight_double":
        return "underweight_double"
    if kind in REBALANCE_REVERSE_KINDS and kind != "rsi_rebalance":
        return kind
    return None

def eff_pcts(pair, bought, sold, spot_rate_for_loss=None, reverse_of_rebalance=False):
    """Required % after bases, own-pair multiplier, and per-token USD loss tilt.

    buy_raw = buy_base * pair_mult * (2 if bought token USD P&L% < 0 else 1)
    sell_raw = sell_base * pair_mult * (0.5 if sold token USD P&L% < 0 else 1)
    Then skip-halving. A reverse of a rebalance fill (not a normal edge reverse)
    doubles that composed % before the clamp:
    eff% = min(5.0, max(0.20, composed)) or min(5.0, max(0.20, 2 * composed)).
    No pair-rate 2^n. spot_rate_for_loss is ignored (kept so old call sites do not break).
    """
    buy_b, sell_b = pair_base_pcts(pair, bought, sold)
    m = mults.get(pair, 1)
    buy_pnl = sold_token_usd_unrealized_pct(bought)
    sell_pnl = sold_token_usd_unrealized_pct(sold)
    buy_tilt = 2.0 if buy_pnl is not None and buy_pnl < 0 else 1.0
    sell_tilt = 0.5 if sell_pnl is not None and sell_pnl < 0 else 1.0
    buy_raw = buy_b * m * buy_tilt
    sell_raw = sell_b * m * sell_tilt
    buy_composed = apply_skip_halving(buy_raw)
    sell_composed = apply_skip_halving(sell_raw)
    reb_mult = REBALANCE_REVERSE_MULT if reverse_of_rebalance else 1.0
    if reverse_of_rebalance:
        buy_composed *= reb_mult
        sell_composed *= reb_mult
    buy_eff = clamp_eff(buy_composed)
    sell_eff = clamp_eff(sell_composed)
    return buy_eff, sell_eff, buy_tilt, sell_tilt, m, buy_b, sell_b, reb_mult

def required_pct(buy_eff, sell_eff):
    """buyside 2026-10-12: the route's required % is the bought coin's BUY leg only
    (bias, loss tilt, multiplier, halving, clamp already applied). Replaces lowerside.
    sell_eff is still computed and logged but no longer used."""
    return float(buy_eff), "buy"

def tilted_sell_amt(pair, sold, base_amt):
    """Per-pair tilt on the normal-cap size, clamped to that pair's caps.

    Up pair: x1.25, clamp to 18.75% combined portfolio / 50% balance / 0.02 SOL.
    Down: x0.75 (normal caps). Flat / no fills: x1.0. Before the profit cut.
    """
    pt = pair_tilt(pair)
    total = float(savings_mode.combined_portfolio_usd(cb, bals, prices) or 0)
    if total <= 0:
        total = float(savings_mode.book_usd_total(bals, prices) or 0)
    amt, clamped, _caps = EP.apply_pair_size_tilt(
        base_amt, pt["mult"], pt["label"], sold, bals.get(sold), prices.get(sold), total, size_lim, PAIR_TILT_CFG)
    return amt, clamped

def size_tilt_fields(pair, clamped=None):
    pt = pair_tilt(pair)
    return {
        "size_tilt": pt["mult"], "size_tilt_label": pt["label"], "size_tilt_clamped": clamped,
        "pair_pnl_pct": None if pt["pnl_pct"] is None else round(pt["pnl_pct"], 4),
        "size_caps": {"portfolio_pct": pt["caps"][0], "balance_frac": pt["caps"][1], "sol_buffer": pt["caps"][2]},
    }

# audit 2026-10-14: catch swaps that landed after being reported failed/uncertain (logged before deposit detection).
try:
    import late_fills as _LF
    import solana_rpc as _SRPC
    for _lr in _LF.scan(ADDR, lambda m, p: _SRPC.rpc(m, p, retries=1, timeout=15)):
        _ll = f"- LATE_FILL {_lr['time_pt']} {_lr['side']} {_lr['sold_amount']:.9g} -> {_lr['bought_amount']:.9g} tx {_lr['tx']}"
        print(_ll, flush=True)
        with open(_DH.HOME_STR + "/sol-usdc-log.md", "a", encoding="utf-8", newline="") as _tf:
            _tf.write(_ll + "\n")
        LATE_FILLS = globals().setdefault("LATE_FILLS", [])
        LATE_FILLS.append(_lr)
except Exception as _e:
    print(f"LATE_FILL_ERR {type(_e).__name__}", flush=True)

# depfix 2026-10-14: deposit/withdrawal detection once, before tokens_usd reconcile and any buy test.
try:
    _aux = {"JitoSOL": HELD_WALLET.get("JitoSOL"), "GreedyGoats": HELD_WALLET.get("GreedyGoats")} if HELD_WALLET.get("_read") else None
    _bals_dep = dict(bals)
    try:  # rent1014: token-account rent this check, so opening/closing accounts is never a flow
        import token_rent as _TR
        _bals_dep["_rent_sol"] = _TR.rent_sol(_TR.accounts(rpc, ADDR))
        if _aux is not None:
            _aux = dict(_aux, _rent_sol=_bals_dep["_rent_sol"])
    except Exception as _tre:
        print(f"RENT_READ_ERR {type(_tre).__name__}", flush=True)
    DEPOSIT_STATUS = savings_mode.apply_external_deposits(cb, _bals_dep, prices, aux=_aux)
    if DEPOSIT_STATUS.get("reason") in ("deposit", "outflow", "deposit_and_outflow", "external_pending_confirm"):
        _dl = (f"- DEPOSIT_WATCH {datetime.now(PT).replace(microsecond=0).isoformat()} {DEPOSIT_STATUS.get('reason')} "
               f"dep=${DEPOSIT_STATUS.get('deposit_usd', 0):.2f} wd=${DEPOSIT_STATUS.get('withdrawal_usd', 0):.2f} "
               f"funded=${float(DEPOSIT_STATUS.get('funded_usd') or 0):.2f}")
        print(_dl, flush=True)
        with open(_DH.HOME_STR + "/sol-usdc-log.md", "a", encoding="utf-8", newline="") as _tf:
            _tf.write(_dl + "\n")
except Exception as _e:
    DEPOSIT_STATUS = {"reason": "error", "error": type(_e).__name__}
    print(f"DEPOSIT_WATCH_ERR {_e}", flush=True)

# tokens_usd drift guard (proposed 2026-10-12): book qty vs live trading balances.
# Deposits are added at market price, outflows removed at avg cost; drift is logged.
try:
    _tok_when = datetime.now(PT).replace(microsecond=0).isoformat()
    _tok_drift = tokens_book.reconcile(cb, savings_mode.trading_balances(cb, bals), prices, _tok_when)
    if _tok_drift:  # log diet: write only when a correction happened
        _tok_lines = tokens_book.drift_lines(_tok_drift)
        for _ln in _tok_lines:
            print(_ln, flush=True)
        with open(_DH.HOME_STR + "/sol-usdc-log.md", "a", encoding="utf-8", newline="") as _tf:
            _tf.write("".join(f"- {_ln}\n" for _ln in _tok_lines))
except Exception as _e:
    print(f"TOKENS_USD_DRIFT_ERR {_e}", flush=True)

def clamp_reverse_amt(sold, amt, bal):
    """bugfix1012: reverse size clamped to 40% of balance, SOL fee buffer, and the 15% $ cap."""
    amt = min(float(amt), float(bal) * float(size_lim["max_sell_fraction_of_balance"]))
    if sold == "SOL":
        amt = min(amt, max(0.0, float(bal) - float(size_lim["min_sol_fee_buffer"])))
    px = float(prices.get(sold) or 0)
    if px > 0:
        amt = min(amt, max_trade_usd_cap() / px)
    return max(0.0, amt)

def max_sell_amt(sold, price_usd):
    """Normal caps only: 15% of portfolio, ≤40% of balance, ≥0.02 SOL buffer. No profit cut here."""
    bal = bals[sold]
    cap = max_trade_usd_cap()
    amt = min(bal * size_lim["max_sell_fraction_of_balance"], cap / price_usd if price_usd > 0 else 0)
    if sold == "SOL":
        amt = min(amt, max(0, bal - size_lim["min_sol_fee_buffer"]))
    return max(0, amt)

def sold_token_usd_unrealized_pct(sym):
    """USD unrealized P&L% of one holding: live price vs tokens_usd avg_cost_usd.

    This is the loss-tilt measure and the profit-sale size-cut test. Not pair-rate
    P&L and not wallet-level unrealized. None when cost or price is missing.
    """
    info = (cb.get("tokens_usd") or {}).get(sym) or {}
    try:
        cost = float(info.get("avg_cost_usd") or 0)
    except (TypeError, ValueError):
        cost = 0.0
    try:
        px = float(prices.get(sym) or 0)
    except (TypeError, ValueError):
        px = 0.0
    if cost <= 0 or px <= 0:
        return None
    return (px / cost - 1.0) * 100.0

def apply_profit_sale_cut(sold, sell_amt):
    """Shrink a sale by 20% when the sold token is below its USD cost.

    Applied after the normal caps (15% of portfolio, 40% of balance, 0.02 SOL buffer).
    If USD unrealized P&L% vs tokens_usd avg cost is < 0, multiply by cut_fraction
    (0.80). At cost, in profit, or unknown cost, size is unchanged.
    Buys are not passed here. Returns (amount, cut_applied, pnl_pct).
    """
    cfg = cb.get("profit_sale_size_cut") or {}
    pnl = sold_token_usd_unrealized_pct(sold)
    if not cfg.get("enabled", True):
        return float(sell_amt), False, pnl
    if pnl is None or pnl >= 0:
        return float(sell_amt), False, pnl
    frac = float(cfg.get("cut_fraction", 0.80))
    return float(sell_amt) * frac, True, pnl

def same_pair_streak_blocks(pair):
    """True when another fill on pair would be the 4th consecutive edge-check fill."""
    st = cb.get("same_pair_streak_cap") or {}
    if not st.get("enabled", True):
        return False
    cap = H093.streak_cap(cb, int(st.get("max_consecutive_fills", 3)))  # H093 preset: 5 when on
    return st.get("pair") == pair and int(st.get("count") or 0) >= cap

ROUTE_META = [
    ("SOL→USDC", "SOL-USDC", "SOL", "USDC", "sell"),
    ("USDC→SOL", "SOL-USDC", "USDC", "SOL", "buy"),
    ("SOL→cbBTC", "SOL-cbBTC", "SOL", "cbBTC", "buy"),
    ("cbBTC→SOL", "SOL-cbBTC", "cbBTC", "SOL", "sell"),
    ("SOL→WETH", "SOL-WETH", "SOL", "WETH", "buy"),
    ("WETH→SOL", "SOL-WETH", "WETH", "SOL", "sell"),
    ("USDC→cbBTC", "USDC-cbBTC", "USDC", "cbBTC", "buy"),
    ("cbBTC→USDC", "USDC-cbBTC", "cbBTC", "USDC", "sell"),
    ("USDC→WETH", "USDC-WETH", "USDC", "WETH", "buy"),
    ("WETH→USDC", "USDC-WETH", "WETH", "USDC", "sell"),
    ("cbBTC→WETH", "cbBTC-WETH", "cbBTC", "WETH", "buy"),
    ("WETH→cbBTC", "cbBTC-WETH", "WETH", "cbBTC", "sell"),
]

def rate_from_quote(sold, bought, sell_amt, buy_amt, pair):
    base = pairs[pair]["base"]
    quote = pairs[pair]["quote"]
    amt = {sold: sell_amt, bought: buy_amt}
    return amt[quote] / amt[base]

def apply_gas_to_rate(side, sold, bought, sell_amt, buy_amt, pair, gas_sol, sol_usd):
    gas_usd = gas_sol * sol_usd
    quote = pairs[pair]["quote"]
    base = pairs[pair]["base"]
    amt = {sold: sell_amt, bought: buy_amt}
    if quote == "USDC":
        gas_quote = gas_usd
    elif quote == "SOL":
        gas_quote = gas_sol
    elif quote == "cbBTC":
        gas_quote = gas_usd / prices["cbBTC"]
    else:
        gas_quote = gas_usd / prices["WETH"]
    base_amt = amt[base]
    if side == "sell":
        return (amt[quote] - gas_quote) / base_amt
    return (amt[quote] + gas_quote) / base_amt

def score_leg(pair, sold, bought, side, sell_amt, buy_amt, gas_sol, basis, reverse_of_rebalance=False, reverse_of_rsi=False):
    """Required eff% AND a leftover floor after fees. Above zero is not enough.

    eff% is bases + holdings bias + own-pair multiplier + loss tilt + skip-halving.
    Only a reverse of a band or underweight rebalance doubles that composed % before the clamp:
    eff% = min(5.0, max(0.20, composed_or_doubled)). Basis routes and a reverse of
    an edge-check fill do not double. A reverse of rsi_rebalance is not doubled and
    is not that clamp: required profit is flat 5%, and the leftover floor is not added.
    Otherwise the leftover floor is extra, not a replacement:
    0.15% on USDC pairs, 0.10% on SOL-cbBTC, SOL-WETH, and cbBTC-WETH.
    gas_sol must already include the Solana network fee and any Jito tip.
    Ultra platform fee, when present, is removed from buy_amt before this call.
    """
    exec_rate = rate_from_quote(sold, bought, sell_amt, buy_amt, pair)
    # rsi flat 5% wins over the 2x band path. Do not double and then replace.
    if reverse_of_rsi:
        reverse_of_rebalance = False
    buy_eff, sell_eff, buy_tilt, sell_tilt, m, buy_b, sell_b, reb_mult = eff_pcts(
        pair, bought, sold, exec_rate, reverse_of_rebalance=reverse_of_rebalance)
    if reverse_of_rsi:
        spec = friday_rsi.reverse_eff("rsi_rebalance", 0.0)
        buy_eff = sell_eff = spec["eff"]
        reb_mult = spec["reb_mult"]
    # buyside 2026-10-12: required % = the bought coin's BUY leg only (biased, tilted,
    # multiplied, halved and clamped); replaces lowerside. RSI flat 5% sets both legs to 5, unchanged.
    eff, eff_leg = required_pct(buy_eff, sell_eff)
    # min1pct 2026-10-12: required % = max(1.0 x skip-halving mult, lower-side clamped %); RSI flat 5% unchanged.
    _eff_pre = eff
    eff = EP.apply_min_required(eff, MIN_FLOOR, reverse_of_rsi=reverse_of_rsi,
                              halving_mult=apply_skip_halving(1.0))  # 0.5% while skip-halved
    if eff > _eff_pre + 1e-12:
        eff_leg = "min1"
    if side == "sell":
        thresh = basis * (1 + eff / 100.0)
        gap = (exec_rate - thresh) / thresh * 100.0
        edge = (exec_rate / basis - 1) * 100.0
        clears = exec_rate >= thresh
    else:
        thresh = basis * (1 - eff / 100.0)
        gap = (thresh - exec_rate) / thresh * 100.0
        edge = (basis / exec_rate - 1) * 100.0
        clears = exec_rate <= thresh
    exec_net = apply_gas_to_rate(side, sold, bought, sell_amt, buy_amt, pair, gas_sol, prices["SOL"])
    if side == "sell":
        gap_net = (exec_net - thresh) / thresh * 100.0
        clears_required = exec_net >= thresh
    else:
        gap_net = (thresh - exec_net) / thresh * 100.0
        clears_required = exec_net <= thresh
    floor = 0.0 if reverse_of_rsi else VQ.additional_profit_floor_pct(pair)
    edge_net = VQ.fee_inclusive_edge_pct(side, basis, exec_net)
    leftover = None if edge_net is None else edge_net - eff
    # Above-zero after fees does not clear. Need required eff% and the leftover floor.
    clears_leftover = leftover is not None and leftover + 1e-9 >= floor
    clears_net = bool(clears_required and clears_leftover)
    return {
        "exec_rate": exec_rate, "exec_net": exec_net, "thresh": thresh, "eff": eff,
        "eff_leg": eff_leg, "buy_eff": buy_eff, "sell_eff": sell_eff,
        "gap": gap, "gap_net": gap_net, "clears": clears, "clears_net": clears_net,
        "clears_required": bool(clears_required), "clears_leftover": clears_leftover,
        "edge": edge, "edge_net": edge_net, "leftover_pct": leftover, "leftover_floor_pct": floor,
        "buy_tilt": buy_tilt, "sell_tilt": sell_tilt, "m": m, "buy_b": buy_b, "sell_b": sell_b,
        "buy_amt": buy_amt, "gas_sol": gas_sol,
        "sell_usd": float(sell_amt) * float(prices.get(sold) or 0),
        "reverse_of_rebalance": bool(reverse_of_rebalance), "rebalance_eff_mult": reb_mult,
        "reverse_of_rsi": bool(reverse_of_rsi),
    }

def pick_best(scored):
    """Highest fee-inclusive gap wins. Tie: jupiter_manual > orca > raydium."""
    return max(scored, key=lambda s: (s["gap_net"], VENUE_RANK.get(s["venue"], 0)))

# Update cbBTC/WETH prices from first successful sells to USDC during route loop
routes_out = {}
quote_source = None
errors = []

def quote_basis_route(p, prefetched=None):
    """Full Jupiter+Raydium quote for one ranked basis route (top-N only).

    Top-1 also quotes Orca Whirlpool; best fee-inclusive net wins.
    prefetched = (rows, fetched_at) from the parallel quote phase: no network
    call and no QUOTE_GAP pause here. None = old sequential fetch + pause.
    """
    _pause = prefetched is None
    route, pair, sold, bought, side = p["route"], p["pair"], p["sold"], p["bought"], p["side"]
    sell_amt, capped, profit_cut, sold_pnl_r = p["sell_amt"], p["capped"], p["profit_cut"], p["sold_pnl_r"]
    _orca = EP.include_orca_for_rank(p.get("quote_rank"))
    rank_fields = {
        "quoted": True, "est_gap_net_pct": p.get("est_gap_net_pct_r"), "quote_rank": p.get("quote_rank"),
        **size_tilt_fields(pair, p.get("tilt_clamped")),
        "orca_quoted": _orca,
    }
    if prefetched is None:
        rows, notes = fetch_route_quotes(sold, bought, to_base(sell_amt, sold), include_orca=_orca)
        remember_route_quotes(route, sell_amt, rows)
    else:
        rows, notes = route_rows_notes(sold, bought, prefetched[0])
        remember_route_quotes(route, sell_amt, rows, at=prefetched[1])
    scored = []
    scored_by_venue = {}
    for row in rows:
        row = VQ.adjust_for_ultra_fee(row)
        if not row.get("ok"):
            continue
        buy_amt_v = from_base(row["out_amount"], bought)
        sc = score_leg(pair, sold, bought, side, sell_amt, buy_amt_v, float(row["gas_sol"]), pairs[pair]["rate"])
        kh_hurdle(sc, False)
        sc["venue"] = row["venue"]
        sc["buy_amt"] = buy_amt_v
        sc["row"] = row
        scored.append(sc)
        scored_by_venue[row["venue"]] = sc
    peers = peer_summary(rows, scored_by_venue)
    if not scored:
        err = "all_venues_failed: " + "; ".join(notes)
        routes_out[route] = {
            "pair": pair, "side": side, "error": err, "sell_amt": sell_amt,
            "sell_amt_capped": capped, "profit_sale_cut": profit_cut,
            "sold_unrealized_pnl_pct": sold_pnl_r,
            "venue_quotes": peers, "venue_notes": notes,
            **rank_fields,
        }
        errors.append((route, err))
        print(f"{route}: ERROR {err}", flush=True)
        if _pause:
            time.sleep(QUOTE_GAP)
        return
    best = pick_best(scored)
    buy_amt = best["buy_amt"]
    maybe_any_profit_bypass(best, route=route)
    bad_state = winning_quote_state(pair, best.get("row"), bool(best["clears_net"]))
    # refine USD prices from the winning executable quote
    if bought == "USDC" and sell_amt > 0:
        prices[sold] = buy_amt / sell_amt
    elif sold == "USDC" and buy_amt > 0:
        prices[bought] = sell_amt / buy_amt
    routes_out[route] = {
        "pair": pair, "side": side, "exec": round(best["exec_rate"], 6), "exec_net": round(best["exec_net"], 6),
        "thresh": round(best["thresh"], 6), "eff_pct": best["eff"],
        "buy_loss_tilt": best["buy_tilt"], "sell_loss_tilt": best["sell_tilt"], "mult": best["m"],
        "gap_pct": round(best["gap"], 3), "gap_net_pct": round(best["gap_net"], 3),
        "clears": best["clears"], "clears_net": best["clears_net"], "gas_sol": best["gas_sol"],
        "clears_required": best["clears_required"], "clears_leftover": best["clears_leftover"],
        "leftover_pct": None if best["leftover_pct"] is None else round(best["leftover_pct"], 4),
        "leftover_floor_pct": best["leftover_floor_pct"],
        "edge_net_pct": None if best["edge_net"] is None else round(best["edge_net"], 4),
        "any_profit_bypass": bool(best.get("any_profit_bypass")),
        "sold": sold, "bought": bought, "edge_pct": round(best["edge"], 3),
        "src": best["venue"], "sell_amt": sell_amt, "buy_amt": buy_amt,
        "sell_amt_capped": capped, "profit_sale_cut": profit_cut,
        "sold_unrealized_pnl_pct": sold_pnl_r,
        "buy_base": best["buy_b"], "sell_base": best["sell_b"],
        "venue_quotes": peers, "venue_notes": notes,
        **rank_fields,
        **bad_state,
    }
    if bad_state["bad_condition_skip"]:
        bad_blocks[-1]["route"] = route
        print(
            f"BAD_CONDITION_SKIP {route} pair={pair} venue={best['venue']} "
            f"{';'.join(bad_state['bad_condition_reasons'])}",
            flush=True,
        )
    print(
        f"{route}: venue={best['venue']} exec={best['exec_rate']:.6f} thresh={best['thresh']:.6f} "
        f"gap_net={best['gap_net']:.3f}% clears_net={best['clears_net']} eff={best['eff']}% "
        f"leftover={best['leftover_pct']} floor={best['leftover_floor_pct']}% "
        f"impact_pct={bad_state['price_impact_pct']} "
        f"priority_micro={bad_state['priority_fee_micro_lamports_per_cu']} "
        f"profit_cut={profit_cut} sold_pnl={sold_pnl_r} est_gap_net={p.get('est_gap_net_pct_r')} "
        f"skipped={notes or '-'}",
        flush=True,
    )
    _cr = _clear_req_log(route, best, True, bad_state, False)
    if _cr is not None:
        routes_out[route]["clear_req"] = _cr
    if _pause:
        time.sleep(QUOTE_GAP)

def _clear_req_log(route, best, size_ok=True, bad_state=None, is_reverse=False):
    """loggingonly 2026-10-13: full required % incl. REV hurdle + block reasons + gap to clear. Never alters decisions."""
    if not _DH.research_logging():  # template: study-only log, off
        return None
    try:
        eff = float(best.get("eff") or 0.0)
        fl = float(best.get("leftover_floor_pct") or 0.0)
        h = best.get("hurdle_pct")
        h = 0.0 if h is None else float(h)
        req = eff + fl + h
        en = best.get("edge_net")
        gap_clear = None if (en is None or req == float("inf")) else round(req - float(en), 4)
        blocks = []
        if not best.get("clears_required"):
            blocks.append("below_required_eff")
        if best.get("hurdle_block") or (best.get("leftover_pct") is not None and h > 0 and float(best["leftover_pct"]) - h + 1e-9 < fl):
            blocks.append("hurdle")
        elif best.get("leftover_pct") is not None and float(best["leftover_pct"]) + 1e-9 < fl:
            blocks.append("leftover_floor")
        if not size_ok:
            blocks.append("size")
        if bad_state and bad_state.get("bad_condition_skip"):
            blocks.append("bad_condition:" + ";".join(bad_state.get("bad_condition_reasons") or []))
        if route in (globals().get("COOLED_ROUTES") or ()):
            blocks.append("route_hit_cooldown")
        if best.get("any_profit_bypass"):
            blocks.append("any_profit_bypass_on")
        print(
            f"CLEAR_REQ {route} rev={bool(is_reverse)} eff={eff:.4f}% floor={fl:.4f}% "
            f"hurdle={'inf' if h == float('inf') else round(h, 4)}% hurdle_usd={best.get('hurdle_usd')} "
            f"required_total={'inf' if req == float('inf') else round(req, 4)}% "
            f"edge_net={None if en is None else round(float(en), 4)}% gap_to_clear={gap_clear} "
            f"clears_net={bool(best.get('clears_net'))} blocks={','.join(blocks) or '-'}",
            flush=True,
        )
        rec = {"route": route, "rev": bool(is_reverse), "eff_pct": round(eff, 4), "floor_pct": round(fl, 4),
               "hurdle_pct": None if h == float("inf") else round(h, 4), "hurdle_inf": h == float("inf"),
               "hurdle_usd": best.get("hurdle_usd"),
               "required_total_pct": None if req == float("inf") else round(req, 4),
               "edge_net_pct": None if en is None else round(float(en), 4), "gap_to_clear_pts": gap_clear,
               "gap_net_pct": None if best.get("gap_net") is None else round(float(best["gap_net"]), 4),
               "clears_net": bool(best.get("clears_net")), "blocks": blocks}
        _CLEAR_REQ_ROWS.append(rec)
        return rec
    except Exception as _e:
        print(f"CLEAR_REQ {route} log_error={type(_e).__name__}", flush=True)
        return None

_CLEAR_REQ_ROWS = []
_CLEAR_REQ_DONE = [False]
def _clear_req_flush():
    """Append one JSON line per check (logging only)."""
    if not _DH.research_logging():
        return
    if _CLEAR_REQ_DONE[0]:
        return
    _CLEAR_REQ_DONE[0] = True
    try:
        _now = datetime.now(ZoneInfo(_TZ_NAME))
        _d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runner-logs")
        os.makedirs(_d, exist_ok=True)
        _p = os.path.join(_d, f"clear-req-{_now:%Y-%m-%d}.jsonl")
        with open(_p, "a", encoding="utf-8", newline="") as _f:
            _f.write(json.dumps({"t": _now.isoformat(timespec="seconds"),
                                 "cooled_routes": sorted(globals().get("COOLED_ROUTES") or ()),
                                 "routes": _CLEAR_REQ_ROWS}) + "\n")
    except Exception as _e:
        print(f"CLEAR_REQ_FLUSH_FAIL {type(_e).__name__}: {str(_e)[:160]}", flush=True)
_atexit.register(_clear_req_flush)

def est_gap_net(pair, sold, bought, side, sell_amt, basis, reverse_of_rebalance=False, reverse_of_rsi=False):
    """Rank-only gap_net from USD marks and the flat gas estimate. Never a fill input."""
    est_buy = EP.mark_buy_amount(sell_amt, prices.get(sold), prices.get(bought))
    if not est_buy:
        return None
    try:
        sc = score_leg(pair, sold, bought, side, sell_amt, est_buy, GAS_SOL_EST, basis,
                       reverse_of_rebalance=reverse_of_rebalance, reverse_of_rsi=reverse_of_rsi)
    except Exception as est_err:
        print(f"EST_ERR {pair} {sold}->{bought} {type(est_err).__name__}", flush=True)
        return None
    return float(sc["gap_net"])

# Fear & Greed (rule 1) + Kraken 24h VWAP (rule 3) hard gates, fetched once
# per check (FNG cached ~1h). Applied here, before quoting, to every basis and
# reverse candidate, so no fill path (any-profit, quote reuse, reverses) can
# trade a blocked USDC route. Fail closed per rule. See market_gates.py.
import market_gates as _MG
try:
    MG = _MG.Gates(cb)
except Exception as _mg_err:
    MG = _MG.Gates(cb, fng={"value": None, "error": type(_mg_err).__name__},
                   vwap={"error": type(_mg_err).__name__})
GREED_MULT = MG.size_mult()
_rcc = H093.rev_cap_pct(cb)  # H093 preset: 60 when on, else stored reversal_close_cap_pct (85)
H093_STATE = H093.load_state(cb) if H093.enabled(cb) else None
print(f"H093 enabled={H093.enabled(cb)} state={None if H093_STATE is None else {k: (v.get('g20_50'), v.get('ret30')) for k, v in (H093_STATE.get('coins') or {}).items()}} "
      f"src={None if H093_STATE is None else H093_STATE.get('source')} err={None if H093_STATE is None else (H093_STATE.get('error') or H093_STATE.get('stale_error'))}", flush=True)
REVERSAL_CAP = 1.0 if _rcc is None else max(0.0, min(1.0, float(_rcc) / 100.0))
print(f"MARKET_GATES fng={MG.fng} vwap={MG.vwap} greed_mult={GREED_MULT} reversal_cap={REVERSAL_CAP}", flush=True)

def rank_prefilter(kind, pair, sold, bought, route=None):
    """Cheap no-network blocks that make a route unfillable this check."""
    _mg = MG.block(sold, bought, skip_vwap=(kind == "reverse"))
    if _mg:
        MG.note(route or f"{sold}→{bought}", _mg)
        return _mg
    if fade_trend.current().blocks(sold):
        return "fade_trend"
    if kind == "basis" and KH_CFG["enabled"]:
        try:
            _ft = fade_trend.current()
            _ft.begin_check()
            _k = EP.knife_block(sold, bought, _ft.quote_by_symbol)
        except Exception:
            _k = "knife_price_missing"
        if _k:
            KH_STATS["knife" if _k == "knife_24h" else "knife_price_missing"] += 1
            return _k
    if kind == "basis" and friday_rsi.basis_unwinds_rsi(rl.get(pair), sold, bought):
        return "rsi_unwind"
    if same_pair_streak_blocks(pair):
        return "same_pair_streak_cap"
    return None

# Quote cap: size and rank every route without a venue call. Quoting happens
# after the reverse window is ranked too (see QUOTE_RANK below).
rank_pool = []
rev_out = {}
if FLAT_MARKET:
    print("FLAT_MARKET_SKIP skip ranking/quoting", flush=True)
for route, pair, sold, bought, side in (ROUTE_META if not FLAT_MARKET else ()):
    # Savings JitoSOL is not an edge inventory token and is never sold here.
    if held_outside_book(sold) or held_outside_book(bought):
        routes_out[route] = {
            "pair": pair, "side": side, "error": "held_not_edge",
            "skip_reason": "held_not_edge_inventory",
            "sold": sold, "bought": bought,
        }
        print(f"QUOTE_SKIP {route} held_not_edge_inventory", flush=True)
        continue
    capped = max_sell_amt(sold, prices[sold])
    # Per-pair P&L tilt first (clamped to that pair's caps), then the profit-sale cut.
    _pt = pair_tilt(pair)
    tilted, tilt_clamped = tilted_sell_amt(pair, sold, capped)
    sell_amt, profit_cut, sold_pnl = apply_profit_sale_cut(sold, tilted)
    if GREED_MULT != 1.0:  # FNG > 75: final x0.5 after caps/tilts/cuts
        sell_amt = sell_amt * GREED_MULT
    _dm = H093.sell_mult(cb, sold, bought, H093_STATE)  # H093 D cut: coin->USDC edge sells
    if _dm != 1.0:
        print(f"H093_DCUT {route} x{_dm}", flush=True)
        sell_amt = sell_amt * _dm
    sold_pnl_r = None if sold_pnl is None else round(sold_pnl, 4)
    skip_reason = inventory_skip_reason(sold, sell_amt, 0.05, _pt["caps"])
    if skip_reason:
        routes_out[route] = {
            "pair": pair, "side": side, "error": "dust", "skip_reason": skip_reason,
            "sell_amt": sell_amt,
            "sell_amt_capped": capped, "profit_sale_cut": profit_cut,
            "sold_unrealized_pnl_pct": sold_pnl_r,
            "size_tilt": _pt["mult"], "size_tilt_label": _pt["label"],
            "pair_pnl_pct": None if _pt["pnl_pct"] is None else round(_pt["pnl_pct"], 4),
        }
        print(f"QUOTE_SKIP {route} {skip_reason} sell_amt={sell_amt}", flush=True)
        continue
    if EP.below_min_size(sell_amt * float(prices.get(sold) or 0), MIN_FLOOR, is_reverse=False):
        routes_out[route] = {"pair": pair, "side": side, "error": "min_size", "skip_reason": "min_size",
                             "sell_amt": sell_amt, "sell_usd": round(sell_amt * float(prices.get(sold) or 0), 3)}
        print(f"QUOTE_SKIP {route} min_size ${sell_amt * float(prices.get(sold) or 0):.2f}<${MIN_FLOOR['min_edge_usd']}", flush=True)
        continue
    est = est_gap_net(pair, sold, bought, side, sell_amt, pairs[pair]["rate"])
    rank_pool.append({
        "kind": "basis", "route": route, "pair": pair, "sold": sold, "bought": bought, "side": side,
        "sell_amt": sell_amt, "capped": capped, "profit_cut": profit_cut, "sold_pnl_r": sold_pnl_r,
        "tilt_clamped": tilt_clamped,
        "est_gap_net_pct": est, "est_gap_net_pct_r": None if est is None else round(est, 3),
        "prefilter_block": rank_prefilter("basis", pair, sold, bought, route),
    })

trades = []
with open(_DH.HOME_STR + "/trades.jsonl", encoding="utf-8") as f:
    for line in f:
        if line.strip():
            _t = json.loads(line)
            # Savings buys (JitoSOL / saved cbBTC) are not reversible edge fills.
            if _t.get("savings") or _t.get("kind") == "savings_buy":
                continue
            trades.append(_t)

def last_fill_size(side_str, tx=None):
    for t in reversed(trades):
        if t["side"] != side_str:
            continue
        if tx and t.get("tx") and t.get("tx") != tx:
            continue
        return t["sold_amount"], t["bought_amount"], t.get("rate")
    if tx:
        for t in reversed(trades):
            if t["side"] == side_str:
                return t["sold_amount"], t["bought_amount"], t.get("rate")
    return None, None, None

def prepare_one_reverse(pair, info):
    """Size one window-fill reverse without a venue call.

    Returns (final_result, None) when it is blocked before quoting, or
    (None, prep) with everything quote_one_reverse needs.
    """
    last_side = info["side"]
    fill_rate = info["rate"]
    fill_tx = info.get("tx")
    rsi_rev = friday_rsi.canonical_fill_kind(info.get("kind")) == "rsi_rebalance"
    reb_kind = None if rsi_rev else last_fill_rebalance_kind(info)
    a, b = last_side.split("→")
    rev_route = f"{b}→{a}"
    route_key = f"REV:{rev_route}"
    if fill_tx:
        route_key = f"REV:{rev_route}#{str(fill_tx)[:12]}"
    bought_amt_prev = friday_rsi.fill_bought_amount(info, trades)
    if bought_amt_prev is None:
        _, bought_amt_prev, _ = last_fill_size(last_side, fill_tx)
    base_out = {
        "rev_route": rev_route, "route_key": route_key, "pair": pair,
        "fill_tx": fill_tx, "fill_rate": fill_rate,
        "fill_time_pt": info.get("time_pt"),
        "last_fill_kind": info.get("kind"),
        "fill_reversed": bool(info.get("reversed")),
        "clears": False, "clears_net": False,
    }
    if bought_amt_prev is None:
        return {**base_out, "error": "no_trade_row"}, None
    opp_sold, opp_bought = b, a
    # Rule 2: a reverse closes at most reversal_close_cap_pct (85%) of the prior fill.
    # dailyv2c: daily_v2 fills reverse at most daily_rebalance_v2.reversal_cap_pct (50%)
    opp_sell_amt = bought_amt_prev * _REV.reversal_cap(info.get("kind"), REVERSAL_CAP, cb)
    bal = bals[opp_sold]
    # bugfix1012: shrink an oversized reverse instead of skipping it:
    # <= 40% of the coin balance, keep the 0.02 SOL buffer, <= 15%-of-portfolio $ cap.
    _rev_raw_amt = opp_sell_amt
    opp_sell_amt = clamp_reverse_amt(opp_sold, opp_sell_amt, bal)
    if opp_sell_amt + 1e-15 < _rev_raw_amt:
        print(f"REV {pair} tx={str(fill_tx)[:8] if fill_tx else '-'}: clamped {_rev_raw_amt:.9g}->{opp_sell_amt:.9g} {opp_sold}", flush=True)
    sell_frac = opp_sell_amt / bal if bal > 0 else 999
    size_ok = sell_frac <= size_lim["max_sell_fraction_of_balance"] + 1e-9
    if opp_sold == "SOL" and bals["SOL"] - opp_sell_amt < size_lim["min_sol_fee_buffer"]:
        size_ok = False
    notional = opp_sell_amt * float(prices.get(opp_sold) or 0)
    within_usd_cap = notional <= max_trade_usd_cap() + 1e-9
    if opp_sell_amt > bal + 1e-12 or opp_sell_amt * prices[opp_sold] < 0.01:
        print(f"REV {pair} tx={str(fill_tx)[:8] if fill_tx else '-'}: size blocked frac={sell_frac:.3f}", flush=True)
        return {
            **base_out, "size_ok": False, "sell_frac": round(sell_frac, 3),
            "opp_sell_amt": opp_sell_amt, "opp_sold": opp_sold, "error": "insufficient_or_dust",
            "profit_sale_cut": False,
        }, None
    sold_pnl = sold_token_usd_unrealized_pct(opp_sold)
    profit_cut = False
    if size_ok and within_usd_cap:
        opp_sell_amt, profit_cut, sold_pnl = apply_profit_sale_cut(opp_sold, opp_sell_amt)
        sell_frac = opp_sell_amt / bal if bal > 0 else 999
        if opp_sell_amt * prices[opp_sold] < 0.01:
            print(f"REV {pair} tx={str(fill_tx)[:8] if fill_tx else '-'}: profit cut to dust", flush=True)
            return {
                **base_out, "size_ok": False, "sell_frac": round(sell_frac, 3),
                "opp_sell_amt": opp_sell_amt, "opp_sold": opp_sold, "error": "profit_cut_dust",
                "profit_sale_cut": True,
                "sold_unrealized_pnl_pct": None if sold_pnl is None else round(sold_pnl, 4),
            }, None
    if GREED_MULT != 1.0:  # FNG > 75: final x0.5, still subject to dust minimum
        opp_sell_amt = opp_sell_amt * GREED_MULT
        sell_frac = opp_sell_amt / bal if bal > 0 else 999
        if opp_sell_amt * prices[opp_sold] < 0.01:
            return {
                **base_out, "size_ok": False, "sell_frac": round(sell_frac, 3),
                "opp_sell_amt": opp_sell_amt, "opp_sold": opp_sold, "error": "greed_cut_dust",
                "profit_sale_cut": profit_cut,
            }, None
    _dm = H093.sell_mult(cb, opp_sold, opp_bought, H093_STATE)  # H093 D cut: coin->USDC reversal sells
    if _dm != 1.0:
        print(f"H093_DCUT REV {pair} x{_dm}", flush=True)
        opp_sell_amt = opp_sell_amt * _dm
        sell_frac = opp_sell_amt / bal if bal > 0 else 999
    if EP.below_min_size(opp_sell_amt * float(prices.get(opp_sold) or 0), MIN_FLOOR, is_reverse=True):
        print(f"QUOTE_SKIP {route_key} min_size ${opp_sell_amt * float(prices.get(opp_sold) or 0):.2f}<${MIN_FLOOR['min_reverse_usd']}", flush=True)
        return {
            **base_out, "size_ok": False, "sell_frac": round(sell_frac, 3),
            "opp_sell_amt": opp_sell_amt, "opp_sold": opp_sold, "error": "min_size",
            "skip_reason": "min_size", "profit_sale_cut": profit_cut,
        }, None
    if not size_ok:
        print(f"QUOTE_SKIP {route_key} max_size_or_buffer frac={sell_frac:.3f}", flush=True)
        return {
            **base_out, "size_ok": False, "sell_frac": round(sell_frac, 3),
            "opp_sell_amt": opp_sell_amt, "opp_sold": opp_sold, "error": "insufficient_or_dust",
            "skip_reason": "max_size_or_buffer", "profit_sale_cut": profit_cut,
        }, None
    base = pairs[pair]["base"]
    side = "sell" if opp_sold == base else "buy"
    return None, {
        "kind": "reverse", "route": route_key, "pair": pair, "base_out": base_out,
        "fill_rate": fill_rate, "fill_tx": fill_tx, "route_key": route_key,
        "opp_sold": opp_sold, "opp_bought": opp_bought, "opp_sell_amt": opp_sell_amt,
        "sold": opp_sold, "bought": opp_bought, "side": side,
        "size_ok": size_ok, "sell_frac": sell_frac, "sold_pnl": sold_pnl,
        "profit_cut": profit_cut, "reb_kind": reb_kind, "rsi_rev": rsi_rev,
    }

def quote_one_reverse(prep, prefetched=None):
    """Full Jupiter+Raydium quote for one ranked reverse (top-N only).

    Top-1 also quotes Orca Whirlpool; best fee-inclusive net wins.
    prefetched: see quote_basis_route.
    """
    _pause = prefetched is None
    pair = prep["pair"]
    base_out = dict(prep["base_out"])
    _orca = EP.include_orca_for_rank(prep.get("quote_rank"))
    base_out.update({"quoted": True, "est_gap_net_pct": prep.get("est_gap_net_pct_r"),
                     "quote_rank": prep.get("quote_rank"), "orca_quoted": _orca})
    fill_rate, fill_tx, route_key = prep["fill_rate"], prep["fill_tx"], prep["route_key"]
    opp_sold, opp_bought, opp_sell_amt = prep["opp_sold"], prep["opp_bought"], prep["opp_sell_amt"]
    side, size_ok, sell_frac = prep["side"], prep["size_ok"], prep["sell_frac"]
    sold_pnl, profit_cut = prep["sold_pnl"], prep["profit_cut"]
    reb_kind, rsi_rev = prep["reb_kind"], prep["rsi_rev"]
    if prefetched is None:
        rows, notes = fetch_route_quotes(opp_sold, opp_bought, to_base(opp_sell_amt, opp_sold), include_orca=_orca)
        remember_route_quotes(route_key, opp_sell_amt, rows)
    else:
        rows, notes = route_rows_notes(opp_sold, opp_bought, prefetched[0])
        remember_route_quotes(route_key, opp_sell_amt, rows, at=prefetched[1])
    scored = []
    scored_by_venue = {}
    for row in rows:
        row = VQ.adjust_for_ultra_fee(row)
        if not row.get("ok"):
            continue
        buy_amt_v = from_base(row["out_amount"], opp_bought)
        sc = score_leg(pair, opp_sold, opp_bought, side, opp_sell_amt, buy_amt_v, float(row["gas_sol"]), fill_rate,
                       reverse_of_rebalance=bool(reb_kind), reverse_of_rsi=bool(rsi_rev))
        kh_hurdle(sc, True)
        sc["venue"] = row["venue"]
        sc["row"] = row
        sc["clears"] = bool(sc["clears"] and size_ok)
        sc["clears_net"] = bool(sc["clears_net"] and size_ok)
        maybe_any_profit_bypass(sc, route=route_key)
        # size_ok still required even under any-profit bypass
        if sc.get("any_profit_bypass") and not size_ok:
            sc["clears"] = False
            sc["clears_net"] = False
            sc["any_profit_bypass"] = False
        scored.append(sc)
        scored_by_venue[row["venue"]] = sc
    peers = peer_summary(rows, scored_by_venue)
    if not scored:
        err = "all_venues_failed: " + "; ".join(notes)
        print(f"{route_key}: ERROR {err}", flush=True)
        if _pause:
            time.sleep(QUOTE_GAP)
        return {
            **base_out, "error": err, "size_ok": size_ok,
            "opp_sell_amt": opp_sell_amt, "opp_sold": opp_sold, "sell_frac": round(sell_frac, 3),
            "venue_quotes": peers, "venue_notes": notes,
        }
    best = pick_best(scored)
    buy_amt = best["buy_amt"]
    maybe_any_profit_bypass(best, route=route_key)
    if best.get("any_profit_bypass") and not size_ok:
        best["clears"] = False
        best["clears_net"] = False
        best["any_profit_bypass"] = False
    bad_state = winning_quote_state(pair, best.get("row"), bool(best["clears_net"]))
    out = {
        **base_out,
        "thresh": best["thresh"], "eff_pct": best["eff"],
        "exec": round(best["exec_rate"], 6), "exec_net": round(best["exec_net"], 6),
        "gap_pct": round(best["gap"], 3), "gap_net_pct": round(best["gap_net"], 3),
        "size_ok": size_ok, "sell_frac": round(sell_frac, 3),
        "clears": best["clears"], "clears_net": best["clears_net"],
        "opp_sell_amt": opp_sell_amt, "opp_sold": opp_sold, "opp_bought": opp_bought,
        "buy_loss_tilt": best["buy_tilt"], "sell_loss_tilt": best["sell_tilt"],
        "mult": best["m"], "buy_amt": buy_amt, "side": side,
        "edge_pct": round(best["edge"], 3),
        "edge_net_pct": None if best["edge_net"] is None else round(best["edge_net"], 4),
        "leftover_pct": None if best["leftover_pct"] is None else round(best["leftover_pct"], 4),
        "leftover_floor_pct": best["leftover_floor_pct"],
        "clears_required": best["clears_required"], "clears_leftover": best["clears_leftover"],
        "src": best["venue"], "gas_sol": best["gas_sol"],
        "profit_sale_cut": profit_cut,
        "sold_unrealized_pnl_pct": None if sold_pnl is None else round(sold_pnl, 4),
        "venue_quotes": peers, "venue_notes": notes,
        "reverse_of_rebalance": bool(reb_kind),
        "reverse_of_rsi": bool(rsi_rev),
        "rebalance_eff_mult": best.get("rebalance_eff_mult", 1.0),
        **bad_state,
    }
    if bad_state["bad_condition_skip"]:
        bad_blocks[-1]["route"] = route_key
        print(
            f"BAD_CONDITION_SKIP {route_key} pair={pair} venue={best['venue']} "
            f"{';'.join(bad_state['bad_condition_reasons'])}",
            flush=True,
        )
    print(
        f"{route_key}: venue={best['venue']} exec={best['exec_rate']:.6f} thresh={best['thresh']:.6f} "
        f"gap_net={best['gap_net']:.3f}% size_ok={size_ok} clears_net={best['clears_net']} "
        f"eff={best['eff']}% rebalance_x={best.get('rebalance_eff_mult', 1)} "
        f"impact_pct={bad_state['price_impact_pct']} "
        f"priority_micro={bad_state['priority_fee_micro_lamports_per_cu']} "
        f"profit_cut={profit_cut} skipped={notes or '-'}",
        flush=True,
    )
    out["clear_req"] = _clear_req_log(route_key, best, size_ok, bad_state, True)
    if _pause:
        time.sleep(QUOTE_GAP)
    return out

rev_cfg = cb.get("reverse_last_trade") or {}
# rev_out already {} when flat; rebuilt below when not flat
if not FLAT_MARKET:
    rev_out = {}
for pair in (rl if not FLAT_MARKET else {}):
    pair_results = []
    for info in friday_rsi.unreversed_fills(rl, pair, rev_cfg):
        if EP.reverse_fill_expired(info, datetime.now(PT), REVERSE_MAX_AGE_H):
            print(
                f"REV_EXPIRED {pair} tx={str((info or {}).get('tx') or '-')[:12]} "
                f"time_pt={(info or {}).get('time_pt')} max_age_h={REVERSE_MAX_AGE_H}",
                flush=True,
            )
            a, b = str(info.get("side") or "→").split("→") if "→" in str(info.get("side") or "") else ("?", "?")
            rev_route = f"{b}→{a}"
            route_key = f"REV:{rev_route}"
            if info.get("tx"):
                route_key = f"REV:{rev_route}#{str(info.get('tx'))[:12]}"
            pair_results.append({
                "rev_route": rev_route, "route_key": route_key, "pair": pair,
                "fill_tx": info.get("tx"), "fill_rate": info.get("rate"),
                "fill_time_pt": info.get("time_pt"), "last_fill_kind": info.get("kind"),
                "fill_reversed": bool(info.get("reversed")), "expired": True,
                "skip_reason": "reverse_expired", "quoted": False,
                "clears": False, "clears_net": False,
            })
            continue
        final, prep = prepare_one_reverse(pair, info)
        if final is not None:
            pair_results.append(final)
            continue
        slot = len(pair_results)
        pair_results.append(None)  # filled after ranking, keeps window order
        est = est_gap_net(pair, prep["opp_sold"], prep["opp_bought"], prep["side"], prep["opp_sell_amt"],
                          prep["fill_rate"], reverse_of_rebalance=bool(prep["reb_kind"]),
                          reverse_of_rsi=bool(prep["rsi_rev"]))
        prep.update({
            "slot": slot, "est_gap_net_pct": est,
            "est_gap_net_pct_r": None if est is None else round(est, 3),
            "prefilter_block": rank_prefilter("reverse", pair, prep["opp_sold"], prep["opp_bought"], prep["route_key"]),
        })
        rank_pool.append(prep)
    rev_out[pair] = pair_results

# QUOTE_RANK: Jupiter+Raydium quote only the QUOTE_TOP_N routes closest to
# clearing (highest estimated gap_net). Prefilter-blocked routes never take a slot.
# Hit-rate cooldown: routes with no fill in 7d are deferred for 24h; if that
# would leave < top_n, fill remaining slots with the best cooled routes.
# Reverse candidates share the cooldown of their directed reverse route.
if not FLAT_MARKET:
    _all_routes = [p.get("route") for p in rank_pool]
    _trades_boot = trades  # loaded above for reverse sizing
    ROUTE_HIT.bootstrap(EP.bootstrap_last_fills(
        _trades_boot, rl, ROUTE_HIT.data.get("deploy_pt"), 
        [EP.directed_route_key(r) for r in _all_routes] + list(
            (load_json(_DH.HOME_STR + "/allowed-pairs.json").get("directed_routes") or [])
        ),
    ))
    COOLED_ROUTES = ROUTE_HIT.refresh(datetime.now(PT))
    try:
        ROUTE_HIT.save()
    except Exception as _rhs_err:
        print(f"ROUTE_HIT_SAVE_ERR {type(_rhs_err).__name__}", flush=True)
    print(f"ROUTE_HIT cooled={sorted(COOLED_ROUTES)}", flush=True)
to_quote, not_quoted = ([], []) if FLAT_MARKET else EP.rank_quote_candidates(
    rank_pool, QUOTE_TOP_N, cooled_routes=COOLED_ROUTES,
)
for i, p_ in enumerate(to_quote):
    p_["quote_rank"] = i + 1
_quoted_ids = {id(x) for x in to_quote}
quote_rank_out = []
for p_ in to_quote + not_quoted:
    quote_rank_out.append({
        "route": p_["route"], "kind": p_["kind"], "pair": p_["pair"],
        "est_gap_net_pct": p_.get("est_gap_net_pct_r"),
        "quoted": id(p_) in _quoted_ids, "quote_rank": p_.get("quote_rank"),
        "prefilter_block": p_.get("prefilter_block"),
    })
print(
    f"QUOTE_RANK top_n={QUOTE_TOP_N} pool={len(rank_pool)} quoting="
    + json.dumps([(p_["route"], p_.get("est_gap_net_pct_r")) for p_ in to_quote]),
    flush=True,
)
def _quote_request(p_):
    """(sell, buy, amount_base_units, include_orca) for one ranked candidate."""
    _orca = EP.include_orca_for_rank(p_.get("quote_rank"))
    if p_["kind"] == "basis":
        return p_["sold"], p_["bought"], to_base(p_["sell_amt"], p_["sold"]), _orca
    return p_["opp_sold"], p_["opp_bought"], to_base(p_["opp_sell_amt"], p_["opp_sold"]), _orca

def _rows_rate_limited(rows):
    for r in rows or []:
        if not r.get("ok") and "rate_limit_429" in str(r.get("note") or ""):
            return True
    return False

def prefetch_top_quotes(items):
    """Quote the ranked top-N routes concurrently (all venues per route).

    Returns a list aligned with items: (rows, fetched_at) or None when that
    route must be re-quoted sequentially (429 / error / timeout). Network only:
    no prints from workers, no scoring, no state.
    """
    if not items:
        return [], []
    try:
        VQ.priority_micro()  # warm the shared priority-fee cache once, main thread
    except Exception:
        pass

    def _one(req):
        rows = fetch_route_rows(*req)
        return rows, time.monotonic()

    pool = ThreadPoolExecutor(max_workers=min(QUOTE_PARALLEL_MAX_WORKERS, len(items)))
    futs = [pool.submit(_one, _quote_request(p_)) for p_ in items]
    out, why = [], []
    deadline = time.monotonic() + QUOTE_PARALLEL_TIMEOUT_SEC
    for f in futs:
        try:
            rows, at = f.result(timeout=max(0.1, deadline - time.monotonic()))
        except Exception as e:  # timeout or worker error -> sequential re-quote
            out.append(None)
            why.append(f"{type(e).__name__}")
            continue
        if _rows_rate_limited(rows):
            out.append(None)
            why.append("rate_limit_429")
        else:
            out.append((rows, at))
            why.append(None)
    pool.shutdown(wait=False, cancel_futures=True)
    return out, why

QUOTE_PARALLEL = bool(_qc.get("parallel", True))
QUOTE_PARALLEL_MAX_WORKERS = int(_qc.get("parallel_max_workers", 5) or 5)  # quote5: all top-N at once
QUOTE_PARALLEL_TIMEOUT_SEC = float(_qc.get("parallel_timeout_sec", 150) or 150)
QUOTE_PHASE = {"mode": "flat_skip" if FLAT_MARKET else ("parallel" if QUOTE_PARALLEL else "sequential"),
               "routes": len(to_quote), "fallback_routes": [], "fallback_reasons": []}
if not FLAT_MARKET:
    _qp_t0 = time.monotonic()
    _pre = [None] * len(to_quote)
    if QUOTE_PARALLEL and to_quote:
        _pre, _why = prefetch_top_quotes(to_quote)
        _fb = [(p_["route"], w) for p_, r_, w in zip(to_quote, _pre, _why) if r_ is None]
        if _fb:
            QUOTE_PHASE["mode"] = "parallel_with_sequential_fallback"
            QUOTE_PHASE["fallback_routes"] = [r for r, _w in _fb]
            QUOTE_PHASE["fallback_reasons"] = sorted({w for _r, w in _fb})
            print(
                f"QUOTE_PARALLEL_FALLBACK sequential for {len(_fb)} route(s) "
                f"reasons={QUOTE_PHASE['fallback_reasons']} routes={QUOTE_PHASE['fallback_routes']}",
                flush=True,
            )
            if "rate_limit_429" in QUOTE_PHASE["fallback_reasons"]:
                # Jupiter 429s: longer backoff then sequential (no parallel retry).
                time.sleep(QUOTE_GAP * 2)
    # Same ranked order and the same per-route processing as before. A None
    # entry is fetched here sequentially with the old QUOTE_GAP pause.
    for p_, pre_ in zip(to_quote, _pre):
        if p_["kind"] == "basis":
            quote_basis_route(p_, prefetched=pre_)
        else:
            rev_out[p_["pair"]][p_["slot"]] = quote_one_reverse(p_, prefetched=pre_)
    QUOTE_PHASE["wall_sec"] = round(time.monotonic() - _qp_t0, 3)
    print(f"QUOTE_PHASE mode={QUOTE_PHASE['mode']} routes={len(to_quote)} wall_sec={QUOTE_PHASE['wall_sec']}", flush=True)
for p_ in not_quoted:
    why = p_.get("prefilter_block") or f"not_top{QUOTE_TOP_N}"
    print(f"QUOTE_SKIP_RANK {p_['route']} {why} est_gap_net={p_.get('est_gap_net_pct_r')}", flush=True)
    if p_["kind"] == "basis":
        routes_out[p_["route"]] = {
            "pair": p_["pair"], "side": p_["side"], "sold": p_["sold"], "bought": p_["bought"],
            "sell_amt": p_["sell_amt"], "sell_amt_capped": p_["capped"],
            "profit_sale_cut": p_["profit_cut"], "sold_unrealized_pnl_pct": p_["sold_pnl_r"],
            **size_tilt_fields(p_["pair"], p_.get("tilt_clamped")),
            "quoted": False, "skip_reason": why, "est_gap_net_pct": p_.get("est_gap_net_pct_r"),
        }
    else:
        rev_out[p_["pair"]][p_["slot"]] = {
            **p_["base_out"], "quoted": False, "skip_reason": why,
            "est_gap_net_pct": p_.get("est_gap_net_pct_r"),
            "size_ok": p_["size_ok"], "sell_frac": round(p_["sell_frac"], 3),
            "opp_sell_amt": p_["opp_sell_amt"], "opp_sold": p_["opp_sold"], "opp_bought": p_["opp_bought"],
            "side": p_["side"], "profit_sale_cut": p_["profit_cut"],
            "reverse_of_rebalance": bool(p_["reb_kind"]), "reverse_of_rsi": bool(p_["rsi_rev"]),
        }
RANKED_BASIS = sum(1 for p_ in rank_pool if p_["kind"] == "basis" and p_.get("est_gap_net_pct") is not None)
QUOTED_N = len(to_quote)

# recompute usd after price updates
usd_vals = book_usd_map(bals, prices)
total_usd = wallet_total_usd(bals, prices)
rank = sorted(usd_vals.items(), key=lambda x: x[1])
least2, most2 = rank[0][0], rank[-1][0]
# Keep original least/most used for thresholds (consistent with prior run style — they used prices at start)
# If ranking unchanged, fine. If changed, note it.
print("rank_final", least2, most2, "used_bias", least, most, flush=True)



def post_json(url, payload, timeout=60):
    import jupkey as _JK  # jupkey1012
    with _JK.curl_args(url) as _kh:
        r = subprocess.run(
            ["curl", "-sS", "-w", "\n%{http_code}", "--max-time", str(timeout),
             "-H", "Accept: application/json", "-H", "Content-Type: application/json", *_kh,
             "-X", "POST", "--data-binary", "@-", url],
            input=json.dumps(payload), capture_output=True, text=True,
        )
    if r.returncode != 0:
        return {"error": r.stderr.strip() or f"curl_exit_{r.returncode}"}
    body, _, code = r.stdout.rpartition("\n")
    try:
        data = json.loads(body) if body.strip() else {}
    except Exception as e:
        return {"error": f"bad_json:{e}", "http": code.strip(), "body": body[:180]}
    if isinstance(data, dict):
        data["_http"] = code.strip()
    return data

def cand_legs(c):
    if c.get("opp_sold"):
        return c["opp_sold"], c["opp_bought"], float(c["opp_sell_amt"]), float(c["buy_amt"]), c["pair"], c["side"]
    return c["sold"], c["bought"], float(c["sell_amt"]), float(c["buy_amt"]), c["pair"], c["side"]

def concentration_ok(sold, bought, sell_amt, buy_amt):
    """Post-trade max share of book USD (SOL fee buffer excluded) must be <= the cap.

    JitoSOL and Greedy Goats are not in this total. Rank and size only the four.
    """
    cap = float(size_lim.get("max_one_token_tradable_usd_pct", 90)) / 100.0
    buf = float(size_lim["min_sol_fee_buffer"])
    nb = savings_mode.book_balances(bals)
    if sold not in nb or bought not in nb:
        return False, None
    nb[sold] -= float(sell_amt)
    nb[bought] += float(buy_amt)
    if nb["SOL"] < buf - 1e-9:
        return False, None
    vals = {}
    for k in nb:
        qty = nb[k]
        if k == "SOL":
            qty = max(0.0, qty - buf)
        vals[k] = qty * float(prices.get(k) or 0)
    tot = sum(vals.values())
    if tot <= 0:
        return True, 0.0
    pct = max(vals.values()) / tot
    return pct <= cap + 1e-9, pct

_SIGNER = None

def _load_signer_keypair():
    """signerfix1014: wallet key from env or degen_home (DPAPI on Windows). Accepts a JSON
    byte array or base58; strips BOM/whitespace. Never logs the key. Fails closed."""
    raw = os.environ.get("SOLANA_TRADING_PRIVATE_KEY") or _DH.get_secret("SOLANA_TRADING_PRIVATE_KEY") or ""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    raw = str(raw).replace("\ufeff", "").strip().strip('"').strip()
    if not raw:
        raise RuntimeError("signer_key_missing")
    try:
        if raw.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(raw)))
        return Keypair.from_base58_string(raw)
    except Exception:
        raise RuntimeError("signer_env_unexpected_format") from None


def load_trading_keypair():
    """Build the keypair once per process. Does not sign by itself."""
    global _SIGNER
    if _SIGNER is not None:
        return _SIGNER
    raw = os.environ.get("SOLANA_TRADING_PRIVATE_KEY") or ""
    kp = _load_signer_keypair()  # signerfix1014: env, else DPAPI via degen_home; JSON or base58
    if str(kp.pubkey()) != ADDR_LOCAL:
        raise RuntimeError("signer_pubkey_mismatch")
    _SIGNER = kp
    return _SIGNER

def sign_ultra_tx(tx_b64, kp):
    vtx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    msg = vtx.message
    pub = kp.pubkey()
    keys = list(msg.account_keys)
    if pub not in keys:
        raise RuntimeError("signer_not_in_tx")
    idx = keys.index(pub)
    nreq = int(msg.header.num_required_signatures)
    if nreq == 1:
        signed = VersionedTransaction(msg, [kp])
    else:
        sigs = list(vtx.signatures)
        sigs[idx] = kp.sign_message(bytes(msg))
        signed = VersionedTransaction.populate(msg, sigs)
    checks = signed.verify_with_results()
    if idx >= len(checks) or not checks[idx]:
        raise RuntimeError("local_sig_verify_failed")
    return base64.b64encode(bytes(signed)).decode()

def toggle_pair_mult(cur):
    """rules1012: pair multiplier is 1x or 2x only. 1x (or anything below 2) -> 2x; 2x or more -> 1x."""
    try:
        cur = int(cur)
    except (TypeError, ValueError):
        cur = 1
    return 1 if cur >= 2 else 2

PAIR_DIRECT_RE = r"direct ([0-9]+(?:\.[0-9]+)?(?:e-?[0-9]+)?) base @([0-9]+(?:\.[0-9]+)?)"


def pair_book_update(pair, pinfo, base_qty, quote_qty, fill_rate):
    """Average every fill into pinfo['book_base'/'book_quote'] (proposed 2026-10-12).

    Old code: when fills_note had no 'X@r' parts (SOL-WETH, cbBTC-WETH: notes are
    'direct X base @r', which the old regex never matched) the rate was
    overwritten by the last fill. Seeds the totals once from the note, then adds.
    """
    import re as _re
    note = pinfo.get("fills_note") or ""
    if "book_base" not in pinfo:
        parts = _re.findall(r"(?<![a-z] )(?<!base )\b([0-9]+(?:\.[0-9]+)?)@([0-9]+(?:\.[0-9]+)?)", note)
        direct = _re.findall(PAIR_DIRECT_RE, note)
        qw = pair in ("USDC-cbBTC", "USDC-WETH")
        b = q = 0.0
        for a, r in parts:
            a, r = float(a), float(r)
            if r <= 0:
                continue
            if qw:
                q += a; b += a / r
            else:
                b += a; q += a * r
        for a, r in direct:
            b += float(a); q += float(a) * float(r)
        if b <= 0:
            b = 0.0; q = 0.0
        pinfo["book_base"], pinfo["book_quote"] = b, q
    pinfo["book_base"] = float(pinfo["book_base"]) + float(base_qty)
    pinfo["book_quote"] = float(pinfo["book_quote"]) + float(quote_qty)
    new_rate = pinfo["book_quote"] / pinfo["book_base"] if pinfo["book_base"] > 0 else fill_rate
    pinfo["fills_note"] = (note + " | " if note else "") + f"direct {base_qty} base @{fill_rate:.6f}"
    return new_rate


def apply_fill_books(pair, sold, bought, sell_amt, buy_amt, fill_rate, side_str, sig, when, gas_sol, venue="jupiter_manual", counts_as_edge=True, fill_kind=None, reverse_of_tx=None):
    import re
    # Savings buys are not edge fills. Do not touch this pair's book, streak, or multipliers.
    if held_outside_book(sold) or held_outside_book(bought):
        print(f"HELD_NOT_EDGE {side_str}", flush=True)
        return None
    base = pairs[pair]["base"]
    quote = pairs[pair]["quote"]
    amts = {sold: float(sell_amt), bought: float(buy_amt)}
    base_qty = amts[base]
    quote_qty = amts[quote]
    pinfo = cb["pairs"][pair]
    new_rate = pair_book_update(pair, pinfo, base_qty, quote_qty, fill_rate)
    pinfo["source"] = f"fill_{venue}_local_signer"
    pinfo["rate"] = new_rate
    tokens_book.record_fill(cb, sold, bought, sell_amt, buy_amt, prices, when)
    # Rebalance and morning underweight fills update the rate book only.
    # They must not change multipliers, reset another pair, or touch the streak.
    # counts_as_edge false skips all multiplier changes.
    if counts_as_edge:
        esc = cb["pair_edge_escalation"]
        cur = int(esc["multipliers"].get(pair, 1))
        # Edge fill doubles only this pair. A reverse fill on this pair also doubles;
        # it does not reset the multiplier to 1. Another pair's fill does not reset it.
        # Rebalance (counts_as_edge false) never reaches this block.
        # rules1012 2026-10-11: max 2x toggle. 1x -> 2x; 2x -> back to 1x on the next
        # edge fill on this pair (incl. reverses / any-profit). Other pairs never affect it.
        new = toggle_pair_mult(cur)
        esc["multipliers"][pair] = new
        esc["note"] = (
            f"{pair} at {new}x after {side_str}; "
            f"max 2x toggle (1x->2x, 2x->1x); other pairs unchanged"
        )
        esc.setdefault("last_edge_fill_side_by_pair", {})[pair] = side_str
        mults.clear()
        mults.update(esc["multipliers"])
        esc["last_fill_pair"] = pair
        esc["last_fill_tx"] = sig
        st = cb.setdefault("same_pair_streak_cap", {})
        if st.get("enabled", True):
            if st.get("pair") == pair:
                st["count"] = int(st.get("count") or 0) + 1
            else:
                st["pair"] = pair
                st["count"] = 1
            st["last_edge_fill_tx"] = sig
            st["last_edge_fill_side"] = side_str
            st["last_edge_fill_time_pt"] = when
    # Edge fills, including a reverse of an rsi_rebalance or band_rebalance fill,
    # are kind "edge" so the next reverse is the normal path (not flat 5%, not 2x).
    # Rebalance writers must pass fill_kind rsi_rebalance, band_rebalance, or
    # underweight_double and counts_as_edge=False. rsi_rebalance is NOT remapped
    # to band_rebalance. friday_rebalance and friday_band_rebalance still are.
    # Kind does not change multipliers or the same-pair streak; counts_as_edge still owns that.
    kind = friday_rsi.kind_from_writer(fill_kind, counts_as_edge, venue)
    last_rec = friday_rsi.make_fill_record(
        side_str, fill_rate, sig, when, kind=kind,
        sold_amount=sell_amt, bought_amount=buy_amt, reversed_flag=False,
    )
    friday_rsi.append_pair_fill(
        cb.setdefault("reverse_last_trade", {}),
        pair,
        last_rec,
        reverse_of_tx=reverse_of_tx,
    )
    cb["updated_pt"] = when
    return new_rate

def append_fill_logs(when, side_str, sold, sell_amt, bought, buy_amt, fill_rate, pair, sig, bals_after, total_after, gas_sol, note, rev=None):
    import csv
    rate_unit = pairs[pair]["rate_unit"]
    url = f"https://solscan.io/tx/{sig}"
    row = {
        "time_pt": when, "side": side_str, "sold_asset": sold, "sold_amount": sell_amt,
        "bought_asset": bought, "bought_amount": buy_amt, "rate": fill_rate, "rate_unit": rate_unit,
        "tx": sig, "tx_url": url,
        "sol_after": bals_after.get("SOL"), "usdc_after": bals_after.get("USDC"),
        "cbbtc_after": bals_after.get("cbBTC"), "weth_after": bals_after.get("WETH"),
        "total_usd_after": total_after, "notes": note,
    }
    if rev:  # revflag 2026-10-11: record-only {"rev": 1, "rev_of": tx} on reversal fills
        row.update(rev)
    with open(_DH.HOME_STR + "/trades.jsonl", "a", encoding="utf-8", newline="") as f:
        f.write(json.dumps(row) + "\n")
    with open(_DH.trades_csv_path(), "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([when, side_str, sold, sell_amt, bought, buy_amt, fill_rate, rate_unit, sig, url,
                    bals_after.get("SOL"), bals_after.get("USDC"), bals_after.get("cbBTC"), bals_after.get("WETH"),
                    total_after, note])
    # fees-vs-edge tracking retired 2026-10-05: nothing else is written per fill.
    # Per-pair P&L: fold this fill in at this check's USD prices with its gas.
    try:
        if PAIR_PNL_STATE is not None:
            _pp_pair = PP.apply_fill(PAIR_PNL_STATE, row, pairs, lambda sym, _t: prices.get(sym), gas_sol=gas_sol)
            if _pp_pair:
                PP.ensure_pairs(PAIR_PNL_STATE, pairs)
                PAIR_PNL_STATE["updated_pt"] = when
                PP.save(PAIR_PNL_PATH, PAIR_PNL_STATE)
                refresh_pair_tilts()
                _e = PAIR_PNL_STATE["pairs"][_pp_pair]
                print(f"PAIR_PNL_FILL {_pp_pair} pnl_pct={_e.get('pnl_pct')} net_usd={_e.get('realized_net_usd')}", flush=True)
    except Exception as _pp_err:
        print(f"PAIR_PNL_FILL_ERR {type(_pp_err).__name__}", flush=True)
    try:
        ROUTE_HIT.record_fill(side_str, when)
        ROUTE_HIT.save()
    except Exception as _rh_err:
        print(f"ROUTE_HIT_FILL_ERR {type(_rh_err).__name__}", flush=True)

def materialize_venue(row, sold, bought):
    """Build the execution payload for this quote only. Does not submit."""
    venue = row["venue"]
    if venue == "jupiter_manual":
        tx_b64, err = VQ.build_jupiter_manual_tx(row.get("payload"))
        if err or not tx_b64:
            return None, err or "jupiter_manual_no_tx"
        return {"tx_b64": tx_b64, "request_id": None, "txs": None, "ixs": None}, None
    if venue == "orca":
        tx_b64, err = VQ.build_orca_tx(row.get("payload"))
        if err or not tx_b64:
            print(f"ORCA_BUILD_SKIP {err or 'orca_no_tx'}", flush=True)
            return None, err or "orca_no_tx"
        return {"tx_b64": tx_b64, "request_id": None, "txs": None, "ixs": None}, None
    if venue == "jupiter_ultra":
        # Not requested. Only a quote that came back labeled Ultra and still
        # cleared after its platform fee was removed can reach here.
        if not row.get("tx_b64") or not row.get("request_id"):
            return None, "jupiter_ultra_no_tx"
        return {"tx_b64": row["tx_b64"], "request_id": row["request_id"], "txs": None, "ixs": None}, None
    if venue == "raydium":
        txs, err = VQ.build_raydium_tx(row["swap_response"], sold, bought)
        if err:
            return None, err
        return {"tx_b64": txs[-1], "txs": txs, "request_id": None, "ixs": None}, None
    return None, f"unknown_venue:{venue}"

def send_signed_b64(b64):
    """Helius only. Does not fall through and does not resubmit these bytes.

    Prefer submit_unsigned_txs, which replaces the blockhash first. This
    remains for a transaction that is already signed for Helius.
    """
    try:
        resp = solana_rpc.rpc_send("sendTransaction", [b64, solana_rpc.send_config()], "helius")
    except RuntimeError as exc:
        return None, solana_rpc.redact(str(exc))[:1500]
    if solana_rpc.is_already_processed(resp):
        sig = solana_rpc.signature_from_signed_b64(b64)
        return (sig, None) if sig else (None, solana_rpc.error_line("helius", "sendTransaction", resp))
    if not isinstance(resp, dict) or "result" not in resp:
        if isinstance(resp, dict) and resp.get("error"):
            return None, solana_rpc.error_line("helius", "sendTransaction", resp)
        return None, "missing_result"
    sig = resp.get("result")
    if not sig:
        return None, "no_signature"
    status, detail = solana_rpc.confirm_signature(sig)
    if status == "ok":
        return sig, None
    if status == "err":
        return None, solana_rpc.redact(f"chain_err:{sig}:{detail}")[:1500]
    return None, f"unconfirmed:{sig}"


def submit_unsigned_txs(txs, kp, requote_fn=None, prior_sends=None):
    """Confirmed Helius blockhash, sign, sendTransaction on Helius.

    On Helius failure, requote_fn must return a new unsigned tx only if the
    trade rules still pass. That tx is signed with a fresh Alchemy blockhash
    and sent once on Alchemy. The same signed bytes are not sent again.
    Public mainnet is not a send endpoint. Returns (sig, err, uncertain).
    prior_sends: one list per candidate per check (Jupiter send, its Alchemy
    re-quote, and any Raydium fallback share it). Before any re-quote or
    re-send, every signature in it is status-checked; a landed one is the
    fill and nothing more is sent (2026-10-08 duplicate-send fix).
    """
    def sign_fn(raw, blockhash):
        patched = VQ.replace_recent_blockhash(raw, blockhash)
        return sign_ultra_tx(patched, kp)

    return solana_rpc.submit_bundle(
        list(txs),
        sign_fn,
        requote_fn,
        accept_unconfirmed=False,
        prior_sends=prior_sends,
    )

def fresh_executable(c, allow_reuse=False, fresh_why=None):
    """Fill from this route only. Re-quote it, unless the quote-reuse fast path applies.

    Both venues of this route are still quoted together, so best-net and the
    bad-condition check still apply to the winner. Other pairs are not quoted.
    Balances are the check snapshot. This does not RPC the wallet.

    allow_reuse=True only from execute_fills for the first send attempt of the
    check. Then the scan quote is used (no re-quote) when it is <= max_age_sec
    old, the same size, clears eff% + leftover floor by extra_margin_pct, passes
    bad-condition / fade / concentration, and builds. Anything else falls through
    to the normal fresh re-quote below (QUOTE_FRESH <route> only reason=...).
    The Alchemy re-quote (_requote_unsigned) never reuses.
    """
    sold, bought, sell_amt, _buy_old, pair, side = cand_legs(c)
    if held_outside_book(sold) or held_outside_book(bought):
        return None, "held_not_edge_inventory"
    # Profit-sale cut disabled; this re-check is a no-op while enabled is false.
    if not c.get("profit_sale_cut"):
        sell_amt, cut_now, pnl_now = apply_profit_sale_cut(sold, sell_amt)
        if cut_now:
            c["profit_sale_cut"] = True
            c["sold_unrealized_pnl_pct"] = None if pnl_now is None else round(pnl_now, 4)
            print(f"PROFIT_CUT_FRESH {c.get('route')} sold={sold} pnl={pnl_now} sell_amt={sell_amt}", flush=True)
            px_cut = float(prices.get(sold) or 0)
            if px_cut > 0 and sell_amt * px_cut < 0.05:
                return None, "profit_cut_dust"
    _caps = exec_caps(c, pair)
    px = float(prices.get(sold) or 0)
    if px <= 0 or sell_amt * px - 1e-9 > max_trade_usd_cap(_caps[0]):
        return None, "over_max_usd"
    bal = float(bals.get(sold) or 0)
    if bal <= 0 or sell_amt > bal * float(_caps[1]) + 1e-9:
        return None, "over_max_fraction"
    if sold == "SOL" and bal - sell_amt < float(size_lim["min_sol_fee_buffer"]) - 1e-12:
        return None, "sol_buffer"
    basis = c["fill_rate"] if c.get("kind") == "reverse" else pairs[pair]["rate"]
    fresh_reason = None if allow_reuse else (fresh_why or "requote")
    if allow_reuse:
        rec = _QUOTE_BOOK.get(c.get("route"))
        ok_r, fresh_reason, age_r = EP.quote_reuse_precheck(
            QR_CFG, 0, SENDS_ATTEMPTED[0], rec, sell_amt, time.monotonic())
        if ok_r:
            # Same venue set as the fresh re-quote (Jupiter + Raydium; no Orca row).
            rows_r = [r for r in (rec.get("rows") or []) if (r or {}).get("venue") != "orca"]
            info_r, err_r = _route_from_rows(
                c, rows_r, [], sold, bought, sell_amt, pair, side, basis,
                reuse={"age": age_r})
            if err_r is None and info_r:
                if JRF.skip_jupiter_when_prefer_raydium(
                    RAY_PREF.prefer_raydium(pair, datetime.now(PT)), info_r.get("src")
                ):
                    fresh_reason = "raydium_first_no_jupiter_reuse"
                    print(
                        f"QUOTE_REUSE_SKIP_JUPITER {c.get('route')} pair={pair} "
                        f"(prefer_raydium; re-quote for Raydium)",
                        flush=True,
                    )
                else:
                    print(
                        f"QUOTE_REUSE {c.get('route')} venue={info_r.get('src')} age={age_r:.2f}s "
                        f"margin={info_r.get('quote_reuse_margin_pp')}pp "
                        f"(need>={QR_CFG.get('extra_margin_pct')}pp over eff%+floor)",
                        flush=True,
                    )
                    return info_r, None
            elif err_r is not None or not info_r:
                fresh_reason = (err_r or "reuse_failed").replace("reuse:", "", 1)
        elif age_r is not None:
            fresh_reason = f"{fresh_reason}(age={age_r:.2f}s)"
    print(f"QUOTE_FRESH {c.get('route')} only reason={fresh_reason}", flush=True)
    rows, notes = fetch_route_quotes(sold, bought, to_base(sell_amt, sold))
    remember_route_quotes(c.get("route"), sell_amt, rows)
    return _route_from_rows(c, rows, notes, sold, bought, sell_amt, pair, side, basis)


def _route_from_rows(c, rows, notes, sold, bought, sell_amt, pair, side, basis, reuse=None):
    """Score venue rows for one route, pick the winner, gate it, build it.

    reuse=None: the normal fresh path (logs, bad_blocks, and errors unchanged).
    reuse={"age": s}: the scan quote. No side effects on refusal: any block,
    thin margin, or build failure returns (None, "reuse:<reason>") so the caller
    re-quotes fresh, which then applies and logs the normal gates.
    """
    record_fee = True
    scored = []
    for row in rows:
        row = VQ.adjust_for_ultra_fee(row)
        if not row.get("ok"):
            continue
        buy_amt_v = from_base(row["out_amount"], bought)
        sc = score_leg(
            pair, sold, bought, side, sell_amt, buy_amt_v, float(row["gas_sol"]), basis,
            reverse_of_rebalance=bool(c.get("reverse_of_rebalance")),
            reverse_of_rsi=bool(c.get("reverse_of_rsi")),
        )
        kh_hurdle(sc, c.get("kind") == "reverse")
        sc["venue"] = row["venue"]
        sc["row"] = row
        maybe_any_profit_bypass(sc, route=c.get("route"))
        scored.append(sc)
    if not scored:
        if reuse is not None:
            return None, "reuse:no_ok_venue"
        return None, "all_venues_failed: " + "; ".join(notes)
    # Raydium first on a pair whose Jupiter sends keep failing "Program failed
    # to complete" (jupiter-ix6-state.json). Otherwise best gap_net, Jupiter on a tie.
    prefer_ray = RAY_PREF.prefer_raydium(pair, datetime.now(PT))
    # Jupiter quote 429 this attempt: prefer Raydium (not only after 2 ix-6 fails).
    jup_429 = any(
        (not (r or {}).get("ok"))
        and str((r or {}).get("venue") or "").startswith("jupiter")
        and "rate_limit_429" in str((r or {}).get("note") or "")
        for r in (rows or [])
    )
    if jup_429 and not prefer_ray:
        prefer_ray = True
        time.sleep(max(0.5, float(QUOTE_GAP) * 1.5))
        print(
            f"JUPITER_429_PREFER_RAYDIUM {c.get('route')} pair={pair} (backoff+raydium)",
            flush=True,
        )
    if prefer_ray and reuse is None:
        _prow = (RAY_PREF.data.get("pairs") or {}).get(pair) or {}
        print(
            f"RAYDIUM_FIRST {c.get('route')} pair={pair} "
            f"jupiter_program_fails={_prow.get('consecutive_jupiter_program_fails')} "
            f"last_fail={_prow.get('last_jupiter_program_fail_pt')}",
            flush=True,
        )
    ranked = EP.order_venues(scored, prefer_ray, VENUE_RANK)
    build_notes = list(notes)
    built = None
    chosen = None
    post_pct = None
    blocked_concentration = False
    blocked_bad = None
    refused = None
    fade_blocked = False
    reuse_margin = None
    for i, sc in enumerate(ranked):
        # Raydium-first: never send Jupiter (incl. reverses). Fall through only
        # to Raydium/Orca; if Raydium does not clear/build, miss — do not open
        # with Jupiter and hit ix-6 again.
        if prefer_ray and JRF.skip_jupiter_when_prefer_raydium(True, sc.get("venue")):
            print(
                f"RAYDIUM_FIRST_SKIP_JUPITER {c.get('route')} pair={pair} "
                f"venue={sc.get('venue')} gap_net={sc.get('gap_net')}",
                flush=True,
            )
            build_notes.append(f"{sc.get('venue')}_skipped:raydium_first")
            continue
        ok_i, post_i = concentration_ok(sold, bought, sell_amt, sc["buy_amt"])
        # Best net that would trade but fails the 90% cap blocks the route.
        if i == 0 and sc["clears"] and sc["clears_net"] and not ok_i:
            if reuse is not None:
                return None, "reuse:concentration"
            blocked_concentration = True
            post_pct = post_i
            break
        if not (sc["clears"] and sc["clears_net"] and ok_i):
            continue
        # Winning quote only. A bad condition skips this route, not the other venue.
        impact = VQ.quote_price_impact_pct(sc["row"])
        micro = VQ.quote_priority_fee_micro(sc["row"])
        reasons = bad_condition_reasons(BAD_CFG, pairs.get(pair) or {}, impact, sc["row"])
        if reasons and reuse is not None:
            return None, "reuse:bad_condition:" + ";".join(reasons)
        if reasons:
            blocked_bad = reasons
            refused = sc
            post_pct = post_i
            bad_blocks.append({
                "pair": pair,
                "route": c.get("route"),
                "venue": sc.get("venue"),
                "reasons": reasons,
                "when": "fresh_quote",
            })
            print(
                f"BAD_CONDITION_SKIP {c.get('route')} pair={pair} venue={sc.get('venue')} "
                f"{';'.join(reasons)}",
                flush=True,
            )
            break
        # About to accept this sell. Fade is not waived for a memecoin tag.
        if fade_trend.current().blocks(sold):
            if reuse is not None:
                return None, "reuse:fade_trend"
            fade_blocked = True
            refused = sc
            post_pct = post_i
            print(
                f"FADE_TREND_SKIP {c.get('route')} pair={pair} sold={sold} venue={sc.get('venue')}",
                flush=True,
            )
            break
        if reuse is not None:
            # Extra safety margin over eff% + leftover floor, fee-inclusive.
            ok_m, reuse_margin = EP.quote_reuse_margin(QR_CFG, sc)
            if not ok_m:
                return None, (
                    "reuse:thin_margin(" + ("na" if reuse_margin is None else f"{reuse_margin:.3f}")
                    + f"pp<{QR_CFG.get('extra_margin_pct')}pp)"
                )
        mat, merr = materialize_venue(sc["row"], sold, bought)
        if reuse is not None and (merr or not mat or not (mat.get("txs") or mat.get("tx_b64"))):
            print(f"VENUE_SKIP {sc['venue']} {merr or 'no_tx'} (scan quote)", flush=True)
            return None, "reuse:build_failed"
        if merr:
            build_notes.append(f"{sc['venue']}_build: {merr}")
            print(f"VENUE_SKIP {sc['venue']} {merr}", flush=True)
            continue
        built = mat
        chosen = sc
        post_pct = post_i
        break
    if reuse is not None and chosen is None:
        return None, "reuse:no_clearing_venue"
    if reuse is not None and prefer_ray:
        _prow = (RAY_PREF.data.get("pairs") or {}).get(pair) or {}
        print(
            f"RAYDIUM_FIRST {c.get('route')} pair={pair} "
            f"jupiter_program_fails={_prow.get('consecutive_jupiter_program_fails')} "
            f"last_fail={_prow.get('last_jupiter_program_fail_pt')}",
            flush=True,
        )
    top = chosen or refused or ranked[0]
    info = {
        "route": c.get("route"), "pair": pair, "side": side, "sold": sold, "bought": bought,
        "sell_amt": sell_amt, "buy_amt": top["buy_amt"], "exec_rate": top["exec_rate"], "basis": basis,
        "eff_pct": top["eff"], "edge_pct": round(top["edge"], 3), "gap_net_pct": round(top["gap_net"], 3),
        "clears_net": bool(chosen), "gas_sol": top["gas_sol"],
        "post_max_token_pct": None if post_pct is None else round(post_pct * 100, 3),
        "request_id": None if not built else built.get("request_id"),
        "tx_b64": None if not built else built.get("tx_b64"),
        "txs": None if not built else built.get("txs"),
        "ixs": None if not built else built.get("ixs"),
        "src": top["venue"],
        "raydium_first": bool(prefer_ray and chosen is not None and chosen.get("venue") == "raydium"),
        "venue_notes": build_notes,
        "bad_condition_skip": bool(blocked_bad),
        "bad_condition_reasons": blocked_bad or [],
        "fade_trend_skip": bool(fade_blocked),
        "fade_reason": "fade_trend" if fade_blocked else None,
        "price_impact_pct": None if VQ.quote_price_impact_pct(top.get("row")) is None else round(VQ.quote_price_impact_pct(top.get("row")), 6),
        "priority_fee_micro_lamports_per_cu": None if VQ.quote_priority_fee_micro(top.get("row")) is None else round(VQ.quote_priority_fee_micro(top.get("row")), 3),
        "quote_reused": reuse is not None,
        "quote_age_sec": None if reuse is None else round(float(reuse.get("age") or 0.0), 3),
        "quote_reuse_margin_pp": None if reuse_margin is None else round(reuse_margin, 3),
    }
    if blocked_concentration:
        return info, "concentration"
    if blocked_bad:
        # Not a fill. Caller tries another pair. Do not sign this route.
        info["clears_net"] = False
        return info, "bad_condition:" + ";".join(blocked_bad)
    if fade_blocked:
        # Not a fill. Do not sign. Streak, multipliers, and cost basis stay put.
        info["clears_net"] = False
        info["fade_trend_skip"] = True
        info["fade_reason"] = "fade_trend"
        return info, "fade_trend"
    if chosen is None:
        if any(s["clears"] and s["clears_net"] for s in ranked):
            return info, "venues_down: " + "; ".join(build_notes)
        return info, "fresh_quote_miss"
    return info, None



def raydium_only_executable(c, sent, force_path=False):
    """One fresh Raydium quote for the same pair, direction, and size. No send.

    Same gates as a fill: size caps, required percent and leftover floor,
    concentration, price-impact cap, priority-fee spike, and no-fade.
    Slippage is the Raydium default (30 bps). Does not quote Jupiter.

    force_path: after a Jupiter ix-6 abort, if Raydium does not clear but its
    gap_net is at least the failed Jupiter attempt's gap_net, still build
    (rescue only; primary edge % selection unchanged).
    """
    sold = sent["sold"]
    bought = sent["bought"]
    sell_amt = float(sent["sell_amt"])
    pair = sent["pair"]
    side = sent["side"]
    route = sent.get("route") or c.get("route")
    if held_outside_book(sold) or held_outside_book(bought):
        return None, "held_not_edge_inventory"
    _caps = exec_caps(c, pair)
    px = float(prices.get(sold) or 0)
    if px <= 0 or sell_amt * px - 1e-9 > max_trade_usd_cap(_caps[0]):
        return None, "over_max_usd"
    bal = float(bals.get(sold) or 0)
    if bal <= 0 or sell_amt > bal * float(_caps[1]) + 1e-9:
        return None, "over_max_fraction"
    if sold == "SOL" and bal - sell_amt < float(size_lim["min_sol_fee_buffer"]) - 1e-12:
        return None, "sol_buffer"
    basis = c["fill_rate"] if c.get("kind") == "reverse" else pairs[pair]["rate"]
    print(f"QUOTE_RAYDIUM_FALLBACK {route} only", flush=True)
    row = VQ.quote_raydium(sold, bought, to_base(sell_amt, sold))
    row = VQ.adjust_for_ultra_fee(row)
    if not isinstance(row, dict) or not row.get("ok") or row.get("venue") != "raydium":
        return None, "raydium_quote_failed"
    buy_amt_v = from_base(row["out_amount"], bought)
    sc = score_leg(
        pair, sold, bought, side, sell_amt, buy_amt_v, float(row["gas_sol"]), basis,
        reverse_of_rebalance=bool(c.get("reverse_of_rebalance")),
        reverse_of_rsi=bool(c.get("reverse_of_rsi")),
    )
    kh_hurdle(sc, c.get("kind") == "reverse")
    maybe_any_profit_bypass(sc, route=route)
    ok_i, post_i = concentration_ok(sold, bought, sell_amt, buy_amt_v)
    impact = VQ.quote_price_impact_pct(row)
    info = {
        "route": route, "pair": pair, "side": side, "sold": sold, "bought": bought,
        "sell_amt": sell_amt, "buy_amt": buy_amt_v, "exec_rate": sc["exec_rate"], "basis": basis,
        "eff_pct": sc["eff"], "edge_pct": round(sc["edge"], 3), "gap_net_pct": round(sc["gap_net"], 3),
        "clears_net": False, "gas_sol": sc["gas_sol"],
        "post_max_token_pct": None if post_i is None else round(post_i * 100, 3),
        "request_id": None, "tx_b64": None, "txs": None, "ixs": None,
        "src": "raydium",
        "raydium_after_jupiter_program_fail": True,
        "price_impact_pct": None if impact is None else round(impact, 6),
        "force_path": bool(force_path),
    }
    clears_net = bool(sc["clears"] and sc["clears_net"])
    if not JRF.force_raydium_ok(clears_net, force_path, sc["gap_net"], sent.get("gap_net_pct")):
        return info, "fresh_quote_miss"
    if force_path and not clears_net:
        print(
            f"RAYDIUM_FORCE_GAP_BEST {route} ray_gap={sc['gap_net']:.3f} "
            f"jup_gap={sent.get('gap_net_pct')} (ix6 rescue; edge rules unchanged)",
            flush=True,
        )
    if not ok_i:
        return info, "concentration"
    reasons = bad_condition_reasons(BAD_CFG, pairs.get(pair) or {}, impact, row)
    if reasons:
        info["bad_condition_skip"] = True
        info["bad_condition_reasons"] = reasons
        bad_blocks.append({
            "pair": pair,
            "route": route,
            "venue": "raydium",
            "reasons": reasons,
            "when": "raydium_fallback",
        })
        print(
            f"BAD_CONDITION_SKIP {route} pair={pair} venue=raydium {';'.join(reasons)}",
            flush=True,
        )
        return info, "bad_condition:" + ";".join(reasons)
    if fade_trend.current().blocks(sold):
        info["fade_trend_skip"] = True
        info["fade_reason"] = "fade_trend"
        print(f"FADE_TREND_SKIP {route} pair={pair} sold={sold} venue=raydium", flush=True)
        return info, "fade_trend"
    mat, merr = materialize_venue(row, sold, bought)
    if merr or not mat or not (mat.get("txs") or mat.get("tx_b64")):
        print(f"VENUE_SKIP raydium {merr}", flush=True)
        return info, merr or "raydium_no_tx"
    info["clears_net"] = True
    info["request_id"] = mat.get("request_id")
    info["tx_b64"] = mat.get("tx_b64")
    info["txs"] = mat.get("txs")
    info["ixs"] = mat.get("ixs")
    return info, None


def _prepare_raydium_fallback(c, sent):
    """Quote and gate Raydium. None means do not send and do not record a failure.

    Always same-size as the failed Jupiter send. After ix-6, force_path allows
    Raydium when gap_net is best vs that Jupiter attempt even if leftover/eff
    miss slightly (rescue only).
    """
    info_r, err_r = raydium_only_executable(c, sent, force_path=True)
    if err_r or not info_r:
        print(f"RAYDIUM_FALLBACK_SKIP {sent.get('route')} {err_r}", flush=True)
        return None
    # force_path may set clears_net False in scoring but still build a tx.
    if not info_r.get("tx_b64") and not info_r.get("txs"):
        print(f"RAYDIUM_FALLBACK_SKIP {sent.get('route')} {err_r or 'no_tx'}", flush=True)
        return None
    if not info_r.get("clears_net") and not info_r.get("force_path"):
        print(f"RAYDIUM_FALLBACK_SKIP {sent.get('route')} fresh_quote_miss", flush=True)
        return None
    # Mark sendable for force rescue.
    if info_r.get("tx_b64") or info_r.get("txs"):
        info_r["clears_net"] = True
    return info_r


# Fresh re-quote behind the Alchemy backup send, per route (2026-10-08). When
# the backup send is the fill, the books use this quote (what was executed),
# not the first quote from up to ~2 minutes earlier.
_LAST_REQUOTE = {}


def _requote_unsigned(c):
    """Re-quote this one route and return new unsigned transactions. No send."""
    _QUOTE_BOOK.pop(c.get("route"), None)
    info2, err2 = fresh_executable(c, allow_reuse=False, fresh_why="alchemy_requote")
    if err2 or not info2 or not info2.get("clears_net") or not info2.get("tx_b64"):
        return None
    _LAST_REQUOTE[c.get("route")] = info2
    return info2.get("txs") or [info2["tx_b64"]]


def _executed_info(info, venue, sig, cand_sends):
    """Book the re-quote when the landed signature is the Alchemy backup send."""
    rq = _LAST_REQUOTE.get(info.get("route"))
    if not rq or not sig:
        return info, venue
    if any(isinstance(e, dict) and e.get("host") == "alchemy" and e.get("state") == "ok"
           and e.get("sig") == sig for e in (cand_sends or [])):
        print(f"BACKUP_FILL_BOOKED_FROM_REQUOTE {info.get('route')} venue={rq.get('src') or venue}", flush=True)
        return rq, (rq.get("src") or venue)
    return info, venue


def _record_jupiter_send(pair, sig, err_s):
    """Persist the per-pair Jupiter program-fail streak. Never blocks a fill."""
    try:
        row = RAY_PREF.record_jupiter_result(
            pair, err_s, datetime.now(PT).replace(microsecond=0).isoformat(), signature=sig,
        )
        RAY_PREF.save()
        if err_s and JRF.program_failed_to_complete(err_s):
            print(
                f"JUPITER_PROGRAM_FAIL_TRACK pair={pair} consecutive="
                f"{row.get('consecutive_jupiter_program_fails')} raydium_first_next="
                f"{RAY_PREF.prefer_raydium(pair, datetime.now(PT))}",
                flush=True,
            )
    except Exception as st_err:
        print(f"RAY_PREF_STATE_ERR {type(st_err).__name__}", flush=True)

def execute_fills(executable):
    """Sign and submit at most one swap per pair. Several pairs may fill in one check.

    executable is already one-best-per-pair. Fill them sequentially (best net first).
    After each successful fill, refresh bals so the next does not oversell.
    Do not fill two routes on the same pair. No Phantom.
    """
    global bals
    kp = None
    tried = []
    send_fails = []
    fills = []
    filled_pairs = set()
    fresh_attempts = 0
    for c in executable:
        pair = c.get("pair")
        if solana_rpc.long_wait_used():
            # 2026-10-08: one dropped-send wait per check, then no more sends.
            print(f"LONG_WAIT_STOP_FURTHER_PAIRS next={c.get('route')}", flush=True)
            break
        if pair in filled_pairs:
            print(f"PAIR_ALREADY_FILLED_SKIP {c.get('route')} pair={pair}", flush=True)
            continue
        sold_c, bought_c, _sa, _ba, _pair, _side = cand_legs(c)
        if held_outside_book(sold_c) or held_outside_book(bought_c):
            print(f"HELD_NOT_EDGE {c.get('route')}", flush=True)
            continue
        if c.get("kind") != "reverse" and friday_rsi.basis_unwinds_rsi(
            rl.get(c.get("pair")), sold_c, bought_c
        ):
            print(
                f"RSI_UNWIND_BLOCK {c.get('route')} pair={c.get('pair')} sold={sold_c} bought={bought_c}",
                flush=True,
            )
            continue
        if same_pair_streak_blocks(c.get("pair")):
            tried.append({
                "route": c.get("route"), "error": "same_pair_streak_cap",
                "pair": c.get("pair"),
                "gap_net_pct": c.get("gap_net_pct"),
                "eff_pct": c.get("eff_pct"), "edge_pct": c.get("edge_pct"),
            })
            print(f"STREAK_SKIP {c.get('route')} pair={c.get('pair')}", flush=True)
            continue
        # Alchemy cap key: one directed route attempt (Jupiter + its Raydium fallback share it).
        ALCHEMY.set_route(c.get("route"))
        # Quote-reuse fast path: first send attempt of this check only. Every
        # later candidate re-quotes (balances or market may have moved).
        first_send = fresh_attempts == 0 and SENDS_ATTEMPTED[0] == 0
        if not QR_CFG.get("first_send_only", True):
            first_send = True
        fresh_attempts += 1
        info, err = fresh_executable(
            c,
            allow_reuse=bool(QR_CFG.get("enabled") and first_send),
            fresh_why=("fast_path_disabled" if not QR_CFG.get("enabled") else "not_first_send"),
        )
        if err or not info or not info.get("clears_net"):
            tried.append({"route": c.get("route"), "error": err, "gap_net_pct": None if not info else info.get("gap_net_pct"),
                          "eff_pct": None if not info else info.get("eff_pct"), "edge_pct": None if not info else info.get("edge_pct"),
                          "pair": pair})
            print(f"FRESH_SKIP {c.get('route')} {err}", flush=True)
            time.sleep(QUOTE_GAP)
            continue
        if kp is None:
            kp = load_trading_keypair()
        venue = info["src"]
        if not FILLED_THIS_CHECK[0] and _JL.held():
            _job_skip_exit("started mid-check; no send")
        SENDS_ATTEMPTED[0] += 1
        # Every broadcast for this candidate this check (duplicate-send guard).
        cand_sends = []
        prefer_ray_now = RAY_PREF.prefer_raydium(pair, datetime.now(PT))
        if venue in ("jupiter_manual", "orca"):
            # When Raydium-first is already on for this pair, do NOT Alchemy
            # re-quote Jupiter again after a Helius fail. Rebuild as Raydium-only
            # (or skip Alchemy). Ix-6 then still hits the Raydium fallback below
            # if the Raydium rebuild did not clear / send.
            _ray_switch = {"info": None}

            def _requote_no_jupiter_again(_c=c, _sent=info, _venue=venue, _pr=prefer_ray_now, _pair=pair):
                if JRF.alchemy_rebuild_mode(_pr, _venue) == "raydium_only":
                    print(
                        f"RAYDIUM_FIRST_NO_JUPITER_REQUOTE {_sent.get('route')} pair={_sent.get('pair')} "
                        f"(Alchemy rebuild = Raydium-only)",
                        flush=True,
                    )
                    info2, err2 = raydium_only_executable(_c, _sent, force_path=True)
                    if err2 or not info2 or not (info2.get("tx_b64") or info2.get("txs")):
                        print(
                            f"RAYDIUM_FIRST_REQUOTE_SKIP {_sent.get('route')} {err2}",
                            flush=True,
                        )
                        return None
                    if info2.get("tx_b64") or info2.get("txs"):
                        info2["clears_net"] = True
                    _ray_switch["info"] = info2
                    return info2.get("txs") or ([info2["tx_b64"]] if info2.get("tx_b64") else None)
                # Jupiter Alchemy rebuild: bump CU / priority after prior ix-6 fails.
                _prow = (RAY_PREF.data.get("pairs") or {}).get(_pair) or {}
                _nfail = int(_prow.get("consecutive_jupiter_program_fails") or 0)
                # Helius just failed; treat as at least one fail for this rebuild.
                if JRF.should_bump_jupiter_budget(_pr, _venue, max(_nfail, 1)):
                    cu_f, micro_f = JRF.ix6_budget_factors()
                    VQ.set_jupiter_budget_bump(cu_f, micro_f)
                    print(
                        f"JUPITER_IX6_BUDGET_BUMP {_sent.get('route')} pair={_pair} "
                        f"cu_x={cu_f} micro_x={micro_f}",
                        flush=True,
                    )
                try:
                    return _requote_unsigned(_c)
                finally:
                    VQ.clear_jupiter_budget_bump()

            _LAST_REQUOTE.pop(c.get("route"), None)
            sig, err_s, uncertain = submit_unsigned_txs(
                [info["tx_b64"]], kp, _requote_no_jupiter_again, prior_sends=cand_sends,
            )
            if _ray_switch["info"] is not None and sig and not err_s:
                info = _ray_switch["info"]
                venue = "raydium"
                print("EXEC", venue, sig, err_s, "(raydium_first alchemy rebuild)", flush=True)
            else:
                print("EXEC", venue, sig, err_s, flush=True)
            if venue == "jupiter_manual":
                _record_jupiter_send(pair, sig, err_s)
            if _ray_switch["info"] is None and sig and not err_s:
                info, venue = _executed_info(info, venue, sig, cand_sends)
            if err_s or not sig:
                fail_j = {
                    "route": info["route"],
                    "error": err_s or ("orca_send_failed" if venue == "orca" else "jupiter_manual_send_failed"),
                    "pair": pair,
                    "venue": venue,
                }
                tried.append(fail_j)
                # Uncertain (submitted, not confirmed): stop further pairs.
                # A preflight that never landed stops this pair only.
                # Raydium is not started while a Jupiter send may still land.
                if JRF.program_failed_to_complete(err_s):
                    send_fails.append(fail_j)
                if uncertain:
                    return {
                        "signature": None, "submitted": True,
                        "error": err_s or ("orca_send_failed" if venue == "orca" else "jupiter_manual_send_failed"),
                        "route": info["route"], "venue": venue, "tried": tried,
                        "send_fails": send_fails,
                        "fills": fills, "pairs_filled": list(filled_pairs),
                        "balances_after": bals,
                    }
                # Alchemy retry already finished inside submit_unsigned_txs.
                # Only "Program failed to complete" may re-quote Raydium once.
                sent_jup = info

                def _send_ray(payload, _kp=kp, _c=c, _sent=sent_jup, _prior=cand_sends):
                    txs = payload.get("txs") or [payload["tx_b64"]]

                    def _requote_ray():
                        info2, err2 = raydium_only_executable(_c, _sent)
                        if err2 or not info2 or not info2.get("clears_net"):
                            return None
                        return info2.get("txs") or ([info2["tx_b64"]] if info2.get("tx_b64") else None)

                    return submit_unsigned_txs(txs, _kp, _requote_ray, prior_sends=_prior)

                fb = JRF.consider_raydium(
                    err_s,
                    venue,
                    lambda _c=c, _sent=sent_jup: _prepare_raydium_fallback(_c, _sent),
                    _send_ray,
                )
                if fb.get("action") != "sent":
                    continue
                sig = fb.get("signature")
                err_s = fb.get("error")
                uncertain = bool(fb.get("uncertain"))
                info = fb.get("info") or info
                venue = "raydium"
                print("EXEC", venue, sig, err_s, flush=True)
                if err_s or not sig:
                    fail_r = {
                        "route": info.get("route"),
                        "error": err_s or "raydium_send_failed",
                        "pair": pair,
                        "venue": venue,
                    }
                    tried.append(fail_r)
                    send_fails.append(fail_r)
                    if uncertain:
                        return {
                            "signature": None, "submitted": True,
                            "error": err_s or "raydium_send_failed",
                            "route": info.get("route"), "venue": venue, "tried": tried,
                            "send_fails": send_fails,
                            "fills": fills, "pairs_filled": list(filled_pairs),
                            "balances_after": bals,
                        }
                    continue
        elif venue == "jupiter_ultra":
            signed_b64 = sign_ultra_tx(info["tx_b64"], kp)
            resp = post_json("https://api.jup.ag/ultra/v1/execute", {
                "signedTransaction": signed_b64,
                "requestId": info["request_id"],
            })
            status = resp.get("status") if isinstance(resp, dict) else None
            sig = resp.get("signature") if isinstance(resp, dict) else None
            print("EXEC", venue, status, sig, resp.get("code") if isinstance(resp, dict) else None, flush=True)
            if status != "Success" or not sig:
                err_s = ""
                if isinstance(resp, dict):
                    err_s = str(resp.get("error") or resp.get("errorMessage") or resp.get("code") or "")[:240]
                tried.append({"route": info["route"], "error": err_s or "execute_failed", "pair": pair, "venue": venue})
                return {
                    "signature": None, "submitted": True,
                    "error": err_s or "execute_failed",
                    "route": info["route"], "venue": venue, "status": status, "tried": tried, "send_fails": send_fails,
                    "fills": fills, "pairs_filled": list(filled_pairs),
                    "balances_after": bals,
                }
        elif venue == "raydium":
            txs = info.get("txs") or ([info["tx_b64"]] if info.get("tx_b64") else [])
            _LAST_REQUOTE.pop(c.get("route"), None)
            sig, err_s, uncertain = submit_unsigned_txs(
                txs, kp, lambda: _requote_unsigned(c), prior_sends=cand_sends,
            )
            print("EXEC", venue, sig, err_s, flush=True)
            if sig and not err_s:
                info, venue = _executed_info(info, venue, sig, cand_sends)
            if err_s or not sig:
                tried.append({"route": info["route"], "error": err_s or "raydium_send_failed", "pair": pair, "venue": venue})
                if uncertain:
                    return {
                        "signature": None, "submitted": True,
                        "error": err_s or "raydium_send_failed",
                        "route": info["route"], "venue": venue, "tried": tried, "send_fails": send_fails,
                        "fills": fills, "pairs_filled": list(filled_pairs),
                        "balances_after": bals,
                    }
                continue
        else:
            tried.append({"route": info["route"], "error": f"refusing_unknown_venue:{venue}", "pair": pair})
            continue
        when = datetime.now(PT).replace(microsecond=0).isoformat()
        side_str = f"{info['sold']}→{info['bought']}"
        fill_rate = info["exec_rate"]
        apply_fill_books(
            info["pair"], info["sold"], info["bought"], info["sell_amt"], info["buy_amt"],
            fill_rate, side_str, sig, when, info["gas_sol"], venue=venue,
            reverse_of_tx=c.get("fill_tx") if c.get("kind") == "reverse" else None,
        )
        # Persist books immediately so a later RPC blip cannot strand a live fill as SKIP.
        FILLED_THIS_CHECK[0] = True
        _cb_write(cb, force=True)
        # Confirmed fill only. Refresh before the next pair so we do not oversell.
        # Read at "confirmed" and retry until the fill shows (default finalized
        # reads lag the confirmed send and returned pre-trade balances).
        bals_after, bals_estimated = post_fill_balances(bals, info, ADDR)
        bals = bals_after
        usd_after = {k: bals_after[k] * prices[k] for k in ("SOL", "USDC", "cbBTC", "WETH")}
        total_after = round(sum(usd_after.values()) + savings_mode.savings_reserved_usd(bals_after, prices)
                            + _held_jitosol_usd(bals_after, prices), 4)  # jitototal1014
        if venue == "raydium":
            try:
                RAY_PREF.record_raydium_fill(info["pair"], when)
                RAY_PREF.save()
            except Exception as st_err:
                print(f"RAY_PREF_STATE_ERR {type(st_err).__name__}", flush=True)
        if info.get("raydium_first"):
            note = (
                f"{venue} local signer wallet; raydium FIRST (pair has repeated jupiter "
                f"Program failed to complete); fresh best-net quote=execution; "
                f"eff {info['eff_pct']}% edge {info['edge_pct']}% gap_net {info['gap_net_pct']}%; "
                f"gas {info['gas_sol']} SOL; one-per-pair multi-fill"
            )
        elif info.get("raydium_after_jupiter_program_fail"):
            note = (
                f"{venue} local signer wallet; raydium after jupiter "
                f"Program failed to complete; fresh raydium quote=execution; "
                f"eff {info['eff_pct']}% edge {info['edge_pct']}% gap_net {info['gap_net_pct']}%; "
                f"gas {info['gas_sol']} SOL; one-per-pair multi-fill"
            )
        else:
            note = (
                f"{venue} local signer wallet; fresh best-net quote=execution; "
                f"eff {info['eff_pct']}% edge {info['edge_pct']}% gap_net {info['gap_net_pct']}%; "
                f"gas {info['gas_sol']} SOL; one-per-pair multi-fill"
            )
        if bals_estimated:
            note += "; balances_after estimated (post-fill read unchanged)"
        append_fill_logs(when, side_str, info["sold"], info["sell_amt"], info["bought"], info["buy_amt"],
                         fill_rate, info["pair"], sig, bals_after, total_after, info["gas_sol"], note,
                         rev=_REV.rev_fields(c))
        fill_rec = {
            "signature": sig, "pair": info["pair"], "side": side_str,
            "sold": info["sold"], "sold_amount": info["sell_amt"],
            "bought": info["bought"], "bought_amount": info["buy_amt"],
            "rate": fill_rate, "eff_pct": info["eff_pct"], "edge_pct": info["edge_pct"],
            "gap_net_pct": info["gap_net_pct"], "gas_sol": info["gas_sol"],
            "balances_after": bals_after, "total_usd_after": total_after,
            "balances_estimated": bool(bals_estimated),
            "route": info["route"], "venue": venue, "kind": c.get("kind"),
            "fill_tx_reversed": c.get("fill_tx") if c.get("kind") == "reverse" else None,
        }
        fills.append(fill_rec)
        filled_pairs.add(info["pair"])
        print(
            f"FILL_OK pair={info['pair']} route={info['route']} sig={sig} "
            f"fills_so_far={len(fills)}",
            flush=True,
        )
        time.sleep(QUOTE_GAP)
    if not fills:
        return {"signature": None, "fills": [], "tried": tried, "send_fails": send_fails, "pairs_filled": [], "balances_after": bals}
    last = fills[-1]
    return {
        "signature": last["signature"], "pair": last["pair"], "side": last["side"],
        "sold": last["sold"], "sold_amount": last["sold_amount"],
        "bought": last["bought"], "bought_amount": last["bought_amount"],
        "rate": last["rate"], "eff_pct": last["eff_pct"], "edge_pct": last["edge_pct"],
        "gap_net_pct": last["gap_net_pct"], "gas_sol": last["gas_sol"],
        "balances_after": bals, "total_usd_after": last["total_usd_after"],
        "route": last["route"], "venue": last["venue"], "tried_skips": tried,
        "send_fails": send_fails,
        "fills": fills, "pairs_filled": list(filled_pairs),
        "fill_count": len(fills),
    }


candidates = []
try:
    fade_trend.begin_check()
except Exception as fade_err:
    print(f"FADE_TREND fade_trend_price_missing sold=- {type(fade_err).__name__}", flush=True)
for route, r in routes_out.items():
    # A bad-condition skip is not a fill candidate. Another pair may still be.
    # A fade skip is also not a fill. Price-missing does not remove the route.
    if r.get("clears_net") and not r.get("bad_condition_skip"):
        if fade_trend.current().blocks(r.get("sold")):
            r["fade_trend_skip"] = True
            r["fade_reason"] = "fade_trend"
            print(f"FADE_TREND_SKIP {route} sold={r.get('sold')}", flush=True)
        elif friday_rsi.basis_unwinds_rsi(rl.get(r.get("pair")), r.get("sold"), r.get("bought")):
            # Normal edge must not sell what the RSI trade bought or buy back what it sold.
            # The reverse candidate is separate and still needs the flat 5%.
            r["rsi_unwind_blocked"] = True
            r["clears_net"] = False
            print(
                f"RSI_UNWIND_BLOCK {route} pair={r.get('pair')} sold={r.get('sold')} bought={r.get('bought')}",
                flush=True,
            )
        else:
            candidates.append({"kind": "basis", "route": route, **r})
for pair, items in rev_out.items():
    for r in items:
        if r.get("clears_net") and not r.get("bad_condition_skip"):
            if fade_trend.current().blocks(r.get("opp_sold")):
                r["fade_trend_skip"] = True
                r["fade_reason"] = "fade_trend"
                print(f"FADE_TREND_SKIP {r.get('route_key') or ('REV:'+str(r.get('rev_route')))} sold={r.get('opp_sold')}", flush=True)
            else:
                route = r.get("route_key") or f"REV:{r['rev_route']}"
                candidates.append({"kind": "reverse", "route": route, "pair": pair, **r})

closest_list = []
for route, r in routes_out.items():
    if r.get("gap_net_pct") is not None:
        closest_list.append({"route": route, "gap_pct": r["gap_pct"], "gap_net_pct": r["gap_net_pct"],
                             "pts_to_clear": (r.get("clear_req") or {}).get("gap_to_clear_pts"), "clear_req_x": r.get("clear_req"),  # cblock1016 display only
                             "clears": r.get("clears"), "clears_net": r.get("clears_net"),
                             "eff_pct": r.get("eff_pct"), "edge_pct": r.get("edge_pct"),
                             "required_pct": r.get("eff_pct"),
                             "bad_condition_skip": bool(r.get("bad_condition_skip")),
                             "price_impact_pct": r.get("price_impact_pct"),
                             "bad_condition_reasons": r.get("bad_condition_reasons") or [],
                             "fade_trend_skip": bool(r.get("fade_trend_skip")),
                             "fade_reason": r.get("fade_reason")})
for pair, items in rev_out.items():
    for r in items:
        if r.get("gap_net_pct") is not None:
            closest_list.append({"route": r.get("route_key") or f"REV:{r['rev_route']}", "gap_pct": r["gap_pct"], "gap_net_pct": r["gap_net_pct"],
                                 "pts_to_clear": (r.get("clear_req") or {}).get("gap_to_clear_pts"), "clear_req_x": r.get("clear_req"),  # cblock1016 display only
                                 "clears": r.get("clears"), "clears_net": r.get("clears_net"), "size_ok": r.get("size_ok"),
                                 "eff_pct": r.get("eff_pct"), "edge_pct": r.get("edge_pct"),
                                 "required_pct": r.get("eff_pct"),
                                 "bad_condition_skip": bool(r.get("bad_condition_skip")),
                                 "price_impact_pct": r.get("price_impact_pct"),
                                 "bad_condition_reasons": r.get("bad_condition_reasons") or [],
                                 "fade_trend_skip": bool(r.get("fade_trend_skip")),
                                 "fade_reason": r.get("fade_reason"),
                                 "fill_tx": r.get("fill_tx")})
# pts2clear 2026-10-14: rank by points still needed to clear incl. hurdle (display only; never alters decisions)
closest_list.sort(key=lambda x: (x.get("pts_to_clear") is None, x["pts_to_clear"] if x.get("pts_to_clear") is not None else -x["gap_net_pct"]))

ok_routes = sum(1 for r in routes_out.values() if r.get("gap_net_pct") is not None)
ok_revs = sum(1 for items in rev_out.values() for r in items if r.get("gap_net_pct") is not None)
# Quote cap: only the top QUOTE_TOP_N are venue-quoted, so the old "8 of 12
# routes quoted" health test becomes: at least 8 basis routes sized and
# marked for ranking, and at least one venue quote back when any was attempted.
QUOTE_HEALTH_OK = True if FLAT_MARKET else (
    RANKED_BASIS >= 8 and (QUOTED_N == 0 or (ok_routes + ok_revs) >= 1)
)
print(
    f"QUOTE_HEALTH ok={QUOTE_HEALTH_OK} ranked_basis={RANKED_BASIS} quoted={QUOTED_N} "
    f"quoted_ok={ok_routes + ok_revs}",
    flush=True,
)

now = datetime.now(PT)
delay = random.randint(10, 25)
next_at = now + timedelta(minutes=delay)
cron = f"CRON_TZ={_TZ_NAME} {next_at.minute} {next_at.hour} {next_at.day} {next_at.month} *"

# Final USD with refreshed prices
usd_vals = book_usd_map(bals, prices)
total_usd = wallet_total_usd(bals, prices)
funded_usd = float(savings_mode.funded_usd(cb) or 0)
cost_basis_usd = funded_usd  # wallet P&L vs net deposits (not tokens_usd)
unrealized = (total_usd / funded_usd - 1) * 100 if funded_usd else 0
rank = sorted(usd_vals.items(), key=lambda x: x[1])
least, most = rank[0][0], rank[-1][0]

day_stop = {
    "enabled": True,
    "max_day_down_pct": 50.0,
    "hit": False,
    "status": "not_triggered",
    "reason": "No wallet day-start snapshot below the live book. New capital was funded 2026-10-02 (SOL+USDC). This is not a >=50% drawdown vs day-start wallet USD.",
}
fill_info = None
executable = []
# underweight_double is morning-only and must not buy USDC. This edge check does not run it.
if day_stop["hit"] or MIGRATION_BLOCKER:
    outcome = "HARD_BLOCKER"
elif FLAT_MARKET:
    outcome = "SKIP"
    print("RESULT_REASON flat_market", flush=True)
    eligible = []
    executable = []
else:
    eligible = []
    for c in sorted(candidates, key=friday_rsi.candidate_profit_key, reverse=True):
        sold, bought, sell_amt, buy_amt, pair, side = cand_legs(c)
        ok_c, post_pct = concentration_ok(sold, bought, sell_amt, buy_amt)
        c["concentration_ok"] = ok_c
        c["post_max_token_pct"] = None if post_pct is None else round(post_pct * 100.0, 3)
        if ok_c and same_pair_streak_blocks(pair):
            c["streak_blocked"] = True
            st = cb.get("same_pair_streak_cap") or {}
            print(
                f"STREAK_SKIP {c.get('route')} pair={pair} count={st.get('count')} "
                f"max={st.get('max_consecutive_fills')}",
                flush=True,
            )
        elif ok_c:
            eligible.append(c)
        else:
            print(f"CONC_BLOCK {c.get('route')} post_max_pct={c['post_max_token_pct']}", flush=True)
    # One most-profitable clear per pair. Several pairs may all fill this check.
    executable = friday_rsi.pick_best_per_pair(eligible)
    kept_routes = {c.get("route") for c in executable}
    for c in eligible:
        if c.get("route") not in kept_routes:
            print(
                f"PAIR_COLLAPSE_SKIP {c.get('route')} pair={c.get('pair')} "
                f"gap_net={c.get('gap_net_pct')} (kept better on same pair)",
                flush=True,
            )
    conc_by_route = {c.get("route"): c.get("concentration_ok") for c in candidates}
    for item in closest_list:
        if item["route"] in conc_by_route:
            item["concentration_ok"] = conc_by_route[item["route"]]
    print(
        f"EXECUTABLE_ONE_PER_PAIR n={len(executable)} pairs="
        f"{[c.get('pair') for c in executable]}",
        flush=True,
    )
    if executable:
        try:
            fill_info = execute_fills(executable)
        except Exception as e:
            fill_info = {"signature": None, "error": solana_rpc.redact(f"{type(e).__name__}:{e}")[:1500], "fills": []}
            print("EXEC_ERR", fill_info["error"], flush=True)
        if fill_info and (fill_info.get("signature") or fill_info.get("fills")):
            outcome = "FILL"
            bals = fill_info.get("balances_after") or bals
            bals_local = bals
            usd_vals = book_usd_map(bals, prices)
            total_usd = wallet_total_usd(bals, prices)
            funded_usd = float(savings_mode.funded_usd(cb) or 0)
            cost_basis_usd = funded_usd  # wallet P&L vs net deposits (not tokens_usd)
            unrealized = (total_usd / funded_usd - 1) * 100 if funded_usd else 0
            nfill = len(fill_info.get("fills") or ([] if not fill_info.get("signature") else [fill_info]))
            print(f"MULTI_FILL_DONE count={nfill} pairs={fill_info.get('pairs_filled')}", flush=True)
        elif QUOTE_HEALTH_OK:
            outcome = "SKIP"
        else:
            outcome = "HARD_BLOCKER"
    elif QUOTE_HEALTH_OK:
        outcome = "SKIP"
    else:
        outcome = "HARD_BLOCKER"

# Savings and memecoin hooks. A buy is not an edge fill and must not change
# streak or multipliers. Morning balance calls the matching morning_balance_hook.
# Each hook applies external SOL/USDC/cbBTC/WETH deposits to both USD latches
# before the buy test. A swap fill does not move the latches.
# If a latch would buy immediately because it is wrong, the hook fixes it and
# does not quote or swap.
ALCHEMY.set_route("hook:savings")
try:
    savings_status = savings_mode.edge_check_hook(cb, bals, prices, execute=(not FLAT_MARKET))
except Exception as sav_err:
    savings_status = {
        "enabled": False, "action": "skip", "reason": "savings_hook_error",
        "error": type(sav_err).__name__, "purchased": False, "quoted": False,
        "dirty": False, "counts_as_edge": False,
    }
print(
    "SAVINGS", savings_status.get("action"), savings_status.get("reason"),
    "enabled", savings_status.get("enabled"),
    "quoted", savings_status.get("quoted"),
    "latch", savings_status.get("usd_high_water"),
    "portfolio", None if savings_status.get("portfolio_usd") is None else round(float(savings_status.get("portfolio_usd")), 4),
    flush=True,
)
for _ex in savings_status.get("extra_buys") or []:
    print(
        "SAVINGS_EXTRA", _ex.get("symbol"), _ex.get("action"), _ex.get("reason"),
        "fund", _ex.get("fund_symbol"), "sig", _ex.get("signature"),
        flush=True,
    )
SAVINGS_RESERVED = {
    "cbBTC": float(bals.get("cbBTC_savings") or 0),
    "usd": round(savings_mode.savings_reserved_usd(bals, prices), 4),
    "held_ledger": savings_mode.savings_held_summary(cb, prices),
    "rule": "saved cbBTC is subtracted from cbBTC on every trading path; counted in total_usd and the savings latch",
}
ALCHEMY.set_route("hook:memecoin")
try:
    memecoin_status = memecoin_mode.edge_check_hook(cb, bals, prices, execute=(not FLAT_MARKET))
except Exception as mem_err:
    memecoin_status = {
        "enabled": False, "action": "skip", "reason": "memecoin_hook_error",
        "error": type(mem_err).__name__, "purchased": False, "quoted": False,
        "dirty": False, "counts_as_edge": False,
    }
print(
    "MEMECOIN", memecoin_status.get("action"), memecoin_status.get("reason"),
    "enabled", memecoin_status.get("enabled"),
    "quoted", memecoin_status.get("quoted"),
    flush=True,
)

# Refresh wallet P&L vs net funded after deposit/withdrawal detection in hooks.
funded_usd = float(savings_mode.funded_usd(cb) or 0)
cost_basis_usd = funded_usd
unrealized = (total_usd / funded_usd - 1) * 100 if funded_usd else 0

try:
    fade_snap = fade_trend.current().write_snapshot(now.isoformat(timespec="milliseconds"))
except Exception as fade_err:
    print(f"FADE_SNAPSHOT_ERR {type(fade_err).__name__}", flush=True)
    fade_snap = {"error": type(fade_err).__name__, "prices_usd": {s: None for s in fade_trend.TRACKED}}
# Flat-market reference: update only after a NON-skipped check so slow drift
# still eventually triggers a full quote. Flat skips keep the prior reference.
if not FLAT_MARKET:
    try:
        fade_trend.atomic_write_json(FLAT_REF_PATH, {
            "checked_pt": now.isoformat(timespec="milliseconds"),
            "timezone": _TZ_NAME,
            "source": getattr(fade_trend.current(), "price_source", None) or "jupiter_lite_price_v3",
            "rule": "Reference for flat_market_skip; updated only on non-skipped edge checks.",
            "prices_usd": {s: prices.get(s) for s in ("SOL", "cbBTC", "WETH", "USDC")},
        })
        print("FLAT_MARKET_REF_SAVED", flush=True)
    except Exception as _fr_err:
        print(f"FLAT_MARKET_REF_ERR {type(_fr_err).__name__}", flush=True)

result = {
    "checked_pt": now.isoformat(timespec="milliseconds"),
    "result": outcome,
    "skip_reason": ("flat_market" if FLAT_MARKET and outcome == "SKIP" else None),
    "flat_market": {
        "skipped": bool(FLAT_MARKET),
        "threshold_pct": FLAT_MARKET_CFG.get("threshold_pct"),
        "info": FLAT_MARKET_INFO,
        "ref_update_on": "non_skipped_check",
    },
    "route_hit_cooldown": {
        "cooled_routes": sorted(COOLED_ROUTES),
        "no_fill_days": ROUTE_HIT_CFG.get("no_fill_days"),
        "cooldown_hours": ROUTE_HIT_CFG.get("cooldown_hours"),
        "reverse_policy": ROUTE_HIT_CFG.get("reverse_policy"),
    },
    "reverse_max_age_hours": REVERSE_MAX_AGE_H,
    "mode": "primary_edge_check",
    "address_ok": True,
    "solana": ADDR,
    "solana_local": ADDR_LOCAL,
    "solana_old_phantom": ADDR_OLD,
    "balances_local": bals_local,
    "balances_old": bals_old,
    "migration_blocker": MIGRATION_BLOCKER,
    "local_tradable": local_tradable,
    "balances": bals,
    "prices_usd": prices,
    "balances_usd": {k: round(v, 4) for k,v in usd_vals.items()},
    "total_usd": round(total_usd, 4),
    "funded_usd": round(funded_usd, 4),
    "cost_basis_usd": round(funded_usd, 4),
    "unrealized_pnl_pct": round(unrealized, 3),
    "holdings_bias": {"least": least, "most": most, "rank_usd": {k: round(v,4) for k,v in usd_vals.items()}},
    "multipliers": mults,
    "same_pair_streak": {
        "pair": (cb.get("same_pair_streak_cap") or {}).get("pair"),
        "count": (cb.get("same_pair_streak_cap") or {}).get("count"),
        "max_consecutive_fills": H093.streak_cap(cb, (cb.get("same_pair_streak_cap") or {}).get("max_consecutive_fills") or 3),
        "h093_enabled": H093.enabled(cb),
    },
    "closest": closest_list[0] if closest_list else None,
    "closest3": closest_list[:3],
    "routes": routes_out,
    "reverse_last_trade": rev_out,
    "candidates": candidates,
    "executable_one_per_pair": [
        {"pair": c.get("pair"), "route": c.get("route"), "kind": c.get("kind"),
         "gap_net_pct": c.get("gap_net_pct"), "fill_tx": c.get("fill_tx")}
        for c in executable
    ],
    "fills": (fill_info or {}).get("fills") if isinstance(fill_info, dict) else None,
    "pairs_filled": (fill_info or {}).get("pairs_filled") if isinstance(fill_info, dict) else None,
    "best_candidate": max(candidates, key=lambda c: c.get("gap_net_pct", -999)) if candidates else None,
    "quote_source": "best_net:jupiter_manual+raydium(+orca_top1)",
    "phantom_mcp": "forbidden_except_one_time_drain",
    "delay_min": delay,
    "next_at_pt": next_at.replace(microsecond=0).isoformat(),
    "cron": cron,
    "gas_sol_estimate": GAS_SOL,
    "errors": errors,
    "ok_routes": ok_routes,
    "ok_revs": ok_revs,
    "quote_cap": {
        "top_n": QUOTE_TOP_N,
        "rule": "rank all basis routes + unreversed-window reverses by estimated gap_net (USD marks, est gas); Jupiter+Raydium quote only the top_n; fade/RSI-unwind/streak-blocked routes never take a slot",
        "ranked_basis": RANKED_BASIS,
        "quoted": QUOTED_N,
        "health_ok": QUOTE_HEALTH_OK,
        "rank": quote_rank_out,
        "phase": QUOTE_PHASE,
    },
    "pair_pnl_size_tilt": {
        "method": PP.METHOD,
        "state_file": PAIR_PNL_PATH,
        "pairs": {k: {**{kk: vv for kk, vv in v.items() if kk != "caps"},
                      "pnl_pct": None if v["pnl_pct"] is None else round(v["pnl_pct"], 4),
                      "caps": {"portfolio_pct": v["caps"][0], "balance_frac": v["caps"][1], "sol_buffer": v["caps"][2]}}
                  for k, v in PAIR_TILT.items()},
        "cfg": PAIR_TILT_CFG,
        "wallet_funded_pnl_pct_display_only": None if FUNDED_PNL_PCT is None else round(FUNDED_PNL_PCT, 4),
        "note": "basis sale: normal caps (15%/40%/0.02 SOL) -> x pair tilt -> clamp to pair caps (up pair 18.75%/50%/0.02 SOL) -> profit_sale_size_cut. Reverses keep mirrored size.",
    },
    "fade_trend": {
        "rule": "skip_sell_if_usd_strictly_above_last_check_and_24h",
        "scope": "edge_check_sells_including_savings_and_memecoin_funding",
        "not_applied_to": ["friday_rsi", "friday_band_rebalance", "morning_underweight"],
        "events": list(fade_trend.current().events),
        "snapshot": fade_snap,
    },
    "bad_condition_skip": {
        "scope": "edge_checks_only",
        "enabled": BAD_CFG["enabled"],
        "price_impact_max_pct": BAD_CFG["price_impact_max_pct"],
        "memecoin_exempt": BAD_CFG["memecoin_exempt"],
        "tip_multiple": BAD_CFG["tip_multiple"],
        "base_signature_lamports": BAD_CFG["base_signature_lamports"],
        "spike_test": "attached_tip_gt_tip_multiple_x_signature_fee",
        "blocked": bad_blocks,
    },
    "notes": ("HARD_BLOCKER: wallet not tradable. " if MIGRATION_BLOCKER else "Live quotes, balances, and fills are wallet only. ") + "Best net of Jupiter manual (swap/v2/build platformFeeBps=0) and Raydium after network fees and any Jito tip; fill on that venue. Leftover floor is on top of required eff%. No edge fill when the winning quote price impact is over price_impact_max_pct (memecoin-tagged pairs exempt from that impact cap only) or when its attached tip (priority fee plus any Jito tip) is more than about 2x the base signature fee (5000 lamports times signatures). That route is not filled; another pair may be. If none clear, this is a normal skip: not a fill, streak and pair multipliers unchanged, skip-halving applies. No Ultra commission in the edge. No Orca. No Phantom. No underweight_double USDC buy. Not the Friday RSI check. The Friday band fallback is retired (bands_if_no_trade false). Cadence random 10-25. Parent must UpdateState routine cron sol-usdc-cbbtc-jupiter-trading. Fade-trend: an edge-check sell of SOL, USDC, cbBTC, or WETH, including a sell that funds a savings or memecoin buy, is skipped when that token is strictly above its last-check USD price and its USD price 24h ago. Equal or missing prices do not skip. Not a fill. The memecoin impact-cap exemption does not waive it. Not morning underweight. Quote cap (2026-10-05): all routes and window reverses are ranked by estimated gap_net from USD marks; only the top 5 are Jupiter+Raydium quoted. Basis sale size is tilted per pair by that pair's realized P&L% (x1.25 above +0.25% with that trade's caps raised to 18.75% portfolio / 50% balance; x0.75 below -0.25%; else x1.0), clamped to the pair's caps (0.02 SOL buffer always), then the underwater-token profit cut. Reverses keep mirrored size. Raydium first on a pair after 2 consecutive Jupiter 'Program failed to complete' sends within 24h. Alchemy fallback: max 1 per directed route attempt, max 2 per check. No fees-vs-edge tracking.",
    "day_loss_stop": day_stop,
    "fill": fill_info,
    "alchemy_fallback": ALCHEMY.summary(),
    "raydium_first_pairs": sorted(
        pr for pr in (RAY_PREF.data.get("pairs") or {}) if RAY_PREF.prefer_raydium(pr, datetime.now(PT))
    ),
    "memecoin_mode_enabled": bool(BAD_CFG.get("memecoin_mode_enabled")),
    "savings_mode": savings_status,
    "savings_reserved": SAVINGS_RESERVED,
    "memecoin_mode": memecoin_status,
}

fade_trend.atomic_write_json(_DH.HOME_STR + "/last-check.eval.json", result)
slim = {
    "checked_pt": result["checked_pt"], "result": result["result"], "address_ok": True,
    "solana": ADDR, "solana_local": ADDR_LOCAL, "solana_old_phantom": ADDR_OLD,
    "balances_local": bals_local, "balances_old": bals_old,
    "migration_blocker": MIGRATION_BLOCKER, "local_tradable": local_tradable,
    "balances": bals, "prices_usd": prices,
    "usd_price_snapshot": fade_snap,
    "balances_usd": result["balances_usd"], "total_usd": result["total_usd"],
    "funded_usd": result["funded_usd"], "cost_basis_usd": result["funded_usd"],
    "unrealized_pnl_pct": result["unrealized_pnl_pct"],
    "holdings_bias": {"least": least, "most": most}, "multipliers": mults,
    "closest": result["closest"], "quote_source": result["quote_source"],
    "phantom_mcp": "forbidden_except_one_time_drain", "delay_min": delay, "next_at_pt": result["next_at_pt"], "cron": cron,
}
try:  # ultra1012: auto slippage status + SLIPPAGE_FAIL (24h count)
    import auto_slip as _AS
    _AS.note_prices(prices)
    slim["auto_slippage"] = _AS.status(); slim["SLIPPAGE_FAIL"] = slim["auto_slippage"]["fails_24h"]
    result["auto_slippage"] = slim["auto_slippage"]
except Exception as _as_err:
    print(f"AUTO_SLIP_ERR {type(_as_err).__name__}", flush=True)
fade_trend.atomic_write_json(_DH.HOME_STR + "/last-check.json", slim)
fade_trend.atomic_write_json(_DH.HOME_STR + "/last-check.json", slim)

def _trade_fail_lines():
    """Attempted trades that did not land. Quote skips that never tried are not failures."""
    lines = []
    if isinstance(fill_info, dict):
        seen = set()
        err_top = fill_info.get("error")
        top_line = None
        if err_top and not (fill_info.get("signature") or fill_info.get("fills")):
            top_line = {
                "where": "edge",
                "route": fill_info.get("route"),
                "venue": fill_info.get("venue"),
                "reason": err_top,
            }
        reported = list(fill_info.get("tried") or [])
        reported.extend(fill_info.get("send_fails") or [])
        for item in reported:
            if not isinstance(item, dict) or not item.get("error"):
                continue
            key = (item.get("route"), item.get("venue"), item.get("error"))
            if key in seen:
                continue
            seen.add(key)
            lines.append({
                "where": "edge",
                "route": item.get("route"),
                "pair": item.get("pair"),
                "venue": item.get("venue"),
                "reason": item.get("error"),
            })
        # 2026-10-08: one failed send was reported twice (top-level error and
        # the same per-try entry), so alerts said count=2 / "tried twice".
        # Keep the top-level line only when no per-try line already covers it.
        if top_line is not None and (top_line["route"], top_line["venue"], top_line["reason"]) not in seen:
            lines.insert(0, top_line)
    fail_prefixes = ("send_failed", "build_failed", "quote_failed", "no_ok_quote")
    for label, status in (("savings", savings_status), ("memecoin", memecoin_status)):
        if not isinstance(status, dict):
            continue
        reason = str(status.get("reason") or "")
        if reason.startswith(fail_prefixes):
            lines.append({
                "where": label,
                "route": status.get("route") or status.get("fund_symbol"),
                "venue": status.get("venue"),
                "reason": reason,
            })
        # Extra savings buys (cbBTC $1): any attempt that did not land is a
        # failure. It is recorded in savings_mode.extra_buy_failures and is
        # not retried once the latch has advanced.
        for ex in status.get("extra_failures") or []:
            if not isinstance(ex, dict):
                continue
            lines.append({
                "where": f"{label}:{ex.get('symbol')}",
                "route": f"{ex.get('fund_symbol')}->{ex.get('symbol')}",
                "venue": ex.get("venue"),
                "reason": ex.get("reason"),
                "latch_advanced": bool(status.get("latch_advanced")),
            })
    return lines

trade_fails = _trade_fail_lines()
# Paper mode: a skipped send is a would-be fill, not a failure (no trade-fail alert, not in the fail streak).
paper_skips = [f for f in trade_fails if solana_rpc.is_paper_skip(f)]
trade_fails = [f for f in trade_fails if not solana_rpc.is_paper_skip(f)]
for ps in paper_skips[:1]:
    print("PAPER_WOULD_FILL", json.dumps({"route": ps.get("route"), "venue": ps.get("venue"),
                                          "note": solana_rpc.PAPER_SKIP_MSG}, default=str), flush=True)
for fail in trade_fails:
    print("TRADE_FAIL", json.dumps(fail, default=str), flush=True)
if trade_fails:
    print(f"NOTIFY_USER trade_failed count={len(trade_fails)}", flush=True)

print("\nRESULT", outcome, flush=True)
if _DH.research_logging():  # template: closest-route dump is study-only (desk.json still gets closest3)
    print("CLOSEST3", json.dumps(result["closest3"]), flush=True)
print("CANDIDATES", len(candidates), flush=True)
print("NEXT", delay, result["next_at_pt"], cron, flush=True)

# --- post: skip-halving update + schedule/log files ---
now_iso = now.replace(microsecond=0).isoformat()
if outcome == "SKIP" and SEH_ENABLED:
    # After 2 consecutive quiet SKIPs, halve once. Further quiet checks do not.
    # No multiplier floor. No once-per-hour timer.
    # A fade, price-impact, or fee skip is a quiet check (outcome SKIP, nothing filled).
    # eff% clamp (0.20-5) applies to the percent, not as a floor on this multiplier.
    status = apply_skip_edge_halving(cb["skip_edge_halving"], "SKIP")
    ap_q = EP.tick_any_profit_counter(cb, "SKIP", had_fill=False)
    cb["skip_edge_halving"]["updated_pt"] = now_iso
    cb["updated_pt"] = now_iso
    _cb_write(cb)
    seh_now = cb["skip_edge_halving"]
    print(
        f"skip_edge_halving -> mult={seh_now['current_multiplier']} "
        f"quiet={seh_now['consecutive_quiet_checks']} skips={seh_now['skips_since_fill']} "
        f"halved={status == 'quiet_halved'}",
        flush=True,
    )
    print(f"any_profit_after_quiet -> quiet_checks_since_fill={ap_q}", flush=True)
elif outcome == "FILL" and SEH_ENABLED and local_tradable and isinstance(fill_info, dict) and (fill_info.get("signature") or fill_info.get("fills")):
    # Edge fill resets the multiplier to 1.0 and clears the quiet count.
    # A rebalance fill must not reset it and must not count as a quiet check or an edge fill.
    _seh_fill = dict(fill_info)
    if not _seh_fill.get("signature") and _seh_fill.get("fills"):
        _seh_fill["signature"] = _seh_fill["fills"][-1].get("signature")
    status = apply_skip_edge_halving(
        cb["skip_edge_halving"], "FILL", fill_info=_seh_fill, local_tradable=local_tradable,
    )
    # any_profit counter resets on ANY fill (edge or rebalance)
    ap_q = EP.tick_any_profit_counter(cb, "FILL", had_fill=True)
    if status == "edge_reset":
        cb["skip_edge_halving"]["updated_pt"] = now_iso
        cb["updated_pt"] = now_iso
        _cb_write(cb)
        print("skip_edge_halving -> reset mult=1.0 quiet=0 (edge fill)", flush=True)
    else:
        # Persist any_profit counter reset even when skip-halving stays put (rebalance).
        cb["updated_pt"] = now_iso
        _cb_write(cb)
        print(
            f"skip_edge_halving unchanged (non-edge fill) mult={SEH_MULT} quiet={SEH_QUIET} skips={SEH_SKIPS}",
            flush=True,
        )
    print(f"any_profit_after_quiet -> quiet_checks_since_fill={ap_q} (reset on fill)", flush=True)
elif outcome == "HARD_BLOCKER":
    print(f"skip_edge_halving unchanged (HARD_BLOCKER) mult={SEH_MULT} skips={SEH_SKIPS}", flush=True)
    # Fee samples only. Do not touch skip-halving, streak, or pair multipliers.
    # A savings buy is not an edge fill, but its watermark must still be saved.
    hook_dirty = (
        (isinstance(savings_status, dict) and savings_status.get("dirty"))
        or (isinstance(memecoin_status, dict) and memecoin_status.get("dirty"))
    )
    if fee_history_dirty or hook_dirty:
        cb["updated_pt"] = now_iso
        _cb_write(cb)

# A savings latch step (JitoSOL and/or saved cbBTC) must persist whatever the
# outcome branch above did. Idempotent rewrite of the same book.
if (isinstance(savings_status, dict) and savings_status.get("dirty")) or (
    isinstance(memecoin_status, dict) and memecoin_status.get("dirty")
):
    _cb_write(cb)

sched = {
    "updated_pt": now_iso,
    "delay_min": delay,
    "next_at_pt": next_at.replace(microsecond=0).isoformat(),
    "cron": cron,
    "result": outcome,
    "random_min": 10,
    "random_max": 25,
}
with open(_DH.HOME_STR + "/next-check-schedule.json", "w") as f:
    json.dump(sched, f, indent=2)
    f.write("\n")
cad = load_json(_DH.HOME_STR + "/cadence.json")
cad.update({"updated_pt": now_iso, "mode": "random_10_25", "random_min": 10, "random_max": 25, "delay_min_remaining_approx": delay, "next_at_pt": sched["next_at_pt"], "cron": cron, "result": outcome})
with open(_DH.HOME_STR + "/cadence.json", "w") as f:
    json.dump(cad, f, indent=2)
    f.write("\n")

# Held savings JitoSOL / memecoin Greedy Goats (log every check; real wallet qty)
try:
    _jito_qty = float(HELD_WALLET.get("JitoSOL") or 0)
    _goat_qty = held_goats_wallet_qty()
    if _goat_qty is None:
        _goat_qty = float(((cb.get("memecoin_mode") or {}).get("qty")) or 0)
    _need = []
    if _jito_qty > 0:
        _need.append(("JitoSOL", savings_mode.mint_of(cb)))
    if _goat_qty > 0:
        _need.append(("GreedyGoats", memecoin_mode.mint_of(cb)))
    _hpx = held_usd_prices(_need)
    _jito_px = (jitosol_usd_price(_hpx.get("JitoSOL") or savings_mode.JITOSOL_USD, prices.get("SOL"))
                if _jito_qty > 0 else 0.0)
    _jito_usd = _jito_qty * _jito_px
    if _goat_qty > 0 and _hpx.get("GreedyGoats"):
        _goat_usd = _goat_qty * _hpx["GreedyGoats"]
    elif _goat_qty > 0:
        _goat_usd = float(((cb.get("memecoin_mode") or {}).get("cost_usd")) or 0)
    else:
        _goat_usd = 0.0
except Exception as _hx:
    print(f"HELD_LINES_ERR {type(_hx).__name__}", flush=True)
    _jito_qty, _jito_usd, _goat_qty, _goat_usd = 0.0, 0.0, 0.0, 0.0
_jito_qty_s = ("0.0" if abs(_jito_qty) < 1e-12 else f"{_jito_qty:g}")
_goat_qty_s = ("0" if abs(_goat_qty) < 1e-12 else (str(int(_goat_qty)) if float(_goat_qty).is_integer() else f"{_goat_qty:g}"))
HELD_LOG_LINES = (
    f"- savings_jitosol_held: {_jito_qty_s} (${_jito_usd:.2f})\n"
    f"- savings_goat_held: {_goat_qty_s} (${_goat_usd:.2f})\n"
)
result["held_outside_book"] = {
    "JitoSOL": {"qty": _jito_qty, "usd": round(_jito_usd, 4)},
    "GreedyGoats": {"qty": _goat_qty, "usd": round(_goat_usd, 4)},
}

result["market_gates"] = {"fear_greed": MG.fng, "vwap24h": MG.vwap, "greed_size_mult": GREED_MULT,
                          "reversal_cap": REVERSAL_CAP, "blocked": MG.blocked}
def _c3_extras(items):
    """cblock1016 (display/logging only; never alters decisions): per closest-3 route the bot's
    block reasons, required total % and profit after fees, plus send-failed when this check's send failed."""
    out = []
    try:
        failed = {str((f or {}).get("route")) for f in (globals().get("trade_fails") or []) if isinstance(f, dict)}
    except Exception:
        failed = set()
    for x in items or []:
        try:
            cr = x.get("clear_req_x") or {}
            lab = []
            hu = cr.get("hurdle_usd")
            for b in cr.get("blocks") or []:
                b = str(b)
                if b == "hurdle":
                    lab.append(f"${float(hu):.2f} hurdle" if hu is not None else "hurdle")
                elif b == "below_required_eff":
                    lab.append("below required %")
                elif b == "leftover_floor":
                    lab.append("leftover floor")
                elif b == "size":
                    lab.append("size")
                elif b.startswith("bad_condition"):
                    lab.append("bad condition")
                elif b == "route_hit_cooldown":
                    lab.append("cooldown")
                elif b == "any_profit_bypass_on":
                    continue  # a bypass, not a block
                else:
                    lab.append(b.replace("_", " "))
            if x.get("fade_trend_skip") and "fade-trend" not in lab:
                lab.append("fade-trend")
            if x.get("bad_condition_skip") and "bad condition" not in lab:
                lab.append("bad condition")
            out.append({"route": x.get("route"), "k": lab, "req": cr.get("required_total_pct"),
                        "en": cr.get("edge_net_pct"), "sf": 1 if str(x.get("route")) in failed else 0})
        except Exception:
            out.append({"route": x.get("route") if isinstance(x, dict) else None, "k": [], "req": None, "en": None, "sf": 0})
    return out


# brief log append
c3 = result.get("closest3") or []
c3s = ", ".join(f"{x['route']} {x['gap_pct']}%/{x['gap_net_pct']}%" + (f" {x['pts_to_clear']}pts" if x.get('pts_to_clear') is not None else "") for x in c3[:3])
_fill_ln = (
    f"- fill: {fill_info.get('signature') if isinstance(fill_info, dict) else None} {fill_info.get('side') if isinstance(fill_info, dict) else ''} {fill_info.get('sold_amount') if isinstance(fill_info, dict) else ''} -> {fill_info.get('bought_amount') if isinstance(fill_info, dict) else ''}\n"
)
_closest_ln = f"- closest required_pct vs quoted edge: {(result.get('closest') or {}).get('required_pct')} vs {(result.get('closest') or {}).get('edge_pct')} ({(result.get('closest') or {}).get('route')})\n"


def _hook_bought(st):
    if not isinstance(st, dict):
        return False
    if st.get("purchased") or st.get("action") in ("bought", "fill", "purchased", "buy"):
        return True
    return any(
        isinstance(x, dict) and (x.get("signature") or x.get("action") in ("bought", "fill", "purchased", "buy"))
        for x in (st.get("extra_buys") or [])
    )


# slimlog 2026-10-11: a "send attempt" is any swap execution or savings/memecoin buy.
SEND_ATTEMPT = bool(fill_info is not None or _hook_bought(savings_status) or _hook_bought(memecoin_status))
SLIM_SKIP = bool(outcome == "SKIP" and not SEND_ATTEMPT)
try:
    _quiet_now = int(EP.quiet_checks_since_fill(cb))
except Exception:
    _quiet_now = ANY_PROFIT_QUIET
_quiet_thr = ANY_PROFIT_CFG.get("quiet_checks_threshold")


def _compact_blocked(blocked):
    """Group rule_blocked by rule: named routes listed, reversals counted."""
    if not blocked:
        return "none"
    groups = {}
    for r, why in sorted(blocked.items()):
        g = groups.setdefault(str(why), [[], 0])
        if str(r).startswith("REV:"):
            g[1] += 1
        else:
            g[0].append(str(r))
    parts = []
    for why, (routes, nrev) in sorted(groups.items()):
        txt = ", ".join(routes)
        if nrev:
            txt = (txt + "; " if txt else "") + f"+{nrev} reversal{'s' if nrev != 1 else ''}"
        parts.append(f"{why}: {txt}")
    return " | ".join(parts)


if SLIM_SKIP:
    _mg = MG.log_lines().splitlines(True)
    _mg = [ln for ln in _mg if not ln.startswith("- rule_blocked:")]
    _st = cb.get("same_pair_streak_cap") or {}
    _streak_ln = f"- streak: {_st.get('pair')} x{_st.get('count')}\n" if _st.get("pair") and int(_st.get("count") or 0) > 0 else ""
    _flat_ln = f"- flat_market: skip ({FLAT_MARKET_INFO.get('reason')})\n" if FLAT_MARKET else ""
    _runner_iv = int(os.environ.get("DEGEN_RUNNER_INTERVAL_SEC") or 60)
    _next_runner = (datetime.fromisoformat(now_iso) + timedelta(seconds=_runner_iv)).replace(microsecond=0).isoformat()
    _iv_txt = f"+{_runner_iv // 60}m" if _runner_iv % 60 == 0 else f"+{_runner_iv}s"
    # logdiet 2026-10-11: compact + deduplicated SKIP entries (see log_diet.py).
    import log_diet as _LD
    _vw_txt, _vw_flags = _LD.vwap_text(MG.vwap, MG.f_on, MG.v_on)
    _fg_txt, _fg_key = _LD.fg_text(MG.fng, MG.f_on, MG.size_mult())
    _ld_ctx = {
        "now_iso": now_iso, "outcome": outcome, "addr": ADDR,
        "bals": {"SOL": f"{bals['SOL']:.6f}", "USDC": bals["USDC"], "cbBTC": bals["cbBTC"], "WETH": bals["WETH"]},
        "usd": usd_vals, "total": total_usd, "unreal": unrealized, "funded": funded_usd,
        "least": least, "most": most,
        "streak": [_st.get("pair"), _st.get("count")] if _streak_ln else None,
        "sav_qty": [SAVINGS_RESERVED["cbBTC"], _jito_qty, _goat_qty],
        "sav_text": f"cbBTC {SAVINGS_RESERVED['cbBTC']} ${SAVINGS_RESERVED['usd']:.2f} | JitoSOL {_jito_qty_s} ${_jito_usd:.2f} | GOAT {_goat_qty_s} ${_goat_usd:.2f}",
        "fg_text": _fg_txt, "fg_key": _fg_key, "vw_text": _vw_txt, "vw_flags": _vw_flags,
        "blocked": dict(MG.blocked or {}),
        "flat": FLAT_MARKET_INFO.get("reason") if FLAT_MARKET else None,
        "c3": c3, "closest": result.get("closest") or {},
        "quiet": _quiet_now, "quiet_thr": _quiet_thr, "next_iso": _next_runner,
        "last_heading": _LD.last_heading(_DH.HOME_STR + "/sol-usdc-log.md"),
    }
    try:
        _ld_st = _LD.load_state(_DH.HOME_STR)
        log_line, _ld_new, _ld_kind = _LD.render(_ld_ctx, _ld_st)
    except Exception as _ld_err:  # never lose a check: fall back to a forced full entry
        print(f"LOG_DIET_ERR {type(_ld_err).__name__}", flush=True)
        log_line, _ld_new, _ld_kind = _LD.render(_ld_ctx, {})
else:
    log_line = (
        f"\n## {now_iso} Primary edge check — {outcome}\n"
        f"- address_ok: true (RPC) `{ADDR}`\n"
        f"- balances: SOL={bals['SOL']:.6f} (${usd_vals['SOL']:.2f}), USDC={bals['USDC']} (${usd_vals['USDC']:.2f}), "
        f"cbBTC={bals['cbBTC']} (${usd_vals['cbBTC']:.2f}), WETH={bals['WETH']} (${usd_vals['WETH']:.2f})\n"
        f"- total_usd: ${total_usd:.2f} (unrealized {unrealized:.2f}% vs ${funded_usd:.2f} funded)\n"
        f"- holdings_bias: least={least} most={most}; mults={json.dumps(mults, sort_keys=True)}\n"
        f"- same_pair_streak: {(cb.get('same_pair_streak_cap') or {}).get('pair')} x{(cb.get('same_pair_streak_cap') or {}).get('count')}\n"
        f"- savings_mode: enabled={savings_status.get('enabled')} action={savings_status.get('action')} reason={savings_status.get('reason')} latch={savings_status.get('usd_high_water')} extras={[(e.get('symbol'), e.get('action'), e.get('reason')) for e in (savings_status.get('extra_buys') or [])]}\n"
        f"- savings_cbbtc_held: {SAVINGS_RESERVED['cbBTC']} (${SAVINGS_RESERVED['usd']:.2f}) excluded from trading; in total_usd\n"
        + HELD_LOG_LINES + MG.log_lines() +
        f"- memecoin_mode: enabled={memecoin_status.get('enabled')} action={memecoin_status.get('action')} reason={memecoin_status.get('reason')}\n"
        f"- skip_edge_halving: pre_mult={SEH_MULT} clamp floor={CLAMP_FLOOR}% cap={CLAMP_CAP}%\n"
        f"- bad_condition_skip: blocked={len(bad_blocks)} price_impact_max_pct={BAD_CFG['price_impact_max_pct']} "
        f"memecoin_exempt={BAD_CFG['memecoin_exempt']} fee_spike={BAD_CFG['tip_multiple']}x "
        f"signature_lamports={BAD_CFG['base_signature_lamports']}\n"
        f"- flat_market: skip={FLAT_MARKET} reason={FLAT_MARKET_INFO.get('reason')} thr={FLAT_MARKET_CFG.get('threshold_pct')}%\n"
        f"- reverse_max_age_h: {REVERSE_MAX_AGE_H}; route_hit_cooled={sorted(COOLED_ROUTES)}\n"
        f"- closest3: {c3s}\n"
        f"- quote_cap: top{QUOTE_TOP_N} quoted={[x['route'] for x in quote_rank_out if x.get('quoted')]} ranked_basis={RANKED_BASIS} pool={len(quote_rank_out)}\n"
        f"- pair_size_tilt: " + ", ".join(f"{k} {'-' if v['pnl_pct'] is None else round(v['pnl_pct'], 3)}% x{v['mult']}" for k, v in PAIR_TILT.items()) + f"; alchemy_fallbacks={ALCHEMY.total}/{ALCHEMY.per_check_max} capped={len(ALCHEMY.events)}; raydium_first={result.get('raydium_first_pairs') or []}\n"
        f"- candidates: {len(candidates)}; quote_source=best_net jupiter_manual+raydium(+orca_top1); migration_blocker={MIGRATION_BLOCKER}\n"
        + _fill_ln + _closest_ln
        + f"- quiet: {_quiet_now}/{_quiet_thr}\n"
        f"- next: +{delay}m → {sched['next_at_pt']} (`{cron}`)\n"
    )
# One-line summary for box_runner.py's compact runner log (no secrets).
_c0 = c3[0] if c3 else {}
print("LOG_SUMMARY " + json.dumps({
    "result": outcome, "log": (_ld_kind if SLIM_SKIP else "verbose"), "log_bytes": len(log_line.encode("utf-8")), "send_attempt": SEND_ATTEMPT,
    "closest": _c0.get("route"), "gap_pct": _c0.get("gap_pct"), "gap_net_pct": _c0.get("gap_net_pct"),
    "quiet": _quiet_now, "quiet_thr": _quiet_thr, "blocked": len(MG.blocked or {}),
}), flush=True)
with open(_DH.HOME_STR + "/sol-usdc-log.md", "a", encoding="utf-8", newline="") as f:
    f.write(log_line)
try:  # closest0 2026-10-11: live closest3 for desk.json every check (log may only get a one-liner)
    fade_trend.atomic_write_json(_DH.HOME_STR + "/live-closest.json", {"t": now_iso, "c3": c3s, "x": _c3_extras(c3[:3])})
except Exception as _lc_err:
    print(f"LIVE_CLOSEST_ERR {type(_lc_err).__name__}", flush=True)
try:  # status1012: balance status for desk.json every balance_status_every_sec (reporting.json); never in the log
    import balance_status as _BS
    if not _JL.held():
        _BS.maybe_write(result, now_iso)
except Exception as _bs_err:
    print(f"BALANCE_STATUS_ERR {type(_bs_err).__name__}", flush=True)
try:  # logdiet: remember the last full SKIP; any verbose entry forces the next SKIP full
    import log_diet as _LD
    _LD.save_state(_DH.HOME_STR, _ld_new if SLIM_SKIP else _LD.note_verbose(_LD.load_state(_DH.HOME_STR), now_iso))
except Exception as _ld_err:
    print(f"LOG_DIET_STATE_ERR {type(_ld_err).__name__}", flush=True)

# enrich result with skip state
result["skip_edge_halving"] = {
    "pre_multiplier": SEH_MULT,
    "post_multiplier": cb["skip_edge_halving"]["current_multiplier"],
    "skips_since_fill": cb["skip_edge_halving"]["skips_since_fill"],
    "consecutive_quiet_checks": cb["skip_edge_halving"].get("consecutive_quiet_checks", 0),
    "streak_halved": bool(cb["skip_edge_halving"].get("streak_halved", False)),
}
result["effective_pct_clamp"] = {
    "enabled": CLAMP_ENABLED,
    "all_in_floor_pct": CLAMP_FLOOR,
    "cap_pct": CLAMP_CAP,
}
result["quote_source"] = "best_net:jupiter_manual+raydium(+orca_top1)"
slim["skip_edge_halving"] = result["skip_edge_halving"]
slim["closest3"] = result["closest3"]
slim["fill"] = result.get("fill")
slim["knife_hurdle_v1"] = result["knife_hurdle_v1"] = {"enabled": KH_CFG["enabled"], **KH_STATS}
if KH_CFG["enabled"] and _DH.research_logging():  # template: hurdle stats print is study-only
    print("KNIFE_HURDLE " + json.dumps(KH_STATS), flush=True)
try:  # lend1015: Jupiter Lend idle rule / switch-off withdraw, after all trades of this check
    import degen_lend as _LEND
    if solana_rpc.long_wait_used():
        _lend_st = {"skipped": "unresolved send this check"}
    else:
        _lend_st = _LEND.tick(source="edge_check")
    slim["lend"] = result["lend"] = _lend_st
    if _lend_st.get("lent") is not None:
        print("LEND " + json.dumps({k: _lend_st.get(k) for k in ("enabled", "liquid", "lent", "apr", "action", "sig", "err") if _lend_st.get(k) is not None}), flush=True)
except Exception as _lend_err:
    print(f"LEND_ERR edge_tick {type(_lend_err).__name__}", flush=True)
try:  # stake1015: JitoSOL bucket idle rule / switch-off unstake, after all trades of this check
    import sol_stake as _STK
    if solana_rpc.long_wait_used():
        _stk_st = {"skipped": "unresolved send this check"}
    else:
        _stk_st = _STK.tick(source="edge_check")
    slim["stake"] = result["stake"] = _stk_st
    if _stk_st.get("bucket_sol") is not None:
        print("STAKE " + json.dumps({k: _stk_st.get(k) for k in ("enabled", "liquid_sol", "bucket_jitosol", "bucket_sol", "apy", "action", "sig", "err") if _stk_st.get(k) is not None}), flush=True)
except Exception as _stk_err:
    print(f"STAKE_ERR edge_tick {type(_stk_err).__name__}", flush=True)
fade_trend.atomic_write_json(_DH.HOME_STR + "/last-check.eval.json", result)
fade_trend.atomic_write_json(_DH.HOME_STR + "/last-check.json", slim)

# Dashboard snapshot after last-check.json write (Desktop CopyFromBox CANCELLED).
# Always refresh so quiet streak / balances stay current; never wakes chat.
_dash_reason = "edge_check"
if outcome == "FILL":
    _dash_reason = "edge_fill"
elif isinstance(savings_status, dict) and (
    savings_status.get("purchased")
    or savings_status.get("action") in ("bought", "fill", "purchased")
    or any(
        isinstance(x, dict) and (x.get("signature") or x.get("action") in ("bought", "fill", "purchased"))
        for x in (savings_status.get("extra_buys") or [])
    )
):
    _dash_reason = "savings_fill"
elif isinstance(memecoin_status, dict) and (
    memecoin_status.get("purchased")
    or memecoin_status.get("action") in ("bought", "fill", "purchased")
):
    _dash_reason = "memecoin_fill"
try:
    import dashboard_snapshot as _dash
    _dash.refresh(reason=_dash_reason)
    print(f"DASHBOARD_SNAPSHOT reason={_dash_reason}", flush=True)
except Exception as _dash_err:
    print(f"DASHBOARD_SNAPSHOT_ERR {type(_dash_err).__name__}", flush=True)

print("ok", ok_routes, ok_revs, "total_usd", round(total_usd,4), "unreal", round(unrealized,3), flush=True)
_clear_req_flush()
print("REPORT_READY", flush=True)

