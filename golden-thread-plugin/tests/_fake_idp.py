"""A local, plain-http stand-in for Microsoft Entra ID, for the SSO unlock tests (0.20.0).

Serves, on 127.0.0.1 with Entra's path shapes:
  /<tenant>/v2.0/.well-known/openid-configuration   discovery
  /<tenant>/discovery/v2.0/keys                      JWKS (mutable: `keys`, counted)
  /<tenant>/oauth2/v2.0/authorize                    302 to the loopback with code + state
  /<tenant>/oauth2/v2.0/token                        checks PKCE, returns an ID token

ID tokens are signed with tests/_unlock_keys.RSAKey via _unlock_keys.jwt. Hooks let a test
bend the result (claims, the raw token, the redirect) to exercise each refusal. Test-only:
nothing in the shipped tree imports this file, and it never leaves 127.0.0.1.
"""
import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

import _unlock_keys as K

CLIENT_ID = "11111111-2222-3333-4444-555555555555"
TID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OID = "0f0f0f0f-1111-2222-3333-444444444444"


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class FakeIdP:
    def __init__(self, key=None, kid="k1"):
        self.key = key or K.RSAKey()
        self.kid = kid
        self.keys = [self.key.jwk(kid)]
        self.jwks_fetches = 0
        self.codes = {}
        self.issued = []                 # every code / id_token handed out (for leak checks)
        self.tid = TID
        self.oid = OID
        self.sub = "sub-" + OID
        self.claims_hook = None          # f(claims) -> claims
        self.token_hook = None           # f(claims) -> raw token string
        self.state_hook = None           # f(state) -> state sent back
        self.discovery_hook = None       # f(doc) -> doc
        self.last_authorize = None
        idp = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, status=200):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                parts = u.path.strip("/").split("/")
                tenant = parts[0] if parts else ""
                rest = "/".join(parts[1:])
                if rest == "v2.0/.well-known/openid-configuration":
                    return self._json(idp.discovery(tenant))
                if rest == "discovery/v2.0/keys":
                    idp.jwks_fetches += 1
                    return self._json({"keys": list(idp.keys)})
                if rest == "oauth2/v2.0/authorize":
                    q = dict(urllib.parse.parse_qsl(u.query))
                    idp.last_authorize = q
                    code = secrets.token_urlsafe(24)
                    idp.codes[code] = q
                    idp.issued.append(code)
                    state = q.get("state", "")
                    if idp.state_hook:
                        state = idp.state_hook(state)
                    loc = q["redirect_uri"] + "/?" + urllib.parse.urlencode(
                        {"code": code, "state": state, "session_state": "x"})
                    self.send_response(302)
                    self.send_header("Location", loc)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                return self._json({"error": "not_found"}, 404)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                form = dict(urllib.parse.parse_qsl(self.rfile.read(n).decode()))
                q = idp.codes.pop(form.get("code"), None)
                if q is None or form.get("grant_type") != "authorization_code" \
                        or form.get("client_id") != q.get("client_id") \
                        or form.get("redirect_uri") != q.get("redirect_uri"):
                    return self._json({"error": "invalid_grant"}, 400)
                ch = base64.urlsafe_b64encode(hashlib.sha256(
                    form.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
                if q.get("code_challenge_method") != "S256" or ch != q.get("code_challenge"):
                    return self._json({"error": "invalid_grant"}, 400)
                tok = idp.mint(q)
                idp.issued.append(tok)
                return self._json({"token_type": "Bearer", "id_token": tok,
                                   "access_token": "at-" + secrets.token_urlsafe(16),
                                   "expires_in": 3600})

        self.server = _Server(("127.0.0.1", 0), H)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def discovery(self, tenant):
        if tenant in ("organizations", "common"):
            iss = self.base + "/{tenantid}/v2.0"
        else:
            iss = self.base + "/" + tenant + "/v2.0"
        doc = {"issuer": iss,
               "authorization_endpoint": self.base + "/" + tenant + "/oauth2/v2.0/authorize",
               "token_endpoint": self.base + "/" + tenant + "/oauth2/v2.0/token",
               "jwks_uri": self.base + "/" + tenant + "/discovery/v2.0/keys",
               "id_token_signing_alg_values_supported": ["RS256"]}
        return self.discovery_hook(doc) if self.discovery_hook else doc

    def mint(self, q):
        now = int(time.time())
        claims = {"iss": self.base + "/" + self.tid + "/v2.0", "aud": q.get("client_id"),
                  "tid": self.tid, "oid": self.oid, "sub": self.sub,
                  "nonce": q.get("nonce"), "iat": now, "nbf": now, "exp": now + 3600,
                  "auth_time": now, "preferred_username": "owner@example.test",
                  "ver": "2.0"}
        if self.claims_hook:
            claims = self.claims_hook(claims)
        if self.token_hook:
            return self.token_hook(claims)
        return K.jwt(self.key, claims, kid=self.kid)


class _NoFollow(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def browser(log=None):
    """A stand-in for webbrowser.open: in a thread, GET the authorize URL, then follow its
    redirect to the loopback (as a browser would). Records the loopback's HTTP status."""
    def open_(url):
        def run():
            op = urllib.request.build_opener(_NoFollow())
            try:
                op.open(url, timeout=10)
                return
            except urllib.error.HTTPError as e:
                loc = e.headers.get("Location")
            if not loc:
                return
            try:
                with urllib.request.urlopen(loc, timeout=10) as r:
                    status = r.status
            except urllib.error.HTTPError as e:
                status = e.code
            except OSError:
                status = None
            if log is not None:
                log.append(status)
        threading.Thread(target=run, daemon=True).start()
        return True
    return open_


import urllib.error  # noqa: E402  (used inside browser())
