"""Shared Solana JSON-RPC client.

Reads fall through helius, then alchemy, then public mainnet-beta.
Sends are helius first. One later Alchemy send is allowed only after a
fresh quote, with a new Alchemy blockhash and a new signature. Public
mainnet-beta is never a send endpoint. rpc_send talks to one named
provider and does not fall through. A failed JSON-RPC body is logged
(provider name, error.message, error.code, error.data.logs) before any
read failover and before a send gives up. missing_result is only a 2xx
body with neither result nor error.

This module does not print URLs or API keys and does not query a wallet.
"""

import os
import json
import re
import subprocess
import time
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from degen_home import TZ_NAME as _TZ_NAME

# Log names only. Full URLs stay in this file, not in cost-basis.json.
ENDPOINT_ORDER = ("helius", "alchemy", "mainnet-beta")

import degen_home as _DHM
_DH = _DHM
_PUBLIC_RPC = "https://api.mainnet-beta.solana.com"


def _env_url(name):
    v = _DHM.get_secret(name)
    return v.strip() if isinstance(v, str) and v.strip() else None


# template: Helius is required; Alchemy is an optional backup; public mainnet-beta is read-only last resort.
# Sources: HELIUS_RPC_URL / ALCHEMY_RPC_URL, or legacy SOLANA_RPC_URLS ("helius[,alchemy][,public]").
_raw_urls = _DHM.get_secret("SOLANA_RPC_URLS") or ()
if isinstance(_raw_urls, str):
    _raw_urls = [u.strip() for u in _raw_urls.split(",") if u.strip()]
_raw_urls = [u for u in _raw_urls if u.rstrip("/") != _PUBLIC_RPC]
_helius = _env_url("HELIUS_RPC_URL") or (_raw_urls[0] if _raw_urls else None)
_alchemy = _env_url("ALCHEMY_RPC_URL") or (_raw_urls[1] if len(_raw_urls) > 1 else None)
if not _helius:
    raise RuntimeError("HELIUS_RPC_URL (or SOLANA_RPC_URLS) missing from .env / secrets.json")
if _alchemy:
    ENDPOINT_ORDER = ("helius", "alchemy", "mainnet-beta")
    _RPC_URLS = (_helius, _alchemy, _PUBLIC_RPC)
else:
    ENDPOINT_ORDER = ("helius", "mainnet-beta")
    _RPC_URLS = (_helius, _PUBLIC_RPC)
HAS_ALCHEMY = bool(_alchemy)


def endpoint_hosts():
    """Hostnames in try order. No keys and no paths."""
    return tuple(urlsplit(url).hostname for url in _RPC_URLS)



READ_ENDPOINT_ORDER = ENDPOINT_ORDER
# Sends: Helius first, then one Alchemy attempt after a new quote.
# Public mainnet-beta is not a send endpoint.
SEND_ENDPOINTS = ("helius", "alchemy") if HAS_ALCHEMY else ("helius",)
SEND_COMMITMENT = "confirmed"
SEND_MAX_RETRIES = 0
# Accepted Helius signature only. Not used for preflight or a missing signature.
HELIUS_ACCEPTED_CONFIRM_S = 10.0
# Duplicate-send guard (2026-10-08, after the 05:58 PT double fill). Before any
# re-quote / re-send for the same candidate, every signature already sent is
# polled with searchTransactionHistory=true for up to PRIOR_SEND_POLL_S. A prior
# send that landed is the fill. One still unknown before its blockhash expired
# blocks the re-send (wait for expiry; the next check is minutes later).
PRIOR_SEND_POLL_S = 20.0
PRIOR_SEND_POLL_STEP_S = 2.0
# Known not landed (rejected / landed with error / expired unseen) or landed.
_CLOSED_SEND_STATES = ("chain_err", "ok", "expired", "rejected")
# Dropped-send wait (2026-10-08, owner approved). When the first Helius send
# is not confirmed in HELIUS_ACCEPTED_CONFIRM_S, keep polling every prior send
# for this candidate until it lands (that is the fill) or is PROVABLY expired:
# block height on the host that issued its blockhash > its lastValidBlockHeight,
# then one more full-history status read that still finds nothing. Only then
# the existing fresh-quote Alchemy send may run. Not provable inside the cap:
# stop, no re-send. ~10 s confirm + 110 s wait = ~120 s from the send.
DROPPED_SEND_WAIT_S = 110.0
# Per-read timeout while waiting, so one slow RPC cannot stretch the wait far.
DROPPED_SEND_READ_TIMEOUT_S = 6
# One long wait per check (one process). After it, this process sends nothing
# new (the in-call Alchemy backup for that same candidate is the only send).
LONG_WAITS_PER_CHECK = 1
# Check-time gates (seconds since this module was imported, i.e. check start).
# Runner hard timeout is 480 s. Past LONG_WAIT_START_BY_S the old short poll is
# used (then stop). Past BACKUP_SEND_BY_S no backup send after a long wait.
LONG_WAIT_START_BY_S = 150.0
BACKUP_SEND_BY_S = 320.0
CHECK_CLOCK = time.monotonic
CHECK_T0 = CHECK_CLOCK()
_LONG_WAIT = {"used": 0}


