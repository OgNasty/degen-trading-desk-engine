"""jupkey1012: Jupiter API key (secrets.json JUPITER_API_KEY) for api.jup.ag calls, with keyless fallback.

The key is never printed or put on a command line: curl callers pass headers via stdin (-H @-).
On 401/403 with the key, JUP_KEY_FAIL is logged once, the key is dropped for the rest of this process,
and the same request is retried keyless."""
import os

_state = {"key": None, "loaded": False, "failed": False}
UA = "degen-trading-desk/1.0"


def key():
    if _state["failed"]:
        return None
    if not _state["loaded"]:
        _state["loaded"] = True
        try:
            import degen_home as _DH
            k = _DH.get_secret("JUPITER_API_KEY")
        except Exception:
            k = os.environ.get("JUPITER_API_KEY")
        _state["key"] = (k or "").strip() or None
    return _state["key"]


def is_jup(url):
    return "://api.jup.ag/" in str(url)


def headers(url):
    k = key() if is_jup(url) else None
    return {"x-api-key": k} if k else {}


def fail(code, url=""):
    """Call when a keyed request returned 401/403. Returns True if a keyless retry should follow."""
    if _state["failed"] or not _state["key"]:
        return False
    _state["failed"] = True
    path = str(url).split("?")[0].replace("https://api.jup.ag", "")
    print(f"JUP_KEY_FAIL http={code} path={path} -> keyless for the rest of this run", flush=True)
    try:  # quiet SKIPs are compacted in the runner log, so keep a dedicated record
        import degen_home as _DH, datetime
        d = os.path.join(_DH.HOME_STR, "runner-logs"); os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "jup-key-fail.log"), "a", encoding="utf-8", newline="") as f:
            f.write(f"{datetime.datetime.now().astimezone().isoformat(timespec='seconds')} JUP_KEY_FAIL http={code} path={path}\n")
    except Exception:
        pass
    return True


def curl_header_stdin(url):
    """(extra curl args, stdin text) that add the key header without exposing it on the command line."""
    h = headers(url)
    if not h:
        return [], None
    return ["-H", "@-"], "".join(f"{k}: {v}\n" for k, v in h.items())


class curl_args:
    """with jupkey.curl_args(url) as extra: subprocess.run(["curl", ..., *extra, url])
    Writes the header to a private temp file for curl's -H @file (never on the command line); removed on exit."""
    def __init__(self, url):
        self.url, self.path = url, None

    def __enter__(self):
        h = headers(self.url)
        if not h:
            return []
        import tempfile
        fd, self.path = tempfile.mkstemp(prefix=".jh-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("".join(f"{k}: {v}\n" for k, v in h.items()))
        return ["-H", "@" + self.path]

    def __exit__(self, *a):
        if self.path:
            try:
                os.remove(self.path)
            except Exception:
                pass
        return False


def urlopen_json(url, timeout, extra_headers=None):
    """GET JSON via urllib with the key header for api.jup.ag; keyless retry on 401/403."""
    import json, urllib.request, urllib.error
    for attempt in (0, 1):
        h = {"User-Agent": UA, "Accept": "application/json"}  # jupua1014: CF error 1010 blocks Python-urllib
        h.update(extra_headers or {}); h.update(headers(url))
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if attempt == 0 and e.code in (401, 403) and "x-api-key" in h and fail(e.code, url):
                continue
            raise
