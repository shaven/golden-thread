"""A downstream MCP server that speaks stdio (0.5.0, owner 2026-10-13: "stdio is actually what it
is called").

An `mcp` connection with `transport: "stdio"` is a local command LOTR launches itself and talks to
over the child's stdin/stdout: MCP's stdio transport, one JSON-RPC message per line (newline
delimited, no embedded newlines). Nothing is listened on, nothing crosses the network.

Connection fields (registry.py validates them; the command comes from the owner's registry only,
never from a caller):

    command   argv list, e.g. ["python3", "-I", "/abs/server.py"]. NO shell on any platform.
    env       {NAME: secret REF}. Refs only (store:, keychain:, file:, ...), resolved when the
              process starts and never written back, logged or returned. The child gets a small
              base environment (PATH, HOME, temp dirs, locale ...), NOT this process's whole one.
    cwd       optional absolute directory the command starts in.
    timeouts  optional {start_s, call_s, idle_s}: spawn + initialize (30), one request (120),
              how long an unused process is kept (600). Bounded 1..3600.

Lifecycle:
    start     lazily, on the first find-refresh / call; `initialize` + `notifications/initialized`.
    reuse     one process per connection id, kept between calls, restarted when the registry's
              command/env/cwd changes, when it died, or when it sat idle past idle_s.
    shutdown  at interpreter exit (atexit) and by shutdown_all(): stdin closed, 2s grace,
              terminate, kill. A timed-out request kills the process (its state is unknown); the
              next call starts a fresh one.
    stderr    drained into a bounded in-memory tail (8 KiB), values of the env refs masked. It is
              never echoed to the terminal and never put into an error message.
    retry     a process that died mid-request is restarted and the request retried ONCE only for
              tools/list and read-tier tools; a write or consent call is never replayed.

Server-to-client requests (sampling, roots, ...) are answered "method not found": LOTR advertises
no client capabilities.
"""
import atexit
import collections
import hashlib
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

from . import procgroup, upstream
from .errors import GatewayError

PROTOCOL = "2025-03-26"
DEFAULT_TIMEOUTS = {"start_s": 30, "call_s": 120, "idle_s": 600}
MAX_LINE_BYTES = 8 * 1024 * 1024
STDERR_TAIL_BYTES = 8192
_BASE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP",
             "USERPROFILE", "SYSTEMROOT", "SYSTEMDRIVE", "PATHEXT", "APPDATA", "LOCALAPPDATA",
             "PROGRAMDATA", "SHELL")
_EOF = object()


# Phase 112: a stdio command runs a program on this machine, so the first run of a connection's
# command needs a person's yes, kept as a hash of (connection id, argv). A changed command, or the
# same command under another connection, asks again. The store holds hashes only.
APPROVALS_FILE = "approved_commands.json"


def command_hash(cid, command):
    return hashlib.sha256(json.dumps([cid, list(command)], separators=(",", ":")).encode()).hexdigest()


def _approvals(home):
    try:
        d = json.loads((home / APPROVALS_FILE).read_text(encoding="utf-8"))
        return [h for h in d.get("approved", []) if isinstance(h, str)] if isinstance(d, dict) else []
    except (OSError, ValueError, UnicodeDecodeError):
        return []


def is_approved(home, cid, command):
    return command_hash(cid, command) in _approvals(home)


# Owner ruling 2026-10-25: a stdio connection that was already in the registry when this release
# first started is trusted and recorded as approved, once. The marker keeps that from running again
# (deleting the approvals file does not make everything trusted a second time); from then on a
# command nobody approved is refused.
MIGRATED_FILE = "approvals_migrated.json"


