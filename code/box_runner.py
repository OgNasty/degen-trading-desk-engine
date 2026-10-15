#!/usr/bin/env python3
"""Box-local wallet edge-check runner (no AI agent).

Runs _edge_check_run.py on a fixed 6-minute start-to-start cadence.
Posts an alert webhook only when something happened (fill, trade_fail,
hard_blocker, stop, or 3 consecutive runner crashes/timeouts).

Never prints SOLANA_TRADING_PRIVATE_KEY or solana_rpc._RPC_URLS.
Does not start itself; use runner_ctl.sh.
"""
from __future__ import annotations

import degen_home as _DH  # portable base dir (pcmove 2026-10-10)
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from degen_home import TZ_NAME as _TZ_NAME

PT = ZoneInfo(_TZ_NAME)
ROOT = Path(_DH.HOME_STR)
CHECKER = ROOT / "_edge_check_run.py"
VENV_PYTHON = _DH.VENV_PY if _DH.VENV_PY.exists() else Path(sys.executable)  # template: fall back to current python
LOG_DIR = ROOT / "runner-logs"
LOCK_PATH = ROOT / "runner.lock"
HEARTBEAT_PATH = ROOT / "runner-heartbeat.json"
SCHEDULE_PATH = ROOT / "next-check-schedule.json"
CADENCE_PATH = ROOT / "cadence.json"
EVAL_PATH = ROOT / "last-check.eval.json"

INTERVAL_SEC = _DH.check_interval_sec()  # template: configurable (box 300 s / PC 60 s default)
CHECK_TIMEOUT_SEC = 8 * 60
LOG_KEEP_DAYS = 3  # template: trimmed for use (check-in needs 24 h)
CONSEC_FAIL_ALERT = 3

# Env keys the checker / webhook need. Load by NAME only; never log values.
SECRET_ENV_NAMES = (
    "SOLANA_TRADING_PRIVATE_KEY",
    "TRADING_ALERT_WEBHOOK_URL",
    "TRADING_ALERT_WEBHOOK_KEY",
)

# Solana tx signatures are base58, typically 86–88 chars; also scrub shorter hashes.
_SIG_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{64,128}\b")
_SIG_JSON_RE = re.compile(
    r'("(?:signature|sig|tx|txid)"\s*:\s*")[^"]+(")',
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s\"']+", re.IGNORECASE)


def now_pt() -> datetime:
    return datetime.now(PT)


def iso_pt(dt: datetime | None = None) -> str:
    d = (dt or now_pt()).astimezone(PT).replace(microsecond=0)
    return d.isoformat()


def load_box_secrets_into_env() -> list[str]:
    """Copy named secrets from box-secrets card into os.environ if missing.

    Returns the list of env NAMES that were set or already present (never values).
    """
    return _DH.load_secrets_into_env(SECRET_ENV_NAMES)


def scrub_line(line: str) -> str:
    """Remove signatures and URLs from a log line before webhook/summary use."""
    s = _SIG_JSON_RE.sub(r"\1[sig_redacted]\2", line)
    s = _SIG_RE.sub("[sig_redacted]", s)
    s = _URL_RE.sub("[url_redacted]", s)
    # Belt-and-suspenders: never leak env secret values if they somehow appear.
    for name in SECRET_ENV_NAMES:
        val = os.environ.get(name) or ""
        if len(val) >= 8 and val in s:
            s = s.replace(val, f"[{name}_redacted]")
    try:
        import solana_rpc as _sr  # noqa: WPS433 — local package

        s = _sr.redact(s)
    except Exception:
        pass
    return s


