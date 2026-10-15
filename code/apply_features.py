#!/usr/bin/env python3
"""Copy config.json "features" (savings_mode / memecoin_mode, chosen once at setup) into cost-basis.json.
Template 2026-10-13. Both default off. Memecoin is hard-defaulted to Greedy Goats; the owner may change the coin
later with the --meme-* flags.
Usage: python apply_features.py [--savings on|off] [--memecoin on|off] [--meme-mint MINT --meme-symbol SYM
       --meme-decimals 6 --meme-token-program PROGRAM]   (flags also update config.json)"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime

import degen_home as _DH

GREEDY_GOATS = {"symbol": "GreedyGoats", "name": "Greedy Goats", "mint": "GGiveYrqhfJc5pgBnjuY3NP5cW3ni1nFHeJXYZGFappF", "decimals": 6, "token_program": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--savings", choices=["on", "off"])
    ap.add_argument("--memecoin", choices=["on", "off"])
    ap.add_argument("--meme-mint"); ap.add_argument("--meme-symbol"); ap.add_argument("--meme-name")
    ap.add_argument("--meme-decimals", type=int); ap.add_argument("--meme-token-program")
    a = ap.parse_args(argv)
    cfg = _DH.config()
    f = cfg.setdefault("features", {"savings_mode": False, "memecoin_mode": False, "memecoin": {}})
    m = f.setdefault("memecoin", {})
    for k, v in GREEDY_GOATS.items():  # default coin, no question
        if m.get(k) in (None, ""):
            m[k] = v
    if a.savings: f["savings_mode"] = a.savings == "on"
    if a.memecoin: f["memecoin_mode"] = a.memecoin == "on"
    for k, v in (("mint", a.meme_mint), ("symbol", a.meme_symbol), ("name", a.meme_name),
                 ("decimals", a.meme_decimals), ("token_program", a.meme_token_program)):
        if v is not None:
            m[k] = v
    if any(x is not None for x in vars(a).values()):
        _DH.CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    cbp = _DH.HOME / "cost-basis.json"
    cb = json.loads(cbp.read_text(encoding="utf-8"))
    cb.setdefault("savings_mode", {})["enabled"] = bool(f.get("savings_mode"))
    mm = cb.setdefault("memecoin_mode", {})
    want_meme = bool(f.get("memecoin_mode"))
    if want_meme and not m.get("mint"):
        print("memecoin_mode left OFF: no features.memecoin.mint set yet")
        want_meme = False
    if want_meme:
        mm.update({k: m[k] for k in ("mint", "symbol", "decimals", "token_program") if m.get(k) not in (None, "")})
        mm["name"] = m.get("name") or m.get("symbol") or ""
    mm["enabled"] = want_meme
    shutil.copy2(cbp, cbp.with_name(f"cost-basis.json.bak-features-{datetime.now():%Y%m%d-%H%M%S}"))
    cbp.write_text(json.dumps(cb, indent=2), encoding="utf-8")
    print(json.dumps({"savings_mode": cb["savings_mode"]["enabled"], "memecoin_mode": mm["enabled"], "memecoin": mm.get("symbol", "")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