def reset_check_clock(t0=None, clock=None):
    """Start of a check (tests call this). Clears the one-long-wait budget."""
    global CHECK_T0, CHECK_CLOCK
    if clock is not None:
        CHECK_CLOCK = clock
    CHECK_T0 = CHECK_CLOCK() if t0 is None else float(t0)
    _LONG_WAIT["used"] = 0


def check_elapsed_s():
    return CHECK_CLOCK() - CHECK_T0


def long_wait_used():
    """True after this check spent its dropped-send wait. No more new sends."""
    return _LONG_WAIT["used"] >= LONG_WAITS_PER_CHECK
ERROR_LOG_PATH = _DH.HOME_STR + "/rpc-send-errors.jsonl"
# Optional Alchemy fallback gate (edge_policies.AlchemyBudget). None = no cap
# beyond the built-in one Alchemy attempt per submit_bundle call. The edge
# check installs one per process: max 1 Alchemy fallback per directed route
# attempt and max 2 per edge check (locked 2026-10-05).
ALCHEMY_GATE = None
ERROR_CAP = 1500
_PT = ZoneInfo(_TZ_NAME)

_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
_KEY_RE = re.compile(r"(api-key|api_key|apikey)\s*[:=]\s*([^\s&\"']+)", re.I)


def send_endpoints():
    """Helius, then Alchemy. Never public mainnet-beta."""
    return SEND_ENDPOINTS


def provider_of(url):
    """Map a URL to helius, alchemy, or mainnet-beta. Unknown host is rpc."""
    host = urlsplit(str(url or "")).hostname or ""
    for known, name in zip(_RPC_URLS, ENDPOINT_ORDER):
        if urlsplit(known).hostname == host:
            return name
    return "rpc"


def _endpoint_url(name):
    for url, known in zip(_RPC_URLS, ENDPOINT_ORDER):
        if known == name:
            return url
    return None


def _secret_fragments():
    frags = []
    for url in _RPC_URLS:
        frags.append(url)
        parts = urlsplit(url)
        if parts.hostname:
            frags.append(parts.hostname)
        if parts.query:
            frags.append(parts.query)
            for bit in parts.query.split("&"):
                if "=" in bit:
                    _k, val = bit.split("=", 1)
                    if val:
                        frags.append(val)
    frags.sort(key=len, reverse=True)
    return frags


def redact(text):
    """Strip URLs and key material. Safe to store."""
    s = "" if text is None else str(text)
    for frag in _secret_fragments():
        if frag and frag in s:
            s = s.replace(frag, "rpc")
    s = _URL_RE.sub("rpc", s)
    s = _KEY_RE.sub(lambda m: m.group(1) + "=redacted", s)
    if "://" in s:
        return "rpc_failed"
    return s


def _fail_label(kind):
    return redact(kind or "rpc_failed")[:160]


def error_record(provider, method, data):
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        err = {"message": None if err is None else str(err)}
    logs = None
    payload = err.get("data")
    if isinstance(payload, dict):
        logs = payload.get("logs")
    name = provider if provider in ENDPOINT_ORDER else "rpc"
    rec = {
        "time_pt": datetime.now(_PT).replace(microsecond=0).isoformat(),
        "provider": name,
        "method": redact(method)[:80],
        "message": redact(err.get("message")),
        "code": err.get("code"),
        "logs": _redact_logs(logs),
    }
    return rec


def _redact_logs(logs):
    if logs is None:
        return None
    if isinstance(logs, list):
        return [redact(item)[:500] for item in logs[:40]]
    if isinstance(logs, dict):
        return {redact(k)[:80]: redact(v)[:500] for k, v in list(logs.items())[:40]}
    return redact(logs)[:800]


def error_line(provider, method, data):
    rec = error_record(provider, method, data)
    line = json.dumps(rec, default=str)
    if len(line) <= ERROR_CAP:
        return line
    rec["logs"] = redact(rec.get("logs"))[:800]
    rec["message"] = redact(rec.get("message"))[:400]
    line = json.dumps(rec, default=str)
    return line[:ERROR_CAP]


