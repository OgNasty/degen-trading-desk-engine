#!/usr/bin/env python3
"""Saved setup progress (template). setup-state.json in DEGEN_HOME holds completed steps, choices and NON-SECRET
ids only (never keys). "continue setup" -> `python setup_state.py next` says exactly where to resume.
  python setup_state.py show | next | done <step> | set <key> <value> | reset
Steps are fixed (SETUP.md): 1 welcome, 2 accounts_keys, 3 wallet_options, 4 dashboard_paper, 5 funding, 6 finish."""
import json
import sys
from datetime import datetime

import degen_home as _DH

STEPS = ["welcome", "accounts_keys", "wallet_options", "dashboard_paper", "funding", "finish"]
FILE = _DH.HOME / "setup-state.json"
FORBIDDEN = ("key", "token", "secret", "private", "password", "url")  # values that could carry secrets


def load():
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "done": [], "choices": {}, "ids": {}}


def save(st):
    st["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
    FILE.write_text(json.dumps(st, indent=2), encoding="utf-8")


def nxt(st):
    for i, s in enumerate(STEPS, 1):
        if s not in st["done"]:
            return i, s
    return None, None


def main(a):
    st = load(); cmd = a[1] if len(a) > 1 else "show"
    if cmd == "done":
        if a[2] not in STEPS: raise SystemExit("unknown step")
        if a[2] not in st["done"]: st["done"].append(a[2])
        save(st)
    elif cmd == "set":
        k, v = a[2], " ".join(a[3:])
        if any(f in k.lower() for f in FORBIDDEN): raise SystemExit("refused: secrets never go in setup-state.json")
        (st["ids"] if k.endswith("_id") or k in ("wallet_address", "dashboard_url", "worker_name", "checkin_routine") else st["choices"])[k] = v
        save(st)
    elif cmd == "reset":
        save({"version": 1, "done": [], "choices": {}, "ids": {}})
    i, s = nxt(load())
    if cmd in ("next", "show", "done", "set", "reset"):
        print(json.dumps({"next_step": s, "progress": f"Step {i} of {len(STEPS)}" if i else "Setup complete", **load()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
