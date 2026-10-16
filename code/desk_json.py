#!/usr/bin/env python3
"""Build desk.json: the only data file bot-desk.html loads (deskjson 2026-10-11).

Compact (short keys, no whitespace). Mirrors bot-desk.html parseLog()/parseTrades():
  c  latest edge-check entry: t time, s status, a addr, b {SYM:[amount,usd]}, T total,
     u unrealPct, f funded, sv [jito,jitoUsd,goat,goatUsd], bi [least,most]|null,
     i closest items [[rev,from,to,a,b(,ptsToClear)]], it itemsTime, ih held(0/1), nx next
  p  deduped closest pairs ever logged [[rev,from,to]] (feeds rememberPair)
  h  total_usd history (last 200; only when fewer than 2 fills carry total_usd_after)
  r  last 69 fills [time_pt, sold_asset, sold_amount, bought_asset, bought_amount, tx]
     (tx = solscan tx id, or the full tx_url when it is not a solscan /tx/ link)
  n  total fill count;  v  volume over all fills {SYM:[sold,bought]};  cs total_usd_after series
sol-usdc-log.md is scanned incrementally (offset cache in desk-json-state.json), so a
huge log costs only the newly appended bytes per check.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import degen_home as _DH

ROOT = Path(_DH.HOME_STR)
LOG = ROOT / "sol-usdc-log.md"
TRADES = ROOT / "trades.jsonl"
STATE = ROOT / "desk-json-state.json"
LIVE = ROOT / "live-closest.json"
COST_BASIS = ROOT / "cost-basis.json"  # revflag: reverse_last_trade.reversed_fill_txs_by_pair  # closest0 2026-10-11: written by _edge_check_run every check
LAST_FILLS = 69
HIST_POINTS = 200
SERIES_MAX = 2000
TAIL_BYTES = 256 * 1024
SOLSCAN = "https://solscan.io/tx/"

R_TOT2 = re.compile(r"^-\s*tot:\s*\$([\d.,]+)\s+(-?[\d.]+)%\s+of\s+\$([\d.,]+)", re.M)
R_TOTAL = re.compile(r"total_usd:\s*\$([\d.,]+)(?:\s+\(unrealized\s+(-?[\d.]+)%\s+vs\s+\$([\d.,]+)\s+funded\))?")
R_BAL_LINE = re.compile(r"^-\s*(?:balances|bal):\s*(.+)$", re.M)
R_BAL = re.compile(r"([A-Za-z]+)(?:=|\s+)([\d.eE+-]+)\s+\(?\$([\d.,]+)\)?")
R_SJ = re.compile(r"savings_jitosol_held:\s*([\d.eE+-]+)\s+\(\$([\d.,]+)\)")
R_SAV2 = re.compile(r"^-\s*sav:.*?JitoSOL\s+([\d.eE+-]+)\s+\$([\d.,]+).*?GOAT\s+([\d.eE+-]+)\s+\$([\d.,]+)", re.M)
R_SG = re.compile(r"savings_goat_held:\s*([\d.eE+-]+)\s+\(\$([\d.,]+)\)")
R_BIAS = re.compile(r"holdings_bias:\s*least=(\S+)\s+most=(\S+)")
R_BIAS2 = re.compile(r"^-\s*bias:\s*([^/\s]+)/(\S+)", re.M)
R_DOT = re.compile(r"^\. (\d{2}:\d{2}:\d{2})\b", re.M)
R_TIME = re.compile(r"^##\s+(\S+)")
R_STATUS = re.compile(r"Primary edge check\s+[—–-]\s+(\S+)")
R_ADDR = re.compile(r"`([1-9A-HJ-NP-Za-km-z]{32,44})`")
R_NEXT = re.compile(r"^-\s*next:\s*(.+)$", re.M)
R_CL_LINE = re.compile(r"^-\s*(?:closest3|c3):\s*(.+)$", re.M)
R_CL_SPLIT = re.compile(r",\s+(?=(?:REV:)?[A-Za-z0-9]+→)")
R_CL_ITEM = re.compile(r"^(REV:)?([A-Za-z0-9]+)→([A-Za-z0-9]+).*?(-?\d+(?:\.\d+)?)\s*%\s*/\s*(-?\d+(?:\.\d+)?)\s*%(?:\s+(-?\d+(?:\.\d+)?)pts)?")
R_BLOCK_SPLIT = re.compile(r"\n(?=## )")


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _jsnum(s: str):
    v = float(s)
    return int(v) if v.is_integer() and abs(v) < 2**53 else v


def closest(block: str) -> list:
    m = R_CL_LINE.search(block)
    line = (m.group(1) if m else "").strip()
    if not line or re.match(r"^n/?a$", line, re.I) or re.match(r"^none$", line, re.I):
        return []
    out = []
    for p in R_CL_SPLIT.split(line):
        mm = R_CL_ITEM.search(p.strip())
        if mm:
            out.append([1 if mm.group(1) else 0, mm.group(2), mm.group(3), _jsnum(mm.group(4)), _jsnum(mm.group(5))]
                       + ([_jsnum(mm.group(6))] if mm.group(6) else []))
    return out


def _meme_on(key: str = "memecoin_mode"):
    """memeoff 2026-10-14: 1/0 from cost-basis memecoin_mode.enabled; None if unreadable (page keeps Greedy Goats lines)."""
    try:
        mm = json.loads(COST_BASIS.read_text(encoding="utf-8")).get(key)
        return 1 if isinstance(mm, dict) and mm.get("enabled") is True else 0
    except Exception:
        return None


def _blocks(text: str) -> list[str]:
    return [b for b in R_BLOCK_SPLIT.split(text) if "total_usd:" in b or "\n- tot:" in b]


def _tot(b: str):
    """logdiet: old 'total_usd: $X (unrealized U% vs $F funded)' or new '- tot: $X U% of $F'."""
    return R_TOT2.search(b) or R_TOTAL.search(b)


def _last_time(b: str) -> str:
    """Heading time, moved to the last '. HH:MM:SS' dedup line under it (logdiet)."""
    t = _btime(b)
    dots = R_DOT.findall(b)
    if dots and len(t) >= 19:
        return t[:11] + dots[-1] + t[19:]
    return t


def _btime(b: str) -> str:
    m = R_TIME.search(b)
    return m.group(1) if m else ""


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def scan_log(path: Path = LOG) -> dict:
    """Incremental pass for pairs + last block with closest items. Returns the state."""
    st = _load_state()
    size = path.stat().st_size if path.exists() else 0
    off = int(st.get("off") or 0)
    if off > size or st.get("v") != 1:
        st = {"v": 1, "off": 0, "pairs": [], "li": [], "lit": ""}
        off = 0
    if size > off:
        with open(path, "rb") as f:
            f.seek(off)
            raw = f.read()
        # keep the last block for re-reading next time (it may still be growing)
        cut = raw.rfind(b"\n## ")
        text = raw.decode("utf-8", errors="replace")
        seen = {tuple(p) for p in st["pairs"]}
        for b in _blocks(text):
            items = closest(b)
            for it in items:
                k = (it[0], it[1], it[2])
                if k not in seen:
                    seen.add(k)
                    st["pairs"].append(list(k))
            if items:
                st["li"], st["lit"] = items, _btime(b)
            ad = R_ADDR.search(b)
            if ad:
                st["addr"] = ad.group(1)
        if cut > 0:
            st["off"] = off + cut + 1
        tmp = STATE.with_name(STATE.name + ".tmp")
        tmp.write_text(json.dumps(st, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, STATE)
    return st


def _tail(path: Path, n: int = TAIL_BYTES) -> str:
    with open(path, "rb") as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - n))
        raw = f.read()
    if size > n:
        i = raw.find(b"\n## ")
        raw = raw[i + 1:] if i >= 0 else raw
    return raw.decode("utf-8", errors="replace")


def live_closest(log_time: str, path: Path | None = None):
    """(items, time) from live-closest.json when it is at least as new as the log's latest
    check; else None (missing/stale/unparseable -> log carry-forward)."""
    try:
        d = json.loads((path or LIVE).read_text(encoding="utf-8"))
        t, line = str(d.get("t") or ""), str(d.get("c3") or "")
    except Exception:
        return None
    if not t or not log_time or t < log_time:
        return None
    items = closest("- closest3: " + line)
    # cblock1016: per-route extras [.., c, blocks, required%, profit_after_fees%, send_failed]
    xs = d.get("x") if isinstance(d.get("x"), list) else []
    for it, x in zip(items, xs):
        if isinstance(x, dict):
            while len(it) < 6:
                it.append(None)
            it += [[str(k) for k in (x.get("k") or [])][:4], x.get("req"), x.get("en"), 1 if x.get("sf") else 0]
    return (items, t) if items else None


LIVE_BAL = ROOT / "live-balance.json"  # status1012


def live_balance(log_time: str, path: Path | None = None):
    """Balance status from the minute check when newer than the log's latest entry, else None."""
    try:
        d = json.loads((path or LIVE_BAL).read_text(encoding="utf-8"))
        t = str(d.get("t") or "")
    except Exception:
        return None
    if not t or not isinstance(d.get("b"), dict):
        return None
    # mini log entries repeat the last full entry's (stale) balances, so a live write from
    # the previous check(s) still wins; only a log entry >150s newer than it takes over.
    try:
        from datetime import datetime as _dt
        if log_time and (_dt.fromisoformat(log_time) - _dt.fromisoformat(t)).total_seconds() > 150:
            return None
    except Exception:
        if log_time and t < log_time:
            return None
        return None
    return d


