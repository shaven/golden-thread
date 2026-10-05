"""A downstream MCP endpoint behind SSO/OAuth, reached the way its own client reaches it (0.2.0).

Request 2026-10-02-lotr-front-oauth-mcp-endpoints. Many enterprise systems answer only to an
SSO/OAuth token, and LOTR had no OAuth machinery. It does not grow one: the MCP client already
signed in and keeps its token in an owner-only file, refreshed by its own helper. An `mcp`
connection reuses both:

  * the token BY REFERENCE (`file:<path>#access_token`, resolved by secrets.py), sent only as
    the Authorization header -- never logged, returned, or put in an error;
  * on HTTP 401, `refresh_cmd` (an argv list, no shell) is run with stdout and stderr thrown away
    -- a helper that prints the new token cannot leak it -- then the ref is re-read and the
    request retried ONCE. A refresh that does not help is `auth_failed`, not a loop.

Transport: MCP streamable HTTP. Each JSON-RPC message is a POST; the reply is JSON or an SSE
stream (`data:` lines), and `Mcp-Session-Id` from `initialize` is carried on later requests.
Redirects are refused, so a token is never re-sent to another host.

0.3.0 (gt 0.20.1): `auth.scheme == "oauth"` -- LOTR's own OAuth (lotrlib.oauth). The access
token is minted from the sealed/stored refresh token on demand, lives only in this process's
memory, and is renewed on expiry or a 401 (once). A 401 that survives a fresh refresh, or a
refresh the authorization server refuses, is `needs_login` naming the exact command.
"""
import json
import subprocess
import urllib.error
import urllib.request

from . import oauth
from .errors import GatewayError

PROTOCOL = "2025-03-26"
REFRESH_TIMEOUT_S = 60
USER_AGENT = "gt-lotr/0.3.0"
MAX_REPLY_BYTES = 8 * 1024 * 1024       # an oauth connection's reply is read to this, no further
RUNTIME_TIMEOUT_S = 120                  # and for at most this long, whole exchange


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GatewayError("redirect_refused", "the MCP endpoint redirected; a bearer token is "
                                               "never re-sent to another URL")


class _Unauthorized(Exception):
    pass


