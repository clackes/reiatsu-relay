#!/usr/bin/env bash
# Removes the Reiatsu relay job. Usage: ./uninstall.sh [--dir DIR] [--label LABEL] [--purge]
#   --purge  also deletes the data folder (state, history, gist id). The gist itself stays on GitHub.
set -euo pipefail

DIR="$HOME/.local/share/reiatsu-relay"
LABEL="com.reiatsu.relay"
PURGE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dir)   DIR="${2:?--dir needs a path}"; shift 2 ;;
    --label) LABEL="${2:?--label needs a name}"; shift 2 ;;
    --purge) PURGE=1; shift ;;
    -h|--help) sed -n '2,3p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 2 ;;
  esac
done
case "$DIR" in "~"*) DIR="$HOME${DIR#\~}" ;; esac
[[ "$LABEL" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "Invalid label"; exit 2; }

if [ "$(uname)" = "Darwin" ]; then
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
else
  ( crontab -l 2>/dev/null | grep -vF "$DIR/claude_usage_relay.py" || true ) | crontab -
fi
rm -f "$HOME/Library/Logs/$LABEL.log" "$HOME/Library/Logs/$LABEL.log.1" "$HOME/.local/state/$LABEL.log"
if [ -n "$PURGE" ]; then
  [ -f "$DIR/claude_usage_relay.py" ] && rm -rf "$DIR" || echo "Not removing $DIR (does not look like a relay folder)"
fi
echo "Relay job removed. Delete the gist from github.com if you no longer need it."
