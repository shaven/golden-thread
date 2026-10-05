"""The gateway's two front doors: a unix socket (local and hub-admin) and HTTP (hub mode).

Both speak the same five methods (find, call, status, catalog, ping) and hand them to one
engine per process. The engine is built lazily through `engine_factory` and every engine
call is serialised under a lock: the engine reloads its files on mtime change and is not
written to be re-entrant.

Unix protocol: one JSON object per line, request {"id","method","params"} and response
{"id","result"} or {"id","error"}. The caller is always client "local"; the socket is
chmod 600, so only its owner can reach it.

0.3.0: on native Windows (no AF_UNIX in CPython) the same JSON-lines protocol is served on a
named pipe through gt core's gt_ipc (serve_pipe below): a protected DACL naming only the
user's SID, PIPE_REJECT_REMOTE_CLIENTS, FILE_FLAG_FIRST_PIPE_INSTANCE against squatting, and
every client's SID read from its token and compared with ours. While gt unlock is on, each
local request also carries the caller's KERNEL identity ({pid, start}) to the engine as
`subject`, which the engine asks the unlock authority about.

HTTP protocol: POST /v1/<method> with a JSON params body, Authorization: Bearer <secret>
and X-GT-Client: <client id>. Admin methods are simply not routes (404). A gateway error
from the engine is a 200 carrying {"error": ...}; transport and auth failures use HTTP
status codes with the same {"error": ...} body.
"""
import json
import os
import re
import socket
import socketserver
import ipaddress
import ssl
import stat
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import unlock
from .errors import GatewayError

METHODS = ("find", "call", "status", "catalog", "ping")
MAX_BODY = 1024 * 1024            # request size cap, both transports
VERSION = "0.3.0"
IS_WINDOWS = os.name == "nt"
# find / status / catalog reveal the catalog, not a downstream; under gt unlock they are asked
# about as this read scope (connection ids always contain "@", so it names no connection).
CATALOG_SCOPE = "lotr:catalog:read"

# -- local security (ADR-5) ------------------------------------------------------------------
# The socket is the local front door, and it is NOT open: three checks stand in front of it.
#   1. its directory must be private (owner only), or the daemon refuses to start;
#   2. the socket is created 600 from the first instant;
#   3. every connection's PEER UID is read from the kernel and must equal the daemon's own.
# (3) fails CLOSED: on a platform where the peer cannot be identified, nobody is served.

_LOCAL_PEERCRED = 0x001        # macOS <sys/un.h>; level SOL_LOCAL (0)
_SO_PEERCRED = getattr(socket, "SO_PEERCRED", 17)   # Linux


