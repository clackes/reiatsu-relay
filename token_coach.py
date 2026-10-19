#!/usr/bin/env python3
"""Analizza le sessioni locali di Claude Code (~/.claude/projects/*/*.jsonl)
e produce consigli brevi per non sprecare l'uso del piano.

Uso:  token_coach.py            (report leggibile)
      token_coach.py --json     (dati per relay / widget)
"""
import glob
import json
import os
import sys
import time
from datetime import datetime

PROJECTS = os.path.expanduser("~/.claude/projects")
# Lingua dei consigli brevi mostrati sull'orologio: "it" o "en".
LANG = os.environ.get("CLAUDE_FACE_LANG", "it")


def L(it, en):
    return en if LANG == "en" else it
DAYS = 7


def parse_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def project_name(dirname):
    # "-Users-mario-Desktop-Garmin" -> "Garmin"
    parts = [p for p in dirname.split("-") if p]
    return parts[-1] if parts else dirname


def analyze_session(path):
    s = {
        "file": path,
        "project": project_name(os.path.basename(os.path.dirname(path))),
        "start": None, "end": None,
        "prompts": 0, "calls": 0, "peak_ctx": 0, "last_ctx": 0,
        "output": 0, "cache_write": 0, "cache_read": 0, "fresh_input": 0,
        "models": {}, "compacts": 0, "subagent_calls": 0, "big_results": 0,
        "title": None, "first_prompt": None,
    }
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            ts = parse_ts(d.get("timestamp") or "")
            if ts:
                s["start"] = ts if s["start"] is None else min(s["start"], ts)
                s["end"] = ts if s["end"] is None else max(s["end"], ts)
            if d.get("type") == "custom-title" and d.get("customTitle"):
                s["title"] = d["customTitle"]
            if d.get("type") == "summary" or d.get("isCompactSummary"):
                s["compacts"] += 1
            msg = d.get("message") or {}
            if d.get("type") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    s["prompts"] += 1
                    if s["first_prompt"] is None and not content.startswith("<"):
                        s["first_prompt"] = content.strip().splitlines()[0][:60]
                elif isinstance(content, list):
                    if any(c.get("type") == "text" for c in content if isinstance(c, dict)):
                        s["prompts"] += 1
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "tool_result":
                            if len(json.dumps(c.get("content", ""))) > 40000:
                                s["big_results"] += 1
            u = msg.get("usage")
            if d.get("type") == "assistant" and u:
                ctx = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) \
                    + (u.get("cache_creation_input_tokens") or 0)
                if d.get("isSidechain"):
                    s["subagent_calls"] += 1
                else:
                    s["peak_ctx"] = max(s["peak_ctx"], ctx)
                    s["last_ctx"] = ctx
                s["calls"] += 1
                s["output"] += u.get("output_tokens") or 0
                s["cache_write"] += u.get("cache_creation_input_tokens") or 0
                s["cache_read"] += u.get("cache_read_input_tokens") or 0
                s["fresh_input"] += u.get("input_tokens") or 0
                m = msg.get("model") or "?"
                s["models"][m] = s["models"].get(m, 0) + 1
    # "Peso" approssimativo: ogni chiamata rilegge tutto il contesto.
    s["weight"] = s["cache_read"] + s["cache_write"] + s["fresh_input"] + 5 * s["output"]
    return s


def sessions(days=DAYS):
    cutoff = time.time() - days * 86400
    out = []
    for p in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
        if os.path.getmtime(p) < cutoff:
            continue
        s = analyze_session(p)
        if s["calls"] and (s["end"] or 0) >= cutoff:
            out.append(s)
    out.sort(key=lambda s: s["end"] or 0)
    return out


def k(n):
    return f"{n / 1000:.0f}k" if n < 1_000_000 else f"{n / 1_000_000:.1f}M"