def migration_state(home):
    """{"at", "count"} once the one-time migration has run on this home, else None."""
    try:
        d = json.loads((Path(home) / MIGRATED_FILE).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def migrate_approvals(home, connections):
    """Run the one-time migration if it has not run. -> the number of connections recorded as
    approved when THIS call did it (0 or more), None when it had already run or could not be
    recorded (it is tried again at the next start). With an approvals file already there (an
    install that approved commands by hand) nothing is trusted: only the marker is written."""
    from .util import now_iso
    home = Path(home)
    marker = home / MIGRATED_FILE
    if marker.exists():
        return None
    first = not (home / APPROVALS_FILE).exists()
    todo = []
    if first:
        for c in (connections or {}).values():
            cmd = c.get("command")
            if c.get("kind") == "mcp" and c.get("transport") == "stdio" and isinstance(cmd, list):
                todo.append((c["id"], cmd))
    try:
        for cid, cmd in todo:
            approve(home, cid, cmd)
        fd = os.open(str(marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # one winner
    except FileExistsError:
        return None
    except OSError:
        return None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"schema": 1, "at": now_iso(), "count": len(todo)}, f)
    return len(todo)


def approve(home, cid, command):
    from .util import write_json
    h = command_hash(cid, command)
    have = _approvals(home)
    if h not in have:
        write_json(home / APPROVALS_FILE, {"schema": 1, "approved": have + [h]})
    return h


def require_approved(home, conn):
    """Raise command_not_approved unless this connection's exact command was approved."""
    if not is_approved(home, conn["id"], conn.get("command") or []):
        raise GatewayError("command_not_approved",
                           f"{conn['id']}: the command {' '.join(conn.get('command') or [])!r} has not "
                           "been approved on this machine, so it is not started",
                           hints=[f"you approve it by running, in your own terminal: lotr setup "
                                  "TEMPLATE (or lotr add-stdio ...) and answering yes"])


def timeouts_of(conn):
    t = dict(DEFAULT_TIMEOUTS)
    t.update(conn.get("timeouts") or {})
    return t


def _fingerprint(conn):
    return json.dumps([conn.get("command"), conn.get("env"), conn.get("env_plain"), conn.get("cwd")],
                      sort_keys=True)


class _Died(Exception):
    pass


class _Proc:
    def __init__(self, conn, env_values, resolved_command):
        self.cid = conn["id"]
        self.fingerprint = _fingerprint(conn)
        self.t = timeouts_of(conn)
        self._mask = [v for v in env_values.values() if len(v) >= 4]
        env = {k: os.environ[k] for k in _BASE_ENV if k in os.environ}
        env.update({k: v for k, v in (conn.get("env_plain") or {}).items() if isinstance(v, str)})
        env.update(env_values)
        try:
            self.p = subprocess.Popen(
                resolved_command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=conn.get("cwd") or None, env=env, shell=False,
                bufsize=0, close_fds=True, **procgroup.popen_kwargs())
        except (OSError, ValueError) as e:
            raise GatewayError("unreachable", f"{self.cid}: cannot start the command "
                                              f"({type(e).__name__})",
                               hints=["check `command` and `cwd` in the connection"])
        self._tree = procgroup.attach(self.p)
        self.started = time.time()
        self.last_used = self.started
        self.io = threading.Lock()        # one request at a time on this server (not on the others)
        self._inbox = queue.Queue()
        self._err = collections.deque()
        self._err_len = 0
        self.stderr_total = 0
        self._next_id = 0
        threading.Thread(target=self._pump_out, daemon=True).start()
        threading.Thread(target=self._pump_err, daemon=True).start()

    # -- reader threads ----------------------------------------------------------------------

    def _pump_out(self):
        try:
            while True:
                line = self.p.stdout.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_LINE_BYTES:
                    self.kill()
                    break
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                except ValueError:
                    continue                     # a stray log line on stdout is not a message
                if isinstance(msg, dict):
                    self._inbox.put(msg)
        except (OSError, ValueError):
            pass
        self._inbox.put(_EOF)

    def _pump_err(self):
        try:
            while True:
                chunk = self.p.stderr.read(1024)
                if not chunk:
                    break
                self.stderr_total += len(chunk)
                self._err.append(chunk)
                self._err_len += len(chunk)
                while self._err_len > STDERR_TAIL_BYTES and len(self._err) > 1:
                    self._err_len -= len(self._err.popleft())
        except (OSError, ValueError):
            pass

    def stderr_tail(self, limit=300):
        text = b"".join(self._err).decode("utf-8", errors="replace")
        for m in self._mask:
            text = text.replace(m, "***")
        return text[-limit:]

    # -- protocol ----------------------------------------------------------------------------

    def alive(self):
        return self.p.poll() is None

    def _send(self, msg):
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            self.p.stdin.write(data)
            self.p.stdin.flush()
        except (OSError, ValueError):
            raise _Died()

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def rpc(self, method, params, timeout):
        self._next_id += 1
        want = self._next_id
        msg = {"jsonrpc": "2.0", "id": want, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                self.kill()
                raise GatewayError("downstream_error", f"{self.cid}: {method} timed out after "
                                                       f"{timeout}s; the process was stopped")
            try:
                m = self._inbox.get(timeout=left)
            except queue.Empty:
                continue
            if m is _EOF:
                self._inbox.put(_EOF)
                raise _Died()
            if "method" in m and "id" in m:      # a request from the server: we offer nothing
                try:
                    self._send({"jsonrpc": "2.0", "id": m["id"],
                                "error": {"code": -32601, "message": "method not found"}})
                except _Died:
                    raise
                continue
            if m.get("id") == want and "method" not in m:
                if m.get("error"):
                    err = m["error"] if isinstance(m["error"], dict) else {}
                    raise GatewayError("downstream_error", f"{self.cid}: {method} failed: "
                                       f"{str(err.get('message') or m['error'])[:300]}")
                res = m.get("result")
                return res if isinstance(res, dict) else {}

    def handshake(self):
        try:
            self.rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                    "clientInfo": {"name": "gt-lotr", "version": "0.5.1"}},
                     self.t["start_s"])
            self.notify("notifications/initialized")
        except _Died:
            self.kill()
            raise GatewayError("unreachable", f"{self.cid}: the process exited while starting"
                                              f" (code {self.p.poll()})",
                               hints=["run the command by hand to see why; its stderr is not shown"])

    def kill(self):
        try:
            if self.p.poll() is None:
                self.p.kill()
            procgroup.kill(self._tree)            # and anything the server started
        except OSError:
            pass

    def close(self):
        try:
            self.p.stdin.close()
        except (OSError, ValueError):
            pass
        for step in (None, "terminate", "kill"):
            if step:
                try:
                    getattr(self.p, step)()
                except OSError:
                    pass
            try:
                self.p.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                continue
        procgroup.kill(self._tree)                # a grandchild must not outlive its server
        procgroup.release(self._tree)
        self._tree = None
        for s in (self.p.stdout, self.p.stderr):
            try:
                s.close()
            except (OSError, ValueError):
                pass