def rotate_logs(today: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = (now_pt() - timedelta(days=LOG_KEEP_DAYS)).date()
    # Only YYYY-MM-DD.log daily files; other logs fail strptime and are kept.
    for path in LOG_DIR.glob("*.log"):
        try:
            d = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < cutoff:
            try:
                path.unlink()
            except OSError:
                pass


_LAST_ROTATE_DAY: str | None = None
_LAST_SIZE_CHECK = 0.0


def _size_valve() -> None:
    """Hourly stat of sol-usdc-log.md; trims oldest sections only if over 350 MB, to under 300 MB (logdiet)."""
    global _LAST_SIZE_CHECK
    import time as _t
    if _t.monotonic() - _LAST_SIZE_CHECK < 3600 and _LAST_SIZE_CHECK:
        return
    _LAST_SIZE_CHECK = _t.monotonic()
    try:
        import prune_log
        res = prune_log.maybe_prune_by_size()
        if res:
            with (LOG_DIR / f"{now_pt().strftime('%Y-%m-%d')}.log").open("a", encoding="utf-8", newline="") as f:
                f.write(f"LOG_SIZE_PRUNE {json.dumps(res)}\n")
    except Exception as e:
        try:
            with (LOG_DIR / f"{now_pt().strftime('%Y-%m-%d')}.log").open("a", encoding="utf-8", newline="") as f:
                f.write(f"LOG_SIZE_PRUNE_ERR {type(e).__name__}\n")
        except Exception:
            pass


def append_log(text: str) -> Path:
    global _LAST_ROTATE_DAY
    today = now_pt().strftime("%Y-%m-%d")
    if _LAST_ROTATE_DAY != today:  # prune old daily logs once per day (slimlog)
        rotate_logs(today)
        _LAST_ROTATE_DAY = today
    _size_valve()
    path = LOG_DIR / f"{today}.log"
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write(text)
        if not text.endswith("\n"):
            f.write("\n")
    return path


_FULL_LOG_MARKERS = (
    "TRADE_FAIL", "NOTIFY_USER", "EXEC_ERR", "EXEC ", "Traceback", "HARD_BLOCKER",
    "MULTI_FILL_DONE", "STOP_HIT", "DAY_LOSS_STOP", "SEND", "_ERR", "LATE_FILL",
)


def compact_skip_line(start_iso: str, run: dict) -> str | None:
    """One summary line for a clean quiet SKIP; None means write full stdout.

    Full output is kept for FILL, errors, non-zero exit, timeout, any send
    attempt, or any unexpected error marker in the checker output.
    """
    if run.get("timed_out") or not run.get("ok") or run.get("returncode") != 0:
        return None
    combined = run.get("combined") or ""
    summary = None
    outcome = None
    for raw in combined.splitlines():
        st = raw.strip()
        if st.startswith("LOG_SUMMARY "):
            try:
                summary = json.loads(st[len("LOG_SUMMARY "):])
            except (ValueError, TypeError):
                return None
        elif st.startswith("RESULT ") or st.startswith("RESULT\t"):
            parts = st.split(None, 1)
            outcome = parts[1].strip() if len(parts) == 2 else None
        elif any(m in st for m in _FULL_LOG_MARKERS) and not st.startswith("DASHBOARD_SNAPSHOT reason="):
            return None
    if not isinstance(summary, dict) or outcome != "SKIP" or summary.get("result") != "SKIP":
        return None
    if summary.get("send_attempt"):
        return None
    return scrub_line(
        f"{start_iso} SKIP elapsed={run.get('elapsed_sec')}s "
        f"closest={summary.get('closest')} gap={summary.get('gap_pct')}%/{summary.get('gap_net_pct')}% "
        f"quiet={summary.get('quiet')}/{summary.get('quiet_thr')} blocked={summary.get('blocked')}"
    )


def write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2) + "\n")
    tmp.replace(path)


