#!/usr/bin/env python3
"""gt_log.py -- one structured local log for gt itself (gt 0.22.0, phase 107a). Standard library only.

    gt_log.py tail [-n N]          the last N records (default 20), one line each
    gt_log.py search TEXT          every record whose line contains TEXT
    gt_log.py export --redact      every record re-checked against the schema, as JSON lines on stdout,
                                   safe to hand to a person who is helping you debug
    gt_log.py status               where it is, how big, which level, and every audit and log file gt keeps

A record is a CLOSED schema: {ts, component, level, event, counts, code, ref}. `component`, `event` and
`code` are short identifiers (lowercase letters, digits, _ : -, up to 48 characters); `counts` maps such
names to whole numbers; `ref` is 8 hex characters that let two records about the same thing be matched
without naming it. Anything else -- a path, a URL, a host, an address, a credential, a sentence -- is not
representable: it is replaced by <redacted-field> and never written. The check is for SHAPE, not meaning:
text that happens to look like an identifier (a short lowercase word with digits and dashes) is accepted,
whatever it says, so look at an export before you hand it over.

Setting `log_level` (env GT_LOG_LEVEL, else ~/.claude/vault-config.json): off | error (default) | info |
debug. The file is <logs>/gt.log.jsonl (GT_LOG_DIR overrides the folder), mode 600 in a mode-700 folder,
appended under a lock, rotated at 10 MB keeping two older files. A failed write never raises and never
blocks anything: logging is the last thing allowed to break a session.

The audit and ledger files gt already keeps (unlock, LOTR, gateway, write queue, savings, events) keep
their own formats. They are indexed here (`status`, and the doctor's `logs` row), not replaced.
"""
import contextlib
import hashlib
import json
import math
import os
import re
import sys
import time

sys.dont_write_bytecode = True

REDACTED = "<redacted-field>"
LEVELS = ("off", "error", "info", "debug")
RECORD_LEVELS = ("error", "info", "debug")
_IDENT = re.compile(r"^[a-z][a-z0-9_:-]{0,47}$")
_REF = re.compile(r"^[0-9a-f]{8}$")
_SECRETISH = re.compile(r"(^|[_:-])(sk|ghp|gho|ghs|xox[a-z]|akia|eyj|pk|rk)[_:-]?[a-z0-9]{8,}", re.I)
_MAX_COUNT = 10 ** 12
CAP = 10 * 1024 * 1024
FILE = "gt.log.jsonl"


def default_dir():
    return os.environ.get("GT_LOG_DIR") or os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "logs")


