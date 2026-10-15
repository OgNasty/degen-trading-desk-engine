"""Edge-check policies locked 2026-10-05. Pure helpers, no network.

#5  Quote only the closest 5 routes: rank every basis route and every
    unreversed-window reverse by an ESTIMATED fee-inclusive gap (USD marks,
    no venue call), then Jupiter+Raydium quote at most the top 5 (quote_cap.top_n).
#7  Raydium first on pairs whose Jupiter sends keep dying with
    "Program failed to complete" (instruction 6 / 3 router aborts).
#8  Size tilt per pair by that pair's realized P&L% (pair_pnl.py); an up
    pair gets raised caps (18.75% portfolio / 50% balance) for that trade.
#10 Cap Alchemy fallback sends: 1 per directed route attempt, 2 per check.

This module does not quote, sign, or send. Tests: test_edge_policies.py.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timedelta

# ---------------------------------------------------------------- #5 quote cap
QUOTE_TOP_N = 5  # quote5 2026-10-11: was 3


def _est_key(c):
    try:
        v = float(c.get("est_gap_net_pct"))
        if math.isnan(v):
            raise ValueError
        has = True
    except (TypeError, ValueError):
        v, has = -math.inf, False
    # Higher estimated gap_net = closer to clearing. Reverse wins an exact tie
    # (same tie-break direction as friday_rsi.candidate_profit_key).
    return (has, v, 1 if c.get("kind") == "reverse" else 0, str(c.get("route") or ""))


def mark_buy_amount(sell_amt, px_sold, px_bought):
    """Mid-mark output for ranking only. None when a mark is missing."""
    try:
        s, a, b = float(sell_amt), float(px_sold), float(px_bought)
    except (TypeError, ValueError):
        return None
    if s <= 0 or a <= 0 or b <= 0:
        return None
    return s * a / b


# ------------------------------------------------------ #8 per-pair P&L tilt
# Replaces the portfolio-wide funded-P&L tilt (2026-10-05, owner). The tilt is
# per pair, from that pair's own realized P&L% (pair_pnl.py, pair-pnl-state.json).
PAIR_TILT_DEFAULTS = {
    "enabled": True,
    "up_mult": 1.25,
    "down_mult": 0.75,
    "flat_band_pct": 0.25,
    # An UP pair's caps for that trade (normal: 15% / 40%). SOL buffer unchanged.
    "up_max_trade_portfolio_pct": 18.75,
    "up_max_sell_fraction_of_balance": 0.50,
}


def pair_tilt_cfg(cb):
    raw = (cb or {}).get("pair_pnl_size_tilt") if isinstance(cb, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    out = dict(PAIR_TILT_DEFAULTS)
    for k in PAIR_TILT_DEFAULTS:
        if k in raw:
            out[k] = raw[k]
    return out


def size_tilt_multiplier(pnl_pct, cfg=None):
    """(multiplier, label). label: up, down, flat, unknown, off.

    P&L% > +flat_band -> up_mult; P&L% < -flat_band -> down_mult; else 1.0.
    Missing P&L -> 1.0.
    """
    cfg = cfg or PAIR_TILT_DEFAULTS
    if not cfg.get("enabled", True):
        return 1.0, "off"
    if pnl_pct is None:
        return 1.0, "unknown"
    try:
        p = float(pnl_pct)
    except (TypeError, ValueError):
        return 1.0, "unknown"
    band = abs(float(cfg.get("flat_band_pct", 0.25)))
    if p > band:
        return float(cfg.get("up_mult", 1.25)), "up"
    if p < -band:
        return float(cfg.get("down_mult", 0.75)), "down"
    return 1.0, "flat"


def pair_tilt_multiplier(pair_pnl_pct, fills=None, cfg=None):
    """Per-pair tilt. A pair with no fills is x1.0 ("no_fills")."""
    cfg = cfg or PAIR_TILT_DEFAULTS
    if not cfg.get("enabled", True):
        return 1.0, "off"
    if fills is not None and int(fills) <= 0:
        return 1.0, "no_fills"
    return size_tilt_multiplier(pair_pnl_pct, cfg)


def pair_size_caps(label, size_lim, cfg=None):
    """(max_trade_portfolio_pct, max_sell_fraction_of_balance, min_sol_fee_buffer).

    Up pair -> raised caps (18.75% / 50%); everything else -> normal caps
    from cost-basis size_limits (15% / 40%). The SOL buffer never changes.
    """
    cfg = cfg or PAIR_TILT_DEFAULTS
    size_lim = size_lim or {}
    pct = float(size_lim.get("max_trade_portfolio_pct", 15))
    frac = float(size_lim.get("max_sell_fraction_of_balance", 0.4))
    buf = float(size_lim.get("min_sol_fee_buffer", 0.02))
    if label == "up" and cfg.get("enabled", True):
        pct = float(cfg.get("up_max_trade_portfolio_pct", 18.75))
        frac = float(cfg.get("up_max_sell_fraction_of_balance", 0.50))
    return pct, frac, buf


def clamp_to_caps(amount, sold, bal, px_usd, portfolio_usd, caps):
    """Clamp a sale to caps=(portfolio_pct, balance_frac, sol_buffer). Returns (amt, clamped)."""
    pct, frac, buf = caps
    amt = max(0.0, float(amount or 0))
    bal = max(0.0, float(bal or 0))
    lim = bal * float(frac)
    try:
        px = float(px_usd or 0)
    except (TypeError, ValueError):
        px = 0.0
    lim = min(lim, (max(0.0, float(portfolio_usd or 0)) * float(pct) / 100.0) / px) if px > 0 else 0.0
    if sold == "SOL":
        lim = min(lim, max(0.0, bal - float(buf)))
    lim = max(0.0, lim)
    if amt > lim + 1e-12:
        return lim, True
    return amt, False


def apply_pair_size_tilt(base_amount, mult, label, sold, bal, px_usd, portfolio_usd, size_lim, cfg=None):
    """base (normal caps) -> x mult -> clamp to this pair's caps (raised if up).

    Returns (amount, clamped, caps). profit_sale_size_cut is applied by the
    caller AFTER this, unchanged.
    """
    caps = pair_size_caps(label, size_lim, cfg)
    amt = max(0.0, float(base_amount or 0)) * float(mult)
    amt, clamped = clamp_to_caps(amt, sold, bal, px_usd, portfolio_usd, caps)
    return amt, clamped, caps


# ------------------------------------------------- #7 Raydium-first per pair
RAYDIUM_PREFER_DEFAULTS = {
    "fail_threshold": 2,      # consecutive Jupiter program fails, no Jupiter success between
    "window_hours": 24.0,     # newest such fail must be this recent
}
PROGRAM_FAILED = "Program failed to complete"


def _parse_ts(s):
    try:
        return datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


class RaydiumPreferState:
    """Tiny persisted per-pair counter of Jupiter 'Program failed to complete' sends."""

    def __init__(self, path, data=None):
        self.path = path
        self.data = data if isinstance(data, dict) else {}
        self.data.setdefault("pairs", {})
        for k, v in RAYDIUM_PREFER_DEFAULTS.items():
            self.data.setdefault(k, v)
        self.data.setdefault(
            "rule",
            "Per pair: after fail_threshold consecutive Jupiter sends that end in "
            "'Program failed to complete' (instruction-6/3 router abort) with no Jupiter "
            "success in between, and the newest such fail within window_hours, the edge "
            "check uses Raydium-first on that pair and NEVER sends Jupiter (incl. reverses) "
            "while prefer_raydium is on. A Jupiter success on the pair resets the count. "
            "Other pairs: Jupiter-first plus the existing one-shot Raydium fallback after "
            "ix-6. Locked 2026-10-06.",
        )

    @classmethod
    def load(cls, path):
        data = None
        try:
            with open(path) as fh:
                data = json.load(fh)
        except Exception:
            data = None
        return cls(path, data)

    def save(self):
        d = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(prefix=".atomic-", suffix=".json", dir=d)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(self.data, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _row(self, pair):
        return self.data["pairs"].setdefault(str(pair), {"consecutive_jupiter_program_fails": 0})

    def record_jupiter_result(self, pair, err, when_iso, signature=None):
        """Update after one finished Jupiter send on pair. Returns the new row."""
        row = self._row(pair)
        if signature and not err:
            row["consecutive_jupiter_program_fails"] = 0
            row["last_jupiter_success_pt"] = when_iso
        elif PROGRAM_FAILED in str(err or ""):
            row["consecutive_jupiter_program_fails"] = int(row.get("consecutive_jupiter_program_fails") or 0) + 1
            row["last_jupiter_program_fail_pt"] = when_iso
            row["total_jupiter_program_fails"] = int(row.get("total_jupiter_program_fails") or 0) + 1
        # Other Jupiter errors neither count nor reset.
        self.data["updated_pt"] = when_iso
        return row

    def record_raydium_fill(self, pair, when_iso):
        row = self._row(pair)
        row["last_raydium_fill_pt"] = when_iso
        self.data["updated_pt"] = when_iso
        return row

    def prefer_raydium(self, pair, now):
        row = (self.data.get("pairs") or {}).get(str(pair)) or {}
        n = int(row.get("consecutive_jupiter_program_fails") or 0)
        if n < int(self.data.get("fail_threshold", 2)):
            return False
        last = _parse_ts(row.get("last_jupiter_program_fail_pt"))
        if last is None or now is None:
            return False
        try:
            age = now - last
        except TypeError:
            return False
        return age <= timedelta(hours=float(self.data.get("window_hours", 24.0)))


def order_venues(scored, prefer_raydium, venue_rank):
    """Venue try-order for one route. Default: best gap_net, Jupiter on a tie.

    prefer_raydium: every Raydium row first, then the rest by gap_net.
    """
    def base(s):
        return (s["gap_net"], venue_rank.get(s.get("venue"), 0))
    if not prefer_raydium:
        return sorted(scored, key=base, reverse=True)
    return sorted(scored, key=lambda s: (s.get("venue") == "raydium",) + base(s), reverse=True)


# ---------------------------------------------------- #10 Alchemy fallback cap
ALCHEMY_PER_ROUTE_MAX = 1
ALCHEMY_PER_CHECK_MAX = 2


class AlchemyBudget:
    """Gate for solana_rpc.submit_bundle's Alchemy re-quote/send.

    At most per_route_max Alchemy fallback sends per directed route attempt
    (a Jupiter send and its Raydium fallback on the same route share one) and
    at most per_check_max per edge-check process (edge fills plus savings and
    memecoin hook sends). When capped, the re-quote is not even fetched and
    nothing goes to Alchemy. Public mainnet is never a send endpoint.
    """

    def __init__(self, per_route_max=ALCHEMY_PER_ROUTE_MAX, per_check_max=ALCHEMY_PER_CHECK_MAX, log=None):
        self.per_route_max = int(per_route_max)
        self.per_check_max = int(per_check_max)
        self.route = None
        self.by_route = {}
        self.total = 0
        self.events = []
        self._log = log or (lambda msg: print(msg, flush=True))

    def set_route(self, route):
        self.route = None if route is None else str(route)

    def allow(self):
        key = self.route or "-"
        used_r = int(self.by_route.get(key, 0))
        if self.total >= self.per_check_max:
            why = "per_check_cap"
        elif used_r >= self.per_route_max:
            why = "per_route_cap"
        else:
            return True
        ev = {"route": key, "reason": why, "used_route": used_r, "used_check": self.total}
        self.events.append(ev)
        self._log(
            f"ALCHEMY_CAP route={key} reason={why} used_route={used_r}/{self.per_route_max} "
            f"used_check={self.total}/{self.per_check_max} (no Alchemy re-quote/send)"
        )
        return False

    def used(self):
        key = self.route or "-"
        self.by_route[key] = int(self.by_route.get(key, 0)) + 1
        self.total += 1
        self._log(
            f"ALCHEMY_FALLBACK_USE route={key} used_route={self.by_route[key]}/{self.per_route_max} "
            f"used_check={self.total}/{self.per_check_max}"
        )

    def summary(self):
        return {
            "per_route_max": self.per_route_max,
            "per_check_max": self.per_check_max,
            "used_check": self.total,
            "used_by_route": dict(self.by_route),
            "capped_events": list(self.events),
        }


# ------------------------------------------- #3 reverse candidate max age
REVERSE_MAX_AGE_HOURS = 96.0


def reverse_max_age_hours(cb_or_rev_cfg):
    """Hours after which a reverse source fill is expired. Default 96."""
    raw = cb_or_rev_cfg if isinstance(cb_or_rev_cfg, dict) else {}
    if "max_age_hours" in raw:
        src = raw
    else:
        src = (raw.get("reverse_last_trade") or {}) if isinstance(raw.get("reverse_last_trade"), dict) else {}
    try:
        v = float(src.get("max_age_hours", REVERSE_MAX_AGE_HOURS))
    except (TypeError, ValueError):
        v = REVERSE_MAX_AGE_HOURS
    return max(0.0, v)


def fill_time_pt(fill):
    """ISO time from a last_fill_by_pair entry (or trades.jsonl row)."""
    if not isinstance(fill, dict):
        return None
    for key in ("time_pt", "time", "filled_pt", "checked_pt"):
        if fill.get(key):
            return str(fill[key])
    return None


def reverse_fill_expired(fill, now, max_age_hours=REVERSE_MAX_AGE_HOURS):
    """True when the source fill is older than max_age_hours.

    Missing/unparseable time -> not expired (keep eligible; do not wipe history).
    """
    ts = _parse_ts(fill_time_pt(fill))
    if ts is None or now is None:
        return False
    try:
        age = now - ts
    except TypeError:
        # naive vs aware
        try:
            if ts.tzinfo is None and getattr(now, "tzinfo", None) is not None:
                ts = ts.replace(tzinfo=now.tzinfo)
            age = now - ts
        except Exception:
            return False
    try:
        return age > timedelta(hours=float(max_age_hours))
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------- #4 flat-market skip
FLAT_MARKET_DEFAULTS = {
    "enabled": True,
    "threshold_pct": 0.05,
    "symbols": ("SOL", "cbBTC", "WETH"),
    # Reference prices update only after a NON-skipped check so slow drift
    # still eventually triggers a full quote pass.
    "ref_update_on": "non_skipped_check",
}


def flat_market_cfg(cb):
    raw = (cb or {}).get("flat_market_skip") if isinstance(cb, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    out = dict(FLAT_MARKET_DEFAULTS)
    out["symbols"] = tuple(FLAT_MARKET_DEFAULTS["symbols"])
    for k in ("enabled", "threshold_pct", "ref_update_on"):
        if k in raw:
            out[k] = raw[k]
    if isinstance(raw.get("symbols"), (list, tuple)) and raw["symbols"]:
        out["symbols"] = tuple(str(s) for s in raw["symbols"])
    return out


def _abs_move_pct(cur, prev):
    try:
        c, p = float(cur), float(prev)
    except (TypeError, ValueError):
        return None
    if p <= 0 or c <= 0:
        return None
    return abs(c / p - 1.0) * 100.0


def flat_market_skip(current_prices, reference_prices, threshold_pct=0.05, symbols=None):
    """True when EVERY tracked symbol moved less than threshold_pct (abs).

    Missing previous prices for any symbol -> False (run normally).
    Missing current for any symbol -> False.
    """
    syms = tuple(symbols) if symbols else FLAT_MARKET_DEFAULTS["symbols"]
    thr = abs(float(threshold_pct))
    cur = current_prices if isinstance(current_prices, dict) else {}
    ref = reference_prices if isinstance(reference_prices, dict) else {}
    if not ref:
        return False, {"reason": "missing_reference", "moves_pct": {}}
    moves = {}
    for s in syms:
        m = _abs_move_pct(cur.get(s), ref.get(s))
        if m is None:
            return False, {"reason": "missing_price", "symbol": s, "moves_pct": moves}
        moves[s] = m
        if m >= thr - 1e-12:
            return False, {"reason": "moved", "moves_pct": moves}
    return True, {"reason": "flat_market", "moves_pct": moves, "threshold_pct": thr}


# --------------------------------------------- #5 route hit-rate cooldown
ROUTE_HIT_DEFAULTS = {
    "no_fill_days": 7.0,
    "cooldown_hours": 24.0,
    "deploy_pt": "",  # blank = first run of this install (set in RouteHitState)
    "state_file": "route-hit-state.json",
    # Reverse candidates share the cooldown of their reverse directed route
    # (REV:SOL→USDC#tx -> SOL→USDC). They are not exempt.
    "reverse_policy": "tie_to_directed_route",
}


def route_hit_cfg(cb):
    raw = (cb or {}).get("route_hit_rate") if isinstance(cb, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    out = dict(ROUTE_HIT_DEFAULTS)
    for k in ROUTE_HIT_DEFAULTS:
        if k in raw:
            out[k] = raw[k]
    return out


def directed_route_key(route):
    """Normalize a basis or REV:A→B#tx key to the directed A→B route."""
    s = str(route or "")
    if s.startswith("REV:"):
        s = s[4:]
    if "#" in s:
        s = s.split("#", 1)[0]
    return s