def persist_rpc_error(provider, method, data, path=None):
    """Append one redacted JSON line. Does not store the request payload."""
    line = error_line(provider, method, data)
    try:  # ultra1012: count slippage failures for auto_slip (24h fallback + SLIPPAGE_FAIL)
        if path is None:
            import auto_slip as _AS
            _AS.note_fail(line, venue=provider)
    except Exception:
        pass
    dest = path or ERROR_LOG_PATH
    try:
        with open(dest, "a", encoding="utf-8", newline="") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    return line


def same_signature_retry_allowed(last_valid_block_height, current_block_height):
    """The same signed bytes may be sent again only after that blockhash expired."""
    try:
        return int(current_block_height) > int(last_valid_block_height)
    except (TypeError, ValueError):
        return False


def is_already_processed(data):
    if not isinstance(data, dict):
        return False
    err = data.get("error")
    blob = json.dumps(err, default=str).lower() if err is not None else ""
    compact = blob.replace("_", "").replace(" ", "")
    if "alreadyprocessed" in compact or "alreadybeenprocessed" in compact:
        return True
    return False


def _interpret(data, err):
    """Classify one transport result. An error object is not missing_result."""
    if isinstance(data, dict) and "result" in data:
        return "ok", data
    if isinstance(data, dict) and "error" in data and "result" not in data:
        return "jsonrpc_error", data
    if err and err != "jsonrpc_error":
        return str(err), None
    return "missing_result", None


def _curl_post(url, payload, timeout):
    """One POST. Returns (data, None) or (data_or_None, failure_name). Never raises the URL."""
    try:
        r = subprocess.run(
            [
                "curl", "-sS", "-w", "\n%{http_code}", "--max-time", str(timeout),
                "-X", "POST", "-H", "Content-Type: application/json",
                "--data-binary", "@-", url,
            ],
            input=payload, capture_output=True, text=True,
        )
    except Exception:
        return None, "rpc_failed"
    if r.returncode != 0:
        # 28 is curl's timeout. Do not keep stderr; it can echo the URL.
        return None, "timeout" if r.returncode == 28 else "rpc_failed"
    body, _, code = r.stdout.rpartition("\n")
    code = (code or "").strip()
    if code in ("413", "429"):
        return None, "http_" + code
    if (not code.isdigit()) or int(code) < 200 or int(code) >= 300:
        return None, "http_error" if code else "rpc_failed"
    if not body.strip():
        return None, "empty_body"
    try:
        data = json.loads(body)
    except Exception:
        return None, "rpc_failed"
    kind, parsed = _interpret(data, None)
    if kind == "ok":
        return parsed, None
    if kind == "jsonrpc_error":
        return parsed, "jsonrpc_error"
    return None, "missing_result"


def rpc(method, params, retries=1, timeout=30, transport=None, error_log_path=None):
    """Read call. Fall through helius, then alchemy, then public mainnet-beta.

    A later endpoint is used only after the earlier one fails. A JSON-RPC
    error object is logged before that failover. It is not labeled
    missing_result. retries is how many full passes to make. Raises
    RuntimeError('rpc_failed:...') if every endpoint fails.
    """
    post = transport or _curl_post
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    attempts = max(1, int(retries or 1))
    last = "rpc_failed"
    for attempt in range(attempts):
        for name in ENDPOINT_ORDER:
            url = _endpoint_url(name)
            try:
                data, err = post(url, payload, timeout)
            except Exception:
                data, err = None, "rpc_failed"
            kind, parsed = _interpret(data, err)
            if kind == "ok":
                return parsed
            if kind == "jsonrpc_error":
                # Full body first, then the next provider.
                last = name + ":" + persist_rpc_error(name, method, parsed, error_log_path)
                continue
            last = name + ":" + _fail_label(kind or err or "rpc_failed")
        if attempt + 1 < attempts:
            time.sleep(min(1.5 * (attempt + 1), 4.0))
    raise RuntimeError("rpc_failed:" + str(method) + ":" + redact(last)[:ERROR_CAP])


PAPER_SKIP_MSG = "PAPER_TRADING: send skipped"


def is_paper_skip(x) -> bool:
    """True for a send that paper mode skipped on purpose (a would-be fill, not a failure)."""
    try:
        return PAPER_SKIP_MSG in (x if isinstance(x, str) else json.dumps(x, default=str))
    except Exception:
        return False