def _vault_config():
    try:
        with open(os.path.join(os.path.expanduser("~"), ".claude", "vault-config.json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def ident(value):
    """The value if it is a plain identifier, else the redaction marker (None stays None)."""
    if value is None:
        return None
    v = str(value)
    if not _IDENT.fullmatch(v) or _SECRETISH.search(v) or re.search(r"[a-z0-9]{32,}", v):
        return REDACTED
    return v


_MAX_TS = 1e12          # year 33658: a hand-edited 1e300 or a 400-digit integer is not a time


def _usable_ts(ts):
    """The time as a float, or 0.0 when it is not a finite number a clock could hold (a bool, text,
    NaN, Infinity, a 400-digit integer that float() cannot take, 1e300)."""
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return 0.0
    try:
        f = float(ts)
    except (OverflowError, ValueError):
        return 0.0
    return f if math.isfinite(f) and abs(f) < _MAX_TS else 0.0


def clean_record(rec):
    """-> a record that satisfies the schema, or None when it cannot (an unknown level, no usable event)."""
    if not isinstance(rec, dict) or rec.get("level") not in RECORD_LEVELS:
        return None
    counts = {}
    raw = rec.get("counts")
    for k, v in (raw.items() if isinstance(raw, dict) else []):
        if ident(k) == str(k) and isinstance(v, int) and not isinstance(v, bool) and abs(v) < _MAX_COUNT:
            counts[str(k)] = v
    ref = rec.get("ref")
    ts = rec.get("ts")
    return {"ts": _usable_ts(ts),                          # 1e999 reads back as Infinity, which is not JSON
            "component": ident(rec.get("component")) or REDACTED, "level": rec["level"],
            "event": ident(rec.get("event")) or REDACTED, "counts": counts, "code": ident(rec.get("code")),
            "ref": ref if isinstance(ref, str) and _REF.fullmatch(ref) else None}


@contextlib.contextmanager
def _locked(path):
    fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            while True:
                try:
                    os.lseek(fd, 0, 0)
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        os.close(fd)


class Log:
    def __init__(self, dir=None, config=None, clock=time.time, cap_bytes=CAP):
        self.dir, self._config, self.clock, self.cap = dir or default_dir(), config, clock, cap_bytes
        self.path = os.path.join(self.dir, FILE)

    def level(self):
        v = (self._config.get("log_level") if self._config is not None else
             (os.environ.get("GT_LOG_LEVEL") or _vault_config().get("log_level")))
        v = str(v).strip().lower() if v not in (None, "") else "error"
        return v if v in LEVELS else "error"

    def write(self, component, level, event, counts=None, code=None, ref=None):
        """-> True when a record was written. Never raises."""
        try:
            if level not in RECORD_LEVELS:
                return False
            if LEVELS.index(level) > LEVELS.index(self.level()):
                return False
            rec = clean_record({"ts": self.clock(), "component": component, "level": level, "event": event,
                                "counts": counts, "code": code, "ref": ref})
            if rec is None:
                return False
            os.makedirs(self.dir, mode=0o700, exist_ok=True)
            if os.name != "nt":
                os.chmod(self.dir, 0o700)
            with _locked(self.path):
                self._rotate()
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
                with os.fdopen(fd, "a", encoding="utf-8", newline="\n") as f:
                    f.write(json.dumps(rec, sort_keys=True, allow_nan=False) + "\n")
            return True
        except Exception:                                       # noqa: BLE001 - logging never breaks work
            return False

    def _rotate(self):
        if os.path.exists(self.path) and os.path.getsize(self.path) >= self.cap:
            for i in (2, 1):
                src = self.path + (".%d" % (i - 1) if i > 1 else "")
                if os.path.exists(src):
                    os.replace(src, self.path + ".%d" % i)

    def records(self):
        out = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        out.append(None)
        except OSError:
            pass
        return out


def ref_of(thing):
    """8 hex characters standing for `thing`: two records about the same thing match, and say nothing about it."""
    return hashlib.sha256(str(thing).encode("utf-8")).hexdigest()[:8]


_default = None


def log(component, level, event, counts=None, code=None, ref=None):
    """Module-level helper for producers. Never raises."""
    global _default
    try:
        if _default is None:
            _default = Log()
        return _default.write(component, level, event, counts, code, ref)
    except Exception:                                           # noqa: BLE001
        return False


# -- every audit and log file gt keeps ---------------------------------------------------------------

def known_files(home=None):
    """[{label, path, exists, bytes, modified}]: where gt writes records, so there is one place to look."""
    home = home or os.path.expanduser("~")
    gt = os.path.join(home, ".claude", "golden-thread")
    lotr = os.path.join(home, ".config", "gt-lotr")
    cand = [("gt log", os.path.join(default_dir() if home == os.path.expanduser("~") else
                                    os.path.join(gt, "logs"), FILE)),
            ("unlock audit", os.path.join(gt, "unlock", "audit.jsonl")),
            ("LOTR audit", os.path.join(lotr, "personal", "state", "audit.jsonl")),
            ("gateway audit", os.path.join(gt, "gateway", "audit.jsonl")),
            ("savings ledger", os.path.join(gt, "usage", "savings.jsonl")),
            ("farm results", os.path.join(gt, "farm", "results")),
            ("write-queue log", "<vault>/Projects/golden-thread/spool/broker/log-YYYY-MM-DD.jsonl"),
            ("project events", "<vault>/Projects/<project>/events.jsonl")]
    out = []
    for label, path in cand:
        st = None
        if not path.startswith("<"):
            try:
                st = os.stat(path)
            except OSError:
                st = None
        out.append({"label": label, "path": path, "exists": st is not None,
                    "bytes": st.st_size if st else 0,
                    "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)) if st else ""})
    return out


# -- command line -------------------------------------------------------------------------------------

def _clock(ts):
    """mm-dd HH:MM:SS for a record's time; a time that cannot be shown (hand-edited file) reads as the epoch."""
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(_usable_ts(ts)))
    except (OverflowError, OSError, ValueError):
        return time.strftime("%m-%d %H:%M:%S", time.localtime(0))


def _line(r):
    return "%s  %-5s %-12s %s%s%s" % (
        _clock(r.get("ts", 0)), r.get("level", "?"),
        r.get("component", "?"), r.get("event", "?"),
        ("  " + " ".join("%s=%s" % kv for kv in sorted((r.get("counts") or {}).items()))) if r.get("counts") else "",
        ("  code=" + r["code"]) if r.get("code") else "")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    lg = Log()
    cmd = argv[0] if argv else "status"
    if cmd == "tail":
        try:
            n = int(argv[argv.index("-n") + 1]) if "-n" in argv else 20
        except (ValueError, IndexError):
            print("tail -n wants a whole number", file=sys.stderr)
            return 2
        for r in [x for x in lg.records() if isinstance(x, dict) and x][-n:]:     # a hand-edited line that is not an object is skipped
            print(_line(r))
        return 0
    if cmd == "search" and len(argv) > 1:
        for r in lg.records():
            if isinstance(r, dict) and r and argv[1] in json.dumps(r):
                print(_line(r))
        return 0
    if cmd == "export":
        if "--redact" not in argv:
            print("export needs --redact: it re-checks every line against the schema first", file=sys.stderr)
            return 2
        dropped = 0
        for r in lg.records():
            c = clean_record(r) if r else None
            if c is None:
                dropped += 1
                continue
            print(json.dumps(c, sort_keys=True, allow_nan=False))
        if dropped:
            print("%d line%s could not be read as a record and was left out" % (dropped, "" if dropped == 1 else "s"),
                  file=sys.stderr)
        return 0
    if cmd == "status":
        recs = [r for r in lg.records() if r]
        print("gt log: %s" % lg.path)
        print("level: %s   (off | error | info | debug; set log_level)" % lg.level())
        print("records: %d   size: %d bytes" % (len(recs), os.path.getsize(lg.path) if os.path.exists(lg.path) else 0))
        print("\nEvery audit and log file gt keeps:")
        for k in known_files():
            print("  %-16s %s%s" % (k["label"], k["path"],
                                    ("   %d bytes, last written %s" % (k["bytes"], k["modified"])) if k["exists"] else ""))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
