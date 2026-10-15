"""Peer quotes for wallet: Jupiter manual, Raydium, and (top-1 only) Orca.

Quote equals execution. The best net after Solana network fees and any Jito
tip is the fill, built for that venue. Jupiter is manual mode only:
swap/v2/build, platformFeeBps=0, no Jupiter commission. Ultra is not requested.
Orca Whirlpool has no public REST swap API; venue "orca" quotes via Jupiter
swap/v2/build with dexes=Whirlpool (Whirlpool-only route). Build/sign reuses
the Jupiter manual assembler (identical response shape). Orca is requested
only for the single best-ranked candidate each check. Nothing here calls
Phantom or sends a swap. Jupiter manual and Raydium are quoted together;
Orca is an optional third call. solders is imported once per process.
"""
import base64
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

try:
    from solders.address_lookup_table_account import AddressLookupTableAccount
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.signature import Signature
    from solders.transaction import VersionedTransaction
    _SOLDERS_ERR = None
except Exception as _solders_import_error:
    AddressLookupTableAccount = Hash = AccountMeta = Instruction = None
    MessageV0 = Pubkey = Signature = VersionedTransaction = None
    _SOLDERS_ERR = _solders_import_error

ADDR = __import__("degen_home").wallet_address()
MINTS = {
    "SOL": "So11111111111111111111111111111111111111112",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "cbBTC": "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij",
    "WETH": "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
}
# Mint -> token program. Empty means the classic SPL program. Held memecoins
# may be Token-2022; that does not add them to the six-pair book.
TOKEN_PROGRAMS = {}
BASE_SIGNATURE_LAMPORTS = 5000
RAYDIUM_QUOTE_CU = 250_000
import solana_rpc
import auto_slip as _AS
import jupkey as _JK  # jupkey1012  # ultra1012: capped auto slippage (<=30 bps)
# Manual market route. No Jupiter commission. Not ultra/v1/order.
JUP_MANUAL = "https://api.jup.ag/swap/v2/build"
RAY_COMPUTE = "https://transaction-v1.raydium.io/compute/swap-base-in"
RAY_TX = "https://transaction-v1.raydium.io/transaction/swap-base-in"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
VENUES = ("jupiter_manual", "raydium")
VENUES_WITH_ORCA = ("jupiter_manual", "raydium", "orca")
SLIPPAGE_BPS = 30  # 0.3%. Locked 2026-10-04.
# /build omits the CU limit. Use the same estimate for the fee and the tx.
JUP_CU_EST = 200_000
# ix-3 fix (2026-10-08): 4-hop Jupiter routes plus a fresh wSOL ATA (~16k CU)
# need ~200-220k CU, so a flat 200k limit failed with ComputationalBudgetExceeded /
# ProgramFailedToComplete. At build time (send path only) simulate once with a
# high limit and size the tx limit to used*margin+pad, never below JUP_CU_EST.
# Sim error or RPC failure keeps the old behaviour (JUP_CU_EST). Read-only RPC.
JUP_CU_SIM_SIZE = True
JUP_CU_SIM_LIMIT = 1_400_000
JUP_CU_MARGIN = 1.2
JUP_CU_PAD = 10_000
# Optional one-shot bump applied inside build_jupiter_manual_tx (post ix-6 rebuilds).
_JUP_BUDGET_BUMP = None  # (cu_factor, micro_factor) or None
USDC_PAIRS = ("SOL-USDC", "USDC-cbBTC", "USDC-WETH")
OTHER_PAIRS = ("SOL-cbBTC", "SOL-WETH", "cbBTC-WETH")

_prio_micro = None


def set_jupiter_budget_bump(cu_factor=1.25, micro_factor=1.5):
    """Raise CU limit and priority micro on the next Jupiter tx build only."""
    global _JUP_BUDGET_BUMP
    _JUP_BUDGET_BUMP = (float(cu_factor), float(micro_factor))


def clear_jupiter_budget_bump():
    global _JUP_BUDGET_BUMP
    _JUP_BUDGET_BUMP = None