def bootstrap_last_fills(trades, last_fill_by_pair, deploy_pt, all_routes):
    """Map directed route -> last fill ISO from trades.jsonl / window history."""
    out = {}
    for t in trades or []:
        if not isinstance(t, dict):
            continue
        if t.get("savings") or t.get("kind") == "savings_buy":
            continue  # held savings buy, not a route fill
        side = t.get("side")
        if not side or "→" not in str(side):
            continue
        ts = fill_time_pt(t)
        if not ts:
            continue
        prev = out.get(side)
        if prev is None or str(ts) > str(prev):
            out[side] = str(ts)
    store = last_fill_by_pair if isinstance(last_fill_by_pair, dict) else {}
    for _pair, window in store.items():
        fills = window if isinstance(window, list) else ([window] if isinstance(window, dict) else [])
        for fill in fills:
            if not isinstance(fill, dict):
                continue
            side = fill.get("side")
            ts = fill_time_pt(fill)
            if not side or not ts:
                continue
            prev = out.get(side)
            if prev is None or str(ts) > str(prev):
                out[side] = str(ts)
    for route in all_routes or []:
        r = directed_route_key(route)
        if r and r not in out:
            out[r] = str(deploy_pt)
    return out


class RouteHitState:
    """Per directed-route last-fill + 7d/24h cooldown cycle. Persisted JSON."""

    def __init__(self, path, data=None, cfg=None):
        self.path = path
        self.cfg = cfg or dict(ROUTE_HIT_DEFAULTS)
        self.data = data if isinstance(data, dict) else {}
        self.data.setdefault("routes", {})
        if not self.data.get("deploy_pt"):
            self.data["deploy_pt"] = self.cfg.get("deploy_pt") or datetime.now().astimezone().isoformat(timespec="seconds")
        self.data.setdefault(
            "rule",
            "Directed route with no fill for no_fill_days is dropped from top-N "
            "ranking for cooldown_hours, then eligible again for another no_fill_days "
            "window. Reverse candidates use the reverse directed route key. "
            "Never-filled routes bootstrap window_start from deploy_pt (this install's first run).",
        )

    @classmethod
    def load(cls, path, cfg=None):
        data = None
        try:
            with open(path) as fh:
                data = json.load(fh)
        except Exception:
            data = None
        return cls(path, data, cfg)

    def save(self):
        d = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(prefix=".atomic-", suffix=".json", dir=d)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(self.data, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def ensure_route(self, route, last_fill_pt=None):
        key = directed_route_key(route)
        row = self.data["routes"].setdefault(key, {})
        if last_fill_pt and not row.get("last_fill_pt"):
            row["last_fill_pt"] = str(last_fill_pt)
        if not row.get("window_start_pt"):
            row["window_start_pt"] = row.get("last_fill_pt") or self.data.get("deploy_pt") or self.cfg.get("deploy_pt")
        return row

    def bootstrap(self, last_fill_map):
        for route, ts in (last_fill_map or {}).items():
            self.ensure_route(route, ts)

    def record_fill(self, route, when_iso):
        row = self.ensure_route(route)
        row["last_fill_pt"] = str(when_iso)
        row["window_start_pt"] = str(when_iso)
        row["cooldown_until_pt"] = None
        self.data["updated_pt"] = str(when_iso)
        return row

    def refresh(self, now):
        """Advance cooldown windows. Mutates state. Returns set of cooled route keys."""
        cooled = set()
        days = float(self.cfg.get("no_fill_days", 7.0))
        cool_h = float(self.cfg.get("cooldown_hours", 24.0))
        for key, row in list((self.data.get("routes") or {}).items()):
            if not isinstance(row, dict):
                continue
            until = _parse_ts(row.get("cooldown_until_pt"))
            if until is not None and now is not None:
                try:
                    if now < until:
                        cooled.add(key)
                        continue
                    # Cooldown ended: start a fresh eligibility window.
                    if row.get("cooldown_until_pt"):
                        row["window_start_pt"] = now.isoformat()
                        row["cooldown_until_pt"] = None
                except TypeError:
                    pass
            start = _parse_ts(row.get("window_start_pt") or row.get("last_fill_pt"))
            if start is None or now is None:
                continue
            try:
                age = now - start
            except TypeError:
                continue
            if age >= timedelta(days=days):
                row["cooldown_until_pt"] = (now + timedelta(hours=cool_h)).isoformat()
                cooled.add(key)
        if now is not None:
            self.data["updated_pt"] = now.isoformat()
        return cooled

    def is_cooled(self, route, cooled_set):
        return directed_route_key(route) in (cooled_set or set())


def _orca_enabled():
    import json as _j, os as _o
    try:
        d = _j.load(open(_o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "cost-basis.json")))
    except Exception:
        return False
    def _find(o):
        if isinstance(o, dict):
            if isinstance(o.get("orca_top1"), dict):
                return o["orca_top1"]
            for v in o.values():
                r = _find(v)
                if r is not None:
                    return r
        return None
    c = _find(d) or {}
    return bool(c.get("enabled", False))


