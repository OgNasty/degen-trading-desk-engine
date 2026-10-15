"""Portable base dir, secrets, venv and lock helpers (box + Windows PC). 2026-10-10 pcmove.

DEGEN_HOME env var wins; default is this file's own folder. Paths use forward
slashes (fine on Windows). Secrets come from <HOME>/secrets.json (flat dict of
NAME -> value; SOLANA_RPC_URLS is a list), plus <HOME>/.env and <HOME>/config.json (template).
Never prints secret values.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HOME = Path(os.environ.get("DEGEN_HOME") or Path(__file__).resolve().parent)
HOME_STR = str(HOME).replace("\\", "/")
IS_WIN = os.name == "nt"
VENV_DIR = HOME / ".venv"
VENV_PY = VENV_DIR / ("Scripts/python.exe" if IS_WIN else "bin/python")
SECRETS_FILE = HOME / "secrets.json"
MIRROR_FLAG = HOME / "MIRROR_ONLY"
_BOX_SECRETS = ()  # template: secrets come from .env / secrets.json only
ENV_FILE = HOME / ".env"
CONFIG_FILE = HOME / "config.json"  # copy config.example.json -> config.json


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from <HOME>/.env into os.environ (.env wins over inherited values)."""
    try:
        for ln in ENV_FILE.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v:  # template: the bot's own .env wins over inherited environment
                os.environ[k] = v
            elif k:  # blank in .env = deliberately unset; drop any inherited (e.g. platform-injected) value
                os.environ.pop(k, None)
    except FileNotFoundError:
        pass


_load_dotenv()


def config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def tz_name() -> str:
    """Owner's time zone (IANA name): DEGEN_TZ, else config.json "timezone", else this machine's zone, else UTC."""
    from zoneinfo import ZoneInfo
    cands = [os.environ.get("DEGEN_TZ"), config().get("timezone"), os.environ.get("TZ")]
    try:
        cands.append(Path("/etc/timezone").read_text(encoding="utf-8").strip())
    except Exception:
        pass
    try:
        lt = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in lt:
            cands.append(lt.split("zoneinfo/", 1)[1])
    except Exception:
        pass
    for c in cands:
        c = str(c or "").strip()
        if not c or c.upper().startswith("YOUR"):
            continue
        try:
            ZoneInfo(c)
            return c
        except Exception:
            continue
    return "UTC"


TZ_NAME = tz_name()


def paper_trading() -> bool:
    """True unless PAPER_TRADING is explicitly false. Paper mode never sends a transaction."""
    v = os.environ.get("PAPER_TRADING")
    if v is None:
        v = str(config().get("paper_trading", True))
    return str(v).strip().lower() not in ("0", "false", "no", "off")


def wallet_address() -> str:
    """Public trading wallet address from .env WALLET_ADDRESS or config.json wallet.address."""
    return (os.environ.get("WALLET_ADDRESS") or (config().get("wallet") or {}).get("address") or "").strip()
_cache: dict | None = None


