"""Compact + deduplicated SKIP entries for sol-usdc-log.md (logdiet 2026-10-11, owner).

A SKIP check (no send attempt) writes:
  * a FULL compact entry when its meaningful state differs from the last full entry,
    when the clock hour changed since the last full entry, or on the first check of a day;
  * otherwise a one-line DOT entry inside the previous section:  ". HH:MM:SS q289\n"
    (16 bytes).  It starts with ". " so no reader treats it as a field ("- ") or a section
    ("## "); prune_log/log_chunks keep it with its section; desk_json uses it as the
    latest check time.  Full entries are forced on a date change, so a dot line always
    shares the date of the "## " heading above it.
Meaningful state (closest0 2026-10-11): status, rule_blocked set, fear_greed value, VWAP above/below per token, balance quantities, savings quantities,
active streak.  Prices and gap % wiggles are ignored.
FILL / HARD_BLOCKER / send attempts keep the old verbose format (written by the checker).

Compact full entry (fields omitted when noted):
  - addr: `<wallet>`                  first full entry of each day only
  - bal: SOL 2.602162 $284.44 | USDC 1.670276 $1.67 | cbBTC 0.00023969 $19.89 | WETH 0.02004544 $50.10
  - tot: $356.10 -3.92% of $370.64
  - bias: USDC/SOL                    least/most
  - streak: SOL-USDC x3               only when active
  - sav: cbBTC 0.0 $0.00 | JitoSOL 0.0 $0.00 | GOAT 0 $0.00   when any nonzero, or first of day
  - fg: 61 Greed                      when the value changed, or first entry of the hour
  - vwap: SOL 109.32<109.88 | cbBTC 82958.7>82915.7 | WETH 2497.91<2503.79
  - blocked: vw:not_above SOL→USDC, WETH→USDC | vw:not_below USDC→cbBTC
  - flat: <reason>                    only when flat-market skip
  - c3: cbBTC→SOL -0.524%/-0.533%, ...   (3-decimal gaps kept: the dashboard shows 3)
  - req: 5.0 vs 4.45 cbBTC→SOL
  - quiet: 289/420
  - next: 01:04:29
Omitted savings mean all three are zero; omitted addr/fg mean "same as before".
"""
from __future__ import annotations

import hashlib
import json
import os

STATE_NAME = "log-diet-state.json"
SYMS = ("SOL", "USDC", "cbBTC", "WETH")
TOKENS = ("SOL", "cbBTC", "WETH")


def _rule(why: str) -> str:
    w = str(why)
    if w.startswith("vwap24h_"):
        return "vw:" + w[8:]
    if w.startswith("fear_greed_"):
        return "fg:" + w[11:]
    return w


def compact_blocked(blocked: dict) -> str:
    if not blocked:
        return "none"
    groups: dict = {}
    for r, why in sorted(blocked.items()):
        g = groups.setdefault(_rule(why), [[], 0])
        if str(r).startswith("REV:"):
            g[1] += 1
        else:
            g[0].append(str(r))
    parts = []
    for why, (routes, nrev) in sorted(groups.items()):
        txt = ", ".join(routes)
        if nrev:
            txt = (txt + "; " if txt else "") + f"+{nrev}rev"
        parts.append(f"{why} {txt}")
    return " | ".join(parts)


def _p(x) -> str:
    """Price with ~6 significant digits, trailing zeros dropped."""
    try:
        return f"{float(x):.6g}"
    except (TypeError, ValueError):
        return str(x)


def vwap_text(vwap, f_on=True, v_on=True):
    if not v_on:
        return "off", {}
    if (vwap or {}).get("error"):
        return f"unavailable ({vwap['error']})", {"err": True}
    parts, flags = [], {}
    for t in TOKENS:
        q = (vwap or {}).get(t)
        if not q:
            parts.append(f"{t} n/a")
            flags[t] = None
            continue
        below = q["last"] < q["vwap"]
        flags[t] = "b" if below else "a"
        parts.append(f"{t} {_p(q['last'])}{'<' if below else '>'}{_p(q['vwap'])}")
    return " | ".join(parts), flags


def fg_text(fng, f_on=True, size_mult=1.0):
    f = fng or {}
    if not f_on:
        return "off", "off"
    if f.get("value") is None:
        return f"unavailable ({f.get('error')})", "na"
    extra = f" x{size_mult:g}" if size_mult != 1 else ""
    return f"{f['value']} {f.get('class')}{extra}", f["value"]


