"""token_coach: il parse incrementale deve dare esattamente gli stessi aggregati
di un parse completo (niente rete, niente file reali dell'utente)."""
import io
import json
import os
import random
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import token_coach as tc  # noqa: E402

PROJ = "-Users-mario-Desktop-My-App"
SID = "11111111-2222-3333-4444-555555555555"
BASE = int(time.time()) - 3600


def ts(i):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(BASE + i)) + ".%03dZ" % (i % 1000)


def user(i, text, **kw):
    d = {"type": "user", "timestamp": ts(i), "cwd": "/Users/mario/Desktop/My App",
         "message": {"role": "user", "content": text}}
    d.update(kw)
    return d


def tool_result(i, size):
    return {"type": "user", "timestamp": ts(i),
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "x", "content": "y" * size}]}}


def asst(i, mid, inp=10, cr=1000, cw=200, out=50, model="claude-opus-5-5", side=False):
    return {"type": "assistant", "timestamp": ts(i), "isSidechain": side,
            "message": {"id": mid, "model": model, "role": "assistant",
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": inp, "cache_read_input_tokens": cr,
                                  "cache_creation_input_tokens": cw, "output_tokens": out}}}


def enc(lines):
    return "".join(json.dumps(d, ensure_ascii=False) + "\n" for d in lines).encode("utf-8")


class CoachBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="coach-test-")
        self.root = os.path.join(self.tmp, "projects")
        self.pdir = os.path.join(self.root, PROJ)
        os.makedirs(self.pdir)
        self.main = os.path.join(self.pdir, SID + ".jsonl")
        self.cache = os.path.join(self.tmp, "cache.json")
        self.tick = 0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, path, data, mode="ab"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, mode) as f:
            f.write(data)
        # mtime sempre diverso, anche se due scritture cadono nello stesso istante
        self.tick += 1
        t = time.time() - 100 + self.tick
        os.utime(path, (t, t))

    def both(self):
        full = tc.scan(cache=None, projects=self.root)
        inc = tc.scan(cache=self.cache, projects=self.root)
        self.assertEqual(inc[0], full[0])
        self.assertEqual(inc[1]["activity"], full[1]["activity"])
        return inc