# --- dpapi1014: wallet key is DPAPI-encrypted (CurrentUser) on Windows. ---
KEY_NAME = "SOLANA_TRADING_PRIVATE_KEY"
DPAPI_KEY_FILE = HOME / "wallet-key.dpapi"
TEMPLATE_OPT_IN = True  # template: no file -> plaintext/.env still allowed


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    inp = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    c32 = ctypes.windll.crypt32
    fn = c32.CryptProtectData if protect else c32.CryptUnprotectData
    ok = fn(ctypes.byref(inp), None, None, None, None, 0x1, ctypes.byref(out))  # UI_FORBIDDEN
    if not ok:
        raise OSError("dpapi_failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def dpapi_encrypt_to_file(secret: str, path: Path = None) -> None:
    (path or DPAPI_KEY_FILE).write_bytes(_dpapi(secret.encode("utf-8"), True))


DPAPI_SECRETS_FILE = HOME / "secrets.dpapi"
# Names that live ONLY in secrets.dpapi on Windows (JSON dict, DPAPI CurrentUser).
DPAPI_NAMES = (
    "CLOUDFLARE_API_TOKEN",
    "JUPITER_API_KEY",
    "TRADING_ALERT_WEBHOOK_KEY",
    "TRADING_ALERT_WEBHOOK_URL",
    "SOLANA_RPC_URLS",
)


def dpapi_encrypt_secrets_file(values: dict, path: Path = None) -> None:
    (path or DPAPI_SECRETS_FILE).write_bytes(_dpapi(json.dumps(values).encode("utf-8"), True))


def _load_dpapi_key(merged: dict) -> None:
    """Windows: the wallet key and DPAPI_NAMES come from DPAPI files. Decrypted values
    replace any plaintext copy in merged AND os.environ (signer/children read env).
    Undecryptable file -> name removed everywhere (fail closed)."""
    sources = ((DPAPI_KEY_FILE, (KEY_NAME,)), (DPAPI_SECRETS_FILE, DPAPI_NAMES))
    for path, names in sources:
        if not path.is_file():
            if path is DPAPI_KEY_FILE and not TEMPLATE_OPT_IN:
                merged.pop(KEY_NAME, None)
                os.environ.pop(KEY_NAME, None)
            continue
        try:
            raw = _dpapi(path.read_bytes(), False).decode("utf-8-sig").strip()
            vals = {KEY_NAME: raw} if path is DPAPI_KEY_FILE else json.loads(raw)
        except Exception:
            vals = {}
        for n in names:
            v = vals.get(n)
            if v:
                merged[n] = v.strip() if isinstance(v, str) else v
                if isinstance(merged[n], str):
                    os.environ[n] = merged[n]
            else:
                merged.pop(n, None)
                os.environ.pop(n, None)


def secrets() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    merged: dict = {}
    for p in _BOX_SECRETS:
        try:
            card = json.loads(p.read_text(encoding="utf-8")).get("card") or {}
        except Exception:
            continue
        if isinstance(card, dict):
            merged.update(card)
            break
    try:
        local = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
        if isinstance(local, dict):
            merged.update(local)  # local file wins
    except Exception:
        pass
    if IS_WIN:
        _load_dpapi_key(merged)  # dpapi1014
    _cache = merged
    return merged


def get_secret(name: str, default=None):
    v = secrets().get(name)
    if isinstance(v, str):
        v = v.strip()
    return v if v else (os.environ.get(name) or default)


def load_secrets_into_env(names) -> list[str]:
    secrets()  # dpapi1014: apply DPAPI override (drops plaintext env copies) first
    present = []
    for n in names:
        if os.environ.get(n):
            present.append(n)
            continue
        v = secrets().get(n)
        if isinstance(v, str) and v.strip():
            os.environ[n] = v.strip()
            present.append(n)
    return present


def maybe_reexec_venv(argv=None) -> None:
    """Re-run under the project venv if not already in it (signing needs solders)."""
    if os.environ.get("DEGEN_NO_VENV_SWITCH") or not VENV_PY.exists():
        return
    try:
        if os.path.realpath(sys.prefix) == os.path.realpath(str(VENV_DIR)):
            return
    except Exception:
        return
    args = [str(VENV_PY)] + list(sys.argv if argv is None else argv)
    if IS_WIN:
        import subprocess
        sys.exit(subprocess.call(args))
    os.execv(str(VENV_PY), args)


def try_lock(fh) -> bool:
    """Non-blocking exclusive lock on an open file handle."""
    if IS_WIN:
        import msvcrt
        pos = fh.tell()
        try:
            fh.seek(1 << 30)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
        finally:
            fh.seek(pos)
    import fcntl
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (BlockingIOError, OSError):
        return False


def unlock(fh) -> None:
    try:
        if IS_WIN:
            import msvcrt
            pos = fh.tell()
            fh.seek(1 << 30)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            fh.seek(pos)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass


def detached_popen_kwargs() -> dict:
    import subprocess
    if IS_WIN:
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"start_new_session": True}


# ---- template: operating side + check interval -------------------------------------------
# Only ONE side may trade a wallet. config.json "run_side" is "box" (Grok Bot box, Phase 1)
# or "pc" (owner's Windows PC, Phase 2). A runner whose platform doesn't match refuses to start.
THIS_SIDE = "pc" if IS_WIN else "box"


def run_side() -> str:
    return (os.environ.get("DEGEN_RUN_SIDE") or config().get("run_side") or "box").strip().lower()


_remote_cache = {"t": 0.0, "side": None, "paused": False}