def latest_check(path: Path, st: dict, blocks: list[str]) -> dict | None:
    if not blocks:
        return None
    last = blocks[-1]
    bal = {}
    m = R_BAL_LINE.search(last)
    for mm in R_BAL.finditer(m.group(1) if m else ""):
        bal[mm.group(1)] = [_jsnum(mm.group(2)), round(_num(mm.group(3)), 2)]
    tot = _tot(last)
    new_fmt = "\n- tot:" in last
    sj, sg = R_SJ.search(last), R_SG.search(last)
    sv2 = R_SAV2.search(last)
    bias = R_BIAS.search(last) or R_BIAS2.search(last)
    if sv2:
        sv = [_jsnum(sv2.group(1)), round(_num(sv2.group(2)), 2), _jsnum(sv2.group(3)), round(_num(sv2.group(4)), 2)]
    elif new_fmt and not sj and not sg:
        sv = [0, 0.0, 0, 0.0]  # logdiet: omitted savings line == all three zero
    else:
        sv = [_jsnum(sj.group(1)) if sj else None, round(_num(sj.group(2)), 2) if sj else None,
              _jsnum(sg.group(1)) if sg else None, round(_num(sg.group(2)), 2) if sg else None]
    items, it_time, held = closest(last), _last_time(last), 0
    lv = live_closest(_last_time(last))
    if lv:
        items, it_time = lv
        seen = {tuple(p) for p in st.get("pairs") or []}
        for it in items:  # keep p complete although dedup'd checks put no routes in the log
            k = (it[0], it[1], it[2])
            if k not in seen:
                seen.add(k)
                st.setdefault("pairs", []).append(list(k))
                st["_dirty"] = True
    if not items:
        for b in reversed(blocks):
            found = closest(b)
            if found:
                items, it_time, held = found, _btime(b) or it_time, 1
                break
        else:
            if st.get("li"):
                items, it_time, held = st["li"], st.get("lit") or it_time, 1
    nx = R_NEXT.search(last)
    sa = R_STATUS.search(last)
    ad = R_ADDR.search(last)
    addr = ad.group(1) if ad else ""
    if not addr:  # logdiet: addr is written on the first full entry of each day only
        for b in reversed(blocks):
            m2 = R_ADDR.search(b)
            if m2:
                addr = m2.group(1)
                break
        else:
            addr = st.get("addr") or ""
    out = {
        "t": _last_time(last), "s": sa.group(1) if sa else "", "a": addr,
        "b": bal,
        "T": round(_num(tot.group(1)), 2) if tot else None,
        "u": _jsnum(tot.group(2)) if tot and tot.group(2) is not None else None,
        "f": round(_num(tot.group(3)), 2) if tot and tot.group(3) else None,
        "sv": sv,
        "bi": [bias.group(1), bias.group(2)] if bias else None,
        "i": items, "it": it_time, "ih": held,
        "nx": (nx.group(1) if nx else "").split("`")[0].strip(),
        "bt": _last_time(last),
        "mm": _meme_on(),
        "sm": _meme_on("savings_mode"),
    }
    try:  # status1012: newer minute-check balances override the log's bal/tot
        lb = live_balance(out["t"])
        if lb:
            out["b"] = {k: [_jsnum(str(v[0])), round(float(v[1]), 2)] for k, v in lb["b"].items() if isinstance(v, list) and v[0] is not None}
            out["T"] = lb.get("T", out["T"])
            if lb.get("u") is not None:
                out["u"] = round(float(lb["u"]), 2)
            if lb.get("f") is not None:
                out["f"] = round(float(lb["f"]), 2)
            out["bt"] = lb["t"]
    except Exception:
        pass
    try:  # lend1015: Jupiter Lend [APR %, lent USDC] for the USD card
        _ls = json.loads((ROOT / "lend-state.json").read_text(encoding="utf-8"))
        if _ls.get("apr") is not None and float(_ls.get("shares") or 0) > 0:
            out["ln"] = [round(float(_ls["apr"]), 2), round(float(_ls.get("lent_usdc") or 0), 2)]
    except Exception:
        pass
    try:  # stake1015: JitoSOL bucket [APY %, bucket value in SOL] for the SOL card
        _ss = json.loads((ROOT / "sol-stake-state.json").read_text(encoding="utf-8"))
        if _ss.get("apy") and float(_ss.get("bucket_jitosol") or 0) > 0 and _ss.get("rate"):
            out["js"] = [round(float(_ss["apy"]), 2), round(float(_ss["bucket_jitosol"]) * float(_ss["rate"]), 6)]
    except Exception:
        pass
    return out


