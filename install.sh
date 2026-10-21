#!/usr/bin/env bash
# Installs the Reiatsu relay: every 5 minutes it reads your Claude plan usage
# and publishes it to a secret gist that the Reiatsu watch face and widget download.
# Requirements: Claude Code CLI logged in (`claude auth login`), GitHub CLI (`gh auth login`), python3 (3.9+).
#
# Usage: ./install.sh [--gist ID] [--dir DIR] [--label LABEL]
#   --gist   reuse this gist (otherwise: the stored one, otherwise a new secret gist is created)
#   --dir    where the relay lives and keeps its state (default ~/.local/share/reiatsu-relay)
#   --label  launchd label / log name (default com.reiatsu.relay)
set -euo pipefail

DIR="$HOME/.local/share/reiatsu-relay"
LABEL="com.reiatsu.relay"
GIST=""
while [ $# -gt 0 ]; do
  case "$1" in
    --gist)  GIST="${2:?--gist needs an id}"; shift 2 ;;
    --dir)   DIR="${2:?--dir needs a path}"; shift 2 ;;
    --label) LABEL="${2:?--label needs a name}"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 2 ;;
  esac
done
case "$DIR" in "~"*) DIR="$HOME${DIR#\~}" ;; esac
[[ "$GIST" =~ ^[0-9A-Za-z]*$ ]] || { echo "Invalid gist id"; exit 2; }
[[ "$LABEL" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "Invalid label"; exit 2; }
HERE="$(cd "$(dirname "$0")" && pwd)"
# RELAY_NO_RUN=1 (tests): configure only, no network and no keychain access.
NO_RUN="${RELAY_NO_RUN:-}"

need() { command -v "$1" >/dev/null || { echo "Missing '$1': $2"; exit 1; }; }
need python3 "install Python 3"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || { echo "Python 3.9 or newer is required"; exit 1; }
need gh "install GitHub CLI (https://cli.github.com) and run 'gh auth login'"
if [ -z "$NO_RUN" ]; then
  need claude "install Claude Code (https://claude.com/claude-code) and run 'claude auth login'"
  gh auth status >/dev/null 2>&1 || { echo "Run first: gh auth login"; exit 1; }
  claude auth status 2>/dev/null | grep -q '"loggedIn": true' || { echo "Run first: claude auth login"; exit 1; }
fi

# Cartella nuova = privata (dentro: titoli di sessione); una già esistente non la tocco.
[ -d "$DIR" ] || { mkdir -p "$DIR"; chmod 700 "$DIR"; }
DIR="$(cd "$DIR" && pwd)"
# Se lo lancio dalla cartella di installazione non c'è nulla da copiare (cp darebbe errore).
if [ "$HERE" != "$DIR" ]; then
  cp "$HERE/claude_usage_relay.py" "$HERE/token_coach.py" "$DIR/"
fi
chmod +x "$DIR/claude_usage_relay.py"

if [ -z "$NO_RUN" ]; then
  echo "Reading your plan usage..."
  python3 "$DIR/claude_usage_relay.py" --dry-run --data-dir "$DIR" >/dev/null
fi

if [ -z "$GIST" ] && [ -f "$DIR/gist_id" ]; then GIST="$(tr -d '[:space:]' < "$DIR/gist_id")"; fi
if [ -z "$GIST" ]; then
  if [ -n "$NO_RUN" ]; then GIST="0000000000000000"; else
    echo "0,0,0,0,0,0,0,0,0" > "$DIR/usage.txt"
    URL=$(gh gist create -d "Reiatsu: AI plan usage for my Garmin" "$DIR/usage.txt" | tail -1)
    GIST="${URL##*/}"
  fi
fi
echo "$GIST" > "$DIR/gist_id"

PY="$(command -v python3)"
LOG="$HOME/Library/Logs/$LABEL.log"
if [ "$(uname)" = "Darwin" ]; then
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string>
    <string>$DIR/claude_usage_relay.py</string>
    <string>$GIST</string>
    <string>--data-dir</string><string>$DIR</string>
    <string>--log</string><string>$LOG</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$(dirname "$(command -v gh)"):/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>Nice</key><integer>10</integer>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>/dev/null</string>
</dict></plist>
PLISTEOF
  plutil -lint "$PLIST" >/dev/null 2>&1 || { [ -z "$(command -v plutil)" ] || { echo "Generated plist is invalid"; exit 1; }; }
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  # Dopo bootout il servizio può metterci un attimo a sparire: bootstrap subito dà "Input/output error".
  for try in 1 2 3 4 5; do
    launchctl bootstrap "gui/$(id -u)" "$PLIST" && break
    [ "$try" = 5 ] && { echo "launchctl bootstrap failed"; exit 1; }
    sleep 1
  done
else
  # Linux: cron every 5 minutes, low priority.
  mkdir -p "$HOME/.local/state"
  LOG="$HOME/.local/state/$LABEL.log"
  ( { crontab -l 2>/dev/null | grep -vF "$DIR/claude_usage_relay.py"; } || true
    echo "*/5 * * * * nice -n 10 '$PY' '$DIR/claude_usage_relay.py' $GIST --data-dir '$DIR' --log '$LOG' >/dev/null 2>&1" ) | crontab -
fi

if [ -z "$NO_RUN" ]; then
  USER_GH=$(gh api user -q .login)
  RAW="https://gist.githubusercontent.com/$USER_GH/$GIST/raw"
else
  RAW="https://gist.githubusercontent.com/<user>/$GIST/raw"
fi

cat <<DONE

Done! The relay runs every 5 minutes (log: $LOG).

In the Garmin Connect app on your phone:
  Device -> Connect IQ -> Reiatsu -> Settings, paste (the same URL works for the face and the widget):

  $RAW

(If your watch app wants the file explicitly: $RAW/usage.txt for the face, $RAW/widget.txt for the widget.)
DONE