def update_schedule(next_at: datetime, result: str) -> None:
    """Keep next-check-schedule.json current for the morning watchdog (fixed 6m)."""
    next_iso = iso_pt(next_at)
    now_iso = iso_pt()
    cron = (
        f"CRON_TZ={_TZ_NAME} {next_at.minute} {next_at.hour} "
        f"{next_at.day} {next_at.month} *"
    )
    sched = {
        "updated_pt": now_iso,
        "delay_min": 6,
        "next_at_pt": next_iso,
        "cron": cron,
        "result": result,
        "random_min": 6,
        "random_max": 6,
        "runner": "box_runner",
        "cadence_sec": INTERVAL_SEC,
    }
    write_json(SCHEDULE_PATH, sched)
    try:
        cad = json.loads(CADENCE_PATH.read_text()) if CADENCE_PATH.exists() else {}
    except (OSError, json.JSONDecodeError):
        cad = {}
    if not isinstance(cad, dict):
        cad = {}
    cad.update(
        {
            "updated_pt": now_iso,
            "mode": "fixed_6",
            "delay_min": 6,
            "delay_min_remaining_approx": 6,
            "next_at_pt": next_iso,
            "cron": cron,
            "result": result,
            "random_min": 6,
            "random_max": 6,
            "runner": "box_runner",
        }
    )
    write_json(CADENCE_PATH, cad)


def write_heartbeat(
    *,
    last_start: str | None,
    last_end: str | None,
    last_result: str | None,
    next_at_pt: str,
    extra: dict | None = None,
) -> None:
    obj = {
        "updated_pt": iso_pt(),
        "last_start": last_start,
        "last_end": last_end,
        "last_result": last_result,
        "next_at_pt": next_at_pt,
        "interval_sec": INTERVAL_SEC,
        "check_timeout_sec": CHECK_TIMEOUT_SEC,
        "pid": os.getpid(),
    }
    if extra:
        obj.update(extra)
    write_json(HEARTBEAT_PATH, obj)


def _log_webhook_resp(payload: dict, status: int, raw: bytes, url: str, key: str) -> None:
    """Log HTTP status + response body (<=500 chars), scrubbed of URL/key."""
    try:
        txt = (raw or b"").decode("utf-8", "replace")
        for secret in (key, url):
            if secret:
                txt = txt.replace(secret, "[redacted]")
        txt = re.sub(r"https?://\S+", "[url]", txt)
        txt = re.sub(r"(?i)(key|token|secret|bearer)([\"'=: ]+)[A-Za-z0-9._\-]{8,}", r"\1\2[redacted]", txt)
        txt = " ".join(txt.split())[:500]
        append_log(f"{iso_pt()} WEBHOOK_RESP kind={payload.get('kind')} http={status} body={txt!r}")
    except Exception:
        pass


def post_webhook(payload: dict) -> int:
    """POST JSON alert. Returns HTTP status (or 0 on local failure). Never prints the key."""
    url = (os.environ.get("TRADING_ALERT_WEBHOOK_URL") or "").strip()
    key = (os.environ.get("TRADING_ALERT_WEBHOOK_KEY") or "").strip()
    if not url:
        append_log(f"{iso_pt()} WEBHOOK_SKIP missing TRADING_ALERT_WEBHOOK_URL kind={payload.get('kind')}")
        return 0
    body = json.dumps(payload, default=str).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "degen-trading-box-runner/1.0",
    }
    if key:
        headers["Authorization"] = f"Bearer {key}"
        headers["X-Webhook-Key"] = key
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            st = int(getattr(resp, "status", 0) or 0)
            _log_webhook_resp(payload, st, resp.read(2000), url, key)
            return st
    except urllib.error.HTTPError as e:
        try:
            rb = e.read(2000)
        except Exception:
            rb = b""
        _log_webhook_resp(payload, int(e.code or 0), rb, url, key)
        return int(e.code or 0)
    except Exception as e:
        append_log(f"{iso_pt()} WEBHOOK_ERR {type(e).__name__}")
        return 0


