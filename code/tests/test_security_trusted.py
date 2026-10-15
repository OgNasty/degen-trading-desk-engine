#!/usr/bin/env python3
"""The template's own sites, tokens and program addresses are trusted (no false warnings);
unknown ones still get flagged. Offline. python tests/test_security_trusted.py"""
import sys, re
from pathlib import Path
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent          # template root (SETUP.md + code/)
sys.path.insert(0, str(HERE.parent))
import security_scan as S


def _tree():
    out = {}
    for p in ROOT.rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts and ".bak" not in p.name and p.name != "bot-picture.png":
            out[p.relative_to(ROOT).as_posix()] = p.read_bytes()
    return out


def test_own_hosts_and_addresses_are_trusted():
    for rel, raw in _tree().items():
        if not rel.endswith(S.SCAN_EXT):
            continue
        t = raw.decode("utf-8", "ignore")
        for h in set(S.URL_RX.findall(t)) | set(S.HOST_RX.findall(t)):
            h = h.lower().rstrip(".")
            assert "{" in h or "." not in h or S._trusted_domain(h), (rel, h)
        for a in set(S.ADDR_RX.findall(t)):
            if re.search(r"\d", a) and re.search(r"[A-Z]", a) and re.search(r"[a-z]", a):
                assert a in S.TRUSTED_ADDRESSES, (rel, a)


def test_tree_scans_clean():
    r = S.scan_files(_tree())
    assert r["status"] == "clean", r["findings"][:5]


def test_unknown_things_are_still_flagged():
    files = _tree()
    bad_host = "steal-keys" + "." + "example-bad" + "." + "ru"  # split so this file itself scans clean
    files["code/evil.py"] = ('import urllib.request\nurllib.request.urlopen("https://' + bad_host + '/x")\n').encode()
    r = S.scan_files(files)
    kinds = {f["kind"] for f in r["findings"]}
    assert r["status"] == "suspicious" and "network" in kinds, r


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"): v(); print("ok", k)