def include_orca_for_rank(quote_rank):
    """Orca is a third venue only for the single best-ranked candidate (rank 1).

    Disabled 2026-10-05: orca_top1.enabled=false in cost-basis.json.
    """
    if not _orca_enabled():
        return False
    try:
        return int(quote_rank) == 1
    except (TypeError, ValueError):
        return False


def pick_best_venue(scored, venue_rank, prefer_raydium=False):
    """Highest fee-inclusive gap_net wins. venue_rank breaks ties.

    prefer_raydium: every Raydium row first (existing #7), then gap_net.
    """
    if not scored:
        return None
    ordered = order_venues(scored, prefer_raydium, venue_rank)
    return ordered[0] if ordered else None


def rank_quote_candidates(cands, top_n=QUOTE_TOP_N, cooled_routes=None):
    """Split candidates into (to_quote, not_quoted).

    Candidates carrying prefilter_block (fade, RSI unwind, same-pair streak,
    reverse_expired) can never fill this check, so they never take a quote slot.
    The rest are ranked by est_gap_net_pct descending. Cooled routes (hit-rate
    cooldown) are deferred: preferred slots go to non-cooled open candidates;
    if that leaves fewer than top_n, remaining slots are filled with the best
    cooled routes so something can still be quoted.
    """
    items = [c for c in (cands or []) if isinstance(c, dict)]
    open_ = [c for c in items if not c.get("prefilter_block")]
    blocked = [c for c in items if c.get("prefilter_block")]
    cooled = set(cooled_routes or [])
    hot = [c for c in open_ if directed_route_key(c.get("route")) not in cooled]
    cold = [c for c in open_ if directed_route_key(c.get("route")) in cooled]
    hot_ranked = sorted(hot, key=_est_key, reverse=True)
    cold_ranked = sorted(cold, key=_est_key, reverse=True)
    n = max(0, int(top_n))
    picked = list(hot_ranked[:n])
    if len(picked) < n:
        picked.extend(cold_ranked[: n - len(picked)])
    picked_ids = {id(c) for c in picked}
    rest = [c for c in hot_ranked + cold_ranked if id(c) not in picked_ids] + blocked
    for c in picked:
        if directed_route_key(c.get("route")) in cooled:
            c["cooldown_slot"] = True
    return picked, rest


