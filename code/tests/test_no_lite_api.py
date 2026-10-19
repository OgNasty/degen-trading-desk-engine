#!/usr/bin/env python3
"""No engine file may point at Jupiter's retired lite-api host (shut down 2026-10-30); use api.jup.ag.
Checks the shipped code, docs and examples only (owner data, logs and backups are skipped).
Offline. python tests/test_no_lite_api.py"""
import os, sys, tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent          # template root (SETUP.md + code/)
OLD_HOST = "lite-api" + ".jup.ag"  # split so this file itself doesn't match
EXT = (".py", ".md", ".js", ".html", ".sh", ".ps1", ".toml", ".example", ".txt")
SKIP = {".venv", "engine-backups", "backups", "__pycache__", "logs", "state", "data", "node_modules"}


def _engine_files():
    for p in ROOT.rglob("*"):
        rel = p.relative_to(ROOT)
        if not p.is_file() or SKIP & set(rel.parts) or ".bak" in p.name:
            continue
        if p.suffix in EXT or p.name.endswith(".example.json") or p.name in ("ENGINE_VERSION.json", "security-baseline.json"):
            yield rel.as_posix(), p


def test_no_file_points_at_lite_api():
    hits = [rel for rel, p in _engine_files() if OLD_HOST.encode() in p.read_bytes().lower()]
    assert not hits, "old Jupiter lite-api host still used in: " + ", ".join(sorted(hits))


def test_lend_default_uses_api_jup_ag():
    os.environ.setdefault("DEGEN_HOME", tempfile.mkdtemp(prefix="degen-test-"))
    os.environ.setdefault("PAPER_TRADING", "true")
    sys.path.insert(0, str(HERE.parent))
    import degen_lend as L
    assert L.DEFAULT_CFG["api_base"].startswith("https://api.jup.ag/lend/v1/earn"), L.DEFAULT_CFG["api_base"]


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"): v(); print("ok", k)