def load_fills(path: Path = TRADES) -> list[dict]:
    out = []
    if not path.exists():
        return out
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            s = ln.strip()
            if not s.startswith("{"):
                continue
            try:
                out.append(json.loads(s))
            except Exception:
                pass
    out.sort(key=lambda r: str(r.get("time_pt") or ""))
    return out


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0
    return v if v == v and v not in (float("inf"), float("-inf")) else 0


def _tx(r: dict) -> str:
    t = r.get("tx")
    if t:
        return str(t)
    u = str(r.get("tx_url") or "")
    return u[len(SOLSCAN):] if u.startswith(SOLSCAN) else ""


def rev_flags(fills: list[dict], cb_path: Path | None = None) -> list[int]:
    """revflag 2026-10-11: 1 when the fill is a reversal. rev:1 on the record wins; else the first
    opposite-direction fill on the same pair after an original listed in
    reverse_last_trade.reversed_fill_txs_by_pair; else 0."""
    flags = [1 if str(r.get("rev")) in ("1", "True", "true") else 0 for r in fills]
    try:
        cb = json.loads((cb_path or COST_BASIS).read_text(encoding="utf-8"))
        orig = {t for v in ((cb.get("reverse_last_trade") or {}).get("reversed_fill_txs_by_pair") or {}).values() for t in v or []}
    except Exception:
        orig = set()
    explicit = {str(r.get("rev_of")) for r in fills if r.get("rev_of")}
    used = {i for i, f in enumerate(flags) if f}
    for i, r in enumerate(fills):
        t = _tx(r)
        if not t or t not in orig or t in explicit:
            continue
        for j in range(i + 1, len(fills)):
            q = fills[j]
            if j not in used and q.get("sold_asset") == r.get("bought_asset") and q.get("bought_asset") == r.get("sold_asset"):
                flags[j] = 1
                used.add(j)
                break
    return flags


