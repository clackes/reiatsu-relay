#!/usr/bin/env python3
"""Analyzes your local Claude Code sessions (~/.claude/projects) and produces
short tips for not wasting your plan usage.

Subagent transcripts (<project>/<session>/subagents/**/*.jsonl) are merged
into their parent session. Results are cached per file, so later runs only
parse the bytes appended since the previous run.

Usage:  token_coach.py               readable report
        token_coach.py --json        data for the relay / widget
        token_coach.py --no-cache    parse everything from scratch
        token_coach.py --days N      look back N days (default 7)
"""
import json
import os
import re
import sys
import time
from datetime import datetime

PROJECTS = os.path.expanduser("~/.claude/projects")
DAYS = 7
CACHE_FILE = "coach_cache.json"
# Va incrementata quando cambia la logica di _fold: invalida le cache vecchie.
CACHE_VERSION = 2
BIG_RESULT = 40000          # ~10k token: un output di tool "enorme"
RECENT_IDS = 4              # le righe di uno stesso messaggio sono contigue: 4 bastano
TAIL = 32                   # byte prima dell'offset usati per riconoscere un file riscritto
_FIELDS = ("input_tokens", "cache_read_input_tokens",
           "cache_creation_input_tokens", "output_tokens")
# Testi "user" che non sono prompt scritti dall'utente.
_NOT_PROMPT = ("<task-notification", "<local-command", "<system-reminder", "<bash-stdout",
               "<bash-stderr", "<user-prompt-submit-hook", "[Request interrupted", "Caveat:")
_FRACTION = re.compile(r"^(.*T\d\d:\d\d:\d\d)\.(\d+)(.*)$")


def parse_ts(s):
    if not isinstance(s, str) or not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        pass
    # Python 3.9 accetta solo 3 o 6 cifre decimali: le porto a 6, cosi' il risultato
    # e' lo stesso delle versioni nuove (che accettano qualsiasi numero di cifre).
    m = _FRACTION.match(s)
    if m:
        try:
            frac = (m.group(2) + "000000")[:6]
            return datetime.fromisoformat(m.group(1) + "." + frac + m.group(3)).timestamp()
        except ValueError:
            pass
    return None


def project_name(dirname):
    # Ripiego quando manca cwd: "-Users-mario-Desktop-Garmin" -> "Garmin"
    parts = [p for p in dirname.split("-") if p]
    return parts[-1] if parts else dirname


