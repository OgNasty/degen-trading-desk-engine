#!/usr/bin/env python3
"""Keyless public data sources with graceful rate-limit handling (template v1.0.0).

get_json(url): short backoff on HTTP 429 / 5xx / timeouts (honours Retry-After up to 4 s), no retry on
other 4xx. first_ok(): try sources in order, cache the last good value in keyless-cache.json, and when
every source fails return the cached value only while it is younger than max_age_sec; otherwise raise
AllSourcesFailed so the caller skips that gate (never trades on stale data). One concise warning per
key per process (stderr + keyless-warnings.log, last 200 lines). Stdlib only; never logs bodies or URLs
with query strings."""
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

import degen_home as _DH

CACHE = _DH.HOME / "keyless-cache.json"
WARN_LOG = _DH.HOME / "keyless-warnings.log"
UA = "degen-template/1.2"
_sleep = time.sleep  # tests replace this
_urlopen = urllib.request.urlopen  # tests replace this
_warned = set()


class SourceError(Exception):
    def __init__(self, kind, retryable=False):
        super().__init__(kind)
        self.kind, self.retryable = kind, retryable


class AllSourcesFailed(Exception):
    pass


def get_json(url, timeout=6.0, tries=2, headers=None):
    last = None
    for i in range(max(1, tries)):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})})
            with _urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            code = int(getattr(e, "code", 0) or 0)
            retry = code == 429 or code >= 500
            last = SourceError(f"http_{code}", retry)
            wait = 1.0 * (i + 1)
            try:
                wait = min(4.0, float(e.headers.get("Retry-After") or wait))
            except Exception:
                pass
        except (socket.timeout, TimeoutError) as e:
            last, wait = SourceError("timeout", True), 1.0 * (i + 1)
        except urllib.error.URLError as e:
            r = getattr(e, "reason", None)
            kind = "timeout" if isinstance(r, (socket.timeout, TimeoutError)) else "network"
            last, wait = SourceError(kind, True), 1.0 * (i + 1)
        except ValueError:
            last, wait = SourceError("bad_json", False), 0
        if not last.retryable or i == tries - 1:
            break
        _sleep(wait)
    raise last


def _load():
    try:
        d = json.loads(CACHE.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _store(key, value, source):
    d = _load()
    d[key] = {"value": value, "at": time.time(), "source": source}
    try:
        tmp = CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(CACHE)
    except Exception:
        pass


def cached(key, max_age_sec):
    e = _load().get(key)
    if not e:
        return None
    age = time.time() - float(e.get("at", 0))
    return (e["value"], age, e.get("source")) if age <= max_age_sec else None


def warn(key, msg):
    if key in _warned:
        return
    _warned.add(key)
    line = f"{datetime.now().astimezone().isoformat(timespec='seconds')} KEYLESS_WARN {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        lines = WARN_LOG.read_text().splitlines()[-199:] if WARN_LOG.exists() else []
        WARN_LOG.write_text("\n".join(lines + [line]) + "\n")
    except Exception:
        pass


def first_ok(key, sources, max_age_sec):
    """sources: [(name, zero-arg fn returning a value)]. Returns (value, meta). Raises AllSourcesFailed."""
    errs = []
    for name, fn in sources:
        try:
            v = fn()
            _store(key, v, name)
            return v, {"source": name, "cached": False, **({"rotated_after": errs} if errs else {})}
        except Exception as e:
            errs.append(f"{name}:{getattr(e, 'kind', type(e).__name__)}")
    c = cached(key, max_age_sec)
    if c is not None:
        v, age, src = c
        warn(key, f"{key}: all sources failed ({', '.join(errs)}); using cached {src} value {age / 3600:.1f}h old")
        return v, {"source": src, "cached": True, "stale": True, "age_sec": int(age), "errors": errs}
    warn(key, f"{key}: all sources failed ({', '.join(errs)}); no fresh cache, gate skipped")
    raise AllSourcesFailed(";".join(errs))
