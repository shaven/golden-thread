#!/usr/bin/env python3
"""fake_oauth_mcp.py -- an in-process fake OAuth authorization server AND an OAuth-protected MCP
server (streamable HTTP), for testing and reviewing gt-lotr's native OAuth. Stdlib only.

    from fake_oauth_mcp import FakeWorld
    w = FakeWorld(); w.start()
    w.rs_url            # https-less loopback MCP endpoint, e.g. http://127.0.0.1:PORT/mcp
    w.as_url            # the authorization server (a DIFFERENT origin from the MCP server)
    w.stop()

    python3 dev/fake_oauth_mcp.py --serve      # stand-alone, prints the two URLs, Ctrl-C to stop

Never a real service: no network beyond 127.0.0.1, every token is a random string that exists only
in this process (`w.issued` lists them so a test can assert none of them leaks into output).

What it implements (RFC / draft in brackets):
  RS   POST /mcp  initialize, notifications/initialized, tools/list, tools/call; 401 with
       WWW-Authenticate Bearer resource_metadata=... [RFC 9728, RFC 6750]; rejects a token that is
       expired, revoked, issued for another resource, or unknown.
       GET /.well-known/oauth-protected-resource[/mcp]                          [RFC 9728]
  AS   GET /.well-known/oauth-authorization-server                              [RFC 8414]
       POST /register                       dynamic client registration         [RFC 7591]
       GET  /authorize                      code + PKCE S256 + resource + iss   [RFC 7636/8707/9207]
       POST /token                          authorization_code and refresh_token, refresh-token
                                            ROTATION with reuse detection (a replayed old refresh
                                            token revokes the whole family)     [OAuth 2.1 4.3.1]
       POST /revoke                         [RFC 7009]
Behaviour switches live in `w.opt` (a dict; see DEFAULTS) so a test can break exactly one thing.
"""
import base64
import hashlib
import json
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULTS = {
    "dcr": True,                    # advertise + serve /register
    "advertise_pkce": True,         # code_challenge_methods_supported present with S256
    "iss_param": True,              # send iss in the authorization response + advertise it
    "omit_iss": False,              # advertise RFC 9207 but do not send iss
    "wrong_iss": False,             # send the wrong iss
    "wrong_state": False,           # send the wrong state back
    "deny": False,                  # answer the authorization request with access_denied
    "legacy_no_prm": False,         # no protected-resource metadata at all (origin = AS)
    "prm_in_header": True,          # 401 carries resource_metadata (else well-known probing)
    "prm_resource": None,           # override the PRM `resource` (wrong-resource test)
    "prm_redirect": None,           # PRM URL answers 302 to this URL
    "as_issuer": None,              # override the AS metadata `issuer`
    "entra_template_origin": None,  # entra: the origin in the {tenantid} issuer template (wrong-tenant test)
    "authorization_endpoint": None, # override the metadata authorization_endpoint
    "as_servers": None,             # override PRM authorization_servers
    "oversize_prm": False,          # PRM body larger than any sane cap
    "oversize_token": False,        # token response larger than any sane cap
    "access_ttl": 3600,
    "issue_refresh": True,
    "rotate": True,                 # refresh-token rotation
    "jwt_aud": None,                # make access tokens JWTs whose aud is this value
    "scope_in_challenge": "read write",
    "scopes_supported": ["read", "write"],
    "revocation": True,
    "token_type": "Bearer",
    "authorize_extra_params": {},
    "dcr_alter_redirect": False,    # the registration response changes the redirect_uris
    # -- provider emulations (pre-registered clients, no registration_endpoint) --------------
    "google": False,                # PRM lists the AS with a trailing slash; metadata issuer has none;
                                    # token auth methods client_secret_post/basic (no `none`);
                                    # initialize + tools/list answer 200 with NO credentials, only
                                    # tools/call is challenged (401 whose resource_metadata URL has
                                    # the tool name appended)
    "entra": False,                 # metadata only at <issuer>/.well-known/openid-configuration with a
                                    # {tenantid} issuer template; no code_challenge_methods_supported;
                                    # no iss; endpoints under /organizations/v2.0
    "require_secret": None,         # the token endpoint wants this client_secret in the form body
    "audience_only": None,          # reject a `resource` not starting with this (AADSTS9010010)
    "require_resource": True,       # the authorization / token endpoints demand `resource`
    "redirect_hosts": None,         # accepted redirect hosts (port ignored), e.g. ["localhost"]
}


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