def parse_markers(combined: str) -> dict:
    """Parse checker stdout/stderr for alert-worthy markers."""
    lines = combined.splitlines()
    result_outcome: str | None = None
    trade_fail_lines: list[str] = []
    notify_lines: list[str] = []
    hard_blocker_lines: list[str] = []
    stop_lines: list[str] = []
    fill_lines: list[str] = []
    late_fill_lines: list[str] = []
    relevant: list[str] = []

    for raw in lines:
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped:
            continue
        upper = stripped.upper()

        if stripped.startswith("RESULT ") or stripped.startswith("RESULT\t"):
            parts = stripped.split(None, 1)
            if len(parts) == 2:
                result_outcome = parts[1].strip()
                relevant.append(scrub_line(stripped))
                if result_outcome == "FILL":
                    fill_lines.append(scrub_line(stripped))
                elif result_outcome == "HARD_BLOCKER":
                    hard_blocker_lines.append(scrub_line(stripped))

        if stripped.startswith("- LATE_FILL ") or stripped.startswith("LATE_FILL "):
            lf = scrub_line(stripped[2:] if stripped.startswith("- ") else stripped)
            if lf not in late_fill_lines:
                late_fill_lines.append(lf)
                relevant.append(lf)

        if stripped.startswith("TRADE_FAIL") and "PAPER_TRADING: send skipped" not in stripped:
            trade_fail_lines.append(scrub_line(stripped))
            relevant.append(scrub_line(stripped))

        if stripped.startswith("NOTIFY_USER trade_failed"):
            notify_lines.append(scrub_line(stripped))
            relevant.append(scrub_line(stripped))

        # Dedicated stop markers (agent-era wording); checker may not emit these.
        if (
            "DAY_LOSS_STOP" in upper
            or "WEEKLY_USAGE_STOP" in upper
            or "DAY-LOSS STOP" in upper
            or "DAY LOSS STOP" in upper
            or "WEEKLY USAGE STOP" in upper
            or stripped.startswith("STOP ")
            or stripped.startswith("STOP_HIT")
        ):
            # Ignore NEXT / normal schedule noise
            if not stripped.startswith("NEXT "):
                stop_lines.append(scrub_line(stripped))
                relevant.append(scrub_line(stripped))

        if "HARD_BLOCKER" in stripped and not stripped.startswith("RESULT "):
            # e.g. notes or skip_edge_halving line
            if "skip_edge_halving unchanged (HARD_BLOCKER)" in stripped:
                hard_blocker_lines.append(scrub_line(stripped))
                relevant.append(scrub_line(stripped))

        if stripped.startswith("MULTI_FILL_DONE") or stripped.startswith("EXEC "):
            if result_outcome == "FILL" or stripped.startswith("MULTI_FILL_DONE"):
                fill_lines.append(scrub_line(stripped))
                relevant.append(scrub_line(stripped))

    # Post-run eval file: day_loss_stop.hit (checker currently folds this into HARD_BLOCKER)
    day_loss_hit = False
    try:
        if EVAL_PATH.exists():
            ev = json.loads(EVAL_PATH.read_text())
            ds = ev.get("day_loss_stop") if isinstance(ev, dict) else None
            if isinstance(ds, dict) and ds.get("hit"):
                day_loss_hit = True
                stop_lines.append(
                    scrub_line(
                        f"DAY_LOSS_STOP hit status={ds.get('status')} "
                        f"reason={ds.get('reason')}"
                    )
                )
                relevant.append(stop_lines[-1])
    except (OSError, json.JSONDecodeError):
        pass

    return {
        "result_outcome": result_outcome,
        "trade_fail_lines": trade_fail_lines,
        "notify_lines": notify_lines,
        "hard_blocker_lines": hard_blocker_lines,
        "stop_lines": stop_lines,
        "fill_lines": fill_lines,
        "late_fill_lines": late_fill_lines,
        "relevant": relevant,
        "day_loss_hit": day_loss_hit,
        "has_trade_fail": bool(trade_fail_lines or notify_lines),
        # empty wallet (no SOL/USDC/cbBTC/WETH): None when the check didn't report it
        "wallet_empty": (True if "MIGRATION_BLOCKER True" in combined
                         else False if "MIGRATION_BLOCKER False" in combined else None),
    }


AWAIT_DEPOSIT_STATE = ROOT / "awaiting-deposit.json"


