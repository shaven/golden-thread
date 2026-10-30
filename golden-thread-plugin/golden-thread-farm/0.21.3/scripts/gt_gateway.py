#!/usr/bin/env python3
"""gt_gateway.py -- a circuit breaker, a hard-timeout setting and an audit log for the services gt
calls on its own (gt 0.22.0, phase 72). Standard library only; no network.

    gt_gateway.py status           every service's circuit, and the last audit lines
    gt_gateway.py reset SERVICE    close one circuit by hand

For callers (the farm's sender today; the farm's LOTR rows next):

    ok, why, retry_in = should_call("farm:node-a")     # before a call
    record_result("farm:node-a", "ok", latency_ms)     # after it

Outcomes: ok | timeout | unreachable | http_5xx | other_error  (these four count toward the breaker)
          client_error | needs_signin                          (reachable: they do not)

A circuit is closed until `gateway_failure_threshold` consecutive failures (default 3), then open for
`gateway_cooldown_seconds` (default 60); after that exactly one probe call is let through (half-open)
and its result closes the circuit or reopens it. A probe that never reports back (a crashed caller)
loses its lease after PROBE_LEASE_S so the circuit cannot wedge. The breaker is ADVISORY: it never
kills anything. `call_timeout(service)` is the hard-timeout setting (`gateway_call_timeout_seconds`,
default 120); the caller applies it and records an overrun as `timeout`.

Settings (the `gateway` group in gt_settings.py, read here from the environment as GT_<NAME> or from
~/.claude/vault-config.json): gateway on|off (default on), gateway_failure_threshold,
gateway_cooldown_seconds, gateway_call_timeout_seconds. `gateway off` makes every call allowed and
writes nothing.

State is one file shared by every session: <dir>/state.json, mode 600 in a mode-700 folder, written
atomically under an exclusive lock. A state file that cannot be read FAILS OPEN (every circuit
closed, one audit line saying so), so a bad file can never block work. <dir>/audit.jsonl holds one
line per recorded result: time, service, outcome, latency, token counts and the circuit before and
after. Never a URL, a key or any request or response text; a service name that is not a plain word
is replaced by a short hash. The log rotates at 10 MB and keeps two older files.
"""
import contextlib
import hashlib
import json
import os
import re
import sys
import time

sys.dont_write_bytecode = True

COUNTED = ("timeout", "unreachable", "http_5xx", "other_error")
NOT_COUNTED = ("client_error", "needs_signin")
OUTCOMES = ("ok",) + COUNTED + NOT_COUNTED
PROBE_LEASE_S = 120
AUDIT_CAP = 10 * 1024 * 1024
_NAME = re.compile(r"^[A-Za-z0-9._@:-]{1,80}$")

DEFAULTS = {"gateway": "on", "gateway_failure_threshold": "3", "gateway_cooldown_seconds": "60",
            "gateway_call_timeout_seconds": "120"}


def default_dir():
    return os.environ.get("GT_GATEWAY_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude", "golden-thread", "gateway")


def _vault_config():
    try:
        with open(os.path.join(os.path.expanduser("~"), ".claude", "vault-config.json"),
                  encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _gt_log():
    """gt's own log, when gt is installed beside the farm (never required; logging must not break a call)."""
    try:
        if "gt_log" in sys.modules:
            return sys.modules["gt_log"]
        import glob
        pat = os.path.join(os.path.expanduser("~"), ".claude", "plugins", "cache", "golden-thread-plugin", "gt",
                           "*", "scripts", "gt_log.py")
        found = sorted(glob.glob(pat), key=lambda p: [int(x) for x in re.findall(r"\d+", p.split(os.sep)[-3])])
        try:
            import gt_log
            return gt_log
        except ImportError:
            pass
        if found:
            sys.path.insert(0, os.path.dirname(found[-1]))
            import gt_log
            return gt_log
    except Exception:                                           # noqa: BLE001
        pass
    return None


def _emit(level, event, service=None, counts=None, code=None):
    g = _gt_log()
    if g is not None:
        try:
            g.log("gateway", level, event, counts, code, g.ref_of(service) if service else None)
        except Exception:                                       # noqa: BLE001
            pass


def _clean(service):
    s = str(service)
    return s if _NAME.match(s) else "svc-" + hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]


@contextlib.contextmanager
def _locked(path):
    """An exclusive advisory lock on <path>.lock (flock on POSIX, msvcrt on Windows)."""
    fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            while True:
                try:
                    os.lseek(fd, 0, 0)
                    # LK_NBLCK, not LK_LOCK: LK_LOCK sleeps a whole second between its own tries, so
                    # every contended call (two pool workers asking should_call at once) lost 1 s
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
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