def curl_json(url, timeout=25, retries=4):
    last = None
    _jup429 = 0
    for i in range(retries):
        with _JK.curl_args(url) as _kh:  # jupkey1012: x-api-key for api.jup.ag (header via temp file)
            r = subprocess.run(
                ["curl", "-sS", "-w", "\n%{http_code}", "--max-time", str(timeout),
                 "-H", "Accept: application/json", *_kh, url],
                capture_output=True, text=True,
            )
        if r.returncode != 0:
            last = r.stderr.strip() or f"curl_exit_{r.returncode}"
            time.sleep(1.5 * (i + 1))
            continue
        body, _, code = r.stdout.rpartition("\n")
        code = code.strip()
        if _kh and code in ("401", "403") and _JK.fail(code, url):
            continue  # keyless retry right away
        if code == "429" or "Too many requests" in body or "Rate limit" in body:
            last = "rate_limit_429"
            if "jup.ag" in url:  # jup429once1015: Jupiter 429 -> one retry per quote, then give up
                _jup429 += 1
                if _jup429 > 1:
                    break
            time.sleep(4.0 * (i + 1))
            continue
        if code != "200":
            last = f"http_{code}:{body[:160]}"
            time.sleep(1.2 * (i + 1))
            continue
        try:
            return json.loads(body)
        except Exception as e:
            last = str(e)
            time.sleep(1.0)
    return {"error": last or "no_response"}

