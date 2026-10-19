#!/usr/bin/env bash
# Installs the Reiatsu relay: every 5 minutes it reads your Claude plan usage
# and publishes it to a secret gist that the Reiatsu watch face and the
# Reiatsu Gauge widget download.
# Requirements: Claude Code CLI logged in (`claude auth login`), GitHub CLI (`gh auth login`), python3.
set -euo pipefail

DIR="$HOME/.local/share/reiatsu-relay"
HERE="$(cd "$(dirname "$0")" && pwd)"

need() { command -v "$1" >/dev/null || { echo "Missing '$1': $2"; exit 1; }; }
need python3 "install Python 3"
need gh "install GitHub CLI (https://cli.github.com) and run 'gh auth login'"
need claude "install Claude Code (https://claude.com/claude-code) and run 'claude auth login'"

gh auth status >/dev/null 2>&1 || { echo "Run first: gh auth login"; exit 1; }
claude auth status 2>/dev/null | grep -q '"loggedIn": true' || { echo "Run first: claude auth login"; exit 1; }

mkdir -p "$DIR"
cp "$HERE/claude_usage_relay.py" "$HERE/token_coach.py" "$DIR/"

echo "Reading your plan usage…"
python3 "$DIR/claude_usage_relay.py" --print | head -1 >/dev/null

if [ -f "$DIR/gist_id" ]; then
  GIST=$(cat "$DIR/gist_id")
else
  echo "0,0,0,0,0" > "$DIR/usage.txt"
  URL=$(gh gist create -d "Reiatsu: AI plan usage for my Garmin" "$DIR/usage.txt" | tail -1)
  GIST="${URL##*/}"
  echo "$GIST" > "$DIR/gist_id"
fi
python3 "$DIR/claude_usage_relay.py" "$GIST"
USER_GH=$(gh api user -q .login)
RAW="https://gist.githubusercontent.com/$USER_GH/$GIST/raw"

if [ "$(uname)" = "Darwin" ]; then
  PLIST="$HOME/Library/LaunchAgents/com.reiatsu.relay.plist"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.reiatsu.relay</string>
  <key>ProgramArguments</key><array>
    <string>$(command -v python3)</string><string>$DIR/claude_usage_relay.py</string><string>$GIST</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$(dirname "$(command -v gh)"):/usr/bin:/bin</string>
  </dict>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/tmp/reiatsu-relay.log</string>
  <key>StandardErrorPath</key><string>/tmp/reiatsu-relay.log</string>
</dict></plist>
EOF
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
else
  # Linux: cron every 5 minutes.
  ( crontab -l 2>/dev/null | grep -v claude_usage_relay.py; echo "*/5 * * * * python3 $DIR/claude_usage_relay.py $GIST >> /tmp/reiatsu-relay.log 2>&1" ) | crontab -
fi

cat <<EOF

Done! The relay runs every 5 minutes.

In the Garmin Connect app on your phone:
  Device → Connect IQ → Reiatsu → Settings, paste:

  $RAW/usage.txt

For the Reiatsu Gauge widget use:

  $RAW/widget.txt
EOF
