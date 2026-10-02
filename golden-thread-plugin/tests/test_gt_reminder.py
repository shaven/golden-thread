"""The reminder tool (gt 0.18.0): dated must-do items reach the owner by the channels chosen.

Request 2026-10-01-reminder-tool. Every sender is stubbed: macOS via GT_REMINDER_OSASCRIPT,
the relay via a local HTTP server, email via a local fake SMTP server. Pinned:
  * deadlines.md parsing (through the mirror): overdue, due within N days, no date, malformed;
    a credential-shaped label is withheld before it reaches the mirror;
  * the threshold setting `reminder_days`; channel selection from settings, every push channel
    OFF by default;
  * one test per channel: the message text arrives; no credential value appears in stdout,
    stderr or the log;
  * `check` against a failing stub reports "could not deliver" and exit 1 -- never a pass;
    a credentials file readable by others is refused before it is read;
  * the TCC design: the scheduled job reads only the mirror (never the vault); session start
    refreshes the mirror only when a channel is on or a mirror exists; a stale mirror says so;
  * `gt_schedule.py` knows a `reminder` job: plist runs the installed copy with `run
    --scheduled`, no --vault, outside CloudStorage; install refuses when the preflight fails;
  * `import-tsv` folds `label<TAB>date` rows into deadlines.md through the write queue.
"""
import datetime
import http.server
import json
import os
import plistlib
import socketserver
import stat
import threading
import unittest
from pathlib import Path

from _harness import Sandbox, SCRIPTS, load_module

REM = SCRIPTS / "gt_reminder.py"
SURFACE = SCRIPTS / "gt_surface.py"
SCHED = SCRIPTS / "gt_schedule.py"
TODAY = "2026-10-10"
SECRET = "pw-Zq8vK2mXr7Lw3"        # must never appear in any output


def deadlines(rows):
    return ("# Deadlines\n\nIntro text.\n\n| item | category | due | see |\n|---|---|---|---|\n"
            + "".join("| %s | %s | %s | %s |\n" % r for r in rows))


ROWS = [
    ("Rotate the widget key", "rotation", "2026-10-01", "[[pending]]"),     # 9 days overdue
    ("Decide the ladder variant", "decision", "2026-10-13", ""),             # due in 3d
    ("Renew the cert", "renewal", "2026-10-15", ""),                         # due in 5d
    ("Far away thing", "later", "2026-12-25", ""),                           # outside window
    ("No date at all", "x", "", ""),                                         # malformed
    ("Bad date", "x", "next week", ""),                                      # malformed
]


def _servers():
    # Built inside a function: tests/prun.py runs every column-0 `class X(` as a test unit.
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self.server.got.append({"headers": dict(self.headers), "body": self.rfile.read(n)})
            self.send_response(self.server.status)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass


    class SMTPHandler(socketserver.StreamRequestHandler):
        def handle(self):
            def w(s):
                self.wfile.write((s + "\r\n").encode())
            w("220 fake ESMTP")
            data, buf = False, []
            while True:
                line = self.rfile.readline()
                if not line:
                    break
                t = line.decode("utf-8", "replace").rstrip("\r\n")
                if data:
                    if t == ".":
                        data = False
                        self.server.msgs.append("\n".join(buf))
                        buf = []
                        w("250 queued")
                    else:
                        buf.append(t)
                    continue
                cmd = t[:4].upper()
                if cmd == "EHLO":
                    w("250-fake")
                    w("250 8BITMIME")
                elif cmd in ("HELO", "MAIL", "RCPT", "RSET", "NOOP"):
                    w("250 ok")
                elif cmd == "DATA":
                    w("354 go ahead")
                    data = True
                elif cmd == "QUIT":
                    w("221 bye")
                    break
                else:
                    w("502 not here")

    return Handler, SMTPHandler


Handler, SMTPHandler = _servers()


class ReminderBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        (self.vault / "Projects" / "golden-thread").mkdir(parents=True)
        (self.vault / "deadlines.md").write_text(deadlines(ROWS), encoding="utf-8")
        self.env["GT_TODAY"] = TODAY
        self.settings = {"vault_path": str(self.vault)}
        self.config(**self.settings)
        self.rdir = self.home / ".claude" / "golden-thread" / "reminder"
        self.log = self.home / ".claude" / "golden-thread" / "reminder.log"

    def set(self, **kw):
        self.settings.update(kw)
        self.config(**self.settings)

    def rem(self, *args, **kw):
        return self.py(REM, *args, **kw)

    def mirror(self):
        p = self.rem("mirror", "--vault", self.vault)
        self.assertOk(p)
        return json.loads((self.rdir / "deadlines.json").read_text())

    def cred(self, channel, data, mode=0o600):
        self.rdir.mkdir(parents=True, exist_ok=True)
        p = self.rdir / ("%s.json" % channel)
        p.write_text(json.dumps(data))
        os.chmod(p, mode)
        return p

    def osascript_stub(self, code=0):
        p = self.tmp / "osascript"
        out = self.tmp / "osascript.args"
        p.write_text("#!/bin/sh\nprintf '%%s\\n' \"$@\" > '%s'\nexit %d\n" % (out, code))
        os.chmod(p, 0o755)
        self.env["GT_REMINDER_OSASCRIPT"] = str(p)
        return out

    def http(self, status=200):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        srv.got, srv.status = [], status
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return srv

    def smtp(self):
        srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), SMTPHandler)
        srv.daemon_threads = True
        srv.msgs = []
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return srv

    def closed_port(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def assertNoSecret(self, *procs):
        for p in procs:
            self.assertNotIn(SECRET, p.stdout + p.stderr)
        if self.log.exists():
            self.assertNotIn(SECRET, self.log.read_text())
        for f in self.rdir.glob("deadlines.json"):
            self.assertNotIn(SECRET, f.read_text())


class Parsing(ReminderBase):
    def test_mirror_rows_and_malformed_lines(self):
        m = self.mirror()
        items = [r["item"] for r in m["rows"]]
        self.assertEqual(items[:4], [r[0] for r in ROWS[:4]])
        self.assertEqual(m["skipped"], 2, "no-date and bad-date rows are skipped and counted")
        mode = stat.S_IMODE((self.rdir / "deadlines.json").stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_window_threshold_setting(self):
        self.mirror()
        out = self.osascript_stub()
        self.set(reminder_macos="on")
        p = self.rem("run", "--dry-run")
        self.assertOk(p)
        self.assertIn("OVERDUE 9d: Rotate the widget key", p.stdout)
        self.assertIn("due in 3d: Decide the ladder variant", p.stdout)
        self.assertIn("due in 5d: Renew the cert", p.stdout)        # default window 7
        self.assertNotIn("Far away", p.stdout)
        self.set(reminder_days="3")
        p = self.rem("run", "--dry-run")
        self.assertIn("Decide the ladder variant", p.stdout)
        self.assertNotIn("Renew the cert", p.stdout)
        self.assertIn("1 overdue, 1 due within 3d", p.stdout)
        self.assertFalse(out.exists(), "--dry-run sent something")

    def test_credential_shaped_label_never_reaches_the_mirror(self):
        (self.vault / "deadlines.md").write_text(deadlines(
            [("Rotate token=%s now" % SECRET, "rotation", "2026-10-01", "")]))
        m = self.mirror()
        self.assertIn("withheld", m["rows"][0]["item"])
        self.assertNoSecret()

    def test_stale_mirror_is_said(self):
        m = self.mirror()
        m["mirrored"] = "2026-09-01T08:00:00+00:00"
        (self.rdir / "deadlines.json").write_text(json.dumps(m))
        self.set(reminder_macos="on")
        self.osascript_stub()
        p = self.rem("run", "--dry-run")
        self.assertIn("dates as of 2026-09-01", p.stdout)


class Selection(ReminderBase):
    def test_every_push_channel_is_off_by_default(self):
        mod = load_module(REM, "gt_reminder_defaults")
        import sys
        sys.path.insert(0, str(SCRIPTS))
        import gt_settings
        for ch in mod.PUSH_CHANNELS:
            self.assertEqual(gt_settings.SETTINGS[mod.SETTING[ch]]["default"], "off", ch)
        self.mirror()
        p = self.rem("run", "--json")
        self.assertOk(p)          # due items, no channel: nothing sent, said on stderr
        self.assertIn("no push channel is on", p.stderr)
        st = self.rem("status")
        self.assertOk(st)
        for ch in ("macos", "relay", "email"):
            self.assertRegex(st.stdout, r"%s\s+reminder_%s\s+off" % (ch, ch))

    def test_run_without_a_mirror_cannot_run(self):
        p = self.rem("run")
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("CANNOT RUN", p.stderr)

    def test_preflight(self):
        p = self.rem("run", "--check")
        self.assertEqual(p.returncode, 1)
        self.assertIn("no deadlines mirror", p.stderr)
        self.assertIn("no push channel is on", p.stderr)
        self.mirror()
        self.set(reminder_relay="sms")
        p = self.rem("run", "--check")
        self.assertEqual(p.returncode, 1)
        self.assertIn("relay.json does not exist", p.stderr)
        self.osascript_stub()
        self.set(reminder_relay="off", reminder_macos="on")
        self.assertOk(self.rem("run", "--check"))


class MacOS(ReminderBase):
    def test_sends_the_list(self):
        self.mirror()
        out = self.osascript_stub()
        self.set(reminder_macos="on")
        p = self.rem("run")
        self.assertOk(p)
        args = out.read_text()
        self.assertIn("display notification", args)
        self.assertIn("Rotate the widget key", args)
        self.assertIn("macos delivered", self.log.read_text())

    def test_check_against_a_failing_stub(self):
        self.osascript_stub(code=1)
        p = self.rem("check", "macos")
        self.assertEqual(p.returncode, 1)
        self.assertIn("could not deliver", p.stdout)
        self.assertNotIn("DELIVERED", p.stdout)
        ok = self.osascript_stub(code=0)
        p = self.rem("check", "macos")
        self.assertOk(p)
        self.assertIn("DELIVERED", p.stdout)
        self.assertIn("test", ok.read_text())


class Relay(ReminderBase):
    def test_sends_json_with_auth_and_leaks_nothing(self):
        self.mirror()
        srv = self.http()
        self.cred("relay", {"url": "http://127.0.0.1:%d/notify" % srv.server_address[1],
                            "auth": {"type": "basic", "user": "gt", "password": SECRET}})
        self.set(reminder_relay="discord")
        p = self.rem("run")
        self.assertOk(p)
        self.assertEqual(len(srv.got), 1)
        body = json.loads(srv.got[0]["body"])
        self.assertEqual(body["route"], "discord")
        self.assertEqual(body["source"], "gt-reminder")
        self.assertIn("Rotate the widget key", body["detail"])
        self.assertTrue(srv.got[0]["headers"].get("Authorization", "").startswith("Basic "))
        c = self.rem("check", "relay")
        self.assertOk(c)
        self.assertIn("DELIVERED", c.stdout)
        self.assertNoSecret(p, c)

    def test_check_reports_could_not_deliver(self):
        srv = self.http(status=500)
        self.cred("relay", {"url": "http://127.0.0.1:%d/" % srv.server_address[1]})
        p = self.rem("check", "relay")
        self.assertEqual(p.returncode, 1)
        self.assertIn("could not deliver", p.stdout)
        self.assertIn("HTTP 500", p.stdout)
        self.cred("relay", {"url": "http://127.0.0.1:%d/" % self.closed_port()})
        p = self.rem("check", "relay")
        self.assertEqual(p.returncode, 1)
        self.assertIn("could not be reached", p.stdout)

    def test_a_loose_credentials_file_is_refused(self):
        srv = self.http()
        self.cred("relay", {"url": "http://127.0.0.1:%d/" % srv.server_address[1],
                            "auth": {"type": "bearer", "token": SECRET}}, mode=0o644)
        p = self.rem("check", "relay")
        self.assertEqual(p.returncode, 1)
        self.assertIn("readable by others", p.stdout)
        self.assertEqual(srv.got, [], "a request went out with a loose credential file")
        self.assertNoSecret(p)


class Email(ReminderBase):
    def test_sends_one_email(self):
        self.mirror()
        srv = self.smtp()
        self.cred("email", {"host": "127.0.0.1", "port": srv.server_address[1],
                            "security": "none", "from": "gt@example.invalid",
                            "to": ["me@example.invalid"]})
        self.set(reminder_email="on")
        p = self.rem("run")
        self.assertOk(p)
        self.assertEqual(len(srv.msgs), 1)
        self.assertIn("Rotate the widget key", srv.msgs[0])
        self.assertIn("Subject: GT reminder: 1 overdue, 2 due within 7d", srv.msgs[0])

    def test_check_could_not_deliver(self):
        self.cred("email", {"host": "127.0.0.1", "port": self.closed_port(), "security": "none",
                            "from": "a@example.invalid", "to": "b@example.invalid",
                            "user": "u", "password": SECRET})
        p = self.rem("check", "email")
        self.assertEqual(p.returncode, 1)
        self.assertIn("could not deliver", p.stdout)
        self.assertNoSecret(p)


class SessionStartMirror(ReminderBase):
    def surface(self):
        return self.py(SURFACE, "check", "--vault", self.vault)

    def test_no_mirror_when_nothing_asked_for_it(self):
        self.assertOk(self.surface())
        self.assertFalse((self.rdir / "deadlines.json").exists())

    def test_refreshed_when_a_channel_is_on(self):
        self.set(reminder_macos="on")
        self.assertOk(self.surface())
        m = json.loads((self.rdir / "deadlines.json").read_text())
        self.assertEqual(m["rows"][0]["item"], "Rotate the widget key")
        # an edit to the vault reaches the mirror at the next session start
        (self.vault / "deadlines.md").write_text(deadlines([("Only row", "x", "2026-10-11", "")]))
        self.assertOk(self.surface())
        m = json.loads((self.rdir / "deadlines.json").read_text())
        self.assertEqual([r["item"] for r in m["rows"]], ["Only row"])

    def test_the_job_never_reads_the_vault(self):
        self.mirror()
        self.set(reminder_macos="on", vault_path=str(self.tmp / "nowhere"))
        self.osascript_stub()
        (self.vault / "deadlines.md").unlink()        # the vault is gone; the mirror is enough
        p = self.rem("run", "--scheduled")
        self.assertOk(p)
        self.assertIn("macos delivered", self.log.read_text())


class Schedule(ReminderBase):
    def mod(self):
        return load_module(SCHED, "gt_schedule_reminder")

    def test_plist(self):
        m = self.mod()
        self.assertIn("reminder", m.JOBS)
        self.assertEqual(m.BENIGN_EXITS["reminder"], {"0"})
        doc = m.build_plist("reminder", None, [], *m.JOBS["reminder"][1:4])
        args = doc["ProgramArguments"]
        self.assertTrue(args[1].endswith(".claude/golden-thread/hooks/gt_reminder.py"))
        self.assertNotIn("CloudStorage", " ".join(args))
        self.assertEqual(args[2:], ["run", "--scheduled"])
        self.assertNotIn("--vault", args, "the job must not be pointed at the vault (TCC)")
        self.assertNotIn("Weekday", doc["StartCalendarInterval"])

    def install_scripts(self):
        hooks = self.home / ".claude" / "golden-thread" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        for n in ("gt_reminder.py", "gt_settings.py", "gt_surface.py", "gt_handoff_status.py",
                  "gt_schedule.py", "gt_write_queue.py", "gt_paths.py"):
            if (SCRIPTS / n).is_file():
                (hooks / n).write_text((SCRIPTS / n).read_text())

    def test_install_refuses_when_the_preflight_fails(self):
        self.install_scripts()
        p = self.py(SCHED, "install", "reminder", "--vault", self.vault)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("CANNOT INSTALL", p.stderr)
        self.assertIn("no push channel is on", p.stderr)
        self.assertFalse((self.home / "Library" / "LaunchAgents").exists())
        # ...but the mirror WAS refreshed from the terminal, which can read the vault
        self.assertTrue((self.rdir / "deadlines.json").exists())


class ImportTsv(ReminderBase):
    def test_folds_rows_through_the_queue(self):
        tsv = self.tmp / "deadlines.tsv"
        tsv.write_text("Ladder decision: keep prod\t2026-10-04\n"
                       "Rotate the widget key\t2026-10-01\n"
                       "Broken\tsoon\n# a comment\n")
        before = (self.vault / "deadlines.md").read_text()
        d = self.rem("import-tsv", "--vault", self.vault, "--tsv", tsv, "--dry-run")
        self.assertOk(d)
        self.assertIn("would add | Ladder decision: keep prod | reminder | 2026-10-04 |", d.stdout)
        self.assertIn("already in deadlines.md", d.stdout)
        self.assertIn("not YYYY-MM-DD", d.stdout)
        self.assertEqual((self.vault / "deadlines.md").read_text(), before)
        o = self.rem("import-tsv", "--vault", self.vault, "--tsv", tsv, "--only", "nothing")
        self.assertIn("nothing to add", o.stdout)
        p = self.rem("import-tsv", "--vault", self.vault, "--tsv", tsv)
        self.assertOk(p)
        self.assertIn("-> apply", p.stdout)
        m = self.mirror()
        self.assertIn("Ladder decision: keep prod", [r["item"] for r in m["rows"]])
        self.assertEqual(m["skipped"], 2, "the table must still parse as one table")


if __name__ == "__main__":
    unittest.main()