def load_state(home: str) -> dict:
    try:
        with open(os.path.join(home, STATE_NAME), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(home: str, st: dict) -> None:
    p = os.path.join(home, STATE_NAME)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, separators=(",", ":"))
    os.replace(tmp, p)


def signature(ctx: dict) -> str:
    c3 = ctx.get("c3") or []
    sig = {
        "s": ctx.get("outcome"),
        # closest0 2026-10-11: no closest/req route or target in the dedup state; the live
        # closest3 reaches desk.json every check via live-closest.json instead.
        "bl": sorted(f"{r}|{w}" for r, w in (ctx.get("blocked") or {}).items()),
        "fg": ctx.get("fg_key"),
        "vw": ctx.get("vw_flags"),
        "b": [str(ctx["bals"].get(s)) for s in SYMS],
        "sv": [str(x) for x in ctx.get("sav_qty") or []],
        "st": ctx.get("streak") or None,
        "fl": bool(ctx.get("flat")),
    }
    return hashlib.sha1(json.dumps(sig, sort_keys=True, default=str).encode()).hexdigest()[:16]


def render(ctx: dict, st: dict) -> tuple[str, dict, str]:
    """Return (text to append, new state, kind in {'full','dot'})."""
    now = ctx["now_iso"]                      # 2026-10-11T01:03:29-07:00
    day, hour = now[:10], now[:13]
    sig = signature(ctx)
    first_of_day = st.get("day") != day
    new_hour = st.get("hour") != hour
    ours = ctx.get("last_heading") is None or ctx.get("last_heading") == st.get("full")
    if ours and not first_of_day and not new_hour and st.get("sig") == sig:
        line = f". {now[11:19]} q{ctx.get('quiet')}\n"
        return line, dict(st, last=now), "dot"
    b, u = ctx["bals"], ctx["usd"]
    lines = [f"\n## {now} Primary edge check — {ctx['outcome']}\n"]
    if first_of_day:
        lines.append(f"- addr: `{ctx['addr']}`\n")
    lines.append("- bal: " + " | ".join(f"{s} {b[s]} ${u[s]:.2f}" for s in SYMS) + "\n")
    lines.append(f"- tot: ${ctx['total']:.2f} {ctx['unreal']:.2f}% of ${ctx['funded']:.2f}\n")
    lines.append(f"- bias: {ctx['least']}/{ctx['most']}\n")
    if ctx.get("streak"):
        lines.append(f"- streak: {ctx['streak'][0]} x{ctx['streak'][1]}\n")
    sq = ctx.get("sav_qty") or []
    if first_of_day or any(_nz(x) for x in sq):
        lines.append("- sav: " + ctx["sav_text"] + "\n")
    if first_of_day or new_hour or st.get("fg") != ctx.get("fg_key"):
        lines.append(f"- fg: {ctx['fg_text']}\n")
    lines.append(f"- vwap: {ctx['vw_text']}\n")
    lines.append(f"- blocked: {compact_blocked(ctx.get('blocked') or {})}\n")
    if ctx.get("flat"):
        lines.append(f"- flat: {ctx['flat']}\n")
    c3 = ctx.get("c3") or []
    lines.append("- c3: " + (", ".join(f"{x['route']} {x['gap_pct']}%/{x['gap_net_pct']}%" for x in c3[:3]) or "none") + "\n")
    cl = ctx.get("closest") or {}
    lines.append(f"- req: {cl.get('required_pct')} vs {cl.get('edge_pct')} {cl.get('route')}\n")
    lines.append(f"- quiet: {ctx.get('quiet')}/{ctx.get('quiet_thr')}\n")
    lines.append(f"- next: {str(ctx.get('next_iso') or '')[11:19]}\n")
    ns = {"v": 1, "sig": sig, "day": day, "hour": hour, "fg": ctx.get("fg_key"), "last": now, "full": now}
    return "".join(lines), ns, "full"


def last_heading(path: str, n: int = 16384):
    """Timestamp of the last '## ' heading in the log (another writer may have appended)."""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, 2)
            f.seek(max(0, size - n))
            raw = f.read()
    except OSError:
        return ""
    i = raw.rfind(b"\n## ")
    if i < 0:
        return ""
    return raw[i + 4:].split(b" ", 1)[0].decode("utf-8", "replace")


def _nz(x) -> bool:
    try:
        return abs(float(x)) > 1e-12
    except (TypeError, ValueError):
        return bool(x)


def note_verbose(st: dict, now_iso: str) -> dict:
    """A verbose (FILL/error/send) entry was written: force the next SKIP to be full."""
    return dict(st, sig=None, last=now_iso)
