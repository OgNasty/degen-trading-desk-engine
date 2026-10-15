#!/usr/bin/env bash
# Start/stop/status for the box-local wallet edge-check runner.
# Usage: ./runner_ctl.sh start|stop|status|autostart|install-reboot
# Does not post webhooks. Does not run a one-shot live check.
#
# Race-safe (2026-10-05):
#  - All commands serialize on runner-ctl.lock (flock).
#  - A live runner is any process whose argv is "<python> $RUNNER" (pidfile pid
#    first, else a /proc scan). start adopts it instead of starting a duplicate.
#  - runner.pid is written only after the new process is confirmed alive.
#  - A failed start never deletes a pidfile that points at a live runner.
#  - status detects a live runner with a missing/stale pidfile and repairs it.
#  - stop writes runner.stopped (stop marker) first, then stops every runner
#    process for this folder. The profile autostart hook calls "autostart",
#    which does nothing while the marker exists. "start" clears the marker.
#    A never-stopped runner is still revived on boot/login by the hook.
set -euo pipefail

ROOT="${DEGEN_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
RUNNER="$ROOT/box_runner.py"
PIDFILE="$ROOT/runner.pid"
STOP_MARKER="$ROOT/runner.stopped"
CTL_LOCK="$ROOT/runner-ctl.lock"
LOGDIR="$ROOT/runner-logs"
CTLLOG="$LOGDIR/runner-ctl.log"
PYTHON="$ROOT/.venv/bin/python"
START_CONFIRM_SEC="${START_CONFIRM_SEC:-2}"
STOP_GRACE_SEC="${STOP_GRACE_SEC:-10}"

mkdir -p "$LOGDIR"

ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }

log() {
  printf '%s %s\n' "$(ts)" "$*" >>"$CTLLOG"
}

# Serialize ctl commands. fd 9 is closed for the runner child (9>&-).
take_lock() {
  local wait="${1:-30}"
  exec 9>>"$CTL_LOCK"
  if ! flock -w "$wait" 9; then
    echo "runner_ctl busy (lock wait ${wait}s timed out)" >&2
    return 1
  fi
}

