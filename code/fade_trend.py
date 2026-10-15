"""Refuse to fade a trend on edge-check sells. Not a fill.

Applies only when an edge check is about to sell SOL, USDC, cbBTC, or WETH,
including a sell that funds a savings or memecoin buy in that same check.
Friday RSI and the morning underweight trade are other paths (Friday band fallback retired) and
must not call this gate.

Skip only when the sold token's current USD price is strictly above its USD
price at the previous edge check AND strictly above its USD price 24 hours ago.
Equal to either price does not skip. A missing previous price, or a missing or
failed 24h price, does not skip (the trend is not confirmed). Those cases log
fade_trend_price_missing and do not raise. A fade_trend skip does not change
same-pair streak, pair multipliers, or cost basis.

Current and 24h USD prices come from one Jupiter lite price v3 response, cached
for the check. If Jupiter fails or rate-limits (429), CoinGecko simple price is
the automatic fallback (ids solana/bitcoin/ethereum for SOL/cbBTC/WETH; USDC=1).
Never invent prices. Log PRICE_FALLBACK source=coingecko when used. CoinGecko
results are cached briefly (COINGECKO_CACHE_SEC). 24h-ago USD = usd /
(1 + change_24h/100). The four current USD prices are written at the end of the
check to usd-price-snapshot.json.
"""
import degen_home as _DH  # portable base dir (pcmove 2026-10-10)
import json, os, subprocess, tempfile, time
from datetime import datetime
from zoneinfo import ZoneInfo
from degen_home import TZ_NAME as _TZ_NAME

PT = ZoneInfo(_TZ_NAME)
TRACKED = ("SOL", "USDC", "cbBTC", "WETH")
MINTS = {
    "SOL": "So11111111111111111111111111111111111111112",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "cbBTC": "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij",
    "WETH": "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
}
SNAPSHOT_PATH = _DH.HOME_STR + "/usd-price-snapshot.json"
JUP_PRICE_API = "https://api.jup.ag/price/v3"  # jupkey1012: lite-api retiring; keyed (keyless fallback)
COINGECKO_PRICE_API = "https://api.coingecko.com/api/v3/simple/price"
SOURCE = "jupiter_lite_price_v3"
SOURCE_COINGECKO = "coingecko_simple"
# CoinGecko free simple price: solana/bitcoin/ethereum proxy SOL/cbBTC/WETH.
COINGECKO_IDS = {"solana": "SOL", "bitcoin": "cbBTC", "ethereum": "WETH"}
COINGECKO_CACHE_SEC = 45.0
_COINGECKO_CACHE = {"at": 0.0, "parsed": None, "err": None}

