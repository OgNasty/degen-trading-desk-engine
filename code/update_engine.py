#!/usr/bin/env python3
"""Engine updater (template). Stdlib only; works on the box and the PC.

  python update_engine.py check            -> JSON: status up_to_date | update_available | error
  python update_engine.py apply [--yes]    -> download, verify, pause, back up, install, check, resume
  python update_engine.py install --dest DIR [--engine-file F]   -> fresh install (setup Step 1 fallback)
  python update_engine.py scan [--engine-file F]  -> download + verify + security check only (installs nothing)

Every install and update runs security_scan.py first (keys sent out, unknown domains or addresses,
hidden downloads/exec, system changes, obfuscated code; updates are also diffed against what is
installed). If anything looks suspicious, nothing is installed and the result is status
"security_hold" with a plain summary; rerun with --accept-security-findings only after the owner
says to install anyway.

Remote: config.json "engine_manifest_url" (default {{ENGINE_URL}} with the file name swapped for
engine-version.json) and "engine_url". http(s):// and file:// URLs both work.
The manifest is small (version, released, sha256, bytes, notes, engine_url) so `check` never downloads
the full engine. apply only replaces files that ship in the engine's code/ tree (plus docs/), so owner
files (.env, config.json, wallet, state, trades, cost basis, setup-state, features) are never written;
they are hashed before and after to prove it. Any failure rolls back automatically and restores the
previous pause state. Never prints secrets; never trades; sends nothing."""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

HOME = Path(os.environ.get("DEGEN_HOME") or Path(__file__).resolve().parent)
PLACEHOLDER = "{{ENGINE_URL}}"
DEFAULT_ENGINE_URL = "https://raw.githubusercontent.com/OgNasty/degen-trading-desk-engine/main/TRADING%20TEMPLATE%20ENGINE.txt"
MANIFEST_NAME = "engine-version.json"
# Owner data: never overwritten by an update (checked by name and by hash).
PROTECT = {
    ".env", "config.json", "cost-basis.json", "setup-state.json", "features.json", "wallet.json",
    "wallet-backup.json", "state.json", "trades.jsonl", "trades.json", "allowed-pairs.json", "cadence.json",
    "reporting.json", "secrets.json", "TRADING_PAUSED", "mode.json",
}
PROTECT_GLOBS = ("*.jsonl", "*.log", "*-state.json", "*.db", "wallet*")
SKIP_DIRS = {".venv", "engine-backups", "__pycache__", "logs", "state", "data"}


class UpdateError(Exception):
    pass