# True when pid is alive and its argv is exactly "<python...> $RUNNER [args]".
is_runner_pid() {
  local pid="${1:-}"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  local -a argv=()
  mapfile -d '' -t argv <"/proc/$pid/cmdline" 2>/dev/null || return 1
  (( ${#argv[@]} >= 2 )) || return 1
  [[ "$(basename -- "${argv[0]}")" == python* ]] || return 1
  [[ "${argv[1]}" == "$RUNNER" ]] || return 1
  return 0
}

# All live runner pids for this folder (one per line, ascending).
find_runner_pids() {
  local p
  for p in $(pgrep -f -- "$RUNNER" 2>/dev/null || true); do
    if is_runner_pid "$p"; then
      echo "$p"
    fi
  done
}

# The runner that holds runner.lock, if its pid is recorded there.
lock_holder_pid() {
  local line pid
  line="$(head -n1 "$ROOT/runner.lock" 2>/dev/null || true)"
  pid="${line#pid=}"
  pid="${pid%% *}"
  if is_runner_pid "$pid"; then echo "$pid"; fi
}

# Echo the live runner pid (pidfile first, then lock holder, then scan).
live_runner_pid() {
  local pid=""
  if [[ -f "$PIDFILE" ]]; then
    pid="$(tr -dc '0-9' <"$PIDFILE" 2>/dev/null || true)"
    if is_runner_pid "$pid"; then echo "$pid"; return 0; fi
  fi
  pid="$(lock_holder_pid)"
  if [[ -n "$pid" ]]; then echo "$pid"; return 0; fi
  pid="$(find_runner_pids | head -n1)"
  if [[ -n "$pid" ]]; then echo "$pid"; return 0; fi
  return 1
}

write_pidfile() {
  local tmp="$PIDFILE.tmp.$$"
  echo "$1" >"$tmp"
  mv -f "$tmp" "$PIDFILE"
}

# Remove the pidfile only when it does not point at a live runner.
clear_stale_pidfile() {
  if [[ -f "$PIDFILE" ]]; then
    local pid
    pid="$(tr -dc '0-9' <"$PIDFILE" 2>/dev/null || true)"
    if ! is_runner_pid "$pid"; then
      rm -f "$PIDFILE"
    fi
  fi
}

print_heartbeat() {
  if [[ -f "$ROOT/runner-heartbeat.json" ]]; then
    DEGEN_HOME="$ROOT" "$PYTHON" - <<'PY' || true
import json
from pathlib import Path
p = Path(__import__("os").environ.get("DEGEN_HOME", ".")) / "runner-heartbeat.json"
try:
    d = json.loads(p.read_text())
except Exception as e:
    print("heartbeat: unreadable", type(e).__name__)
else:
    for k in ("last_start", "last_end", "last_result", "next_at_pt", "updated_pt", "consecutive_fails"):
        if k in d:
            print(f"  {k}={d[k]}")
PY
  fi
}

cmd_status() {
  # Repair needs the lock; if busy, report read-only.
  local locked=1
  take_lock 5 2>/dev/null || locked=0
  local pid
  if pid="$(live_runner_pid)"; then
    local cur=""
    [[ -f "$PIDFILE" ]] && cur="$(tr -dc '0-9' <"$PIDFILE" 2>/dev/null || true)"
    if [[ "$cur" != "$pid" ]] && (( locked )); then
      write_pidfile "$pid"
      log "status repaired pidfile (was '${cur:-missing}') -> $pid"
      echo "running pid=$pid (pidfile repaired; was ${cur:-missing})"
    else
      echo "running pid=$pid"
    fi
    local n
    n="$(find_runner_pids | wc -l | tr -d ' ')"
    if (( n > 1 )); then
      echo "  WARNING: $n runner processes: $(find_runner_pids | tr '\n' ' ')"
    fi
    print_heartbeat
    return 0
  fi
  (( locked )) && clear_stale_pidfile
  if [[ -f "$STOP_MARKER" ]]; then
    echo "stopped (deliberate stop; autostart suppressed: $(head -n1 "$STOP_MARKER" 2>/dev/null))"
  else
    echo "stopped"
  fi
  return 0
}

# Start (or adopt). $1 = "manual" clears the stop marker; "auto" respects it.
do_start() {
  local mode="$1"
  take_lock 30
  if [[ "$mode" == "auto" && -f "$STOP_MARKER" ]]; then
    echo "autostart skipped: deliberately stopped ($STOP_MARKER present)"
    return 0
  fi
  if [[ "$mode" == "manual" && -f "$STOP_MARKER" ]]; then
    rm -f "$STOP_MARKER"
    log "stop marker cleared by start"
  fi
  local pid
  if pid="$(live_runner_pid)"; then
    local cur=""
    [[ -f "$PIDFILE" ]] && cur="$(tr -dc '0-9' <"$PIDFILE" 2>/dev/null || true)"
    if [[ "$cur" != "$pid" ]]; then
      write_pidfile "$pid"
      log "start ($mode): adopted live runner pid=$pid (pidfile was '${cur:-missing}')"
    elif [[ "$mode" == "manual" ]]; then
      log "start (manual): already running pid=$pid"
    fi
    echo "already running pid=$pid"
    return 0
  fi
  if [[ ! -x "$PYTHON" ]]; then
    echo "missing venv python: $PYTHON" >&2
    return 1
  fi
  if [[ ! -f "$RUNNER" ]]; then
    echo "missing runner: $RUNNER" >&2
    return 1
  fi
  log "start requested ($mode)"
  # Detached session; the ctl lock fd is not inherited.
  setsid nohup "$PYTHON" "$RUNNER" >>"$LOGDIR/runner-daemon.log" 2>&1 9>&- </dev/null &
  local launched=$!
  sleep "$START_CONFIRM_SEC"
  local newpid=""
  if is_runner_pid "$launched"; then
    newpid="$launched"
  else
    newpid="$(live_runner_pid || true)"
  fi
  if [[ -n "$newpid" ]]; then
    write_pidfile "$newpid"
    log "started pid=$newpid ($mode)"
    echo "started pid=$newpid"
    return 0
  fi
  log "start failed launched=$launched ($mode)"
  echo "start failed (process exited); see $LOGDIR/runner-daemon.log" >&2
  clear_stale_pidfile
  return 1
}

cmd_start() { do_start manual; }
cmd_autostart() { do_start auto; }

cmd_stop() {
  take_lock 30
  # Marker first so a racing profile hook cannot revive the runner.
  printf 'stopped_pt=%s by=runner_ctl_stop\n' "$(ts)" >"$STOP_MARKER"
  local -a pids=()
  mapfile -t pids < <(find_runner_pids)
  if (( ${#pids[@]} == 0 )); then
    rm -f "$PIDFILE"
    log "stop: none running (marker set)"
    echo "stopped"
    return 0
  fi
  log "stop requested pids=${pids[*]}"
  kill -TERM "${pids[@]}" 2>/dev/null || true
  local i alive
  for (( i = 0; i < STOP_GRACE_SEC * 2; i++ )); do
    alive=0
    for p in "${pids[@]}"; do
      if kill -0 "$p" 2>/dev/null; then alive=1; fi
    done
    (( alive )) || break
    sleep 0.5
  done
  for p in "${pids[@]}"; do
    if kill -0 "$p" 2>/dev/null; then
      log "stop escalate KILL pid=$p"
      kill -KILL "$p" 2>/dev/null || true
    fi
  done
  # Any runner that appeared during the stop window (should be none).
  mapfile -t pids < <(find_runner_pids)
  if (( ${#pids[@]} > 0 )); then
    log "stop: late runners ${pids[*]} -> KILL"
    kill -KILL "${pids[@]}" 2>/dev/null || true
  fi
  rm -f "$PIDFILE"
  log "stopped (marker $STOP_MARKER)"
  echo "stopped"
  return 0
}

cmd_install_reboot() {
  # Best-effort reboot revive. Prefer cron @reboot when available; always
  # install shell/profile hooks as a durable backup on this tini/pod-daemon box
  # (cron package is often absent and SysV rc.local is not run by PID 1).
  local rc=0
  local line="@reboot $ROOT/box/runner_ctl.sh start >>$ROOT/runner-logs/runner-ctl.log 2>&1"

  if command -v crontab >/dev/null 2>&1; then
    local existing
    existing="$(crontab -l 2>/dev/null || true)"
    if printf '%s\n' "$existing" | grep -Fqx "$line"; then
      echo "@reboot already installed"
    else
      {
        printf '%s\n' "$existing"
        printf '%s\n' "$line"
      } | crontab -
      echo "installed: $line"
    fi
  else
    echo "cron/crontab not available; skipping @reboot (no install candidate on this image)"
    rc=0
  fi

  # Shell/profile hooks (survive box file persistence; revive on login/interactive shells)
  local hook="$ROOT/box/autostart_hook.sh"
  local marker_begin="# >>> degen-trading-runner autostart >>>"
  local marker_end="# <<< degen-trading-runner autostart <<<"
  local hook_line="[ -x $ROOT/box/autostart_hook.sh ] && $ROOT/box/autostart_hook.sh"

  if [[ ! -x "$hook" ]]; then
    echo "missing autostart hook: $hook" >&2
    return 1
  fi

  _degen_strip_block() {
    local src="$1" dest="$2"
    if [[ -f "$src" ]]; then
      awk -v b="$marker_begin" -v e="$marker_end" '
        $0==b {skip=1; next}
        $0==e {skip=0; next}
        !skip {print}
      ' "$src" >"$dest"
    else
      : >"$dest"
    fi
  }

  # ~/.profile — append block
  local tmp
  tmp="$(mktemp)"
  _degen_strip_block "$HOME/.profile" "$tmp"
  {
    cat "$tmp"
    printf '\n%s\n' "$marker_begin"
    printf '%s\n' "# Revive box trading runner after reboot (idempotent; no secrets printed)."
    printf '%s\n' "$hook_line"
    printf '%s\n' "$marker_end"
  } >"${tmp}.out"
  cp "${tmp}.out" "$HOME/.profile"
  echo "installed shell hook: $HOME/.profile"

  # ~/.bashrc — insert before interactive early-return
  _degen_strip_block "$HOME/.bashrc" "$tmp"
  awk -v begin="$marker_begin" -v end="$marker_end" -v hook="$hook_line" '
    BEGIN {inserted=0}
    /^case \$- in/ && !inserted {
      print begin
      print "# Revive box trading runner after reboot (idempotent; no secrets printed)."
      print "# Placed before interactive guard so login shells sourcing bashrc still run it."
      print hook
      print end
      print ""
      inserted=1
    }
    {print}
    END {
      if (!inserted) {
        print ""
        print begin
        print hook
        print end
      }
    }
  ' "$tmp" >"${tmp}.out"
  cp "${tmp}.out" "$HOME/.bashrc"
  echo "installed shell hook: $HOME/.bashrc"

  # /etc/profile.d when writable via passwordless sudo
  if sudo -n true 2>/dev/null; then
    sudo tee /etc/profile.d/degen-trading-runner.sh >/dev/null <<EOF
# Revive box trading runner after reboot (idempotent; no secrets printed).
$marker_begin
$hook_line
$marker_end
EOF
    sudo chmod 644 /etc/profile.d/degen-trading-runner.sh
    echo "installed profile.d: /etc/profile.d/degen-trading-runner.sh"
  else
    echo "sudo not available; skipped /etc/profile.d"
  fi

  rm -f "$tmp" "${tmp}.out"
  return "$rc"
}

usage() {
  echo "usage: $0 start|stop|status|autostart|install-reboot"
}

case "${1:-}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  status) cmd_status ;;
  autostart) cmd_autostart ;;
  install-reboot) cmd_install_reboot ;;
  *) usage; exit 2 ;;
esac
