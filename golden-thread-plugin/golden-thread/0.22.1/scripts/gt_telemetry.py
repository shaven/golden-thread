#!/usr/bin/env python3
"""gt_telemetry.py -- opt-in telemetry, LOCAL PREVIEW ONLY (gt 0.22.0, phase 107b). Standard library only.

    gt_telemetry.py status           what is on, where the files are, what the install id is
    gt_telemetry.py preview          prints EXACTLY the payload that would be stored (or sent) next
    gt_telemetry.py record           stores today's payload locally (mode local only; once a day)
    gt_telemetry.py set off|local    change the setting (share is refused: no destination exists)
    gt_telemetry.py reset-id         replace the random install id

Setting `telemetry`: off (the default) | local. `off` writes nothing at all. `local` keeps counters on this
machine and writes one payload a day to local.jsonl so YOU can see what a sending mode would send. NOTHING
IS SENT ANYWHERE: this module has no network code, and `share` is refused with `telemetry_no_destination`
until the owner names a destination and a later release builds the sender.

The payload is a CLOSED schema: {schema, gt_version, platform, modules_on, features_used, gate_outcomes,
error_codes, install_id}. Names are short identifiers, values are whole numbers, platform is one of
darwin | windows | linux, install_id is 32 random hex characters you can reset. A path, a URL, a host, an
address, a prompt, a note, a file name, a connection id or a credential has no field to sit in, and a name
shaped like one is dropped. No account, no address, no clock time finer than the day.
"""
import json
import os
import re
import sys
import time
import uuid

sys.dont_write_bytecode = True

_IDENT = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
_SECRETISH = re.compile(r"(^|[_-])(sk|ghp|gho|ghs|xox[a-z]|akia|eyj|pk|rk)[_-]?[a-z0-9]{8,}", re.I)
_ID = re.compile(r"^[0-9a-f]{32}$")
_VER = re.compile(r"^\d+(\.\d+){1,3}$")
MODES = ("off", "local")
PLATFORMS = ("darwin", "windows", "linux")
_MAX = 10 ** 9


class TelemetryError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def default_dir():
    return os.environ.get("GT_TELEMETRY_DIR") or os.path.join(os.path.expanduser("~"), ".claude", "golden-thread",
                                                              "telemetry")