# -- the process table: one process per connection id, shared by every connection object -------

_LOCK = threading.RLock()
_PROCS = {}


def _resolve_command(conn, env_values):
    argv = list(conn["command"])
    first = argv[0]
    if not os.path.isabs(first):
        path = env_values.get("PATH") or os.environ.get("PATH") or os.defpath
        from . import runtimes
        found = runtimes.which(first, path=path)       # past the Windows Store python stub
        if not found:
            r = runtimes.check(conn.get("runtime"), argv, path=path)
            raise GatewayError("unreachable",
                               f"{conn['id']}: command {first!r} was not found",
                               hints=[r.get("fix") or "use an absolute path in `command`"])
        argv[0] = found
    return argv


_SPAWN = {}          # connection id -> lock held while that connection's server starts or is replaced


def _spawn_lock(cid):
    with _LOCK:
        return _SPAWN.setdefault(cid, threading.Lock())


def reap_idle(now=None):
    now = now if now is not None else time.time()
    with _LOCK:
        for cid, pr in list(_PROCS.items()):
            if not pr.alive() or now - pr.last_used > pr.t["idle_s"]:
                # 0.5.1: a request in flight keeps its server: a call longer than idle_s is not idle
                if pr.alive() and not pr.io.acquire(blocking=False):
                    continue
                _PROCS.pop(cid, None)
                pr.close()


def shutdown_all():
    with _LOCK:
        procs = list(_PROCS.values())
        _PROCS.clear()
    for pr in procs:
        pr.close()


def retain(connections):
    """Stop the stdio server of every connection no longer in `connections` (a registry reload)."""
    with _LOCK:
        gone = [cid for cid in _PROCS if cid not in connections]
        procs = [_PROCS.pop(cid) for cid in gone]
    for pr in procs:
        pr.close()


def forget(cid):
    with _LOCK:
        pr = _PROCS.pop(cid, None)
    if pr:
        pr.close()


def process_info(cid):
    with _LOCK:
        pr = _PROCS.get(cid)
        if pr is None:
            return {"running": False}
        running = pr.alive()
        info = {"running": running, "pid": pr.p.pid, "started": int(pr.started),
                "stderr_bytes": pr.stderr_total}
        if not running:
            info["exit_code"] = pr.p.poll()
            info["stderr_tail"] = pr.stderr_tail()
        return info


atexit.register(shutdown_all)