# ---------------------------------------------------------------------------
# Quote-reuse fast path (locked 2026-10-05, owner). Pure helpers; the edge
# checker's fresh_executable() calls these. No network, no state.
QUOTE_REUSE_DEFAULTS = {
    "enabled": False,          # missing config -> always re-quote (old behavior)
    "max_age_sec": 10.0,
    "extra_margin_pct": 0.10,
    "first_send_only": True,
}
SIZE_EPS = 1e-9


def quote_reuse_cfg(cb):
    raw = (cb or {}).get("quote_reuse_fast_path") if isinstance(cb, dict) else None
    out = dict(QUOTE_REUSE_DEFAULTS)
    if isinstance(raw, dict):
        out["enabled"] = raw.get("enabled") is True
        for k in ("max_age_sec", "extra_margin_pct"):
            try:
                if raw.get(k) is not None:
                    out[k] = float(raw[k])
            except (TypeError, ValueError):
                pass
        if raw.get("first_send_only") is not None:
            out["first_send_only"] = bool(raw.get("first_send_only"))
    return out


def quote_reuse_precheck(cfg, attempt_index, sends_attempted, rec, sell_amt, now_mono):
    """Conditions 1, 2 and the size half of 4 (before any scoring).

    rec is the scan quote book entry {"at": monotonic response time,
    "sell_amt": quoted size, "rows": [...]} or None.
    Returns (ok, reason, age_sec). reason is a QUOTE_FRESH reason when not ok.
    """
    if not cfg or not cfg.get("enabled"):
        return False, "fast_path_disabled", None
    if cfg.get("first_send_only", True) and (int(attempt_index or 0) > 0 or int(sends_attempted or 0) > 0):
        return False, "not_first_send", None
    if not isinstance(rec, dict) or not rec.get("rows"):
        return False, "no_scan_quote", None
    try:
        age = float(now_mono) - float(rec["at"])
    except (KeyError, TypeError, ValueError):
        return False, "scan_quote_age_unknown", None
    try:
        quoted = float(rec.get("sell_amt"))
    except (TypeError, ValueError):
        return False, "size_changed", age
    if abs(quoted - float(sell_amt)) > SIZE_EPS:
        return False, "size_changed", age
    if age < 0 or age > float(cfg.get("max_age_sec", 10.0)) + 1e-9:
        return False, "too_old", age
    return True, None, age


