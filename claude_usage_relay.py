#!/usr/bin/env python3
"""Reiatsu relay v2: reads your Claude plan usage (5-hour window + weekly
limit) and publishes it to a secret gist that the watch face and the widget
download through your phone.

    usage.txt   s,sr,w,wr,t,n,f,e,we
    widget.txt  the same line, then tagged lines B, H, R, D, E, C, S, X, T

The OAuth token comes from the macOS keychain ("Claude Code-credentials") or
~/.claude/.credentials.json: the same login the `claude` CLI uses. The token
never leaves this computer: the gist only gets percentages, reset times and
short tips. The gist is updated only when its content changed, or every 30
minutes as a heartbeat.

Usage:
  claude_usage_relay.py <gist_id>          read the usage, publish if needed
  claude_usage_relay.py --dry-run [gist]   everything except publishing; prints both files
  claude_usage_relay.py --print            print the API answer and both files, save nothing
  claude_usage_relay.py --status           last publish, last error, next poll hint
Options:
  --data-dir DIR   state.json and caches (default: the folder of this script)
  --log FILE       append publishes and errors to FILE (size-capped)
"""
import argparse
import bisect
import hashlib
import http.client
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta

try:
    import fcntl
except ImportError:  # non-POSIX: niente lock, il relay gira comunque
    fcntl = None

VERSION = "2.0"
KEYCHAIN_SERVICE = "Claude Code-credentials"
CRED_FILE = os.path.expanduser("~/.claude/.credentials.json")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URLS = [
    "https://platform.claude.com/v1/oauth/token",
    "https://console.anthropic.com/v1/oauth/token",
]
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
USER_AGENT = "claude-watch-relay/" + VERSION

GIST_FILE = "usage.txt"
WIDGET_FILE = "widget.txt"
STATE_FILE = "state.json"
LOCK_FILE = ".relay.lock"
COACH_CACHE = "coach_cache.json"
GIST_ID_FILE = "gist_id"

HTTP_TIMEOUT = 15
GH_TIMEOUT = 60
RETRY_DELAY = 3             # secondi prima dell'unico nuovo tentativo
HEARTBEAT = 1800            # pubblica comunque ogni 30 min: l'orologio vede che il relay è vivo
HEARTBEAT_SLACK = 120       # i giri launchd ogni 300 s non cadono esatti
KEEP_DAYS = 8
MAX_ROWS = 6000
MAX_WIDGET = 1536           # byte, limite del protocollo (§2)
SLOT = 900                  # riga R: un campione ogni 15 min
SLOTS = 20
LOG_CAP = 256 * 1024
LOG_KEEP = 64 * 1024
BACKOFF_DEFAULT = 600
# v1 scriveva la storia qui, qualunque fosse la cartella di installazione.
V1_HISTORY = os.path.expanduser("~/.local/share/claude-watch-relay/history.jsonl")
# Log v1 (plist personale e installer pubblico): danno s/sr che history.jsonl non aveva.
V1_LOGS = ("/tmp/claude-usage-relay.log", "/tmp/reiatsu-relay.log")
_V1_LOGLINE = re.compile(r"^\d{4}-\d\d-\d\dT[\d:]+ (\d+),(\d+),(\d+),(\d+),(\d+)\s*$", re.M)
_FRACTION = re.compile(r"^(.*T\d\d:\d\d:\d\d)\.\d+(.*)$")
_GIST_ID = re.compile(r"^[0-9A-Za-z]{1,64}$")
_SECRET = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")


class RelayError(Exception):
    def __init__(self, msg, code=None, retry_after=None):
        Exception.__init__(self, msg)
        self.code = code
        self.retry_after = retry_after


def _redact(text):
    return _SECRET.sub("sk-ant-***", str(text))


def _err(e):
    # Solo tipo e messaggio: mai header o corpi delle richieste.
    if isinstance(e, urllib.error.URLError) and not isinstance(e, urllib.error.HTTPError):
        e = e.reason
    return _redact("%s: %s" % (type(e).__name__, e) if str(e) else type(e).__name__)


# ---- credenziali -------------------------------------------------------------

def read_creds():
    """Una sola lettura: ritorna (sorgente, dict)."""
    if sys.platform == "darwin":
        try:
            r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                               capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            r = None
        if r is not None and r.returncode == 0 and r.stdout.strip():
            try:
                return "keychain", json.loads(r.stdout)
            except ValueError:
                raise RelayError("keychain entry is not valid JSON")
    try:
        with open(CRED_FILE, encoding="utf-8") as f:
            return "file", json.load(f)
    except FileNotFoundError:
        raise RelayError("no Claude Code login found: run 'claude auth login'")
    except (OSError, ValueError) as e:
        raise RelayError("cannot read %s (%s)" % (CRED_FILE, type(e).__name__))