def _awaiting_deposit(rearm: bool = False) -> bool:
    """rearm=True: wallet holds funds, clear the sent flag. Else: True exactly once per empty spell."""
    try:
        st = json.loads(AWAIT_DEPOSIT_STATE.read_text(encoding="utf-8"))
    except Exception:
        st = {}
    if rearm:
        if st.get("sent"):
            AWAIT_DEPOSIT_STATE.write_text(json.dumps({"sent": False}), encoding="utf-8")
        return False
    if st.get("sent"):
        return False
    AWAIT_DEPOSIT_STATE.write_text(json.dumps({"sent": True, "at": iso_pt()}), encoding="utf-8")
    return True


def _awaiting_deposit_alert(t: str) -> dict:
    return {"kind": "awaiting_deposit", "time_pt": t,
            "summary": "Your wallet's empty right now, so there's nothing for me to trade yet. "
                       "I'll stay quiet until your first deposit shows up.",
            "lines": []}


def decide_alerts(parsed: dict, run_meta: dict) -> list[dict]:
    """Build zero or more webhook payloads for this check."""
    alerts: list[dict] = []
    t = iso_pt()
    outcome = parsed.get("result_outcome")

    if outcome == "FILL" or parsed.get("fill_lines"):
        lines = parsed["fill_lines"] or [f"RESULT {outcome}"]
        alerts.append(
            {
                "kind": "fill",
                "time_pt": t,
                "summary": "I just made a trade. The details are below and on your dashboard.",
                "lines": lines[:40],
            }
        )

    if parsed.get("late_fill_lines"):
        # Not a fill alert (those stay muted) and not a trade failure: it does not
        # count toward or reset the consecutive-fail counter.
        lines = parsed["late_fill_lines"]
        alerts.append(
            {
                "kind": "late_fill",
                "type": "late_fill",
                "time_pt": t,
                "summary": (
                    f"Good news: {'a trade I thought had failed' if len(lines) == 1 else str(len(lines)) + ' trades I thought had failed'} "
                    "actually went through. I've now logged it in your trade history, so your numbers are right."
                ),
                "lines": lines[:40],
                "counts_toward_fail_streak": False,
                "notify_owner": True,
            }
        )

    if parsed.get("has_trade_fail"):
        lines = list(parsed["trade_fail_lines"]) + list(parsed["notify_lines"])
        alerts.append(
            {
                "kind": "trade_fail",
                "time_pt": t,
                "summary": "A trade didn't go through. Nothing was lost, and I'll keep checking prices as usual. The technical details are below.",
                "lines": [scrub_line(x) for x in lines[:40]],
            }
        )

    if parsed.get("wallet_empty") is False:
        _awaiting_deposit(rearm=True)  # wallet funded: a later empty wallet gets one new notice
    if (outcome == "HARD_BLOCKER" or parsed.get("hard_blocker_lines")) and parsed.get("wallet_empty") \
            and not parsed.get("day_loss_hit") and not parsed.get("stop_lines"):
        # template: an empty wallet is not an error. ONE "awaiting your first deposit" notice, then quiet
        # until a deposit lands; re-armed only after the wallet held funds again.
        if _awaiting_deposit():
            alerts.append(_awaiting_deposit_alert(t))
    elif outcome == "HARD_BLOCKER" or parsed.get("hard_blocker_lines"):
        lines = parsed["hard_blocker_lines"] or [f"RESULT {outcome}"]
        alerts.append(
            {
                "kind": "hard_blocker",
                "time_pt": t,
                "summary": "Something is stopping me from trading right now, so I'm sitting this check out. The details are below.",
                "lines": lines[:40],
            }
        )

    if parsed.get("stop_lines") or parsed.get("day_loss_hit"):
        lines = parsed["stop_lines"] or ["stop triggered"]
        alerts.append(
            {
                "kind": "stop",
                "time_pt": t,
                "summary": "I've paused trading for now because one of my safety limits was hit. I'll pick back up on my own when it clears.",
                "lines": lines[:40],
            }
        )

    if run_meta.get("runner_error_alert"):
        alerts.append(
            {
                "kind": "runner_error",
                "time_pt": t,
                "summary": run_meta.get("runner_error_summary")
                or f"My price check has failed {CONSEC_FAIL_ALERT} times in a row. I'll keep retrying.",
                "lines": [scrub_line(x) for x in (run_meta.get("runner_error_lines") or [])[:40]],
            }
        )

    return alerts