def quote_reuse_margin(cfg, sc):
    """Condition 3 on one scored venue row (score_leg output).

    Needs clears and clears_net, and leftover (fee-inclusive edge minus the
    required eff%) above the pair leftover floor by >= extra_margin_pct
    percentage points, i.e. edge_net >= eff + floor + extra_margin.
    Returns (ok, margin_pp_over_floor).
    """
    if not isinstance(sc, dict) or not (sc.get("clears") and sc.get("clears_net")):
        return False, None
    lo = sc.get("leftover_pct")
    if lo is None:
        return False, None
    margin = float(lo) - float(sc.get("leftover_floor_pct") or 0.0)
    return margin + 1e-9 >= float(cfg.get("extra_margin_pct", 0.10)), margin


# -------------------------------- any-profit after quiet checks (owner 2026-10-06)
DEFAULT_ANY_PROFIT_QUIET = 420


def any_profit_after_quiet_cfg(cb):
    """Config for any_profit_after_quiet_checks (aka any_profit_after_69_quiet_checks).

    After quiet_checks_threshold consecutive checks with no fill, a candidate
    with fee-inclusive edge_net > 0 may fill — bypassing required_pct and the
    leftover floor. Counter lives on skip_edge_halving.skips_since_fill (reuse)
    and is mirrored onto any_profit_after_quiet_checks.quiet_checks_since_fill.
    """
    raw = (cb or {}).get("any_profit_after_quiet_checks") or {}
    if not isinstance(raw, dict):
        raw = {}
    thr = raw.get("quiet_checks_threshold", DEFAULT_ANY_PROFIT_QUIET)
    try:
        thr = int(thr)
    except (TypeError, ValueError):
        thr = DEFAULT_ANY_PROFIT_QUIET
    return {
        "enabled": bool(raw.get("enabled", True)),
        "name": str(raw.get("name") or "any_profit_after_69_quiet_checks"),
        "quiet_checks_threshold": max(1, thr),
        "signal": str(raw.get("signal") or "edge_net > 0"),
        "counter_source": str(raw.get("counter_source") or "skip_edge_halving.skips_since_fill"),
    }