NO_VOLUME = {"JitoSOL", "GreedyGoats", "GOAT"}


def build(log_path: Path = LOG, trades_path: Path = TRADES) -> dict:
    st = scan_log(log_path) if log_path.exists() else {"pairs": [], "li": [], "lit": ""}
    blocks = _blocks(_tail(log_path)) if log_path.exists() else []
    fills = load_fills(trades_path)
    vol: dict[str, list] = {}
    series = []
    for r in fills:
        s, b = r.get("sold_asset"), r.get("bought_asset")
        # JitoSOL / Greedy Goats are buy-only holds; their cost is already in the paying coin's volume.
        if str(s) not in NO_VOLUME:
            vol.setdefault(str(s), [0.0, 0.0])
            vol[str(s)][0] += _f(r.get("sold_amount"))
        if str(b) not in NO_VOLUME:
            vol.setdefault(str(b), [0.0, 0.0])
            vol[str(b)][1] += _f(r.get("bought_amount"))
        if "total_usd_after" in r:  # chartdip1014: only real wallet totals (> 0) go on the chart
            tv = r["total_usd_after"]  # backfilled / late_fill rows without a total are skipped, not 0
            try:
                v = float(tv) if tv is not None else float("nan")
                if v == v and v > 0 and abs(v) != float("inf"):
                    series.append(round(v, 2))
            except (TypeError, ValueError):
                pass
    rows = []
    for r in fills[-LAST_FILLS:]:
        url = str(r.get("tx_url") or "")
        tx = url[len(SOLSCAN):] if url.startswith(SOLSCAN) else url
        rows.append([r.get("time_pt") or "", r.get("sold_asset") or "", r.get("sold_amount"),
                     r.get("bought_asset") or "", r.get("bought_amount"), tx])
    c = latest_check(log_path, st, blocks)
    if st.pop("_dirty", False) and log_path.exists():
        try:
            tmp = STATE.with_name(STATE.name + ".tmp")
            tmp.write_text(json.dumps(st, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, STATE)
        except Exception:
            pass
    rf = rev_flags(fills)[-LAST_FILLS:] if fills else []
    d = {"c": c, "p": st.get("pairs") or [],
         "r": rows, "n": len(fills),
         "v": {k: [round(a, 9), round(b, 9)] for k, (a, b) in vol.items()},
         "cs": series[-SERIES_MAX:], "rf": rf}
    if len(series) < 2:
        d["h"] = [round(_num(m.group(1)), 2) for b in blocks for m in [_tot(b)] if m][-HIST_POINTS:]
    return d


# --- logdiet/split 2026-10-11: desk.json (polled every 15s) + fills.json (on fv change)
FILLS_NAME = "fills.json"
LEGEND = {"S": "SOL", "U": "USDC", "B": "cbBTC", "W": "WETH"}
_CODE = {v: k for k, v in LEGEND.items()}
_R_PT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}-07:00$")