TOOLS = [
    {"name": "search_issues", "description": "Search issues with a query",
     "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}},
    {"name": "create_issue", "description": "Create an issue",
     "inputSchema": {"type": "object", "properties": {"summary": {"type": "string"}}}},
]


class FakeWorld:
    def __init__(self, **opt):
        self.opt = dict(DEFAULTS)
        self.opt.update(opt)
        self.lock = threading.RLock()
        self.clients = {}            # client_id -> {redirect_uris, secret}
        self.codes = {}              # code -> {...}
        self.access = {}             # token -> {exp, resource, family, scope, revoked}
        self.refresh = {}            # token -> family
        self.current_rt = {}         # family -> the one valid refresh token
        self.dead_families = set()
        self.issued = []             # every secret value ever issued (for leak scans)
        self.log = []                # (server, method, path) -- no query, no tokens
        self.refresh_calls = 0
        self.revoked_calls = []
        self.tool_calls = []
        self.token_requests = []     # parsed forms (test inspection only)
        self.authorize_requests = []
        self.registrations = []
        self.rs = self.as_ = None

    # -- lifecycle ---------------------------------------------------------------------------
    def start(self):
        world = self
        self.rs = ThreadingHTTPServer(("127.0.0.1", 0), _handler("rs", world))
        self.as_ = ThreadingHTTPServer(("127.0.0.1", 0), _handler("as", world))
        for s in (self.rs, self.as_):
            s.daemon_threads = True
            threading.Thread(target=s.serve_forever, daemon=True).start()
        self.rs_origin = "http://127.0.0.1:%d" % self.rs.server_address[1]
        self.as_url = "http://127.0.0.1:%d" % self.as_.server_address[1]
        self.rs_url = self.rs_origin + "/mcp"
        return self

    def stop(self):
        for s in (self.rs, self.as_):
            if s:
                s.shutdown()
                s.server_close()

    @property
    def issuer(self):
        if self.opt["entra"]:
            return self.as_url + "/organizations/v2.0"
        return self.rs_origin if self.opt["legacy_no_prm"] else self.as_url

    # -- test helpers ------------------------------------------------------------------------
    def expire_all_access(self):
        with self.lock:
            for t in self.access.values():
                t["exp"] = 0

    def revoke_everything(self):
        with self.lock:
            for t in self.access.values():
                t["revoked"] = True
            self.dead_families.update(set(self.current_rt))

    def last_refresh_token(self):
        with self.lock:
            return list(self.current_rt.values())[-1] if self.current_rt else None

    # -- token minting -----------------------------------------------------------------------
    def _mint(self, family, resource, scope):
        ttl = self.opt["access_ttl"]
        if self.opt["jwt_aud"]:
            payload = _b64(json.dumps({"aud": self.opt["jwt_aud"], "sub": "u", "n": secrets.token_hex(8)}).encode())
            tok = "e30." + payload + "." + _b64(secrets.token_bytes(16))
        else:
            tok = "at_" + secrets.token_urlsafe(24)
        with self.lock:
            self.access[tok] = {"exp": time.time() + ttl, "resource": resource, "family": family,
                                "scope": scope, "revoked": False}
            self.issued.append(tok)
        return tok, ttl

    def _new_refresh(self, family):
        rt = "rt_" + secrets.token_urlsafe(24)
        with self.lock:
            self.refresh[rt] = family
            self.current_rt[family] = rt
            self.issued.append(rt)
        return rt


def _handler(which, world):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        # -- plumbing
        def _send(self, status, body=b"", ctype="application/json", extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _body(self):
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def _form(self):
            return {k: v[0] for k, v in urllib.parse.parse_qs(self._body().decode(),
                                                               keep_blank_values=True).items()}

        def _note(self, path):
            with world.lock:
                world.log.append((which, self.command, path))

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            self._note(u.path)
            o = world.opt
            if which == "rs" and u.path.startswith("/.well-known/oauth-protected-resource"):
                if o["legacy_no_prm"]:
                    return self._send(404, b"Not Found", "text/plain")
                if o["prm_redirect"] and not u.path.endswith("/other"):
                    return self._send(302, b"", extra={"Location": o["prm_redirect"]})
                if o["oversize_prm"]:
                    return self._send(200, b"{" + b'"x":"' + b"A" * (2 * 1024 * 1024) + b'"}')
                servers = o["as_servers"] or [world.issuer + ("/" if o["google"] else "")]
                return self._send(200, {"resource": o["prm_resource"] or world.rs_url,
                                        "authorization_servers": servers,
                                        "scopes_supported": o["scopes_supported"],
                                        "bearer_methods_supported": ["header"]})
            if which == "as" and o["entra"] and u.path == "/organizations/v2.0/.well-known/openid-configuration":
                base = world.issuer
                return self._send(200, {"issuer": (o["entra_template_origin"] or world.as_url) + "/{tenantid}/v2.0",
                                        "authorization_endpoint": o["authorization_endpoint"] or base + "/authorize",
                                        "token_endpoint": base + "/token",
                                        "response_types_supported": ["code", "id_token"],
                                        "token_endpoint_auth_methods_supported": ["client_secret_post", "private_key_jwt"],
                                        "scopes_supported": ["openid", "profile", "offline_access"]})
            if which == "as" and u.path.startswith("/.well-known/") and not o["entra"] or \
                    (which == "rs" and o["legacy_no_prm"] and u.path.startswith("/.well-known/oauth-authorization-server")):
                if "oauth-authorization-server" not in u.path:
                    return self._send(404, b"Not Found", "text/plain")
                base = world.issuer
                md = {"issuer": o["as_issuer"] or base,
                      "authorization_endpoint": o["authorization_endpoint"] or base + "/authorize",
                      "token_endpoint": base + "/token",
                      "response_types_supported": ["code"],
                      "grant_types_supported": ["authorization_code", "refresh_token"],
                      "token_endpoint_auth_methods_supported": (
                          ["client_secret_post", "client_secret_basic"] if o["google"]
                          else ["none", "client_secret_basic"]),
                      "scopes_supported": o["scopes_supported"]}
                if o["google"]:
                    md["scopes_supported"] = ["openid", "email", "profile"]
                if o["dcr"] and not o["google"]:
                    md["registration_endpoint"] = base + "/register"
                if o["revocation"]:
                    md["revocation_endpoint"] = base + "/revoke"
                if o["advertise_pkce"]:
                    md["code_challenge_methods_supported"] = ["S256"]
                if o["iss_param"]:
                    md["authorization_response_iss_parameter_supported"] = True
                return self._send(200, md)
            if (which == "as" or o["legacy_no_prm"]) and u.path.endswith("/authorize"):
                return self._authorize(u)
            return self._send(404, b"Not Found", "text/plain")

        def do_POST(self):
            u = urllib.parse.urlsplit(self.path)
            self._note(u.path)
            if which == "rs" and u.path == "/mcp":
                return self._mcp()
            if which == "as" or (which == "rs" and world.opt["legacy_no_prm"]):
                if u.path.endswith("/register"):
                    return self._register()
                if u.path.endswith("/token"):
                    return self._token()
                if u.path.endswith("/revoke"):
                    return self._revoke()
            return self._send(404, b"Not Found", "text/plain")

        # -- AS
        def _register(self):
            if not world.opt["dcr"]:
                return self._send(404, b"Not Found", "text/plain")
            doc = json.loads(self._body() or b"{}")
            cid = "client_" + secrets.token_hex(6)
            with world.lock:
                world.clients[cid] = {"redirect_uris": list(doc.get("redirect_uris") or [])}
                world.registrations.append(doc)
            ru = ["http://127.0.0.1:1/other"] if world.opt["dcr_alter_redirect"] else doc.get("redirect_uris")
            return self._send(201, {"client_id": cid, "redirect_uris": ru,
                                    "token_endpoint_auth_method": "none",
                                    "grant_types": doc.get("grant_types")})

        def _authorize(self, u):
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query, keep_blank_values=True).items()}
            o = world.opt
            with world.lock:
                world.authorize_requests.append(dict(q))
            c = world.clients.get(q.get("client_id"))
            if c is None and not (q.get("client_id") or "").startswith("pre_"):
                return self._send(400, {"error": "invalid_client"})
            ru = q.get("redirect_uri", "")
            if c is not None and ru not in c["redirect_uris"]:
                return self._send(400, {"error": "invalid_request",
                                        "error_description": "redirect_uri mismatch"})
            if q.get("response_type") != "code" or q.get("code_challenge_method") != "S256" \
                    or not q.get("code_challenge") or not q.get("state"):
                return self._send(400, {"error": "invalid_request"})
            if not q.get("resource") and o["require_resource"]:
                return self._send(400, {"error": "invalid_target"})
            if q.get("resource") and o["audience_only"] and not q["resource"].startswith(o["audience_only"]):
                return self._send(400, {"error": "invalid_target",
                                        "error_description": "AADSTS9010010: resource and scope audience differ"})
            if o["redirect_hosts"] is not None:
                rp = urllib.parse.urlsplit(ru)
                if rp.hostname not in o["redirect_hosts"] or rp.path != "/callback" or rp.scheme != "http":
                    return self._send(400, {"error": "invalid_request",
                                            "error_description": "redirect_uri not registered"})
            out = {}
            if o["deny"]:
                out["error"] = "access_denied"
            else:
                code = "code_" + secrets.token_urlsafe(16)
                with world.lock:
                    world.codes[code] = {"client_id": q["client_id"], "redirect_uri": ru,
                                         "challenge": q["code_challenge"],
                                         "resource": q.get("resource"), "scope": q.get("scope"),
                                         "used": False}
                    world.issued.append(code)
                out["code"] = code
            out["state"] = "forged" if o["wrong_state"] else q["state"]
            if o["iss_param"] and not o["omit_iss"]:
                out["iss"] = "https://evil.example" if o["wrong_iss"] else world.issuer
            out.update(o["authorize_extra_params"])
            loc = ru + ("&" if "?" in ru else "?") + urllib.parse.urlencode(out)
            return self._send(302, b"", extra={"Location": loc})

        def _client_ok(self, form):
            cid = form.get("client_id")
            if not cid:
                auth = self.headers.get("Authorization", "")
                if auth.startswith("Basic "):
                    cid = urllib.parse.unquote(base64.b64decode(auth[6:]).decode().split(":", 1)[0])
            return cid if (cid in world.clients or (cid or "").startswith("pre_")) else None

        def _token(self):
            o = world.opt
            f = self._form()
            with world.lock:
                world.token_requests.append(dict(f))
            if self._client_ok(f) is None:
                return self._send(401, {"error": "invalid_client"})
            gt = f.get("grant_type")
            if o["require_secret"] is not None and f.get("client_secret") != o["require_secret"]:
                return self._send(401, {"error": "invalid_client"})
            if not f.get("resource") and o["require_resource"]:
                return self._send(400, {"error": "invalid_target"})
            if f.get("resource") and o["audience_only"] and not f["resource"].startswith(o["audience_only"]):
                return self._send(400, {"error": "invalid_target"})
            if gt == "authorization_code":
                with world.lock:
                    rec = world.codes.get(f.get("code"))
                    if not rec or rec["used"]:
                        return self._send(400, {"error": "invalid_grant"})
                    rec["used"] = True
                verifier = f.get("code_verifier", "")
                chal = _b64(hashlib.sha256(verifier.encode("ascii", "replace")).digest())
                if chal != rec["challenge"] or rec["redirect_uri"] != f.get("redirect_uri") \
                        or rec["client_id"] != f.get("client_id", rec["client_id"]) \
                        or rec["resource"] != f.get("resource"):
                    return self._send(400, {"error": "invalid_grant"})
                family = "fam_" + secrets.token_hex(4)
                tok, ttl = world._mint(family, f.get("resource") or world.rs_url, rec["scope"])
                doc = {"access_token": tok, "token_type": o["token_type"], "expires_in": ttl}
                if rec["scope"]:
                    doc["scope"] = rec["scope"]
                if o["issue_refresh"]:
                    doc["refresh_token"] = world._new_refresh(family)
                if o["oversize_token"]:
                    doc["pad"] = "A" * (2 * 1024 * 1024)
                return self._send(200, doc)
            if gt == "refresh_token":
                rt = f.get("refresh_token", "")
                with world.lock:
                    world.refresh_calls += 1
                    fam = world.refresh.get(rt)
                    if fam is None or fam in world.dead_families:
                        return self._send(400, {"error": "invalid_grant"})
                    if o["rotate"] and world.current_rt.get(fam) != rt:
                        world.dead_families.add(fam)          # reuse of a rotated token
                        return self._send(400, {"error": "invalid_grant"})
                tok, ttl = world._mint(fam, f.get("resource") or world.rs_url, None)
                doc = {"access_token": tok, "token_type": o["token_type"], "expires_in": ttl}
                if o["rotate"]:
                    doc["refresh_token"] = world._new_refresh(fam)
                return self._send(200, doc)
            return self._send(400, {"error": "unsupported_grant_type"})

        def _revoke(self):
            f = self._form()
            tok = f.get("token", "")
            with world.lock:
                world.revoked_calls.append(f.get("token_type_hint"))
                fam = world.refresh.get(tok)
                if fam:
                    world.dead_families.add(fam)
                    for t in world.access.values():
                        if t["family"] == fam:
                            t["revoked"] = True
                if tok in world.access:
                    world.access[tok]["revoked"] = True
            return self._send(200, b"", "text/plain")

        # -- RS
        def _unauth(self, error=None):
            o = world.opt
            parts = ["Bearer realm=\"fake\""]
            if error:
                parts.append('error="%s"' % error)
            if o["prm_in_header"] and not o["legacy_no_prm"]:
                parts.append('resource_metadata="%s/.well-known/oauth-protected-resource/mcp"'
                             % world.rs_origin)
            if o["scope_in_challenge"]:
                parts.append('scope="%s"' % o["scope_in_challenge"])
            return self._send(401, b"", extra={"WWW-Authenticate": ", ".join(parts)})

        def _mcp(self):
            raw = self._body()
            auth = self.headers.get("Authorization", "")
            if world.opt["google"] and not auth:
                m0 = json.loads(raw)
                if m0.get("method") != "tools/call":      # Google challenges only tools/call
                    mm = m0.get("method")
                    if "id" not in m0:
                        return self._send(202, b"")
                    res = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                           "serverInfo": {"name": "fake-google", "version": "1"}} if mm == "initialize" \
                        else {"tools": TOOLS}
                    return self._send(200, {"jsonrpc": "2.0", "id": m0["id"], "result": res},
                                      extra={"Mcp-Session-Id": "sess-g"})
                tool = (m0.get("params") or {}).get("name", "x")
                return self._send(401, b"", extra={"WWW-Authenticate":
                    'Bearer resource_metadata="%s/.well-known/oauth-protected-resource/mcp/%s"'
                    % (world.rs_origin, tool)})
            if not auth.startswith("Bearer "):
                return self._unauth()
            tok = auth[7:]
            with world.lock:
                rec = world.access.get(tok)
            if rec is None or rec["revoked"] or rec["exp"] <= time.time() \
                    or rec["resource"] != world.rs_url or rec["family"] in world.dead_families:
                return self._unauth("invalid_token")
            msg = json.loads(raw)
            if "id" not in msg:
                return self._send(202, b"")
            m, result = msg["method"], None
            if m == "initialize":
                result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "fake-oauth", "version": "1"}}
            elif m == "tools/list":
                result = {"tools": TOOLS}
            elif m == "tools/call":
                name = msg["params"]["name"]
                with world.lock:
                    world.tool_calls.append(name)
                result = {"content": [{"type": "text", "text": json.dumps(
                    {"tool": name, "items": [{"key": "ABC-1"}]})}]}
            return self._send(200, {"jsonrpc": "2.0", "id": msg["id"], "result": result},
                              extra={"Mcp-Session-Id": "sess-1"})
    return H


def main():
    w = FakeWorld().start()
    print("MCP endpoint :", w.rs_url)
    print("auth server  :", w.as_url)
    print("connect with : lotr connect fake --url %s --allow-insecure-localhost "
          "--allow-private-network" % w.rs_url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        w.stop()


if __name__ == "__main__":
    sys.exit(main() if "--serve" in sys.argv else print(__doc__))