def quiet_checks_since_fill(cb):
    """Checks with no fill since the last fill. Prefer dedicated counter, else skips_since_fill."""
    ap = (cb or {}).get("any_profit_after_quiet_checks") or {}
    if isinstance(ap, dict) and ap.get("quiet_checks_since_fill") is not None:
        try:
            return int(ap["quiet_checks_since_fill"])
        except (TypeError, ValueError):
            pass
    seh = (cb or {}).get("skip_edge_halving") or {}
    try:
        return int(seh.get("skips_since_fill") or 0)
    except (TypeError, ValueError):
        return 0


def any_profit_armed(cfg, quiet_count, oneshot_route=None, route=None):
    """True when the 100-quiet rule is live, or a oneshot env targets this route."""
    if oneshot_route and route and str(oneshot_route) == str(route):
        return True
    if not isinstance(cfg, dict) or not cfg.get("enabled", True):
        return False
    try:
        q = int(quiet_count)
        thr = int(cfg.get("quiet_checks_threshold") or DEFAULT_ANY_PROFIT_QUIET)
    except (TypeError, ValueError):
        return False
    return q >= thr


def any_profit_edge_ok(edge_net):
    """Fee-inclusive edge vs cost basis strictly above zero."""
    try:
        return float(edge_net) > 0.0
    except (TypeError, ValueError):
        return False


