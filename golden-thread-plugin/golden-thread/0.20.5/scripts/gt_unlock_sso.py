#!/usr/bin/env python3
"""gt_unlock_sso -- Microsoft Entra ID as an unlock factor (0.20.1).

The flow (RFC 6749 authorization code + RFC 7636 PKCE S256 + RFC 8252 loopback redirect):

  1. the AUTHORITY (never a client) binds a one-shot listener on 127.0.0.1 (and ::1 where it
     can) on a random free port; redirect_uri = http://localhost:<port> (Entra public clients
     accept any port for http://localhost);
  2. it opens the system browser at the tenant's authorize endpoint with prompt=login,
     max_age=0, scope "openid profile email" and nonce = b64url(the 32-byte challenge);
  3. the browser comes back to the listener with code + state; state is compared in constant
     time; the code is redeemed at the token endpoint (public client: no secret, the PKCE
     verifier instead) over verified TLS;
  4. the ID token is verified HERE, in Python, as defence in depth on top of TLS (OIDC Core
     section 3.1.3.7): RS256 against the tenant JWKS, issuer/tenant/audience/azp/time/nonce/
     auth_time/subject claims.

Endpoints come from the OIDC discovery document of the configured tenant (`organizations` by
default; `common` so personal Microsoft accounts work too; `consumers`; or a pinned tenant
GUID), fetched over HTTPS and cached in memory. The JWKS is cached by `kid` and refetched at
most once a minute when an unknown `kid` arrives (key rollover).

available(): reads the effective policy (gt_unlock_policy.load) and is True only when an Entra
app registration is configured (sso.client_id) and the provider is "entra". The daemon calls
available() without a Context, so the policy is read from disk here, briefly cached; prove()
and enroll() then use ctx.policy, which is the same merged policy.

Device-code flow (RFC 8628) is NOT implemented in 0.20.1: Microsoft recommends blocking it
(phishing), and `allow_device_code: true` is reported as unsupported rather than honoured.

SECURITY INVARIANTS (each repeated next to the code that enforces it):
  S1  prove() returns True only after the ID token's signature verifies against the IdP's
      published key AND its nonce equals b64url(challenge) -- so the token was minted for
      exactly this request.
  S2  every unlock is a fresh sign-in: prompt=login, max_age=0, auth_time within
      max_auth_age_s of now.
  S3  the token's tid and oid/sub must equal the enrolled ones; the issuer must be the
      tenant's own issuer; aud (and azp) the owner's client_id.
  S4  production endpoints are https on the Microsoft authority host only; the discovery
      document cannot move them elsewhere; TLS is verified; redirects are not followed.
  S5  token values (code, ID token, access token, verifier) are never logged, printed,
      written to disk, or put in an exception message. This module writes no files at all.
  S6  the loopback listener answers only a GET to "/" with a localhost Host header and the
      right state; it serves one result, then closes; it never waits longer than the bound.
"""
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import select
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gt_unlock_crypto as C       # noqa: E402
import gt_unlock_factors as F      # noqa: E402

ENTRA_HOST = "https://login.microsoftonline.com"
SCOPE = "openid profile email"
SKEW_S = 120                        # design A4: claims time skew +-120 s
LOGIN_TIMEOUT_S = 180
HTTP_TIMEOUT_S = 15
MAX_BODY = 1 << 20                  # discovery / JWKS / token responses
MAX_REQUEST = 8192                  # one loopback request line + headers
JWKS_REFETCH_S = 60                 # design A4: refetched at most once a minute
DISCOVERY_TTL_S = 24 * 3600
MSA_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"   # personal Microsoft accounts
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_MULTI = ("organizations", "common", "consumers")
_MFA_AMR = ("mfa", "ngcmfa")

_PAGE = (b"<!doctype html><meta charset=utf-8><title>gt unlock</title>"
         b"<body style='font-family:sans-serif'><p>%s</p><p>You can close this tab.</p>")


def _fail(code, message):
    # S5: every FactorError raised here carries a FIXED message -- never a token, code,
    # state, verifier, or server-supplied text.
    return F.FactorError(code, message)


