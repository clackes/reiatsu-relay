#!/usr/bin/env bash
# Installa il relay di Claude Face: legge l'uso del tuo piano Claude ogni
# 5 minuti e lo pubblica in un gist segreto che il watch face scarica.
# Requisiti: Claude Code CLI con login (`claude auth login`), GitHub CLI (`gh auth login`), python3.
set -euo pipefail

DIR="$HOME/.local/share/claude-watch-relay"
HERE="$(cd "$(dirname "$0")" && pwd)"

need() { command -v "$1" >/dev/null || { echo "Manca '$1': $2"; exit 1; }; }
need python3 "installa Python 3"
need gh "installa GitHub CLI (https://cli.github.com) e fai 'gh auth login'"
need claude "installa Claude Code (https://claude.com/claude-code) e fai 'claude auth login'"

gh auth status >/dev/null 2>&1 || { echo "Fai prima: gh auth login"; exit 1; }
claude auth status 2>/dev/null | grep -q '"loggedIn": true' || { echo "Fai prima: claude auth login"; exit 1; }

mkdir -p "$DIR"
cp "$HERE/claude_usage_relay.py" "$HERE/token_coach.py" "$DIR/"

echo "Provo a leggere l'uso del tuo piano…"
python3 "$DIR/claude_usage_relay.py" --print | head -1 >/dev/null

if [ -f "$DIR/gist_id" ]; then
  GIST=$(cat "$DIR/gist_id")
else
  echo "0,0,0,0,0" > "$DIR/usage.txt"
  URL=$(gh gist create -d "Claude Face usage (watch face Garmin)" "$DIR/usage.txt" | tail -1)
  GIST="${URL##*/}"
  echo "$GIST" > "$DIR/gist_id"
fi
python3 "$DIR/claude_usage_relay.py" "$GIST"
USER_GH=$(gh api user -q .login)
RAW="https://gist.githubusercontent.com/$USER_GH/$GIST/raw"

if [ "$(uname)" = "Darwin" ]; then
  PLIST="$HOME/Library/LaunchAgents/com.claudeface.relay.plist"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.claudeface.relay</string>
  <key>ProgramArguments</key><array>
    <string>$(command -v python3)</string><string>$DIR/claude_usage_relay.py</string><string>$GIST</string>
  </array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$(dirname "$(command -v gh)"):/usr/bin:/bin</string></dict>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/tmp/claude-face-relay.log</string>
  <key>StandardErrorPath</key><string>/tmp/claude-face-relay.log</string>
</dict></plist>
EOF
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
else
  # Linux: cron ogni 5 minuti.
  ( crontab -l 2>/dev/null | grep -v claude_usage_relay.py; echo "*/5 * * * * python3 $DIR/claude_usage_relay.py $GIST >> /tmp/claude-face-relay.log 2>&1" ) | crontab -
fi

cat <<EOF

Fatto! Il relay gira ogni 5 minuti.

Nell'app Garmin Connect sul telefono:
  Dispositivo → Connect IQ → Claude Face → Impostazioni
  incolla questo URL:

  $RAW/usage.txt

Per il widget "Claude Usage" usa invece:

  $RAW/widget.txt
EOF
