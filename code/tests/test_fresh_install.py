#!/usr/bin/env python3
"""Fresh-install basics: day counter starts at this install, time zone comes from config. Offline."""
import os, sys, tempfile, json
from datetime import date
from pathlib import Path
TMP = tempfile.mkdtemp(prefix="degen-test-")
os.environ["DEGEN_HOME"] = TMP
os.environ.setdefault("PAPER_TRADING", "true")
Path(TMP, ".env").write_text("HELIUS_RPC_URL=https://rpc.invalid/test\nPAPER_TRADING=true\n")
Path(TMP, "config.json").write_text(json.dumps({"timezone": "America/Chicago"}))
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import degen_home as DH


def test_timezone_from_config():
    assert DH.tz_name() == "America/Chicago"
    Path(TMP, "config.json").write_text(json.dumps({"timezone": "YOUR_TIME_ZONE"}))
    assert DH.tz_name() != "YOUR_TIME_ZONE"          # placeholder -> machine zone or UTC
    Path(TMP, "config.json").write_text(json.dumps({"timezone": "America/Chicago"}))


def test_day_counter_starts_at_install():
    import morning_balance_run as M
    days = {}
    assert M.day_n(date(2026, 11, 2), days) == 1 and days["day1_date"] == "2026-11-02"
    assert M.day_n(date(2026, 11, 4), days) == 3


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"): v(); print("ok", k)
