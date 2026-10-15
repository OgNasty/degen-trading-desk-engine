#!/usr/bin/env bash
# Silent, non-blocking, idempotent boot/shell revive for the trading runner.
# Safe if the folder is missing. Never prints secrets. Does not run a trade check.
# Intended for shell profile / profile.d sourcing (backgrounded by the caller).
#
# Respects a deliberate stop: "runner_ctl.sh stop" writes runner.stopped and
# this hook then does nothing until "runner_ctl.sh start" clears it. A runner
# that was never deliberately stopped is revived on boot/login as before.
set -u

ROOT="${DEGEN_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CTL="$ROOT/box/runner_ctl.sh"
STOP_MARKER="$ROOT/runner.stopped"

# Fast no-op if tree or ctl missing (e.g. mid-hydrate)
# Mirror guard: a MIRROR_ONLY file means this copy must never trade (Phase 2 = PC is live).
if [[ -e "$ROOT/MIRROR_ONLY" ]]; then
  exit 0
fi
if [[ ! -x "$CTL" ]]; then
  exit 0
fi

# Deliberately stopped: do not revive.
if [[ -e "$STOP_MARKER" ]]; then
  exit 0
fi

# Avoid re-entry storms within the same shell session
if [[ -n "${DEGEN_RUNNER_AUTOSTART_DONE:-}" ]]; then
  exit 0
fi
export DEGEN_RUNNER_AUTOSTART_DONE=1

# "autostart" is idempotent (adopts a live runner, never duplicates) and
# re-checks the stop marker under the ctl lock. Detach so profiles never block.
(
  export PATH="${PATH:-/usr/bin:/bin}:/usr/local/bin"
  mkdir -p "$ROOT/runner-logs" 2>/dev/null || true
  "$CTL" autostart >>"$ROOT/runner-logs/runner-ctl.log" 2>&1 || true
  # Dashboard static server (localhost:8765); idempotent
  if [[ -x "$ROOT/dashboard_serve.sh" ]]; then
    "$ROOT/dashboard_serve.sh" start >>"$ROOT/runner-logs/dashboard-server.log" 2>&1 || true
  fi
  # Cloudflare tunnel DISABLED 2026-10-08: box egress blocks :7844; the site is
  # now served by the Worker `degen-dashboard` (dashboard_publish.py after each check).
  # dashboard_tunnel.sh is kept on disk; re-enable here only if :7844 egress returns.
) >/dev/null 2>&1 &
disown 2>/dev/null || true
exit 0
