"""install.sh / uninstall.sh con HOME finta e launchctl, gh, claude, crontab finti (niente rete, niente keychain)."""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUBS = {
    "gh": 'echo "gh $*" >> "$STUBLOG"\n',
    "claude": 'echo \'{"loggedIn": true}\'\n',
    "launchctl": 'echo "launchctl $*" >> "$STUBLOG"\n',
    # crontab finto su file: legge tutto lo stdin prima di scrivere, come quello vero
    "crontab": 'if [ "$1" = "-l" ]; then [ -f "$CRONF" ] && cat "$CRONF"; exit 0; fi\nnew=$(cat); printf \'%s\\n\' "$new" > "$CRONF"\n',
}


class Install(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.t, True)
        self.home = os.path.join(self.t, "home")
        self.stubs = os.path.join(self.t, "stubs")
        os.makedirs(self.home)
        os.makedirs(self.stubs)
        for name, body in STUBS.items():
            p = os.path.join(self.stubs, name)
            with open(p, "w") as f:
                f.write("#!/bin/bash\n" + body)
            os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
        self.cron = os.path.join(self.t, "cron.txt")
        with open(self.cron, "w") as f:
            f.write("0 1 * * * echo unrelated\n")
        self.env = dict(os.environ, HOME=self.home, RELAY_NO_RUN="1", STUBLOG=os.path.join(self.t, "stub.log"),
                        CRONF=self.cron, PATH=self.stubs + ":/usr/bin:/bin:/usr/sbin:/sbin")
        self.data = os.path.join(self.t, "data dir")
        self.mac = os.uname().sysname == "Darwin"

    def sh(self, script, *args):
        r = subprocess.run(["bash", os.path.join(ROOT, script), *args], env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def test_install_twice_then_uninstall(self):
        args = ("--gist", "abc123", "--dir", self.data, "--label", "com.test.relay")
        out = self.sh("install.sh", *args)
        self.sh("install.sh", *args)  # idempotente
        self.assertIn("abc123/raw", out)
        self.assertEqual(stat.S_IMODE(os.stat(self.data).st_mode), 0o700)
        for f in ("claude_usage_relay.py", "token_coach.py", "gist_id"):
            self.assertTrue(os.path.exists(os.path.join(self.data, f)), f)
        if self.mac:
            plist = os.path.join(self.home, "Library", "LaunchAgents", "com.test.relay.plist")
            self.assertTrue(os.path.exists(plist))
            with open(plist) as f:
                text = f.read()
            for k in ("<key>ProcessType</key><string>Background</string>", "<key>LowPriorityIO</key><true/>",
                      "<key>Nice</key><integer>10</integer>", "<key>StartInterval</key><integer>300</integer>"):
                self.assertIn(k, text)
            self.assertIn("com.test.relay.log", text)
            self.assertNotIn("sk-ant", text)
        else:
            with open(self.cron) as f:
                cron = f.read()
            self.assertEqual(cron.count("claude_usage_relay.py"), 1)  # senza duplicati
            self.assertIn("unrelated", cron)
            self.assertIn("nice -n 10", cron)
        self.sh("uninstall.sh", "--dir", self.data, "--label", "com.test.relay", "--purge")
        self.assertFalse(os.path.exists(self.data))
        if self.mac:
            self.assertFalse(os.path.exists(plist))
        else:
            with open(self.cron) as f:
                self.assertEqual(f.read().strip(), "0 1 * * * echo unrelated")

    def test_uninstall_without_install_is_fine(self):
        self.sh("uninstall.sh", "--dir", self.data, "--label", "com.test.none")

    def test_rejects_bad_label_and_gist(self):
        for args in (("--label", "a b"), ("--gist", "x/../y")):
            r = subprocess.run(["bash", os.path.join(ROOT, "install.sh"), *args], env=self.env,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
