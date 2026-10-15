#!/usr/bin/env python3
"""Publish the bot-desk dashboard to the Cloudflare Worker `degen-dashboard`.

Serves https://your-dashboard.example.com via a Workers route (no tunnel needed; HTTPS only).
Stages dashboard files into dashboard-worker/public/, hashes them, and runs
`wrangler deploy` only when something changed. Quiet; one log line per deploy or
error in runner-logs/dashboard-publish.log. Never prints the API token.
Safe to call after every runner check: non-locking for trading, exits fast if
another publish is running, hard timeout on the deploy.
"""
from __future__ import annotations

import degen_home as _DH  # portable base dir (pcmove 2026-10-10)
import desk_json  # desk.json is the only data file published (deskjson 2026-10-11)
import base64
import urllib.error
import urllib.request
import uuid
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from degen_home import TZ_NAME as _TZ_NAME

PT = ZoneInfo(_TZ_NAME)
ROOT = Path(_DH.HOME_STR)
DASH = ROOT / "dashboard"
WORKER_DIR = ROOT / "dashboard-worker"
PUBLIC = WORKER_DIR / "public"
STATE = ROOT / "dashboard-publish-state.json"
LOCK = ROOT / "dashboard-publish.lock"
LOG = ROOT / "runner-logs" / "dashboard-publish.log"
WRANGLER_LOGS = ROOT / "runner-logs" / "wrangler"
WRANGLER = "wrangler"  # legacy; REST upload is used now (pcmove 2026-10-10)
CF_API = "https://api.cloudflare.com/client/v4"
SCRIPT_NAME = (os.environ.get("DEGEN_WORKER_NAME") or "degen-dashboard").strip()
LAST_UPLOAD = (0, 0)  # (files, bytes) actually sent in the last deploy
DEPLOY_TIMEOUT_SEC = 150
LOG_MAX_BYTES = 256 * 1024

# staged name -> source path (symlinks resolved)
FILES = {
    "index.html": DASH / "bot-desk.html",
    "bot-desk.html": DASH / "bot-desk.html",
    "favicon.png": DASH / "favicon.png",
    "image.jpg": DASH / "image.jpg",
    "metaimage.jpg": DASH / "metaimage.jpg",
    "piespider.png": DASH / "piespider.png",
}
# deskjson 2026-10-11: sol-usdc-log.md and trades.jsonl stay on the PC (no log/ chunks);
# the dashboard reads the compact desk.json built from them after each check.
DESK_NAME = "desk.json"
CODE_FILES = (WORKER_DIR / "wrangler.toml", WORKER_DIR / "src" / "index.js")


def iso() -> str:
    return datetime.now(PT).replace(microsecond=0).isoformat()