def _ct(t: str):
    """Epoch seconds when the -07:00 string round-trips exactly, else the string."""
    if isinstance(t, str) and _R_PT.match(t):
        from datetime import datetime
        return int(datetime.fromisoformat(t).timestamp())
    return t


def split(d: dict) -> tuple[dict, dict]:
    """Old build() dict -> (desk, fills). fills rows: [time, sold, sold_amt, bought, bought_amt, tx];
    time = epoch s (PT -07:00) or the original string; coins coded via L (unknown kept as-is);
    tx = solscan tx id (full) or the full url."""
    fills = {"L": LEGEND,
             # row: [time, sold, sold_amt, bought, bought_amt, tx, rev(0/1)]  (revflag 2026-10-11)
             "r": [[_ct(r[0]), _CODE.get(r[1], r[1]), r[2], _CODE.get(r[3], r[3]), r[4], r[5]]
                   + ([int(d["rf"][i])] if i < len(d.get("rf") or []) else [])
                   for i, r in enumerate(d.get("r") or [])],
             "v": d.get("v") or {}, "cs": d.get("cs") or []}
    fb = dumps(fills)
    desk = {"c": d.get("c"), "p": d.get("p") or [], "n": d.get("n"),
            "fv": __import__("hashlib").sha1(fb).hexdigest()[:10]}
    if "h" in d:
        desk["h"] = d["h"]
    return desk, fills


def dumps(d: dict) -> bytes:
    return json.dumps(d, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


if __name__ == "__main__":
    import sys
    dk, fl = split(build())
    sys.stdout.write(f"desk.json {len(dumps(dk))} bytes, fills.json {len(dumps(fl))} bytes\n")