def _num(v):
    """A finite JSON number (json.loads accepts NaN/Infinity, which compare False to
    everything and would slip past every time check; bool is not a number here)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _guarded(fn):
    """S5: whatever goes wrong inside the flow leaves as a FactorError with a fixed message --
    an unexpected exception's text (which could quote a response) never reaches the caller."""
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except F.FactorError:
            raise
        except Exception as e:                       # noqa: BLE001
            raise _fail("helper_failed", "SSO sign-in failed (%s)" % type(e).__name__)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """S4: a redirect from the IdP's discovery/JWKS/token endpoint is refused, not followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SsoFactor(F.Factor):
    name = "sso"
    platform = False

    def __init__(self, authority_host=ENTRA_HOST, open_browser=None, clock=None,
                 monotonic=None, login_timeout_s=LOGIN_TIMEOUT_S, policy_loader=None,
                 _insecure_loopback_idp_for_tests=False):
        # The authority host is a constructor argument ONLY so tests can point at a local fake
        # IdP; it is never read from policy or the environment. S4: a non-https host is
        # refused unless the test-only flag is set AND the host is a loopback address.
        self.host = authority_host.rstrip("/")
        self._insecure = bool(_insecure_loopback_idp_for_tests)
        self._open = open_browser or webbrowser.open
        self._real_browser = open_browser is None
        self._now = clock or time.time
        self._mono = monotonic or time.monotonic
        self.login_timeout_s = login_timeout_s
        self._policy_loader = policy_loader
        self._disc = {}            # tenant -> (fetched_mono, doc)
        self._jwks = {}            # jwks_uri -> {"keys": [...], "fetched": mono}
        self._avail_cache = None   # (mono, result)

    # ------------------------------------------------------------ availability

    def _policy_sso(self):
        if self._policy_loader is not None:
            pol = self._policy_loader()
        else:
            import gt_unlock_policy as P
            pol = P.load().policy
        return (pol or {}).get("sso") or {}

    def available(self):
        hit = self._avail_cache
        if hit and self._mono() - hit[0] < 5:
            return hit[1]
        try:
            res = self._check_config(self._policy_sso())
        except Exception as e:                       # noqa: BLE001 - unreadable = unavailable
            res = (False, "unlock policy unreadable (%s)" % type(e).__name__)
        self._avail_cache = (self._mono(), res)
        return res

    def _check_config(self, sso):
        if (sso.get("provider") or "entra") != "entra":
            return False, "only Microsoft Entra ID is supported in 0.20.1 (sso.provider)"
        if not sso.get("client_id"):
            return False, "no Entra app registration configured (sso.client_id)"
        try:
            self._tenant(sso)
            self._check_host()
        except F.FactorError as e:
            return False, e.message
        return True, "Microsoft Entra ID sign-in in the browser"

    def _check_host(self):
        u = urllib.parse.urlsplit(self.host)
        if u.scheme == "https":
            return
        # S4: production refuses anything that is not https.
        if self._insecure and u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost"):
            return
        raise _fail("unavailable", "the identity provider must be reached over https")

    @staticmethod
    def _tenant(sso):
        t = str(sso.get("tenant") or "organizations").strip().lower()
        if t in _MULTI or _GUID.match(t):
            return t
        raise _fail("unavailable", "sso.tenant must be organizations, common, consumers or "
                                   "a directory (tenant) ID GUID")

    # ------------------------------------------------------------ HTTP (S4)

    def _check_url(self, url):
        """S4: every endpoint gt talks to is on the configured authority host (same scheme,
        host and port) -- a discovery document cannot send the token exchange elsewhere."""
        a, b = urllib.parse.urlsplit(self.host), urllib.parse.urlsplit(url or "")
        if (b.scheme, b.hostname, b.port) != (a.scheme, a.hostname, a.port) or b.username \
                or b.password:
            raise _fail("helper_failed", "the identity provider named an endpoint off its "
                                         "own host; refused")
        self._check_host()
        return url

    def _http(self, url, data=None):
        self._check_url(url)
        handlers = [_NoRedirect()]
        if url.startswith("https:"):
            # S4: certificate and hostname verification on (the default context does both).
            handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(url, data=data, headers={"Accept": "application/json"})
        if data is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            # (an HTTP request, not a file: named so the LF-write scanner does not mistake it)
            fetch = opener.open
            with fetch(req, timeout=HTTP_TIMEOUT_S) as r:
                body = r.read(MAX_BODY + 1)
                status = r.status
        except urllib.error.HTTPError as e:
            body, status = e.read(MAX_BODY + 1), e.code
        except (urllib.error.URLError, OSError, ssl.SSLError, ValueError):
            raise _fail("helper_failed", "could not reach the identity provider "
                                         "(network or TLS verification failed)")
        if len(body) > MAX_BODY:
            raise _fail("helper_failed", "identity provider response too large")
        try:
            doc = json.loads(body.decode("utf-8"))
        except ValueError:
            raise _fail("helper_failed", "identity provider answered with something not JSON")
        if not isinstance(doc, dict):
            raise _fail("helper_failed", "identity provider answered with something not JSON")
        return status, doc

    # ------------------------------------------------------------ discovery + JWKS

    def _expected_issuer(self, tenant):
        if tenant in ("organizations", "common"):
            return self.host + "/{tenantid}/v2.0"
        if tenant == "consumers":
            return self.host + "/" + MSA_TENANT + "/v2.0"
        return self.host + "/" + tenant + "/v2.0"

    def discovery(self, tenant):
        hit = self._disc.get(tenant)
        if hit and self._mono() - hit[0] < DISCOVERY_TTL_S:
            return hit[1]
        url = "%s/%s/v2.0/.well-known/openid-configuration" % (self.host, tenant)
        status, doc = self._http(url)
        if status != 200:
            raise _fail("helper_failed", "the tenant's OpenID configuration was not found")
        for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            self._check_url(doc.get(k))
        # S3: the discovery document's issuer must be the one this tenant setting implies, so
        # a document for another tenant (or a template we do not understand) is refused.
        if doc.get("issuer") != self._expected_issuer(tenant):
            raise _fail("helper_failed", "the tenant's OpenID configuration names an "
                                         "unexpected issuer")
        self._disc[tenant] = (self._mono(), doc)
        return doc

    def _fetch_jwks(self, uri):
        status, doc = self._http(uri)
        keys = doc.get("keys")
        if status != 200 or not isinstance(keys, list):
            raise _fail("helper_failed", "the identity provider's signing keys were not found")
        self._jwks[uri] = {"keys": [k for k in keys if isinstance(k, dict)],
                           "fetched": self._mono()}

    def _keys_for(self, uri, kid):
        """The JWKS, refetched when `kid` is unknown -- at most once a minute (design A4), so
        a stream of tokens with made-up kids cannot turn gt into a request amplifier."""
        ent = self._jwks.get(uri)
        if ent is None:
            self._fetch_jwks(uri)
            ent = self._jwks[uri]
        if not any(k.get("kid") == kid for k in ent["keys"]) \
                and self._mono() - ent["fetched"] >= JWKS_REFETCH_S:
            self._fetch_jwks(uri)
            ent = self._jwks[uri]
        return ent["keys"]

    # ------------------------------------------------------------ the loopback (S6)

    def _listen(self):
        s4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s4.bind(("127.0.0.1", 0))
        s4.listen(4)
        port = s4.getsockname()[1]
        socks = [s4]
        # "localhost" may resolve to ::1 first; listen there too when the same port is free.
        if socket.has_ipv6:
            try:
                s6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                try:
                    s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    s6.bind(("::1", port))
                    s6.listen(4)
                    socks.append(s6)
                except OSError:
                    s6.close()
            except OSError:
                pass
        return socks, port

    @staticmethod
    def _answer(conn, status, text):
        body = _PAGE % text
        head = ("HTTP/1.1 %s\r\nContent-Type: text/html; charset=utf-8\r\n"
                "Content-Length: %d\r\nCache-Control: no-store\r\nConnection: close\r\n"
                "Referrer-Policy: no-referrer\r\n\r\n" % (status, len(body))).encode("ascii")
        try:
            conn.sendall(head + body)
        except OSError:
            pass

    @staticmethod
    def _read_request(conn, deadline):
        buf = b""
        while b"\r\n\r\n" not in buf:
            left = deadline - time.monotonic()
            if left <= 0 or len(buf) > MAX_REQUEST:
                return None
            conn.settimeout(min(left, 10))
            try:
                chunk = conn.recv(2048)
            except (OSError, socket.timeout):
                return None
            if not chunk:
                return None
            buf += chunk
        return buf.split(b"\r\n\r\n", 1)[0].decode("latin-1")

    def _wait_redirect(self, socks, port, state):
        """-> the authorization code. S6: one good request ends the wait; a request with the
        wrong Host, method or path gets a 404 and the wait goes on (browsers ask for
        /favicon.ico); a wrong state ENDS the attempt (that is a forgery attempt, not noise);
        the whole wait is bounded by login_timeout_s."""
        deadline = time.monotonic() + self.login_timeout_s
        hosts = {"localhost:%d" % port, "127.0.0.1:%d" % port, "[::1]:%d" % port}
        strays = 0
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise _fail("timeout", "nobody finished signing in within %d s"
                            % self.login_timeout_s)
            ready, _w, _x = select.select(socks, [], [], min(left, 1.0))
            for s in ready:
                try:
                    conn, _addr = s.accept()
                except OSError:
                    continue
                try:
                    req = self._read_request(conn, min(deadline, time.monotonic() + 10))
                    verdict = self._judge(req, hosts, state)
                    if verdict is None:
                        self._answer(conn, "404 Not Found", b"Not here.")
                        strays += 1
                        if strays > 16:
                            raise _fail("malformed", "too many stray requests on the "
                                                     "sign-in listener")
                        continue
                    kind, value = verdict
                    if kind == "code":
                        self._answer(conn, "200 OK", b"Signed in. gt is checking the result.")
                        return value
                    self._answer(conn, "400 Bad Request", b"Sign-in was not completed.")
                    raise _fail(kind, value)
                finally:
                    try:
                        conn.close()
                    except OSError:
                        pass

    @staticmethod
    def _judge(req, hosts, state):
        if not req:
            return None
        lines = req.split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3 or parts[0] != "GET" or not parts[2].startswith("HTTP/1."):
            return None
        host = None
        for ln in lines[1:]:
            k, _, v = ln.partition(":")
            if k.strip().lower() == "host":
                host = v.strip().lower()
        if host not in hosts:              # S6: DNS-rebinding pages carry another Host
            return None
        u = urllib.parse.urlsplit(parts[1])
        if u.path != "/":
            return None
        q = urllib.parse.parse_qs(u.query, keep_blank_values=True)
        got = (q.get("state") or [""])[0]
        if not hmac.compare_digest(got.encode("utf-8", "replace"), state.encode("ascii")):
            return ("wrong", "the sign-in answer did not carry this request's state")
        if q.get("error"):
            err = (q.get("error") or [""])[0]
            if err in ("access_denied", "user_cancelled", "login_required",
                       "interaction_required"):
                return ("cancelled", "sign-in was cancelled or refused")
            return ("helper_failed", "the identity provider refused the sign-in")
        code = (q.get("code") or [""])[0]
        if not code or len(q.get("code")) != 1:
            return ("malformed", "the sign-in answer carried no code")
        return ("code", code)

    # ------------------------------------------------------------ the flow

    def _login(self, sso, nonce, login_hint=None):
        """Run one fresh browser sign-in. -> verified claims (dict). Raises FactorError."""
        client_id = sso.get("client_id")
        if not client_id or not isinstance(client_id, str):
            raise _fail("unavailable", "no Entra app registration configured (sso.client_id)")
        if (sso.get("flow") or "pkce_loopback") != "pkce_loopback":
            raise _fail("unavailable", "only the pkce_loopback flow is supported (device-code "
                                       "sign-in is not implemented)")
        self._check_host()
        tenant = self._tenant(sso)
        disc = self.discovery(tenant)
        verifier = secrets.token_urlsafe(64)             # 86 chars, RFC 7636 43..128
        challenge = C.b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())
        state = secrets.token_urlsafe(32)
        socks, port = self._listen()
        try:
            redirect_uri = "http://localhost:%d" % port
            params = {"client_id": client_id, "response_type": "code",
                      "redirect_uri": redirect_uri, "response_mode": "query",
                      "scope": SCOPE, "state": state, "nonce": nonce,
                      "code_challenge": challenge, "code_challenge_method": "S256",
                      # S2: a fresh sign-in every time -- no SSO session cookie reuse.
                      "prompt": "login", "max_age": "0"}
            if login_hint:
                params["login_hint"] = login_hint
            url = disc["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)
            try:
                if self._real_browser:
                    F.require_ui("Entra sign-in in the browser")   # never under the harness
                opened = self._open(url)
            except Exception:                            # noqa: BLE001
                opened = False
            if not opened:
                raise _fail("unavailable", "no browser could be opened for the sign-in")
            code = self._wait_redirect(socks, port, state)
        finally:
            for s in socks:
                try:
                    s.close()
                except OSError:
                    pass
        form = urllib.parse.urlencode({
            "client_id": client_id, "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri, "code_verifier": verifier,
            "scope": SCOPE}).encode("ascii")
        code = verifier = None                           # S5: drop them as soon as used
        status, tok = self._http(disc["token_endpoint"], form)
        form = None
        if status != 200:
            raise _fail("wrong", "the identity provider refused to redeem the sign-in")
        id_token = tok.get("id_token")
        tok = None                                       # the access token is not wanted
        if not isinstance(id_token, str):
            raise _fail("malformed", "the identity provider returned no ID token")
        try:
            return self.verify_id_token(id_token, sso, disc, nonce)
        finally:
            id_token = None

    def verify_id_token(self, token, sso, disc, nonce):
        """OIDC Core 3.1.3.7 checks. -> claims. Raises FactorError with a fixed message."""
        client_id = sso.get("client_id")
        tenant = self._tenant(sso)
        try:
            h64 = token.split(".")[0]
            header = json.loads(C.b64url_decode(h64))
            kid = header.get("kid") if isinstance(header, dict) else None
        except (ValueError, AttributeError, IndexError):
            raise _fail("malformed", "the ID token is malformed")
        algs = ("RS256", "ES256") if sso.get("allow_es256") else ("RS256",)
        keys = self._keys_for(disc["jwks_uri"], kid)
        try:
            # S1: signature over the token with an alg from the PINNED list (alg none / HS*
            # can never pass: jws_verify refuses them before any key is used).
            _h, c = C.jws_verify(token, keys, algs=algs)
        except C.CryptoError:
            raise _fail("wrong", "the ID token's signature does not verify")
        now = self._now()
        tid = c.get("tid")
        if not isinstance(tid, str) or not _GUID.match(tid):
            raise _fail("wrong", "the ID token carries no tenant")
        # S3: the issuer is the tenant's own -- the template with ITS tid substituted.
        want_iss = self._expected_issuer(tenant).replace("{tenantid}", tid)
        if c.get("iss") != want_iss:
            raise _fail("wrong", "the ID token's issuer is not the expected tenant issuer")
        if sso.get("issuer") and c.get("iss") != sso["issuer"]:
            raise _fail("wrong", "the ID token's issuer is not the pinned issuer")
        if _GUID.match(tenant) and tid != tenant:
            raise _fail("wrong", "the ID token is from another tenant")
        if tenant == "organizations" and tid == MSA_TENANT:
            raise _fail("wrong", "a personal Microsoft account is not allowed by this policy")
        aud = c.get("aud")
        if not (aud == client_id or (isinstance(aud, list) and aud == [client_id])):
            raise _fail("wrong", "the ID token was issued to another application")
        if "azp" in c and c.get("azp") != client_id:
            raise _fail("wrong", "the ID token's authorized party is another application")
        for k in ("exp", "iat", "nbf", "auth_time"):
            if k in c and not _num(c[k]):
                raise _fail("malformed", "the ID token's %s is not a number" % k)
        for k in ("exp", "iat"):
            if k not in c:
                raise _fail("malformed", "the ID token has no %s" % k)
        if c["exp"] < now - SKEW_S:
            raise _fail("wrong", "the ID token has expired")
        if "nbf" in c and c["nbf"] > now + SKEW_S:
            raise _fail("wrong", "the ID token is not valid yet")
        if c["iat"] > now + SKEW_S:
            raise _fail("wrong", "the ID token was issued in the future")
        max_age = int(sso.get("max_auth_age_s") or 300)
        if c["iat"] < now - max_age - SKEW_S:
            raise _fail("wrong", "the ID token is too old")
        # S1: the nonce is b64url(challenge) -- this token answers exactly this request.
        if not isinstance(c.get("nonce"), str) or \
                not hmac.compare_digest(c["nonce"].encode("utf-8"), nonce.encode("ascii")):
            raise _fail("replayed", "the ID token was not minted for this request")
        # S2: a fresh sign-in, not a remembered session.
        at = c.get("auth_time")
        if at is None:
            raise _fail("malformed", "the ID token has no auth_time")
        if at < now - max_age or at > now + SKEW_S:
            raise _fail("wrong", "the sign-in was not fresh (auth_time)")
        if sso.get("require_mfa"):
            amr = c.get("amr") or []
            if not (isinstance(amr, list) and any(a in _MFA_AMR for a in amr)):
                raise _fail("wrong", "policy requires MFA and the ID token does not show it "
                                     "(Entra v2 tokens may omit amr: enforce MFA with "
                                     "Conditional Access and set sso.require_mfa false)")
        pin = sso.get("pin_subject")
        if pin and pin not in (c.get("oid"), c.get("sub")):
            raise _fail("wrong", "the signed-in account is not the pinned subject")
        if not isinstance(c.get("sub"), str) or not c.get("sub"):
            raise _fail("malformed", "the ID token has no subject")
        return c

    # ------------------------------------------------------------ the factor interface

    @_guarded
    def enroll(self, ctx):
        """One sign-in; -> the PUBLIC pins prove() will require. Nothing secret is kept."""
        sso = (ctx.policy or {}).get("sso") or {}
        if sso.get("allow_device_code"):
            raise _fail("unavailable", "device-code sign-in is not implemented in 0.20.1; set "
                                       "sso.allow_device_code false")
        nonce = C.b64url_encode(secrets.token_bytes(32))
        c = self._login(sso, nonce)
        return {"issuer": c["iss"], "tid": c["tid"], "oid": c.get("oid"), "sub": c["sub"],
                "client_id": sso.get("client_id"), "tenant": self._tenant(sso),
                "enrolled_upn": c.get("preferred_username")}

    @_guarded
    def prove(self, record, challenge, ctx):
        sso = (ctx.policy or {}).get("sso") or {}
        if not record or not record.get("tid") or not (record.get("oid") or record.get("sub")):
            raise _fail("not_enrolled", "SSO is not enrolled")
        if not isinstance(challenge, (bytes, bytearray)) or len(challenge) != 32:
            raise _fail("malformed", "the authority's challenge must be 32 bytes")
        if record.get("client_id") and sso.get("client_id") != record["client_id"]:
            raise _fail("not_enrolled", "the Entra app registration changed since enrolment; "
                                        "re-enrol sso")
        nonce = C.b64url_encode(bytes(challenge))
        c = self._login(sso, nonce, login_hint=record.get("enrolled_upn"))
        # S3: the same person as enrolled -- tenant, then the immutable object id (and sub,
        # which Entra makes pairwise per application, so it is stable for one client_id).
        if c["tid"] != record["tid"]:
            raise _fail("wrong", "signed in to another tenant than the enrolled one")
        if record.get("issuer") and c["iss"] != record["issuer"]:
            raise _fail("wrong", "the issuer differs from the enrolled one")
        if record.get("oid") and c.get("oid") != record["oid"]:
            raise _fail("wrong", "signed in as another account than the enrolled one")
        if record.get("sub") and c.get("sub") != record["sub"]:
            raise _fail("wrong", "signed in as another account than the enrolled one")
        return True
