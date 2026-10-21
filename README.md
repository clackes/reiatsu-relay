# Reiatsu relay

The relay for the **Reiatsu** watch face and the **Reiatsu Gauge** widget for Garmin watches (Connect IQ 3.0 or newer). They show your **Claude** plan usage on your wrist: the 5-hour window, the weekly limit, the countdowns to the resets, and tips for spending fewer tokens.

The watch can't read your usage directly. This script runs on your computer every 5 minutes:
1. It reads your plan's usage with the same login as the Claude Code CLI.
2. It publishes it as a few lines of text in a **secret gist** on your GitHub.
3. The watch downloads that gist through your phone.

**Your token never leaves your computer.** The gist holds only percentages, reset times and short usage tips.

## Install (macOS or Linux, Python 3.9+, no extra packages)

```bash
git clone https://github.com/clackes/reiatsu-relay && cd reiatsu-relay
claude auth login      # if you haven't already
gh auth login          # GitHub CLI
./install.sh           # options: --gist ID  --dir DIR  --label LABEL
```

A secret gist is created only if you do not pass `--gist` and none is stored yet. At the end the script prints one URL. In **Garmin Connect** go to **Device -> Connect IQ -> Reiatsu -> Settings** and paste it; the same URL works for the **Reiatsu Gauge** widget (the apps pick `usage.txt` or `widget.txt` themselves).

The job is light on purpose: macOS launchd with `ProcessType Background`, low I/O priority, `Nice 10`, every 5 minutes (Linux: cron with `nice`). It publishes to GitHub only when the content changed or every 30 minutes as a heartbeat, and re-reads only the new bytes of your Claude Code logs. Log: `~/Library/Logs/<label>.log` (publishes and errors only, size-capped).

Useful commands (all safe, none publishes):

```bash
python3 claude_usage_relay.py --status                  # last publish / error / next poll hint
python3 claude_usage_relay.py --dry-run --data-dir DIR  # full run, prints both files, does not publish
python3 claude_usage_relay.py --print                   # API answer + both files, saves nothing
python3 -m unittest discover -s tests                   # tests (no network, no keychain)
```

## What the gist contains

`usage.txt` is one line read by the watch face; fields 6-9 are new in v2 and older watch apps ignore them:

```
s,sr,w,wr,t,n,f,e,we
```

| field | meaning |
|---|---|
| `s`, `sr` | 5-hour utilisation %, its reset (epoch) |
| `w`, `wr` | weekly utilisation %, its reset |
| `t` | when the relay read the API |
| `n` | seconds until the watch should poll again (600 / 900 / 1800 / 3600, from how close to the limit and how active you are) |
| `f` | flags: 1 = 5-hour limit reached, 2 = weekly limit reached, 4 = Claude Code active in the last 15 min, 8 = extra usage enabled |
| `e`, `we` | projected time the 5-hour / weekly limit hits 100 % at the current pace (0 = not before the reset) |

`widget.txt` starts with the same line, then optional tagged lines: `B` weekly breakdown (Code/Chat/Cowork/other), `H` points used in each of the last 7 days, `R` 5-hour utilisation every 15 min over the last 5 h (`-1` = no reading), `D` cloud credit, `E` extra usage, `C`/`S`/`X` last Claude Code session (project, peak context, title, tip) and `T` general tips. At most 1.5 KB.

The tips come from `token_coach.py`, which analyzes your local Claude Code sessions (`~/.claude/projects`, subagents included) incrementally. For the full report: `python3 token_coach.py`.

## Privacy

Your OAuth token never leaves your computer and is never logged. The gist holds only percentages, reset times, a project folder name (16 chars), a session title and short tips. Anyone with the gist URL can read it, so keep the URL private. Local state (`state.json`, history, coach cache) stays in the data folder, readable only by you.

## Uninstall

```bash
./uninstall.sh [--dir DIR] [--label LABEL] [--purge]
```

Then delete the gist from github.com.

---

*Unofficial personal project, not affiliated with or endorsed by Anthropic. "Claude" is a trademark of Anthropic. The relay reads the same usage endpoint the Claude Code CLI uses for `/usage`, which may change without notice.*