def _keychain_account():
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE],
                       capture_output=True, text=True, timeout=20)
    m = re.search(r'"acct"<blob>="(.*)"', r.stdout or "")
    if m:
        return m.group(1)
    return os.environ.get("USER") or os.environ.get("LOGNAME") or ""


def write_creds(source, creds):
    """Salva il token ruotato dove l'ho letto, così la CLI continua a funzionare."""
    blob = json.dumps(creds, separators=(",", ":"))
    if source == "file":
        tmp = CRED_FILE + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(blob)
        os.replace(tmp, CRED_FILE)
        return
    acct = _keychain_account()
    hexv = blob.encode("utf-8").hex()
    cmd = 'add-generic-password -U -a "%s" -s "%s" -X %s\n' % (acct, KEYCHAIN_SERVICE, hexv)
    # Via stdin il segreto non compare in `ps` né nei messaggi d'errore. `security -i` però spezza le
    # righe a 4096 byte: oltre, come fa la CLI, ripiego sugli argomenti (visibili solo al mio utente).
    if '"' not in acct and "\\" not in acct and len(cmd) < 4000:
        r = subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True, timeout=20)
    else:
        r = subprocess.run(["security", "add-generic-password", "-U", "-a", acct,
                            "-s", KEYCHAIN_SERVICE, "-X", hexv], capture_output=True, timeout=20)
    if r.returncode != 0:
        raise RelayError("could not save the refreshed token to the keychain")
    _, back = read_creds()
    if (back.get("claudeAiOauth") or {}).get("accessToken") != creds["claudeAiOauth"]["accessToken"]:
        raise RelayError("the keychain did not keep the refreshed token")


def _expired(oauth):
    try:
        return float(oauth.get("expiresAt") or 0) / 1000 < time.time() + 60
    except (TypeError, ValueError):
        return True


def refresh_token(oauth):
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": oauth["refreshToken"],
        "client_id": CLIENT_ID,
    }).encode()
    last = None
    for url in TOKEN_URLS:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                tok = json.load(r)
            if isinstance(tok, dict) and tok.get("access_token"):
                break
            last = RelayError("no access_token in the answer")
        except urllib.error.HTTPError as e:
            last = RelayError("HTTP %d" % e.code, code=e.code)
            e.close()
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
            last = RelayError(_err(e))
    else:
        raise RelayError("token refresh failed (%s)" % last, code=getattr(last, "code", None))
    oauth["accessToken"] = tok["access_token"]
    if tok.get("refresh_token"):
        oauth["refreshToken"] = tok["refresh_token"]
    try:
        life = float(tok.get("expires_in") or 3600)
    except (TypeError, ValueError):
        life = 3600
    oauth["expiresAt"] = int((time.time() + life) * 1000)
    return oauth


def _refreshed(used):
    """Rilegge prima di ruotare: la CLI potrebbe averlo appena fatto lei."""
    source, creds = read_creds()
    oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
    if not isinstance(oauth, dict):
        raise RelayError("not logged in: run 'claude auth login'")
    if oauth.get("accessToken") and oauth["accessToken"] != used and not _expired(oauth):
        return oauth["accessToken"]
    if not oauth.get("refreshToken"):
        raise RelayError("token expired: run 'claude auth login'")
    refresh_token(oauth)
    write_creds(source, creds)
    return oauth["accessToken"]


def _retry_after(headers):
    try:
        return int(float(headers.get("Retry-After")))
    except (TypeError, ValueError, AttributeError):
        return None


def fetch_usage(token):
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + token,
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    for attempt in (0, 1):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                data = json.load(r)
            if not isinstance(data, dict):
                raise ValueError("not an object")
            return data
        except urllib.error.HTTPError as e:
            code, ra = e.code, _retry_after(e.headers)
            e.close()
            if code >= 500 and attempt == 0:
                time.sleep(RETRY_DELAY)
                continue
            raise RelayError("usage API: HTTP %d" % code, code=code, retry_after=ra)
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
            if attempt == 0:
                time.sleep(RETRY_DELAY)
                continue
            raise RelayError("usage API: " + _err(e))


def get_usage():
    source, creds = read_creds()
    oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        raise RelayError("not logged in: run 'claude auth login'")
    token = oauth["accessToken"]
    if _expired(oauth):
        token = _refreshed(None)
    try:
        return fetch_usage(token)
    except RelayError as e:
        if e.code != 401:
            raise
    return fetch_usage(_refreshed(token))


# ---- lettura della risposta --------------------------------------------------

