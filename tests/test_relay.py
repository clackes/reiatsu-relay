"""Relay v2: formato payload, calcoli, decisione di pubblicazione, stato (niente rete, niente keychain)."""
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claude_usage_relay as r  # noqa: E402
import token_coach  # noqa: E402

NOW = 1_800_000_000
SR = NOW + 3 * 3600
WR = NOW + 3 * 86400


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123456+00:00")


def usage(s=40, w=20, extra=False, **kw):
    u = {"five_hour": {"utilization": s, "resets_at": iso(SR)},
         "seven_day": {"utilization": w, "resets_at": iso(WR)},
         "extra_usage": {"is_enabled": extra}}
    u.update(kw)
    return u


def rows_linear(n=24, step=300, s0=10, ds=1.0):
    """Righe [t, s, sr, w, wr] con s crescente linearmente fino a NOW."""
    return [[NOW - (n - 1 - i) * step, s0 + i * ds, SR, 20 + i * 0.1, WR] for i in range(n)]


class Payload(unittest.TestCase):
    def test_v1_compat_and_nine_fields(self):
        rows = rows_linear()
        line, widget, n = r.build(usage(), rows, NOW, None, None)
        p = line.split(",")
        self.assertEqual(len(p), 9)
        self.assertEqual([int(x) for x in p[:5]], [40, SR, 20, WR, NOW])  # primi 5 come v1
        self.assertEqual(int(p[5]), n)
        self.assertEqual(widget.split("\n")[0], line)
        self.assertLessEqual(len(widget.encode()), r.MAX_WIDGET)

    def test_flags(self):
        base = r.read_usage(usage())
        self.assertEqual(r.flags(base, NOW, None), 0)
        self.assertEqual(r.flags(r.read_usage(usage(s=100)), NOW, None), 1)
        self.assertEqual(r.flags(r.read_usage(usage(w=105)), NOW, None), 2)
        self.assertEqual(r.flags(base, NOW, NOW - 100), 4)
        self.assertEqual(r.flags(base, NOW, NOW - 2000), 0)
        self.assertEqual(r.flags(r.read_usage(usage(extra=True)), NOW, None), 8)
        self.assertEqual(r.flags(r.read_usage(usage(s=100, w=100, extra=True)), NOW, NOW), 15)
        # finestra già scaduta: niente flag limite
        expired = dict(r.read_usage(usage(s=100)), sr=NOW - 5)
        self.assertEqual(r.flags(expired, NOW, None), 0)

    def test_eta(self):
        rows = rows_linear(n=24, step=300, s0=10, ds=3.0)  # +3 % ogni 300 s
        cur = rows[-1][1]
        e = r.eta(rows, NOW, cur, SR, r.ETA_5H)
        self.assertGreater(e, NOW)
        self.assertLess(e, SR)
        self.assertEqual(e % 300, 0)
        exp = NOW + (100 - cur) / (3 / 300.0)
        self.assertLess(abs(e - exp), 600)
        # troppo pochi punti, già al 100 %, ritmo piatto: 0
        self.assertEqual(r.eta(rows[-2:], NOW, cur, SR, r.ETA_5H), 0)
        self.assertEqual(r.eta(rows, NOW, 100, SR, r.ETA_5H), 0)
        flat = [[t, 30, SR, 20, WR] for t, *_ in rows]
        self.assertEqual(r.eta(flat, NOW, 30, SR, r.ETA_5H), 0)
        # superiore al reset: 0
        slow = rows_linear(ds=0.01)
        self.assertEqual(r.eta(slow, NOW, slow[-1][1], SR, r.ETA_5H), 0)

    def test_eta_still_after_burst(self):
        # sale 30 min, poi fermo da 20 min: nessuna stima su un ritmo che non c'è più
        rows, v = [], 20
        for i in range(12, -1, -1):
            if i > 4:
                v += 3
            rows.append([NOW - i * 300, v, SR, 20, WR])
        self.assertEqual(r.eta(rows, NOW, v, SR, r.ETA_5H), 0)
        # ancora in salita nell'ultimo quarto d'ora: stima presente
        rows[-1][1] = v + 3
        self.assertGreater(r.eta(rows, NOW, v + 3, SR, r.ETA_5H), NOW)

    def test_hint(self):
        quiet = [[NOW - i * 300, 30, SR, 20, WR] for i in range(40, -1, -1)]  # 3h20 fermo
        self.assertEqual(r.hint(quiet, NOW, None), 3600)
        self.assertEqual(r.hint(quiet[-12:], NOW, None), 1800)  # solo 1 h di storia
        self.assertEqual(r.hint(quiet, NOW, NOW - 300), 900)  # Claude Code attivo
        hot = [[NOW - i * 300, 90 - i * 0.5, SR, 20, WR] for i in range(10, -1, -1)]
        self.assertEqual(r.hint(hot, NOW, None), 600)
        moving = [[NOW - i * 300, 30 - i, SR, 20, WR] for i in range(10, -1, -1)]
        self.assertEqual(r.hint(moving, NOW, None), 900)
        self.assertEqual(r.hint([], NOW, None), 900)

    def test_r_slots(self):
        rows = rows_linear()
        rv = r.r_values(rows, NOW)
        self.assertEqual(len(rv), 20)
        self.assertEqual(rv[-1], round(rows[-1][1]))
        self.assertEqual(r.r_values([[NOW - 86400, 5, SR, 1, WR]], NOW), [-1] * 20)
        # buco di 1 h nel mezzo -> -1 negli slot scoperti
        gap = [x for x in rows_linear(n=60) if not (NOW - 9000 < x[0] < NOW - 4500)]
        self.assertIn(-1, r.r_values(gap, NOW))
        # window scaduta -> 0
        old = [[NOW - 600, 70, NOW - 300, 5, WR], [NOW - 300, 70, NOW - 300, 5, WR]]
        self.assertEqual(r.r_values(old, NOW)[-1], 0)

    def test_r_line_needs_two_points(self):
        line, widget, _ = r.build(usage(), [[NOW + 100, 40, SR, 20, WR]], NOW + 100, None, None)
        self.assertFalse([x for x in widget.split("\n") if x.startswith("R,")])
        _, widget, _ = r.build(usage(), rows_linear(), NOW, None, None)
        rl = [x for x in widget.split("\n") if x.startswith("R,")]
        self.assertEqual(len(rl), 1)
        self.assertEqual(len(rl[0].split(",")), 21)

    def test_tagged_lines(self):
        u = usage(seven_day_breakdown={"rows": [{"key": "claude_code", "percent": 60},
                                                  {"key": "chat", "percent": 30}]})
        _, widget, _ = r.build(u, rows_linear(), NOW, None, None)
        lines = widget.split("\n")
        self.assertIn("B,60,30,0,0", lines)
        h = [x for x in lines if x.startswith("H,")][0].split(",")
        self.assertEqual(len(h), 8)

    def test_coach_lines_sanitised(self):
        summ = {"last": {"project": "my,proj\nx", "peak_ctx": 123456, "calls": 7,
                          "title": "Fix, the\nbug \U0001F600 città", "tip": "Use /clear, now"},
                "tips": [{"short": "a,b\nc"}] * 6}
        out = r.coach_lines(summ)
        self.assertEqual(out[0], "C,my proj x,123,7")
        self.assertEqual(out[1], "S,Fix, the bug città")
        self.assertEqual(sum(1 for x in out if x.startswith("T,")), 4)
        for x in out:
            self.assertNotIn("\n", x)
        self.assertEqual(len(out[0].split(",")), 4)

    def test_clean(self):
        self.assertEqual(r.clean("a,b", 10, final=False), "a b")
        self.assertEqual(r.clean("a,b", 10), "a,b")
        self.assertEqual(r.clean("x\r\ny\tz", 10), "x y z")
        self.assertEqual(len(r.clean("à" * 100, 20)), 20)
        self.assertEqual(r.clean(None, 5), "")
        r.clean("ok", 5).encode("utf-8")

    def test_widget_size_cap(self):
        summ = {"last": {"project": "p", "peak_ctx": 1, "calls": 1, "title": "t" * 200, "tip": "x" * 200},
                "tips": [{"short": "y" * 100}] * 4}
        u = usage(seven_day_breakdown={"rows": []})
        _, widget, _ = r.build(u, rows_linear(), NOW, summ, None)
        self.assertLessEqual(len(widget.encode()), r.MAX_WIDGET)

    def test_d_and_e_lines(self):
        u = usage(extra=True)
        u["extra_usage"] = {"is_enabled": True, "used_credits": 1234, "monthly_limit": 5000,
                            "utilization": 24.7}
        _, widget, _ = r.build(u, rows_linear(), NOW, None, None)
        for l in widget.split("\n"):
            if l[:2] in ("D,", "E,"):
                self.assertTrue(all(x.lstrip("-").isdigit() for x in l.split(",")[1:3]), l)


