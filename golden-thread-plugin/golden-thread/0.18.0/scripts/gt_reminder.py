#!/usr/bin/env python3
"""gt_reminder.py -- the reminder tool: dated must-do items reach you by the channels you choose.

    gt_reminder.py status                                   # channels, mirror age, schedule
    gt_reminder.py setup  CHANNEL                           # how to turn a channel on
    gt_reminder.py check  CHANNEL [--json]                  # send ONE test message, prove delivery
    gt_reminder.py mirror --vault V [--dry-run]             # copy deadlines.md out of the vault
    gt_reminder.py run    [--dry-run] [--json] [--check] [--scheduled]
    gt_reminder.py import-tsv --vault V --tsv FILE [--only TEXT ...] [--category C] [--dry-run]

CHANNELS -- every one off by default, each switched by a gt setting (`/gt:gt-settings`):

    session  the MUST DO block at every session start (gt_surface, since 0.17.2; setting
             `surface`). Needs nothing; reaches you only when a session starts.
    macos    a macOS notification (setting `reminder_macos`). No credential.
    relay    SMS or Discord through your notification relay (setting `reminder_relay` = sms |
             discord): one HTTP POST of JSON to the relay's URL.
    email    one email over SMTP (setting `reminder_email`).

`reminder_days` (default 7) sets the window: overdue items always, plus items due within it.

WHY (owner, 2026-10-01): "we will just call it a reminder tool and give them options of how to be
reminded and give them the setup for how they work". The session-start MUST DO block existed and
only reached the owner when a session started; a rotation sat 15 days overdue that way.

ONE SOURCE OF DATES: `<vault>/deadlines.md`, the table MUST DO already reads. There is no second
list. `import-tsv` folds an old `label<TAB>YYYY-MM-DD` file into it, through the write queue.

THE TCC PROBLEM, AND THE DESIGN AROUND IT. A launchd job on macOS is refused access to
~/Library/CloudStorage (TCC): it can stat the vault and read nothing, or be denied outright --
the weekly lint died that way for three Mondays. A vault in Dropbox/OneDrive is therefore
unreadable to a scheduled reminder. So the scheduled `run` never reads the vault. It reads a
MIRROR, `~/.claude/golden-thread/reminder/deadlines.json` (mode 600), which is refreshed:
  * at every session start, by gt_surface (a Claude Code session HAS the access), and
  * by `mirror --vault V`, which `gt_schedule.py install reminder --vault V` also runs.
Countdowns are computed from the due DATES at send time, so they stay right however old the
mirror is; what a stale mirror misses is a row added or deleted since. When the mirror is older
than 7 days the reminder says so ("dates as of …"), rather than nagging about work already done
in silence. Alternatives rejected: Full Disk Access for python (grants every script that python
runs the whole disk), and moving the vault (the owner's choice, not gt's).

CREDENTIALS never live in settings, the vault, a log, or this output. A channel that needs one
reads `~/.claude/golden-thread/reminder/<channel>.json`, a file your secrets store writes, mode
600 and owned by you -- anything looser is refused before it is read. Values are read at send
time and never printed: a failure names the file and the KIND of problem, never a value.

`check` SENDS A REAL TEST MESSAGE through one channel, whatever its setting (so you can prove it
before turning it on), and reports DELIVERED only on positive evidence -- HTTP 2xx, SMTP accepted,
osascript exit 0 -- otherwise "could not deliver" and exit 1. Never a pass by default.

`run` is what the `reminder` job runs (gt_schedule.py install reminder): nothing due -> nothing
sent, exit 0. `--check` is the scheduler's preflight: mirror readable, at least one push channel
on, each enabled channel configured -- and sends nothing.

Every send is logged to `~/.claude/golden-thread/reminder.log`: time, channel, outcome and item
counts. Not the message, not a credential.

Exit: 0 ok / nothing due | 1 a channel could not deliver, or a preflight failed | 2 usage |
3 could not run (no mirror: no session has run since the reminder was set up).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OK, PROBLEM, USAGE, CANNOT = 0, 1, 2, 3

CHANNELS = ("session", "macos", "relay", "email")
PUSH_CHANNELS = ("macos", "relay", "email")
SETTING = {"session": "surface", "macos": "reminder_macos", "relay": "reminder_relay",
           "email": "reminder_email"}
MIRROR_STALE_DAYS = 7
SEND_TIMEOUT = 20
TITLE = "Golden Thread reminder"


def home() -> Path:
    return Path.home() / ".claude" / "golden-thread"


def rdir() -> Path:
    return home() / "reminder"


def mirror_path() -> Path:
    return rdir() / "deadlines.json"


def log_path() -> Path:
    return home() / "reminder.log"


def cred_path(channel: str) -> Path:
    return rdir() / ("%s.json" % channel)


def today() -> datetime.date:
    pinned = os.environ.get("GT_TODAY")
    if pinned:
        try:
            return datetime.date.fromisoformat(pinned)
        except ValueError:
            pass
    return datetime.date.today()


def _mod(name):
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    return __import__(name)


def setting(name: str, fallback: str) -> str:
    try:
        return _mod("gt_settings").get(name) or fallback
    except Exception:
        return fallback


def enabled_channels() -> list[str]:
    out = []
    for ch in PUSH_CHANNELS:
        if setting(SETTING[ch], "off") != "off":
            out.append(ch)
    return out


def window_days() -> int:
    try:
        return int(setting("reminder_days", "7"))
    except ValueError:
        return 7


def log(channel: str, outcome: str, **counts) -> None:
    try:
        log_path().parent.mkdir(parents=True, exist_ok=True)
        bits = " ".join("%s=%s" % kv for kv in sorted(counts.items()))
        with log_path().open("a", encoding="utf-8") as fh:
            fh.write("%s %s %s %s\n" % (datetime.datetime.now().astimezone().isoformat(
                timespec="seconds"), channel, outcome, bits))
        os.chmod(log_path(), 0o600)
    except OSError:
        pass


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=".m.", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# ------------------------------------------------------------------ the mirror ----

def build_mirror(vault: Path) -> dict | None:
    """Read <vault>/deadlines.md with gt_surface's own parser -> mirror dict, or None if absent.
    Credential-shaped labels are withheld HERE, so a secret never reaches the mirror file."""
    gs = _mod("gt_surface")
    src = vault / gs.DEADLINES
    if not src.is_file():
        return None
    rows, skipped = gs.read_deadlines(vault)
    out = []
    for r in rows:
        label = r["item"]
        if gs.looks_secret(label) or gs.looks_secret(r.get("see") or ""):
            label = "[label withheld: it looks like a credential -- fix the row]"
        out.append({"item": label, "category": r["category"], "due": r["due"].isoformat()})
    return {"schema": 1, "source": str(src),
            "mirrored": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "rows": out, "skipped": skipped}


def refresh_mirror(vault: Path | None, dry_run: bool = False):
    """-> (wrote?, note). Called by gt_surface at session start; never raises."""
    try:
        if vault is None:
            return False, "no vault"
        m = build_mirror(Path(vault))
        if m is None:
            return False, "no deadlines.md in %s" % vault
        if not dry_run:
            _atomic_json(mirror_path(), m)
        return True, "%d row(s)" % len(m["rows"])
    except Exception as exc:
        return False, "mirror not refreshed (%s)" % exc.__class__.__name__


def load_mirror():
    try:
        m = json.loads(mirror_path().read_text(encoding="utf-8"))
        if not isinstance(m, dict) or not isinstance(m.get("rows"), list):
            return None
        return m
    except (OSError, ValueError):
        return None


def due_rows(m: dict, days: int) -> list[dict]:
    now = today()
    out = []
    for r in m.get("rows", []):
        try:
            d = datetime.date.fromisoformat(r["due"])
        except (KeyError, ValueError, TypeError):
            continue
        left = (d - now).days
        if left <= days:
            out.append(dict(r, days=left))
    out.sort(key=lambda r: r["days"])
    return out


def mirror_age_days(m: dict) -> float | None:
    try:
        t = datetime.datetime.fromisoformat(m["mirrored"])
        return (datetime.datetime.now(t.tzinfo) - t).total_seconds() / 86400
    except Exception:
        return None


def compose(rows: list[dict], m: dict, days: int) -> tuple[str, str]:
    """-> (one-line summary, full text). Plain text: SMS, Discord and email all carry it."""
    over = [r for r in rows if r["days"] < 0]
    soon = [r for r in rows if r["days"] >= 0]
    head = []
    if over:
        head.append("%d overdue" % len(over))
    if soon:
        head.append("%d due within %dd" % (len(soon), days))
    summary = "GT reminder: " + ", ".join(head)
    lines = [summary]
    for r in rows:
        when = ("OVERDUE %dd" % -r["days"] if r["days"] < 0 else
                "due today" if r["days"] == 0 else "due in %dd" % r["days"])
        lines.append("- %s: %s (%s)" % (when, r["item"], r.get("category", "-")))
    age = mirror_age_days(m)
    if age is not None and age > MIRROR_STALE_DAYS:
        lines.append("(dates as of %s: no session has refreshed them since; open a Claude Code "
                     "session to update)" % str(m.get("mirrored", "?"))[:10])
    return summary, "\n".join(lines)


# ------------------------------------------------------------------ credentials ----

class ChannelError(Exception):
    """A channel could not deliver. The message names a kind of problem, never a value."""


def read_cred(channel: str) -> dict:
    p = cred_path(channel)
    try:
        st = p.stat()
    except FileNotFoundError:
        raise ChannelError("%s does not exist -- see `gt_reminder.py setup %s`" % (p, channel))
    if st.st_mode & 0o077:
        raise ChannelError("%s is readable by others (mode %s); it must be 600 -- refusing to "
                           "read it" % (p, oct(stat.S_IMODE(st.st_mode))))
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise ChannelError("%s is not owned by you -- refusing to read it" % p)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ChannelError("%s is not valid JSON (%s)" % (p, exc.__class__.__name__))
    if not isinstance(d, dict):
        raise ChannelError("%s must hold a JSON object" % p)
    return d


# ------------------------------------------------------------------ senders ----

def send_macos(summary: str, text: str) -> str:
    binary = os.environ.get("GT_REMINDER_OSASCRIPT") or "/usr/bin/osascript"
    if not os.environ.get("GT_REMINDER_OSASCRIPT") and platform.system() != "Darwin":
        raise ChannelError("not macOS -- there is no Notification Center here")
    body = (text.split("\n", 1)[1] if "\n" in text else text).replace("\n", "  ·  ")
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
    script = 'display notification "%s" with title "%s" subtitle "%s"' % (
        esc(body[:900]), esc(TITLE), esc(summary[:120]))
    try:
        p = subprocess.run([binary, "-e", script], capture_output=True, text=True,
                           timeout=SEND_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ChannelError("osascript could not run (%s)" % exc.__class__.__name__)
    if p.returncode != 0:
        raise ChannelError("osascript exited %d" % p.returncode)
    return ("handed to Notification Center (osascript exit 0). If nothing appeared, allow "
            "notifications for Script Editor in System Settings > Notifications")


def send_relay(summary: str, text: str, route: str) -> str:
    import base64
    import urllib.error
    import urllib.request
    c = read_cred("relay")
    url = c.get("url")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise ChannelError("%s has no http(s) `url`" % cred_path("relay"))
    fmt = c.get("format", "relay")
    if fmt == "discord-webhook":
        payload = {"content": text[:1900]}
    else:
        payload = {"severity": "warning", "source": "gt-reminder", "host": socket.gethostname(),
                   "route": route, "text": summary, "detail": text,
                   "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
    headers = {"Content-Type": "application/json", "User-Agent": "gt-reminder"}
    auth = c.get("auth") or {}
    if auth.get("type") == "basic":
        tok = base64.b64encode(("%s:%s" % (auth.get("user", ""), auth.get("password", "")))
                               .encode()).decode()
        headers["Authorization"] = "Basic " + tok
    elif auth.get("type") == "bearer":
        headers["Authorization"] = "Bearer " + str(auth.get("token", ""))
    elif auth:
        raise ChannelError("%s: auth.type must be basic or bearer" % cred_path("relay"))
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=SEND_TIMEOUT) as resp:
            code = resp.status
    except urllib.error.HTTPError as exc:
        raise ChannelError("the relay answered HTTP %d" % exc.code)
    except (urllib.error.URLError, OSError) as exc:
        raise ChannelError("the relay could not be reached (%s)" % exc.__class__.__name__)
    if not 200 <= code < 300:
        raise ChannelError("the relay answered HTTP %d" % code)
    return "the relay accepted it (HTTP %d, route %s)" % (code, route)


def send_email(summary: str, text: str) -> str:
    import smtplib
    from email.message import EmailMessage
    c = read_cred("email")
    for k in ("host", "from", "to"):
        if not c.get(k):
            raise ChannelError("%s is missing `%s`" % (cred_path("email"), k))
    port = int(c.get("port") or 587)
    sec = c.get("security", "starttls")
    msg = EmailMessage()
    msg["Subject"] = summary
    msg["From"] = c["from"]
    msg["To"] = c["to"] if isinstance(c["to"], str) else ", ".join(c["to"])
    msg.set_content(text + "\n\n-- sent by gt_reminder.py; turn off with /gt:gt-settings "
                           "(reminder_email)\n")
    try:
        if sec == "ssl":
            s = smtplib.SMTP_SSL(c["host"], port, timeout=SEND_TIMEOUT)
        else:
            s = smtplib.SMTP(c["host"], port, timeout=SEND_TIMEOUT)
        with s:
            if sec == "starttls":
                s.starttls()
            elif sec != "ssl" and sec != "none":
                raise ChannelError("security must be starttls, ssl or none")
            if c.get("user"):
                s.login(c["user"], c.get("password", ""))
            refused = s.send_message(msg)
    except ChannelError:
        raise
    except smtplib.SMTPAuthenticationError as exc:
        raise ChannelError("the SMTP server refused the login (code %s)" % exc.smtp_code)
    except smtplib.SMTPException as exc:
        raise ChannelError("SMTP failed (%s)" % exc.__class__.__name__)
    except (OSError, ValueError) as exc:
        raise ChannelError("the SMTP server could not be reached (%s)" % exc.__class__.__name__)
    if refused:
        raise ChannelError("the SMTP server refused %d recipient(s)" % len(refused))
    return "the SMTP server accepted it for delivery"


def send(channel: str, summary: str, text: str, route: str | None = None) -> str:
    if channel == "macos":
        return send_macos(summary, text)
    if channel == "relay":
        r = route or setting("reminder_relay", "off")
        return send_relay(summary, text, r if r in ("sms", "discord") else "sms")
    if channel == "email":
        return send_email(summary, text)
    raise ChannelError("%s is not a push channel" % channel)


def configured(channel: str) -> str | None:
    """-> None when the channel is ready to send, else why not. Reads no secret value."""
    if channel == "macos":
        if not os.environ.get("GT_REMINDER_OSASCRIPT") and platform.system() != "Darwin":
            return "not macOS"
        return None
    try:
        read_cred(channel)
        return None
    except ChannelError as exc:
        return str(exc)


# ------------------------------------------------------------------ setup text ----

SETUP = {
    "session": """\