def _vault_config():
    try:
        with open(os.path.join(os.path.expanduser("~"), ".claude", "vault-config.json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def ok_name(n):
    return isinstance(n, str) and bool(_IDENT.fullmatch(n)) and not _SECRETISH.search(n) \
        and not re.search(r"[a-z0-9]{32,}", n)


def _count(v):
    return isinstance(v, int) and not isinstance(v, bool) and 0 < v < _MAX


def validate(p):
    """Raise TelemetryError unless `p` is exactly the closed schema, every value in its allowed shape."""
    bad = lambda why: TelemetryError("telemetry_payload_invalid", why)      # noqa: E731
    if not isinstance(p, dict) or set(p) != {"schema", "gt_version", "platform", "modules_on", "features_used",
                                              "gate_outcomes", "error_codes", "install_id"}:
        raise bad("the payload has fields outside the schema")
    if p["schema"] != 1 or not (isinstance(p["gt_version"], str) and _VER.fullmatch(p["gt_version"])):
        raise bad("schema or version")
    if p["platform"] not in PLATFORMS or not (isinstance(p["install_id"], str) and _ID.fullmatch(p["install_id"])):
        raise bad("platform or install id")
    if not isinstance(p["modules_on"], list) or not all(ok_name(m) for m in p["modules_on"]):
        raise bad("modules_on holds a name that is not a plain identifier")
    for k in ("features_used", "error_codes"):
        if not isinstance(p[k], dict) or not all(ok_name(n) and _count(v) for n, v in p[k].items()):
            raise bad(k)
    g = p["gate_outcomes"]
    if not isinstance(g, dict) or not all(ok_name(n) and isinstance(v, dict) and set(v) == {"pass", "fail"}
                                          and all(isinstance(x, int) and not isinstance(x, bool) and 0 <= x < _MAX
                                                  for x in v.values()) for n, v in g.items()):
        raise bad("gate_outcomes")
    return p


def _platform():
    return {"darwin": "darwin", "win32": "windows"}.get(sys.platform, "linux")


class Telemetry:
    def __init__(self, dir=None, config=None, version=None, modules=None):
        self.dir = dir or default_dir()
        self._config, self._version, self._modules = config, version, modules
        self.counters_path = os.path.join(self.dir, "counters.json")
        self.local_path = os.path.join(self.dir, "local.jsonl")
        self.id_path = os.path.join(self.dir, "install-id")

    # -- setting
    def mode(self):
        v = (self._config.get("telemetry") if self._config is not None
             else (os.environ.get("GT_TELEMETRY") or _vault_config().get("telemetry")))
        v = str(v).strip().lower() if v not in (None, "") else "off"
        return v if v in MODES else "off"

    def set_mode(self, mode):
        if mode == "share":
            raise TelemetryError("telemetry_no_destination",
                                 "share is not available: no destination has been named and nothing in gt sends "
                                 "telemetry anywhere. `local` shows what sharing would send.")
        if mode not in MODES:
            raise TelemetryError("telemetry_bad_mode", "telemetry is off or local")
        return mode

    # -- private files
    def _ensure(self):
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.dir, 0o700)

    def _write(self, path, text, append=False):
        self._ensure()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC) | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "a" if append else "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def _counters(self):
        try:
            with open(self.counters_path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    # -- counting (off: nothing at all)
    def count(self, kind, name, arg=1):
        """count("features_used", name[, n]) | count("error_codes", code[, n]) | count("gate_outcomes", gate, "pass"|"fail")."""
        try:
            if self.mode() == "off" or not ok_name(name):
                return
            c = self._counters()
            if kind == "gate_outcomes":
                if arg not in ("pass", "fail"):
                    return
                g = c.setdefault("gate_outcomes", {}).setdefault(name, {"pass": 0, "fail": 0})
                g[arg] += 1
            elif kind in ("features_used", "error_codes"):
                if not _count(arg):
                    return
                d = c.setdefault(kind, {})
                d[name] = d.get(name, 0) + arg
            else:
                return
            self._write(self.counters_path, json.dumps(c, sort_keys=True))
        except Exception:                                       # noqa: BLE001 - counting never breaks work
            return

    def install_id(self):
        try:
            with open(self.id_path, encoding="utf-8") as f:
                v = f.read().strip()
            if _ID.fullmatch(v):
                return v
        except OSError:
            pass
        return self.reset_id()

    def reset_id(self):
        v = uuid.uuid4().hex
        self._write(self.id_path, v + "\n")
        return v

    def payload(self):
        """What would be stored or sent next. Built from counters only; every field re-validated."""
        c = self._counters()
        p = {"schema": 1, "gt_version": self._version or "0.22.1", "platform": _platform(),
             "modules_on": sorted(m for m in (self._modules or []) if ok_name(m)),
             "features_used": {n: v for n, v in sorted((c.get("features_used") or {}).items())
                               if ok_name(n) and _count(v)},
             "gate_outcomes": {n: {"pass": int(v.get("pass", 0)), "fail": int(v.get("fail", 0))}
                               for n, v in sorted((c.get("gate_outcomes") or {}).items())
                               if ok_name(n) and isinstance(v, dict)},
             "error_codes": {n: v for n, v in sorted((c.get("error_codes") or {}).items())
                             if ok_name(n) and _count(v)},
             "install_id": self.install_id()}
        return validate(p)

    def record_day(self):
        """local mode: one payload per day into local.jsonl. -> True when a line was added."""
        if self.mode() != "local":
            return False
        day = time.strftime("%Y-%m-%d")
        try:
            with open(self.local_path, encoding="utf-8") as f:
                if any(json.loads(l).get("day") == day for l in f if l.strip()):
                    return False
        except (OSError, ValueError):
            pass
        self._write(self.local_path, json.dumps({"day": day, "payload": self.payload()}, sort_keys=True) + "\n",
                    append=True)
        return True


def _modules_on():
    root = os.path.join(os.path.expanduser("~"), ".claude", "plugins", "cache", "golden-thread-plugin")
    try:
        return sorted(d.replace("gt-", "gt-") for d in os.listdir(root) if _IDENT.fullmatch(d))
    except OSError:
        return []


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    t = Telemetry(modules=_modules_on())
    cmd = argv[0] if argv else "status"
    try:
        if cmd == "preview":
            print("This is exactly what would be stored (mode local) or sent (a later sharing mode). "
                  "Nothing is sent anywhere today.\n")
            print(json.dumps(t.payload(), indent=2, sort_keys=True))
            return 0
        if cmd == "record":
            print("stored today's payload" if t.record_day() else "nothing stored (telemetry is off, or already stored today)")
            return 0
        if cmd == "set" and len(argv) > 1:
            m = t.set_mode(argv[1])
            print("telemetry %s: set it with `gt_settings.py set telemetry %s`" % (m, m))
            return 0
        if cmd == "reset-id":
            print("new install id: " + t.reset_id())
            return 0
        if cmd == "status":
            print("telemetry: %s   (off | local; share does not exist yet)" % t.mode())
            print("Nothing is sent anywhere. `local` keeps counters and one payload a day on this machine:")
            print("  " + t.dir)
            print("See what it would hold: gt_telemetry.py preview")
            return 0
    except TelemetryError as e:
        print("%s: %s" % (e.code, e.message), file=sys.stderr)
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