def peer_uid(sock):
    """The uid of the process on the other end of a unix socket, from the kernel. None if unknown."""
    try:
        if sys.platform == "darwin":
            # struct xucred { u_int cr_version; uid_t cr_uid; short cr_ngroups; gid_t cr_groups[16]; }
            raw = sock.getsockopt(0, _LOCAL_PEERCRED, 76)
            _version, uid = struct.unpack_from("=II", raw)
            return uid
        if sys.platform.startswith("linux"):
            raw = sock.getsockopt(socket.SOL_SOCKET, _SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
            return uid
    except (OSError, struct.error):
        return None
    return None


def check_private_dir(path):
    """Refuse a socket directory that anyone but its owner can enter, list or write."""
    st = os.stat(path)
    if st.st_uid != os.getuid():
        raise GatewayError("insecure_dir", f"{path} is not owned by this user",
                           hints=["the daemon's home must belong to the user running it"])
    if st.st_mode & 0o077:
        raise GatewayError("insecure_dir", f"{path} is mode {oct(st.st_mode & 0o777)}; must be 700",
                           hints=[f"chmod 700 {path}"])


def is_loopback(host):
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

_FIND_KEYS = ("query", "connection", "limit", "detail")
_CALL_KEYS = ("tool", "connection", "op", "args", "select", "cursor")


def _internal(exc):
    """An unexpected exception, reduced to its type name: messages may carry anything."""
    return {"code": "internal", "message": type(exc).__name__, "hints": []}


class _EngineHolder:
    """One engine per process, built on first use, every call made under one lock."""

    def __init__(self, engine_factory):
        self._factory = engine_factory
        self._engine = None
        self._lock = threading.Lock()

    def run(self, fn):
        with self._lock:
            if self._engine is None:
                self._engine = self._factory()      # a failure is not cached; next call retries
            return fn(self._engine)


EngineHolder = _EngineHolder    # lotrd shares one holder between both front doors


def _holder(engine_factory):
    return engine_factory if isinstance(engine_factory, _EngineHolder) else _EngineHolder(engine_factory)


def _pick(params, keys):
    return {k: params[k] for k in keys if k in params and params[k] is not None}


def dispatch(holder, method, params, client_id, subject=None, remote=False):
    """Run one method for `client_id`. Returns the result; raises GatewayError.

    `subject` is the local caller's kernel identity, given only while gt unlock is on (the
    front door looks it up then and only then, so with unlock off every call is exactly
    0.2.0's). With a subject, the catalog methods are asked about as CATALOG_SCOPE -- so under
    door mcp_only a process the session's shell started cannot even list the gateway -- and
    `call` hands the subject to the engine, which asks per connection and tier."""
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise GatewayError("bad_request", "params must be a JSON object")
    if remote and client_id in (None, "", "local"):
        # Review M2: a hub (HTTP) caller is never the local caller, whatever id it carries.
        raise GatewayError("unauthorized", "a hub client is never the local caller")
    if method == "ping":
        return {"pong": True, "version": VERSION}
    if subject is not None and method in ("find", "status", "catalog"):
        unlock.check(CATALOG_SCOPE, subject, request=False, reason=f"lotr {method}")
    if method == "find":
        kw = _pick(params, _FIND_KEYS)
        if "limit" in kw and (not isinstance(kw["limit"], int) or isinstance(kw["limit"], bool)):
            raise GatewayError("bad_request", "limit must be an integer")
        return holder.run(lambda e: e.find(client_id=client_id, **kw))
    if method == "call":
        kw = _pick(params, _CALL_KEYS)
        missing = [k for k in ("tool", "connection", "op") if not isinstance(kw.get(k), str)]
        if missing:
            raise GatewayError("bad_request", "call needs string params: " + ", ".join(missing))
        if "args" in kw and not isinstance(kw["args"], dict):
            raise GatewayError("bad_request", "args must be a JSON object")
        if subject is not None:
            kw["subject"] = subject
        return holder.run(lambda e: e.call(client_id=client_id, **kw))
    if method == "status":
        return holder.run(lambda e: e.status())
    if method == "catalog":
        mc = params.get("max_chars")
        kw = {"max_chars": mc} if isinstance(mc, int) and not isinstance(mc, bool) and mc > 0 else {}
        return {"text": holder.run(lambda e: e.catalog_text(**kw))}
    raise GatewayError("unknown_method", f"no method {method!r}", hints=list(METHODS))


def _answer(holder, method, params, client_id, subject=None, remote=False):
    """dispatch() with every failure folded into an {"error"} dict. Never raises."""
    try:
        return {"result": dispatch(holder, method, params, client_id, subject, remote)}
    except GatewayError as e:
        return {"error": e.to_dict()}
    except Exception as e:                      # noqa: BLE001 - the server must keep serving
        return {"error": _internal(e)}


# ---------------------------------------------------------------- unix socket

def socket_alive(sock_path, timeout=1.0):
    """True when a daemon answers ping on `sock_path`."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(sock_path))
        s.sendall(b'{"id": 0, "method": "ping", "params": {}}\n')
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        return bool(json.loads(buf.decode("utf-8")).get("result", {}).get("pong"))
    except (OSError, ValueError, AttributeError):
        return False
    finally:
        s.close()


class _UnixHandler(socketserver.StreamRequestHandler):
    timeout = 300

    def handle(self):
        holder = self.server.holder
        uid = peer_uid(self.request)
        if uid is None or uid != os.getuid():
            # Fail closed: an unidentified or foreign peer gets one refusal and the door shut.
            self._send({"id": None, "error": {"code": "peer_refused",
                                              "message": "this socket serves only its owner",
                                              "hints": []}})
            return
        while True:
            try:
                line = self.rfile.readline(MAX_BODY + 1)
            except (OSError, socket.timeout):
                return
            if not line:
                return
            if len(line) > MAX_BODY:
                self._send({"id": None, "error": {"code": "too_large",
                                                  "message": "request over 1 MB", "hints": []}})
                return
            if not line.strip():
                continue
            try:
                req = json.loads(line.decode("utf-8"))
                if not isinstance(req, dict):
                    raise ValueError
            except (ValueError, UnicodeDecodeError):
                self._send({"id": None, "error": {"code": "bad_request",
                                                  "message": "not a JSON object", "hints": []}})
                continue
            out = {"id": req.get("id")}
            subject = None
            if unlock.enabled():
                # Looked up per request (unlock may be switched on mid-connection), from the
                # kernel (gt_ipc.unix_peer: LOCAL_PEERTOKEN / SO_PEERCRED + start time).
                # INVARIANT: unlock on and the peer unidentifiable = refused, never "local".
                subject = unlock.kernel_peer(self.request)
                if subject is None:
                    self._send({"id": req.get("id"), "error": {
                        "code": "peer_refused", "message": "gt unlock is on and this caller's "
                        "process could not be identified", "hints": []}})
                    return
            out.update(_answer(holder, req.get("method"), req.get("params"), "local", subject))
            if not self._send(out):
                return

    def _send(self, obj):
        try:
            self.wfile.write((json.dumps(obj) + "\n").encode("utf-8"))
            self.wfile.flush()
            return True
        except OSError:
            return False


if hasattr(socketserver, "UnixStreamServer"):
    class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True
        allow_reuse_address = False
else:                                       # native Windows: the named pipe serves instead
    _UnixServer = None


def _run(server, stop_event, on_ready, addr):
    if on_ready is not None:
        if isinstance(addr, tuple):
            on_ready(*addr)
        else:
            on_ready(addr)
    if stop_event is None:
        server.serve_forever(poll_interval=0.2)
        return
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    t.start()
    try:
        while not stop_event.wait(0.2):
            if not t.is_alive():
                break
    finally:
        server.shutdown()
        t.join(5)


def serve_unix(engine_factory, sock_path, *, stop_event=None, on_ready=None):
    """Serve the unix socket until `stop_event` is set (forever when None).

    A socket file already at `sock_path` is removed only when no daemon answers ping on it;
    a live one raises GatewayError("daemon_running"). The socket is chmod 600 and removed on
    exit if it is still ours.
    """
    sock_path = str(sock_path)
    limit = 108 if sys.platform.startswith("linux") else 104       # sun_path, NUL included
    if len(os.fsencode(sock_path)) >= limit:
        raise GatewayError("socket_path_too_long",
                           f"the daemon socket path is {len(os.fsencode(sock_path))} bytes; this "
                           f"kernel allows {limit - 1} for a unix socket, so lotrd cannot listen there",
                           hints=["use a shorter --home (or a shorter TMPDIR for a test home)",
                                  sock_path])
    if os.path.lexists(sock_path):
        if socket_alive(sock_path):
            raise GatewayError("daemon_running", f"a daemon already answers on {sock_path}",
                               hints=["stop it first, or use a different --home"])
        st = os.lstat(sock_path)
        if not stat.S_ISSOCK(st.st_mode):
            raise GatewayError("socket_path_taken", f"{sock_path} exists and is not a socket")
        os.unlink(sock_path)
    sock_dir = os.path.dirname(os.path.abspath(sock_path))
    os.makedirs(sock_dir, mode=0o700, exist_ok=True)
    check_private_dir(sock_dir)
    old = os.umask(0o177)                      # created 600 from the first instant
    try:
        server = _UnixServer(sock_path, _UnixHandler)
    finally:
        os.umask(old)
    os.chmod(sock_path, 0o600)
    ino = os.stat(sock_path).st_ino
    server.holder = _holder(engine_factory)
    try:
        _run(server, stop_event, on_ready, sock_path)
    finally:
        server.server_close()
        try:
            if os.stat(sock_path).st_ino == ino:
                os.unlink(sock_path)
        except OSError:
            pass


# ---------------------------------------------------------------- named pipe (native Windows)

def _gt_ipc():
    m = unlock.ipc()
    if m is None:
        raise GatewayError("no_front_door",
                           "the Windows front door is gt core's named pipe (gt_ipc.py), which "
                           f"cannot be loaded from {unlock.hooks_dir()}",
                           hints=["install gt first (install.cmd), then gt-lotr"])
    return m


def pipe_address(home):
    """The pipe lotrd serves for `home`: gt_ipc names it from a hash of the user's SID and
    the home, so two homes (or two users) never share one."""
    return _gt_ipc().default_address(os.path.abspath(str(home)), "lotrd")


def _pipe_handler(holder):
    def handle(conn):
        # conn.peer was read from the kernel by gt_ipc (GetNamedPipeClientProcessId and the
        # impersonated token's SID) and already checked to be this user; anyone else got
        # peer_refused before this runs.
        ipc = _gt_ipc()
        while True:
            try:
                req = conn.recv_obj()
            except ValueError:
                conn.send_obj({"id": None, "error": {"code": "bad_request",
                                                     "message": "not a JSON object", "hints": []}})
                continue
            except ipc.IpcError as e:
                conn.send_obj({"id": None, "error": {"code": e.code, "message": e.message,
                                                     "hints": []}})
                return
            if req is None:
                return
            subject = None
            if unlock.enabled():
                p = conn.peer or {}
                if p.get("pid") is None or p.get("start") is None:
                    conn.send_obj({"id": req.get("id"), "error": {
                        "code": "peer_refused", "message": "gt unlock is on and this caller's "
                        "process could not be identified", "hints": []}})
                    return
                subject = {"pid": int(p["pid"]), "start": p["start"]}
            out = {"id": req.get("id")}
            out.update(_answer(holder, req.get("method"), req.get("params"), "local", subject))
            conn.send_obj(out)
    return handle


def serve_pipe(engine_factory, home, *, stop_event=None, on_ready=None):
    """Serve the named pipe for `home` until `stop_event` is set (native Windows).

    A live daemon on the same pipe raises GatewayError("daemon_running"); a name held by
    something that does not answer is GatewayError("pipe_squatted") -- the pipe is created with
    FILE_FLAG_FIRST_PIPE_INSTANCE, so this server never shares a name with a squatter."""
    ipc = _gt_ipc()
    addr = pipe_address(home)
    if ipc.alive_at(addr, timeout=0.5):
        raise GatewayError("daemon_running", f"a daemon already answers on {addr}",
                           hints=["stop it first, or use a different --home"])
    holder = _holder(engine_factory)
    stop = stop_event or threading.Event()
    try:
        ipc.serve(addr, _pipe_handler(holder), stop_event=stop, on_ready=on_ready)
    except ipc.IpcError as e:
        raise GatewayError(e.code, e.message, list(e.hints or []))


# ---------------------------------------------------------------- HTTP (hub)

_ROUTE = re.compile(r"^/v1/(%s)$" % "|".join(METHODS))


def _linger(sock, max_bytes, seconds):
    """Half-close `sock` (FIN) and discard whatever the peer still sends, until it closes, or
    `max_bytes` or `seconds` run out. Nothing read here is parsed."""
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        return
    deadline = time.monotonic() + seconds
    seen = 0
    try:
        while seen < max_bytes:
            left = deadline - time.monotonic()
            if left <= 0:
                return
            sock.settimeout(left)
            chunk = sock.recv(min(65536, max_bytes - seen))
            if not chunk:
                return
            seen += len(chunk)
    except (OSError, ValueError):
        return


class _HttpHandler(BaseHTTPRequestHandler):
    server_version = "gt-lotr/" + VERSION
    sys_version = ""
    timeout = 60
    # Lingering close (2026-10-04). A reply sent before the request body is read -- 401/403
    # before the client is authenticated, 404 for an unknown route, 400 on a bad
    # Content-Length, 413 over the cap -- used to close the socket with the body still
    # unread. http.client sends the headers and the body in two send() calls. A body already
    # here at the close made it a close with unread data, which sends RST, and macOS and Linux
    # then throw away the reply the client had not read; a body sent after the close (the
    # client descheduled between its two sends, as on a loaded Windows host) reached a closed
    # socket, which answers RST, and Windows raised WinError 10053. Either way the client saw a
    # reset instead of the 401, so a wrong secret looked like an unreachable hub. Now a request
    # whose body was not read ends with a half-close and a bounded drain (discarded, never
    # parsed), so the close is clean. Refusal still happens on the headers alone: nothing
    # unauthenticated is read before the reply.
    LINGER_SECONDS = 2.0
    LINGER_BYTES = MAX_BODY + 64 * 1024

    def setup(self):
        super().setup()
        self._body_read = False

    def finish(self):
        try:
            super().finish()
        finally:
            if not self._body_read:
                _linger(self.connection, self.LINGER_BYTES, self.LINGER_SECONDS)

    def log_message(self, fmt, *args):         # quiet: never echo headers or bodies
        pass

    def _reply(self, code, obj):
        body = (json.dumps(obj) + "\n").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _err(self, code, ecode, message):
        self._reply(code, {"error": {"code": ecode, "message": message, "hints": []}})

    def _not_found(self):
        self._err(404, "not_found", "no such route")

    do_GET = do_PUT = do_DELETE = do_PATCH = do_HEAD = _not_found

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        m = _ROUTE.match(path)
        if not m:
            return self._not_found()
        method = m.group(1)

        auth = self.headers.get("Authorization") or ""
        client_id = (self.headers.get("X-GT-Client") or "").strip()
        if not auth.startswith("Bearer ") or not auth[7:].strip() or not client_id:
            return self._err(401, "unauthorized", "Authorization: Bearer and X-GT-Client required")
        try:
            client = self.server.registry_getter().authenticate(client_id, auth[7:].strip())
        except GatewayError as e:
            if e.code == "client_revoked":
                return self._err(403, "client_revoked", f"client {client_id} is revoked")
            return self._err(401, "unauthorized", "bad client id or secret")
        except Exception as e:                  # noqa: BLE001
            return self._reply(500, {"error": _internal(e)})
        cid = (client or {}).get("id", client_id) if isinstance(client, dict) else client_id
        if not cid or str(cid).lower() == "local":
            return self._err(401, "unauthorized", "a hub client is never the local caller")

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._err(400, "bad_request", "bad Content-Length")
        if length < 0:
            return self._err(400, "bad_request", "bad Content-Length")
        if length > MAX_BODY:
            self.close_connection = True
            return self._err(413, "too_large", "request over 1 MB")
        raw = self.rfile.read(length) if length else b""
        self._body_read = True
        try:
            params = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except (ValueError, UnicodeDecodeError):
            return self._err(400, "bad_request", "body is not JSON")
        if not isinstance(params, dict):
            return self._err(400, "bad_request", "params must be a JSON object")
        self._reply(200, _answer(self.server.holder, method, params, cid, remote=True))


class _HttpServer(ThreadingHTTPServer):
    daemon_threads = True


def serve_http(engine_factory, host, port, *, registry_getter, tls_cert=None, tls_key=None,
               stop_event=None, on_ready=None):
    """Serve HTTP for enrolled clients until `stop_event` is set (forever when None).

    `registry_getter()` returns an object with authenticate(client_id, secret); it is called
    on every request, so a revocation takes effect without a restart. TLS when `tls_cert` is
    given. `on_ready(host, port)` reports the bound address (port 0 picks a free one).
    """
    if not tls_cert and not is_loopback(host):
        # A bearer secret over plaintext on a network is an open door with extra steps.
        raise GatewayError("insecure_listen",
                           f"refusing to listen on {host} without TLS; bearer secrets would cross "
                           "the network in plaintext",
                           hints=["set listen.tls_cert and listen.tls_key", "or listen on 127.0.0.1"])
    server = _HttpServer((host, int(port)), _HttpHandler)
    server.holder = _holder(engine_factory)
    server.registry_getter = registry_getter
    if tls_cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(tls_cert), str(tls_key) if tls_key else None)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    try:
        _run(server, stop_event, on_ready, server.server_address[:2])
    finally:
        server.server_close()