class TestIncrementalEqualsFull(CoachBase):
    def test_append_partial_shrink_subagents(self):
        self.write(self.main, enc([
            {"type": "custom-title", "customTitle": "My title", "sessionId": SID},
            user(1, "fix the login bug"),
            user(2, "<local-command-stdout>noise</local-command-stdout>"),
            user(3, "Caveat: meta", isMeta=True),
            asst(4, "m1", out=10), asst(5, "m1", out=40),        # stessa risposta su 2 righe
            tool_result(6, 50000),
            asst(7, "m2", cr=150000),
        ]))
        ss, info = self.both()
        self.assertEqual(len(ss), 1)
        s = ss[0]
        self.assertEqual(s["calls"], 2)                # m1 contata una volta sola
        self.assertEqual(s["output"], 40 + 50)         # massimo per messaggio, non somma
        self.assertEqual(s["prompts"], 1)              # niente meta / output di comandi
        self.assertEqual(s["big_results"], 1)
        self.assertEqual(s["peak_ctx"], 10 + 150000 + 200)
        self.assertEqual(s["title"], "My title")
        self.assertEqual(s["project"], "My App")       # da cwd, non dal nome della cartella
        self.assertEqual(info["parsed"], 1)

        # append di righe complete, con un messaggio a cavallo del confine
        self.write(self.main, enc([asst(8, "m3", out=5)]))
        self.both()
        self.write(self.main, enc([asst(9, "m3", out=25), user(10, "now add tests")]))
        ss, info = self.both()
        self.assertEqual(ss[0]["calls"], 3)
        self.assertEqual(ss[0]["output"], 40 + 50 + 25)
        self.assertEqual(info["parsed"], 1)

        # riga parziale: ignorata finché non è completa (da entrambi i parse)
        tail = json.dumps(asst(11, "m4", out=7)).encode()
        self.write(self.main, tail[:30])
        ss, _ = self.both()
        self.assertEqual(ss[0]["calls"], 3)
        self.write(self.main, tail[30:] + b"\n")
        ss, _ = self.both()
        self.assertEqual(ss[0]["calls"], 4)

        # nessun cambiamento: niente da rileggere
        _, info = tc.scan(cache=self.cache, projects=self.root)
        self.assertEqual(info["parsed"], 0)

        # subagent annidati: uniti alla sessione madre; journal.jsonl ignorato
        sub1 = os.path.join(self.pdir, SID, "subagents", "agent-a1.jsonl")
        sub2 = os.path.join(self.pdir, SID, "subagents", "workflows", "wf_x", "agent-b2.jsonl")
        jour = os.path.join(self.pdir, SID, "subagents", "workflows", "wf_x", "journal.jsonl")
        self.write(sub1, enc([user(12, "task for agent", isSidechain=True),
                              asst(13, "s1", cr=500000, side=True)]))
        self.write(sub2, enc([asst(14, "s2", side=False, model="claude-haiku")]))  # forzato sidechain
        self.write(jour, b'{"type":"launched"}\n')
        ss, _ = self.both()
        self.assertEqual(len(ss), 1)
        s = ss[0]
        self.assertEqual(s["subagents"], 2)
        self.assertEqual(s["calls"], 6)
        self.assertEqual(s["subagent_calls"], 2)
        self.assertEqual(s["prompts"], 2)               # i prompt dei subagent non contano
        self.assertEqual(s["peak_ctx"], 150210)         # il contesto dei subagent non è la sessione
        self.assertEqual(s["models"].get("claude-haiku"), 1)

        self.write(sub2, enc([asst(15, "s3", side=True)]))
        self.both()

        # file accorciato (riscritto più corto): parse completo
        self.write(self.main, enc([user(1, "short"), asst(2, "z1")]), mode="wb")
        ss, _ = self.both()
        self.assertEqual(ss[0]["calls"], 1 + 3)
        self.assertIsNone(ss[0]["title"])

        # riscritto con la stessa lunghezza ma contenuto diverso: la coda non torna
        with open(self.main, "rb") as f:
            data = f.read()
        self.write(self.main, data.replace(b'"z1"', b'"z2"'), mode="wb")
        self.both()
        self.write(self.main, enc([asst(3, "z3")]))
        self.both()

    def test_random_chunks(self):
        # Scritture a pezzi casuali (anche a metà riga): dopo ogni pezzo incrementale == completo.
        rnd = random.Random(1234)
        lines = []
        for i in range(400):
            k = rnd.random()
            mid = "r%d" % (i // 3)  # gruppi di righe con lo stesso id, come lo streaming
            if k < 0.55:
                lines.append(asst(i, mid, inp=rnd.randint(0, 50), cr=rnd.randint(0, 200000),
                                  cw=rnd.randint(0, 5000), out=rnd.randint(0, 900),
                                  side=rnd.random() < 0.2,
                                  model=rnd.choice(["claude-opus-5-5", "claude-sonnet-5", "x"])))
            elif k < 0.75:
                lines.append(user(i, rnd.choice(["do it", "<task-notification>x", "ok, next", "[Request interrupted"])))
            elif k < 0.8:
                lines.append(tool_result(i, rnd.choice([10, 45000])))
            elif k < 0.85:
                lines.append({"type": "ai-title", "aiTitle": "t%d" % i})
            else:
                lines.append({"type": "attachment", "timestamp": ts(i)})
        data = enc(lines)
        sub = os.path.join(self.pdir, SID, "subagents", "agent-z.jsonl")
        subdata = enc([asst(1000 + i, "q%d" % (i // 2), side=True) for i in range(60)])
        pos = spos = 0
        while pos < len(data) or spos < len(subdata):
            n = rnd.randint(1, 4000)
            if pos < len(data):
                self.write(self.main, data[pos:pos + n])
                pos += n
            if rnd.random() < 0.3 and spos < len(subdata):
                self.write(sub, subdata[spos:spos + n // 3 + 1])
                spos += n // 3 + 1
            self.both()
        ss, _ = self.both()
        self.assertEqual(ss[0]["subagents"], 1)
        self.assertEqual(ss[0]["calls"], len({d["message"]["id"] for d in lines if d["type"] == "assistant"}) + 30)

    def test_garbage_and_bad_utf8(self):
        self.write(self.main, enc([user(1, "ciao è già"), asst(2, "a")]) + b"not json\n"
                   + b'{"type":"user","message":{"content":"bad \xff\xfe utf8"},"timestamp":"' + ts(3).encode()
                   + b'"}\n' + b"[1,2]\n" + enc([asst(4, "b")]))
        ss, _ = self.both()
        self.assertEqual(ss[0]["calls"], 2)
        self.assertEqual(ss[0]["prompts"], 2)

    def test_corrupt_cache_is_ignored(self):
        self.write(self.main, enc([user(1, "hi"), asst(2, "a")]))
        self.both()
        with open(self.cache, "w") as f:
            f.write('{"v": 2, "files": {"%s": {"agg": 1}}}' % self.main)
        self.both()
        with open(self.cache, "w") as f:
            f.write("{broken")
        self.both()

    def test_old_sessions_and_cache_pruning(self):
        self.write(self.main, enc([user(1, "hi"), asst(2, "a")]))
        old = os.path.join(self.pdir, "old.jsonl")
        self.write(old, enc([asst(3, "o")]))
        t = time.time() - 30 * 86400
        os.utime(old, (t, t))
        ss, info = self.both()
        self.assertEqual([s["file"] for s in ss], [self.main])
        with open(self.cache) as f:
            self.assertNotIn(old, json.load(f)["files"])

    def test_metadata_touch_does_not_parse_old_subagents(self):
        # sessione vecchia nel contenuto ma "toccata" dalla CLI con righe senza timestamp
        line = asst(1, "a")
        line["timestamp"] = "2020-01-01T00:00:00.000Z"
        self.write(self.main, enc([line, {"type": "mode", "mode": "x", "sessionId": SID}]))
        sub = os.path.join(self.pdir, SID, "subagents", "agent-old.jsonl")
        self.write(sub, enc([asst(2, "s", side=True)]))
        t = time.time() - 30 * 86400
        os.utime(sub, (t, t))
        ss, info = self.both()
        self.assertEqual(ss, [])
        self.assertEqual(info["files"], 1)

    def test_activity_is_last_timestamp_not_mtime(self):
        self.write(self.main, enc([user(1, "hi"), asst(2, "a")]))
        _, info = tc.scan(projects=self.root)
        self.assertAlmostEqual(info["activity"], tc.parse_ts(ts(2)), places=3)


class TestPublicApi(CoachBase):
    def test_summary_tips_and_cli(self):
        lines = [user(1, "first question about the app")]
        for i in range(30):
            lines.append(asst(2 + i, "m%d" % i, cr=160000 + i))
        self.write(self.main, enc(lines))
        ss = tc.sessions(projects=self.root)
        summ = tc.summary(ss)
        self.assertEqual(summ["sessions"], 1)
        self.assertEqual(summ["last"]["title"], "first question about the app")
        self.assertEqual(summ["last"]["calls"], 30)
        self.assertTrue(summ["last"]["tip"].startswith("Context at 160k"))
        self.assertTrue(all(set(t) == {"short", "long"} for t in summ["tips"]))
        self.assertTrue(any("ctx: /clear" in t["short"] for t in summ["tips"]))
        self.assertEqual(tc.tips([]), [])
        self.assertEqual(tc.summary([])["last"], None)
        for args in (["--json", "--no-cache"], ["--no-cache"], ["--no-cache", "--days", "3"]):
            buf = io.StringIO()
            with mock.patch.object(tc, "PROJECTS", self.root), redirect_stdout(buf):
                self.assertEqual(tc.main(args), 0)
            if "--json" in args:
                self.assertEqual(json.loads(buf.getvalue())["sessions"], 1)
            else:
                self.assertIn("sessions in the last", buf.getvalue())

    def test_parse_ts_variants(self):
        self.assertEqual(tc.parse_ts("2026-10-21T15:00:11Z"), 1792594811.0)
        self.assertEqual(tc.parse_ts("2026-10-21T15:00:11.5Z"), 1792594811.0)
        self.assertAlmostEqual(tc.parse_ts("2026-10-21T15:00:11.123Z"), 1792594811.123, places=3)
        self.assertIsNone(tc.parse_ts("yesterday"))
        self.assertIsNone(tc.parse_ts(None))


if __name__ == "__main__":
    unittest.main()
