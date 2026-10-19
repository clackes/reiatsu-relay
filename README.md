# Reiatsu relay

The relay for the **Reiatsu** watch face and the **Reiatsu Gauge** widget for Garmin (vívoactive 3 family). They show your **Claude** plan usage on your wrist: the 5-hour window, the weekly limit, the countdowns to the resets, and tips for spending fewer tokens.

The watch can't read your usage directly. This script runs on your computer every 5 minutes:
1. It reads your plan's usage with the same login as the Claude Code CLI.
2. It publishes it as a few lines of text in a **secret gist** on your GitHub.
3. The watch downloads that gist through your phone.

**Your token never leaves your computer.** The gist holds only percentages, reset times and short usage tips.

## Install (macOS or Linux)

```bash
git clone https://github.com/clackes/reiatsu-relay && cd reiatsu-relay
claude auth login      # if you haven't already
gh auth login          # GitHub CLI
./install.sh
```

At the end the script prints two URLs. In the **Garmin Connect** app go to **Device → Connect IQ → Reiatsu → Settings** and paste the first one (`usage.txt`). Paste the second one (`widget.txt`) into **Reiatsu Gauge**.

The tips shown on the watch follow your system language (Italian or English). To force one: `CLAUDE_FACE_LANG=en ./install.sh`.

## What the gist contains

```
usage.txt   s,sr,w,wr,t   % 5-hour, 5-hour reset, % weekly, weekly reset, time read
widget.txt  same line + breakdown (Code/Chat/Cowork), 7-day history,
            last session title and a tip for it, general tips
```

The tips come from `token_coach.py`, which analyzes your local Claude Code sessions (`~/.claude/projects`). For the full report:

```bash
python3 token_coach.py
```

## Uninstall

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.reiatsu.relay.plist   # macOS
crontab -l | grep -v claude_usage_relay.py | crontab -                         # Linux
rm -rf ~/.local/share/reiatsu-relay
```

Then delete the gist from github.com.

---

*Unofficial personal project, not affiliated with or endorsed by Anthropic. "Claude" is a trademark of Anthropic. The relay reads the same usage endpoint the Claude Code CLI uses for `/usage`, which may change without notice.*