def post_json(url, payload, timeout=60):
    r = subprocess.run(
        ["curl", "-sS", "-w", "\n%{http_code}", "--max-time", str(timeout),
         "-H", "Accept: application/json", "-H", "Content-Type: application/json",
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

def rpc(method, params):
    """Shared order: helius, alchemy, public. Error dict on total failure (no URL)."""
    try:
        return solana_rpc.rpc(method, params, retries=1, timeout=30)
    except RuntimeError as e:
        return {"error": str(e)[:300]}

def priority_micro():
    global _prio_micro
    if _prio_micro is not None:
        return _prio_micro
    resp = rpc("getRecentPrioritizationFees", [])
    vals = []
    for row in (resp.get("result") or []):
        if isinstance(row, dict):
            vals.append(int(row.get("prioritizationFee") or 0))
    vals = [v for v in vals if v > 0] or [0]
    vals.sort()
    _prio_micro = vals[len(vals) // 2]
    return _prio_micro

# Raydium minimum priority fee (2026-10-08, owner). The shared estimate
# (median of getRecentPrioritizationFees) is usually 0 on our RPC, so Raydium
# swaps went out with no priority fee. Raydium quotes and builds use at least
# this many micro-lamports per CU. Config: cost-basis.json
# send_fees.raydium_min_priority_microlamports. Jupiter is unchanged.
RAYDIUM_MIN_PRIORITY_MICRO_DEFAULT = 35_000
_ray_floor = None


def raydium_min_priority_micro():
    global _ray_floor
    if _ray_floor is not None:
        return _ray_floor
    val = RAYDIUM_MIN_PRIORITY_MICRO_DEFAULT
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cost-basis.json")
        with open(path) as f:
            cfg = (json.load(f).get("send_fees") or {})
        raw = cfg.get("raydium_min_priority_microlamports")
        if raw is not None and int(raw) >= 0:
            val = int(raw)
    except Exception:
        pass
    _ray_floor = int(val)
    return _ray_floor


def raydium_priority_micro():
    """CU price for Raydium quotes and builds: the estimate, never below the floor."""
    return max(int(priority_micro() or 0), int(raydium_min_priority_micro()))


def network_gas_sol(cu=200_000):
    micro = priority_micro()
    lamports = 5000 + (int(micro) * int(cu)) // 1_000_000
    return lamports / 1e9

def additional_profit_floor_pct(pair):
    """Leftover required ON TOP of clamped eff%. Not a replacement for it."""
    if pair in USDC_PAIRS:
        return 0.15
    if pair in OTHER_PAIRS:
        return 0.10
    return 0.15

def fee_inclusive_edge_pct(side, basis, exec_net):
    """Edge % after the gas-adjusted executable rate. None if it cannot be priced."""
    try:
        basis = float(basis)
        exec_net = float(exec_net)
    except (TypeError, ValueError):
        return None
    if basis <= 0 or exec_net <= 0:
        return None
    if side == "sell":
        return (exec_net / basis - 1.0) * 100.0
    return (basis / exec_net - 1.0) * 100.0

def _fail(venue, note):
    return {"venue": venue, "ok": False, "note": str(note)[:240], "out_amount": None, "gas_sol": None}

def _ix_data(ix):
    if not isinstance(ix, dict) or not ix.get("data"):
        return b""
    try:
        return base64.b64decode(ix["data"])
    except Exception:
        return b""

def _cu_price_micro(ixs):
    for ix in ixs or []:
        data = _ix_data(ix)
        if len(data) >= 9 and data[0] == 3:
            return int.from_bytes(data[1:9], "little")
    return None

def _cu_limit(ixs):
    for ix in ixs or []:
        data = _ix_data(ix)
        if len(data) >= 5 and data[0] == 2:
            return int.from_bytes(data[1:5], "little")
    return None

def jupiter_price_impact_pct(payload):
    """Percent. Jupiter priceImpactPct is a ratio: "0.001" = 0.1%. None if absent."""
    if not isinstance(payload, dict) or payload.get("priceImpactPct") is None:
        return None
    try:
        return abs(float(payload["priceImpactPct"])) * 100.0
    except (TypeError, ValueError):
        return None

def raydium_price_impact_pct(swap_response):
    """Percent. Raydium compute priceImpactPct is already percent: 0.05 = 0.05%."""
    data = swap_response.get("data") if isinstance(swap_response, dict) else None
    if not isinstance(data, dict):
        return None
    raw = data.get("priceImpactPct")
    if raw is None:
        raw = data.get("priceImpact")
    if raw is None:
        return None
    try:
        return abs(float(raw))
    except (TypeError, ValueError):
        return None

def quote_price_impact_pct(row):
    """Price impact in percent from a Jupiter manual or Raydium quote, if present."""
    if not isinstance(row, dict):
        return None
    venue = str(row.get("venue") or "")
    if venue.startswith("jupiter"):
        return jupiter_price_impact_pct(row.get("payload"))
    if venue == "raydium":
        return raydium_price_impact_pct(row.get("swap_response"))
    return None

def quote_priority_fee_micro(row):
    """Priority fee in micro-lamports per CU. None if the quote does not carry one.

    Jupiter: SetComputeUnitPrice on the /build compute-budget instruction, else
    prioritizationFeeLamports converted with the quote CU limit.
    Raydium: the computeUnitPrice this quote's network fee used (stamped), else
    inverted from network_fee_sol = (5000 + micro * 250_000 // 1_000_000) / 1e9.
    Signature lamports and any Jito tip are not priority fee.
    """
    if not isinstance(row, dict):
        return None
    stamped = row.get("priority_fee_micro_lamports_per_cu")
    if stamped is not None:
        try:
            return float(stamped)
        except (TypeError, ValueError):
            return None
    venue = str(row.get("venue") or "")
    if venue.startswith("jupiter"):
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        ixs = list(payload.get("computeBudgetInstructions") or [])
        micro = _cu_price_micro(ixs)
        if micro is not None:
            return float(micro)
        if payload.get("prioritizationFeeLamports") is None:
            return None
        cu = _cu_limit(ixs) or JUP_CU_EST
        if not cu:
            return None
        return int(payload["prioritizationFeeLamports"]) * 1_000_000 / float(cu)
    if venue == "raydium":
        net = row.get("network_fee_sol")
        if net is None:
            return None
        lamports = int(round(float(net) * 1e9))
        variable = lamports - 5000
        if variable <= 0:
            return 0.0
        return variable * 1_000_000.0 / 250_000.0
    return None

def quote_signature_count(row):
    """Required signatures on this quote. These pairs are one fee-payer signature
    unless the quote states a count. The base network fee is 5000 lamports each.
    """
    if not isinstance(row, dict):
        return 1
    for key in ("num_required_signatures", "signature_count"):
        raw = row.get(key)
        if raw is None:
            continue
        try:
            n = int(raw)
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    for key in ("numRequiredSignatures", "num_required_signatures"):
        raw = payload.get(key)
        if raw is None:
            continue
        try:
            n = int(raw)
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    return 1

def quote_priority_fee_sol(row):
    """Priority fee in SOL. Not the signature fee and not a Jito tip."""
    micro = quote_priority_fee_micro(row)
    if micro is None or not isinstance(row, dict):
        return None
    venue = str(row.get("venue") or "")
    if venue.startswith("jupiter"):
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        ixs = list(payload.get("computeBudgetInstructions") or [])
        cu = _cu_limit(ixs) or JUP_CU_EST
    else:
        cu = RAYDIUM_QUOTE_CU
    lamports = (int(round(float(micro))) * int(cu)) // 1_000_000
    return lamports / 1e9

def quote_jito_tip_sol(row):
    """Jito tip in SOL. Zero when the quote has no tip."""
    if not isinstance(row, dict):
        return 0.0
    if row.get("jito_tip_sol") is not None:
        try:
            return max(0.0, float(row["jito_tip_sol"]))
        except (TypeError, ValueError):
            return 0.0
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    return jito_tip_lamports(payload.get("tipInstruction")) / 1e9

def normal_signature_fee_sol(row, base_lamports=BASE_SIGNATURE_LAMPORTS):
    """Base signature fee only: 5000 lamports times signatures. Not the tip."""
    n = quote_signature_count(row)
    return int(base_lamports) * int(n) / 1e9

def attached_tip_sol(row):
    """Priority fee plus any Jito tip, in SOL.

    None when the quote states neither a priority fee nor a Jito tip.
    A missing tip does not invent a spike.
    """
    pri = quote_priority_fee_sol(row)
    jito = quote_jito_tip_sol(row)
    if pri is None and jito <= 0:
        return None
    return float(pri or 0.0) + float(jito or 0.0)

def fee_spike_reason(row, tip_multiple=2.0, base_lamports=BASE_SIGNATURE_LAMPORTS):
    """Spike when the attached tip is more than tip_multiple times the signature fee.

    Strictly greater. Exactly 2x does not spike. The 3x median and the
    micro-lamport ceiling are not this test.
    """
    tip = attached_tip_sol(row)
    if tip is None:
        return None
    normal = normal_signature_fee_sol(row, base_lamports)
    if normal <= 0:
        return None
    mult = float(tip_multiple)
    if tip > mult * normal:
        return (
            f"attached_tip_sol={tip:.8g}>{mult:g}x_signature_fee_sol={normal:.8g}"
        )
    return None

def jito_tip_lamports(ix):
    """SOL transfer tip, if the quote included one. Zero when there is no tip."""
    if not isinstance(ix, dict):
        return 0
    if ix.get("programId") != SYSTEM_PROGRAM:
        return 0
    data = _ix_data(ix)
    if len(data) < 12 or int.from_bytes(data[:4], "little") != 2:
        return 0
    return int.from_bytes(data[4:12], "little")

def sol_network_and_tip(payload, fallback_cu=JUP_CU_EST):
    """Signature + priority fee, plus a Jito tip only when the quote has one."""
    if not isinstance(payload, dict):
        return network_gas_sol(fallback_cu), 0.0
    # Prefer explicit lamport fields when a quote still looks like Ultra.
    sig = payload.get("signatureFeeLamports")
    pri = payload.get("prioritizationFeeLamports")
    if sig is not None or pri is not None:
        net = (int(sig or 0) + int(pri or 0)) / 1e9
        if net <= 0:
            net = network_gas_sol(fallback_cu)
    else:
        ixs = list(payload.get("computeBudgetInstructions") or [])
        micro = _cu_price_micro(ixs)
        cu = _cu_limit(ixs) or int(fallback_cu)
        if micro is None:
            net = network_gas_sol(cu)
        else:
            net = (5000 + (int(micro) * int(cu)) // 1_000_000) / 1e9
    tip = jito_tip_lamports(payload.get("tipInstruction")) / 1e9
    return net, tip

def is_ultra_labeled(row):
    """True when the quote is still an Ultra route, whatever we asked for."""
    if not isinstance(row, dict):
        return False
    if row.get("labeled_ultra"):
        return True
    venue = str(row.get("venue") or "")
    if "ultra" in venue.lower():
        return True
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    mode = str(payload.get("mode") or "")
    if mode.lower() == "ultra":
        return True
    router = str(payload.get("router") or "")
    if router.lower() == "ultra":
        return True
    return False

def platform_fee_bps(payload):
    """Jupiter platform fee in bps, or None when the quote does not state it."""
    if not isinstance(payload, dict):
        return None
    pf = payload.get("platformFee")
    if isinstance(pf, dict) and pf.get("feeBps") is not None:
        try:
            return int(pf["feeBps"])
        except (TypeError, ValueError):
            return None
    for key in ("feeBps", "platformFeeBps"):
        if payload.get(key) is not None:
            try:
                return int(payload[key])
            except (TypeError, ValueError):
                return None
    return None

def adjust_for_ultra_fee(row):
    """Ultra-labeled quotes: take Jupiter's platform fee out of the output.

    A missing fee skips that quote. Manual quotes (platformFeeBps=0, not
    labeled Ultra) are unchanged. Idempotent.
    """
    if not isinstance(row, dict) or not row.get("ok") or row.get("ultra_fee_applied"):
        return row
    if not is_ultra_labeled(row):
        return row
    bps = platform_fee_bps(row.get("payload"))
    row = dict(row)
    row["ultra_fee_applied"] = True
    row["labeled_ultra"] = True
    if bps is None or bps < 0 or bps > 10000:
        row["ok"] = False
        row["note"] = "ultra_platform_fee_missing" if bps is None else "ultra_platform_fee_invalid"
        row["out_amount"] = None
        row["platform_fee_bps"] = None
        return row
    out = int(row["out_amount"])
    row["out_amount_before_ultra_fee"] = out
    row["platform_fee_bps"] = int(bps)
    row["platform_fee_pct"] = int(bps) / 100.0
    # bps off the output removes that fee from the edge before the profit test.
    row["out_amount"] = out * (10000 - int(bps)) // 10000
    return row

def quote_jupiter_manual(sell, buy, amount_base_units, slip_bps=None):
    """Market swap quote. platformFeeBps=0 forces no Jupiter commission."""
    if slip_bps is None:
        slip_bps = _AS.bps(sell, buy)
    slip_bps = min(int(slip_bps), SLIPPAGE_BPS)  # never above 30
    venue = "jupiter_manual"
    params = (
        f"inputMint={MINTS[sell]}&outputMint={MINTS[buy]}"
        f"&amount={int(amount_base_units)}&taker={ADDR}"
        f"&slippageBps={int(slip_bps)}&platformFeeBps=0"
    )
    q = curl_json(f"{JUP_MANUAL}?{params}")
    if not isinstance(q, dict) or not q.get("outAmount"):
        err = q.get("error") if isinstance(q, dict) else "no_response"
        if isinstance(q, dict) and not err:
            err = q.get("errorMessage") or q.get("message") or "no_outAmount"
        return _fail(venue, err)
    # Commission must be the one we requested. Refuse a positive integrator fee.
    stated = platform_fee_bps(q)
    if stated not in (None, 0):
        return _fail(venue, f"jupiter_platform_fee_not_zero:{stated}")
    mode = str(q.get("mode") or "")
    labeled_ultra = mode.lower() == "ultra" or str(q.get("router") or "").lower() == "ultra"
    net, tip = sol_network_and_tip(q)
    if q.get("taker") and q.get("taker") != ADDR:
        return _fail(venue, "taker_mismatch")
    ixs = list(q.get("computeBudgetInstructions") or [])
    micro = _cu_price_micro(ixs)
    if micro is None and q.get("prioritizationFeeLamports") is not None:
        cu = _cu_limit(ixs) or JUP_CU_EST
        micro = int(q["prioritizationFeeLamports"]) * 1_000_000 // int(cu) if cu else None
    return adjust_for_ultra_fee({
        "venue": venue,
        "ok": True,
        "note": None,
        "labeled_ultra": labeled_ultra,
        "out_amount": int(q["outAmount"]),
        "slip_bps": int(slip_bps),
        "gas_sol": net + tip,
        "network_fee_sol": net,
        "jito_tip_sol": tip,
        "price_impact_pct": jupiter_price_impact_pct(q),
        "priority_fee_micro_lamports_per_cu": None if micro is None else int(micro),
        "platform_fee_bps": 0 if not labeled_ultra else stated,
        "request_id": None,
        "tx_b64": None,
        "payload": q,
        "quote_endpoint": JUP_MANUAL,
        "platform_fee_bps_requested": 0,
    })

def quote_orca(sell, buy, amount_base_units, slip_bps=None):
    """Orca Whirlpool quote via Jupiter build restricted to dexes=Whirlpool.

    Orca has no public REST swap/quote endpoint. This returns venue="orca" with
    the same payload shape as jupiter_manual so build_jupiter_manual_tx works.
    Failures return ok=False (caller logs and ignores silently).
    """
    if slip_bps is None:
        slip_bps = _AS.bps(sell, buy)
    slip_bps = min(int(slip_bps), SLIPPAGE_BPS)  # never above 30
    venue = "orca"
    params = (
        f"inputMint={MINTS[sell]}&outputMint={MINTS[buy]}"
        f"&amount={int(amount_base_units)}&taker={ADDR}"
        f"&slippageBps={int(slip_bps)}&platformFeeBps=0"
        f"&dexes=Whirlpool"
    )
    q = curl_json(f"{JUP_MANUAL}?{params}")
    if not isinstance(q, dict) or not q.get("outAmount"):
        err = q.get("error") if isinstance(q, dict) else "no_response"
        if isinstance(q, dict) and not err:
            err = q.get("errorMessage") or q.get("message") or "no_outAmount"
        return _fail(venue, err)
    stated = platform_fee_bps(q)
    if stated not in (None, 0):
        return _fail(venue, f"orca_platform_fee_not_zero:{stated}")
    # Require every hop to be Whirlpool; otherwise this is not an Orca quote.
    for hop in (q.get("routePlan") or []):
        label = str(((hop or {}).get("swapInfo") or {}).get("label") or "")
        if label and "whirlpool" not in label.lower() and "orca" not in label.lower():
            return _fail(venue, f"orca_non_whirlpool_hop:{label}")
    mode = str(q.get("mode") or "")
    labeled_ultra = mode.lower() == "ultra" or str(q.get("router") or "").lower() == "ultra"
    net, tip = sol_network_and_tip(q)
    if q.get("taker") and q.get("taker") != ADDR:
        return _fail(venue, "taker_mismatch")
    ixs = list(q.get("computeBudgetInstructions") or [])
    micro = _cu_price_micro(ixs)
    if micro is None and q.get("prioritizationFeeLamports") is not None:
        cu = _cu_limit(ixs) or JUP_CU_EST
        micro = int(q["prioritizationFeeLamports"]) * 1_000_000 // int(cu) if cu else None
    return adjust_for_ultra_fee({
        "venue": venue,
        "ok": True,
        "note": None,
        "labeled_ultra": labeled_ultra,
        "out_amount": int(q["outAmount"]),
        "slip_bps": int(slip_bps),
        "gas_sol": net + tip,
        "network_fee_sol": net,
        "jito_tip_sol": tip,
        "price_impact_pct": jupiter_price_impact_pct(q),
        "priority_fee_micro_lamports_per_cu": None if micro is None else int(micro),
        "platform_fee_bps": 0 if not labeled_ultra else stated,
        "request_id": None,
        "tx_b64": None,
        "payload": q,
        "quote_endpoint": JUP_MANUAL,
        "platform_fee_bps_requested": 0,
        "orca_via": "jupiter_build_dexes_Whirlpool",
    })

def quote_raydium(sell, buy, amount_base_units, slip_bps=None):
    if slip_bps is None:
        slip_bps = _AS.bps(sell, buy)
    slip_bps = min(int(slip_bps), SLIPPAGE_BPS)  # never above 30
    venue = "raydium"
    url = (
        f"{RAY_COMPUTE}?inputMint={MINTS[sell]}&outputMint={MINTS[buy]}"
        f"&amount={int(amount_base_units)}&slippageBps={int(slip_bps)}&txVersion=V0"
    )
    q = curl_json(url)
    if not isinstance(q, dict):
        return _fail(venue, "bad_response")
    if q.get("error") and not q.get("success"):
        return _fail(venue, q.get("error"))
    if not q.get("success"):
        return _fail(venue, q.get("msg") or q.get("error") or "raydium_unsuccessful")
    data = q.get("data") or {}
    out = data.get("outputAmount")
    if out is None:
        return _fail(venue, "no_outputAmount")
    # Raydium compute does not attach a Jito tip. Network fee only.
    # Priority fee is the CU price this quote is priced and later built with.
    micro = int(raydium_priority_micro())
    cu = 250_000
    gas = (5000 + (micro * cu) // 1_000_000) / 1e9
    return {
        "venue": venue,
        "ok": True,
        "note": None,
        "out_amount": int(out),
        "slip_bps": int(slip_bps),
        "gas_sol": gas,
        "network_fee_sol": gas,
        "jito_tip_sol": 0.0,
        "swap_response": q,
        "other_amount_threshold": data.get("otherAmountThreshold"),
        "price_impact_pct": raydium_price_impact_pct(q),
        "priority_fee_micro_lamports_per_cu": micro,
    }

def _pk(s):
    if Pubkey is None:
        raise RuntimeError(f"solders_import:{_SOLDERS_ERR}")
    return Pubkey.from_string(s)

def ata(owner, mint, token_program=None):
    prog = token_program or TOKEN_PROGRAMS.get(mint) or TOKEN_PROGRAM
    pda, _ = Pubkey.find_program_address(
        [bytes(_pk), bytes(_pk(prog)), bytes(_pk(mint))],
        _pk(ATA_PROGRAM),
    )
    return str(pda)

def _api_ix(ix):
    metas = []
    for acc in (ix.get("accounts") or []):
        metas.append(AccountMeta(_pk(acc["pubkey"]), bool(acc.get("isSigner")), bool(acc.get("isWritable"))))
    return Instruction(_pk(ix["programId"]), _ix_data(ix), metas)

def _cu_limit_ix(units):
    data = bytes([2]) + int(units).to_bytes(4, "little")
    return Instruction(_pk(COMPUTE_BUDGET), data, [])

def _cu_price_ix(micro):
    data = bytes([3]) + int(micro).to_bytes(8, "little")
    return Instruction(_pk(COMPUTE_BUDGET), data, [])


def _lend_ensure(input_mint, raw_amount):
    """lend1015: before a swap that spends USDC is built, withdraw from Jupiter Lend if the
    liquid USDC is short (no-op unless this process read a lent position). Never raises."""
    try:
        if input_mint and raw_amount:
            import degen_lend
            degen_lend.ensure_liquid(input_mint, int(raw_amount))
            import sol_stake  # stake1015: SOL short -> swap bucket JitoSOL back first
            sol_stake.ensure_liquid(input_mint, int(raw_amount))
    except Exception as _e:
        print(f"LEND_ERR build_hook {type(_e).__name__}", flush=True)

def build_jupiter_manual_tx(payload):
    """Assemble the /build instructions. Does not sign or send."""
    if not isinstance(payload, dict) or not payload.get("swapInstruction"):
        return None, "jupiter_manual_no_swap_ix"
    _lend_ensure(payload.get("inputMint"), payload.get("inAmount"))
    if _SOLDERS_ERR is not None or MessageV0 is None:
        return None, f"solders_import:{_SOLDERS_ERR}"
    bh_meta = payload.get("blockhashWithMetadata") or {}
    raw_bh = bh_meta.get("blockhash")
    if not isinstance(raw_bh, list) or len(raw_bh) != 32:
        return None, "jupiter_manual_no_blockhash"
    cu_ixs = list(payload.get("computeBudgetInstructions") or [])
    bump = _JUP_BUDGET_BUMP
    api_cu = _cu_limit(cu_ixs)
    base_micro = _cu_price_micro(cu_ixs)

    def _budget_ixs(base_cu, force_limit=None):
        out = []
        if bump is not None or force_limit is not None:
            if force_limit is not None:
                lim = int(force_limit)
            else:
                cu_f, _mf = bump
                lim = max(int(base_cu), int(round(int(base_cu) * float(cu_f))))
            out.append(_cu_limit_ix(lim))
            if bump is not None:
                micro_f = bump[1]
                # Replace any API CU-limit / CU-price ixs with bumped ones.
                if base_micro is not None:
                    bumped_micro = max(int(base_micro), int(round(int(base_micro) * float(micro_f))))
                else:
                    bumped_micro = max(int(priority_micro() or 0), int(round(1000 * float(micro_f))))
                out.append(_cu_price_ix(bumped_micro))
                skip_tags = (2, 3)
            else:
                skip_tags = (2,)
            for ix in cu_ixs:
                data = _ix_data(ix)
                # Skip original CU limit (tag 2) and, when bumped, CU price (tag 3).
                if len(data) >= 1 and data[0] in skip_tags:
                    continue
                out.append(_api_ix(ix))
        else:
            if api_cu is None:
                out.append(_cu_limit_ix(int(base_cu)))
            for ix in cu_ixs:
                out.append(_api_ix(ix))
        return out

    body = []
    for ix in payload.get("setupInstructions") or []:
        body.append(_api_ix(ix))
    body.append(_api_ix(payload["swapInstruction"]))
    if payload.get("cleanupInstruction"):
        body.append(_api_ix(payload["cleanupInstruction"]))
    for ix in payload.get("otherInstructions") or []:
        body.append(_api_ix(ix))
    if payload.get("tipInstruction"):
        body.append(_api_ix(payload["tipInstruction"]))
    alts = []
    for key, addrs in (payload.get("addressesByLookupTableAddress") or {}).items():
        alts.append(AddressLookupTableAccount(_pk(key), [_pk(a) for a in addrs]))

    def _compile(ixs):
        msg = MessageV0.try_compile(
            _pk(ADDR),
            ixs,
            alts,
            Hash.from_bytes(bytes(raw_bh)),
        )
        nreq = int(msg.header.num_required_signatures)
        tx = VersionedTransaction.populate(msg, [Signature.default() for _ in range(nreq)])
        return base64.b64encode(bytes(tx)).decode()

    base_cu = api_cu or JUP_CU_EST
    # Size the CU limit by simulation when the API gave none (or a bump rebuild).
    if JUP_CU_SIM_SIZE and (api_cu is None or bump is not None):
        try:
            sized = jupiter_sim_cu(_compile(_budget_ixs(base_cu, force_limit=JUP_CU_SIM_LIMIT) + body))
        except Exception as sim_e:
            sized = None
            print(f"JUP_CU_SIM_ERR {type(sim_e).__name__}", flush=True)
        if sized:
            base_cu = max(int(base_cu), int(sized))
    try:
        return _compile(_budget_ixs(base_cu) + body), None
    except Exception as e:
        return None, f"jupiter_manual_tx_build:{type(e).__name__}:{e}"[:200]


def jupiter_sim_cu(tx_b64):
    """Simulate (no signature check, no send) and return a sized CU limit or None.

    None when the simulation errors or the RPC fails, so callers keep JUP_CU_EST.
    """
    res = rpc("simulateTransaction", [tx_b64, {
        "encoding": "base64", "sigVerify": False,
        "replaceRecentBlockhash": True, "commitment": "confirmed",
    }])
    val = ((res or {}).get("result") or {}).get("value") if isinstance(res, dict) else None
    if not isinstance(val, dict):
        print("JUP_CU_SIM no_result", flush=True)
        return None
    units = val.get("unitsConsumed")
    if val.get("err") is not None or not units:
        print(f"JUP_CU_SIM err={str(val.get('err'))[:80]} units={units}", flush=True)
        return None
    sized = min(JUP_CU_SIM_LIMIT, int(int(units) * JUP_CU_MARGIN) + JUP_CU_PAD)
    print(f"JUP_CU_SIM units={units} limit={max(sized, JUP_CU_EST)}", flush=True)
    return sized

def orca_needed(jup_row):
    """True when the Jupiter manual row is missing, not ok, or has no output amount."""
    if not isinstance(jup_row, dict) or not jup_row.get("ok"):
        return True
    try:
        return not int(jup_row.get("out_amount") or 0) > 0
    except (TypeError, ValueError):
        return True


def fetch_venue_quotes(sell, buy, amount_base_units, include_orca=False):
    """Quote Jupiter manual and Raydium together; optionally Orca (Whirlpool).

    One venue failing does not fail the others. The priority-fee RPC is warmed
    first so Raydium does not wait on it alone. Orca failures are ok=False rows
    (caller may ignore silently).
    """
    try:
        priority_micro()
    except Exception:
        pass
    fns = [quote_jupiter_manual, quote_raydium]
    names = list(VENUES)
    out = []
    with ThreadPoolExecutor(max_workers=len(fns)) as pool:
        futs = [pool.submit(fn, sell, buy, amount_base_units) for fn in fns]
        for fut in futs:
            try:
                out.append(fut.result())
            except Exception as e:
                out.append(_fail("unknown", f"{type(e).__name__}:{e}"))
    by = {row.get("venue"): row for row in out}
    # orcafallback1012: Orca (top-1 only) is quoted ONLY when the Jupiter manual quote failed or returned nothing.
    if include_orca and orca_needed(by.get("jupiter_manual")):
        try:
            by["orca"] = quote_orca(sell, buy, amount_base_units)
        except Exception as e:
            by["orca"] = _fail("orca", f"{type(e).__name__}:{e}")
        names = list(VENUES_WITH_ORCA)
    rows = []
    for name in names:
        row = by.get(name) or _fail(name, "missing")
        rows.append(adjust_for_ultra_fee(row))
    return rows

def build_orca_tx(payload):
    """Assemble an Orca (Whirlpool-via-Jupiter-build) tx. Same as jupiter manual."""
    return build_jupiter_manual_tx(payload)

def build_raydium_tx(swap_response, sell, buy):
    _lend_ensure(MINTS.get(sell), ((swap_response or {}).get("data") or {}).get("inputAmount"))
    body = {
        "wallet": ADDR,
        "swapResponse": swap_response,
        "txVersion": "V0",
        "computeUnitPriceMicroLamports": str(raydium_priority_micro()),
        "wrapSol": sell == "SOL",
        "unwrapSol": buy == "SOL",
    }
    if sell != "SOL":
        body["inputAccount"] = ata(ADDR, MINTS[sell])
    if buy != "SOL":
        body["outputAccount"] = ata(ADDR, MINTS[buy])
    resp = post_json(RAY_TX, body)
    if not isinstance(resp, dict):
        return None, "raydium_tx_bad"
    if resp.get("error") and not resp.get("success"):
        return None, str(resp.get("error"))[:200]
    if resp.get("success") is False:
        return None, str(resp.get("msg") or resp.get("error") or "raydium_tx_failed")[:200]
    data = resp.get("data")
    txs = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("transaction"):
                txs.append(item["transaction"])
            elif isinstance(item, str):
                txs.append(item)
    elif isinstance(data, dict) and data.get("transaction"):
        txs.append(data["transaction"])
    elif isinstance(data, str):
        txs.append(data)
    if not txs:
        return None, f"raydium_tx_empty:{str(resp)[:160]}"
    return txs, None


def replace_recent_blockhash(tx_b64, blockhash_b58):
    """Swap the recent blockhash on an unsigned versioned transaction.

    Jupiter /build and Raydium's transaction API do not take a caller
    blockhash. The compiled message is patched and left unsigned so the
    caller can sign it for the RPC that just produced this blockhash.
    A durable nonce is left unchanged.
    """
    if _SOLDERS_ERR is not None or MessageV0 is None or Hash is None:
        raise RuntimeError(f"solders_import:{_SOLDERS_ERR}")
    raw = base64.b64decode(tx_b64)
    vtx = VersionedTransaction.from_bytes(raw)
    if vtx.uses_durable_nonce():
        return tx_b64
    msg = vtx.message
    new_hash = Hash.from_string(str(blockhash_b58))
    payload = json.loads(msg.to_json())
    payload["recentBlockhash"] = list(bytes(new_hash))
    blob = json.dumps(payload)
    if isinstance(msg, MessageV0):
        new_msg = MessageV0.from_json(blob)
    else:
        from solders.message import Message
        new_msg = Message.from_json(blob)
    nreq = int(new_msg.header.num_required_signatures)
    blank = VersionedTransaction.populate(new_msg, [Signature.default() for _ in range(nreq)])
    return base64.b64encode(bytes(blank)).decode()
