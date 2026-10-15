#!/usr/bin/env python3
"""Interest mode (template v1.1.0): ONE switch for both earning sides, like savings / memecoin mode.

  * USDC: half of the bot's USDC sits in Jupiter Lend (Earn) as jlUSDC and earns lending interest
    (degen_lend.py).
  * SOL: half of the bot's trading SOL sits as JitoSOL in a separate bucket and earns staking
    rewards (sol_stake.py). Never mixed with savings-mode JitoSOL.
  * Whenever a trade needs more than is liquid, the bot pulls money back first (withdraw /
    swap back), then trades. Both count as plain USDC / SOL everywhere.

Off by default. The flag lives in cost-basis.json -> interest_mode.enabled (set at setup by
apply_features.py --interest on|off). Turning it off withdraws the USDC and swaps the JitoSOL
bucket back to SOL on the next check.

Watch the JitoSOL side: every swap into or out of the bucket costs a little (spread and fees);
JitoSOL's rising value should outweigh that over time. `status` shows the running tally.

  python interest_mode.py status | on | off
"""
from __future__ import annotations

import json
import sys

import degen_home as _DH

CB = _DH.HOME / "cost-basis.json"


def flag():
    """True/False from cost-basis.json interest_mode.enabled; None when not set up (module configs decide)."""
    try:
        im = json.loads(CB.read_text(encoding="utf-8")).get("interest_mode")
    except Exception:
        return None
    if not isinstance(im, dict) or "enabled" not in im:
        return None
    return bool(im.get("enabled"))


def set_flag(v: bool) -> None:
    cb = json.loads(CB.read_text(encoding="utf-8"))
    cb.setdefault("interest_mode", {})["enabled"] = bool(v)
    tmp = CB.with_name(CB.name + ".tmp")
    tmp.write_text(json.dumps(cb, indent=2), encoding="utf-8")
    tmp.replace(CB)


def status() -> dict:
    out = {"interest_mode": bool(flag())}
    try:
        import degen_lend
        out["usdc_lend"] = degen_lend.status()
    except Exception as e:
        out["usdc_lend"] = {"error": type(e).__name__}
    try:
        import sol_stake
        out["sol_stake"] = sol_stake.status()
        out["jitosol_tracker"] = out["sol_stake"].get("tracker")
    except Exception as e:
        out["sol_stake"] = {"error": type(e).__name__}
    return out


def main(argv) -> int:
    cmd = (argv[1:] or ["status"])[0]
    if cmd in ("on", "off"):
        import mirror_guard as _MG
        _MG.check("interest_mode " + cmd)
        set_flag(cmd == "on")
        print(json.dumps({"interest_mode": cmd == "on",
                          "note": "applies on the next check" + ("" if cmd == "on" else "; USDC is withdrawn and the JitoSOL bucket swapped back to SOL")}))
        return 0
    if cmd == "status":
        print(json.dumps(status(), indent=1, default=str))
        return 0
    print("usage: interest_mode.py status|on|off")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