# min1pct 2026-10-12: 1% minimum required profit + $1 / $0.50 minimum trade size.
# unlock035/buyside1012 2026-10-12: no size minimum for normal fills/reversals; any-profit unlock needs edge_net >= 0.25% and >= $2.
# min1usd 2026-10-12 (owner, sim11): $1 minimum for edge fills, $0.49 for reversals (owner final; was $0.85 then $0.50).
MIN_FLOOR_DEFAULTS = {"enabled": True, "min_required_pct": 1.0, "min_edge_usd": 1.0, "min_reverse_usd": 0.49,
                      "any_profit_min_edge_pct": 0.25, "any_profit_min_usd": 2.0}  # buyside1012: was 0.35% / $1


def min_floor_cfg(cb):
    raw = (cb or {}).get("min_required_floor") or {}
    out = dict(MIN_FLOOR_DEFAULTS)
    if isinstance(raw, dict):
        for k in MIN_FLOOR_DEFAULTS:
            if k in raw:
                out[k] = raw[k]
    return out


def apply_min_required(eff, cfg, reverse_of_rsi=False, halving_mult=1.0):
    """required % = max(min_required_pct x skip-halving mult, lower-side clamped %).

    unlock035: while skip-halving is active (x0.5 after 60 quiet checks) the 1% floor is 0.5%;
    back to 1% after an edge fill resets halving. RSI flat 5% is untouched.
    """
    if reverse_of_rsi or not cfg.get("enabled", True):
        return float(eff)
    try:
        hm = float(halving_mult)
    except (TypeError, ValueError):
        hm = 1.0
    return max(float(cfg.get("min_required_pct", 1.0)) * hm, float(eff))


def below_min_size(notional_usd, cfg, is_reverse=False):
    """True when a trade is below $1 (edge) / $0.49 (reverse)."""
    if not cfg.get("enabled", True):
        return False
    lim = float(cfg.get("min_reverse_usd" if is_reverse else "min_edge_usd", 0) or 0)
    return float(notional_usd or 0) + 1e-9 < lim


def apply_any_profit_bypass(sc, cfg, quiet_count, route=None, oneshot_route=None, min_edge_pct=0.0, min_usd=0.0):
    """Mutate a score_leg dict: if armed and edge_net > 0, set clears/clears_net.

    Returns True when the bypass fired. Still leaves bad-condition / fade to the caller.
    """
    if not isinstance(sc, dict):
        return False
    if not any_profit_armed(cfg, quiet_count, oneshot_route=oneshot_route, route=route):
        return False
    if not any_profit_edge_ok(sc.get("edge_net")):
        return False
    # the unlock needs edge_net >= any_profit_min_edge_pct (0.25%, buyside1012).
    try:
        if float(sc.get("edge_net")) + 1e-9 < float(min_edge_pct or 0):
            return False
    except (TypeError, ValueError):
        return False
    # unlock035: unlock fills need at least min_usd notional (sell_usd from score_leg).
    if float(min_usd or 0) > 0:
        try:
            if float(sc.get("sell_usd")) + 1e-9 < float(min_usd):
                return False
        except (TypeError, ValueError):
            return False
    sc["clears"] = True
    sc["clears_net"] = True
    sc["clears_required"] = True
    sc["clears_leftover"] = True
    sc["any_profit_bypass"] = True
    return True