class McpStdioConnection:
    """Same interface as McpConnection / HttpConnection: discover() and call(op, args)."""

    def __init__(self, conn, profile, *, secret_resolver=None, timeout=None, home=None, **_ignored):
        # `home` (a Path) switches the approval check on: the engine passes it, the one-shot CLI
        # discovery in `lotr add-stdio` has just asked for the yes itself.
        self.home = home
        if secret_resolver is None:
            from .secrets import resolve as secret_resolver
        self.conn = conn
        self.profile = profile or {}
        self._resolve = secret_resolver
        self.cid = conn["id"]

    def _env_values(self):
        out = {}
        for name, ref in (self.conn.get("env") or {}).items():
            value = self._resolve(ref)
            if not isinstance(value, str) or not value or "\x00" in value:
                raise GatewayError("secret_unreadable",
                                   f"{self.cid}: env {name} (ref {ref}) did not resolve")
            out[name] = value
        return out

    def _proc(self):
        reap_idle()
        # 0.5.1: the table lock is held only to read or write the table. A server that starts (up
        # to start_s) holds its own connection's lock, so it blocks no other connection.
        with _spawn_lock(self.cid):
            with _LOCK:
                pr = _PROCS.get(self.cid)
            if pr is not None and (not pr.alive() or pr.fingerprint != _fingerprint(self.conn)):
                if pr.alive():
                    pr.io.acquire()             # a request on the old command finishes first
                with _LOCK:
                    if _PROCS.get(self.cid) is pr:
                        _PROCS.pop(self.cid, None)
                pr.close()
                pr = None
            if pr is None:
                if self.home is not None:
                    require_approved(self.home, self.conn)
                env_values = self._env_values()
                pr = _Proc(self.conn, env_values, _resolve_command(self.conn, env_values))
                try:
                    pr.handshake()
                except BaseException:
                    pr.close()
                    raise
                with _LOCK:
                    _PROCS[self.cid] = pr
            pr.last_used = time.time()
            return pr

    def _run(self, fn, retry):
        for attempt in (1, 2):
            pr = self._proc()
            with pr.io:                         # this server's one request; others run beside it
                try:
                    return fn(pr)
                except _Died:
                    code = pr.p.poll()
                    self._drop(pr)
                    if retry and attempt == 1:
                        continue
                    raise GatewayError("unreachable", f"{self.cid}: the process exited"
                                                      f" (code {code}) before it answered",
                                       hints=["run `lotr status --check` to restart it"])
                except GatewayError:
                    if not pr.alive():
                        self._drop(pr)
                    raise
                finally:
                    pr.last_used = time.time()

    def _drop(self, pr):
        with _LOCK:
            if _PROCS.get(self.cid) is pr:
                _PROCS.pop(self.cid, None)
        pr.close()

    def _call_s(self, pr, timeout_s):
        """Seconds for one request: the connection's own `timeouts.call_s`, or a per-call value
        held to [1, 3600] as on an http connection (not a number: the connection's own)."""
        if timeout_s is None:
            return pr.t["call_s"]
        try:
            return upstream.clamp_timeout(timeout_s, upstream.HARD_CALL_TIMEOUT_S)
        except ValueError:
            return pr.t["call_s"]

    def discover(self):
        def run(pr):
            tools, cursor = [], None
            for _ in range(50):
                result = pr.rpc("tools/list", {"cursor": cursor} if cursor else {},
                                pr.t["call_s"])
                tools += [t for t in result.get("tools") or [] if isinstance(t, dict)]
                cursor = result.get("nextCursor")
                if not cursor:
                    break
            return tools
        return self._run(run, retry=True)

    def call(self, op, args, *, cursor=None, timeout_s=None):
        name = op.get("name")
        if not name or op.get("method") != "MCP":
            raise GatewayError("bad_op", f"{self.cid} is an MCP connection: call one of "
                                         "its tools by name (see find)")

        def run(pr):
            result = pr.rpc("tools/call", {"name": name, "arguments": dict(args or {})},
                            self._call_s(pr, timeout_s))
            texts = [c.get("text", "") for c in result.get("content") or []
                     if isinstance(c, dict) and c.get("type") == "text"]
            if result.get("isError"):
                raise GatewayError("downstream_error",
                                   f"{self.cid}: {name} failed: {' '.join(texts)[:300]}")
            data = result.get("structuredContent")
            if data is None:
                joined = "\n".join(texts)
                try:
                    data = json.loads(joined) if len(texts) == 1 else joined
                except ValueError:
                    data = joined
            return {"status": 200, "data": data, "next_cursor": None, "notes": []}
        return self._run(run, retry=op.get("tier") == "read")
