#!/usr/bin/env python3
"""Kill switch (template). Owner says "pause trading" / "resume trading"; the agent runs this on the ACTIVE side.
  python pause_ctl.py pause|resume|status
pause: writes TRADING_PAUSED (every send is refused at once; the runner skips checks) and republishes mode.json
with paused=true so the other side refuses too. resume: removes it and republishes. Never touches positions."""
import subprocess
import sys
from datetime import datetime

import degen_home as _DH


def _publish():
    try:
        subprocess.run([sys.executable, str(_DH.HOME / "dashboard_publish.py"), "--force"], cwd=str(_DH.HOME),
                       timeout=180, capture_output=True)
    except Exception:
        pass


def main(argv):
    cmd = (argv[1] if len(argv) > 1 else "status").lower()
    if cmd == "pause":
        _DH.PAUSE_FILE.write_text(datetime.now().astimezone().isoformat(timespec="seconds") + " paused by owner\n", encoding="utf-8")
        _publish(); print("PAUSED")
    elif cmd == "resume":
        try:
            _DH.PAUSE_FILE.unlink()
        except FileNotFoundError:
            pass
        _publish(); print("RESUMED" if not _DH.trading_paused() else "STILL_PAUSED (shared flag not cleared yet)")
    else:
        print("PAUSED" if _DH.trading_paused() else "RUNNING")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
