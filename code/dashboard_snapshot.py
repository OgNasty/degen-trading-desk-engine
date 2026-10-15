#!/usr/bin/env python3
"""Write dashboard/data.json and keep bot-desk data files in sync (no RPC, no secrets).

Refresh: morning balance end, after each fill, after each box_runner check,
and via CLI:  .venv/bin/python dashboard_snapshot.py

Also ensures dashboard/ serves bot-desk.html as index, with
sol-usdc-log.md + trades.jsonl alongside for relative fetches.

Never wakes chat. Never copies to Desktop.
"""
from __future__ import annotations

import degen_home as _DH  # portable base dir (pcmove 2026-10-10)
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from degen_home import TZ_NAME as _TZ_NAME

ROOT = Path(_DH.HOME_STR)
DASH = ROOT / "dashboard"
OUT = DASH / "data.json"
LOG = ROOT / "runner-logs" / "dashboard-snapshot.log"
PT = ZoneInfo(_TZ_NAME)
BOOK = ("SOL", "USDC", "cbBTC", "WETH")
RECENT_TRADES = 25


def _now_pt() -> str:
    return datetime.now(PT).replace(microsecond=0).isoformat()


def _log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(LOG, "a", encoding="utf-8", newline="") as f:
            f.write(f"{_now_pt()} {msg}\n")
    except OSError:
        pass


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_jsonl_tail(path: Path, n: int) -> list:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _runner_alive() -> dict:
    pidfile = ROOT / "runner.pid"
    stop = ROOT / "runner.stopped"
    pid = None
    alive = False
    if _DH.IS_WIN:
        # Windows: os.kill(pid, 0) would terminate the process. Use heartbeat freshness.
        try:
            import time as _t
            hb = ROOT / "runner-heartbeat.json"
            alive = hb.is_file() and (_t.time() - hb.stat().st_mtime) < 300
            pid = os.getpid() if alive else None
        except OSError:
            alive = False
    elif pidfile.is_file():
        raw = pidfile.read_text(encoding="utf-8").strip()
        digits = "".join(c for c in raw if c.isdigit())
        if digits:
            try:
                pid = int(digits)
                os.kill(pid, 0)
                # Confirm argv looks like box_runner
                cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                if any(b"box_runner.py" in c for c in cmd):
                    alive = True
            except (OSError, ValueError):
                alive = False
    return {
        "alive": alive,
        "pid": pid if alive else None,
        "deliberately_stopped": stop.is_file(),
    }


def build_snapshot(reason: str = "manual") -> dict:
    last = _read_json(ROOT / "last-check.json") or {}
    hb = _read_json(ROOT / "runner-heartbeat.json") or {}
    sched = _read_json(ROOT / "next-check-schedule.json") or {}
    cb = _read_json(ROOT / "cost-basis.json") or {}
    morning = _read_json(ROOT / "morning-balance-last.json") or {}
    days = _read_json(ROOT / "daily-balance-days.json") or {}
    prices_snap = _read_json(ROOT / "usd-price-snapshot.json") or {}

    funding = (cb.get("funding") or {}) if isinstance(cb, dict) else {}
    seh = last.get("skip_edge_halving") or (cb.get("skip_edge_halving") or {})

    balances = last.get("balances") or last.get("balances_local") or {}
    balances_usd = last.get("balances_usd") or {}
    prices = last.get("prices_usd") or (prices_snap.get("prices_usd") or {})

    # Prefer live last-check totals; fall back to morning wallet if missing.
    total_usd = last.get("total_usd")
    funded_usd = last.get("funded_usd") or funding.get("funded_usd")
    pnl_pct = last.get("unrealized_pnl_pct")
    if total_usd is None and isinstance(morning.get("main_wallet"), dict):
        total_usd = morning["main_wallet"].get("total_usd")
        funded_usd = funded_usd or morning["main_wallet"].get("funded_usd")
        pnl_pct = pnl_pct if pnl_pct is not None else morning["main_wallet"].get(
            "unrealized_pnl_pct"
        )

    # Truncate public address only (never keys).
    addr = last.get("solana") or last.get("solana_local") or funding.get("wallet") or ""
    addr_short = f"{addr[:4]}…{addr[-4:]}" if len(addr) > 10 else addr

    trades_raw = _read_jsonl_tail(ROOT / "trades.jsonl", RECENT_TRADES)
    trades = []
    for t in reversed(trades_raw):  # newest first
        trades.append(
            {
                "time_pt": t.get("time_pt"),
                "side": t.get("side"),
                "sold_asset": t.get("sold_asset"),
                "sold_amount": t.get("sold_amount"),
                "bought_asset": t.get("bought_asset"),
                "bought_amount": t.get("bought_amount"),
                "rate": t.get("rate"),
                "rate_unit": t.get("rate_unit"),
                "total_usd_after": t.get("total_usd_after"),
                "tx_url": t.get("tx_url"),
                "tx": t.get("tx"),
            }
        )

    closest = last.get("closest") or {}
    book_rows = []
    for sym in BOOK:
        book_rows.append(
            {
                "symbol": sym,
                "qty": balances.get(sym),
                "usd": balances_usd.get(sym),
                "price_usd": prices.get(sym),
            }
        )

    runner = _runner_alive()
    snap = {
        "generated_pt": _now_pt(),
        "timezone": _TZ_NAME,
        "reason": reason,
        "wallet": {
            "label": "wallet",
            "address_short": addr_short,
            "total_usd": total_usd,
            "funded_usd": funded_usd,
            "unrealized_pnl_pct": pnl_pct,
            "balances": book_rows,
        },
        "last_check": {
            "checked_pt": last.get("checked_pt"),
            "result": last.get("result"),
            "closest_route": closest.get("route"),
            "closest_gap_net_pct": closest.get("gap_net_pct"),
            "closest_edge_pct": closest.get("edge_pct"),
            "closest_required_pct": closest.get("required_pct"),
            "quote_source": last.get("quote_source"),
        },
        "quiet_check": {
            "consecutive_quiet_checks": seh.get("consecutive_quiet_checks", 0),
            "streak_halved": bool(seh.get("streak_halved", False)),
            "current_multiplier": seh.get("current_multiplier")
            or seh.get("post_multiplier"),
            "skips_since_fill": seh.get("skips_since_fill"),
        },
        "runner": {
            "alive": runner["alive"],
            "pid": runner["pid"],
            "deliberately_stopped": runner["deliberately_stopped"],
            "last_result": hb.get("last_result"),
            "last_start": hb.get("last_start"),
            "last_end": hb.get("last_end"),
            "next_at_pt": hb.get("next_at_pt") or sched.get("next_at_pt"),
            "interval_sec": hb.get("interval_sec") or sched.get("cadence_sec"),
            "consecutive_fails": hb.get("consecutive_fails"),
            "updated_pt": hb.get("updated_pt"),
        },
        "morning": {
            "day": days.get("this_morning_was_day") or morning.get("day"),
            "when_pt": morning.get("when_pt"),
            "main_wallet_total_usd": (morning.get("main_wallet") or {}).get("total_usd"),
            "main_wallet_pnl_pct": (morning.get("main_wallet") or {}).get(
                "unrealized_pnl_pct"
            ),
        },
        "prices": {
            "checked_pt": prices_snap.get("checked_pt") or last.get("checked_pt"),
            "source": prices_snap.get("source") or "last-check",
            "usd": {k: prices.get(k) for k in BOOK if k in prices},
            "change_24h_pct": {
                k: (prices_snap.get("price_change_24h_pct") or {}).get(k)
                for k in BOOK
            },
        },
        "recent_trades": trades,
        "trade_count_shown": len(trades),
        "notes": [
            "Static snapshot only — no private keys, no RPC from this page.",
            "Desktop CopyFromBox sync is CANCELLED; use this dashboard instead.",
        ],
    }
    return snap