def run_checker_once() -> dict:
    """Run one edge check with hard timeout. Does not print secrets."""
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # Ensure secrets loaded for child
    load_box_secrets_into_env()
    for name in SECRET_ENV_NAMES:
        if name in os.environ:
            env[name] = os.environ[name]

    cmd = [str(VENV_PYTHON), str(CHECKER)]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CHECK_TIMEOUT_SEC,
        )
        elapsed = time.monotonic() - started
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "timed_out": False,
            "elapsed_sec": round(elapsed, 2),
            "stdout": stdout,
            "stderr": stderr,
            "combined": stdout + ("\n" + stderr if stderr else ""),
        }
    except subprocess.TimeoutExpired as e:
        elapsed = time.monotonic() - started
        stdout = (e.stdout or "") if isinstance(e.stdout, str) else (
            e.stdout.decode("utf-8", errors="replace") if e.stdout else ""
        )
        stderr = (e.stderr or "") if isinstance(e.stderr, str) else (
            e.stderr.decode("utf-8", errors="replace") if e.stderr else ""
        )
        return {
            "ok": False,
            "returncode": -9,
            "timed_out": True,
            "elapsed_sec": round(elapsed, 2),
            "stdout": stdout,
            "stderr": stderr,
            "combined": stdout + ("\n" + stderr if stderr else "") + "\nRUNNER_TIMEOUT\n",
        }
    except Exception as e:
        elapsed = time.monotonic() - started
        return {
            "ok": False,
            "returncode": -1,
            "timed_out": False,
            "elapsed_sec": round(elapsed, 2),
            "stdout": "",
            "stderr": f"RUNNER_SPAWN_ERR {type(e).__name__}",
            "combined": f"RUNNER_SPAWN_ERR {type(e).__name__}\n",
        }


def acquire_lock():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_PATH, "a+", encoding="utf-8")
    if not _DH.try_lock(fh):
        fh.close()
        print("another runner holds the lock; exiting", file=sys.stderr)
        sys.exit(2)
    fh.seek(0)
    fh.truncate()
    fh.write(f"pid={os.getpid()} started={iso_pt()}\n")
    fh.flush()
    return fh