def _num(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def _new_agg():
    return {
        "start": None, "end": None,
        "prompts": 0, "calls": 0, "peak_ctx": 0, "last_ctx": 0,
        "output": 0, "cache_write": 0, "cache_read": 0, "fresh_input": 0,
        "models": {}, "compacts": 0, "subagent_calls": 0, "big_results": 0,
        "title": None, "ai_title": None, "first_prompt": None, "cwd": None,
        "recent": [],
    }


_AGG_KEYS = frozenset(_new_agg())


def _is_prompt(text):
    t = text.lstrip()
    return bool(t) and not t.startswith(_NOT_PROMPT)


def _fold(agg, d, raw_len, sidechain):
    """Aggiunge una riga del transcript agli aggregati del file.

    È una fold pura su righe complete: lo stato sta tutto in `agg` (anche gli
    id recenti per la deduplica), quindi parse incrementale == parse completo."""
    t = parse_ts(d.get("timestamp"))
    if t is not None:
        if agg["start"] is None or t < agg["start"]:
            agg["start"] = t
        if agg["end"] is None or t > agg["end"]:
            agg["end"] = t
    if agg["cwd"] is None:
        cwd = d.get("cwd")
        if isinstance(cwd, str) and cwd:
            agg["cwd"] = cwd
    typ = d.get("type")
    if typ == "custom-title":
        v = d.get("customTitle")
        if isinstance(v, str) and v.strip():
            agg["title"] = v.strip()
        return
    if typ == "ai-title":
        v = d.get("aiTitle")
        if isinstance(v, str) and v.strip():
            agg["ai_title"] = v.strip()
        return
    msg = d.get("message")
    if not isinstance(msg, dict):
        return
    side = sidechain or bool(d.get("isSidechain"))
    if typ == "user":
        content = msg.get("content")
        if d.get("isCompactSummary"):
            agg["compacts"] += 1
        elif not side and not d.get("isMeta"):
            text = None
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str):
                        text = c["text"]
                        break
            if text is not None and _is_prompt(text):
                agg["prompts"] += 1
                if agg["first_prompt"] is None and not text.lstrip().startswith("<"):
                    agg["first_prompt"] = text.strip().splitlines()[0][:60]
        # Un tool_result non può essere più lungo della riga: evito dumps inutili.
        if raw_len > BIG_RESULT and isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    if len(json.dumps(c.get("content", ""))) > BIG_RESULT:
                        agg["big_results"] += 1
        return
    if typ != "assistant":
        return
    u = msg.get("usage")
    if not isinstance(u, dict):
        return
    vals = [_num(u.get(f)) for f in _FIELDS]
    mid = msg.get("id")
    prev = None
    if isinstance(mid, str) and mid:
        for item in agg["recent"]:
            if item[0] == mid:
                prev = item
                break
    if prev is None:
        # Una risposta API = una chiamata, anche se è scritta su più righe.
        old = [0, 0, 0, 0]
        agg["calls"] += 1
        if side:
            agg["subagent_calls"] += 1
        m = msg.get("model")
        m = m if isinstance(m, str) and m else "?"
        agg["models"][m] = agg["models"].get(m, 0) + 1
        if isinstance(mid, str) and mid:
            agg["recent"].append([mid, vals])
            if len(agg["recent"]) > RECENT_IDS:
                del agg["recent"][0]
    else:
        # Righe successive dello stesso messaggio: i contatori crescono (streaming), mai sommarli due volte.
        old = prev[1]
        vals = [max(a, b) for a, b in zip(old, vals)]
        prev[1] = vals
    agg["fresh_input"] += vals[0] - old[0]
    agg["cache_read"] += vals[1] - old[1]
    agg["cache_write"] += vals[2] - old[2]
    agg["output"] += vals[3] - old[3]
    if not side:
        ctx = vals[0] + vals[1] + vals[2]
        if ctx > agg["peak_ctx"]:
            agg["peak_ctx"] = ctx
        agg["last_ctx"] = ctx


def _parse_from(path, agg, offset, sidechain):
    """Legge le righe complete da `offset`; ritorna (nuovo offset, coda)."""
    pos = offset
    with open(path, "rb") as f:
        f.seek(offset)
        for raw in f:
            if not raw.endswith(b"\n"):
                break  # riga ancora in scrittura: la riprendo al prossimo giro
            pos += len(raw)
            try:
                d = json.loads(raw)
            except ValueError:
                try:
                    d = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
            if isinstance(d, dict):
                _fold(agg, d, len(raw), sidechain)
        f.seek(max(0, pos - TAIL))
        tail = f.read(pos - max(0, pos - TAIL))
    return pos, tail.hex()


def _file_agg(path, st, old, sidechain):
    """Aggregati di un file, riusando/estendendo la voce di cache `old`."""
    mtime = st.st_mtime_ns
    if old is not None and not (isinstance(old, dict) and isinstance(old.get("agg"), dict)
                                and isinstance(old.get("off"), int) and _AGG_KEYS <= set(old["agg"])):
        old = None  # voce di cache malformata: riparto da zero
    if (old and old.get("side") == sidechain and old.get("size") == st.st_size
            and old.get("mtime") == mtime):
        return old, False
    agg = None
    offset = 0
    if old and old.get("side") == sidechain and 0 < old.get("off", 0) <= st.st_size:
        # Solo append: verifico che i byte prima dell'offset siano gli stessi.
        off = old["off"]
        try:
            with open(path, "rb") as f:
                f.seek(max(0, off - TAIL))
                same = f.read(off - max(0, off - TAIL)).hex() == old.get("tail")
        except OSError:
            same = False
        if same:
            agg = json.loads(json.dumps(old["agg"]))  # copia: la voce vecchia resta intatta
            offset = off
    if agg is None:
        agg = _new_agg()  # file nuovo, accorciato o riscritto: parse completo
    try:
        pos, tail = _parse_from(path, agg, offset, sidechain)
    except (TypeError, KeyError, AttributeError, IndexError):
        if not offset:
            raise
        agg = _new_agg()  # voce di cache con tipi sbagliati: la scarto e rileggo il file
        pos, tail = _parse_from(path, agg, 0, sidechain)
    return {"size": st.st_size, "mtime": mtime, "off": pos, "tail": tail,
            "side": sidechain, "agg": agg}, True