class Publish(unittest.TestCase):
    def test_key_ignores_t(self):
        a = "40,%d,20,%d,%d,900,0,0,0" % (SR, WR, NOW)
        b = "40,%d,20,%d,%d,900,0,0,0" % (SR, WR, NOW + 300)
        self.assertEqual(r.payload_key(a, a + "\nH,1,2"), r.payload_key(b, b + "\nH,1,2"))
        c = "41,%d,20,%d,%d,900,0,0,0" % (SR, WR, NOW)
        self.assertNotEqual(r.payload_key(a, a), r.payload_key(c, c))
        # la riga R che scorre da sola non basta a pubblicare; ogni altra riga sì
        self.assertEqual(r.payload_key(a, a + "\nR,1,2,3"), r.payload_key(a, a + "\nR,2,3,3"))
        self.assertNotEqual(r.payload_key(a, a + "\nH,1,2"), r.payload_key(a, a + "\nH,1,3"))

    def test_reason(self):
        st = {"last_hash": "k", "last_publish": NOW - 300, "gist": "g1"}
        self.assertIsNone(r.publish_reason(st, "k", NOW, "g1"))
        self.assertEqual(r.publish_reason(st, "other", NOW, "g1"), "changed")
        self.assertEqual(r.publish_reason(st, "k", NOW, "g2"), "changed")
        st["last_publish"] = NOW - r.HEARTBEAT
        self.assertEqual(r.publish_reason(st, "k", NOW, "g1"), "heartbeat")
        st["last_publish"] = NOW - 1700  # launchd a 300 s: slack
        self.assertEqual(r.publish_reason(st, "k", NOW, "g1"), "heartbeat")
        self.assertEqual(r.publish_reason({}, "k", NOW, "g1"), "changed")


