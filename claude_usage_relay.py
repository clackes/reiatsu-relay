#!/usr/bin/env python3
"""Reads your Claude plan usage (5-hour window + weekly limit) and publishes
it to a secret gist as a few lines of CSV that the watch face downloads:

    s,sr,w,wr,t
    s/w   = percent used (5-hour / weekly)
    sr/wr = reset as UTC epoch (0 if no window is active)
    t     = when the usage was read

The OAuth token comes from the macOS keychain ("Claude Code-credentials") or
~/.claude/.credentials.json: the same one the `claude` CLI uses after
`claude auth login`. The token never leaves this computer: the gist only gets
the percentages.

Usage:  claude_usage_relay.py <gist_id>        (publish)
        claude_usage_relay.py --print          (print only, for testing)
"""
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

KEYCHAIN_SERVICE = "Claude Code-credentials"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URLS = [
    "https://platform.claude.com/v1/oauth/token",
    "https://console.anthropic.com/v1/oauth/token",
]
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
GIST_FILE = "usage.txt"


CRED_FILE = os.path.expanduser("~/.claude/.credentials.json")


def use_file():
    # Linux (and macOS without a keychain entry): the CLI stores credentials in a file.
    return sys.platform != "darwin" or (os.path.exists(CRED_FILE) and not _keychain_has())


def _keychain_has():
    return subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE],
                          capture_output=True).returncode == 0


def keychain_read():
    if use_file():
        with open(CRED_FILE) as f:
            return json.load(f)
    out = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


def keychain_account():
    out = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE],
        capture_output=True, text=True, check=True,
    ).stdout
    for line in out.splitlines():
        if '"acct"' in line and "=" in line:
            return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError("keychain account not found")


def keychain_write(creds):
    if use_file():
        with open(CRED_FILE, "w") as f:
            json.dump(creds, f)
        os.chmod(CRED_FILE, 0o600)
        return
    subprocess.run(
        ["security", "add-generic-password", "-U", "-a", keychain_account(),
         "-s", KEYCHAIN_SERVICE, "-w", json.dumps(creds)],
        capture_output=True, check=True,
    )


def refresh(creds):
    """Refreshes the token and saves it back to the keychain, so the CLI keeps
    working with the rotated refresh token."""
    oauth = creds["claudeAiOauth"]
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": oauth["refreshToken"],
        "client_id": CLIENT_ID,
    }).encode()
    last = None
    for url in TOKEN_URLS:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                tok = json.load(r)
            break
        except urllib.error.HTTPError as e:
            last = e
    else:
        raise last
    oauth["accessToken"] = tok["access_token"]
    if tok.get("refresh_token"):
        oauth["refreshToken"] = tok["refresh_token"]
    oauth["expiresAt"] = int((time.time() + tok.get("expires_in", 3600)) * 1000)
    keychain_write(creds)
    return creds


def fetch_usage(token):
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-watch-relay/1.0",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def get_usage():
    creds = keychain_read()
    oauth = creds["claudeAiOauth"]
    if oauth.get("expiresAt", 0) / 1000 < time.time() + 60:
        creds = refresh(creds)
    try:
        return fetch_usage(creds["claudeAiOauth"]["accessToken"])
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
        creds = refresh(creds)
        return fetch_usage(creds["claudeAiOauth"]["accessToken"])


def epoch(iso):
    if not iso:
        return 0
    ts = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    # The API returns e.g. 15:59:59.9: round to the minute.
    return int(round(ts / 60) * 60)


def to_line(usage):
    def part(key):
        win = usage.get(key) or {}
        return round(win.get("utilization") or 0), epoch(win.get("resets_at"))
    s, sr = part("five_hour")
    w, wr = part("seven_day")
    return f"{s},{sr},{w},{wr},{int(time.time())}"


HISTORY = os.path.expanduser("~/.local/share/claude-watch-relay/history.jsonl")
WIDGET_FILE = "widget.txt"


def record_history(usage):
    """Keeps one row per reading: the widget uses it for the daily chart."""
    w = usage.get("seven_day") or {}
    row = {"t": int(time.time()), "w": w.get("utilization") or 0, "wr": epoch(w.get("resets_at"))}
    os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
    with open(HISTORY, "a") as f:
        f.write(json.dumps(row) + "\n")
    return load_history()


def load_history(days=8):
    cutoff = time.time() - days * 86400
    rows = []
    try:
        with open(HISTORY) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r["t"] >= cutoff:
                    rows.append(r)
    except FileNotFoundError:
        pass
    return rows


def daily_usage(rows, days=7):
    """Weekly-usage percentage points spent on each of the last `days` days
    (local time), oldest first. Handles resets."""
    today = dt.date.today()
    out = {today - dt.timedelta(days=i): 0.0 for i in range(days)}
    prev = None
    for r in rows:
        if prev is not None:
            delta = r["w"] - prev["w"] if r["wr"] == prev["wr"] else r["w"]
            day = dt.datetime.fromtimestamp(r["t"]).date()
            if delta > 0 and day in out:
                out[day] += delta
        prev = r
    return [round(out[d]) for d in sorted(out)]


def widget_text(usage, rows):
    lines = [to_line(usage)]
    rows_b = ((usage.get("seven_day_breakdown") or {}).get("rows")) or []
    pct = {r.get("key"): r.get("percent") or 0 for r in rows_b}
    lines.append("B,%d,%d,%d,%d" % (pct.get("claude_code", 0), pct.get("chat", 0),
                                    pct.get("cowork", 0), pct.get("other", 0)))
    lines.append("H," + ",".join(str(v) for v in daily_usage(rows)))
    # Dollar-based allowance (the cloud credit), when the account has one:
    # D,used_cents,limit_cents,reset_epoch
    for key, win in usage.items():
        if isinstance(win, dict) and win.get("limit_dollars"):
            lines.append("D,%d,%d,%d" % (round((win.get("used_dollars") or 0) * 100),
                                         round(win["limit_dollars"] * 100), epoch(win.get("resets_at"))))
            break
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import token_coach
        summ = token_coach.summary(token_coach.sessions())
        last = summ.get("last") or {}
        if last:
            lines.append("C,%s,%d,%d" % (last["project"].replace(",", " ")[:16], last["peak_ctx"] // 1000, last["calls"]))
            lines.append("S," + last["title"].replace("\n", " ")[:48])
            lines.append("X," + last["tip"].replace("\n", " ")[:60])
        for tip in summ["tips"][:4]:
            lines.append("T," + tip["short"].replace("\n", " ")[:44])
    except Exception as e:  # the widget must work even without tips
        lines.append("T,coach unavailable")
        print("token_coach:", e, file=sys.stderr)
    return "\n".join(lines)


def publish(gist_id, files):
    args = ["gh", "api", "-X", "PATCH", f"gists/{gist_id}"]
    for name, content in files.items():
        args += ["-f", f"files[{name}][content]={content}"]
    subprocess.run(args, capture_output=True, check=True)


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    usage = get_usage()
    line = to_line(usage)
    if sys.argv[1] == "--print":
        print(json.dumps(usage, indent=2))
        print(line)
        print(widget_text(usage, load_history()))
    else:
        widget = widget_text(usage, record_history(usage))
        publish(sys.argv[1], {GIST_FILE: line, WIDGET_FILE: widget})
        print(dt.datetime.now().isoformat(timespec="seconds"), line)


if __name__ == "__main__":
    main()