def rpc_send(method, params, endpoint_name, timeout=30, transport=None, error_log_path=None):
    """One provider. No fallthrough. Refuses public mainnet-beta.

    A 2xx JSON-RPC error is logged and returned (the body), not relabeled
    missing_result. Transport failures raise RuntimeError with a redacted label.
    """
    if method == "sendTransaction":
        try:
            import degen_home as _DHp
            if _DHp.trading_paused():
                return {"jsonrpc": "2.0", "id": 1, "error": {"code": -3, "message": "PAUSED: trading paused by owner; send skipped"}}
            if not _DHp.run_side_ok():
                return {"jsonrpc": "2.0", "id": 1, "error": {"code": -2, "message": "RUN_SIDE: this side is not the active trading side; send skipped"}}
            if _DHp.paper_trading() and os.environ.get("DEGEN_SEED_TEST_TRADE") != "approved":
                # Template paper mode: never broadcast. Callers see a JSON-RPC style error.
                return {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": PAPER_SKIP_MSG}}
        except ImportError:
            raise RuntimeError("rpc_failed:paper_guard_unavailable")
    if endpoint_name == "mainnet-beta":
        raise RuntimeError("rpc_failed:send_refuses_mainnet-beta")
    if endpoint_name not in ENDPOINT_ORDER:
        raise RuntimeError("rpc_failed:unknown_endpoint")
    url = _endpoint_url(endpoint_name)
    post = transport or _curl_post
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    try:
        data, err = post(url, payload, timeout)
    except Exception:
        data, err = None, "rpc_failed"
    kind, parsed = _interpret(data, err)
    if kind == "ok":
        return parsed
    if kind == "jsonrpc_error":
        persist_rpc_error(endpoint_name, method, parsed, error_log_path)
        return parsed
    label = _fail_label(kind or err or "rpc_failed")
    raise RuntimeError("rpc_failed:" + str(method) + ":" + endpoint_name + ":" + label)


def send_config():
    return {
        "encoding": "base64",
        "skipPreflight": False,
        "preflightCommitment": SEND_COMMITMENT,
        "maxRetries": SEND_MAX_RETRIES,
    }


def latest_blockhash(endpoint_name, timeout=30, transport=None, error_log_path=None):
    """Confirmed blockhash from this endpoint only. Same endpoint must send."""
    data = rpc_send(
        "getLatestBlockhash",
        [{"commitment": SEND_COMMITMENT}],
        endpoint_name,
        timeout=timeout,
        transport=transport,
        error_log_path=error_log_path,
    )
    if not isinstance(data, dict) or "result" not in data:
        line = error_line(endpoint_name, "getLatestBlockhash", data if isinstance(data, dict) else {})
        raise RuntimeError(line)
    val = (data.get("result") or {}).get("value") or {}
    blockhash = val.get("blockhash")
    if not blockhash:
        raise RuntimeError("rpc_failed:getLatestBlockhash:no_blockhash")
    return str(blockhash), val.get("lastValidBlockHeight")


def signature_from_signed_b64(signed_b64):
    """First signature, if these are real signed transaction bytes."""
    try:
        import base64
        from solders.transaction import VersionedTransaction
        raw = base64.b64decode(signed_b64)
        vtx = VersionedTransaction.from_bytes(raw)
        sig = vtx.signatures[0]
        text = str(sig)
        if text and not text.startswith("1111111111"):
            return text
    except Exception:
        return None
    return None


def _status_once(sig, timeout, transport, error_log_path):
    try:
        st = rpc(
            "getSignatureStatuses",
            [[sig], {"searchTransactionHistory": False}],
            retries=1,
            timeout=timeout,
            transport=transport,
            error_log_path=error_log_path,
        )
    except RuntimeError:
        return None
    if not isinstance(st, dict):
        return None
    val = ((st.get("result") or {}).get("value") or [None])[0]
    if not isinstance(val, dict):
        return None
    if val.get("err"):
        return "err", val.get("err")
    if val.get("confirmationStatus") in ("confirmed", "finalized"):
        return "ok", None
    return None


def confirm_signature(
    sig,
    tries=None,
    sleep_s=1.0,
    timeout=30,
    transport=None,
    error_log_path=None,
    budget_s=None,
    sleep_fn=None,
    clock=None,
):
    """Read path. ok, err, or unconfirmed. Does not send.

    budget_s is the total wait measured by clock (monotonic by default),
    not a block height. sleep_fn defaults to time.sleep so tests can
    advance a fake clock. When budget_s and tries are both omitted, an
    accepted signature is given HELIUS_ACCEPTED_CONFIRM_S.
    """
    pause = sleep_fn or time.sleep
    now = clock or time.monotonic
    if budget_s is None and tries is None:
        budget_s = HELIUS_ACCEPTED_CONFIRM_S
    if budget_s is not None:
        start = now()
        limit = float(budget_s)
        while True:
            hit = _status_once(sig, timeout, transport, error_log_path)
            if hit is not None:
                return hit
            elapsed = now() - start
            if elapsed >= limit:
                return "unconfirmed", None
            remaining = limit - elapsed
            step = 1.0 if not sleep_s else float(sleep_s)
            pause(min(step, remaining))
        return "unconfirmed", None
    for _ in range(max(0, int(tries))):
        hit = _status_once(sig, timeout, transport, error_log_path)
        if hit is not None:
            return hit
        if sleep_s:
            pause(sleep_s)
    return "unconfirmed", None


def send_with_fresh_blockhash(
    sign_fn,
    endpoints,
    timeout=30,
    transport=None,
    error_log_path=None,
    confirm_tries=12,
    sleep_s=1.0,
    seen_signed=None,
    accept_unconfirmed=False,
    confirm_budget_s=None,
    sleep_fn=None,
    clock=None,
    sent_log=None,
):
    """Fetch a confirmed blockhash and send on that same one endpoint.

    Only the first allowed name is used. Public mainnet-beta is skipped.
    This does not fail over to another host: the same signed bytes must
    not be submitted elsewhere. sign_fn(endpoint_name, blockhash,
    last_valid_block_height) returns signed transaction bytes (base64).

    Returns (signature_or_None, error_or_None, kind) where kind is
    ok, already_processed, preflight, or uncertain.
    """
    names = []
    for name in endpoints or ():
        if name == "mainnet-beta":
            continue
        if name not in ENDPOINT_ORDER:
            continue
        names.append(name)
    if not names:
        return None, "no_send_endpoint", "preflight"
    name = names[0]
    seen = seen_signed if seen_signed is not None else []
    try:
        blockhash, height = latest_blockhash(
            name, timeout=timeout, transport=transport, error_log_path=error_log_path,
        )
    except RuntimeError as exc:
        return None, redact(str(exc))[:ERROR_CAP], "preflight"
    try:
        signed = sign_fn(name, blockhash, height)
    except Exception as exc:
        return None, redact(str(exc))[:ERROR_CAP], "preflight"
    if signed in seen:
        return None, "refusing_same_signed_bytes", "preflight"
    seen.append(signed)
    # Every broadcast attempt is logged (signature, blockhash expiry height)
    # so a later re-send for this candidate can first check it did not land.
    entry = {
        "sig": signature_from_signed_b64(signed),
        "last_valid": height,
        "host": name,
        "state": "pending",
    }
    if sent_log is not None:
        sent_log.append(entry)
    try:
        resp = rpc_send(
            "sendTransaction",
            [signed, send_config()],
            name,
            timeout=timeout,
            transport=transport,
            error_log_path=error_log_path,
        )
    except RuntimeError as exc:
        # The node may have accepted it. Do not submit these bytes elsewhere.
        entry["state"] = "uncertain"
        return None, redact(str(exc))[:ERROR_CAP], "uncertain"
    if is_already_processed(resp):
        sig = signature_from_signed_b64(signed)
        if sig:
            entry["state"] = "ok"
            entry["sig"] = sig
            return sig, None, "already_processed"
        entry["state"] = "uncertain"
        return None, error_line(name, "sendTransaction", resp), "already_processed"
    if isinstance(resp, dict) and "error" in resp and "result" not in resp:
        # Preflight / JSON-RPC rejection: the node did not forward it.
        entry["state"] = "rejected"
        return None, error_line(name, "sendTransaction", resp), "preflight"
    sig = resp.get("result") if isinstance(resp, dict) else None
    if not sig:
        # 2xx with neither result nor error: may have been accepted.
        entry["state"] = "uncertain"
        return None, name + ":missing_result", "preflight"
    entry["sig"] = str(sig)
    if confirm_budget_s is not None:
        status, detail = confirm_signature(
            sig,
            budget_s=confirm_budget_s,
            sleep_s=sleep_s,
            timeout=timeout,
            transport=transport,
            error_log_path=error_log_path,
            sleep_fn=sleep_fn,
            clock=clock,
        )
    else:
        status, detail = confirm_signature(
            sig,
            tries=confirm_tries,
            budget_s=None,
            sleep_s=sleep_s,
            timeout=timeout,
            transport=transport,
            error_log_path=error_log_path,
            sleep_fn=sleep_fn,
            clock=clock,
        )
    if status == "ok":
        entry["state"] = "ok"
        return sig, None, "ok"
    if status == "err":
        # Landed with an error. Do not resubmit these bytes. Caller may re-quote.
        entry["state"] = "chain_err"
        err = redact(f"chain_err:{sig}:{detail}")[:ERROR_CAP]
        return None, err, "preflight"
    entry["state"] = "uncertain"
    if accept_unconfirmed:
        return sig, None, "ok"
    return None, f"unconfirmed:{sig}", "uncertain"


def _statuses_with_history(sigs, timeout, transport, error_log_path):
    """getSignatureStatuses(searchTransactionHistory=true). None on read failure."""
    try:
        st = rpc(
            "getSignatureStatuses",
            [list(sigs), {"searchTransactionHistory": True}],
            retries=1,
            timeout=timeout,
            transport=transport,
            error_log_path=error_log_path,
        )
    except RuntimeError:
        return None
    if not isinstance(st, dict):
        return None
    vals = (st.get("result") or {}).get("value")
    if not isinstance(vals, list) or len(vals) != len(sigs):
        return None
    return vals


def _block_height(timeout, transport, error_log_path):
    try:
        st = rpc(
            "getBlockHeight",
            [{"commitment": SEND_COMMITMENT}],
            retries=1,
            timeout=timeout,
            transport=transport,
            error_log_path=error_log_path,
        )
    except RuntimeError:
        return None
    try:
        return int(st.get("result"))
    except (AttributeError, TypeError, ValueError):
        return None


def _host_block_height(host, timeout, transport, error_log_path):
    """getBlockHeight on the one host that issued the blockhash. None on failure."""
    if host not in SEND_ENDPOINTS:
        return None
    try:
        st = rpc_send(
            "getBlockHeight",
            [{"commitment": SEND_COMMITMENT}],
            host,
            timeout=timeout,
            transport=transport,
            error_log_path=error_log_path,
        )
    except RuntimeError:
        return None
    try:
        return int(st.get("result"))
    except (AttributeError, TypeError, ValueError):
        return None


def resolve_prior_sends(
    sent_log,
    budget_s=None,
    step_s=None,
    timeout=30,
    transport=None,
    error_log_path=None,
    sleep_fn=None,
    clock=None,
):
    """Read path. Decide whether earlier broadcasts for this candidate landed.

    Returns (verdict, entry): "landed" with the landed entry (that is the
    fill), "clear" when every broadcast is known not to have landed (rejected
    by preflight, landed with an error, or unseen after its blockhash
    expired), or "uncertain" when one is still unknown before its blockhash
    expired at the end of the poll budget. Never sends.
    Expiry proof (2026-10-08): block height read on the entry's own host
    (the one that issued its blockhash) is past its lastValidBlockHeight, and
    a fresh full-history status read after that height read still finds
    nothing. A read failure or a merely "processed" status is not proof.
    """
    entries = [e for e in (sent_log or []) if isinstance(e, dict) and e.get("state") != "rejected"]
    landed_before = next((e for e in entries if e.get("state") == "ok"), None)
    if not any(e.get("state") not in _CLOSED_SEND_STATES for e in entries):
        return ("landed", landed_before) if landed_before else ("clear", None)
    pause = sleep_fn or time.sleep
    now = clock or time.monotonic
    limit = PRIOR_SEND_POLL_S if budget_s is None else float(budget_s)
    step = PRIOR_SEND_POLL_STEP_S if step_s is None else float(step_s)
    start = now()

    def _apply(known, vals):
        """Close entries from a status read. Returns the landed entry, if any."""
        for e, v in zip(known, vals or []):
            if not isinstance(v, dict):
                continue
            if v.get("err"):
                e["state"] = "chain_err"
            elif v.get("confirmationStatus") in ("confirmed", "finalized"):
                e["state"] = "ok"
                return e
        return None

    while True:
        open_ = [e for e in entries if e.get("state") not in _CLOSED_SEND_STATES]
        # Height first, per issuing host, so "not found" afterwards is final.
        heights = {}
        for host in {e.get("host") for e in open_}:
            heights[host] = _host_block_height(host, timeout, transport, error_log_path)
        known = [e for e in open_ if e.get("sig")]
        vals = None
        if known:
            vals = _statuses_with_history([e["sig"] for e in known], timeout, transport, error_log_path)
            hit = _apply(known, vals)
            if hit is not None:
                return "landed", hit
        open_ = [e for e in entries if e.get("state") not in _CLOSED_SEND_STATES]
        if not open_:
            return ("landed", landed_before) if landed_before else ("clear", None)
        if vals is not None and all(
            e.get("sig") and same_signature_retry_allowed(e.get("last_valid"), heights.get(e.get("host")))
            for e in open_
        ):
            # Final full-history read after the expiry height was seen.
            final = _statuses_with_history([e["sig"] for e in open_], timeout, transport, error_log_path)
            if final is not None:
                hit = _apply(open_, final)
                if hit is not None:
                    return "landed", hit
                pairs = [(e, v) for e, v in zip(open_, final) if e.get("state") not in _CLOSED_SEND_STATES]
                # Expired only if still unseen; a "processed" status keeps waiting.
                if all(v is None for _e, v in pairs):
                    for e, _v in pairs:
                        e["state"] = "expired"
                    return ("landed", landed_before) if landed_before else ("clear", None)
        elapsed = now() - start
        if elapsed >= limit:
            return "uncertain", None
        pause(max(0.0, min(step, limit - elapsed)))


def _tx_list(value):
    if value is None:
        return None
    if isinstance(value, tuple):
        value = value[0] if value else None
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    try:
        items = list(value)
    except TypeError:
        return None
    return items or None


def submit_bundle(
    unsigned_txs,
    sign_fn,
    requote_fn=None,
    timeout=30,
    transport=None,
    error_log_path=None,
    confirm_tries=12,
    sleep_s=1.0,
    accept_unconfirmed=False,
    confirm_budget_s=HELIUS_ACCEPTED_CONFIRM_S,
    sleep_fn=None,
    clock=None,
    prior_sends=None,
):
    """Helius first. One Alchemy send only after requote_fn returns a new tx.

    An accepted Helius signature (a send result) waits confirm_budget_s
    seconds, measured by clock. A preflight rejection or a send with no
    signature does not wait. An accepted but unconfirmed send then gets the
    dropped-send wait below before any requote.

    sign_fn(unsigned_b64, blockhash) -> signed_b64. The blockhash was just
    fetched from the host about to send. A Helius failure (preflight,
    JSON-RPC error, missing result, HTTP, timeout, or no confirmation)
    does not resubmit those signed bytes. requote_fn() must apply the
    trade rules and return a new unsigned list, or None to skip Alchemy.
    Public mainnet-beta is never used. One Alchemy attempt, then stop.
    An unconfirmed send sets uncertain so the caller does not start
    another pair.

    Duplicate-send guard: every broadcast is logged in prior_sends (pass
    one list per candidate so a later fallback for the same candidate in the
    check shares it). Before the Alchemy re-quote, and before a first send
    when prior_sends already holds broadcasts, resolve_prior_sends polls
    them. A prior send that landed is returned as the fill and nothing else
    is sent. One still unknown before its blockhash expired returns
    uncertain with no re-quote and no send.

    Dropped-send wait (2026-10-08): an unconfirmed Helius send is polled for
    up to DROPPED_SEND_WAIT_S until it lands (the fill) or is provably expired
    (see resolve_prior_sends); only then the Alchemy fresh-quote send. One
    such wait per check; afterwards submit_bundle refuses new sends.

    Returns (signature_or_None, error_or_None, uncertain_bool).
    """
    current = _tx_list(unsigned_txs)
    if not current:
        return None, "no_tx", False
    if long_wait_used():
        # This check already spent its dropped-send wait: no new sends.
        print("LONG_WAIT_STOP no new sends this check", flush=True)
        return None, "long_wait_used_no_new_sends", False
    seen = []
    sent = prior_sends if prior_sends is not None else []

    def _resolve():
        return resolve_prior_sends(
            sent, timeout=timeout, transport=transport,
            error_log_path=error_log_path, sleep_fn=sleep_fn, clock=clock,
        )

    def _landed_is_final(entry, tx_list):
        # The swap is the last tx of its list (setup txs come first).
        return entry.get("idx") is None or entry.get("idx") == entry.get("n", 1) - 1

    if any(e.get("state") not in ("rejected",) for e in sent):
        verdict, hit = _resolve()
        print(f"PRIOR_SEND_CHECK before_first_send verdict={verdict} n={len(sent)}", flush=True)
        if verdict != "clear":
            # A fallback for this candidate must not send while an earlier
            # broadcast landed or may still land. The caller stops (uncertain).
            return None, "prior_send_" + verdict, True

    def _send_list(tx_list, host):
        sig_out = None
        err_out = None
        kind_out = "ok"
        landed_here = 0
        for raw in tx_list:
            def _sign(endpoint, blockhash, height, raw=raw):
                return sign_fn(raw, blockhash)

            _mark = len(sent)
            sig_out, err_out, kind_out = send_with_fresh_blockhash(
                _sign,
                (host,),
                timeout=timeout,
                transport=transport,
                error_log_path=error_log_path,
                confirm_tries=confirm_tries,
                sleep_s=sleep_s,
                seen_signed=seen,
                accept_unconfirmed=False,
                confirm_budget_s=confirm_budget_s if host == "helius" else None,
                sleep_fn=sleep_fn,
                clock=clock,
                sent_log=sent,
            )
            for _e in sent[_mark:]:
                _e["idx"] = tx_list.index(raw)
                _e["n"] = len(tx_list)
            if kind_out in ("ok", "already_processed") and sig_out:
                landed_here += 1
                continue
            return sig_out, err_out, kind_out, landed_here
        return sig_out, err_out, kind_out, landed_here

    sig, err, kind, landed = _send_list(current, "helius")
    if kind in ("ok", "already_processed") and sig and landed == len(current):
        return sig, None, False
    waited_long = False
    helius_uncertain = kind == "uncertain" or (landed > 0 and kind != "ok")
    if any(e.get("state") not in _CLOSED_SEND_STATES for e in sent):
        # Accepted but unconfirmed (or transport-uncertain): check history
        # before deciding anything else. 2026-10-08 05:58 PT double fill fix.
        elapsed_check = check_elapsed_s()
        if (not long_wait_used()) and elapsed_check <= LONG_WAIT_START_BY_S:
            # Dropped-send wait: until it lands or is provably expired (cap).
            _LONG_WAIT["used"] += 1
            _now = clock or time.monotonic
            _w0 = _now()
            verdict, hit = resolve_prior_sends(
                sent, budget_s=DROPPED_SEND_WAIT_S,
                timeout=min(int(timeout), int(DROPPED_SEND_READ_TIMEOUT_S)),
                transport=transport, error_log_path=error_log_path,
                sleep_fn=sleep_fn, clock=clock,
            )
            waited_long = True
            print(
                f"PRIOR_SEND_CHECK after_helius_wait verdict={verdict} n={len(sent)} "
                f"waited_s={_now() - _w0:.1f} cap_s={DROPPED_SEND_WAIT_S:g}",
                flush=True,
            )
        else:
            verdict, hit = _resolve()
            print(
                f"PRIOR_SEND_CHECK after_helius verdict={verdict} n={len(sent)} "
                f"short_poll(long_wait_used={long_wait_used()} check_s={elapsed_check:.0f})",
                flush=True,
            )
        if verdict == "landed":
            if _landed_is_final(hit, current) and landed + 1 >= len(current):
                return hit.get("sig"), None, False
            return None, err or "prior_send_landed_partial", True
        if verdict == "uncertain":
            return None, err or "prior_send_uncertain", True
        # clear: every Helius broadcast is known not to have landed.
        helius_uncertain = landed > 0 and kind != "ok"
        if waited_long and requote_fn is not None and not landed and check_elapsed_s() > BACKUP_SEND_BY_S:
            # Proven expired, but too late in the check for a backup send.
            print(f"BACKUP_SEND_SKIP too_late check_s={check_elapsed_s():.0f}", flush=True)
            return None, "backup_send_too_late", False
    if kind == "already_processed" and not sig:
        return None, err or "already_processed", True
    # Anything already accepted on Helius is not rebuilt onto Alchemy.
    if landed:
        return None, err or "send_failed", True
    if requote_fn is None or not HAS_ALCHEMY:  # template: no Alchemy configured -> no fallback send
        return None, err or "send_failed", helius_uncertain
    gate = ALCHEMY_GATE
    if gate is not None and not gate.allow():
        # Capped: no Alchemy re-quote and no Alchemy send. Keep the Helius error.
        return None, err or "alchemy_capped", helius_uncertain
    try:
        rebuilt = requote_fn()
    except Exception as exc:
        return None, redact(str(exc))[:ERROR_CAP], helius_uncertain
    rebuilt = _tx_list(rebuilt)
    if not rebuilt:
        return None, err or "requote_failed", helius_uncertain
    if gate is not None:
        gate.used()
    sig2, err2, kind2, landed2 = _send_list(rebuilt, "alchemy")
    if kind2 in ("ok", "already_processed") and sig2 and landed2 == len(rebuilt):
        return sig2, None, False
    if any(e.get("state") not in _CLOSED_SEND_STATES for e in sent):
        verdict, hit = _resolve()
        print(f"PRIOR_SEND_CHECK after_alchemy verdict={verdict} n={len(sent)}", flush=True)
        if verdict == "landed" and hit.get("host") == "alchemy" and _landed_is_final(hit, rebuilt) \
                and landed2 + 1 >= len(rebuilt):
            return hit.get("sig"), None, False
        if verdict != "clear":
            return None, err2 or err or "send_failed", True
    if kind2 == "uncertain" or helius_uncertain or landed2:
        return None, err2 or err or "send_failed", True
    return None, err2 or err or "send_failed", False