def _cfg() -> dict:
    try:
        return json.loads((HOME / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def urls() -> tuple[str, str]:
    c = _cfg()
    real = lambda v: v if v and "{{" not in str(v) else ""
    eng = real(os.environ.get("ENGINE_URL")) or real(c.get("engine_url")) or DEFAULT_ENGINE_URL
    man = real(os.environ.get("ENGINE_MANIFEST_URL")) or real(c.get("engine_manifest_url"))
    if not man and eng != PLACEHOLDER:
        man = eng.rsplit("/", 1)[0] + "/" + MANIFEST_NAME
    return eng, man


def _fetch(url: str, timeout=60) -> bytes:
    if not url or PLACEHOLDER in url or "{{" in url:
        raise UpdateError("ENGINE_URL is still the placeholder; set engine_url in config.json")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read()
    except Exception as e:  # message only; no headers or bodies
        raise UpdateError(f"download failed for {url.split('?')[0]}: {type(e).__name__}: {e}") from None


def _ver(v: str) -> tuple:
    out = []
    for p in str(v).split("-")[0].split("."):
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    return tuple((out + [0, 0, 0])[:3])


def installed_version() -> dict:
    try:
        return json.loads((HOME / "ENGINE_VERSION.json").read_text(encoding="utf-8"))
    except Exception:
        return {"version": "0.0.0", "released": "unknown"}


def remote_manifest() -> dict:
    _, man = urls()
    try:
        m = json.loads(_fetch(man, 30).decode("utf-8"))
    except UpdateError:
        raise
    except Exception as e:
        raise UpdateError(f"manifest is not valid JSON: {type(e).__name__}") from None
    for k in ("version", "sha256", "bytes"):
        if k not in m:
            raise UpdateError(f"manifest missing '{k}'")
    return m


def check() -> dict:
    inst = installed_version()
    try:
        m = remote_manifest()
    except UpdateError as e:
        return {"status": "error", "installed": inst.get("version"), "error": str(e)}
    newer = _ver(m["version"]) > _ver(inst.get("version", "0.0.0"))
    return {"status": "update_available" if newer else "up_to_date", "installed": inst.get("version"),
            "remote": m["version"], "released": m.get("released"), "notes": m.get("notes", ""),
            "bytes": m.get("bytes")}


def verify_engine(blob: bytes, manifest: dict) -> None:
    if len(blob) != int(manifest["bytes"]):
        raise UpdateError(f"size mismatch: got {len(blob)} bytes, manifest says {manifest['bytes']}")
    h = hashlib.sha256(blob).hexdigest()
    if h != manifest["sha256"]:
        raise UpdateError(f"hash mismatch: got {h[:12]}..., manifest says {manifest['sha256'][:12]}... (file corrupt or tampered; nothing installed)")


def parse_engine(blob: bytes) -> dict:
    """Return {relpath: raw bytes}; every file's sha256 must match its header."""
    files, i, M = {}, 0, b"\n=== FILE: "
    while True:
        i = blob.find(M, i)
        if i < 0:
            break
        e = blob.index(b"\n", i + 1)
        head = blob[i + len(M):e].decode().rstrip(" =")
        parts = [x.strip() for x in head.split("|")]
        kv = dict(x.split("=", 1) for x in parts[1:])
        n = int(kv["bytes"])
        data = blob[e + 1:e + 1 + n]
        if blob[e + 1 + n:e + 1 + n + 17] != b"\n=== END FILE ===":
            raise UpdateError(f"engine block malformed: {parts[0]}")
        raw = base64.b64decode(data) if kv.get("enc") == "base64" else data
        if hashlib.sha256(raw).hexdigest() != kv["sha256"]:
            raise UpdateError(f"file hash mismatch inside engine: {parts[0]}")
        if parts[0] != "__extractor__.py":
            files[parts[0]] = raw
        i = e + 1 + n
    if not files:
        raise UpdateError("engine contains no files")
    return files


def security_check(files_raw: dict, home: Path | None) -> dict:
    """Run the INSTALLED security_scan.py (an update can't vouch for itself)."""
    import importlib.util
    here = Path(__file__).resolve().parent
    src = (home / "security_scan.py") if home and (home / "security_scan.py").exists() else here / "security_scan.py"
    if not src.exists():
        raise UpdateError("security check tool (security_scan.py) is missing; nothing installed")
    spec = importlib.util.spec_from_file_location("degen_security_scan", src)
    mod = importlib.util.module_from_spec(spec)
    old_flag, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # leave the install folder untouched
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = old_flag
    return mod.scan_files(files_raw, home)


def _hold(res: dict, sec: dict) -> dict:
    res.update({"status": "security_hold", "note": "nothing was installed; ask the owner before installing",
                "security": sec["plain"], "finding_count": sec["finding_count"], "findings": sec["findings"][:20]})
    return res


def _protected(name: str) -> bool:
    from fnmatch import fnmatch
    base = name.rsplit("/", 1)[-1]
    return base in PROTECT or any(fnmatch(base, g) for g in PROTECT_GLOBS)


def plan(files: dict) -> dict:
    """Map engine paths -> install paths relative to HOME. code/X -> X; SETUP.md etc -> docs/."""
    out = {}
    for rel, raw in files.items():
        dest = rel[5:] if rel.startswith("code/") else "docs/" + rel
        if _protected(dest) and not dest.startswith("seeds/"):
            raise UpdateError(f"engine tried to overwrite owner file {dest}; refused")
        out[dest] = raw
    return out


def _owner_hashes() -> dict:
    res = {}
    for p in HOME.rglob("*"):
        if not p.is_file() or any(s in p.relative_to(HOME).parts for s in SKIP_DIRS):
            continue
        rel = p.relative_to(HOME).as_posix()
        if _protected(rel) and not rel.startswith("seeds/"):
            res[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return res


def _checks(stage_fail: bool) -> None:
    py = sys.executable
    env = dict(os.environ, DEGEN_HOME=str(HOME), PAPER_TRADING="true", PYTHONDONTWRITEBYTECODE="1")
    for k in ("SOLANA_TRADING_PRIVATE_KEY", "TRADING_ALERT_WEBHOOK_URL", "TRADING_ALERT_WEBHOOK_KEY"):
        env.pop(k, None)  # dry check never sees a signer or an alert hook
    pys = [str(p) for p in HOME.glob("*.py")]
    r = subprocess.run([py, "-c", "import sys,py_compile\nfor f in sys.argv[1:]: py_compile.compile(f, doraise=True, cfile=None)", *pys],
                       cwd=str(HOME), env=env, capture_output=True, text=True, timeout=300)
    if r.returncode:
        raise UpdateError("compile check failed: " + (r.stderr.strip().splitlines() or ["?"])[-1][:300])
    dry = ("import os,degen_home as D,solana_rpc as S,pause_ctl,setup_state\n"
           "assert D.paper_trading(), 'paper mode not on'\n"
           "r=S.rpc_send('sendTransaction',['dry-check'],'helius')\n"
           "assert isinstance(r,dict) and r.get('error'), 'send was not refused'\n"
           "print('dry-check ok', r['error'].get('code'))\n")
    t = HOME / "tests" / "test_keyless.py"
    if t.exists() and not stage_fail:  # v1.0.0: offline fallback/jitter tests (mocked network)
        r = subprocess.run([py, str(t)], cwd=str(HOME), env=env, capture_output=True, text=True, timeout=180)
        if r.returncode:
            raise UpdateError("offline tests failed: " + ((r.stderr or r.stdout).strip().splitlines() or ["?"])[-1][:300])
    if stage_fail or os.environ.get("UPDATE_ENGINE_FORCE_FAIL") == "1":
        dry = "raise SystemExit('forced failure (rollback test)')"
    r = subprocess.run([py, "-c", dry], cwd=str(HOME), env=env, capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise UpdateError("paper dry check failed: " + ((r.stderr or r.stdout).strip().splitlines() or ["?"])[-1][:300])


def _clean_pycache():
    for p in HOME.rglob("__pycache__"):
        if ".venv" not in p.parts:
            shutil.rmtree(p, ignore_errors=True)


def apply(force_fail: bool = False, engine_file: str | None = None, accept: bool = False) -> dict:
    inst = installed_version()
    m = remote_manifest()
    if _ver(m["version"]) <= _ver(inst.get("version", "0.0.0")) and not engine_file:
        return {"status": "up_to_date", "installed": inst.get("version"), "remote": m["version"]}
    eng_url = m.get("engine_url") or urls()[0]
    blob = Path(engine_file).read_bytes() if engine_file else _fetch(eng_url, 300)
    verify_engine(blob, m)
    raw_files = parse_engine(blob)
    sec = security_check(raw_files, HOME)
    if sec["status"] != "clean" and not accept:
        return _hold({"installed": inst.get("version"), "attempted": m["version"]}, sec)
    files = plan(raw_files)
    new_ver = json.loads(files.get("ENGINE_VERSION.json", b"{}") or b"{}").get("version")
    if new_ver != m["version"]:
        raise UpdateError(f"engine says version {new_ver}, manifest says {m['version']}; nothing installed")

    pause = HOME / "TRADING_PAUSED"
    was_paused = pause.exists()
    if not was_paused:
        pause.write_text(datetime.now().astimezone().isoformat(timespec="seconds") + " paused by update_engine\n", encoding="utf-8")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = HOME / "engine-backups" / f"{inst.get('version', '0.0.0')}-{ts}"
    n = 1
    while bdir.exists():
        n += 1
        bdir = HOME / "engine-backups" / f"{inst.get('version', '0.0.0')}-{ts}-{n}"
    before = _owner_hashes()
    written, created = [], []
    try:
        bdir.mkdir(parents=True)
        for dest in files:
            p = HOME / dest
            if p.exists():
                (bdir / dest).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, bdir / dest)
                written.append(dest)
            else:
                created.append(dest)
        for dest, raw in files.items():
            p = HOME / dest
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".upd-tmp")
            tmp.write_bytes(raw)
            os.replace(tmp, p)
            if dest.endswith(".sh"):
                os.chmod(p, 0o755)
        _checks(force_fail)
        if _owner_hashes() != before:
            raise UpdateError("owner files changed during update")
        _clean_pycache()
    except Exception as e:
        for dest in written:
            shutil.copy2(bdir / dest, HOME / dest)
        for dest in created:
            try:
                (HOME / dest).unlink()
            except FileNotFoundError:
                pass
        _clean_pycache()
        ok_restored = _owner_hashes() == before and installed_version().get("version") == inst.get("version")
        if not was_paused:
            pause.unlink(missing_ok=True)
        return {"status": "rolled_back", "installed": inst.get("version"), "attempted": m["version"],
                "error": str(e), "restored_ok": ok_restored, "backup": str(bdir),
                "trading": "paused (was paused before)" if was_paused else "resumed"}
    if not was_paused:
        pause.unlink(missing_ok=True)
    return {"status": "updated", "from": inst.get("version"), "to": m["version"], "files": len(files),
            "owner_files_unchanged": len(before), "backup": str(bdir),
            "trading": "paused (was paused before)" if was_paused else "resumed", "notes": m.get("notes", ""),
            "security": sec["plain"]}


def scan(engine_file: str | None = None) -> dict:
    """Download + verify + security check only; installs nothing. Used before asking the owner to update."""
    inst = installed_version()
    m = remote_manifest()
    blob = Path(engine_file).read_bytes() if engine_file else _fetch(m.get("engine_url") or urls()[0], 300)
    verify_engine(blob, m)
    sec = security_check(parse_engine(blob), HOME)
    return {"status": "clean" if sec["status"] == "clean" else "suspicious", "installed": inst.get("version"),
            "remote": m["version"], "security": sec["plain"], "finding_count": sec["finding_count"],
            "findings": sec["findings"][:20], "changes": sec.get("changes")}


def install(dest: str, engine_file: str | None, accept: bool = False) -> dict:
    """Fresh install for setup Step 1: verify against the manifest, security check, extract code/ into dest."""
    global HOME
    m = remote_manifest()
    blob = Path(engine_file).read_bytes() if engine_file else _fetch(m.get("engine_url") or urls()[0], 300)
    verify_engine(blob, m)
    raw_files = parse_engine(blob)
    d0 = Path(dest)
    sec = security_check(raw_files, d0 if (d0 / "ENGINE_VERSION.json").exists() else None)
    if sec["status"] != "clean" and not accept:
        return _hold({"version": m["version"], "dest": dest}, sec)
    files = plan(raw_files)
    HOME = Path(dest)
    for d, raw in files.items():
        p = HOME / d
        if p.exists() and _protected(d) and not d.startswith("seeds/"):
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(raw)
        if d.endswith(".sh"):
            os.chmod(p, 0o755)
    return {"status": "installed", "version": m["version"], "files": len(files), "dest": str(HOME), "security": sec["plain"]}


def main(argv):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "apply", "install", "scan"])
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--dest")
    ap.add_argument("--engine-file")
    ap.add_argument("--accept-security-findings", action="store_true", help="owner said install anyway")
    ap.add_argument("--force-fail-test", action="store_true", help="rollback test: fail the post-install check")
    a = ap.parse_args(argv[1:])
    try:
        if a.cmd == "check":
            res = check()
        elif a.cmd == "apply":
            res = apply(a.force_fail_test, a.engine_file, a.accept_security_findings)
        elif a.cmd == "scan":
            res = scan(a.engine_file)
        else:
            res = install(a.dest or str(HOME), a.engine_file, a.accept_security_findings)
    except UpdateError as e:
        res = {"status": "error", "error": str(e), "note": "nothing was changed"}
    print(json.dumps(res, indent=1))
    return 0 if res.get("status") in ("up_to_date", "update_available", "updated", "installed", "clean") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