def _f(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return float(v)


def epoch(iso):
    if not iso or not isinstance(iso, str):
        return 0
    s = iso.strip().replace("Z", "+00:00")
    try:
        ts = datetime.fromisoformat(s).timestamp()
    except ValueError:
        m = _FRACTION.match(s)
        try:
            ts = datetime.fromisoformat(m.group(1) + m.group(2)).timestamp() if m else None
        except ValueError:
            ts = None
        if ts is None:
            return 0
    # L'API dà p.es. 15:59:59.9: arrotondo al minuto.
    return int(round(ts / 60) * 60)


def _window(usage, key, kind):
    win = usage.get(key)
    lim = None
    for item in usage.get("limits") or []:
        if isinstance(item, dict) and item.get("kind") == kind:
            lim = item
            break
    if isinstance(win, dict):
        util = _f(win.get("utilization")) or 0.0
        reset = epoch(win.get("resets_at"))
        locked = bool(win.get("locked_reason"))
    elif lim is not None:
        # Ripiego sulla lista "limits" se la finestra manca.
        util = _f(lim.get("percent")) or 0.0
        reset = epoch(lim.get("resets_at"))
        locked = False
    else:
        return 0.0, 0, False
    if lim is not None and (_f(lim.get("percent")) or 0) >= 100:
        locked = True
    return max(0.0, util), reset, locked


def read_usage(usage):
    s, sr, l5 = _window(usage, "five_hour", "session")
    w, wr, l7 = _window(usage, "seven_day", "weekly_all")
    extra = usage.get("extra_usage")
    return {"s": s, "sr": sr, "lock5": l5, "w": w, "wr": wr, "lock7": l7,
            "extra": isinstance(extra, dict) and extra.get("is_enabled") is True}


# ---- calcoli -----------------------------------------------------------------

def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return 0.0
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def burn_rate(points):
    """Pendenza di Theil-Sen (%/s): robusta a letture anomale."""
    slopes = []
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            dt = points[j][0] - points[i][0]
            if dt >= 60:
                slopes.append((points[j][1] - points[i][1]) / dt)
    return _median(slopes)


# (indice valore, indice reset, finestra, punti minimi, arco minimo, secchio, quanto, fermo)
# `fermo`: se da tanti secondi l'uso non sale, il ritmo recente è zero e la stima sarebbe vecchia.
ETA_5H = (1, 2, 3600, 4, 900, 0, 300, 900)
ETA_WEEK = (3, 4, 86400, 4, 3 * 3600, 3600, 900, 0)


def eta(rows, now, cur, reset, spec):
    """Epoch in cui la finestra arriva al 100 % al ritmo attuale; 0 = non prima del reset."""
    vi, ri, lookback, min_pts, min_span, bucket, quantum, still = spec
    if not reset or reset <= now or cur >= 100:
        return 0
    pts = [(r[0], r[vi]) for r in rows
           if now - lookback <= r[0] <= now and r[vi] is not None and abs((r[ri] or 0) - reset) <= 120]
    if bucket:
        last = {}
        for p in pts:
            last[p[0] // bucket] = p  # ultima lettura di ogni ora: meno coppie, stesso trend
        pts = [last[b] for b in sorted(last)]
    if len(pts) < min_pts or pts[-1][0] - pts[0][0] < min_span:
        return 0
    if still:
        before = [p for p in pts if p[0] <= now - still]
        if before and cur <= before[-1][1]:
            return 0  # fermo da `still` secondi: niente proiezione su un ritmo che non c'è più
    rate = burn_rate(pts)
    if rate <= 0:
        return 0
    at = int(math.ceil((now + (100 - cur) / rate) / quantum) * quantum)
    return at if at < reset else 0


def _eff(r, now, vi, ri):
    v = r[vi]
    if v is None:
        return None
    return 0 if (r[ri] and r[ri] <= now) else int(round(v))


def _changed(rows, since, now, idx):
    """Vero se dopo `since` s/w (arrotondati) non sono sempre stati uguali a ora."""
    cur = rows[-1]
    base = None
    window = []
    for r in rows:
        if r[0] < since:
            base = r
        else:
            window.append(r)
    for r in ([base] if base else []) + window:
        for vi in idx:
            a, b = _eff(r, r[0], vi, vi + 1), _eff(cur, now, vi, vi + 1)
            if a is not None and b is not None and a != b:
                return True
    return False


def hint(rows, now, activity):
    """Secondi suggeriti all'orologio prima del prossimo GET (§3)."""
    if not rows:
        return 900
    cur = rows[-1]
    active = activity is not None and now - activity <= 900
    s_now = _eff(cur, now, 1, 2) or 0
    if s_now >= 80 and _changed(rows, now - 1800, now, (1,)):
        return 600
    if active or _changed(rows, now - 1800, now, (1, 3)):
        return 900
    if (rows[0][0] <= now - 3 * 3600 and not _changed(rows, now - 3 * 3600, now, (1, 3))
            and (activity is None or now - activity > 3 * 3600)):
        return 3600
    return 1800


def flags(rd, now, activity):
    f = 0
    if (rd["s"] >= 100 or rd["lock5"]) and not (rd["sr"] and rd["sr"] <= now):
        f |= 1
    if (rd["w"] >= 100 or rd["lock7"]) and not (rd["wr"] and rd["wr"] <= now):
        f |= 2
    if activity is not None and now - activity <= 900:
        f |= 4
    if rd["extra"]:
        f |= 8
    return f


def r_values(rows, now):
    """5 ore di utilizzo a passi di 15 min (allineati), l'ultimo è la lettura attuale."""
    base = now - now % SLOT
    taus = [base - (SLOTS - 2 - k) * SLOT for k in range(SLOTS - 1)] + [now]
    ts = [r[0] for r in rows]
    out = []
    for tau in taus:
        j = bisect.bisect_right(ts, tau) - 1
        while j >= 0 and rows[j][0] > tau - SLOT and rows[j][1] is None:
            j -= 1
        if j < 0 or rows[j][0] <= tau - SLOT:
            out.append(-1)  # nessuna lettura (Mac spento o in stop)
        else:
            out.append(_eff(rows[j], tau, 1, 2))
    return out


def daily_usage(rows, now, days=7):
    """Punti di limite settimanale spesi in ciascuno degli ultimi `days` giorni locali."""
    today = datetime.fromtimestamp(now).date()
    out = {today - timedelta(days=i): 0.0 for i in range(days)}
    prev = None
    for r in rows:
        if prev is not None and r[3] is not None and prev[3] is not None:
            # Nuova finestra se la vecchia è scaduta o l'uso è crollato con un reset diverso.
            new = bool(prev[4] and prev[4] <= r[0]) or (r[4] != prev[4] and r[3] + 1 < prev[3])
            delta = r[3] if new else r[3] - prev[3]
            day = datetime.fromtimestamp(r[0]).date()
            if delta > 0 and day in out:
                out[day] += delta
        prev = r
    return [int(round(out[d])) for d in sorted(out)]


def clean(text, cap, final=True):
    """Testo sicuro per il protocollo: una riga, niente virgole fuori dall'ultimo campo."""
    if text is None:
        text = ""
    elif not isinstance(text, str):
        text = str(text)
    out = []
    for ch in text:
        o = ord(ch)
        if ch in "\r\n\t\v\f\x85\u2028\u2029":
            out.append(" ")
        elif o < 32 or 0x7f <= o < 0xa0 or o > 0xffff or 0xfe00 <= o <= 0xfe0f:
            continue  # controllo, emoji fuori BMP, selettori di variante: i font dell'orologio non li hanno
        elif unicodedata.category(ch) in ("Cf", "Cs", "Co", "Cn"):
            continue
        elif ch == "," and not final:
            out.append(" ")
        else:
            out.append(ch)
    s = " ".join("".join(out).split())
    return s[:cap].rstrip() if len(s) > cap else s


_STD_WINDOWS = ("five_hour", "seven_day")


def d_line(usage):
    """Credito cloud: la prima finestra con un tetto in dollari (non le finestre del piano)."""
    cands = []
    for key, win in usage.items():
        if isinstance(win, dict):
            lim = _f(win.get("limit_dollars"))
            if lim and lim > 0:
                cands.append((key in _STD_WINDOWS or key.startswith("seven_day"), key, win, lim))
    if not cands:
        return None
    cands.sort(key=lambda c: c[0])
    _, _, win, lim = cands[0]
    used = _f(win.get("used_dollars"))
    if used is None:
        rem = _f(win.get("remaining_dollars"))
        util = _f(win.get("utilization"))
        used = lim - rem if rem is not None else (lim * util / 100 if util is not None else 0.0)
    return "D,%d,%d,%d" % (round(max(0.0, used) * 100), round(lim * 100), epoch(win.get("resets_at")))


def _cents(amount, exponent):
    if amount is None:
        return None
    if isinstance(exponent, int) and not isinstance(exponent, bool) and 0 <= exponent <= 6:
        return amount * 10 ** (2 - exponent)
    return amount  # senza esponente: già in centesimi


def e_line(usage):
    """Extra usage (solo se attivo): E,usati_cent,limite_cent,pct."""
    ex = usage.get("extra_usage")
    if not isinstance(ex, dict) or ex.get("is_enabled") is not True:
        return None
    dp = ex.get("decimal_places")
    used = _cents(_f(ex.get("used_credits")), dp)
    limit = _cents(_f(ex.get("monthly_limit")), dp)
    pct = _f(ex.get("utilization"))
    sp = usage.get("spend")
    if isinstance(sp, dict):
        u, lim = sp.get("used"), sp.get("limit")
        if used is None and isinstance(u, dict):
            used = _cents(_f(u.get("amount_minor")), u.get("exponent"))
        if limit is None and isinstance(lim, dict):
            limit = _cents(_f(lim.get("amount_minor")), lim.get("exponent"))
        if pct is None:
            pct = _f(sp.get("percent"))
    used = used or 0.0
    if pct is None:
        pct = used * 100.0 / limit if limit else 0.0
    return "E,%d,%d,%d" % (round(used), round(limit or 0), round(max(0.0, pct)))


def coach_lines(summ):
    out = []
    if not summ:
        return out
    last = summ.get("last")
    if last:
        proj = clean(last.get("project"), 16, final=False) or "-"
        out.append("C,%s,%d,%d" % (proj, int(last.get("peak_ctx") or 0) // 1000, int(last.get("calls") or 0)))
        title = clean(last.get("title"), 48)
        if title:
            out.append("S," + title)
        tip = clean(last.get("tip"), 60)
        if tip:
            out.append("X," + tip)
    for item in (summ.get("tips") or [])[:4]:
        tip = clean(item.get("short") if isinstance(item, dict) else item, 44)
        if tip:
            out.append("T," + tip)
    return out


def build(usage, rows, now, summ, activity):
    """Ritorna (usage.txt, widget.txt, n). `rows` include già la lettura attuale."""
    rd = read_usage(usage)
    n = hint(rows, now, activity)
    e = eta(rows, now, rd["s"], rd["sr"], ETA_5H)
    we = eta(rows, now, rd["w"], rd["wr"], ETA_WEEK)
    line = "%d,%d,%d,%d,%d,%d,%d,%d,%d" % (round(rd["s"]), rd["sr"], round(rd["w"]), rd["wr"], now,
                                           n, flags(rd, now, activity), e, we)
    lines = [line]
    rows_b = (usage.get("seven_day_breakdown") or {}).get("rows") if isinstance(
        usage.get("seven_day_breakdown"), dict) else None
    if isinstance(rows_b, list) and rows_b:
        pct = {}
        for r in rows_b:
            if isinstance(r, dict) and isinstance(r.get("key"), str):
                pct[r["key"]] = _f(r.get("percent")) or 0.0
        lines.append("B,%d,%d,%d,%d" % tuple(round(pct.get(k, 0)) for k in ("claude_code", "chat", "cowork", "other")))
    lines.append("H," + ",".join(str(v) for v in daily_usage(rows, now)))
    rv = r_values(rows, now)
    if sum(1 for v in rv if v >= 0) >= 2:
        lines.append("R," + ",".join(str(v) for v in rv))
    for extra in (d_line(usage), e_line(usage)):
        if extra:
            lines.append(extra)
    lines += coach_lines(summ)
    # Tetto di 1.5 KB: sacrifico prima i consigli in fondo.
    while len(lines) > 1 and len("\n".join(lines).encode("utf-8")) > MAX_WIDGET:
        lines.pop()
    return line, "\n".join(lines), n


def payload_key(line, widget):
    """Impronta del contenuto senza `t` e senza la riga R: decide se pubblicare.
    R scorre da sola ogni 15 min anche a Mac fermo; il suo ultimo valore è `s` (già nella
    riga) e il resto arriva con il battito dei 30 min, quindi non vale una pubblicazione."""
    p = line.split(",")
    rest = [l for l in widget.split("\n")[1:] if not l.startswith("R,")]
    return hashlib.sha1((",".join(p[:4] + p[5:]) + "\n" + "\n".join(rest)).encode("utf-8")).hexdigest()


def publish_reason(state, key, now, gist):
    if state.get("last_hash") != key or (gist and state.get("gist") != gist):
        return "changed"
    last = state.get("last_publish") or 0
    if now - last >= HEARTBEAT - HEARTBEAT_SLACK or last > now + 300:
        return "heartbeat"
    return None


# ---- pubblicazione -------------------------------------------------------------

def gh_path():
    p = shutil.which("gh")
    if p:
        return p
    # launchd ha un PATH minimo.
    for c in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh",
              os.path.expanduser("~/.local/bin/gh"), "/snap/bin/gh"):
        if os.access(c, os.X_OK):
            return c
    raise RelayError("GitHub CLI 'gh' not found")


def publish(gist, files):
    body = json.dumps({"files": {name: {"content": c} for name, c in files.items()}}).encode("utf-8")
    cmd = [gh_path(), "api", "-X", "PATCH", "gists/" + gist, "--input", "-", "--silent"]
    err = "unknown error"
    for attempt in (0, 1):
        try:
            r = subprocess.run(cmd, input=body, capture_output=True, timeout=GH_TIMEOUT)
        except subprocess.TimeoutExpired:
            err = "gh timed out"
        except OSError as e:
            raise RelayError("gh: " + _err(e))
        else:
            if r.returncode == 0:
                return
            msg = (r.stderr or r.stdout or b"").decode("utf-8", "replace").strip().splitlines()
            err = _redact(msg[0][:200]) if msg else "gh exit %d" % r.returncode
            if re.search(r"HTTP 4(?!29)\d\d", err):
                break  # 404/401/422: un nuovo tentativo non cambia niente
        if attempt == 0:
            time.sleep(RETRY_DELAY)
    raise RelayError("publish failed: " + err)


# ---- stato, storia, lock, log ------------------------------------------------

def new_state():
    return {"version": 2, "rows": [], "migrated": None, "gist": None, "last_hash": None,
            "last_publish": 0, "last_reason": None, "last_read": 0, "last_line": None,
            "hint": None, "last_error": None, "backoff_until": 0}


def _num(v):
    # Numeri compatti nel JSON (39 invece di 39.0).
    if v is None:
        return None
    v = round(float(v), 2)
    return int(v) if v.is_integer() else v


def _row(r):
    if not isinstance(r, list) or len(r) != 5 or not isinstance(r[0], (int, float)):
        return None
    try:
        return [int(r[0]), _num(_f(r[1])), int(r[2] or 0) if r[1] is not None else None,
                _num(_f(r[3])), int(r[4] or 0)]
    except (TypeError, ValueError):
        return None


def _int(v, default):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return default
    return int(v)


def load_state(data_dir):
    path = os.path.join(data_dir, STATE_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except FileNotFoundError:
        return new_state()
    except (OSError, ValueError):
        try:
            os.replace(path, path + ".bad")  # tengo la copia rotta per capire cosa è successo
        except OSError:
            pass
        return new_state()
    if not isinstance(st, dict):
        return new_state()
    base = new_state()
    for key in ("version", "last_publish", "last_read", "backoff_until"):
        base[key] = _int(st.get(key), base[key])
    base["hint"] = _int(st.get("hint"), None)
    base["migrated"] = _int(st.get("migrated"), None)
    for key in ("gist", "last_hash", "last_reason", "last_line"):
        base[key] = st[key] if isinstance(st.get(key), str) else None
    base["last_error"] = st["last_error"] if isinstance(st.get("last_error"), dict) else None
    if isinstance(st.get("coach_error"), dict):
        base["coach_error"] = st["coach_error"]
    rows = [_row(r) for r in st["rows"]] if isinstance(st.get("rows"), list) else []
    base["rows"] = sorted((r for r in rows if r and r[3] is not None), key=lambda r: r[0])
    return base


def save_state(data_dir, st):
    path = os.path.join(data_dir, STATE_FILE)
    tmp = path + ".tmp"
    # 0600: la cache e lo stato contengono nomi di progetto e titoli delle sessioni.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(st, f, separators=(",", ":"))
    os.replace(tmp, path)


def prune(rows, now):
    cutoff = now - KEEP_DAYS * 86400
    return [r for r in rows if cutoff <= r[0] <= now + 300][-MAX_ROWS:]


def add_row(rows, row):
    if rows and rows[-1][0] >= row[0]:
        rows = [r for r in rows if r[0] != row[0]]
        rows.append(row)
        rows.sort(key=lambda r: r[0])
        return rows
    rows.append(row)
    return rows


def migrate(state, data_dir, now, legacy=None, logs=None):
    """Importa una volta sola la history.jsonl di v1 (t, w, wr) e, dai log v1, s/sr."""
    if state.get("migrated"):
        return 0
    legacy = V1_HISTORY if legacy is None else legacy
    logs = V1_LOGS if logs is None else logs
    src = os.path.join(data_dir, "history.jsonl")
    if not os.path.exists(src) and legacy:
        src = legacy
    found = {}
    try:
        with open(src, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    t = int(r["t"])
                    w = _f(r.get("w"))
                    wr = int(r.get("wr") or 0)
                except (ValueError, KeyError, TypeError):
                    continue
                if w is not None:
                    found[t] = [t, None, None, _num(w), wr]
    except OSError:
        pass
    if found:
        for lp in logs or ():
            try:
                # /tmp è scrivibile da tutti: leggo solo i log che sono miei.
                if hasattr(os, "getuid") and os.stat(lp).st_uid != os.getuid():
                    continue
                with open(lp, "rb") as f:
                    f.seek(0, 2)
                    f.seek(max(0, f.tell() - 5 * 1024 * 1024))
                    text = f.read().decode("utf-8", "replace")
            except OSError:
                continue
            for m in _V1_LOGLINE.finditer(text):
                s, sr, _, _, t = (int(x) for x in m.groups())
                for c in (t, t + 1, t - 1, t + 2, t - 2):
                    if c in found and found[c][1] is None:
                        found[c][1], found[c][2] = s, sr
                        break
    have = {r[0] for r in state["rows"]}
    rows = state["rows"] + [r for t, r in found.items() if t not in have]
    rows.sort(key=lambda r: r[0])
    state["rows"] = prune(rows, now)
    state["migrated"] = now
    return len(found)


class Lock:
    """Un solo relay alla volta (flock: si libera da solo se il processo muore)."""

    def __init__(self, path):
        self.path = path
        self.fd = None

    def acquire(self):
        if fcntl is None:
            return True
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def release(self):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


def _trim_log(path):
    with open(path, "r+b") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - LOG_KEEP))
        tail = f.read()
        nl = tail.find(b"\n")
        if nl >= 0:
            tail = tail[nl + 1:]
        f.seek(0)
        # Riscrivo sul posto: launchd tiene aperto lo stesso file in append.
        f.write(b"... log trimmed ...\n" + tail)
        f.truncate()


class Log:
    def __init__(self, path=None):
        self.path = path

    def _write(self, msg, err):
        line = "%s %s" % (datetime.now().isoformat(timespec="seconds"), _redact(msg))
        if self.path:
            try:
                d = os.path.dirname(self.path)
                if d:
                    os.makedirs(d, exist_ok=True)
                if os.path.exists(self.path) and os.path.getsize(self.path) > LOG_CAP:
                    _trim_log(self.path)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                return
            except OSError:
                pass
        print(line, file=sys.stderr if err else sys.stdout)

    def info(self, msg):
        self._write(msg, False)

    def error(self, msg):
        self._write(msg, True)


def note_error(state, log, msg, now, always=False, key="last_error"):
    """Registra l'errore; nel log lo stesso messaggio compare al massimo una volta l'ora
    (Mac offline = un errore ogni 5 min, inutile riempire il log)."""
    msg = _redact(msg)
    prev = state.get(key) if isinstance(state.get(key), dict) else {}
    logged = prev.get("logged") if isinstance(prev.get("logged"), (int, float)) else 0
    if always or prev.get("msg") != msg or now - logged >= 3600:
        log.error("error: " + msg)
        logged = now
    state[key] = {"t": now, "msg": msg, "logged": logged}


# ---- comandi -------------------------------------------------------------------

def _ago(t, now):
    d = int(now - t)
    if d < 0:
        return "in %s" % _span(-d)
    return "%s ago" % _span(d)


def _span(d):
    if d < 90:
        return "%ds" % d
    if d < 5400:
        return "%dm" % round(d / 60)
    if d < 172800:
        return "%dh%02d" % (d // 3600, d % 3600 // 60)
    return "%dd" % (d // 86400)


def _when(t):
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")


def status(data_dir, out=sys.stdout):
    st = load_state(data_dir)
    now = time.time()
    p = lambda *a: print(*a, file=out)
    p("Reiatsu relay %s   data: %s" % (VERSION, data_dir))
    p("gist:          %s" % (st.get("gist") or "-"))
    if st.get("last_read"):
        p("last read:     %s (%s)  %s" % (_when(st["last_read"]), _ago(st["last_read"], now), st.get("last_line") or ""))
    else:
        p("last read:     never")
    if st.get("last_publish"):
        nxt = st["last_publish"] + HEARTBEAT - now
        p("last publish:  %s (%s, %s); heartbeat %s" % (
            _when(st["last_publish"]), _ago(st["last_publish"], now), st.get("last_reason") or "?",
            "due" if nxt <= 0 else "in " + _span(nxt)))
    else:
        p("last publish:  never")
    p("poll hint n:   %s" % ("%d s" % st["hint"] if st.get("hint") else "-"))
    err = st.get("last_error")
    if isinstance(err, dict) and err.get("t"):
        p("last error:    %s (%s): %s" % (_when(err["t"]), _ago(err["t"], now), err.get("msg")))
    else:
        p("last error:    none")
    if (st.get("backoff_until") or 0) > now:
        p("backoff:       API paused until %s (rate limited)" % _when(st["backoff_until"]))
    rows = st["rows"]
    if rows:
        p("history:       %d readings since %s" % (len(rows), _when(rows[0][0])))
    return 0


def run(mode, gist, data_dir, log_path=None, out=sys.stdout):
    log = Log(log_path)
    os.makedirs(data_dir, mode=0o700, exist_ok=True)
    lock = Lock(os.path.join(data_dir, LOCK_FILE))
    if not lock.acquire():
        if mode != "publish":
            print("another relay run is in progress", file=sys.stderr)
            return 1
        return 0
    try:
        state = load_state(data_dir)
        started = int(time.time())
        migrated = migrate(state, data_dir, started)
        if migrated and mode != "print":
            log.info("imported %d readings from v1 history" % migrated)
        if mode == "publish" and (state.get("backoff_until") or 0) > started:
            return 0  # 429 recente: lascio in pace l'API, il gist tiene gli ultimi dati
        try:
            usage = get_usage()
        except RelayError as e:
            now = int(time.time())
            if e.code == 429:
                state["backoff_until"] = now + min(3600, max(60, e.retry_after or BACKOFF_DEFAULT))
            note_error(state, log, str(e), now, always=mode != "publish")
            if mode != "print":
                save_state(data_dir, state)
            return 1
        now = int(time.time())
        rd = read_usage(usage)
        row = [now, _num(rd["s"]), rd["sr"], _num(rd["w"]), rd["wr"]]
        rows = prune(add_row(list(state["rows"]), row), now)
        summ, activity = None, None
        try:
            sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
            import token_coach
            ss, info = token_coach.scan(cache=os.path.join(data_dir, COACH_CACHE), write=mode != "print")
            summ, activity = token_coach.summary(ss), info.get("activity")
            state.pop("coach_error", None)
        except Exception as e:  # il widget deve funzionare anche senza consigli
            note_error(state, log, "token_coach: " + _err(e), now, key="coach_error")
        line, widget, n = build(usage, rows, now, summ, activity)
        if mode == "print":
            print(json.dumps(usage, indent=2), file=out)
            print(line, file=out)
            print(widget, file=out)
            return 0
        key = payload_key(line, widget)
        reason = publish_reason(state, key, now, gist)
        state.update(rows=rows, last_read=now, last_line=line, hint=n, backoff_until=0)
        if mode == "dry-run":
            print("usage.txt (%d bytes)\n%s\n" % (len(line.encode("utf-8")), line), file=out)
            print("widget.txt (%d bytes)\n%s\n" % (len(widget.encode("utf-8")), widget), file=out)
            print("decision: %s" % ("would publish (%s)" % reason if reason else
                                    "would skip (unchanged; heartbeat %s)" % _ago(
                                        (state.get("last_publish") or 0) + HEARTBEAT, now)), file=out)
            save_state(data_dir, state)
            return 0
        if reason:
            try:
                publish(gist, {GIST_FILE: line, WIDGET_FILE: widget})
            except RelayError as e:
                note_error(state, log, str(e), now)
                save_state(data_dir, state)
                return 1
            state.update(last_hash=key, last_publish=now, last_reason=reason, gist=gist)
            log.info("published (%s) %s" % (reason, line))
        save_state(data_dir, state)
        return 0
    finally:
        lock.release()


def parse_args(argv):
    ap = argparse.ArgumentParser(
        prog="claude_usage_relay.py", add_help=True,
        description="Publishes your Claude plan usage to a secret gist for the Garmin watch apps.")
    ap.add_argument("gist", nargs="?", help="gist id to publish to")
    m = ap.add_mutually_exclusive_group()
    m.add_argument("--print", dest="mode", action="store_const", const="print",
                   help="print the API answer and both files, save nothing")
    m.add_argument("--dry-run", dest="mode", action="store_const", const="dry-run",
                   help="everything except publishing; prints both files")
    m.add_argument("--status", dest="mode", action="store_const", const="status",
                   help="last publish, last error, next poll hint")
    ap.add_argument("--data-dir", default=os.path.dirname(os.path.realpath(__file__)),
                    help="folder for state.json and caches (default: next to this script)")
    ap.add_argument("--log", help="append publishes and errors to this file (size-capped)")
    ap.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    data_dir = os.path.abspath(os.path.expanduser(args.data_dir))
    mode = args.mode or "publish"
    if mode == "status":
        return status(data_dir)
    gist = args.gist
    if not gist:
        try:
            with open(os.path.join(data_dir, GIST_ID_FILE)) as f:
                gist = f.read().strip() or None
        except OSError:
            gist = None
    if gist and not _GIST_ID.match(gist):
        print("invalid gist id: %r" % gist, file=sys.stderr)
        return 2
    if mode == "publish" and not gist:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        return run(mode, gist, data_dir, args.log)
    except Exception as e:
        # Ultima rete: una riga nel log, niente traceback con dati sensibili.
        Log(args.log).error("error: unexpected %s" % _err(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