def analyze_file(path, sidechain=False):
    """Aggregati di un solo transcript, senza cache."""
    agg = _new_agg()
    _parse_from(path, agg, 0, sidechain)
    return agg


def _merge(main_path, project_dir, aggs, n_sub):
    main = aggs[0]
    s = {
        "file": main_path,
        "project": os.path.basename(main["cwd"].rstrip("/")) if main["cwd"] else project_name(project_dir),
        "start": None, "end": None,
        "prompts": 0, "calls": 0, "peak_ctx": 0, "last_ctx": main["last_ctx"],
        "output": 0, "cache_write": 0, "cache_read": 0, "fresh_input": 0,
        "models": {}, "compacts": 0, "subagent_calls": 0, "big_results": 0,
        "title": main["title"] or main["ai_title"], "first_prompt": main["first_prompt"],
        "subagents": n_sub,
    }
    for a in aggs:
        if a["start"] is not None and (s["start"] is None or a["start"] < s["start"]):
            s["start"] = a["start"]
        if a["end"] is not None and (s["end"] is None or a["end"] > s["end"]):
            s["end"] = a["end"]
        for key in ("prompts", "calls", "output", "cache_write", "cache_read", "fresh_input",
                    "compacts", "subagent_calls", "big_results"):
            s[key] += a[key]
        s["peak_ctx"] = max(s["peak_ctx"], a["peak_ctx"])
        for m, n in a["models"].items():
            s["models"][m] = s["models"].get(m, 0) + n
    # Peso approssimativo: ogni chiamata rilegge tutto il contesto.
    s["weight"] = s["cache_read"] + s["cache_write"] + s["fresh_input"] + 5 * s["output"]
    return s


def _load_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("v") == CACHE_VERSION and isinstance(data.get("files"), dict):
            return data["files"]
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def _save_cache(path, files):
    tmp = path + ".tmp"
    try:
        # 0600: dentro ci sono titoli e primi prompt delle sessioni.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"v": CACHE_VERSION, "files": files}, f, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _sub_stats(pdir, name):
    """Transcript dei subagent di una sessione con il loro stat, in ordine stabile."""
    sub = os.path.join(pdir, name[:-len(".jsonl")], "subagents")
    out = []
    if not os.path.isdir(sub):
        return out
    for dirpath, dirs, files in os.walk(sub):
        dirs.sort()
        for fn in sorted(files):
            # journal.jsonl è il diario del workflow, non un transcript.
            if fn.endswith(".jsonl") and fn != "journal.jsonl":
                p = os.path.join(dirpath, fn)
                try:
                    out.append((p, os.stat(p), True))
                except OSError:
                    pass
    return out