def remote_run_side():
    """Shared mode flag: <dashboard.public_url>/mode.json on the owner's Cloudflare Worker, published by
    the active side. Cached 60 s. None if no dashboard URL or unreachable (local config then decides)."""
    import time, urllib.request
    url = ((config().get("dashboard") or {}).get("public_url") or "").strip()
    if not url.startswith("https://") or "YOUR-" in url:
        return None
    if time.time() - _remote_cache["t"] < 60:
        return _remote_cache["side"]
    side, paused = None, False
    try:
        req = urllib.request.Request(url.rstrip("/") + "/mode.json", headers={"User-Agent": "degen-mode/1", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=5) as r:
            d = json.loads(r.read().decode("utf-8"))
            side = (d.get("run_side") or "").strip().lower() or None
            paused = bool(d.get("paused"))
    except Exception:
        side, paused = None, False
    _remote_cache.update(t=time.time(), side=side, paused=paused)
    return side


def run_side_ok() -> bool:
    """Local config, MIRROR_ONLY and the shared Worker flag must all agree that this side is active."""
    if run_side() != THIS_SIDE or MIRROR_FLAG.exists():
        return False
    rs = remote_run_side()
    return rs is None or rs == THIS_SIDE


def check_interval_sec() -> int:
    """Edge-check cadence. DEGEN_RUNNER_INTERVAL_SEC > config schedule.check_interval_sec_<side> >
    schedule.check_interval_sec > default (300 s on the box, 60 s on a PC). Minimum 30 s."""
    sch = config().get("schedule") or {}
    v = (os.environ.get("DEGEN_RUNNER_INTERVAL_SEC") or sch.get(f"check_interval_sec_{THIS_SIDE}")
         or sch.get("check_interval_sec") or (60 if IS_WIN else 300))
    try:
        return max(30, int(float(v)))
    except Exception:
        return 60 if IS_WIN else 300


# ---- template: kill switch ("pause trading" / "resume trading") ---------------------------
PAUSE_FILE = HOME / "TRADING_PAUSED"


def trading_paused() -> bool:
    """True if TRADING_PAUSED exists here, or the shared mode.json on the Worker says paused."""
    if PAUSE_FILE.exists():
        return True
    remote_run_side()
    return bool(_remote_cache.get("paused"))


def trades_csv_path() -> str:
    """trades.csv (a study copy of trades.jsonl) is not written in this build."""
    return str(HOME / "trades.csv") if research_logging() else os.devnull


def research_logging() -> bool:
    """Study-only logs are not part of this build: always off."""
    return False


# ---- template: per-install timing jitter (v1.0.0) -------------------------------------------
# config.json "jitter" (made once by jitter_setup.py; not a secret): edge_offset_frac 0..1 of the check
# interval, daily_offset_min (even, -6..+6, non-zero so odd owner minutes stay odd). Spreads installs so
# they don't all hit the same keyless APIs (alternative.me, Kraken, Binance, Coinbase, CoinGecko) at once.
def jitter() -> dict:
    j = config().get("jitter")
    return j if isinstance(j, dict) else {}


def edge_offset_sec(interval: int | None = None) -> int:
    interval = int(interval or check_interval_sec())
    try:
        return int(float(jitter().get("edge_offset_frac", 0)) * interval) % interval
    except Exception:
        return 0


def daily_offset_min() -> int:
    try:
        return max(-6, min(6, int(jitter().get("daily_offset_min", 0))))
    except Exception:
        return 0


def jittered_hm(hm: str, offset_min: int | None = None) -> str:
    """'06:59' -> owner time shifted by this install's daily offset (wraps around midnight)."""
    h, m = (int(x) for x in str(hm).split(":")[:2])
    t = (h * 60 + m + (daily_offset_min() if offset_min is None else int(offset_min))) % 1440
    return f"{t // 60:02d}:{t % 60:02d}"


def next_jittered(after, interval: int | None = None):
    """Next start time on this install's grid (epoch % interval == offset), at least half an interval away."""
    from datetime import timedelta
    interval = int(interval or check_interval_sec())
    off = edge_offset_sec(interval)
    ts = after.timestamp()
    base = ts - ((ts - off) % interval) + interval
    if base - ts < interval / 2:
        base += interval
    return after + timedelta(seconds=base - ts)
