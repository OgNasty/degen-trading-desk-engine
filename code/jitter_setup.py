#!/usr/bin/env python3
"""Per-install timing jitter (template v1.0.0). Not a secret.
  python jitter_setup.py            -> create config.json "jitter" once (kept if present), print the schedule
  python jitter_setup.py --show     -> print the schedule only (JSON)
  python jitter_setup.py --crontab  -> print box crontab lines with the jittered times
Owner times stay approximately the same: daily jobs move by an even number of minutes (-6..+6, never 0,
so odd minutes stay odd); the edge loop runs at a fixed offset inside its interval."""
import json
import secrets
import sys

import degen_home as _DH

BASE = {"morning_am": "06:59", "morning_pm": "16:59", "friday_rsi": "21:01", "fear_greed": "21:00", "update_check": "10:17"}


def ensure():
    p = _DH.HOME / "config.json"
    c = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(c.get("jitter"), dict) or "daily_offset_min" not in c["jitter"]:
        r = secrets.SystemRandom()
        c["jitter"] = {"edge_offset_frac": round(r.uniform(0.05, 0.95), 4),
                       "daily_offset_min": r.choice([-6, -4, -2, 2, 4, 6]),
                       "_readme": "per-install timing offset (template); not a secret; keep stable"}
        p.write_text(json.dumps(c, indent=2) + "\n", encoding="utf-8")
    return c["jitter"]


def schedule():
    out = {k: _DH.jittered_hm(v) for k, v in BASE.items()}
    out["edge_offset_sec_box"] = _DH.edge_offset_sec(300)
    out["edge_offset_sec_pc"] = _DH.edge_offset_sec(60)
    out["daily_offset_min"] = _DH.daily_offset_min()
    return out


def crontab():
    s = schedule()
    am, pm, fr = (s[k].split(":") for k in ("morning_am", "morning_pm", "friday_rsi"))
    run = "cd $DEGEN_HOME && [ ! -e MIRROR_ONLY ] && .venv/bin/python"
    return "\n".join([
        f"{int(am[1])} {int(am[0])} * * *   {run} morning_balance_run.py >>runner-logs/morning_balance_run.log 2>&1",
        f"{int(pm[1])} {int(pm[0])} * * *   {run} morning_balance_run.py >>runner-logs/morning_balance_run.log 2>&1",
        f"{int(fr[1])} {int(fr[0])} * * 5   {run} friday_rsi.py >>runner-logs/friday_rsi.log 2>&1",
    ])


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--crontab" in a:
        print(crontab())
    else:
        if "--show" not in a:
            ensure()
        print(json.dumps(schedule(), indent=1))