class McpConnection:
    def __init__(self, conn, profile, *, secret_resolver=None, timeout=20, oauth_writer=None,
                 oauth_seat="-"):
        if secret_resolver is None:
            from .secrets import resolve as secret_resolver
        self.conn = conn
        self.profile = profile or {}
        self.timeout = timeout
        self._resolve = secret_resolver
        self.endpoint = conn["endpoint"]
        self._auth = conn.get("auth") or {}
        self._ref = self._auth.get("token_ref")
        # No proxy: a bearer token never rides an environment or system proxy.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
        self._next_id = 0
        self._oauth = self._auth.get("scheme") == "oauth"
        self._octx = oauth.Context(self._resolve, oauth_writer, oauth_seat) if self._oauth else None

    # -- plumbing ----------------------------------------------------------------------------

    def _headers(self, token, session):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             "User-Agent": USER_AGENT, "MCP-Protocol-Version": PROTOCOL}
        if token:
            h["Authorization"] = "Bearer " + token
        if session:
            h["Mcp-Session-Id"] = session
        return h

    def _exchange(self, message, token, session):
        """One POST -> (status, WWW-Authenticate values, session id, content type, body text).
        An oauth connection goes through oauth.fetch: the address that is connected to is checked
        against the owner's network policy (no private / link-local / metadata address unless the
        owner allowed it), no proxy is consulted, redirects are refused, size and time are
        bounded. Other mcp connections keep the urllib path, minus any environment proxy."""
        data = json.dumps(message).encode("utf-8")
        hdrs = self._headers(token, session)
        if self._oauth:
            r = oauth.fetch(self.endpoint, oauth.NetPolicy.from_block(self._auth["oauth"]),
                            method="POST", body=data, headers=hdrs, max_bytes=MAX_REPLY_BYTES,
                            timeout=RUNTIME_TIMEOUT_S, what="MCP endpoint")
            return (r.status, r.header("WWW-Authenticate"), (r.header("Mcp-Session-Id") or [None])[0],
                    ((r.header("Content-Type") or [""])[0]).lower(),
                    r.body.decode("utf-8", errors="replace"))
        req = urllib.request.Request(self.endpoint, data=data, headers=hdrs, method="POST")
        try:
            resp = self._opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get_all("WWW-Authenticate") or [], None, "", ""
        except urllib.error.URLError as e:
            raise GatewayError("unreachable", f"{self.conn['id']}: cannot reach the MCP "
                                              f"endpoint ({getattr(e, 'reason', e)})")
        with resp:
            return (resp.status, [], resp.headers.get("Mcp-Session-Id"),
                    (resp.headers.get("Content-Type") or "").lower(),
                    resp.read().decode("utf-8", errors="replace"))

    def _post(self, message, token, session):
        """-> (reply message or None, session id from the reply's headers)."""
        status, www, sid, ctype, body = self._exchange(message, token, session)
        if status == 401:
            raise _Unauthorized()
        if status == 403 and self._oauth:
            ch = oauth.parse_www_authenticate(www)
            if ch.get("error") == "insufficient_scope":
                need = oauth.hint_scope(ch.get("scope"))
                raise GatewayError(
                    "insufficient_scope", f"{self.conn['id']}: the MCP server needs more "
                    "permission than this sign-in was granted",
                    hints=["run: " + oauth.login_command(self.conn["id"], self.conn.get("zone"))
                           + (f" --scope {need}" if need else "")])
        if status >= 400 or status < 200:
            raise GatewayError("downstream_error", f"{self.conn['id']}: the MCP endpoint "
                                                   f"answered HTTP {status}")
        if "id" not in message or not body.strip():
            return None, sid
        want = message["id"]
        candidates = []
        if "text/event-stream" in ctype:
            for block in body.split("\n\n"):
                data = "\n".join(l[5:].lstrip() for l in block.splitlines()
                                 if l.startswith("data:"))
                if data:
                    candidates.append(data)
        else:
            candidates.append(body)
        for raw in candidates:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == want:
                return msg, sid
        raise GatewayError("downstream_error", f"{self.conn['id']}: no reply to {message.get('method')}")

    def _rpc(self, method, params, token, session):
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            msg["params"] = params
        reply, sid = self._post(msg, token, session)
        if reply.get("error"):
            err = reply["error"]
            raise GatewayError("downstream_error", f"{self.conn['id']}: {method} failed: "
                                                   f"{str(err.get('message') or err)[:300]}")
        return reply.get("result") or {}, sid

    def _session(self, token):
        result, sid = self._rpc("initialize", {
            "protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "gt-lotr", "version": "0.3.0"}}, token, None)
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, token, sid)
        return sid

    def _token(self, force=False):
        if self._oauth:
            return oauth.access_token(self.conn, self._octx, force=force)
        if self._auth.get("scheme") != "bearer":
            return None
        return oauth.refuse_envelope(self._resolve(self._ref), self.conn.get("id"), ref=self._ref)

    def _refresh(self):
        cmd = self.conn.get("refresh_cmd")
        if not cmd:
            return False
        try:
            subprocess.run(list(cmd), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=REFRESH_TIMEOUT_S, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        return True

    def _authed(self, fn):
        """Run fn(token, session) in a fresh session; on 401 refresh once and retry once."""
        for attempt in (1, 2):
            token = self._token(force=self._oauth and attempt == 2)
            try:
                return fn(token, self._session(token))
            except _Unauthorized:
                if self._oauth:
                    oauth.invalidate(self.conn, self._octx.seat)
                    if attempt == 1:
                        continue
                    raise oauth.needs_login(self.conn, "the MCP server rejected a freshly "
                                                       "refreshed token (HTTP 401)")
                if attempt == 1 and self._refresh():
                    continue
                raise GatewayError(
                    "auth_failed",
                    f"{self.conn['id']}: the MCP endpoint refused the token from "
                    f"{self._ref} (HTTP 401)" + (" after refresh" if attempt == 2 else ""),
                    hints=["sign in again with the MCP client that owns the token file",
                           "check refresh_cmd" if self.conn.get("refresh_cmd")
                           else "add --refresh-cmd so LOTR can renew the token"])

    # -- the connection interface (same shape as HttpConnection) -----------------------------

    def discover(self):
        """The downstream tool list (tools/list, every page)."""
        def run(token, sid):
            tools, cursor = [], None
            for _ in range(50):
                result, _ = self._rpc("tools/list", {"cursor": cursor} if cursor else {},
                                      token, sid)
                tools += [t for t in result.get("tools") or [] if isinstance(t, dict)]
                cursor = result.get("nextCursor")
                if not cursor:
                    break
            return tools
        return self._authed(run)

    def call(self, op, args, *, cursor=None):
        name = op.get("name")
        if not name or op.get("method") != "MCP":
            raise GatewayError("bad_op", f"{self.conn['id']} is an MCP connection: call one of "
                                         "its tools by name (see find)")

        def run(token, sid):
            result, _ = self._rpc("tools/call", {"name": name, "arguments": dict(args or {})},
                                  token, sid)
            texts = [c.get("text", "") for c in result.get("content") or []
                     if isinstance(c, dict) and c.get("type") == "text"]
            if result.get("isError"):
                raise GatewayError("downstream_error",
                                   f"{self.conn['id']}: {name} failed: {' '.join(texts)[:300]}")
            data = result.get("structuredContent")
            if data is None:
                joined = "\n".join(texts)
                try:
                    data = json.loads(joined) if len(texts) == 1 else joined
                except ValueError:
                    data = joined
            return {"status": 200, "data": data, "next_cursor": None,
                    "notes": oauth.notes_for(self.conn) if self._oauth else []}
        return self._authed(run)
