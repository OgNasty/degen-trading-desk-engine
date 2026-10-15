#!/usr/bin/env python3
"""Pre-install / pre-update security check (template). Stdlib only; never prints secrets; sends nothing.

  python security_scan.py scan <engine.txt> [--home DIR]   -> JSON: status clean | suspicious, findings, summary

Looks for: code that reads keys/wallet data and could send them out, contact with domains not on the
trusted list, transfers to addresses that aren't the owner's or a known token/program, hidden downloads
or code execution, system setting changes (scheduled tasks, registry, startup, shell profiles), and
obfuscated code (encoded blobs, exec/eval tricks).
Everything that shipped in engine 1.0.0 is trusted (domains, endpoints, token mints, program ids and the
exact lines that use subprocess/base64/etc.), so the current engine scans clean. For an update, the
trusted list comes from the INSTALLED copy of this file (an update can't vouch for itself), the installed
engine's own lines are trusted too, and a short plain summary of what changed is returned."""
import base64, hashlib, json, re, sys
from pathlib import Path

# ---- trusted list: built from engine 1.0.0 ----
TRUSTED_DOMAINS = {
    "alternative.me", "api.alternative.me", "api.binance.com", "api.cloudflare.com", "api.coingecko.com",
    "api.exchange.coinbase.com", "api.geckoterminal.com", "api.jup.ag", "api.kraken.com",
    "api.mainnet-beta.solana.com", "cloudflareinsights.com", "dash.cloudflare.com", "dashboard.alchemy.com",
    "dashboard.helius.dev", "mainnet.helius-rpc.com",
    "portal.jup.ag", "raw.githubusercontent.com", "rpc.ankr.com",
    "solana-rpc.publicnode.com", "solscan.io", "static.cloudflareinsights.com", "transaction-v1.raydium.io",
    "workers.dev", "example.com", "rpc.invalid", "github.com", "helius-rpc.com", "alchemy.com",
    "solana-mainnet.g.alchemy.com", "pypi.org", "files.pythonhosted.org",
    "your-domain.com",  # setup placeholder for an optional custom dashboard hostname
}
TRUSTED_ADDRESSES = {
    "11111111111111111111111111111111", "5NjzDRLdEvDL3nyZybdEj4tMhLVNE1dpWeL5dHv8zk6s",
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs", "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "ComputeBudget111111111111111111111111111111",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "GGiveYrqhfJc5pgBnjuY3NP5cW3ni1nFHeJXYZGFappF",
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn", "So11111111111111111111111111111111111111112",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij",
}
BASELINE_FILE = "security-baseline.json"  # hashes of the risky-looking lines that shipped in 1.0.0