class Gateway:
    def __init__(self, dir=None, clock=time.time, config=None, audit_cap_bytes=AUDIT_CAP):
        self.dir = dir or default_dir()
        self.clock = clock
        self._config = config                 # tests inject; None = environment, then vault-config
        self.audit_cap = audit_cap_bytes
        self.state_path = os.path.join(self.dir, "state.json")
        self.audit_path = os.path.join(self.dir, "audit.jsonl")

    # -- settings
    def _setting(self, name):
        if self._config is not None:
            v = self._config.get(name)
        else:
            v = os.environ.get("GT_" + name.upper()) or _vault_config().get(name)
        return str(v).strip() if v not in (None, "") else DEFAULTS[name]

    def _num(self, name, lo, hi):
        try:
            n = int(self._setting(name))
        except ValueError:
            n = int(DEFAULTS[name])
        return n if lo <= n <= hi else int(DEFAULTS[name])

    def enabled(self):
        return self._setting("gateway").lower() != "off"

    def threshold(self):
        return self._num("gateway_failure_threshold", 1, 1000000)

    def cooldown(self):
        return self._num("gateway_cooldown_seconds", 1, 86400)

    def call_timeout(self, service=None):
        return self._num("gateway_call_timeout_seconds", 1, 3600)

    # -- state
    def _ensure_dir(self):
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.dir, 0o700)

    def _load(self):
        """-> (services dict, corrupt flag)."""
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("services"), dict):
                return data["services"], False
            return {}, True
        except FileNotFoundError:
            return {}, False
        except (OSError, ValueError):
            return {}, True

    def _save(self, services):
        tmp = self.state_path + ".%d.tmp" % os.getpid()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"schema": 1, "services": services}, f, indent=2, sort_keys=True)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, self.state_path)

    def _audit(self, line):
        try:
            if os.path.exists(self.audit_path) and os.path.getsize(self.audit_path) >= self.audit_cap:
                for i in (2, 1):
                    src = self.audit_path + (".%d" % (i - 1) if i > 1 else "")
                    if os.path.exists(src):
                        os.replace(src, self.audit_path + ".%d" % i)
            fd = os.open(self.audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(fd, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(line, sort_keys=True) + "\n")
        except OSError:
            pass

    @contextlib.contextmanager
    def _txn(self):
        """Lock, load (a corrupt file = empty, with one audit line), yield, save."""
        self._ensure_dir()
        with _locked(self.state_path):
            services, corrupt = self._load()
            if corrupt:
                _emit("error", "state_corrupt")
                self._audit({"ts": self.clock(), "event": "state_corrupt",
                             "detail": "state.json could not be read; every circuit treated as closed"})
            yield services
            self._save(services)

    # -- the API
    def should_call(self, service):
        """-> (allowed, reason, retry_in_s). Allowed with reason "ok", "probe" or "gateway_off"."""
        if not self.enabled():
            return True, "gateway_off", 0
        name = _clean(service)
        now = self.clock()
        with self._txn() as services:
            s = services.get(name)
            if not s or s.get("state", "closed") == "closed":
                return True, "ok", 0
            if s["state"] == "open":
                wait = s["opened_at"] + self.cooldown() - now
                if wait > 0:
                    return False, "circuit_open", max(1, int(wait + 0.999))
                s["state"] = "half-open"
                s["probe_until"] = now + PROBE_LEASE_S
                return True, "probe", 0
            if now < s.get("probe_until", 0):                    # half-open, a probe is out
                return False, "probe_in_flight", max(1, int(s["probe_until"] - now + 0.999))
            s["probe_until"] = now + PROBE_LEASE_S               # its lease ran out: take it over
            return True, "probe", 0

    def record_result(self, service, outcome, latency_ms=None, tokens_in=None, tokens_out=None):
        if not self.enabled():
            return
        if outcome not in OUTCOMES:
            outcome = "other_error"
        name = _clean(service)
        now = self.clock()
        with self._txn() as services:
            s = services.setdefault(name, {"state": "closed", "failures": 0})
            before = s.get("state", "closed")
            if outcome == "ok":
                s.update(state="closed", failures=0)
                s.pop("probe_until", None)
            elif outcome in COUNTED:
                s["failures"] = s.get("failures", 0) + 1
                if before == "half-open" or s["failures"] >= self.threshold():
                    s.update(state="open", opened_at=now)
                    s.pop("probe_until", None)
            else:                                                # reachable: not a failure
                s.pop("probe_until", None)
                if before == "half-open":
                    s.update(state="closed", failures=0)
            s["last"] = now
            if before != "open" and s["state"] == "open":
                _emit("info", "circuit_opened", name, {"failures": s.get("failures", 0)}, outcome)
            elif before in ("open", "half-open") and s["state"] == "closed":
                _emit("info", "circuit_closed", name)
            self._audit({"ts": now, "service": name, "outcome": outcome, "latency_ms": latency_ms,
                         "tokens_in": tokens_in, "tokens_out": tokens_out,
                         "circuit_before": before, "circuit_after": s["state"]})

    def states(self):
        with self._txn() as services:
            return json.loads(json.dumps(services))

    def reset(self, service):
        name = _clean(service)
        with self._txn() as services:
            services.pop(name, None)
        self._audit({"ts": self.clock(), "service": name, "event": "reset"})


# -- module-level helpers for callers that do not want to build a Gateway
_default = None


def _gw():
    global _default
    if _default is None:
        _default = Gateway()
    return _default


def should_call(service):
    try:
        return _gw().should_call(service)
    except Exception:                                    # noqa: BLE001 - the breaker never blocks work
        return True, "gateway_error", 0


def record_result(service, outcome, latency_ms=None, tokens_in=None, tokens_out=None):
    try:
        _gw().record_result(service, outcome, latency_ms, tokens_in, tokens_out)
    except Exception:                                    # noqa: BLE001
        pass


def call_timeout(service=None):
    try:
        return _gw().call_timeout(service)
    except Exception:                                    # noqa: BLE001
        return 120


_STYLE = {"bold": "1", "dim": "2", "green": "32", "yellow": "33", "red": "31", "cyan": "36"}


def _use_color(stream):
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def render_status(gw, color=None, recent=5):
    """The gateway as a short page: each service in words, then the latest activity."""
    on = _use_color(sys.stdout) if color is None else color

    def c(text, style):
        return "\x1b[%sm%s\x1b[0m" % (_STYLE[style], text) if on else text
    lines = ["", c("  gt gateway", "bold") + c("  - the services gt calls on its own", "dim"), ""]
    if not gw.enabled():
        lines += ["  The gateway is switched off, so nothing is paused or recorded.",
                  c("  Turn it on:  gt_settings.py set gateway on", "cyan"), ""]
        return "\n".join(lines)
    st = gw.states()
    if not st:
        lines += ["  Nothing has been called yet. Once gt sends work to an engine it",
                  "  shows up here, and an engine that keeps failing is paused so gt",
                  "  stops waiting on it.", ""]
    for name, s in sorted(st.items()):
        state = s.get("state", "closed")
        if state == "closed":
            dot, words = c("\u25cf", "green"), c("working", "green")
            if s.get("failures"):
                words += c("  (%d recent failure%s)" % (s["failures"], "" if s["failures"] == 1 else "s"), "dim")
        elif state == "open":
            left = max(0, int(s.get("opened_at", 0) + gw.cooldown() - gw.clock()))
            dot, words = c("\u25cf", "red"), c("paused", "red") + c(
                " - skipped, next try in %d s" % left, "dim")
        else:
            dot, words = c("\u25cf", "yellow"), c("testing", "yellow") + c(" - one trial call is out", "dim")
        lines.append("  %s %-22s %s" % (dot, name.replace("farm:", "")[:22], words))
    try:
        with open(gw.audit_path, encoding="utf-8") as f:
            tail = [json.loads(x) for x in f.readlines()[-recent:]]
    except (OSError, ValueError):
        tail = []
    tail = [t for t in tail if t.get("outcome")]
    if tail:
        lines += ["", c("  Latest calls", "bold")]
        for t in tail:
            ms = t.get("latency_ms")
            when = time.strftime("%H:%M:%S", time.localtime(t["ts"])) if t.get("ts") else "--:--:--"
            good = t["outcome"] == "ok"
            lines.append("    %s  %-22s %s%s" % (
                when, str(t["service"]).replace("farm:", "")[:22],
                c("ok", "green") if good else c(t["outcome"], "red"),
                c("  %d ms" % ms, "dim") if isinstance(ms, int) else ""))
    lines += ["", c("  Reset one:  gt_gateway.py reset SERVICE", "cyan"),
              c("  Settings:   gt_settings.py show gateway", "cyan"), ""]
    return "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    gw = Gateway()
    if argv[:1] == ["reset"] and len(argv) == 2:
        gw.reset(argv[1])
        print("  \u2713 %s is back in service." % _clean(argv[1]).replace("farm:", ""))
        return 0
    if argv[:1] in ([], ["status"]):
        print(render_status(gw))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
