"""Raydium fallback after a finished Jupiter send. Locked 2026-10-04.

Call this only after the Jupiter attempt has already finished its Helius
send and, when that failed, its one Alchemy re-quote. Do not call it
instead of that Alchemy retry. Other errors, rejections, and unconfirmed
sends do not come here.

A Raydium quote that misses the profit check is not sent and is not a
second failure — unless force_path and Raydium gap_net is best vs the
failed Jupiter attempt (rescue only; primary edge % rules unchanged).
The send callback is the existing Helius-then-Alchemy helper, invoked
once. This module does not quote, sign, or send.
"""

# RPC text for a Jupiter router abort in simulation, including
# "Error processing Instruction 6: Program failed to complete" (code -32002).
PROGRAM_FAILED = "Program failed to complete"

# Slight budget bump on Jupiter rebuilds after an ix-6 / program-fail (Alchemy
# re-quote). CU and priority micro only — does not change edge % rules.
IX6_CU_FACTOR = 1.25
IX6_MICRO_FACTOR = 1.5


def program_failed_to_complete(err):
    """True only when the finished send error contains the locked trigger."""
    return PROGRAM_FAILED in str(err or "")


def skip_jupiter_when_prefer_raydium(prefer_raydium, venue):
    """When Raydium-first is on, never select Jupiter (incl. reverses)."""
    if not prefer_raydium:
        return False
    return str(venue or "").startswith("jupiter")


def alchemy_rebuild_mode(prefer_raydium, venue):
    """When Raydium-first is already on, Alchemy must not rebuild Jupiter.

    Returns "raydium_only" or "fresh_route". Locked 2026-10-06 after SOL-WETH
    ix-6 aborts that Alchemy-requoted Jupiter while prefer_raydium was True.
    """
    if prefer_raydium and str(venue or "").startswith("jupiter"):
        return "raydium_only"
    return "fresh_route"


def should_bump_jupiter_budget(prefer_raydium, venue, consecutive_program_fails=0):
    """Bump CU/priority on Jupiter Alchemy rebuilds after at least one ix-6."""
    if prefer_raydium:
        return False
    if not str(venue or "").startswith("jupiter"):
        return False
    return int(consecutive_program_fails or 0) >= 1


def ix6_budget_factors():
    """(cu_factor, micro_factor) for post-ix6 Jupiter rebuilds."""
    return (IX6_CU_FACTOR, IX6_MICRO_FACTOR)


def force_raydium_ok(clears_net, force_path, ray_gap_net, jup_gap_net):
    """Raydium rescue after ix-6: clears, or force-path with gap_net best.

    Primary edge % selection rules are unchanged. force_path only applies to
    the post-ix-6 same-size Raydium rescue when Raydium's gap_net is at least
    as good as the failed Jupiter attempt.
    """
    if clears_net:
        return True
    if not force_path:
        return False
    try:
        return float(ray_gap_net) + 1e-12 >= float(jup_gap_net)
    except (TypeError, ValueError):
        return False


def consider_raydium(err, venue, prepare, send):
    """One Raydium re-quote, then one send only when that quote clears.

    prepare() fetches the fresh Raydium quote and applies the fill gates
    (required percent, leftover floor, price-impact cap, priority-fee
    spike, no-fade, size caps). It returns a payload to send, or None
    when the quote does not clear. None does not send and is not a failure.

    send(payload) is the existing send helper (Helius, then one Alchemy
    re-quote if needed). It is not called again if that send fails.
    """
    if not str(venue or "").startswith("jupiter"):
        return {"action": "ignore"}
    if not program_failed_to_complete(err):
        return {"action": "ignore"}
    payload = prepare()
    if not payload:
        return {"action": "skip"}
    sig, err_s, uncertain = send(payload)
    return {
        "action": "sent",
        "signature": sig,
        "error": err_s,
        "uncertain": bool(uncertain),
        "info": payload if isinstance(payload, dict) else None,
    }