CHECKS = [  # (category, plain label, regex)
    ("keys", "reads a private key or secret", r"PRIVATE_KEY|DEGEN_WALLET_KEY|GITHUB_PASSWORD|secret_key|keypair|from_base58|mnemonic|seed phrase|id\.json|\.env\b"),
    ("network", "sends data out", r"urlopen|requests\.(post|put)|httpx|urllib\.request|\bcurl\b|wget|fetch\(|XMLHttpRequest|sendBeacon|WebSocket|socket\.|smtplib|Invoke-WebRequest|Invoke-RestMethod|Net\.WebClient"),
    ("transfer", "moves funds", r"transfer(_checked)?\s*\(|TransferParams|SystemProgram|create_associated_token|close_account|set_authority|approve\s*\("),
    ("exec", "downloads or runs other code", r"\bexec\s*\(|\beval\s*\(|compile\s*\(|__import__|marshal\.|pickle\.loads|subprocess|os\.system|os\.popen|Popen|\|\s*(ba)?sh\b|\biex\b|Start-Process|pip install|-EncodedCommand|\bpowershell\b.*-e(nc)?\b"),
    ("system", "changes system settings", r"crontab|schtasks|Register-ScheduledTask|winreg|reg (add|delete)|HKEY_|CurrentVersion\\\\?Run|Startup\\\\|\.bashrc|\.profile|/etc/|sudo |Set-MpPreference|netsh|Add-MpPreference|chmod\s+[0-7]*7[0-7]*7|ExecutionPolicy"),
    ("obfuscated", "contains hidden or encoded code", r"b64decode|base64 -d|FromBase64String|codecs\.decode|rot_?13|bytes\.fromhex|\\x[0-9a-fA-F]{2}(\\x[0-9a-fA-F]{2}){7,}|chr\(\d+\)\s*\+\s*chr|[A-Za-z0-9+/=]{200,}"),
]
OWNER_WORDS = {  # how each check reads to the owner
    "keys": "reads your wallet key or another secret", "network": "sends information out to the internet",
    "transfer": "moves money out of the wallet", "exec": "downloads or runs other programs",
    "system": "changes settings on the computer", "obfuscated": "is hidden or scrambled",
}
CHECKS_RX = [(c, l, re.compile(r, re.I)) for c, l, r in CHECKS]
URL_RX = re.compile(r"(?:https?|wss?)://([A-Za-z0-9.\-]+)", re.I)
HOST_RX = re.compile(r"[\"'@]((?:[a-z0-9\-]+\.)+(?:com|io|ag|net|me|dev|org|so|xyz|app|co|ru|cn|top|gg|cc|tk|ly|link|site|online|pw|info|biz))(?=[\"'/:?])", re.I)
ADDR_RX = re.compile(r"(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{32,44}(?![A-Za-z0-9])")
EVM_RX = re.compile(r"\b0x[0-9a-fA-F]{40}\b")
SCAN_EXT = (".py", ".sh", ".ps1", ".cmd", ".bat", ".vbs", ".js", ".html", ".json", ".toml", ".txt", ".example", ".md")
DOC_EXT = (".md",)  # docs: only domains/addresses matter, not wording


def _norm(path: str, line: str) -> str:
    return hashlib.sha256((path.rsplit("/", 1)[-1] + "\0" + line.strip()).encode()).hexdigest()[:20]


def _trusted_domain(h: str) -> bool:
    h = h.lower().rstrip(".")
    return any(h == d or h.endswith("." + d) for d in TRUSTED_DOMAINS)


def _is_text(raw: bytes):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def risky_lines(path: str, text: str):
    """Yield (category, label, line_no, line) for lines that match a behaviour check."""
    if path.endswith(DOC_EXT):
        return
    for n, line in enumerate(text.splitlines(), 1):
        for c, l, rx in CHECKS_RX:
            if rx.search(line):
                yield c, l, n, line


def parse_bundle(blob: bytes) -> dict:
    files, i, M = {}, 0, b"\n=== FILE: "
    while True:
        i = blob.find(M, i)
        if i < 0:
            break
        e = blob.index(b"\n", i + 1)
        parts = [x.strip() for x in blob[i + len(M):e].decode().rstrip(" =").split("|")]
        kv = dict(x.split("=", 1) for x in parts[1:])
        n = int(kv["bytes"])
        data = blob[e + 1:e + 1 + n]
        files[parts[0]] = base64.b64decode(data) if kv.get("enc") == "base64" else data
        i = e + 1 + n
    return files


def load_baseline(home: Path | None) -> set:
    here = Path(__file__).resolve().parent
    for d in ([home] if home else []) + [here]:
        p = d / BASELINE_FILE
        if p.exists():
            try:
                return set(json.loads(p.read_text(encoding="utf-8")).get("lines", []))
            except Exception:
                pass
    return set()


def owner_address(home: Path | None) -> set:
    out = set()
    if home:
        try:
            a = json.loads((home / "config.json").read_text(encoding="utf-8")).get("wallet", {}).get("address", "")
            if a:
                out.add(a.strip())
        except Exception:
            pass
    return out