class State(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_migrate_v1_history_once(self):
        with open(os.path.join(self.d, "history.jsonl"), "w") as f:
            f.write(json.dumps({"t": NOW - 600, "w": 12.5, "wr": WR}) + "\n")
            f.write("garbage\n")
            f.write(json.dumps({"t": NOW - 300, "w": 13, "wr": WR}) + "\n")
            f.write(json.dumps({"t": NOW - 20 * 86400, "w": 1, "wr": WR}) + "\n")
        st = r.new_state()
        self.assertEqual(r.migrate(st, self.d, NOW, legacy=None, logs=[]), 3)
        self.assertEqual([x[0] for x in st["rows"]], [NOW - 600, NOW - 300])  # 20 giorni potato
        self.assertEqual(st["rows"][0][3], 12.5)
        self.assertEqual(r.migrate(st, self.d, NOW, legacy=None, logs=[]), 0)  # una volta sola

    def test_prune_and_add_row(self):
        old = [NOW - 9 * 86400, 1, SR, 1, WR]
        new = [NOW - 100, 2, SR, 2, WR]
        self.assertEqual(r.prune([old, new], NOW), [new])
        rows = [new]
        rows = r.add_row(rows, [NOW - 100, 3, SR, 3, WR])  # stesso t: sostituisce
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], 3)

    def test_state_roundtrip_and_corrupt(self):
        st = r.new_state()
        st["rows"] = [[NOW, 39.0, SR, 20.5, WR]]
        r.save_state(self.d, st)
        back = r.load_state(self.d)
        self.assertEqual(back["rows"], [[NOW, 39, SR, 20.5, WR]])
        with open(os.path.join(self.d, "state.json"), "w") as f:
            f.write("{not json")
        self.assertEqual(r.load_state(self.d)["rows"], [])
        self.assertTrue(os.path.exists(os.path.join(self.d, "state.json.bad")))

    def test_state_wrong_types_heal(self):
        for bad in ('{"rows":"abc","last_publish":"x","hint":"y","last_error":"z","backoff_until":"q","last_hash":5}',
                    '{"rows":[[1,2,3],["a",1,2,3,4],null],"last_publish":Infinity,"hint":NaN}', '[1,2]'):
            with open(os.path.join(self.d, "state.json"), "w") as f:
                f.write(bad)
            st = r.load_state(self.d)
            self.assertEqual(st["rows"], [])
            self.assertEqual(st["last_publish"], 0)
            self.assertIsNone(st["last_hash"])
            self.assertIsNone(st["hint"])
            self.assertIsNone(st["last_error"])
            self.assertEqual(st["backoff_until"], 0)
            r.publish_reason(st, "k", NOW, "g")  # non deve lanciare
            r.status(self.d, io.StringIO())

    def test_files_are_private(self):
        r.save_state(self.d, r.new_state())
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.d, "state.json")).st_mode), 0o600)
        lock = r.Lock(os.path.join(self.d, ".relay.lock"))
        self.assertTrue(lock.acquire())
        lock.release()
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.d, ".relay.lock")).st_mode), 0o600)

    def test_lock(self):
        p = os.path.join(self.d, ".relay.lock")
        a, b = r.Lock(p), r.Lock(p)
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())
        a.release()
        self.assertTrue(b.acquire())
        b.release()

    def test_epoch(self):
        self.assertEqual(r.epoch(iso(SR)), SR)
        self.assertEqual(r.epoch(None), 0)