SESSION START (exists since gt 0.17.2)
  What it does: every Claude Code session opens with a MUST DO block -- overdue rows of
  <vault>/deadlines.md in red, rows due within 14 days in amber.
  Needs:  nothing. It reaches you only when a session starts.
  On/off: /gt:gt-settings surface on|off   (default on)
  Test:   gt_reminder.py check session      (prints the block now)
""",
    "macos": """\
macOS NOTIFICATION
  What it does: the scheduled `reminder` job posts one notification listing overdue and
  near items. No credential.
  1. Turn it on:     /gt:gt-settings reminder_macos on
  2. Test it now:    gt_reminder.py check macos
     A test notification appears. If it does not, allow notifications for "Script Editor"
     in System Settings > Notifications (osascript posts as Script Editor).
  3. Schedule it:    gt_schedule.py install reminder --vault "<vault>" [--hour 8 --minute 30]
     The install refreshes the deadlines mirror, runs the preflight, and proves the job
     through launchd -- which sends a real reminder if anything is due.
  Off:               /gt:gt-settings reminder_macos off   (and gt_schedule.py remove reminder
                     when no push channel is left on)
""",
    "relay": """\
SMS OR DISCORD THROUGH YOUR NOTIFICATION RELAY
  What it does: one HTTPS POST of JSON to your relay, which owns the SMS / Discord
  credentials and the throttling. Body:
    {"severity": "warning", "source": "gt-reminder", "host": <hostname>,
     "route": "sms"|"discord", "text": <summary>, "detail": <full list>, "ts": <ISO time>}
  Needs: ~/.claude/golden-thread/reminder/relay.json, mode 600, owned by you, written by your
  secrets store (never typed into a session, never in the vault):
    {"url": "https://<relay>/notify",
     "auth": {"type": "basic", "user": "<user>", "password": "<from the store>"}}
  ("auth" may be {"type": "bearer", "token": ...} or left out when the relay authenticates by
  source address. "format": "discord-webhook" posts {"content": ...} straight to a Discord
  webhook URL instead, for a setup without a relay.)
  1. Have the store install the file:   chmod 600 ~/.claude/golden-thread/reminder/relay.json
  2. Turn it on:     /gt:gt-settings reminder_relay sms      (or discord)
  3. Test it now:    gt_reminder.py check relay   -- DELIVERED only on HTTP 2xx
  4. Schedule it:    gt_schedule.py install reminder --vault "<vault>"
  Off:               /gt:gt-settings reminder_relay off