def installed_files(home: Path | None) -> dict:
    """engine-path -> bytes for what is installed now (code/X <- HOME/X, docs/X <- HOME/docs/X)."""
    out = {}
    if not home or not home.exists():
        return out
    skip = {".venv", "engine-backups", "__pycache__", "logs", "state", "data"}
    for p in home.rglob("*"):
        rel = p.relative_to(home)
        if not p.is_file() or any(s in rel.parts for s in skip):
            continue
        r = rel.as_posix()
        out[r[5:] if r.startswith("docs/") else "code/" + r] = p.read_bytes()
    return out


def scan_files(files: dict, home: Path | None = None) -> dict:
    trusted = load_baseline(home)
    old = installed_files(home)
    for p, raw in old.items():  # what is installed now is trusted
        t = _is_text(raw)
        if t is not None:
            trusted.update(_norm(p, ln) for _, _, _, ln in risky_lines(p, t))
    addrs = TRUSTED_ADDRESSES | owner_address(home)
    findings = []
    for path in sorted(files):
        raw = files[path]
        if path.endswith((".png", ".jpg", ".ico")):
            continue
        text = _is_text(raw)
        if text is None:
            findings.append({"file": path, "line": 0, "kind": "obfuscated", "what": "a file that should be plain text is actually a program or other binary file"})
            continue
        if not path.lower().endswith(SCAN_EXT) and "." in path.rsplit("/", 1)[-1]:
            findings.append({"file": path, "line": 0, "kind": "exec", "what": "a file of a type the bot doesn't normally use"})
        for n, line in enumerate(text.splitlines(), 1):
            for h in set(URL_RX.findall(line)) | set(HOST_RX.findall(line)):
                h = h.lower().rstrip(".")
                if "." in h and not _trusted_domain(h) and "{" not in h and "your-" not in h:
                    findings.append({"file": path, "line": n, "kind": "network", "what": f"it talks to a website I don't recognize ({h.lower()})"})
            for a in set(ADDR_RX.findall(line)):
                if a not in addrs and not a.isalpha() and re.search(r"\d", a):
                    findings.append({"file": path, "line": n, "kind": "transfer", "what": f"it mentions a Solana wallet or app address I don't recognize ({a[:6]}…{a[-4:]})"})
            for a in set(EVM_RX.findall(line)):
                findings.append({"file": path, "line": n, "kind": "transfer", "what": f"it mentions a wallet address I don't recognize ({a[:8]}…)"})
        seen = set()
        for c, l, n, line in risky_lines(path, text):
            if _norm(path, line) in trusted or (n, c) in seen:
                continue
            seen.add((n, c))
            findings.append({"file": path, "line": n, "kind": c, "what": f"there's new code that {OWNER_WORDS.get(c, l)}"})
    # a line that both reads a key and talks to the network is the worst case
    keyl = {(f["file"], f["line"]) for f in findings if f["kind"] == "keys"}
    for f in findings:
        if f["kind"] == "network" and (f["file"], f["line"]) in keyl:
            f["what"] += ", right next to code that reads your key"
    summary = change_summary(old, files) if old else None
    res = {"status": "suspicious" if findings else "clean", "files_scanned": len(files),
           "findings": findings[:60], "finding_count": len(findings)}
    if summary:
        res["changes"] = summary
    res["plain"] = plain(res)
    return res


def change_summary(old: dict, new: dict) -> dict:
    added = sorted(set(new) - set(old) - {"__extractor__.py"})
    # Only engine code counts as "removed"; the owner's own state (logs, backups, alert state) is never touched.
    removed = sorted(p for p in set(old) - set(new) if p.startswith("code/") and not _owner_like(p)
                     and p.endswith(_CODE_EXT) and ".bak" not in p.rsplit("/", 1)[-1])
    changed = []
    for p in sorted(set(new) & set(old)):
        if new[p] != old[p]:
            a, b = (_is_text(old[p]) or "").splitlines(), (_is_text(new[p]) or "").splitlines()
            import difflib
            plus = minus = 0
            for d in difflib.unified_diff(a, b, lineterm="", n=0):
                if d.startswith("+") and not d.startswith("+++"):
                    plus += 1
                elif d.startswith("-") and not d.startswith("---"):
                    minus += 1
            changed.append(f"{p.rsplit('/', 1)[-1]} (+{plus}/-{minus} lines)")
    return {"added": added, "removed": removed, "changed": changed}