class Run(unittest.TestCase):
    """Giri completi di run() con rete, gh, keychain e coach finti."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.now = [NOW]
        self.s = [10.0]
        self.pub = []
        self.fail_pub = [False]
        self.api_error = [None]
        self.scan_error = [None]
        self.log = os.path.join(self.d, "relay.log")
        for p in (mock.patch.object(r.time, "time", lambda: self.now[0]),
                  mock.patch.object(r.time, "sleep", lambda s: None),
                  mock.patch.object(r, "get_usage", self._usage),
                  mock.patch.object(r, "publish", self._publish),
                  mock.patch.object(r, "V1_HISTORY", os.path.join(self.d, "nessuno")),
                  mock.patch.object(r, "V1_LOGS", ()),
                  mock.patch.object(token_coach, "scan", self._scan)):
            p.start()
            self.addCleanup(p.stop)

    def _usage(self):
        if self.api_error[0]:
            raise self.api_error[0]
        return usage(s=self.s[0])

    def _publish(self, gist, files):
        if self.fail_pub[0]:
            raise r.RelayError("publish failed: HTTP 502")
        self.pub.append((gist, dict(files)))

    def _scan(self, **kw):
        if self.scan_error[0]:
            raise self.scan_error[0]
        return [], {"activity": None}

    def logtext(self):
        with open(self.log) as f:
            return f.read()

    def go(self, mode="publish", step=300):
        self.now[0] += step
        return r.run(mode, "abc123", self.d, self.log, out=io.StringIO())

    def test_publish_skip_heartbeat(self):
        self.assertEqual(self.go(step=0), 0)
        self.assertEqual(len(self.pub), 1)
        for _ in range(4):  # 20 min senza cambi: niente gh
            self.go()
        self.assertEqual(len(self.pub), 1)
        self.go()  # 25 min
        self.go()  # 30 min: battito
        self.assertEqual(len(self.pub), 2)
        self.assertEqual(r.load_state(self.d)["last_reason"], "heartbeat")
        self.s[0] = 11.0
        self.go()
        self.assertEqual(len(self.pub), 3)
        gist, files = self.pub[-1]
        self.assertEqual(gist, "abc123")
        self.assertEqual(set(files), {"usage.txt", "widget.txt"})
        self.assertEqual(files["widget.txt"].split("\n")[0], files["usage.txt"])

    def test_publish_failure_is_retried_and_logged_once(self):
        self.fail_pub[0] = True
        self.assertEqual(self.go(step=0), 1)
        self.assertEqual(self.go(), 1)
        self.assertEqual(self.pub, [])
        self.assertEqual(self.logtext().count("publish failed"), 1)  # stesso errore: una riga l'ora
        self.fail_pub[0] = False
        self.assertEqual(self.go(), 0)
        self.assertEqual(len(self.pub), 1)
        self.assertEqual(len(r.load_state(self.d)["rows"]), 3)  # le letture non si perdono

    def test_rate_limit_backoff_keeps_last_data(self):
        self.go(step=0)
        self.api_error[0] = r.RelayError("usage API: HTTP 429", code=429, retry_after=1200)
        self.assertEqual(self.go(), 1)
        st = r.load_state(self.d)
        self.assertEqual(st["backoff_until"], self.now[0] + 1200)
        calls = []
        self.api_error[0] = None
        with mock.patch.object(r, "get_usage", lambda: calls.append(1) or usage()):
            self.assertEqual(self.go(), 0)  # in pausa: nemmeno una richiesta
        self.assertEqual(calls, [])
        self.go(step=1200)
        self.assertEqual(r.load_state(self.d)["backoff_until"], 0)

    def test_print_and_dry_run_never_publish(self):
        self.assertEqual(self.go("print", step=0), 0)
        self.assertEqual(self.go("dry-run"), 0)
        self.assertEqual(self.pub, [])
        self.assertFalse(os.path.exists(os.path.join(self.d, "coach_cache.json")))
        self.assertEqual(len(r.load_state(self.d)["rows"]), 1)  # solo dry-run salva

    def test_coach_failure_is_not_fatal_nor_spammy(self):
        self.scan_error[0] = RuntimeError("boom")
        for _ in range(3):
            self.assertEqual(self.go(), 0)
        self.assertEqual(self.logtext().count("token_coach"), 1)
        self.assertEqual(len(self.pub), 1)  # il widget esce lo stesso, senza consigli

    def test_second_instance_exits(self):
        lock = r.Lock(os.path.join(self.d, ".relay.lock"))
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)
        self.assertEqual(self.go(), 0)
        self.assertEqual(self.pub, [])
        self.assertEqual(self.go("dry-run"), 1)


if __name__ == "__main__":
    unittest.main()