def tips(ss):
    """Ritorna una lista di (priorità, testo corto per l'orologio, testo lungo)."""
    t = []
    if not ss:
        return t
    last = ss[-1]
    total_w = sum(s["weight"] for s in ss) or 1

    if last["peak_ctx"] > 150_000:
        t.append((10, L(f"Ultima sessione {k(last['peak_ctx'])} ctx: /clear", f"Last session {k(last['peak_ctx'])} ctx: /clear"),
                  f"La tua ultima sessione ({last['project']}) è arrivata a {k(last['peak_ctx'])} token di contesto. "
                  "Ogni messaggio rilegge tutto il contesto: oltre 100k ogni domanda costa 5-10 volte di più. "
                  "Usa /clear quando passi a un task diverso, o /compact se devi continuare."))
    elif last["peak_ctx"] > 100_000:
        t.append((6, L(f"Ultima sessione {k(last['peak_ctx'])}: valuta /compact", f"Last session {k(last['peak_ctx'])}: try /compact"),
                  f"L'ultima sessione è a {k(last['peak_ctx'])} di contesto. Un /compact ora la alleggerisce."))

    huge = [s for s in ss if s["peak_ctx"] > 150_000]
    if len(huge) >= 3:
        t.append((8, L(f"{len(huge)} sessioni >150k questa sett.", f"{len(huge)} sessions >150k this week"),
                  f"{len(huge)} sessioni su {len(ss)} questa settimana hanno superato i 150k di contesto. "
                  "Sessioni lunghe consumano molto più di tante sessioni corte: una sessione per task."))

    heavy = max(ss, key=lambda s: s["weight"])
    share = heavy["weight"] / total_w
    if share > 0.3 and len(ss) > 2:
        t.append((7, L(f"{heavy['project']}: {share:.0%} dell'uso", f"{heavy['project']}: {share:.0%} of usage"),
                  f"Da sola la sessione su '{heavy['project']}' vale circa il {share:.0%} dell'uso della settimana "
                  f"({heavy['calls']} chiamate, picco {k(heavy['peak_ctx'])})."))

    big = sum(s["big_results"] for s in ss)
    if big >= 5:
        t.append((5, L(f"{big} output enormi: limita i log", f"{big} huge outputs: trim the logs"),
                  f"{big} risultati di tool sopra ~10k token (log, file interi, dump). "
                  "Chiedi di leggere solo le righe utili (grep, head) invece di file o log completi."))

    sub = sum(s["subagent_calls"] for s in ss)
    calls = sum(s["calls"] for s in ss) or 1
    if sub / calls > 0.35:
        t.append((5, L(f"Subagent {sub / calls:.0%} delle chiamate", f"Subagents: {sub / calls:.0%} of calls"),
                  f"Il {sub / calls:.0%} delle chiamate viene da subagent/workflow. Ognuno riparte da zero: "
                  "usali solo per ricerche davvero ampie."))

    opus = sum(n for s in ss for m, n in s["models"].items() if "opus" in m or "fable" in m)
    if opus / calls > 0.8 and calls > 200:
        t.append((4, L("Task semplici? prova Sonnet", "Simple task? Try Sonnet"),
                  "Quasi tutto gira sul modello più grande. Per modifiche piccole e domande veloci "
                  "Sonnet o Haiku consumano molto meno del limite settimanale."))

    long_ = [s for s in ss if s["prompts"] > 60]
    if long_:
        t.append((3, L(f"{len(long_)} sessioni con >60 richieste", f"{len(long_)} sessions with >60 prompts"),
                  "Sessioni con decine di richieste accumulano contesto: apri una sessione nuova per ogni argomento."))
    t.sort(key=lambda x: -x[0])
    return t


def session_title(s):
    return s["title"] or s["first_prompt"] or s["project"]


def session_tip(s):
    """Il consiglio più utile per una singola sessione: (corto, lungo)."""
    sub_share = s["subagent_calls"] / max(1, s["calls"])
    if s["peak_ctx"] > 150_000:
        return (L(f"Contesto a {k(s['peak_ctx'])}: fai /clear prima del prossimo task", f"Context at {k(s['peak_ctx'])}: /clear before the next task"),
                f"Questa sessione è arrivata a {k(s['peak_ctx'])} di contesto: ogni nuova richiesta lo rilegge tutto. "
                "Se cambi argomento usa /clear, se continui lo stesso lavoro usa /compact.")
    if s["big_results"] >= 3:
        return (L(f"{s['big_results']} output enormi: chiedi solo le righe utili", f"{s['big_results']} huge outputs: ask only for the lines you need"),
                f"{s['big_results']} risultati di tool molto grandi (log o file interi). Chiedi grep/head mirati.")
    if sub_share > 0.4:
        return (L(f"Subagent {sub_share:.0%} delle chiamate: usali meno", f"Subagents {sub_share:.0%} of calls: use fewer"),
                "Molte chiamate vengono da subagent: ognuno ricomincia da zero e costa.")
    if s["prompts"] > 40:
        return (L(f"{s['prompts']} richieste: apri una sessione per argomento", f"{s['prompts']} prompts: one session per topic"),
                "Tante richieste diverse nella stessa sessione accumulano contesto inutile.")
    if s["peak_ctx"] > 100_000:
        return (L(f"Contesto a {k(s['peak_ctx'])}: un /compact ora aiuta", f"Context at {k(s['peak_ctx'])}: a /compact now helps"),
                f"La sessione è a {k(s['peak_ctx'])} di contesto: un /compact la alleggerisce.")
    return (L("Sessione efficiente, continua così", "Efficient session, keep it up"),
            "Contesto contenuto e poche chiamate inutili: ottimo uso.")


def summary(ss):
    return {
        "sessions": len(ss),
        "last": None if not ss else {
            "project": ss[-1]["project"], "peak_ctx": ss[-1]["peak_ctx"],
            "calls": ss[-1]["calls"], "prompts": ss[-1]["prompts"],
            "title": session_title(ss[-1]),
            "tip": session_tip(ss[-1])[0], "tip_long": session_tip(ss[-1])[1],
        },
        "over150k": sum(1 for s in ss if s["peak_ctx"] > 150_000),
        "top": [{"project": s["project"], "peak_ctx": s["peak_ctx"], "weight": s["weight"]}
                for s in sorted(ss, key=lambda s: -s["weight"])[:5]],
        "tips": [{"short": a, "long": b} for _, a, b in tips(ss)],
    }


def main():
    ss = sessions()
    if "--json" in sys.argv:
        print(json.dumps(summary(ss), ensure_ascii=False, indent=1))
        return
    print(f"Sessioni Claude Code negli ultimi {DAYS} giorni: {len(ss)}\n")
    for s in sorted(ss, key=lambda s: -s["weight"])[:10]:
        when = datetime.fromtimestamp(s["end"]).strftime("%a %d %H:%M") if s["end"] else "?"
        print(f"  {when}  {s['project'][:28]:28}  picco {k(s['peak_ctx']):>5}  "
              f"{s['calls']:>4} chiamate  {s['prompts']:>3} richieste  peso {k(s['weight'])}")
    print("\nConsigli:")
    for _, short, long_ in tips(ss):
        print(f"  • {long_}")


if __name__ == "__main__":
    main()