def _ensure_bot_desk_files() -> None:
    """Keep bot-desk.html relative fetches working: sol-usdc-log.md + trades.jsonl
    in the same served directory as the desk (and index.html -> bot-desk.html).

    Prefer relative symlinks to ROOT so the desk always sees live files between
    snapshots. Fall back to atomic copy if linking fails. Never copies secrets.
    """
    DASH.mkdir(parents=True, exist_ok=True)
    # Default URL / should serve owner's desk
    index = DASH / "index.html"
    desk = DASH / "bot-desk.html"
    if desk.is_file():
        try:
            if index.is_symlink() or not index.exists():
                if index.is_symlink() and os.readlink(index) != "bot-desk.html":
                    index.unlink()
                    index.symlink_to("bot-desk.html")
                elif not index.exists():
                    index.symlink_to("bot-desk.html")
            elif index.is_file() and not index.samefile(desk):
                # Demote unknown real index once
                legacy = DASH / "index-legacy.html"
                if not legacy.exists():
                    os.replace(index, legacy)
                if not index.exists():
                    index.symlink_to("bot-desk.html")
        except OSError as e:
            _log(f"BOT_DESK_INDEX_ERR {type(e).__name__}")

    pairs = (
        ("sol-usdc-log.md", ROOT / "sol-usdc-log.md"),
        ("trades.jsonl", ROOT / "trades.jsonl"),
    )
    for name, src in pairs:
        dest = DASH / name
        if not src.is_file():
            _log(f"BOT_DESK_MISSING {name}")
            continue
        rel = os.path.relpath(src, DASH)
        try:
            if dest.is_symlink():
                if os.readlink(dest) == rel:
                    continue
                dest.unlink()
            elif dest.exists():
                # Replace stale copy with symlink when possible
                dest.unlink()
            dest.symlink_to(rel)
            continue
        except OSError:
            pass
        # Atomic copy fallback
        try:
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_bytes(src.read_bytes())
            os.replace(tmp, dest)
        except OSError as e:
            _log(f"BOT_DESK_SYNC_ERR {name} {type(e).__name__}")


def write_snapshot(reason: str = "manual") -> dict:
    DASH.mkdir(parents=True, exist_ok=True)
    _ensure_bot_desk_files()
    snap = build_snapshot(reason=reason)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snap, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, OUT)
    _log(f"SNAPSHOT_OK reason={reason} total_usd={snap['wallet'].get('total_usd')}")
    return snap


def refresh(reason: str = "manual") -> dict:
    """Public hook for fill/morning/runner callers. Never raises."""
    if not __import__("degen_home").research_logging():
        # template: dashboard/data.json and the dashboard/ copies are not read by anything (the
        # dashboard uses desk.json built by dashboard_publish), so nothing is written here.
        return {"ok": True, "skipped": "template", "reason": reason}
    try:
        return write_snapshot(reason=reason)
    except Exception as e:
        _log(f"SNAPSHOT_ERR reason={reason} {type(e).__name__}")
        return {"ok": False, "error": type(e).__name__, "reason": reason}


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    reason = args[0] if args else "cli"
    snap = write_snapshot(reason=reason)
    print(
        json.dumps(
            {
                "ok": True,
                "path": str(OUT),
                "generated_pt": snap.get("generated_pt"),
                "total_usd": (snap.get("wallet") or {}).get("total_usd"),
                "result": (snap.get("last_check") or {}).get("result"),
                "reason": reason,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