def atomic_write_json(path, obj):
    """Replace path with a complete JSON file. Readers see the old or new file."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".atomic-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(obj, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def _pos_float(value):
    try:
        if value is None or isinstance(value, bool):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number <= 0:
        return None
    return number

def _float_or_none(value):
    try:
        if value is None or isinstance(value, bool):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number

def usd_24h_ago(usd_price, change_pct):
    """Jupiter priceChange24h is a percent. None if it cannot be converted."""
    usd = _pos_float(usd_price)
    change = _float_or_none(change_pct)
    if usd is None or change is None:
        return None
    denom = 1.0 + (change / 100.0)
    if denom <= 0:
        return None
    ago = usd / denom
    if ago != ago or ago <= 0:
        return None
    return ago

def fade_sell_reason(symbol, current, previous, ago):
    """Why this sell is or is not a fade. Never raises.

    fade_trend: current > previous and current > ago. Caller must skip.
    fade_trend_price_missing: cannot confirm. Caller must not skip.
    None: not strictly above both (includes equality). Caller must not skip.
    Untracked symbols are not this rule and return None.
    """
    try:
        if symbol not in TRACKED:
            return None
        current_px = _pos_float(current)
        previous_px = _pos_float(previous)
        ago_px = _pos_float(ago)
        if current_px is None or previous_px is None or ago_px is None:
            return "fade_trend_price_missing"
        if current_px > previous_px and current_px > ago_px:
            return "fade_trend"
        return None
    except Exception:
        return "fade_trend_price_missing"

def accepts_edge_sell(symbol, current, previous, ago):
    """False only for a confirmed fade. Missing prices still accept the sell."""
    return fade_sell_reason(symbol, current, previous, ago) != "fade_trend"

def parse_jupiter_price_map(payload):
    """Map symbol -> usd, price_change_24h_pct, usd_24h from one v3 payload."""
    data = payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        if not any(mint in payload for mint in MINTS.values()):
            data = payload["data"]
    found = {}
    if not isinstance(data, dict):
        return found
    by_mint = {mint: sym for sym, mint in MINTS.items()}
    for mint, row in data.items():
        sym = by_mint.get(mint)
        if sym is None or not isinstance(row, dict):
            continue
        usd = _pos_float(row.get("usdPrice"))
        change = _float_or_none(row.get("priceChange24h"))
        found[sym] = {
            "mint": mint,
            "usd": usd,
            "price_change_24h_pct": change,
            "usd_24h": usd_24h_ago(usd, change),
        }
    return found

def fetch_jupiter_prices(timeout=20, retries=4):
    """One request for SOL, USDC, cbBTC, and WETH. Raises if every attempt fails.

    Cached by EdgeFade for the rest of the check. A 429 is retried. Callers
    treat a raised error as a missing price and do not skip the sell.
    """
    ids = ",".join(MINTS[sym] for sym in TRACKED)
    url = JUP_PRICE_API + "?ids=" + ids
    last = "jupiter_price_failed"
    for i in range(retries):
        import jupkey as _JK  # jupkey1012
        with _JK.curl_args(url) as _kh:
            proc = subprocess.run(
                ["curl", "-sS", "-w", "\n%{http_code}", "--max-time", str(timeout),
                 "-H", "Accept: application/json", *_kh, url],
                capture_output=True, text=True,
            )
        if proc.returncode != 0:
            last = (proc.stderr or "").strip() or f"curl_exit_{proc.returncode}"
            time.sleep(1.5 * (i + 1))
            continue
        body, _, code = (proc.stdout or "").rpartition("\n")
        code = code.strip()
        if _kh and code in ("401", "403") and _JK.fail(code, url):
            continue
        if code == "429" or "Too many requests" in body or "Rate limit" in body:
            last = "rate_limit_429"
            time.sleep(1.5 * (i + 1))
            continue
        if code != "200":
            last = f"http_{code}"
            time.sleep(1.5 * (i + 1))
            continue
        try:
            payload = json.loads(body)
        except Exception as exc:
            last = type(exc).__name__
            time.sleep(1.0)
            continue
        return parse_jupiter_price_map(payload), None
    raise RuntimeError(last)

def parse_coingecko_price_map(payload):
    """Map CoinGecko simple-price JSON to the same shape as Jupiter parse."""
    found = {}
    data = payload if isinstance(payload, dict) else {}
    for cid, sym in COINGECKO_IDS.items():
        row = data.get(cid)
        if not isinstance(row, dict):
            continue
        usd = _pos_float(row.get("usd"))
        change = _float_or_none(row.get("usd_24h_change"))
        found[sym] = {
            "mint": MINTS.get(sym),
            "usd": usd,
            "price_change_24h_pct": change,
            "usd_24h": usd_24h_ago(usd, change),
        }
    # Stablecoin mark; no CoinGecko call.
    found["USDC"] = {
        "mint": MINTS["USDC"],
        "usd": 1.0,
        "price_change_24h_pct": 0.0,
        "usd_24h": 1.0,
    }
    return found

def fetch_coingecko_prices(timeout=15, retries=2, use_cache=True):
    """Free CoinGecko simple price for SOL/cbBTC/WETH. Raises if every attempt fails.

    Brief in-process cache (COINGECKO_CACHE_SEC) so morning + edge check in the
    same minute share one call. Never invents prices.
    """
    now = time.monotonic()
    if use_cache and _COINGECKO_CACHE["parsed"] is not None:
        if now - float(_COINGECKO_CACHE["at"] or 0) <= COINGECKO_CACHE_SEC:
            return dict(_COINGECKO_CACHE["parsed"]), None
    ids = ",".join(COINGECKO_IDS.keys())
    url = (
        COINGECKO_PRICE_API
        + "?ids=" + ids
        + "&vs_currencies=usd&include_24hr_change=true"
    )
    last = "coingecko_price_failed"
    for i in range(retries):
        proc = subprocess.run(
            ["curl", "-sS", "-w", "\n%{http_code}", "--max-time", str(timeout),
             "-H", "Accept: application/json", url],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            last = (proc.stderr or "").strip() or f"curl_exit_{proc.returncode}"
            time.sleep(1.0 * (i + 1))
            continue
        body, _, code = (proc.stdout or "").rpartition("\n")
        code = code.strip()
        if code == "429" or "Too many requests" in body or "Rate limit" in body:
            last = "rate_limit_429"
            time.sleep(2.0 * (i + 1))
            continue
        if code != "200":
            last = f"http_{code}"
            time.sleep(1.0 * (i + 1))
            continue
        try:
            payload = json.loads(body)
        except Exception as exc:
            last = type(exc).__name__
            time.sleep(0.5)
            continue
        parsed = parse_coingecko_price_map(payload)
        # Require the three crypto marks; USDC is synthetic.
        if not all(_pos_float((parsed.get(s) or {}).get("usd")) for s in ("SOL", "cbBTC", "WETH")):
            last = "coingecko_incomplete"
            time.sleep(0.5)
            continue
        _COINGECKO_CACHE["at"] = time.monotonic()
        _COINGECKO_CACHE["parsed"] = parsed
        _COINGECKO_CACHE["err"] = None
        return dict(parsed), None
    _COINGECKO_CACHE["err"] = last
    raise RuntimeError(last)

def fetch_usd_prices(timeout=20, retries=4):
    """Jupiter lite price/v3 first; CoinGecko automatic fallback. Never invents.

    Returns (parsed_map, source_str). source is jupiter_lite_price_v3 or
    coingecko_simple. Raises only when both sources fail.
    """
    jup_err = None
    try:
        parsed, _ = fetch_jupiter_prices(timeout=timeout, retries=retries)
        # Need at least SOL to drive flat-market / marks; fill missing from CG.
        if _pos_float((parsed.get("SOL") or {}).get("usd")):
            return parsed, SOURCE
        jup_err = "jupiter_missing_sol"
    except Exception as exc:
        jup_err = str(exc)[:120] if str(exc) else type(exc).__name__
    try:
        parsed, _ = fetch_coingecko_prices(timeout=min(15, timeout), retries=2)
    except Exception as cg_exc:
        raise RuntimeError(
            f"jupiter:{jup_err};coingecko:{cg_exc}"
        ) from cg_exc
    print(
        f"PRICE_FALLBACK source=coingecko reason={jup_err}",
        flush=True,
    )
    return parsed, SOURCE_COINGECKO

def load_previous(path=SNAPSHOT_PATH):
    """USD prices recorded at the end of the previous edge check. Missing is ok."""
    if not os.path.exists(path):
        return {}, "no_snapshot"
    try:
        with open(path) as handle:
            data = json.load(handle)
    except Exception:
        return {}, "snapshot_unreadable"
    prices = data.get("prices_usd") if isinstance(data, dict) else None
    if not isinstance(prices, dict):
        return {}, "snapshot_missing_prices"
    out = {}
    for sym in TRACKED:
        out[sym] = _pos_float(prices.get(sym))
    return out, "ok"

class EdgeFade:
    """One check's cached Jupiter quotes and the previous snapshot."""

    def __init__(self):
        self._ready = False
        self.previous = {}
        self.previous_status = "not_loaded"
        self.quote_by_symbol = {}
        self.fetch_error = None
        self.price_source = None
        self.events = []
        self._logged = set()

    def begin_check(self):
        """Load the last snapshot and fetch 24h quotes once. Does not raise."""
        if self._ready:
            return
        self._ready = True
        try:
            self.previous, self.previous_status = load_previous()
        except Exception as exc:
            self.previous = {}
            self.previous_status = "snapshot_unreadable"
            self._note(None, "fade_trend_price_missing", type(exc).__name__)
        try:
            parsed, source = fetch_usd_prices()
            self.quote_by_symbol = parsed
            self.fetch_error = None
            self.price_source = source
        except Exception as exc:
            self.quote_by_symbol = {}
            self.fetch_error = type(exc).__name__
            self.price_source = None
            self._note(None, "fade_trend_price_missing", type(exc).__name__)

    def _note(self, symbol, reason, detail=None):
        if reason not in ("fade_trend", "fade_trend_price_missing"):
            return
        key = (symbol, reason)
        if key in self._logged:
            return
        self._logged.add(key)
        row = {"symbol": symbol, "reason": reason}
        if detail:
            row["detail"] = str(detail)[:160]
        self.events.append(row)
        sold = symbol if symbol else "-"
        print(f"FADE_TREND {reason} sold={sold}", flush=True)

    def sell_gate(self, symbol):
        """fade_trend, fade_trend_price_missing, or None. Does not raise."""
        try:
            if symbol not in TRACKED:
                return None
            self.begin_check()
            row = self.quote_by_symbol.get(symbol) or {}
            reason = fade_sell_reason(
                symbol,
                row.get("usd"),
                self.previous.get(symbol),
                row.get("usd_24h"),
            )
            self._note(symbol, reason)
            return reason
        except Exception as exc:
            self._note(symbol, "fade_trend_price_missing", type(exc).__name__)
            return "fade_trend_price_missing"

    def blocks(self, symbol):
        """True only when the sell must be skipped. Missing prices do not block."""
        return self.sell_gate(symbol) == "fade_trend"

    def snapshot_doc(self, checked_pt):
        self.begin_check()
        prices = {}
        changes = {}
        for sym in TRACKED:
            row = self.quote_by_symbol.get(sym) or {}
            prices[sym] = row.get("usd")
            changes[sym] = row.get("price_change_24h_pct")
        src = self.price_source or SOURCE
        api = COINGECKO_PRICE_API if src == SOURCE_COINGECKO else JUP_PRICE_API
        return {
            "checked_pt": checked_pt,
            "timezone": _TZ_NAME,
            "source": src,
            "api": api,
            "current_field": "usd" if src == SOURCE_COINGECKO else "usdPrice",
            "change_24h_field": "usd_24h_change" if src == SOURCE_COINGECKO else "priceChange24h",
            "usd_24h_ago": "usd / (1 + change_24h/100)",
            "prices_usd": prices,
            "price_change_24h_pct": changes,
            "error": self.fetch_error,
        }

    def write_snapshot(self, checked_pt, path=SNAPSHOT_PATH):
        """Record this check's current USD prices for the next check. Does not raise."""
        try:
            if not checked_pt:
                checked_pt = datetime.now(PT).isoformat(timespec="milliseconds")
            doc = self.snapshot_doc(checked_pt)
            atomic_write_json(path, doc)
            return doc
        except Exception as exc:
            self._note(None, "fade_trend_price_missing", type(exc).__name__)
            return {
                "checked_pt": checked_pt,
                "timezone": _TZ_NAME,
                "source": SOURCE,
                "api": JUP_PRICE_API,
                "prices_usd": {sym: None for sym in TRACKED},
                "error": type(exc).__name__,
            }

def edge_funding_blocked(source, symbol, sell_amt):
    """True when an edge-check funding sell must be refused.

    Morning balance and any other source are not this rule. A zero sell amount
    is not a book-token sale. Errors do not skip and do not raise.
    """
    try:
        if source != "edge_check":
            return False
        amount = float(sell_amt or 0)
        if amount <= 0:
            return False
        return current().blocks(symbol)
    except Exception as exc:
        print(f"FADE_TREND fade_trend_price_missing sold={symbol} {type(exc).__name__}", flush=True)
        return False

_BOOK = None

def current():
    global _BOOK
    if _BOOK is None:
        _BOOK = EdgeFade()
    return _BOOK

def begin_check():
    current().begin_check()

def reset_book():
    """Test helper. Drops the cached check so the next call starts clean."""
    global _BOOK
    _BOOK = None