""",
    "email": """\
EMAIL (SMTP)
  What it does: one plain-text email per reminder run.
  Needs: ~/.claude/golden-thread/reminder/email.json, mode 600, owned by you, written by your
  secrets store:
    {"host": "smtp.example.com", "port": 587, "security": "starttls",
     "user": "<login>", "password": "<from the store>",
     "from": "reminders@example.com", "to": "you@example.com"}
  ("security": "ssl" for port 465, "none" only for a local relay; "to" may be a list.)
  1. Have the store install the file:   chmod 600 ~/.claude/golden-thread/reminder/email.json
  2. Turn it on:     /gt:gt-settings reminder_email on
  3. Test it now:    gt_reminder.py check email   -- DELIVERED only when the server accepts it
  4. Schedule it:    gt_schedule.py install reminder --vault "<vault>"
  Off:               /gt:gt-settings reminder_email off
""",
}


# ------------------------------------------------------------------ verbs ----

def do_setup(a) -> int:
    print(SETUP[a.channel], end="")
    return OK


def do_check(a) -> int:
    ch = a.channel
    if ch == "session":
        vault = _mod("gt_surface").find_vault(a.vault)
        if vault is None:
            print("gt-reminder check session: could not deliver -- no vault found (pass --vault)")
            return PROBLEM
        block = _mod("gt_surface").must_do(vault)
        print("\n".join(block) if block else "MUST DO: nothing overdue or due soon.")
        print("gt-reminder check session: DELIVERED (this is the block every session start "
              "shows; setting `surface` is %s)" % setting("surface", "on"))
        return OK
    summary = "GT reminder: test message"
    text = summary + "\n- this is a test from `gt_reminder.py check %s`; nothing is due" % ch
    try:
        how = send(ch, summary, text)
    except ChannelError as exc:
        log(ch, "check-failed")
        out = {"channel": ch, "delivered": False, "why": str(exc)}
        print(json.dumps(out) if a.json else
              "gt-reminder check %s: could not deliver -- %s" % (ch, exc))
        return PROBLEM
    log(ch, "check-delivered")
    on = setting(SETTING[ch], "off")
    out = {"channel": ch, "delivered": True, "how": how, "setting": on}
    print(json.dumps(out) if a.json else
          "gt-reminder check %s: DELIVERED -- %s. (setting %s is %s)"
          % (ch, how, SETTING[ch], on))
    return OK


def do_mirror(a) -> int:
    vault = _mod("gt_surface").find_vault(a.vault)
    if vault is None:
        print("gt-reminder: no vault -- pass --vault", file=sys.stderr)
        return USAGE
    ok, note = refresh_mirror(vault, a.dry_run)
    if not ok:
        print("gt-reminder mirror: not written -- %s" % note, file=sys.stderr)
        return PROBLEM
    print("gt-reminder mirror: %s %s (%s)" % ("would write" if a.dry_run else "wrote",
                                              mirror_path(), note))
    return OK


def do_run(a) -> int:
    m = load_mirror()
    chans = enabled_channels()
    if a.check:
        problems = []
        if m is None:
            problems.append("no deadlines mirror at %s -- run `gt_reminder.py mirror --vault V` "
                            "or open a session" % mirror_path())
        if not chans:
            problems.append("no push channel is on (reminder_macos / reminder_relay / "
                            "reminder_email) -- a scheduled reminder would send nothing")
        for ch in chans:
            why = configured(ch)
            if why:
                problems.append("%s: %s" % (ch, why))
        for p in problems:
            print("gt-reminder preflight: %s" % p, file=sys.stderr)
        if not problems:
            print("gt-reminder preflight: ok -- %d row(s) mirrored, channel(s): %s"
                  % (len(m["rows"]), ", ".join(chans)))
        return PROBLEM if problems else OK
    if m is None:
        log("run", "could-not-run", reason="no-mirror")
        print("gt-reminder: CANNOT RUN -- no deadlines mirror at %s. It is written at every "
              "session start, or by `gt_reminder.py mirror --vault V`." % mirror_path(),
              file=sys.stderr)
        return CANNOT
    days = window_days()
    rows = due_rows(m, days)
    if not rows:
        log("run", "nothing-due", rows=len(m["rows"]))
        if a.json:
            print(json.dumps({"due": 0, "sent": []}))
        elif not a.scheduled:
            print("gt-reminder: nothing overdue or due within %dd -- nothing sent" % days)
        return OK
    summary, text = compose(rows, m, days)
    if not chans:
        log("run", "no-channel", due=len(rows))
        print("gt-reminder: %d item(s) due but no push channel is on -- see "
              "`gt_reminder.py status`" % len(rows), file=sys.stderr)
        return OK
    if a.dry_run:
        print(text)
        print("gt-reminder: --dry-run -- would send through %s" % ", ".join(chans))
        return OK
    sent, failed = [], []
    for ch in chans:
        try:
            how = send(ch, summary, text)
            sent.append({"channel": ch, "how": how})
            log(ch, "delivered", due=len(rows))
        except ChannelError as exc:
            failed.append({"channel": ch, "why": str(exc)})
            log(ch, "failed", due=len(rows))
    if a.json:
        print(json.dumps({"due": len(rows), "sent": sent, "failed": failed}, indent=1))
    else:
        for s in sent:
            print("gt-reminder: %s -- %s" % (s["channel"], s["how"]))
        for f in failed:
            print("gt-reminder: %s could not deliver -- %s" % (f["channel"], f["why"]),
                  file=sys.stderr)
    return PROBLEM if failed else OK


def do_status(a) -> int:
    print("%-8s %-16s %-10s %s" % ("CHANNEL", "SETTING", "VALUE", "READY"))
    for ch in CHANNELS:
        val = setting(SETTING[ch], "on" if ch == "session" else "off")
        ready = "yes" if ch == "session" else (configured(ch) or "yes")
        print("%-8s %-16s %-10s %s" % (ch, SETTING[ch], val, ready))
    print("window: overdue + due within %dd (reminder_days)" % window_days())
    m = load_mirror()
    if m is None:
        print("mirror: none at %s" % mirror_path())
    else:
        age = mirror_age_days(m)
        print("mirror: %d row(s), refreshed %s%s" % (
            len(m["rows"]), m.get("mirrored", "?"),
            " -- STALE" if age is not None and age > MIRROR_STALE_DAYS else ""))
    try:
        sched = _mod("gt_schedule")
        print("schedule: %s" % ("installed (%s)" % sched.plist_path("reminder")
                                if sched.plist_path("reminder").is_file() else
                                "not installed -- gt_schedule.py install reminder --vault V"))
    except Exception:
        pass
    return OK


def do_import_tsv(a) -> int:
    """Fold a `label<TAB>YYYY-MM-DD` file into deadlines.md, through the write queue."""
    q = _mod("gt_write_queue")
    gs = _mod("gt_surface")
    vault = q.find_vault(a.vault)
    if vault is None:
        print("gt-reminder: no vault -- pass --vault", file=sys.stderr)
        return USAGE
    target = vault / gs.DEADLINES
    if not target.is_file():
        print("gt-reminder: %s does not exist -- create the table first" % target,
              file=sys.stderr)
        return PROBLEM
    existing, _ = gs.read_deadlines(vault)
    have = {(r["item"].strip().lower(), r["due"].isoformat()) for r in existing}
    have_labels = {r["item"].strip().lower() for r in existing}
    new, notes = [], []
    for n, line in enumerate(Path(a.tsv).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            notes.append("line %d: not `label<TAB>date` -- skipped" % n)
            continue
        label, due = parts[0].strip(), parts[1].strip()
        try:
            datetime.date.fromisoformat(due)
        except ValueError:
            notes.append("line %d: %r is not YYYY-MM-DD -- skipped" % (n, due))
            continue
        if a.only and not any(o.lower() in label.lower() for o in a.only):
            notes.append("line %d: not selected by --only -- skipped: %s" % (n, label))
            continue
        if gs.looks_secret(label):
            notes.append("line %d: label looks like a credential -- skipped" % n)
            continue
        if (label.lower(), due) in have or label.lower() in have_labels:
            notes.append("line %d: already in deadlines.md -- skipped: %s" % (n, label))
            continue
        new.append("| %s | %s | %s | |" % (label.replace("|", "/"), a.category, due))
    for x in notes:
        print("gt-reminder import: %s" % x)
    if not new:
        print("gt-reminder import: nothing to add")
        return OK
    text = target.read_bytes().decode("utf-8")
    lines = text.split("\n")
    last = max((i for i, l in enumerate(lines) if l.lstrip().startswith("|")), default=None)
    if last is None:
        print("gt-reminder import: deadlines.md has no table to add rows to", file=sys.stderr)
        return PROBLEM
    lines[last + 1:last + 1] = new
    for r in new:
        print("gt-reminder import: %s %s" % ("would add" if a.dry_run else "adding", r))
    if a.dry_run:
        return OK
    results, note = q.submit(vault, [{"path": gs.DEADLINES, "op": "replace-file",
                                      "content": "\n".join(lines),
                                      "hint": "reminder: import %d row(s) from %s"
                                              % (len(new), Path(a.tsv).name)}])
    for r in results:
        print("gt-reminder import: %s -> %s%s" % (r["path"], r.get("decision"),
                                                  " (%s)" % r["reason"] if r.get("reason")
                                                  else ""))
    if note:
        print("gt-reminder import: %s; %s" % (note, q.drain_hint(vault)))
    return PROBLEM if any(r.get("decision") in ("refused", "reject") for r in results) else OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gt_reminder.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="channels, settings, readiness, mirror and schedule")
    s = sub.add_parser("setup", help="how to set a channel up")
    s.add_argument("channel", choices=CHANNELS)
    c = sub.add_parser("check", help="send one test message through a channel")
    c.add_argument("channel", choices=CHANNELS)
    c.add_argument("--vault")
    c.add_argument("--json", action="store_true")
    m = sub.add_parser("mirror", help="copy deadlines.md out of the vault for the scheduled job")
    m.add_argument("--vault")
    m.add_argument("--dry-run", action="store_true")
    r = sub.add_parser("run", help="send due reminders through the enabled channels")
    r.add_argument("--dry-run", action="store_true", help="print the message, send nothing")
    r.add_argument("--json", action="store_true")
    r.add_argument("--check", action="store_true",
                   help="preflight only: mirror readable, a channel on and configured")
    r.add_argument("--scheduled", action="store_true", help="run by launchd: quiet when nothing is due")
    i = sub.add_parser("import-tsv", help="fold a label<TAB>date file into deadlines.md")
    i.add_argument("--vault")
    i.add_argument("--tsv", required=True)
    i.add_argument("--only", action="append", default=[], metavar="TEXT",
                   help="import only labels containing TEXT (repeat)")
    i.add_argument("--category", default="reminder")
    i.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    fn = {"status": do_status, "setup": do_setup, "check": do_check, "mirror": do_mirror,
          "run": do_run, "import-tsv": do_import_tsv}[a.cmd]
    return fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
