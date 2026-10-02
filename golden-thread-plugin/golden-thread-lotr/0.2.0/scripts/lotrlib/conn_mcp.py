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
"""
import json
import subprocess
import urllib.error
import urllib.request

from .errors import GatewayError

PROTOCOL = "2025-03-26"
REFRESH_TIMEOUT_S = 60
USER_AGENT = "gt-lotr/0.2.0"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GatewayError("redirect_refused", "the MCP endpoint redirected; a bearer token is "
                                               "never re-sent to another URL")


class _Unauthorized(Exception):
    pass


class McpConnection:
    def __init__(self, conn, profile, *, secret_resolver=None, timeout=20):
        if secret_resolver is None:
            from .secrets import resolve as secret_resolver
        self.conn = conn
        self.profile = profile or {}
        self.timeout = timeout
        self._resolve = secret_resolver
        self.endpoint = conn["endpoint"]
        self._auth = conn.get("auth") or {}
        self._ref = self._auth.get("token_ref")
        self._opener = urllib.request.build_opener(_NoRedirect)
        self._next_id = 0

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

    def _post(self, message, token, session):
        """-> (reply message or None, session id from the reply's headers)."""
        req = urllib.request.Request(self.endpoint, data=json.dumps(message).encode("utf-8"),
                                     headers=self._headers(token, session), method="POST")
        try:
            resp = self._opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise _Unauthorized()
            raise GatewayError("downstream_error", f"{self.conn['id']}: the MCP endpoint "
                                                   f"answered HTTP {e.code}")
        except urllib.error.URLError as e:
            raise GatewayError("unreachable", f"{self.conn['id']}: cannot reach the MCP "
                                              f"endpoint ({getattr(e, 'reason', e)})")
        with resp:
            sid = resp.headers.get("Mcp-Session-Id")
            body = resp.read().decode("utf-8", errors="replace")
            ctype = (resp.headers.get("Content-Type") or "").lower()
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
            "clientInfo": {"name": "gt-lotr", "version": "0.2.0"}}, token, None)
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, token, sid)
        return sid

    def _token(self):
        if self._auth.get("scheme") != "bearer":
            return None
        return self._resolve(self._ref)

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
            token = self._token()
            try:
                return fn(token, self._session(token))
            except _Unauthorized:
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
            return {"status": 200, "data": data, "next_cursor": None, "notes": []}
        return self._authed(run)
