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

0.5.0 (gt 0.21.4): POST /mcp, MCP over HTTP, for a plain MCP client (any MCP-over-HTTP
connector). Off unless gateway.json sets listen.mcp_http true; otherwise /mcp is a 404 like any
unknown route, and nothing else changes. One JSON-RPC 2.0 message per POST (initialize,
tools/list, tools/call, ping; a notification gets 202 and no body), answered as application/json;
no SSE stream, no session. The tools are the stdio shim's four (lotrlib/mcp_dispatch.py) and
tools/call goes through the same dispatch() as /v1, as the authenticated client, so policy, tiers
and consent are the engine's, unchanged. Authentication is /v1's: Bearer + X-GT-Client, with the
same 401 / 403. Only when listen.mcp_http_trust_loopback is also true, a request with NEITHER
header, from this machine's loopback address, directly (no proxy headers, no Origin, a loopback
Host), may name an enrolled client as ?cid=<id> instead; a secret is never read from a URL.
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
from urllib.parse import parse_qs

from . import mcp_dispatch, unlock
from .errors import GatewayError

METHODS = ("find", "describe", "call", "status", "catalog", "ping")
MAX_BODY = 1024 * 1024            # request size cap, both transports
VERSION = "0.5.1"
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

_FIND_KEYS = ("query", "connection", "limit", "detail", "role")
_DESCRIBE_KEYS = ("connection", "op", "role")
_CALL_KEYS = ("tool", "connection", "op", "args", "select", "cursor", "token",
              "timeout_s", "max_result_chars")      # 0.5.1: per-call, clamped by the engine


def _internal(exc):
    """An unexpected exception, reduced to its type name: messages may carry anything."""
    return {"code": "internal", "message": type(exc).__name__, "hints": []}


class _EngineHolder:
    """One engine per process, built on first use. The lock covers only the building: 0.5.1, the
    calls run side by side, and the engine keeps its own state consistent (its lock is held for the
    bookkeeping of a call, never for the wait on the downstream)."""

    def __init__(self, engine_factory):
        self._factory = engine_factory
        self._engine = None
        self._lock = threading.Lock()

    def run(self, fn):
        with self._lock:
            if self._engine is None:
                self._engine = self._factory()      # a failure is not cached; next call retries
            engine = self._engine
        return fn(engine)


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
    if subject is not None and method in ("find", "describe", "status", "catalog"):
        unlock.check(CATALOG_SCOPE, subject, request=False, reason=f"lotr {method}")
    if method == "find":
        kw = _pick(params, _FIND_KEYS)
        if "limit" in kw and (not isinstance(kw["limit"], int) or isinstance(kw["limit"], bool)):
            raise GatewayError("bad_request", "limit must be an integer")
        if subject is not None:
            kw["subject"] = subject
        return holder.run(lambda e: e.find(client_id=client_id, **kw))
    if method == "describe":
        kw = _pick(params, _DESCRIBE_KEYS)
        missing = [k for k in ("connection", "op") if not isinstance(kw.get(k), str)]
        if missing:
            raise GatewayError("bad_request", "describe needs string params: " + ", ".join(missing))
        if subject is not None:
            kw["subject"] = subject
        return holder.run(lambda e: e.describe(client_id=client_id, **kw))
    if method == "call":
        kw = _pick(params, _CALL_KEYS)
        missing = [k for k in ("tool", "connection", "op") if not isinstance(kw.get(k), str)]
        if missing:
            raise GatewayError("bad_request", "call needs string params: " + ", ".join(missing))
        if "args" in kw and not isinstance(kw["args"], dict):
            raise GatewayError("bad_request", "args must be a JSON object")
        if "token" in kw and not isinstance(kw["token"], str):
            raise GatewayError("bad_request", "token must be a string (the token's id)")
        if "timeout_s" in kw and (isinstance(kw["timeout_s"], bool)
                                  or not isinstance(kw["timeout_s"], (int, float))):
            raise GatewayError("bad_request", "timeout_s must be a number of seconds")
        if "max_result_chars" in kw and (isinstance(kw["max_result_chars"], bool)
                                         or not isinstance(kw["max_result_chars"], int)):
            raise GatewayError("bad_request", "max_result_chars must be a whole number")
        if subject is not None:
            kw["subject"] = subject
        return holder.run(lambda e: e.call(client_id=client_id, **kw))
    if method == "status":
        # check only when asked: a plain status keeps the 0.3.0 call shape
        if (params or {}).get("check"):
            return holder.run(lambda e: e.status(check=True, **({"remote": True} if remote else {})))
        return holder.run(lambda e: e.status(**({"remote": True} if remote else {})))
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
MCP_PATH = "/mcp"                 # 0.5.0: MCP over HTTP, only while listen.mcp_http is on
_CLIENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")     # registry.CLIENT_ID, without the import
# A request that carries one of these came through someone else's proxy, so its TCP peer says
# nothing about who really sent it: never loopback-trusted.
_PROXY_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto",
                  "X-Real-IP")