def scan(days=DAYS, cache=None, projects=None, now=None, write=True):
    """Ritorna (sessions, info). `cache` = percorso del file cache o None; write=False la legge soltanto."""
    root = projects or PROJECTS
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    old = _load_cache(cache) if cache else {}
    new = {}
    dirty = False
    parsed = 0
    activity = None
    out = []
    try:
        pdirs = sorted(e.name for e in os.scandir(root) if e.is_dir())
    except OSError:
        pdirs = []
    for proj in pdirs:
        pdir = os.path.join(root, proj)
        try:
            entries = sorted((e.name, e.stat()) for e in os.scandir(pdir)
                             if e.name.endswith(".jsonl") and e.is_file())
        except OSError:
            continue
        for name, st in entries:
            if st.st_mtime < cutoff:
                continue
            main_path = os.path.join(pdir, name)
            aggs = []
            for path, fst, side in [(main_path, st, False)] + _sub_stats(pdir, name):
                if side and (aggs[0]["end"] or 0) < cutoff and fst.st_mtime < cutoff:
                    # La CLI aggiunge righe di metadati (senza timestamp) a sessioni
                    # vecchie: i loro subagent vecchi non servono a nessuno.
                    continue
                try:
                    entry, changed = _file_agg(path, fst, old.get(path), side)
                except OSError:
                    if not side:
                        break
                    continue
                if changed:
                    dirty = True
                    parsed += 1
                new[path] = entry
                a = entry["agg"]
                if a["end"] is not None and a["end"] <= now + 300 and (activity is None or a["end"] > activity):
                    activity = a["end"]
                aggs.append(a)
            if not aggs:
                continue
            s = _merge(main_path, proj, aggs, len(aggs) - 1)
            if s["calls"] and (s["end"] or 0) >= cutoff:
                out.append(s)
    if cache and write and (dirty or set(new) != set(old)):
        _save_cache(cache, new)
    out.sort(key=lambda s: (s["end"] or 0, s["file"]))
    return out, {"activity": activity, "files": len(new), "parsed": parsed}


def sessions(days=DAYS, cache=None, projects=None):
    return scan(days, cache, projects)[0]


def k(n):
    return f"{n / 1000:.0f}k" if n < 1_000_000 else f"{n / 1_000_000:.1f}M"


def tips(ss):
    """Returns a list of (priority, short text for the watch, long text)."""
    t = []
    if not ss:
        return t
    last = ss[-1]
    total_w = sum(s["weight"] for s in ss) or 1

    if last["peak_ctx"] > 150_000:
        t.append((10, f"Last session {k(last['peak_ctx'])} ctx: /clear",
                  f"Your last session ({last['project']}) reached {k(last['peak_ctx'])} tokens of context. "
                  "Every message re-reads the whole context: above 100k each question costs 5-10x more. "
                  "Use /clear when you switch to a different task, or /compact if you need to keep going."))
    elif last["peak_ctx"] > 100_000:
        t.append((6, f"Last session {k(last['peak_ctx'])}: try /compact",
                  f"Your last session is at {k(last['peak_ctx'])} of context. A /compact now makes it lighter."))

    huge = [s for s in ss if s["peak_ctx"] > 150_000]
    if len(huge) >= 3:
        t.append((8, f"{len(huge)} sessions >150k this week",
                  f"{len(huge)} of {len(ss)} sessions this week went past 150k of context. "
                  "Long sessions use far more than many short ones: one session per task."))

    heavy = max(ss, key=lambda s: s["weight"])
    share = heavy["weight"] / total_w
    if share > 0.3 and len(ss) > 2:
        t.append((7, f"{heavy['project']}: {share:.0%} of usage",
                  f"The session on '{heavy['project']}' alone is about {share:.0%} of this week's usage "
                  f"({heavy['calls']} calls, peak {k(heavy['peak_ctx'])})."))

    big = sum(s["big_results"] for s in ss)
    if big >= 5:
        t.append((5, f"{big} huge outputs: trim the logs",
                  f"{big} tool results above ~10k tokens (logs, whole files, dumps). "
                  "Ask to read only the lines you need (grep, head) instead of full files or logs."))

    sub = sum(s["subagent_calls"] for s in ss)
    calls = sum(s["calls"] for s in ss) or 1
    if sub / calls > 0.35:
        t.append((5, f"Subagents: {sub / calls:.0%} of calls",
                  f"{sub / calls:.0%} of calls come from subagents/workflows. Each one starts from scratch: "
                  "use them only for really broad searches."))

    opus = sum(n for s in ss for m, n in s["models"].items() if "opus" in m or "fable" in m)
    if opus / calls > 0.8 and calls > 200:
        t.append((4, "Simple task? Try Sonnet",
                  "Almost everything runs on the largest model. For small edits and quick questions "
                  "Sonnet or Haiku use much less of the weekly limit."))

    long_ = [s for s in ss if s["prompts"] > 60]
    if long_:
        t.append((3, f"{len(long_)} sessions with >60 prompts",
                  "Sessions with dozens of prompts pile up context: start a new session for each topic."))
    t.sort(key=lambda x: -x[0])
    return t


