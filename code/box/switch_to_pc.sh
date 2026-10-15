#!/usr/bin/env bash
# Phase 2 switch, box side (template). Run by the agent; no owner question.
# Order: set PC mode -> publish mode.json (shared flag) -> stop hosted runner -> remove box cron
#        -> MIRROR_ONLY -> verify stopped. Only after "HOSTED_STOPPED_OK" may the PC tasks be enabled.
# The agent must ALSO pause/delete any Grok Bot routine that runs this bot (not reachable from here).
set -uo pipefail
ROOT="${DEGEN_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT" || exit 1
PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY=python3

# 1. Local mode flag -> pc (box runner exits at its next cycle; box sends are refused).
"$PY" - <<'PYEOF'
import json, pathlib
p = pathlib.Path("config.json"); c = json.loads(p.read_text()) if p.exists() else {}
c["run_side"] = "pc"; p.write_text(json.dumps(c, indent=2)); print("config run_side=pc")
PYEOF

# 2. Shared flag on the owner's Worker: mode.json {"run_side":"pc"}; also clears Worker cron triggers.
DEGEN_RUN_SIDE=pc "$PY" dashboard_publish.py --force >/dev/null 2>&1 || "$PY" dashboard_publish.py >/dev/null 2>&1 \
  || echo "WARN dashboard publish failed; mode.json not updated (local flags still block the box)"

# 3. Stop the hosted runner and keep it stopped.
"$ROOT/box/runner_ctl.sh" stop || true
touch "$ROOT/MIRROR_ONLY" "$ROOT/runner.stopped"

# 4. Remove this bot's lines from the box crontab (other entries kept).
if command -v crontab >/dev/null 2>&1; then
  # Drop lines naming this folder literally, our own DEGEN_HOME=<this folder> line, and every
  # $DEGEN_HOME job while that variable points here. Other bots' lines and variables are kept.
  crontab -l 2>/dev/null | awk -v root="$ROOT" '
    /^[[:space:]]*DEGEN_HOME=/ { v=$0; sub(/^[[:space:]]*DEGEN_HOME=/, "", v); gsub(/["'"'"']/, "", v); ours=(v==root); if (ours) next; print; next }
    index($0, root) { next }
    ours && index($0, "$DEGEN_HOME") { next }
    { print }' | crontab - 2>/dev/null || true
fi

# 5. Verify.
sleep 2
if pgrep -f "$ROOT/box_runner.py" >/dev/null 2>&1; then
  echo "HOSTED_STOP_FAILED runner still alive"; exit 2
fi
if crontab -l 2>/dev/null | grep -q -F -e "$ROOT" -e "DEGEN_HOME=$ROOT"; then
  echo "HOSTED_STOP_FAILED cron entries remain"; exit 2
fi
echo "HOSTED_STOPPED_OK (now pause/delete the Grok Bot routine, then enable PC tasks)"
