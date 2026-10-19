# Claude Face — relay

The relay for the **Claude Face** watch face and the **Claude Usage** widget for Garmin.

The watch can't read your Claude usage directly. This script runs on your computer every 5 minutes:
1. It reads your plan's usage (5-hour window and weekly limit) with the same login as the Claude Code CLI.
2. It publishes it as a few lines of text in a **secret gist** on your GitHub.
3. The watch downloads that gist through your phone.

**Your token never leaves your computer.** The gist holds only percentages, reset times and anonymous usage tips.

## Installation (macOS or Linux)

```bash
claude auth login      # if you haven't already
gh auth login          # GitHub CLI
./install.sh
```

At the end the script prints a URL. Open the Garmin Connect app, go to **Device → Connect IQ → Claude Face → Settings** and paste it.

## What the gist contains

```
usage.txt   s,sr,w,wr,t        % 5-hour, 5-hour reset, % weekly, weekly reset, time read
widget.txt  same line + breakdown (Code/Chat/Cowork), 7-day history,
            title and tip for your last session, general tips
```

The tips come from `token_coach.py`, which analyzes the local Claude Code sessions (`~/.claude/projects`), for example "context at 300k: run /clear". You can see them in full with:

```bash
python3 token_coach.py
```

## Uninstall

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.claudeface.relay.plist   # macOS
crontab -l | grep -v claude_usage_relay.py | crontab -                           # Linux
rm -rf ~/.local/share/claude-watch-relay
```

Then delete the gist from github.com.

---

*Unofficial project, not affiliated with Anthropic. It uses the usage endpoint that Claude Code itself uses for `/usage`, so it may change without notice.*
