#!/usr/bin/env python3
"""gt_farm_engine.py -- run a local engine as a service of its own, never as a child of a Claude session.

    gt_farm_engine.py install NAME --model FILE --port N [--alias A] [--context N] [--slots N]
                      [--threads N] [--llama-server FILE] [--host 127.0.0.1] [--system]
                      [--allow-low-ram] [--wait S] [--check] [--dry-run] [--json]
    gt_farm_engine.py check NAME [--json]
    gt_farm_engine.py status [NAME] [--json]
    gt_farm_engine.py remove NAME [--keep-log] [--wait S] [--check] [--dry-run] [--json]

WHY THIS EXISTS. An engine started from inside a Claude session (a background task of the session)
belongs to that session. On 2026-10-14 a llama.cpp server launched that way exited 144 -- SIGTERM --
the instant the session reaped it; and when it does survive, it dies with /exit, /compact or a crash,
the 13-17 GB model has to be loaded again by hand, and gt-farm has no stable always-on endpoint to
send packets to. So the engine is a SERVICE: the session talks to it over 127.0.0.1:<port> and never
owns the process.

WHAT `install` DOES. macOS: a per-user LaunchAgent (RunAtLoad, KeepAlive, ThrottleInterval 10, output
to a log file). Linux: a systemd user unit. Both run a generated control script, `llama-ctl.sh run`,
which `exec`s llama-server in the foreground, so the supervisor tracks the real PID. The service lives
under the label io.goldenthread.farm-engine.NAME (macOS) or the unit gt-farm-engine-NAME.service
(Linux); a hand-made service from another recipe is never touched. `--system` stages a LaunchDaemon
or a systemd system unit instead (boot-time, survives logout) and PRINTS the sudo commands that
install it: this script never runs sudo. Windows is not supported.

  The engine listens on 127.0.0.1 ONLY. It has no authentication, so `--host 0.0.0.0` or any
  other address is refused. To reach an engine from another machine, keep it on loopback and put a
  boundary you own in front of it (an SSH forward, or a reverse proxy that authenticates).

  The RAM rule: model file size + 6 GiB must fit in free RAM (Linux, Intel Mac) or in 75% of the
  unified memory (Apple silicon), or `install` refuses. A model that does not fit pages on every
  token and throughput falls to about a third, with no warning. `--allow-low-ram` installs anyway
  and prints the warning. Threads default to physical cores - 1, slots to 4, context to 16384.

  `install` PROVES ITSELF: it loads the service, then polls http://127.0.0.1:PORT/health for at
  most `--wait` seconds (default 180; a large model takes a while to load). If the engine does not
  answer, or the load is refused, or the process is interrupted, it ROLLS BACK: unloads the service,
  removes every file it wrote and puts back any file it replaced from a backup. Run again with the
  same options it changes nothing; with other options it replaces what differs (backing it up
  first) and restarts the service. A second `install NAME` with no options reuses the recorded ones.

  `--check` changes nothing: exit 0 when nothing would change, 3 when install (or remove) would
  change something. `--dry-run` prints the files and the commands and changes nothing.

  `check NAME` audits an installed engine: every file it wrote is unedited, the service is loaded and
  /health answers (exit 0), or it says what drifted (exit 3). `status` reports without judging.
  `remove` unloads the service and deletes only the files the install recorded, putting back any
  file the install had replaced.

  CALIBRATION (0.21.4). The install ends by printing the calibration command: an engine whose model has not
  been measured gets no automatic work (gt_farm_calibrate.py). With `farm_first` yes it runs the calibration
  once itself (the Claude cost stated first, the model_calibration_budget respected); `farm_first` off prints
  nothing. The calibration is keyed by the model, so it need not wait for the service-table row.

  What was written is recorded in ~/.claude/golden-thread/farm/engines/NAME/record.json beside the
  control script and the log. Nothing is written to the vault: `install` prints the farm-services
  row (local: yes, transport chat-completions, the stable base_url) for you to add.

Exit: 0 ok or current | 1 failed (rolled back) | 2 usage or refusal | 3 would change, or drifted |
      4 not installed.
"""
import argparse
import datetime
import getpass
import hashlib
import json
import os
import platform as _platform
import plistlib
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request

GT_VERSION = "0.21.4"            # the gt-farm module version
LOOPBACK = "127.0.0.1"
HEADROOM_GIB = 6                 # working memory the engine needs beyond the weights
APPLE_BUDGET = 0.75              # share of unified memory the GPU may use on Apple silicon
GIB = 1024 ** 3
DEFAULT_CONTEXT = 16384
DEFAULT_SLOTS = 4
DEFAULT_WAIT = 180.0
POLL_SECONDS = 0.5
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
SHARD_RE = re.compile(r"^(?P<stem>.+)-(?P<i>\d{5})-of-(?P<n>\d{5})\.gguf$")
LABEL_PREFIX = "io.goldenthread.farm-engine."
UNIT_PREFIX = "gt-farm-engine-"
# Where a llama-server is looked for when --llama-server is not given and PATH has none.
EXTRA_CANDIDATES = ("~/src/llama.cpp/build/bin/llama-server", "~/llama.cpp/build/bin/llama-server",
                    "/opt/llm/llama.cpp/build/bin/llama-server", "/opt/homebrew/bin/llama-server",
                    "/usr/local/bin/llama-server")
# The row this script prints for the farm-services table. The table may also carry an optional
# `start` column (gt 0.21.4, read by gt core's gt_vault_answer for vault_answers_start: one
# executable that starts a stopped engine on this machine, run only on the user's yes). A row for an
# engine installed HERE leaves it out on purpose: this engine's launchd/systemd service record is
# already its start command, and gt_vault_answer looks that up first. See gt_farm_send.py's
# docstring for every column.
TABLE_HEADER = ("| name | transport | base_url | model | pool | local | key_file "
                "| reference_rate_per_mtok | notes |")
TABLE_RULE = "|---|---|---|---|---|---|---|---|---|"


class Refusal(Exception):
    """Exit 2: the request is not one this command will carry out."""


class Failure(Exception):
    """Exit 1: something was tried and did not work; the caller rolls back."""


class Interrupted(Exception):
    """SIGTERM or Ctrl-C during an install: roll back, then exit 1."""


# ---- small helpers ------------------------------------------------------------------------

def home():
    return os.path.expanduser("~")


def state_dir():
    return os.path.join(home(), ".claude", "golden-thread", "farm", "engines")


def say(text="", indent=0):
    """A prose line, wrapped so it never runs past a laptop-width terminal."""
    if not text:
        print()
        return
    pad = " " * indent
    print(textwrap.fill(text, width=78, initial_indent=pad, subsequent_indent=pad + "  "))