def session_title(s):
    return s["title"] or s["first_prompt"] or s["project"]


def session_tip(s):
    """The most useful tip for a single session: (short, long)."""
    sub_share = s["subagent_calls"] / max(1, s["calls"])
    if s["peak_ctx"] > 150_000:
        return (f"Context at {k(s['peak_ctx'])}: /clear before the next task",
                f"This session reached {k(s['peak_ctx'])} of context: every new prompt re-reads all of it. "
                "If you change topic use /clear, if you continue the same work use /compact.")
    if s["big_results"] >= 3:
        return (f"{s['big_results']} huge outputs: ask only for the lines you need",
                f"{s['big_results']} very large tool results (logs or whole files). Ask for targeted grep/head.")
    if sub_share > 0.4:
        return (f"Subagents {sub_share:.0%} of calls: use fewer",
                "Many calls come from subagents: each one starts from zero and costs.")
    if s["prompts"] > 40:
        return (f"{s['prompts']} prompts: one session per topic",
                "Many different prompts in the same session pile up useless context.")
    if s["peak_ctx"] > 100_000:
        return (f"Context at {k(s['peak_ctx'])}: a /compact now helps",
                f"The session is at {k(s['peak_ctx'])} of context: a /compact makes it lighter.")
    return ("Efficient session, keep it up",
            "Small context and few wasted calls: great usage.")


def summary(ss):
    last = ss[-1] if ss else None
    tip = session_tip(last) if last else None
    return {
        "sessions": len(ss),
        "last": None if not last else {
            "project": last["project"], "peak_ctx": last["peak_ctx"],
            "calls": last["calls"], "prompts": last["prompts"],
            "title": session_title(last),
            "tip": tip[0], "tip_long": tip[1],
        },
        "over150k": sum(1 for s in ss if s["peak_ctx"] > 150_000),
        "top": [{"project": s["project"], "peak_ctx": s["peak_ctx"], "weight": s["weight"]}
                for s in sorted(ss, key=lambda s: -s["weight"])[:5]],
        "tips": [{"short": a, "long": b} for _, a, b in tips(ss)],
    }


def _arg(argv, name, default):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "-h" in argv or "--help" in argv:
        print(__doc__.strip())
        return 0
    try:
        days = max(1, int(_arg(argv, "--days", DAYS)))
    except ValueError:
        print("--days needs a number", file=sys.stderr)
        return 2
    cache = None if "--no-cache" in argv else os.path.join(
        os.path.dirname(os.path.realpath(__file__)), CACHE_FILE)
    ss, info = scan(days, cache)
    if "--json" in argv:
        print(json.dumps(summary(ss), ensure_ascii=False, indent=1))
        return 0
    subs = sum(s["subagents"] for s in ss)
    print(f"Claude Code sessions in the last {days} days: {len(ss)}"
          + (f" (+{subs} subagent transcripts)" if subs else "") + "\n")
    for s in sorted(ss, key=lambda s: -s["weight"])[:10]:
        when = datetime.fromtimestamp(s["end"]).strftime("%a %d %H:%M") if s["end"] else "?"
        print(f"  {when}  {s['project'][:28]:28}  peak {k(s['peak_ctx']):>5}  "
              f"{s['calls']:>4} calls  {s['prompts']:>3} prompts  weight {k(s['weight'])}")
    print("\nTips:")
    for _, short, long_ in tips(ss):
        print(f"  • {long_}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
