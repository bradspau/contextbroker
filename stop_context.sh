#!/bin/sh
# Stop the telemetry simulator that README starts in the background (echo $! > telemetry.pid).
# Usage: stop_context.sh [PIDFILE]   (PIDFILE defaults to telemetry.pid in the current directory)
#
# Stops one process only: the one whose id is in the pid file and whose command line names
# telemetry_simulator. It never touches containers or the compose stack.
# Exit 0: the simulator was signalled and the pid file removed.
# Exit 1, one fixed line on stderr: the pid file is missing or unreadable, holds anything but
# digits, or names a process that is not the simulator (nothing is signalled).
pidfile="${1:-telemetry.pid}"
[ -r "$pidfile" ] || { echo "stop_context: no readable pid file" >&2; exit 1; }
pid=$(cat "$pidfile")
case "$pid" in
  ''|*[!0-9]*) echo "stop_context: the pid file does not hold a process id" >&2; exit 1 ;;
esac
# -ww: never truncate the command line to a terminal width taken from COLUMNS
cmdline=$(ps -ww -p "$pid" -o args= 2>/dev/null)
case "$cmdline" in
  *telemetry_simulator*) ;;
  *) echo "stop_context: the process is not the telemetry simulator" >&2; exit 1 ;;
esac
kill "$pid" || { echo "stop_context: the signal failed" >&2; exit 1; }
rm -f "$pidfile"