def prose(text, indent=0, first=""):
    """A sentence wrapped to 78 columns, as one string of lines (for the lists the commands print)."""
    pad = " " * indent
    return textwrap.fill(text, width=78, initial_indent=pad + first, subsequent_indent=pad + "  ")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def read_bytes(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def atomic_write(path, data, mode=0o644):
    """Write beside the target, then rename over it: a failed write leaves the old file as it was."""
    d = os.path.dirname(path)
    tmp = os.path.join(d, ".%s.%d.tmp" % (os.path.basename(path), os.getpid()))
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def now_stamp():
    return datetime.datetime.now().strftime("%Y%m%dT%H%M%S")


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gib(n):
    if 0 < n < GIB // 10:
        return "%.1f MiB" % (n / float(1024 ** 2))
    return "%.1f GiB" % (n / float(GIB))


def platform_name():
    """darwin, linux or the raw name. GT_FARM_PLATFORM is for tests only."""
    p = os.environ.get("GT_FARM_PLATFORM") or sys.platform
    if p == "darwin":
        return "darwin"
    if p.startswith("linux"):
        return "linux"
    return p


def require_platform():
    plat = platform_name()
    if plat not in ("darwin", "linux"):
        raise Refusal("this command supports macOS (launchd) and Linux (systemd); on %s run the "
                      "engine under your own supervisor, bound to 127.0.0.1" % plat)
    return plat


def refuse_root():
    """A user service must belong to the user. Run under sudo it would land in root's home and root's
    login session, and nothing here needs root: --system prints the sudo steps instead. (A run with
    the stand-in service manager, which is tests only, is exempt: CI containers run as root.)"""
    if os.environ.get("GT_FARM_CTL_STUB") or not hasattr(os, "geteuid"):
        return
    if os.geteuid() == 0:
        raise Refusal("run this as your own user, not as root or under sudo: the service must belong to "
                      "you, and nothing here needs root (--system stages a boot-time service and prints "
                      "the sudo commands for you to run)")


# ---- the service manager (launchctl / systemctl) -----------------------------------------

class Res(object):
    def __init__(self, rc, out="", err=""):
        self.rc, self.out, self.err = rc, out, err


def ctl(args, timeout=60):
    """Run launchctl or systemctl. GT_FARM_CTL_STUB (tests only) names a script that stands in for
    both; with GT_TEST_SANDBOX set and no stub, nothing runs at all, so a test can never reach the
    real launchd or systemd of the machine it runs on."""
    stub = os.environ.get("GT_FARM_CTL_STUB")
    if stub:
        argv = [sys.executable, stub] + [str(a) for a in args]
    elif os.environ.get("GT_TEST_SANDBOX"):
        return Res(1, "", "refused: %s from a test/sandbox (GT_TEST_SANDBOX set, no GT_FARM_CTL_STUB); "
                          "the real service manager is never touched from one" % args[0])
    else:
        argv = [str(a) for a in args]
    try:
        p = subprocess.run(argv, capture_output=True, universal_newlines=True, timeout=timeout)
        return Res(p.returncode, p.stdout, p.stderr)
    except (OSError, subprocess.SubprocessError) as exc:
        return Res(127, "", str(exc))


def have_ctl(plat):
    if os.environ.get("GT_FARM_CTL_STUB"):
        return True
    if os.environ.get("GT_TEST_SANDBOX"):
        return False
    return shutil.which("launchctl" if plat == "darwin" else "systemctl") is not None


def why(r):
    return ((r.err or "").strip() or (r.out or "").strip() or "exit %d" % r.rc)[-400:]


def domain(scope):
    return "gui/%d" % os.getuid() if scope == "user" else "system"


def svc_loaded(L):
    """True / False, or None when the service manager will not say (a macOS system daemon needs root)."""
    if L["platform"] == "darwin":
        r = ctl(["launchctl", "print", "%s/%s" % (domain(L["scope"]), L["label"])])
        if r.rc == 0:
            return True
        return False if L["scope"] == "user" else None
    args = ["systemctl"] + (["--user"] if L["scope"] == "user" else []) + ["is-active", L["label"]]
    return ctl(args).rc == 0


def svc_load(L):
    """Load (and start) a user service. -> None, or the reason it was refused."""
    if L["platform"] == "darwin":
        dom = domain("user")
        ctl(["launchctl", "bootout", "%s/%s" % (dom, L["label"])])          # idempotent
        r = ctl(["launchctl", "bootstrap", dom, L["file"]])
        if r.rc != 0:
            time.sleep(1.0)                                                 # a bootout may still be settling
            r2 = ctl(["launchctl", "bootstrap", dom, L["file"]])
            if r2.rc != 0:
                return ("launchctl bootstrap refused %s: %s (a LaunchAgent needs a logged-in graphical "
                        "session; for a headless Mac use --system)" % (L["file"], why(r2)))
        return None
    r = ctl(["systemctl", "--user", "daemon-reload"])
    if r.rc != 0:
        return "systemctl --user daemon-reload failed: %s (no user session bus? log in, or use --system)" % why(r)
    r = ctl(["systemctl", "--user", "enable", "--now", L["label"]])
    if r.rc != 0:
        return "systemctl --user enable --now %s failed: %s" % (L["label"], why(r))
    return None


def svc_unload(L):
    """Stop and unload a user service. -> None, or the reason it could not be."""
    if L["platform"] == "darwin":
        r = ctl(["launchctl", "bootout", "%s/%s" % (domain("user"), L["label"])])
        if r.rc != 0 and svc_loaded(L):
            return "launchctl bootout %s failed: %s" % (L["label"], why(r))
        return None
    r = ctl(["systemctl", "--user", "disable", "--now", L["label"]])
    if r.rc != 0 and svc_loaded(L):
        return "systemctl --user disable --now %s failed: %s" % (L["label"], why(r))
    return None


def reload_manager(L):
    if L["platform"] == "linux" and L["scope"] == "user":
        ctl(["systemctl", "--user", "daemon-reload"])


# ---- the machine ---------------------------------------------------------------------------

def sysctl(key):
    try:
        p = subprocess.run(["sysctl", "-n", key], capture_output=True, universal_newlines=True, timeout=10)
        return p.stdout.strip() if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def physical_cores():
    """Physical cores, not hardware threads. Half the logical count when it cannot be read."""
    n = None
    if platform_name() == "darwin":
        v = sysctl("hw.physicalcpu")
        n = int(v) if v.isdigit() else None
    else:
        try:
            p = subprocess.run(["lscpu", "-p=CORE,SOCKET"], capture_output=True, universal_newlines=True,
                               timeout=10)
            seen = set(l.strip() for l in p.stdout.splitlines() if l.strip() and not l.startswith("#"))
            n = len(seen) or None
        except (OSError, subprocess.SubprocessError):
            n = None
    if not n:
        n = max(1, ((os.cpu_count() or 2) + 1) // 2)
    return n


def default_threads(cores=None):
    return max(1, (cores if cores is not None else physical_cores()) - 1)


def is_apple_silicon():
    if platform_name() != "darwin":
        return False
    return sysctl("hw.optional.arm64") == "1" or _platform.machine() == "arm64"


def meminfo_kib(field):
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(field + ":"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def intel_mac_available():
    """Free + inactive + speculative pages, from vm_stat."""
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, universal_newlines=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"page size of (\d+) bytes", out)
    size = int(m.group(1)) if m else 4096
    pages = 0
    for key in ("Pages free", "Pages inactive", "Pages speculative"):
        m = re.search(r"%s:\s+(\d+)" % key, out)
        if m:
            pages += int(m.group(1))
    return pages * size or None


def ram_budget(already_resident=0):
    """-> (bytes or None, how it was derived). `already_resident` is memory an engine of ours that is
    running now already holds; it is free again once that engine restarts, so it is added back."""
    env = os.environ.get("GT_FARM_RAM_BUDGET_BYTES")          # tests only
    if env:
        return int(env), "GT_FARM_RAM_BUDGET_BYTES (tests only)"
    if platform_name() == "darwin":
        total = sysctl("hw.memsize")
        if is_apple_silicon():
            if not total.isdigit():
                return None, "unified memory size unreadable"
            return int(int(total) * APPLE_BUDGET), "75%% of the %s of unified memory" % gib(int(total))
        avail = intel_mac_available()
        if avail is None:
            return None, "free memory unreadable"
        return avail + already_resident, "free RAM (free + inactive pages)"
    kib = meminfo_kib("MemAvailable")
    if kib is None:
        return None, "MemAvailable unreadable"
    return kib * 1024 + already_resident, "free RAM (MemAvailable)"


def model_bytes(path):
    """Size of the model file; a split GGUF (name-00001-of-00003.gguf) is the sum of its parts."""
    m = SHARD_RE.match(os.path.basename(path))
    if not m:
        return os.path.getsize(path)
    d = os.path.dirname(path) or "."
    total, found = 0, 0
    for f in os.listdir(d):
        mm = SHARD_RE.match(f)
        if mm and mm.group("stem") == m.group("stem") and mm.group("n") == m.group("n"):
            total += os.path.getsize(os.path.join(d, f))
            found += 1
    if found != int(m.group("n")):
        raise Refusal("%s is part of a split model of %d files but %d are beside it"
                      % (os.path.basename(path), int(m.group("n")), found))
    return total


def protected_folder(path):
    """The privacy-protected or synced folder a path is under, or None (macOS)."""
    h = home()
    for rel in ("Documents", "Desktop", "Downloads", "Library/Mobile Documents", "Library/CloudStorage"):
        root = os.path.join(h, rel)
        real = os.path.realpath(path)
        if path.startswith(root + os.sep) or real.startswith(os.path.realpath(root) + os.sep):
            return "~/" + rel
    return None


def check_gguf(path):
    """A GGUF file starts with the four bytes GGUF. A failed download leaves an HTML error page that
    the engine would reject minutes into a wait."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4)
    except OSError as exc:
        raise Refusal("cannot read the model file %s: %s" % (path, exc))
    if head != b"GGUF":
        raise Refusal("%s is not a GGUF file (it does not start with the GGUF header; a failed download "
                      "often leaves an error page with the right name)" % os.path.basename(path))


def port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((LOOPBACK, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def health(port, timeout=2.0):
    """-> (ok, detail) for http://127.0.0.1:PORT/health. Proxies in the environment are ignored."""
    url = "http://%s:%d/health" % (LOOPBACK, port)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as r:
            body = r.read(4000).decode("utf-8", "replace")
            if r.status != 200:
                return False, "HTTP %d" % r.status
            try:
                st = json.loads(body).get("status")
            except (ValueError, AttributeError):
                st = None
            if st not in (None, "ok"):
                return False, "status %s" % st
            return True, "ok"
    except urllib.error.HTTPError as e:
        return False, "HTTP %d%s" % (e.code, " (still loading the model)" if e.code == 503 else "")
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, "no answer (%s)" % (getattr(e, "reason", None) or e)


def wait_healthy(port, wait):
    """-> (ok, seconds waited, last detail). Bounded; checks once even when wait is 0."""
    start = time.time()
    deadline = start + max(0.0, wait)
    detail = ""
    while True:
        ok, detail = health(port, timeout=2.0)
        if ok:
            return True, time.time() - start, detail
        if time.time() >= deadline:
            return False, time.time() - start, detail
        time.sleep(POLL_SECONDS)


# ---- where things live ---------------------------------------------------------------------

def layout(name, plat, scope):
    edir = os.path.join(state_dir(), name)
    L = {"name": name, "platform": plat, "scope": scope, "edir": edir,
         "ctl": os.path.join(edir, "llama-ctl.sh"), "log": os.path.join(edir, "engine.log"),
         "record": os.path.join(edir, "record.json")}
    user = scope == "user"
    if plat == "darwin":
        label = LABEL_PREFIX + name
        L.update(label=label, role="plist", kind="LaunchAgent" if user else "LaunchDaemon",
                 manager="launchd")
        L["file"] = (os.path.join(home(), "Library", "LaunchAgents", label + ".plist") if user
                     else os.path.join(edir, label + ".plist"))
        L["system_file"] = None if user else system_root() + "/Library/LaunchDaemons/%s.plist" % label
    else:
        unit = UNIT_PREFIX + name + ".service"
        L.update(label=unit, role="unit", kind="systemd user unit" if user else "systemd system unit",
                 manager="systemd")
        L["file"] = (os.path.join(home(), ".config", "systemd", "user", unit) if user
                     else os.path.join(edir, unit))
        L["system_file"] = None if user else system_root() + "/etc/systemd/system/%s" % unit
    return L


def system_root():
    """Prefix of the system paths a `--system` install prints commands for. Empty, except in tests
    (GT_FARM_SYSTEM_ROOT), which have no business writing /Library or /etc."""
    return os.environ.get("GT_FARM_SYSTEM_ROOT", "").rstrip("/")


def base_url(port):
    return "http://%s:%d/v1" % (LOOPBACK, port)


# ---- the files ----------------------------------------------------------------------------

CTL_SCRIPT = """#!/bin/bash
# Control script for the local engine "{name}" (a llama.cpp server).
# Generated by gt_farm_engine.py (gt-farm). Do not edit by hand: change the engine with
#   gt_farm_engine.py install {name} ...      `check` reports a hand edit as drift.
#
#   run                         exec the engine in the foreground. This is what launchd or systemd
#                               supervises: no PID file, the supervisor tracks the real PID.
#   start|stop|restart|status   manage a detached copy by hand. Do not mix them with the service.
#
# The engine listens on 127.0.0.1 only and has no authentication.
set -u
LLAMA_SERVER={llama_server}
MODEL={model}
ALIAS={alias}
HOST=127.0.0.1
PORT={port}
CONTEXT={context}
SLOTS={slots}
THREADS={threads}
DIR={edir}
LOG={log}
PIDFILE="$DIR/engine.pid"
CMD=("$LLAMA_SERVER" -m "$MODEL" -t "$THREADS" -c "$CONTEXT" -np "$SLOTS" --host "$HOST" --port "$PORT" --alias "$ALIAS")

alive() {{
  [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}}

case "${{1:-}}" in
  run)
    exec "${{CMD[@]}}"
    ;;
  start)
    if alive; then echo "already running (pid $(cat "$PIDFILE"))"; exit 0; fi
    nohup "${{CMD[@]}}" >>"$LOG" 2>&1 &
    echo $! >"$PIDFILE"
    echo "started (pid $!), log $LOG"
    ;;
  stop)
    if alive; then
      kill "$(cat "$PIDFILE")"
      rm -f "$PIDFILE"
      echo "stopped"
    else
      rm -f "$PIDFILE"
      echo "not running"
    fi
    ;;
  restart)
    "$0" stop
    sleep 1
    "$0" start
    ;;
  status)
    if alive; then echo "running (pid $(cat "$PIDFILE"))"; else echo "not running (by this script)"; fi
    if command -v curl >/dev/null 2>&1; then
      if curl -fsS --max-time 3 "http://$HOST:$PORT/health" >/dev/null 2>&1; then
        echo "health: ok"
      else
        echo "health: no answer on http://$HOST:$PORT/health"
        exit 1
      fi
    else
      echo "health: curl not found, not checked"
    fi
    ;;
  *)
    echo "usage: $0 run|start|stop|restart|status" >&2
    exit 2
    ;;
esac
"""


def render_ctl(P, L):
    q = shlex.quote
    return CTL_SCRIPT.format(name=P["name"], llama_server=q(P["llama_server"]), model=q(P["model"]),
                             alias=q(P["alias"]), port=int(P["port"]), context=int(P["context"]),
                             slots=int(P["slots"]), threads=int(P["threads"]), edir=q(L["edir"]),
                             log=q(L["log"])).encode("utf-8")


def unit_text(value, dollars=False):
    """A value placed in a systemd unit: % starts a specifier, and in ExecStart $ starts a variable."""
    value = value.replace("%", "%%")
    return value.replace("$", "$$") if dollars else value


def render_service(P, L):
    if L["platform"] == "darwin":
        doc = {"Label": L["label"], "ProgramArguments": ["/bin/bash", L["ctl"], "run"],
               "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
               "StandardOutPath": L["log"], "StandardErrorPath": L["log"],
               "WorkingDirectory": L["edir"]}
        if L["scope"] == "system":
            doc["UserName"] = getpass.getuser()           # a daemon runs as root unless told otherwise
        return plistlib.dumps(doc)
    lines = ["[Unit]",
             "Description=Local LLM engine %s (llama.cpp server, managed by gt-farm)" % P["name"],
             "After=network.target", "",
             "[Service]", "Type=simple",
             "ExecStart=/bin/bash %s run" % unit_text(shlex.quote(L["ctl"]), dollars=True),
             "WorkingDirectory=%s" % unit_text(L["edir"]),
             "Restart=always", "RestartSec=10",
             "StandardOutput=append:%s" % unit_text(L["log"]),
             "StandardError=append:%s" % unit_text(L["log"])]
    if L["scope"] == "system":
        lines.append("User=%s" % getpass.getuser())
    lines += ["", "[Install]", "WantedBy=%s" % ("default.target" if L["scope"] == "user"
                                                 else "multi-user.target"), ""]
    return "\n".join(lines).encode("utf-8")


def desired_files(P, L):
    """-> [{path, data, mode, role}] -- every file an install writes."""
    return [{"path": L["ctl"], "data": render_ctl(P, L), "mode": 0o755, "role": "ctl"},
            {"path": L["file"], "data": render_service(P, L), "mode": 0o644, "role": L["role"]}]


def services_row(P):
    return "| %s | chat-completions | %s | %s | | yes | | | %s service, %d slots, context %d |" % (
        P["name"], base_url(int(P["port"])), P["alias"],
        "launchd" if platform_name() == "darwin" else "systemd", int(P["slots"]), int(P["context"]))


def sudo_install_commands(L, replacing):
    q = shlex.quote
    if L["platform"] == "darwin":
        cmds = []
        if replacing:
            cmds.append("sudo launchctl bootout system/%s" % L["label"])
        cmds += ["sudo install -o root -g wheel -m 644 %s %s" % (q(L["file"]), q(L["system_file"])),
                 "sudo launchctl bootstrap system %s" % q(L["system_file"])]
        return cmds
    cmds = ["sudo install -m 644 %s %s" % (q(L["file"]), q(L["system_file"])),
            "sudo systemctl daemon-reload",
            "sudo systemctl %s %s" % ("restart" if replacing else "enable --now", L["label"])]
    return cmds


def sudo_remove_commands(L):
    q = shlex.quote
    if L["platform"] == "darwin":
        return ["sudo launchctl bootout system/%s" % L["label"], "sudo rm %s" % q(L["system_file"])]
    return ["sudo systemctl disable --now %s" % L["label"], "sudo rm %s" % q(L["system_file"]),
            "sudo systemctl daemon-reload"]


def user_load_commands(L):
    if L["platform"] == "darwin":
        return ["launchctl bootout %s/%s   (only if already loaded)" % (domain("user"), L["label"]),
                "launchctl bootstrap %s %s" % (domain("user"), shlex.quote(L["file"]))]
    return ["systemctl --user daemon-reload", "systemctl --user enable --now %s" % L["label"]]


def user_unload_commands(L):
    if L["platform"] == "darwin":
        return ["launchctl bootout %s/%s" % (domain("user"), L["label"])]
    return ["systemctl --user disable --now %s" % L["label"], "systemctl --user daemon-reload"]


# ---- the record ---------------------------------------------------------------------------

def record_path(name):
    return os.path.join(state_dir(), name, "record.json")


def load_record(name):
    try:
        with open(record_path(name), encoding="utf-8") as fh:
            rec = json.load(fh)
        return rec if isinstance(rec, dict) else None
    except (OSError, ValueError):
        return None


def all_records():
    out = []
    try:
        names = sorted(os.listdir(state_dir()))
    except OSError:
        return out
    for n in names:
        rec = load_record(n)
        if rec:
            out.append(rec)
    return out


def save_record(L, rec):
    os.makedirs(L["edir"], exist_ok=True)
    atomic_write(L["record"], (json.dumps(rec, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def layout_of(rec):
    return layout(rec["name"], rec["platform"], rec["scope"])


# ---- parameters and the pre-flight --------------------------------------------------------

def find_llama_server(explicit, recorded):
    if explicit:
        p = os.path.abspath(os.path.expanduser(explicit))
        if not (os.path.isfile(p) and os.access(p, os.X_OK)):
            raise Refusal("--llama-server %s is not an executable file" % explicit)
        return p
    for cand in [recorded, shutil.which("llama-server")] + [os.path.expanduser(c) for c in EXTRA_CANDIDATES]:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    raise Refusal("no llama-server found. Pass --llama-server FILE, put it on PATH, or build it "
                  "(FARM-LOCAL-ENGINES.md, section 3). Install it as a service afterwards.")


def resolve(a, prior):
    """-> (parameter dict, scope). Options not given come from the recorded install, then defaults."""
    if not NAME_RE.match(a.name or ""):
        raise Refusal("name %r: use 1-31 lower-case letters, digits and dashes, starting with a letter "
                      "or digit (it becomes part of a service label and a file name)" % a.name)
    scope = "system" if a.system else "user"
    if prior and prior.get("scope") != scope:
        raise Refusal("%s is installed as a %s service; remove it before installing it as a %s one"
                      % (a.name, prior.get("scope"), scope))
    R = (prior or {}).get("params") or {}
    host = a.host if a.host is not None else LOOPBACK
    if host not in (LOOPBACK, "localhost"):
        raise Refusal("--host %s. The engine has no authentication, so it is bound to %s only and never "
                      "to 0.0.0.0 or a LAN address. To reach it from another machine, keep it on "
                      "loopback and put a boundary you own in front of it (an SSH forward, or a "
                      "reverse proxy that authenticates)." % (host or "''", LOOPBACK))
    model = a.model or R.get("model")
    if not model:
        raise Refusal("--model FILE is required (the GGUF file the engine serves)")
    model = os.path.abspath(os.path.expanduser(model))
    port = a.port if a.port is not None else R.get("port")
    if port is None:
        raise Refusal("--port N is required (the port the engine listens on, e.g. 8080)")
    if not 1024 <= int(port) <= 65535:
        raise Refusal("--port %s: use a port from 1024 to 65535" % port)
    alias = a.alias or R.get("alias") or a.name
    if not ALIAS_RE.match(alias):
        raise Refusal("--alias %r: use letters, digits and . _ : - (it is the model name a result reports)" % alias)
    P = {"name": a.name, "model": model, "port": int(port), "alias": alias, "host": LOOPBACK,
         "context": a.context or R.get("context") or DEFAULT_CONTEXT,
         "slots": a.slots or R.get("slots") or DEFAULT_SLOTS,
         "threads": a.threads or R.get("threads") or default_threads()}
    P["llama_server"] = find_llama_server(a.llama_server, R.get("llama_server"))
    return P, scope


def preflight(P, L, prior, loaded, allow_low_ram, will_start):
    """Everything that can be refused before a file is written. -> [report line], [warning line]."""
    lines, warns = [], []
    if not os.path.isfile(P["model"]):
        raise Refusal("model file %s does not exist (or is not a file)" % P["model"])
    size = model_bytes(P["model"])
    if size <= 0:
        raise Refusal("model file %s is empty" % P["model"])
    check_gguf(P["model"])
    lines.append("model     %s (%s)" % (P["model"], gib(size)))
    if L["platform"] == "darwin" and protected_folder(P["model"]):
        warns.append("the model is in a folder macOS protects (%s): a background service is usually refused "
                     "access to it, and the engine will not start. Keep the model in a plain folder such as "
                     "~/models." % protected_folder(P["model"]))
    lines.append("engine    %s" % P["llama_server"])
    lines.append("threads   %d   slots %d   context %d   alias %s" % (
        P["threads"], P["slots"], P["context"], P["alias"]))
    others = [r for r in all_records() if r.get("name") != P["name"]]
    for r in others:
        if (r.get("params") or {}).get("port") == P["port"]:
            raise Refusal("port %d is already the port of the engine %r" % (P["port"], r["name"]))
    # The listener is ours only when the service manager says our service is loaded (or, where it will
    # not say -- a macOS system daemon -- when this engine is the recorded owner of the port). A hand-run
    # server on the same port must never be mistaken for it: its /health would pass the proof.
    ours_running = bool(prior and (prior.get("params") or {}).get("port") == P["port"]
                        and (loaded is True or (loaded is None and health(P["port"])[0])))
    if not ours_running and not port_free(P["port"]):
        raise Refusal("port %d on %s is already in use (an engine started by hand, or another service; "
                      "an earlier hand-made %s is stopped with %s). Stop it or choose another --port."
                      % (P["port"], LOOPBACK, "LaunchAgent" if L["platform"] == "darwin" else "unit",
                         "launchctl bootout gui/$(id -u)/<its label>" if L["platform"] == "darwin"
                         else "systemctl disable --now <its unit>"))
    lines.append("port      %s:%d %s" % (LOOPBACK, P["port"], "held by this engine" if ours_running else "is free"))
    if will_start:
        resident = 0
        if prior and ours_running:
            try:
                resident = model_bytes((prior.get("params") or {}).get("model") or "")
            except (OSError, Refusal):
                resident = 0
        budget, how = ram_budget(resident)
        need = size + HEADROOM_GIB * GIB
        if budget is None:
            warns.append("could not read the memory size (%s), so the RAM rule was not applied" % how)
        elif need > budget:
            msg = ("the model is %s and needs %d GiB of headroom, %s in all, but the budget is %s "
                   "(%s). A model that does not fit pages on every token and throughput falls to about "
                   "a third, with no warning. Choose a smaller quantisation or free memory."
                   % (gib(size), HEADROOM_GIB, gib(need), gib(budget), how))
            if not allow_low_ram:
                raise Refusal("RAM rule: " + msg + " (--allow-low-ram installs it anyway.)")
            warns.append("RAM rule overridden by --allow-low-ram: " + msg)
            lines.append("ram       OVER BUDGET: needs %s, budget %s (%s)" % (gib(need), gib(budget), how))
        else:
            lines.append("ram       needs %s (model + %d GiB), budget %s (%s): fits" % (
                gib(need), HEADROOM_GIB, gib(budget), how))
    return lines, warns


# ---- install ------------------------------------------------------------------------------

def file_states(files):
    out = []
    for f in files:
        cur = read_bytes(f["path"])
        out.append(dict(f, state="absent" if cur is None else ("same" if cur == f["data"] else "differs")))
    return out


def tail(path, n=15):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()[-n:]
    except OSError:
        return []


def emit(a, obj, lines):
    if getattr(a, "json", False):
        print(json.dumps(obj, sort_keys=True))
    else:
        for l in lines:
            print(l)


def cmd_install(a):
    plat = require_platform()
    refuse_root()
    prior = load_record(a.name) if NAME_RE.match(a.name or "") else None
    P, scope = resolve(a, prior)
    L = layout(P["name"], plat, scope)
    files = file_states(desired_files(P, L))
    loaded = svc_loaded(L) if scope == "user" or plat == "linux" else None
    replacing_system = bool(L["system_file"] and os.path.exists(L["system_file"]))
    changed = [f for f in files if f["state"] != "same"]
    healthy = health(P["port"])[0]
    if scope == "user":
        current = prior is not None and not changed and loaded is True and healthy
    else:
        sysfile_ok = bool(L["system_file"]) and read_bytes(L["system_file"]) == files[1]["data"]
        current = prior is not None and not changed and sysfile_ok and healthy
    will_start = not current
    pre, warns = preflight(P, L, prior, loaded, a.allow_low_ram, will_start)
    row = services_row(P)
    plan = [{"path": f["path"], "role": f["role"], "action": {"absent": "create", "differs": "replace",
                                                           "same": "keep"}[f["state"]]} for f in files]
    obj = {"name": P["name"], "scope": scope, "platform": plat, "kind": L["kind"], "label": L["label"],
           "base_url": base_url(P["port"]), "files": plan, "services_row": row, "current": current,
           "warnings": warns}

    if a.check:
        todo = [p for p in plan if p["action"] != "keep"]
        if scope == "user":
            if loaded is not True:
                todo.append({"path": L["label"], "role": "service", "action": "load"})
            elif not healthy:
                todo.append({"path": L["label"], "role": "service", "action": "restart (no /health answer)"})
        elif not current:
            todo.append({"path": L["system_file"], "role": "system copy", "action": "install with sudo"})
        obj["would_change"] = [dict(t) for t in todo]
        lines = ["install --check %s (%s): %s" % (P["name"], L["kind"],
                                                  "nothing would change" if not todo else
                                                  "%d change(s) would be made" % len(todo))]
        for t in todo:
            lines.append("  would %s: %s" % (t["action"], t["path"]))
        lines.append("--check: nothing was changed.")
        emit(a, obj, lines)
        return 0 if not todo else 3

    if a.dry_run:
        lines = ["dry run: nothing was changed. %s would be installed as a %s." % (P["name"], L["kind"]),
                 ""] + ["  " + l for l in pre] + [""]
        for w in warns:
            lines.append(prose(w, first="warning: "))
        lines.append("would write %d file(s):" % len([f for f in files if f["state"] != "same"]))
        for f in files:
            lines.append("")
            lines.append("--- %s (%s, mode %o, %d bytes) ---" % (f["path"], {"absent": "new", "differs":
                         "replaces what is there; the old file is backed up", "same": "already current"}[f["state"]],
                         f["mode"], len(f["data"])))
            lines.extend(f["data"].decode("utf-8", "replace").rstrip("\n").split("\n"))
        lines.append("")
        if scope == "user":
            lines.append("would run:")
            lines += ["  " + c for c in user_load_commands(L)]
            lines.append("  poll http://%s:%d/health for up to %g s" % (LOOPBACK, P["port"], a.wait))
            lines.append("on any failure: unload the service, remove the files this run wrote, restore "
                         "the ones it replaced from their backups.")
        else:
            lines.append("it would then PRINT these commands for you to run (this script never runs sudo):")
            lines += ["  " + c for c in sudo_install_commands(L, replacing_system)]
        lines += ["", "the farm-services row to add (gt does not edit the table):", TABLE_HEADER, TABLE_RULE, row]
        emit(a, obj, lines)
        return 0

    if current:
        obj["state"] = "current"
        lines = ["%s: already current -- files unchanged, service loaded, /health answers. Nothing done." % P["name"]]
        lines += ["", "the farm-services row (already added? gt does not edit the table):", TABLE_HEADER,
                  TABLE_RULE, row]
        emit(a, obj, lines)
        return 0

    return install_for_real(a, P, L, scope, files, prior, loaded, pre, warns, obj, row, replacing_system)


class Rollback(object):
    """What this run changed, so that a failure can put it back."""

    def __init__(self, L, prior_record, was_loaded):
        self.L, self.prior_record, self.was_loaded = L, prior_record, was_loaded
        self.log_existed = os.path.exists(L["log"])
        self.written = []          # (path, backup path or None) in the order written
        self.made_dirs = []
        self.loaded_it = False
        self.stamp = now_stamp()

    def mkdir(self, d):
        missing = []
        cur = d
        while cur and not os.path.isdir(cur):
            missing.append(cur)
            cur = os.path.dirname(cur)
        for m in reversed(missing):
            os.mkdir(m)
            self.made_dirs.append(m)

    def backup_path(self, path):
        return os.path.join(self.L["edir"], "replaced", "%s.%s" % (os.path.basename(path), self.stamp))

    def write(self, path, data, mode):
        self.mkdir(os.path.dirname(path))
        bak = None
        if os.path.exists(path):
            bak = self.backup_path(path)
            self.mkdir(os.path.dirname(bak))
            shutil.copy2(path, bak)
        self.written.append((path, bak))
        atomic_write(path, data, mode)

    def undo(self):
        """-> [problem]. Unload, then put every file back, then the record, then tidy."""
        problems = []
        L = self.L
        if self.loaded_it and L["scope"] == "user":
            msg = svc_unload(L)
            if msg:
                problems.append(msg)
        for path, bak in reversed(self.written):
            try:
                if bak:
                    shutil.copy2(bak, path)
                    os.unlink(bak)
                elif os.path.exists(path):
                    os.unlink(path)
            except OSError as exc:
                problems.append("could not put %s back: %s" % (path, exc))
        if self.prior_record is not None:
            try:
                save_record(L, self.prior_record)
            except OSError as exc:
                problems.append("could not restore the record: %s" % exc)
        else:
            try:
                if os.path.exists(L["record"]):
                    os.unlink(L["record"])
            except OSError:
                pass
        if self.was_loaded and L["scope"] == "user" and self.prior_record is not None:
            msg = svc_load(L)
            problems.append("the previous service was restarted from its restored files"
                            if msg is None else "could not restart the previous service: " + msg)
        if not self.log_existed:
            try:
                os.unlink(L["log"])
            except OSError:
                pass
        for d in reversed(self.made_dirs):
            try:
                os.rmdir(d)
            except OSError:
                pass
        return problems


def install_for_real(a, P, L, scope, files, prior, loaded, pre, warns, obj, row, replacing_system):
    rb = Rollback(L, prior, bool(loaded))
    previous = {}
    for r in ((prior or {}).get("files") or []):
        previous[r.get("path")] = r.get("sha256")
    old_sigterm = signal.getsignal(signal.SIGTERM)

    def on_term(signum, frame):
        raise Interrupted("terminated by signal %d" % signum)
    signal.signal(signal.SIGTERM, on_term)
    try:
        for f in files:
            if f["state"] != "same":
                rb.write(f["path"], f["data"], f["mode"])
        backups = dict(((prior or {}).get("backups") or {}))
        for path, bak in rb.written:
            if bak and previous.get(path) != sha256_bytes(read_bytes(bak) or b""):
                backups.setdefault(path, {"backup": bak, "restore_on_remove": path not in previous})
        rec = {"v": 1, "name": P["name"], "platform": L["platform"], "scope": scope, "kind": L["kind"],
               "label": L["label"], "gt_farm": GT_VERSION, "params": P, "base_url": base_url(P["port"]),
               "installed": (prior or {}).get("installed") or now_iso(), "updated": now_iso(),
               "log": L["log"], "backups": backups,
               "files": [{"path": f["path"], "role": f["role"], "sha256": sha256_bytes(f["data"])} for f in files]}
        rb.mkdir(L["edir"])
        save_record(L, rec)
        if scope == "user":
            if not have_ctl(L["platform"]):
                tool = "launchctl" if L["platform"] == "darwin" else "systemctl"
                raise Failure("the service manager is not reachable (%s)" % (
                    "a test/sandbox run, and the real one is never touched from one"
                    if os.environ.get("GT_TEST_SANDBOX") and not os.environ.get("GT_FARM_CTL_STUB")
                    else "%s not found on this machine" % tool))
            if loaded:
                msg = svc_unload(L)
                if msg:
                    raise Failure(msg)
            rb.loaded_it = True
            msg = svc_load(L)
            if msg:
                raise Failure(msg)
            ok, secs, detail = wait_healthy(P["port"], a.wait)
            if not ok:
                raise Failure("%s did not answer http://%s:%d/health within %g s (last: %s)"
                              % (P["name"], LOOPBACK, P["port"], a.wait, detail))
    except (Exception, KeyboardInterrupt) as exc:
        reason = str(exc) if str(exc) else exc.__class__.__name__
        log_lines = tail(L["log"])
        problems = rb.undo()
        lines = [prose("%s: INSTALL FAILED and was rolled back -- %s" % (P["name"], reason))]
        if log_lines:
            lines.append("engine log (last lines):")
            lines += ["  | " + l for l in log_lines]
        lines.append("rolled back: the service is unloaded; %d file(s) removed or put back from their "
                     "backups." % len(rb.written))
        for p in problems:
            lines.append(prose(p, indent=2, first="note: "))
        obj.update(state="failed", error=reason, rollback_notes=problems)
        emit(a, obj, lines)
        return 1
    finally:
        signal.signal(signal.SIGTERM, old_sigterm)
    for path, bak in rb.written:                     # a replaced file of ours needs no backup kept
        if bak and not any(v.get("backup") == bak for v in rec["backups"].values()):
            try:
                os.unlink(bak)
            except OSError:
                pass
    try:
        os.rmdir(os.path.join(L["edir"], "replaced"))        # only when nothing is kept in it
    except OSError:
        pass
    lines = []
    if scope == "user":
        lines.append("%s: installed and healthy" % P["name"])
        lines.append("  %s %s answered /health after %.1f s" % (L["kind"], L["label"], secs))
        obj.update(state="running", health_seconds=round(secs, 1))
    else:
        lines.append("%s: STAGED, not running." % P["name"])
        lines.append(prose("The files are written; this script never runs sudo. Run these yourself:"))
        lines += ["  " + c for c in sudo_install_commands(L, replacing_system)]
        lines.append("then prove it with: gt_farm_engine.py check %s" % P["name"])
        obj.update(state="staged", sudo_commands=sudo_install_commands(L, replacing_system))
    lines.append("  base_url   %s   (alias %s, %d slots, context %d, %d threads)" % (
        base_url(P["port"]), P["alias"], P["slots"], P["context"], P["threads"]))
    lines.append("  service    %s -> %s" % (L["label"], L["file"]))
    lines.append("  control    %s" % L["ctl"])
    lines.append("  log        %s" % L["log"])
    lines.append("  recorded   %s" % L["record"])
    for w in warns:
        lines.append(prose(w, first="warning: "))
    if L["platform"] == "linux" and scope == "user":
        lines.append(prose("a systemd user unit stops when you log out; to keep it running without a "
                           "login run: loginctl enable-linger $USER", first="note: "))
    lines += ["", "Add this row to your farm-services table (gt does not edit it):", TABLE_HEADER, TABLE_RULE, row,
              "", "Then: gt_farm_send.py send PACKET --service %s --dry-run" % P["name"]]
    lines += calibration_lines(a, P, obj, scope)
    emit(a, obj, lines)
    return 0


def calibrate_tool():
    """The onboarding calibration script beside this one ($GT_FARM_CALIBRATE overrides, for a test)."""
    return os.environ.get("GT_FARM_CALIBRATE") or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                               "gt_farm_calibrate.py")


def farm_first():
    try:
        with open(os.path.join(home(), ".claude", "vault-config.json"), encoding="utf-8") as fh:
            v = str(json.load(fh).get("farm_first") or "suggest").strip().lower()
    except (OSError, ValueError, AttributeError):
        v = "suggest"
    return v if v in ("off", "suggest", "yes") else "suggest"


def calibration_lines(a, P, obj, scope):
    """The end of an install (0.21.4): a new engine gets no automatic work until its model has been measured
    (owner, 2026-10-15), so the install says so and prints the command. With farm_first yes and a healthy
    engine it runs the calibration once itself, which states its Claude cost first and respects
    model_calibration_budget. The calibration is keyed by the MODEL, so it applies to the service row whenever
    that is added, and a model measured before is not measured again."""
    first = farm_first()
    if first == "off":
        return []
    cmd = "python3 gt_farm_calibrate.py --base-url %s --name %s" % (base_url(P["port"]), P["name"])
    out = ["", prose("A new engine gets no automatic work until its model has been measured: harmless practice "
                     "tasks go to the engine and to Claude and a judge compares them. The engine's calls are free; "
                     "the Claude cost is estimated first.", first="calibration: "),
           "  plan and estimate:  %s --dry-run" % cmd, "  run it:             %s" % cmd,
           "  (once the row is in your table you can use the name alone: gt_farm_calibrate.py %s)" % P["name"]]
    obj["calibrate_command"] = cmd
    if first != "yes" or obj.get("state") != "running" or scope != "user":
        return out
    try:
        p = subprocess.run([sys.executable, calibrate_tool(), "--base-url", base_url(P["port"]), "--name", P["name"],
                            "--auto"], capture_output=True, text=True, timeout=3600)
        text = (p.stdout + p.stderr).strip().splitlines()
        out += ["", "farm_first is yes: calibrating %s now (exit %d)" % (P["name"], p.returncode)] + ["  " + l for l in text[-40:]]
        obj["calibration_exit"] = p.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        out += ["", "farm_first is yes, but the calibration could not start (%s); run it yourself" % type(exc).__name__]
    return out


# ---- check and status ---------------------------------------------------------------------

def inspect_engine(rec):
    L = layout_of(rec)
    files = []
    for f in rec.get("files") or []:
        cur = read_bytes(f["path"])
        st = "missing" if cur is None else ("ok" if sha256_bytes(cur) == f.get("sha256") else "edited")
        files.append({"path": f["path"], "role": f.get("role"), "state": st})
    port = int((rec.get("params") or {}).get("port") or 0)
    loaded = svc_loaded(L) if (rec["scope"] == "user" or rec["platform"] == "linux") else None
    ok, detail = health(port)
    sysstate = None
    if L["system_file"]:
        cur = read_bytes(L["system_file"])
        staged = read_bytes(L["file"])
        sysstate = "absent" if cur is None else ("ok" if staged is not None and cur == staged else "differs")
    return {"name": rec["name"], "scope": rec["scope"], "kind": rec.get("kind"), "label": rec["label"],
            "base_url": rec.get("base_url"), "files": files, "loaded": loaded, "healthy": ok,
            "health": detail, "system_copy": sysstate, "layout": L, "params": rec.get("params") or {}}


def verdict(i):
    bad = [f for f in i["files"] if f["state"] != "ok"]
    probs = ["%s: %s" % (f["state"], f["path"]) for f in bad]
    if i["loaded"] is False:
        probs.append("service %s is not loaded" % i["label"])
    if i["system_copy"] in ("absent", "differs"):
        probs.append("the system copy %s is %s -- run the sudo commands install printed"
                     % (i["layout"]["system_file"], "not installed" if i["system_copy"] == "absent"
                        else "different from the staged file"))
    if not i["healthy"]:
        probs.append("/health: %s" % i["health"])
    return probs


def public(i):
    return dict((k, v) for k, v in i.items() if k != "layout")


def cmd_check(a):
    rec = load_record(a.name)
    if not rec:
        emit(a, {"name": a.name, "installed": False}, ["%s: not installed (no record under %s)" % (
            a.name, os.path.join(state_dir(), a.name))])
        return 4
    i = inspect_engine(rec)
    probs = verdict(i)
    lines = []
    if not probs:
        lines.append("%s: ok -- files unedited, service loaded, /health answers" % a.name)
        lines.append("  %s" % i["base_url"])
    else:
        lines.append("%s: DRIFT -- %d problem(s)" % (a.name, len(probs)))
        lines += ["  " + p for p in probs]
        lines.append(prose("repair: gt_farm_engine.py install %s%s (re-runs with the recorded options; "
                           "an edited file is backed up first)"
                           % (a.name, " --system" if rec["scope"] == "system" else "")))
    emit(a, dict(public(i), ok=not probs, problems=probs), lines)
    return 0 if not probs else 3


def cmd_status(a):
    recs = [load_record(a.name)] if a.name else all_records()
    recs = [r for r in recs if r]
    if a.name and not recs:
        emit(a, {"name": a.name, "installed": False}, ["%s: not installed" % a.name])
        return 4
    if not recs:
        emit(a, {"engines": []}, ["no engine service installed by gt-farm (gt_farm_engine.py install NAME ...)"])
        return 0
    out, lines, worst = [], [], 0
    for rec in recs:
        i = inspect_engine(rec)
        P = i["params"]
        running = i["healthy"] and i["loaded"] is not False
        worst = max(worst, 0 if running else 1)
        state = "running, healthy" if running else (
            "loaded, not answering /health (%s)" % i["health"] if i["loaded"] else "NOT RUNNING (%s)" % i["health"])
        lines.append("%s  [%s, %s]  %s" % (rec["name"], i["kind"], rec["scope"], state))
        lines.append("  base_url   %s   alias %s   %s slots   context %s   %s threads" % (
            i["base_url"], P.get("alias"), P.get("slots"), P.get("context"), P.get("threads")))
        lines.append("  model      %s" % P.get("model"))
        lines.append("  service    %s -> %s" % (i["label"], i["layout"]["file"]))
        lines.append("  log        %s" % i["layout"]["log"])
        bad = [f for f in i["files"] if f["state"] != "ok"]
        if bad:
            lines.append("  files      " + "; ".join("%s %s" % (f["state"], f["path"]) for f in bad))
        if a.name:
            lines.append("  farm-services row:")
            lines.append("  " + services_row(P))
        out.append(dict(public(i), running=running))
    emit(a, {"engines": out} if not a.name else out[0], lines)
    return worst


# ---- remove -------------------------------------------------------------------------------

def cmd_remove(a):
    refuse_root()
    rec = load_record(a.name) if NAME_RE.match(a.name or "") else None
    if not rec:
        emit(a, {"name": a.name, "installed": False, "removed": False},
             ["%s: no record of an install under %s; nothing to remove (a service made by hand is never "
              "touched)" % (a.name, state_dir())])
        return 0
    L = layout_of(rec)
    scope = rec["scope"]
    loaded = svc_loaded(L) if (scope == "user" or L["platform"] == "linux") else None
    sys_present = bool(L["system_file"] and os.path.exists(L["system_file"]))
    recfiles = [f for f in (rec.get("files") or []) if os.path.exists(f["path"])]
    restores = [(p, b) for p, b in (rec.get("backups") or {}).items()
                if b.get("restore_on_remove") and os.path.exists(b.get("backup") or "")]
    port = int((rec.get("params") or {}).get("port") or 0)
    pending_sudo = scope == "system" and (sys_present or health(port)[0])
    todo = []
    if scope == "user" and loaded:
        todo.append("unload the service %s" % L["label"])
    todo += ["delete %s" % f["path"] for f in recfiles]
    todo += ["restore %s from its backup" % p for p, b in restores]
    if not a.keep_log and os.path.exists(L["log"]):
        todo.append("delete %s" % L["log"])
    todo.append("delete the record %s" % L["record"])
    cmds = sudo_remove_commands(L) if pending_sudo else []
    obj = {"name": a.name, "scope": scope, "would": todo, "sudo_commands": cmds}

    if a.check:
        lines = ["remove --check %s: %d change(s) would be made" % (a.name, len(todo) + len(cmds))]
        lines += ["  would " + t for t in todo]
        lines += ["  you would run: " + c for c in cmds]
        lines.append("--check: nothing was changed.")
        emit(a, obj, lines)
        return 3
    if a.dry_run:
        lines = ["dry run: nothing was changed. remove %s would:" % a.name]
        lines += ["  " + t for t in todo]
        if cmds:
            lines.append("it would first print these commands for you to run (this script never runs sudo):")
            lines += ["  " + c for c in cmds]
        emit(a, obj, lines)
        return 0
    if pending_sudo:
        lines = ["%s is a system service and still installed. This script never runs sudo; run:" % a.name]
        lines += ["  " + c for c in cmds]
        lines.append("then run gt_farm_engine.py remove %s again to delete the staged files and the record." % a.name)
        obj.update(state="waiting_for_sudo")
        emit(a, obj, lines)
        return 3
    problems, notes = [], []
    if scope == "user" and loaded:
        msg = svc_unload(L)
        if msg:
            problems.append(msg)
    for f in recfiles:
        try:
            os.unlink(f["path"])
        except OSError as exc:
            problems.append("could not delete %s: %s" % (f["path"], exc))
    for p, b in restores:
        try:
            shutil.copy2(b["backup"], p)
            os.unlink(b["backup"])
        except OSError as exc:
            problems.append("could not restore %s: %s" % (p, exc))
    if scope == "user":
        reload_manager(L)
    kept = []
    if not a.keep_log and os.path.exists(L["log"]):
        try:
            os.unlink(L["log"])
        except OSError as exc:
            problems.append("could not delete the log: %s" % exc)
    elif os.path.exists(L["log"]):
        kept.append(L["log"])
    if scope == "user":
        if svc_loaded(L):
            problems.append("the service %s is still loaded" % L["label"])
        elif loaded:
            # the engine had to stop with its service; give it a moment, then say if it did not
            deadline = time.time() + a.wait
            while health(port)[0] and time.time() < deadline:
                time.sleep(POLL_SECONDS)
            if health(port)[0]:
                problems.append("something still answers http://%s:%d/health after the service was "
                                "unloaded and %g s" % (LOOPBACK, port, a.wait))
        elif health(port)[0]:
            notes.append("something answers http://%s:%d/health, but it is not this service (it was not "
                         "loaded) and was left alone" % (LOOPBACK, port))
    if problems:
        lines = ["%s: REMOVE INCOMPLETE -- the record is kept so you can run it again" % a.name]
        lines += [prose(p, indent=2) for p in problems]
        obj.update(state="incomplete", problems=problems)
        emit(a, obj, lines)
        return 1
    try:
        os.unlink(L["record"])
    except OSError:
        pass
    for d in (os.path.join(L["edir"], "replaced"), L["edir"]):
        try:
            os.rmdir(d)
        except OSError:
            pass
    lines = ["%s: removed -- %s" % (a.name, "service unloaded, " if scope == "user" else "") +
             "%d file(s) deleted%s." % (len(recfiles), (", %d restored from backup" % len(restores)) if restores else "")]
    for n in notes:
        lines.append(prose(n, first="note: "))
    if kept:
        lines.append("kept: " + ", ".join(kept))
    left = [d for d in (os.path.join(L["edir"], "replaced"),) if os.path.isdir(d)]
    if left:
        lines.append("kept backups of files this install replaced: %s" % left[0])
    lines.append("Remove its row from your farm-services table (gt does not edit it).")
    obj.update(state="removed")
    emit(a, obj, lines)
    return 0


# ---- the command line ---------------------------------------------------------------------

def posint(v):
    try:
        n = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError("%r is not a whole number" % v)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def seconds(v):
    try:
        n = float(v)
    except ValueError:
        raise argparse.ArgumentTypeError("%r is not a number of seconds" % v)
    if n < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return n


EPILOG = """\
exit: 0 ok or current | 1 failed (rolled back) | 2 usage or refusal
      3 would change (--check) or drifted (check) | 4 not installed
The engine is bound to 127.0.0.1 only: --host accepts nothing else.
"""


def build_parser():
    ap = argparse.ArgumentParser(
        prog="gt_farm_engine.py", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG,
        description="Run a local llama.cpp engine as a launchd or systemd service, not as a\n"
                    "child of a Claude session; prove it answers /health; roll back if it does not.")
    sub = ap.add_subparsers(dest="cmd")

    i = sub.add_parser("install", help="install (or update) the engine service and prove it answers",
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    i.add_argument("name", help="the engine's name: lower-case letters, digits, dashes")
    i.add_argument("--model", metavar="FILE", help="the GGUF model file (required the first time)")
    i.add_argument("--port", type=posint, help="the port on 127.0.0.1 (required the first time)")
    i.add_argument("--alias", help="the model name the engine reports (default: NAME)")
    i.add_argument("--context", type=posint, help="context size, -c (default %d)" % DEFAULT_CONTEXT)
    i.add_argument("--slots", type=posint, help="parallel requests, -np (default %d)" % DEFAULT_SLOTS)
    i.add_argument("--threads", type=posint, help="threads, -t (default: physical cores - 1)")
    i.add_argument("--llama-server", metavar="FILE", help="the llama-server binary (default: PATH, then the usual places)")
    i.add_argument("--host", help="accepts 127.0.0.1 only; anything else is refused")
    i.add_argument("--system", action="store_true",
                   help="stage a boot-time LaunchDaemon / systemd system unit and print the sudo commands")
    i.add_argument("--allow-low-ram", action="store_true",
                   help="install although the model + %d GiB exceeds the RAM budget (prints a warning)" % HEADROOM_GIB)
    i.add_argument("--wait", type=seconds, default=DEFAULT_WAIT, metavar="S",
                   help="seconds to wait for /health (default %g)" % DEFAULT_WAIT)
    i.add_argument("--check", action="store_true", help="change nothing: exit 0 current, 3 would change")
    i.add_argument("--dry-run", action="store_true", help="print the files and commands; change nothing")
    i.add_argument("--json", action="store_true", help="print the result as one JSON object")
    i.set_defaults(fn=cmd_install)

    c = sub.add_parser("check", help="audit an installed engine: files unedited, loaded, /health answers")
    c.add_argument("name")
    c.add_argument("--json", action="store_true", help="print the result as one JSON object")
    c.set_defaults(fn=cmd_check)

    s = sub.add_parser("status", help="report the installed engine(s); all of them without a name")
    s.add_argument("name", nargs="?")
    s.add_argument("--json", action="store_true", help="print the result as one JSON object")
    s.set_defaults(fn=cmd_status)

    r = sub.add_parser("remove", help="unload the service and delete the files the install wrote")
    r.add_argument("name")
    r.add_argument("--keep-log", action="store_true", help="keep engine.log")
    r.add_argument("--wait", type=seconds, default=15.0, metavar="S",
                   help="seconds to wait for the engine to stop answering (default 15)")
    r.add_argument("--check", action="store_true", help="change nothing: exit 0 nothing to remove, 3 would remove")
    r.add_argument("--dry-run", action="store_true", help="print what would be removed; change nothing")
    r.add_argument("--json", action="store_true", help="print the result as one JSON object")
    r.set_defaults(fn=cmd_remove)
    return ap


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        return a.fn(a)
    except Refusal as exc:
        if getattr(a, "json", False):
            print(json.dumps({"ok": False, "refused": str(exc)}))
        else:
            say("refused: %s" % exc)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