_CODE_EXT = (".py", ".sh", ".ps1", ".cmd", ".bat", ".vbs", ".js", ".mjs", ".html", ".toml")


def _owner_like(p: str) -> bool:
    b = p.rsplit("/", 1)[-1]
    return b in {".env", "config.json", "cost-basis.json", "setup-state.json", "features.json", "TRADING_PAUSED",
                 "mode.json", "allowed-pairs.json", "cadence.json", "reporting.json"} or b.endswith((".jsonl", ".log", ".bak")) or b.startswith("wallet")


def plain(res: dict) -> str:
    out = []
    ch = res.get("changes")

    def _n(k, one, many):
        return f"{k} {one if k == 1 else many}"

    def _list(xs):
        xs = [x.split(' ', 1)[0].split('/')[-1] for x in xs[:8]] + (["a few more"] if len(xs) > 8 else [])
        return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]
    if ch:
        bits = []
        if ch["changed"]:
            bits.append(f"{_n(len(ch['changed']), 'file was', 'files were')} updated ({_list(ch['changed'])})")
        if ch["added"]:
            bits.append(f"{_n(len(ch['added']), 'file was', 'files were')} added ({_list(ch['added'])})")
        if ch["removed"]:
            bits.append(f"{_n(len(ch['removed']), 'file was', 'files were')} removed ({_list(ch['removed'])})")
        out.append(("Here's what's different in this version: " + "; ".join(bits) + ".") if bits
                   else "None of the bot's files are different in this version.")
    if res["status"] == "clean":
        out.append("The security check came back clean. Nothing in it sends your keys anywhere, talks to websites or "
                   "wallets I don't recognize, sneaks in downloads, changes your computer's settings or hides code.")
    else:
        kinds = {}
        for f in res["findings"]:
            kinds.setdefault(f["what"], []).append(f"{f['file'].split('/')[-1]} line {f['line']}")
        k = res['finding_count']
        out.append(f"The security check found {_n(k, 'thing', 'things')} I'd want you to look at before I install this:")
        for w, locs in list(kinds.items())[:10]:
            out.append(f"- {w[0].upper() + w[1:]} (in {', '.join(locs[:3])}{', and more' if len(locs) > 3 else ''}).")
    return "\n".join(out)


def scan_bundle(path: str, home: str | None = None) -> dict:
    return scan_files(parse_bundle(Path(path).read_bytes()), Path(home) if home else None)


def build_baseline(root: str, bundle: str | None = None) -> int:
    """Maintainer only: record the risky-looking lines of a trusted engine tree (code/ + docs)."""
    r = Path(root)
    lines = set()
    for p in r.rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts and ".bak" not in p.name:
            t = _is_text(p.read_bytes())
            rel = p.relative_to(r).as_posix()
            if t is not None:
                lines.update(_norm(rel, ln) for _, _, _, ln in risky_lines(rel, t))
    if bundle:  # the extractor ships inside the bundle only
        ex = parse_bundle(Path(bundle).read_bytes()).get("__extractor__.py", b"").decode("utf-8", "replace")
        lines.update(_norm("__extractor__.py", ln) for _, _, _, ln in risky_lines("__extractor__.py", ex))
    (r / "code" / BASELINE_FILE).write_text(json.dumps({"engine": "1.0.0", "lines": sorted(lines)}, indent=0) + "\n", encoding="utf-8")
    return len(lines)


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["baseline"]:
        print(build_baseline(a[1], a[2] if len(a) > 2 else None))
        sys.exit(0)
    if a[:1] != ["scan"] or len(a) < 2:
        print(__doc__)
        sys.exit(2)
    home = a[a.index("--home") + 1] if "--home" in a else None
    r = scan_bundle(a[1], home)
    print(json.dumps(r, indent=1))
    sys.exit(0 if r["status"] == "clean" else 3)
