"""Reversal record fields for trades.jsonl rows (revflag 2026-10-11, owner). Record-only:
nothing here feeds a trading decision. A reversal fill gets {"rev": 1, "rev_of": <original tx>}."""


from __future__ import annotations


def rev_fields(cand) -> dict:
    if isinstance(cand, dict) and cand.get("kind") == "reverse":
        out = {"rev": 1}
        if cand.get("fill_tx"):
            out["rev_of"] = cand["fill_tx"]
        return out
    return {}


def reversal_cap(kind, base_cap: float, cb: dict | None = None) -> float:
    """dailyv2c 2026-10-11: fraction of a prior fill a reverse may close.
    daily_v2 fills: min(base, daily_rebalance_v2.reversal_cap_pct/100, default 50%); others: base."""
    if str(kind or "").strip().lower() == "daily_v2":
        try:
            pct = float(((cb or {}).get("daily_rebalance_v2") or {}).get("reversal_cap_pct", 50))
        except (TypeError, ValueError):
            pct = 50.0
        return max(0.0, min(float(base_cap), pct / 100.0))
    return float(base_cap)