def tick_any_profit_counter(cb, outcome, had_fill=False):
    """Keep quiet_checks_since_fill in sync. Reset on ANY fill; +1 on quiet SKIP."""
    ap = cb.setdefault("any_profit_after_quiet_checks", {})
    if not isinstance(ap, dict):
        ap = {}
        cb["any_profit_after_quiet_checks"] = ap
    # Seed from skips_since_fill when missing so existing quiet streak carries over.
    if ap.get("quiet_checks_since_fill") is None:
        seh = cb.get("skip_edge_halving") or {}
        try:
            ap["quiet_checks_since_fill"] = int(seh.get("skips_since_fill") or 0)
        except (TypeError, ValueError):
            ap["quiet_checks_since_fill"] = 0
    if had_fill or outcome == "FILL":
        ap["quiet_checks_since_fill"] = 0
        ap["last_reset_reason"] = "fill"
        return 0
    if outcome == "SKIP":
        ap["quiet_checks_since_fill"] = int(ap.get("quiet_checks_since_fill") or 0) + 1
        return ap["quiet_checks_since_fill"]
    return int(ap.get("quiet_checks_since_fill") or 0)


# knife_hurdle_v1 2026-10-12 (owner, sim18c pick). ONE switch: cost-basis.json knife_hurdle_v1.enabled.
# Off (or missing) = exact previous behavior.
KNIFE_HURDLE_DEFAULTS = {"enabled": False, "edge_hurdle_usd": 0.10, "reverse_hurdle_usd": 0.01, "disable_vwap": True}


def knife_hurdle_cfg(cb):
    raw = (cb or {}).get("knife_hurdle_v1") or {}
    out = dict(KNIFE_HURDLE_DEFAULTS)
    if isinstance(raw, dict):
        for k in KNIFE_HURDLE_DEFAULTS:
            if k in raw:
                out[k] = raw[k]
    out["enabled"] = bool(out.get("enabled"))
    return out


def knife_block(sold, bought, quote_by_symbol):
    """24h falling-knife test for an edge fill sold->bought (fail closed).

    Blocks when bought/sold price ratio now is below the ratio 24h ago.
    quote_by_symbol: fade_trend rows {sym: {"usd": now, "usd_24h": 24h ago}}.
    Returns "knife_24h", "knife_price_missing", or None.
    """
    def pair(sym):
        if sym == "USDC":
            row = (quote_by_symbol or {}).get("USDC") or {}
            try:
                return float(row.get("usd") or 1.0), float(row.get("usd_24h") or 1.0)
            except (TypeError, ValueError):
                return 1.0, 1.0
        row = (quote_by_symbol or {}).get(sym) or {}
        try:
            now, ago = float(row.get("usd")), float(row.get("usd_24h"))
        except (TypeError, ValueError):
            return None
        if not (now > 0 and ago > 0):
            return None
        return now, ago
    b, s = pair(bought), pair(sold)
    if b is None or s is None:
        return "knife_price_missing"
    return "knife_24h" if (b[0] / s[0]) < (b[1] / s[1]) else None


def apply_profit_hurdle(sc, cfg, is_reverse):
    """Extra $ hurdle on top of required %: leftover must clear floor + hurdle% (hurdle$/sell_usd).

    Returns True when the hurdle (not the required %) is what blocks this quote.
    """
    if not isinstance(sc, dict) or not cfg.get("enabled"):
        return False
    usd = float(cfg["reverse_hurdle_usd"] if is_reverse else cfg["edge_hurdle_usd"])
    try:
        sell_usd = float(sc.get("sell_usd") or 0)
    except (TypeError, ValueError):
        sell_usd = 0.0
    h = (usd / sell_usd * 100.0) if sell_usd > 0 else float("inf")
    sc["hurdle_usd"] = usd
    sc["hurdle_pct"] = h
    lo, fl = sc.get("leftover_pct"), sc.get("leftover_floor_pct") or 0.0
    was = bool(sc.get("clears_net"))
    if lo is None or lo - h + 1e-9 < fl:
        sc["clears_leftover"] = False
        sc["clears_net"] = False
        sc["clears"] = False
        sc["hurdle_block"] = was
        return was
    return False