def _template_seed_gate() -> bool:
    """Template: True when every pair has a starting rate. If not and config seed_mode is "deposit",
    try seeding from the first deposit (live quotes) right now. Never trades while unseeded."""
    try:
        cb = json.loads((ROOT / "cost-basis.json").read_text(encoding="utf-8"))
    except Exception:
        return False
    if all(p.get("rate") for p in (cb.get("pairs") or {}).values()):
        return True
    if (_DH.config().get("seed_mode") or "").lower() == "deposit":
        import subprocess
        r = subprocess.run([str(sys.executable), str(ROOT / "seed_prices.py"), "--mode", "deposit"],
                           cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        append_log(f"{iso_pt()} SEED_DEPOSIT_CHECK rc={r.returncode}")
        if r.returncode == 2 and _awaiting_deposit():
            # New wallet still empty: checks don't run until the first deposit seeds prices,
            # so the ONE awaiting-deposit notice goes out from here (then quiet until funded).
            try:
                st = post_webhook(_awaiting_deposit_alert(iso_pt()))
                append_log(f"{iso_pt()} WEBHOOK_POST kind=awaiting_deposit http={st}")
            except Exception as _aw_err:
                append_log(f"{iso_pt()} WEBHOOK_ERR kind=awaiting_deposit {type(_aw_err).__name__}")
        if r.returncode == 0:
            _awaiting_deposit(rearm=True)
        return r.returncode == 0 and _template_seed_gate_done()
    append_log(f"{iso_pt()} WAITING_FOR_SEED run seed_prices.py (deposit / test-trade / manual)")
    return False


def _template_seed_gate_done() -> bool:
    try:
        cb = json.loads((ROOT / "cost-basis.json").read_text(encoding="utf-8"))
        return all(p.get("rate") for p in (cb.get("pairs") or {}).values())
    except Exception:
        return False


def main_loop() -> int:
    if _DH.MIRROR_FLAG.exists():
        # Box mirror guard (2026-10-10): the live bot runs on the owner's PC.
        append_log(f"{iso_pt()} RUNNER_REFUSED MIRROR_ONLY present; this copy must not trade")
        print("MIRROR_ONLY present: this copy is a mirror and must not trade; refusing to start", file=sys.stderr)
        return 3
    if not _DH.run_side_ok():
        append_log(f"{iso_pt()} RUNNER_REFUSED run_side={_DH.run_side()} this_side={_DH.THIS_SIDE}")
        print(f"run_side is {_DH.run_side()!r}; this is the {_DH.THIS_SIDE!r} side; refusing to start", file=sys.stderr)
        return 3
    load_box_secrets_into_env()
    if not (os.environ.get("SOLANA_TRADING_PRIVATE_KEY") or "").strip():
        append_log(f"{iso_pt()} FATAL missing SOLANA_TRADING_PRIVATE_KEY (env/box-secrets card)")
        print("missing SOLANA_TRADING_PRIVATE_KEY", file=sys.stderr)
        return 1

    lock_fh = acquire_lock()
    consec_fails = 0
    append_log(f"{iso_pt()} RUNNER_START pid={os.getpid()} interval_sec={INTERVAL_SEC}")

    # Graceful stop on SIGTERM/SIGINT
    stopping = {"flag": False}

    def _stop(signum, frame):  # noqa: ARG001
        stopping["flag"] = True
        append_log(f"{iso_pt()} RUNNER_SIGNAL {signum}")

    for _sig in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGBREAK", None)):
        if _sig is not None:
            try:
                signal.signal(_sig, _stop)
            except (ValueError, OSError):
                pass

    try:
        while not stopping["flag"]:
            if not _DH.run_side_ok():  # template: side switched mid-run -> stop, never double trade
                append_log(f"{iso_pt()} RUNNER_EXIT run_side={_DH.run_side()} this_side={_DH.THIS_SIDE}")
                break
            if _DH.trading_paused():  # template kill switch: no checks, no trades while paused
                append_log(f"{iso_pt()} PAUSED (TRADING_PAUSED) - skipping check")
                for _ in range(INTERVAL_SEC):
                    if stopping["flag"] or not _DH.trading_paused():
                        break
                    time.sleep(1)
                continue
            if not _template_seed_gate():
                for _ in range(INTERVAL_SEC):
                    if stopping["flag"]:
                        break
                    time.sleep(1)
                continue
            cycle_start = now_pt()
            next_at = _DH.next_jittered(cycle_start, INTERVAL_SEC)  # v1.0.0: per-install offset within the interval
            start_iso = iso_pt(cycle_start)
            next_iso = iso_pt(next_at)

            write_heartbeat(
                last_start=start_iso,
                last_end=None,
                last_result="running",
                next_at_pt=next_iso,
            )
            # Pre-announce for watchdog in case the check is long
            update_schedule(next_at, "running")

            run = run_checker_once()
            compact = compact_skip_line(start_iso, run)
            if compact is not None:
                # Quiet SKIP with no send attempt: one summary line (slimlog 2026-10-11).
                append_log(compact)
            else:
                append_log(f"\n===== {start_iso} check start =====")
                # Scrub before persisting anything that might hold RPC URLs
                combined_scrubbed = "\n".join(
                    scrub_line(ln) for ln in (run["combined"] or "").splitlines()
                )
                append_log(combined_scrubbed)
                append_log(
                    f"===== {iso_pt()} check end rc={run['returncode']} "
                    f"timed_out={run['timed_out']} elapsed_sec={run['elapsed_sec']} ====="
                )

            parsed = parse_markers(run["combined"] or "")
            outcome = parsed.get("result_outcome") or (
                "TIMEOUT" if run["timed_out"] else f"EXIT_{run['returncode']}"
            )

            run_meta: dict = {}
            failed = (not run["ok"]) or run["timed_out"]
            if failed:
                consec_fails += 1
            else:
                consec_fails = 0

            if consec_fails >= CONSEC_FAIL_ALERT and failed:
                run_meta["runner_error_alert"] = True
                run_meta["runner_error_summary"] = (
                    f"My price check has failed {consec_fails} times in a row, so I may be missing trades. "
                    f"I'll keep retrying. If it keeps happening, ask me to take a look."
                )
                tail = (run["combined"] or "").strip().splitlines()[-15:]
                run_meta["runner_error_lines"] = [
                    scrub_line(x) for x in tail
                ] or [f"rc={run['returncode']} timed_out={run['timed_out']}"]
                # Reset so we don't alert every subsequent failure forever;
                # require another full streak.
                consec_fails = 0

            alerts = decide_alerts(parsed, run_meta)
            for payload in alerts:
                status = post_webhook(payload)
                append_log(
                    f"{iso_pt()} WEBHOOK_POST kind={payload.get('kind')} http={status}"
                )

            # Dashboard snapshot: after every check (keeps quiet streak / runner fresh)
            # and always after fills. Desktop CopyFromBox CANCELLED. Never wakes chat.
            try:
                import dashboard_snapshot as _dash
                _r = "box_runner_fill" if (outcome == "FILL" or parsed.get("fill_lines")) else "box_runner_check"
                _dash.refresh(reason=_r)
                append_log(f"{iso_pt()} DASHBOARD_SNAPSHOT reason={_r}")
            except Exception as _dash_err:
                append_log(f"{iso_pt()} DASHBOARD_SNAPSHOT_ERR {type(_dash_err).__name__}")

            # Publish dashboard to Cloudflare Worker (your-dashboard.example.com). Detached,
            # non-blocking; the script dedupes by hash and logs to
            # runner-logs/dashboard-publish.log. Errors here never affect trading.
            try:
                subprocess.Popen(
                    [str(VENV_PYTHON), str(ROOT / "dashboard_publish.py")],
                    cwd=str(ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    **_DH.detached_popen_kwargs(),
                )
            except Exception as _pub_err:
                append_log(f"{iso_pt()} DASHBOARD_PUBLISH_ERR {type(_pub_err).__name__}")

            end_iso = iso_pt()
            # Checker may have overwritten schedule with random 10–25; restore fixed 6m.
            update_schedule(next_at, outcome if not run["timed_out"] else "TIMEOUT")
            write_heartbeat(
                last_start=start_iso,
                last_end=end_iso,
                last_result=outcome,
                next_at_pt=next_iso,
                extra={
                    "last_returncode": run["returncode"],
                    "last_timed_out": run["timed_out"],
                    "last_elapsed_sec": run["elapsed_sec"],
                    "last_alert_kinds": [a.get("kind") for a in alerts],
                    "consecutive_fails": consec_fails,
                },
            )

            # Sleep start-to-start for INTERVAL_SEC
            while not stopping["flag"]:
                remaining = (next_at - now_pt()).total_seconds()
                if remaining <= 0:
                    break
                time.sleep(min(remaining, 5.0))
    finally:
        _DH.unlock(lock_fh)
        lock_fh.close()
        append_log(f"{iso_pt()} RUNNER_STOP pid={os.getpid()}")
    return 0


def cmd_test_webhook() -> int:
    load_box_secrets_into_env()
    status = post_webhook({"test": True, "kind": "test", "type": "test", "message": "webhook test, ignore"})
    # Print ONLY the HTTP status code (or 0)
    print(status)
    return 0 if status and 200 <= status < 300 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Box-local wallet edge-check runner")
    parser.add_argument(
        "--test-webhook",
        action="store_true",
        help="POST {test:true, kind:test} once; print only HTTP status code",
    )
    args = parser.parse_args(argv)
    if args.test_webhook:
        return cmd_test_webhook()
    return main_loop()


if __name__ == "__main__":
    sys.exit(main())
