"""mirrorguard1015: box mirror safety lock.

If MIRROR_ONLY exists in the bot folder and fallback-active.json does not, an
entry script that can act on-chain must stop before any network, state or
on-chain work. The fallback takeover writes fallback-active.json first, so the
real fallback path keeps working. No-op on the PC (no MIRROR_ONLY file).
Imports only the standard library and degen_home (no network).
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import degen_home as _DH

ACTIVE_NAME = "fallback-active.json"


def blocked(home: Path | None = None) -> bool:
    h = Path(home or _DH.HOME)
    return (h / "MIRROR_ONLY").exists() and not (h / ACTIVE_NAME).exists()


def line(name: str) -> str:
    return (f"{datetime.now().astimezone().isoformat(timespec='seconds')} MIRROR_GUARD_REFUSED {name}: "
            f"MIRROR_ONLY present and no {ACTIVE_NAME}; this box copy must not act (live bot runs on the PC)")


def check(name: str, home: Path | None = None) -> None:
    """Exit(3) immediately when blocked; otherwise return."""
    if blocked(home):
        print(line(name), flush=True)
        try:
            p = Path(home or _DH.HOME) / "runner-logs" / "mirror-guard.log"
            p.parent.mkdir(exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(line(name) + "\n")
        except Exception:
            pass
        raise SystemExit(3)