def log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > LOG_MAX_BYTES:
            tail = LOG.read_bytes()[-LOG_MAX_BYTES // 2:]
            LOG.write_bytes(tail)
        with LOG.open("a", encoding="utf-8", newline="") as f:
            f.write(f"{iso()} {msg}\n")
    except Exception:
        pass


def token() -> str:
    """CLOUDFLARE_API_TOKEN from secrets.json (PC) or the box-secrets card; env fallback."""
    return (_DH.get_secret("CLOUDFLARE_API_TOKEN") or "").strip()


def account_id() -> str:
    v = _DH.get_secret("CLOUDFLARE_ACCOUNT_ID")
    if v:
        return v
    for ln in (WORKER_DIR / "wrangler.toml").read_text(encoding="utf-8").splitlines():
        if ln.strip().startswith("account_id"):
            return ln.split("=", 1)[1].strip().strip('"')
    raise RuntimeError("no account id")


def _req(method, url, tok, body=None, ctype=None, timeout=60):
    hdr = {"Authorization": f"Bearer {tok}", "User-Agent": "degen-publish/1"}
    if ctype:
        hdr["Content-Type"] = ctype
    r = urllib.request.Request(url, data=body, method=method, headers=hdr)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            data = {}
        return e.code, data


def _multipart(parts):
    """parts: list of (name, filename|None, content_type, bytes)."""
    b = "----degen" + uuid.uuid4().hex
    out = bytearray()
    for name, fname, ct, data in parts:
        out += f"--{b}\r\n".encode()
        disp = f'form-data; name="{name}"' + (f'; filename="{fname}"' if fname else "")
        out += f"Content-Disposition: {disp}\r\nContent-Type: {ct}\r\n\r\n".encode()
        out += data + b"\r\n"
    out += f"--{b}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={b}"


def _errs(data) -> str:
    try:
        return ";".join(f"{e.get('code')}:{str(e.get('message'))[:120]}" for e in (data.get("errors") or []))
    except Exception:
        return "?"


def deploy_rest(tok: str) -> None:
    """Workers static-assets direct upload + script PUT (same result as `wrangler deploy`).

    Routes (your-dashboard.example.com/*) are kept; they are not part of the script upload.
    """
    acct = account_id()
    base = f"{CF_API}/accounts/{acct}/workers"
    files = {}
    for f in sorted(PUBLIC.rglob("*")):
        if not f.is_file() or f.name.startswith("."):
            continue
        data = f.read_bytes()
        b64 = base64.b64encode(data).decode()
        h = hashlib.sha256((b64 + f.suffix.lstrip(".")).encode()).hexdigest()[:32]
        files["/" + f.relative_to(PUBLIC).as_posix()] = (h, len(data), b64, f.suffix)
    manifest = {k: {"hash": v[0], "size": v[1]} for k, v in files.items()}
    st, data = _req("POST", f"{base}/scripts/{SCRIPT_NAME}/assets-upload-session", tok,
                    json.dumps({"manifest": manifest}).encode(), "application/json")
    if st != 200 or not data.get("success"):
        raise RuntimeError(f"upload_session http={st} {_errs(data)}")
    res = data.get("result") or {}
    jwt = res.get("jwt")
    by_hash = {v[0]: v for v in files.values()}
    buckets = res.get("buckets") or []
    sent = sum(by_hash[hh][1] for b in buckets for hh in b)
    global LAST_UPLOAD
    LAST_UPLOAD = (sum(len(b) for b in buckets), sent)
    for bucket in buckets:  # Cloudflare lists only hashes it does not already have
        parts = [(hh, hh, "application/null", by_hash[hh][2].encode()) for hh in bucket]
        body, ct = _multipart(parts)
        st, d2 = _req("POST", f"{base}/assets/upload?base64=true", jwt, body, ct, timeout=120)
        if st not in (200, 201, 202) or d2.get("success") is False:
            raise RuntimeError(f"assets_upload http={st} {_errs(d2)}")
        jwt = (d2.get("result") or {}).get("jwt") or jwt
    meta = {
        "main_module": "index.js",
        "compatibility_date": "2025-03-01",
        "assets": {"jwt": jwt, "config": {"html_handling": "none", "not_found_handling": "none", "run_worker_first": True}},
        "bindings": [{"type": "assets", "name": "ASSETS"}],
    }
    js = (WORKER_DIR / "src" / "index.js").read_bytes()
    body, ct = _multipart([
        ("metadata", None, "application/json", json.dumps(meta).encode()),
        ("index.js", "index.js", "application/javascript+module", js),
    ])
    st, d3 = _req("PUT", f"{base}/scripts/{SCRIPT_NAME}", tok, body, ct, timeout=120)
    if st != 200 or not d3.get("success"):
        raise RuntimeError(f"script_put http={st} {_errs(d3)}")
    # template: trading never runs on Cloudflare; clear any cron triggers on this Worker.
    try:
        _req("PUT", f"{base}/scripts/{SCRIPT_NAME}/schedules", tok, b"[]", "application/json")
    except Exception:
        pass
    # template: make sure the free workers.dev hostname is on, and log it once.
    try:
        _req("POST", f"{base}/scripts/{SCRIPT_NAME}/subdomain", tok,
             json.dumps({"enabled": True, "previews_enabled": False}).encode(), "application/json")
        st4, d4 = _req("GET", f"{CF_API}/accounts/{acct}/workers/subdomain", tok)
        sub = ((d4 or {}).get("result") or {}).get("subdomain")
        if sub:
            log(f"PUBLISH_URL https://{SCRIPT_NAME}.{sub}.workers.dev/")
    except Exception:
        pass


def scrub(text: str, secret: str) -> str:
    if secret:
        text = text.replace(secret, "[token_redacted]")
    return text


def stage() -> dict[str, str]:
    """Copy sources into PUBLIC atomically per file; return name->sha256."""
    PUBLIC.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    for name, src in FILES.items():
        if not src.exists():  # template: optional images may be absent
            continue
        data = src.resolve().read_bytes()
        dst = PUBLIC / name
        tmp = dst.with_name(f".{name}.tmp")
        tmp.write_bytes(data)
        tmp.replace(dst)
        hashes[name] = hashlib.sha256(data).hexdigest()
    # template: shared mode flag both sides check before trading (degen_home.remote_run_side)
    mdata = json.dumps({"run_side": _DH.run_side(), "set_by": _DH.THIS_SIDE, "paused": _DH.PAUSE_FILE.exists()}).encode()
    (PUBLIC / "mode.json").write_bytes(mdata)
    hashes["mode.json"] = hashlib.sha256(mdata).hexdigest()
    # logdiet 2026-10-11: desk.json (small, every poll) + fills.json (fetched when fv changes)
    dk, fl = desk_json.split(desk_json.build())
    for nm, obj in ((DESK_NAME, dk), (desk_json.FILLS_NAME, fl)):
        data = desk_json.dumps(obj)
        dst = PUBLIC / nm
        tmp = dst.with_name(f".{nm}.tmp")
        tmp.write_bytes(data)
        tmp.replace(dst)
        hashes[nm] = hashlib.sha256(data).hexdigest()
    for extra in PUBLIC.iterdir():  # never ship stray files (old log/, trades.jsonl, sol-usdc-log.md)
        if extra.name not in FILES and extra.name not in (DESK_NAME, desk_json.FILLS_NAME):
            if extra.is_dir():
                shutil.rmtree(extra, ignore_errors=True)
            else:
                extra.unlink(missing_ok=True)
    for cf in CODE_FILES:
        hashes[f"code:{cf.name}"] = hashlib.sha256(cf.read_bytes()).hexdigest()
    return hashes


def prune_wrangler_logs() -> None:
    cutoff = time.time() - 86400
    try:
        for p in WRANGLER_LOGS.glob("*"):
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
    except Exception:
        pass


def main(argv: list[str]) -> int:
    force = "--force" in argv
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    if _DH.MIRROR_FLAG.exists() and "--allow-mirror" not in argv:
        return 0  # box mirror: the PC publishes; never overwrite its newer data
    lock_fh = open(LOCK, "a+")
    if not _DH.try_lock(lock_fh):
        lock_fh.close()
        return 0  # another publish in flight
    try:
        hashes = stage()
        try:
            prev = json.loads(STATE.read_text()).get("hashes") or {}
        except Exception:
            prev = {}
        if not force and prev == hashes:
            return 0
        changed = sorted(k for k in hashes if prev.get(k) != hashes[k])

        tok = token()
        if not tok:
            log("PUBLISH_ERR missing CLOUDFLARE_API_TOKEN")
            return 1
        t0 = time.monotonic()
        try:
            deploy_rest(tok)
        except Exception as e:
            dt = round(time.monotonic() - t0, 1)
            log(f"PUBLISH_ERR sec={dt} {scrub(str(e), tok)[:800]}")
            return 1
        dt = round(time.monotonic() - t0, 1)
        tmp = STATE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"published_pt": iso(), "hashes": hashes}, indent=2) + "\n")
        tmp.replace(STATE)
        log(f"PUBLISHED sec={dt} uploaded_files={LAST_UPLOAD[0]} uploaded_bytes={LAST_UPLOAD[1]} changed={','.join(changed)[:600]}")
        prune_wrangler_logs()
        return 0
    except Exception as e:  # never raise into the runner
        log(f"PUBLISH_ERR {type(e).__name__}")
        return 1
    finally:
        _DH.unlock(lock_fh)
        lock_fh.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