_MCP_CATALOG_CHARS = 1800         # the catalog text in `initialize`, as the stdio shim asks for it
_MCP_INSTRUCTIONS_MAX = 2048
_UNAUTHENTICATED = "Authorization: Bearer and X-GT-Client required"


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

    def _reply(self, code, obj, headers=None):
        body = (json.dumps(obj) + "\n").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _reply_empty(self, code):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _err(self, code, ecode, message, headers=None):
        self._reply(code, {"error": {"code": ecode, "message": message, "hints": []}}, headers)

    def _not_found(self):
        self._err(404, "not_found", "no such route")

    def _other_method(self):
        if self.path.split("?", 1)[0] == MCP_PATH and self.server.mcp_http:
            # MCP's streamable HTTP lets a client open a GET stream; this door has none (a single
            # request, a single reply), and the spec's answer for that is 405, which clients read
            # as "no stream" rather than as a failure. DELETE (end a session) likewise: no sessions.
            return self._err(405, "method_not_allowed", "POST a JSON-RPC message to /mcp",
                             {"Allow": "POST"})
        self._not_found()

    do_GET = do_PUT = do_DELETE = do_PATCH = do_HEAD = _other_method

    # ---------------------------------------------------------------- authentication

    def _authenticate_bearer(self):
        """Authorization: Bearer + X-GT-Client against the registry -> the client id, or None
        once the 401 / 403 / 500 reply is sent. The one check /v1 and /mcp share."""
        auth = self.headers.get("Authorization") or ""
        client_id = (self.headers.get("X-GT-Client") or "").strip()
        if not auth.startswith("Bearer ") or not auth[7:].strip() or not client_id:
            self._err(401, "unauthorized", _UNAUTHENTICATED)
            return None
        try:
            client = self.server.registry_getter().authenticate(client_id, auth[7:].strip())
        except GatewayError as e:
            if e.code == "client_revoked":
                self._err(403, "client_revoked", f"client {client_id} is revoked")
            else:
                self._err(401, "unauthorized", "bad client id or secret")
            return None
        except Exception as e:                  # noqa: BLE001
            self._reply(500, {"error": _internal(e)})
            return None
        cid = (client or {}).get("id", client_id) if isinstance(client, dict) else client_id
        if not cid or str(cid).lower() == "local":
            self._err(401, "unauthorized", "a hub client is never the local caller")
            return None
        return cid

    def _direct_loopback(self):
        """True when this request came straight from a program on this machine.

        The peer address must be loopback, no proxy header may say someone else stands behind it,
        there must be no Origin header (a browser sends one on a cross-site POST, so a web page
        could otherwise ask a secretless door for tool calls), and the Host must be a loopback
        name (DNS rebinding keeps the attacker's own name in it). Anything less is not trusted."""
        if not is_loopback(self.client_address[0]):
            return False
        if self.headers.get("Origin") is not None:
            return False
        if any(self.headers.get(h) is not None for h in _PROXY_HEADERS):
            return False
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):                # [::1]:8765
            host = host[1:].split("]", 1)[0]
        elif host.count(":") == 1:              # localhost:8765, 127.0.0.1:8765
            host = host.split(":", 1)[0]
        return bool(host) and is_loopback(host)

    def _authenticate_loopback(self, query):
        """The fallback for an MCP connector that cannot send headers: an enrolled client named
        as ?cid=<id>, accepted with no secret, from this machine only. Returns the client id, or
        None once the reply is sent. Every refusal before the registry is looked at is the same
        401 as "no credentials", so a caller that is not local cannot tell the fallback exists."""
        try:
            cids = parse_qs(query, max_num_fields=10).get("cid") or []
        except ValueError:                      # more fields than any connector sends
            cids = []
        if len(cids) != 1 or not _CLIENT_ID.fullmatch(cids[0]) or not self._direct_loopback():
            self._err(401, "unauthorized", _UNAUTHENTICATED)
            return None
        try:
            client = self.server.registry_getter().client(cids[0])
        except GatewayError:
            self._err(401, "unauthorized", "bad client id or secret")
            return None
        except Exception as e:                  # noqa: BLE001
            self._reply(500, {"error": _internal(e)})
            return None
        cid = client.get("id", cids[0]) if isinstance(client, dict) else cids[0]
        if isinstance(client, dict) and client.get("revoked"):
            self._err(403, "client_revoked", f"client {cid} is revoked")
            return None
        if not cid or str(cid).lower() == "local":
            self._err(401, "unauthorized", "a hub client is never the local caller")
            return None
        return cid

    # ---------------------------------------------------------------- request body

    def _read_body(self):
        """The request body (bytes, empty when there is none), or None once an error reply is
        sent. Nothing is read before the caller is authenticated."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._err(400, "bad_request", "bad Content-Length")
            return None
        if length < 0:
            self._err(400, "bad_request", "bad Content-Length")
            return None
        if length > MAX_BODY:
            self.close_connection = True
            self._err(413, "too_large", "request over 1 MB")
            return None
        raw = self.rfile.read(length) if length else b""
        self._body_read = True
        return raw

    # ---------------------------------------------------------------- POST

    def do_POST(self):
        path, _, query = self.path.partition("?")
        if path == MCP_PATH:
            return self._post_mcp(query)
        m = _ROUTE.match(path)
        if not m:
            return self._not_found()
        method = m.group(1)

        cid = self._authenticate_bearer()
        if cid is None:
            return
        raw = self._read_body()
        if raw is None:
            return
        try:
            params = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except (ValueError, UnicodeDecodeError):
            return self._err(400, "bad_request", "body is not JSON")
        if not isinstance(params, dict):
            return self._err(400, "bad_request", "params must be a JSON object")
        self._reply(200, _answer(self.server.holder, method, params, cid, remote=True))

    # ---------------------------------------------------------------- POST /mcp (0.5.0)

    def _post_mcp(self, query):
        """One MCP JSON-RPC message in, one back (application/json), or 202 and no body for a
        notification. Everything the MCP surface decides lives in mcp_dispatch; this method only
        authenticates, reads and hands the engine-bound parts to dispatch() as the client."""
        srv = self.server
        if not srv.mcp_http:
            return self._not_found()            # off: a route that does not exist
        has_creds = (self.headers.get("Authorization") is not None
                     or self.headers.get("X-GT-Client") is not None)
        if has_creds or not srv.mcp_http_trust_loopback:
            cid = self._authenticate_bearer()   # /v1's check, same codes, same bodies
        else:
            cid = self._authenticate_loopback(query)
        if cid is None:
            return
        raw = self._read_body()
        if raw is None:
            return
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):    # [[[[... under the 1 MB cap
            return self._reply(400, mcp_dispatch.rpc_error(None, -32700, "Parse error"))
        if isinstance(msg, list):
            return self._reply(400, mcp_dispatch.rpc_error(
                None, -32600, "Invalid Request: send one JSON-RPC message per POST"))

        # HTTP carries no session, so the protocol is read from each request. A client sends the
        # version it negotiated as MCP-Protocol-Version; a missing or unknown one is the default,
        # as the stdio shim treats a client that never initialized.
        asked = (self.headers.get("MCP-Protocol-Version") or "").strip()
        protocol = asked if asked in mcp_dispatch.PROTOCOLS else mcp_dispatch.DEFAULT_PROTOCOL
        holder = srv.holder

        def initialize(params):
            version = params.get("protocolVersion") if isinstance(params, dict) else None
            instructions = mcp_dispatch.FALLBACK_INSTRUCTIONS
            try:
                res = dispatch(holder, "catalog", {"max_chars": _MCP_CATALOG_CHARS}, cid,
                               remote=True)
                text = res.get("text") if isinstance(res, dict) else res
                if isinstance(text, str) and text.strip():
                    instructions = text[:_MCP_INSTRUCTIONS_MAX]
            except Exception:                   # noqa: BLE001 - engine down: the static line
                pass
            return mcp_dispatch.make_initialize_response(version, instructions)

        def tools_call(name, arguments):
            try:
                method, params = mcp_dispatch.request_for(name, arguments)
                result = dispatch(holder, method, params, cid, remote=True)
            except Exception as e:              # noqa: BLE001 - a GatewayError reads as itself,
                result = mcp_dispatch.error_result(e)   # anything else as its type name only
            return mcp_dispatch.envelope(result)

        response = mcp_dispatch.handle_message(msg, protocol, initialize, tools_call)
        if response is None:
            return self._reply_empty(202)       # a notification, or a response to us
        bad = (response.get("error") or {}).get("code") == -32600
        self._reply(400 if bad else 200, response)


class _HttpServer(ThreadingHTTPServer):
    daemon_threads = True
    mcp_http = False                    # 0.5.0: set by serve_http; off unless asked for
    mcp_http_trust_loopback = False


def serve_http(engine_factory, host, port, *, registry_getter, tls_cert=None, tls_key=None,
               stop_event=None, on_ready=None, mcp_http=False, mcp_http_trust_loopback=False):
    """Serve HTTP for enrolled clients until `stop_event` is set (forever when None).

    `registry_getter()` returns an object with authenticate(client_id, secret) (and client(id),
    for the loopback fallback); it is called on every request, so a revocation takes effect
    without a restart. TLS when `tls_cert` is given. `on_ready(host, port)` reports the bound
    address (port 0 picks a free one).

    `mcp_http` (0.5.0) turns on POST /mcp; `mcp_http_trust_loopback` additionally lets a request
    from this machine name an enrolled client as ?cid= with no secret. Both are keyword-only and
    off by default, and only the real boolean True turns either on: a truthy string such as
    "false" does not (lotrd refuses a non-boolean before it gets here; this is the second lock).
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
    server.mcp_http = mcp_http is True
    server.mcp_http_trust_loopback = server.mcp_http and mcp_http_trust_loopback is True
    if tls_cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(tls_cert), str(tls_key) if tls_key else None)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    try:
        _run(server, stop_event, on_ready, server.server_address[:2])
    finally:
        server.server_close()
