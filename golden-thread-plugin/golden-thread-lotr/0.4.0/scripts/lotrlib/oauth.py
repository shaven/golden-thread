"""Native OAuth for remote MCP servers (gt-lotr 0.3.0, shipping with gt 0.20.1; request 2026-10-05).

IMPLEMENTS the MCP authorization specification, revision 2026-07-28 (the current revision at
the time of writing; modelcontextprotocol.io/specification/2026-07-28/basic/authorization), and
the RFCs it selects: OAuth 2.1 (draft-ietf-oauth-v2-1-13) authorization code + PKCE S256,
RFC 9728 protected-resource metadata (WWW-Authenticate `resource_metadata` and the well-known
fallbacks), RFC 8414 authorization-server metadata with the OpenID Connect discovery fallbacks
(issuer must equal the issuer the URL was built from), RFC 7591 dynamic client registration
(`application_type: native`), OAuth Client ID Metadata Documents (draft-ietf-oauth-client-id-
metadata-document-00), RFC 8707 resource indicators (`resource` in the authorization AND the
token request), RFC 9207 `iss` authorization-response validation, RFC 7009 revocation, RFC 8252
loopback redirect. NOT implemented: device code, client_credentials, private_key_jwt, DPoP,
automatic step-up (an `insufficient_scope` is reported with the command that does it).

What this module is: pure protocol + token lifecycle. It holds no policy. Callers are
`lotr connect|login|disconnect` (a human at a terminal) and conn_mcp.McpConnection (lotrd).

SECURITY INVARIANTS
  O1  Every fetch is HTTPS (plain http only to a loopback host the owner named with
      --allow-insecure-localhost), refuses to follow a redirect off its origin, refuses
      private / loopback / link-local / metadata-service addresses (SSRF) unless the owner
      passed --allow-private-network, checks the address it CONNECTS to (so a DNS answer that
      changes between check and connect cannot slip through), and is bounded in bytes and time.
  O2  The loopback listener binds 127.0.0.1 only, on an ephemeral port (or the one the owner
      named), only while a login is in progress; it accepts ONE callback and closes. A wrong
      state, an `iss` that contradicts the recorded issuer, or an AS error ends the attempt.
  O3  A token (access, refresh, authorization code, client secret, PKCE verifier) is never
      logged, printed, put in argv or the environment, written into an error message, or
      returned in a tool result. Errors carry fixed text plus whitelisted server error CODES;
      URLs in messages have their query and fragment stripped. The ACCESS token lives only in
      lotrd's memory; the REFRESH token is stored by the caller as a ref (sealed: or store:).
  O4  Refresh-token rotation is atomic per connection: one refresh at a time, the new refresh
      token is persisted (re-sealed with the public-key seal, no prompt) before the access
      token is handed out; if persisting fails the new token is held in memory for the seat
      that obtained it, and the caller is told, rather than losing the only valid token.
  O5  The AS must advertise PKCE S256; an AS without `code_challenge_methods_supported`
      containing S256 is refused. `resource` is sent regardless of AS support.
"""
import base64
import binascii
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets as _stdsecrets
import socket
import ssl
import select
import sys
import threading
import time
import unicodedata
import urllib.parse

from .errors import GatewayError

SPEC_REVISION = "2026-07-28"
USER_AGENT = "gt-lotr/0.3.0"
MAX_DISCOVERY_BYTES = 256 * 1024
MAX_TOKEN_BYTES = 64 * 1024
MAX_PROBE_BYTES = 64 * 1024
MAX_TOKEN_CHARS = 16 * 1024
HTTP_TIMEOUT_S = 15
LOGIN_TIMEOUT_S = 180
SKEW_S = 60                      # an access token is renewed this long before it expires
DEFAULT_EXPIRES_S = 600          # an AS that does not say gets a conservative lifetime
MAX_ACCESS_CACHE_S = 3600
READ_TIMEOUT_S = 3                # one loopback request must arrive within this
MAX_STRAYS = 64
MAX_SEATS = 64                   # access tokens cached per connection, oldest evicted
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
METADATA_ADDRS = ("169.254.169.254", "fd00:ec2::254", "100.100.100.200", "168.63.129.16",
                  "192.0.0.192")
CALLBACK_PATH = "/callback"
AUTH_METHODS = ("none", "client_secret_basic", "client_secret_post")
SOURCES = ("preregistered", "cimd", "dcr")
_ERR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
# Only these server-supplied error values are ever echoed: RFC 6749 sections 4.1.2.1 and 5.2,
# RFC 6750, RFC 7591, RFC 8628, RFC 8707 and OIDC core. Any other value -- even one shaped like
# an identifier, which a server or a reflecting proxy could make a secret -- is not shown.
ECHO_ERRORS = frozenset((
    "invalid_request", "invalid_client", "invalid_grant", "unauthorized_client",
    "unsupported_grant_type", "invalid_scope", "access_denied", "unsupported_response_type",
    "server_error", "temporarily_unavailable", "invalid_token", "insufficient_scope",
    "invalid_redirect_uri", "invalid_client_metadata", "invalid_target", "interaction_required",
    "login_required", "consent_required", "authorization_pending", "slow_down", "expired_token",
    "user_cancelled"))
BAD_RESPONSE = "oauth_bad_response"


def echo_error(value):
    return value if isinstance(value, str) and value in ECHO_ERRORS else BAD_RESPONSE


def clock():
    """Wall clock seconds; tests replace it to age tokens without sleeping."""
    return time.time()


def _err(code, message, hints=None):
    return GatewayError(code, message, hints)


# --------------------------------------------------------------------------- URLs and policy

# RFC 6749 access_token charset: %x21-7E without DQUOTE and backslash (visible ASCII). Wider than
# RFC 6750 b64token: a real provider token carried ":" (Linear, 2026-10-05). Space, control,
# DEL and non-ASCII stay refused, so nothing a header cannot carry reaches the Bearer header.
_VISIBLE_TOKEN = re.compile(r"^[\x21\x23-\x5b\x5d-\x7e]+\Z")
_UNSAFE = re.compile(r"[^\x21-\x7e]")
_HINT_SCOPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._/+=@-]{0,127}")   # used with fullmatch: $ would pass "a\n"


def clean(text, limit=200):
    """Server-controlled text made safe to print: every control, space and non-ASCII character
    becomes `?`, cut to `limit`."""
    return _UNSAFE.sub("?", str(text))[:limit]


def hint_scope(text):
    """A server-supplied `scope` fit for a command hint, or None. -> the text that follows the
    first `--scope ` in the hint: `a` for one scope, `a --scope b` for two (the flag repeats, so
    NO quoting is ever needed, on any shell: cmd.exe and PowerShell quote differently from sh).
    Each space-separated token must be a plain scope word (letters and digits first, then also
    : . _ / + = @ -); anything else (quotes, shell metacharacters, control characters, over-long
    input) and the scope is left out of the hint altogether."""
    if not isinstance(text, str) or len(text) > 512:
        return None
    toks = text.split(" ")
    if not toks or len(toks) > 20 or not all(_HINT_SCOPE.fullmatch(t) for t in toks):
        return None
    return " --scope ".join(toks)


def safe_url(url):
    """`url` without query, fragment or userinfo -- the only form that may appear in a message."""
    return clean(_safe_url(url))


def _safe_url(url):
    try:
        p = urllib.parse.urlsplit(str(url))
        host = p.hostname or ""
        if ":" in host:
            host = "[" + host + "]"
        port = ":%d" % p.port if p.port else ""
        return "%s://%s%s%s" % (p.scheme, host, port, p.path)
    except ValueError:
        return "<unparseable url>"


def canonical_resource(url):
    """RFC 8707 canonical URI of an MCP server: lowercase scheme and host, default port
    dropped, no fragment, and no trailing slash on a bare origin (spec 2026-07-28)."""
    bad = _err("oauth_bad_resource", "the MCP endpoint is not a valid resource URI "
                                     "(needs a scheme and host and no fragment)")
    try:
        p = urllib.parse.urlsplit(url)
        if p.fragment or not p.scheme or not p.hostname:
            raise bad
        host = p.hostname.lower()
        port = p.port                                  # ValueError on https://h:notaport
    except (ValueError, TypeError, AttributeError):
        raise bad
    if ":" in host:
        host = "[" + host + "]"
    if port and not ((p.scheme == "https" and port == 443) or (p.scheme == "http" and port == 80)):
        host += ":%d" % port
    path = "" if p.path in ("", "/") else p.path
    return "%s://%s%s%s" % (p.scheme.lower(), host, path, ("?" + p.query) if p.query else "")


def origin(url):
    try:
        p = urllib.parse.urlsplit(url)
        port = p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        return ("", "", 0)
    return (p.scheme, (p.hostname or "").lower(), port)


_SITE_LOCAL = ipaddress.ip_network("fec0::/10")        # deprecated site-local: private


_FULL_STOPS = ("\u3002", "\uff61", "\uff0e")       # ideographic, halfwidth ideographic, fullwidth


def _browser_host(host):
    """The host as a browser (WHATWG host parsing) will resolve it, for the screens: percent-escapes
    decoded, compatibility characters folded (NFKC), every full-stop variant (U+3002, U+FF61, U+FF0E)
    mapped to "." (NFKC leaves U+3002 alone and folds U+FF61 INTO it, so it is mapped after NFKC),
    one trailing dot dropped, lowercased. Third review (1)."""
    h = urllib.parse.unquote(host)
    h = unicodedata.normalize("NFKC", h)
    for stop in _FULL_STOPS:
        h = h.replace(stop, ".")
    h = h.lower()
    if h.endswith("."):
        h = h[:-1]
    return h


class NetPolicy:
    """What a fetch may reach. Both flags are the OWNER's, set at `lotr connect` and kept in the
    registry; neither is ever inferred from a server's answer."""

    def __init__(self, allow_private=False, allow_insecure_localhost=False):
        self.allow_private = bool(allow_private)
        self.allow_insecure_localhost = bool(allow_insecure_localhost)

    @classmethod
    def from_block(cls, block):
        n = (block or {}).get("net") or {}
        return cls(n.get("allow_private"), n.get("allow_insecure_localhost"))

    def to_dict(self):
        return {"allow_private": self.allow_private,
                "allow_insecure_localhost": self.allow_insecure_localhost}

    def check_url(self, url, what="URL"):
        try:
            p = urllib.parse.urlsplit(url)
            host = (p.hostname or "").lower()
            p.port                                   # noqa: B018  (ValueError on a bad port)
        except (ValueError, AttributeError):
            raise _err("oauth_insecure_url", f"the {what} is not a valid URL")
        if p.username or p.password or p.fragment:
            raise _err("oauth_insecure_url", f"the {what} must not carry credentials or a fragment")
        if p.scheme == "https" and host:
            return p
        if p.scheme == "http" and host in LOOPBACK_HOSTS:
            if self.allow_insecure_localhost:
                return p
            raise _err("oauth_insecure_url",
                       f"the {what} {safe_url(url)} is plain http; only a loopback server the "
                       "owner names with --allow-insecure-localhost may use it")
        raise _err("oauth_insecure_url", f"the {what} {safe_url(url)} must be https")

    def check_literal_host(self, url, what="URL"):
        """A URL whose host is an IP literal (any spelling a browser or inet_aton accepts, e.g.
        2130706433 or 0x7f.1) must name an address check_address allows. A DNS name passes here
        (a browser does its own lookup; nothing of ours connects to it)."""
        try:
            host = urllib.parse.urlsplit(url).hostname or ""
        except ValueError:
            raise _err("oauth_insecure_url", f"the {what} is not a valid URL")
        # The screen judges what a BROWSER will resolve: percent-escapes decoded, compatibility
        # characters folded (fullwidth digits, fullwidth full stop), one trailing dot dropped (an
        # absolute DNS name; for a literal it changes nothing), then lowercased.
        host = _browser_host(host)
        if what == "authorization_endpoint" and (host == "localhost" or host.endswith(".localhost")):
            # Design (second review): the owner's browser is sent here; a local NAME is refused.
            raise _err("oauth_ssrf_refused", f"the {what} names localhost; a sign-in is not sent to "
                                             "this machine by name; refused")
        addr = None
        h = host.split("%", 1)[0]                            # a zone id on an IPv6 literal
        try:
            addr = str(ipaddress.ip_address(h))
        except ValueError:
            if re.match(r"^(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+)){0,3}$", h, re.I):
                try:
                    addr = socket.inet_ntoa(socket.inet_aton(h))
                except (OSError, ValueError):
                    addr = None
                if addr is None:
                    raise _err("oauth_ssrf_refused", f"the {what} names an address that cannot be "
                                                     "judged; refused")
        if addr is not None and not self.check_address(addr):
            raise _err("oauth_ssrf_refused",
                       f"the {what} points at a private, loopback, link-local or metadata "
                       "address; refused", hints=["only if you trust the server: add "
                                                  "--allow-private-network at connect time"])

    def check_address(self, ip_text):
        """May a connection to this address be made? Embedded IPv4 addresses are judged too:
        IPv4-mapped (::ffff:a.b.c.d), the RFC 2765 translated form (::ffff:0:a.b.c.d), NAT64
        (64:ff9b::/96), 6to4 (2002::/16) and the deprecated IPv4-compatible form (::a.b.c.d) are
        unwrapped, and EVERY form must pass."""
        ip = ipaddress.ip_address(ip_text)
        forms = [ip]
        if ip.version == 6:
            n = int(ip)
            inner = None                                  # an IPv4 address carried inside
            if ip.ipv4_mapped:
                inner = ip.ipv4_mapped
            elif ip in ipaddress.ip_network("64:ff9b::/96"):
                inner = ipaddress.IPv4Address(n & 0xFFFFFFFF)
            elif ip in ipaddress.ip_network("2002::/16"):
                inner = ipaddress.IPv4Address((n >> 80) & 0xFFFFFFFF)
            elif (n >> 32) == 0 and n > 1:                # ::a.b.c.d (IPv4-compatible)
                inner = ipaddress.IPv4Address(n & 0xFFFFFFFF)
            elif (n >> 32) == 0xFFFF0000:                 # ::ffff:0:a.b.c.d (SIIT, RFC 2765)
                inner = ipaddress.IPv4Address(n & 0xFFFFFFFF)
            if inner is not None:
                forms = [inner]                           # judged by the address inside alone
        for f in forms:
            if (str(f) in METADATA_ADDRS or f.is_multicast or f.is_unspecified
                    or f.is_link_local):              # 169.254.0.0/16 and fe80::/10: cloud
                return False                          # credential services; never, even with
                                                      # --allow-private-network
        ok = True
        for f in forms:
            if f.is_global and not (f.version == 6 and f in _SITE_LOCAL):
                continue
            if f.is_loopback and (self.allow_insecure_localhost or self.allow_private):
                continue
            ok = ok and self.allow_private
        return ok


# --------------------------------------------------------------------------- bounded fetching

class Response:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    def header(self, name):
        return [v for k, v in self.headers if k.lower() == name.lower()]

    def json(self):
        try:
            doc = json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError, OverflowError, MemoryError):
            raise _err("oauth_bad_response", "a server answered with something that is not JSON")
        if not isinstance(doc, dict):
            raise _err("oauth_bad_response", "a server answered with a JSON value that is not "
                                             "an object")
        return doc


def _resolve_within(host, port, deadline):
    """getaddrinfo, bounded by the fetch's total deadline (second review, MINOR 6). A watchdog cannot
    interrupt a blocked resolver, so the lookup runs on a daemon thread that is abandoned at the
    deadline; its answer, if it ever comes, is dropped."""
    box = []

    def run():
        try:
            box.append((True, socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
        except BaseException as e:                       # noqa: BLE001 - handed back to the caller
            box.append((False, e))
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(max(0.0, deadline - time.monotonic()))
    if not box:
        raise _err("unreachable", f"{clean(host)} did not resolve within the time budget")
    ok, val = box[0]
    if ok:
        return val
    if isinstance(val, (socket.gaierror, UnicodeError)):
        raise _err("unreachable", f"cannot resolve {clean(host)}")
    raise val


def _connect(parts, net, timeout, deadline=None):
    """A connected socket to a host whose every address passed the policy; the socket is made
    to the CHECKED address, so DNS cannot answer differently between check and use (O1).
    `deadline` (monotonic) bounds the WHOLE connect, every address of the host included: a
    multi-homed host that black-holes SYNs cannot multiply the timeout by its address count."""
    deadline = deadline if deadline is not None else time.monotonic() + timeout
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    infos = _resolve_within(host, port, deadline)
    addrs = []
    for fam, _t, _p, _c, sa in infos:
        if sa[0] not in addrs:
            addrs.append(sa[0])
    bad = [a for a in addrs if not net.check_address(a)]
    if bad or not addrs:
        raise _err("oauth_ssrf_refused",
                   f"{clean(host)} resolves to a private, loopback, link-local or metadata address; "
                   "refused", hints=["only if you trust the server: add --allow-private-network "
                                     "at connect time"])
    last = None
    for fam, _t, _p, _c, sa in infos:
        left = deadline - time.monotonic()
        if left <= 0:
            raise _err("unreachable", f"{clean(host)} did not accept a connection in time")
        sock = None
        try:
            sock = socket.socket(fam, socket.SOCK_STREAM)
            sock.settimeout(min(timeout, left))
            sock.connect(sa)
            return sock
        except OSError as e:
            last = e
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
    raise _err("unreachable", f"cannot connect to {clean(host)} ({getattr(last, 'strerror', None) or 'error'})")


def fetch(url, net, *, method="GET", body=None, headers=None, max_bytes=MAX_DISCOVERY_BYTES,
          timeout=HTTP_TIMEOUT_S, follow=0, what="URL"):
    """One bounded HTTP exchange -> Response. `follow` same-origin redirects (GET only) are
    followed; any other redirect is refused. 4xx/5xx are returned, not raised."""
    parts = net.check_url(url, what)
    deadline = time.monotonic() + timeout
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    hdrs.update(headers or {})
    if isinstance(body, str):
        body = body.encode("utf-8")
    sock = _connect(parts, net, timeout, deadline)
    held, fired = [sock], []

    def _expire():
        """The TOTAL deadline for connect-to-last-byte. A blocking recv never sees a per-read
        socket timeout when a server dribbles one byte at a time, so this closes the socket under
        it: shutdown wakes the blocked reader, which then fails."""
        fired.append(True)
        for s_ in list(held):
            for op in (lambda: s_.shutdown(socket.SHUT_RDWR), s_.close):
                try:
                    op()
                except (OSError, ValueError):
                    pass

    slow = _err("unreachable", f"{clean(parts.hostname)} answered too slowly")
    watchdog = threading.Timer(max(0.01, deadline - time.monotonic()), _expire)
    watchdog.daemon = True
    watchdog.start()
    try:
        if parts.scheme == "https":
            try:
                sock = ssl.create_default_context().wrap_socket(
                    sock, server_hostname=parts.hostname, do_handshake_on_connect=False)
                held[:] = [sock]
                sock.do_handshake()
            except (ssl.SSLError, OSError, ValueError):
                if fired:
                    raise slow
                raise _err("oauth_tls_failed", f"TLS verification failed for {clean(parts.hostname)}")
        cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        conn = cls(parts.hostname, parts.port, timeout=timeout)
        conn.sock = sock
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        try:
            conn.request(method, path, body=body, headers=hdrs)
            resp = conn.getresponse()
            chunks, total = [], 0
            while True:
                if fired or time.monotonic() > deadline:
                    raise slow
                chunk = resp.read(8192)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise _err("oauth_response_too_large",
                               f"the response from {safe_url(url)} is larger than {max_bytes} bytes")
                chunks.append(chunk)
            if fired:
                raise slow                       # a body cut short by the watchdog is not a body
            out = Response(resp.status, list(resp.getheaders()), b"".join(chunks))
        except GatewayError:
            raise
        except (OSError, http.client.HTTPException, ValueError):
            if fired:
                raise slow
            raise _err("unreachable", f"cannot read the answer of {clean(parts.hostname)}")
    finally:
        watchdog.cancel()
        try:
            sock.close()
        except OSError:
            pass
    if out.status in (301, 302, 303, 307, 308):
        loc = (out.header("Location") or [None])[0]
        if follow > 0 and method == "GET" and loc:
            nxt = urllib.parse.urljoin(url, loc)
            if urllib.parse.urlsplit(nxt).scheme in ("http", "https") and origin(nxt) == origin(url):
                return fetch(nxt, net, method=method, headers=headers, max_bytes=max_bytes,
                             timeout=max(0.05, deadline - time.monotonic()), follow=follow - 1,
                             what=what)
        raise _err("oauth_redirect_refused",
                   f"{safe_url(url)} redirected to another origin (or redirects are not allowed "
                   "here); refused")
    return out


# --------------------------------------------------------------------------- WWW-Authenticate

_PARAM = re.compile(r'\s*([A-Za-z0-9_.-]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,"]*))\s*(?:,|$)')


def parse_www_authenticate(values):
    """-> the auth-params of the first Bearer challenge in these header values ({} if none).
    Keys lowercased. Handles quoted strings with commas and several challenges per header."""
    for raw in values or []:
        for m in re.finditer(r"(?:^|,)\s*Bearer(?=\s|$)", raw, re.I):
            rest, out, pos = raw[m.end():], {}, 0
            while True:
                pm = _PARAM.match(rest, pos)
                if not pm:
                    break
                val = pm.group(2) if pm.group(2) is not None else pm.group(3)
                out[pm.group(1).lower()] = re.sub(r"\\(.)", r"\1", val)
                pos = pm.end()
            return out
    return {}


# --------------------------------------------------------------------------- discovery

def _initialize_probe():
    return json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "gt-lotr", "version": "0.3.0"}}})


def _as_candidates(issuer):
    p = urllib.parse.urlsplit(issuer)
    base = "%s://%s" % (p.scheme, p.netloc)
    path = p.path.rstrip("/")
    if path:
        return [base + "/.well-known/oauth-authorization-server" + path,
                base + "/.well-known/openid-configuration" + path,
                base + path + "/.well-known/openid-configuration"]
    return [base + "/.well-known/oauth-authorization-server",
            base + "/.well-known/openid-configuration"]


def _prm_candidates(endpoint):
    p = urllib.parse.urlsplit(endpoint)
    base = "%s://%s" % (p.scheme, p.netloc)
    path = p.path.rstrip("/")
    out = []
    if path:
        out.append(base + "/.well-known/oauth-protected-resource" + path)
    out.append(base + "/.well-known/oauth-protected-resource")
    return out


def _endpoint(net, doc, key, required=False):
    v = doc.get(key)
    if v is None:
        if required:
            raise _err("oauth_bad_metadata", f"the authorization server metadata has no {key}")
        return None
    if not isinstance(v, str):
        raise _err("oauth_bad_metadata", f"the authorization server metadata {key} is not a URL")
    net.check_url(v, key)
    if key == "authorization_endpoint":
        net.check_literal_host(v, key)               # the owner's browser goes there
    return v


def _same_issuer(listed, published):
    """Is `published` (the issuer in a metadata document, or an `iss`) the issuer `listed` (the
    identifier it was looked up by)? Exact string equality (RFC 8414 3.3), with ONE forgiveness, in
    ONE direction: a listed identifier that is a bare origin plus "/" matches the same origin
    published without it. Google's resource metadata lists `https://accounts.google.com/` while
    its metadata publishes `https://accounts.google.com`. The reverse (a document adding a slash),
    case differences, ports and paths are all mismatches."""
    if not isinstance(listed, str) or not isinstance(published, str):
        return False
    if listed == published:
        return True
    p = urllib.parse.urlsplit(listed)
    return p.path == "/" and not p.query and not p.fragment and listed[:-1] == published


_TENANT = "{tenantid}"


def _fetch_prm(fetcher, net, candidates, header_given):
    for url in candidates:
        r = fetcher(url, net, max_bytes=MAX_DISCOVERY_BYTES, follow=2, what="resource metadata URL")
        if r.status == 200:
            return r.json(), url
        if r.status not in (404, 410) and header_given:
            raise _err("oauth_discovery_failed", f"{safe_url(url)} answered HTTP {r.status}")
    return None, None


def discover(endpoint, net, *, authorization_server=None, fetcher=None, direct=False):
    """The protected-resource metadata -> authorization-server metadata chain (spec 2026-07-28
    'Authorization Server Discovery'). -> a plain dict (see keys at the return).

    `direct` (a pre-registered client was named): the RFC 9728 well-known document is fetched FIRST,
    without waiting for a 401 -- Google's servers answer `initialize` and `tools/list` with 200 and
    only challenge `tools/call`. Otherwise (or when it is not found) an unauthenticated `initialize`
    is sent; a 401's `resource_metadata` is used, and a 2xx answer falls back to the well-known
    document too (a server that needs no credentials AND publishes none is `oauth_not_required`)."""
    fetcher = fetcher or fetch
    net.check_url(endpoint, "MCP endpoint")
    resource = canonical_resource(endpoint)
    notes = []
    prm, prm_url, challenge = None, None, {}
    if direct:
        prm, prm_url = _fetch_prm(fetcher, net, _prm_candidates(endpoint), False)
    if prm is None:
        probe = fetcher(endpoint, net, method="POST", body=_initialize_probe(),
                        headers={"Content-Type": "application/json",
                                 "Accept": "application/json, text/event-stream"},
                        max_bytes=MAX_PROBE_BYTES, what="MCP endpoint")
        if 200 <= probe.status < 300:
            raise _err("oauth_not_required", f"{safe_url(endpoint)} answered without "
                       "credentials, so it does not use OAuth",
                       hints=["use `lotr add-mcp` for a server with its own token",
                              "a server that challenges only tool calls (Google's do) needs a "
                              "pre-registered client: pass --client-id"])
        if probe.status != 401:
            raise _err("oauth_probe_failed", f"{safe_url(endpoint)} answered HTTP {probe.status} to the "
                       "unauthenticated probe; expected 401 with a WWW-Authenticate challenge")
        challenge = parse_www_authenticate(probe.header("WWW-Authenticate"))
        if challenge.get("resource_metadata"):
            url = challenge["resource_metadata"]
            net.check_url(url, "resource_metadata URL")
            if origin(url) != origin(endpoint):
                raise _err("oauth_discovery_cross_origin",
                           "the server pointed its resource metadata at another origin "
                           f"({safe_url(url)}); refused")
            prm, prm_url = _fetch_prm(fetcher, net, [url], True)
        else:
            prm, prm_url = _fetch_prm(fetcher, net, _prm_candidates(endpoint), False)
            prm_url = None
    if prm is not None:
        got = prm.get("resource")
        try:
            same = isinstance(got, str) and canonical_resource(got) == resource
        except GatewayError:
            same = False
        if not same:
            raise _err("oauth_resource_mismatch",
                       "the protected-resource metadata names a different resource than the MCP "
                       "endpoint; refused (RFC 9728 section 3.3)")
        servers = prm.get("authorization_servers")
        if not isinstance(servers, list) or not servers or not all(isinstance(s, str) for s in servers):
            raise _err("oauth_bad_metadata", "the protected-resource metadata lists no "
                                             "authorization_servers")
        if authorization_server:
            match = [x for x in servers if _same_issuer(x, authorization_server)]
            if not match:
                raise _err("oauth_as_not_listed", "the authorization server you named is not one "
                           "the resource lists", hints=["listed: " + ", ".join(safe_url(x) for x in servers)])
            issuer = match[0]
        else:
            issuer = servers[0]
            if len(servers) > 1:
                notes.append("the resource lists %d authorization servers; using the first "
                             "(choose with --authorization-server)" % len(servers))
        scopes_supported = prm.get("scopes_supported") if isinstance(prm.get("scopes_supported"), list) else None
    else:
        p = urllib.parse.urlsplit(endpoint)
        issuer = authorization_server or "%s://%s" % (p.scheme, p.netloc)
        notes.append("the server publishes no protected-resource metadata (RFC 9728); fell back "
                     "to the endpoint's own origin as the authorization server (the pre-2025-06 "
                     "behaviour) -- check the issuer below")
        scopes_supported = None
    net.check_url(issuer, "authorization server issuer")
    if urllib.parse.urlsplit(issuer).query or urllib.parse.urlsplit(issuer).fragment:
        raise _err("oauth_bad_metadata", "an issuer identifier must not carry a query or fragment")
    fetch_issuer = issuer.rstrip("/") if urllib.parse.urlsplit(issuer).path in ("", "/") else issuer

    # -- RFC 8414 / OIDC discovery metadata
    meta, mismatch = None, False
    seg = [x for x in urllib.parse.urlsplit(fetch_issuer).path.split("/") if x][:1]
    for url in _as_candidates(fetch_issuer):
        r = fetcher(url, net, max_bytes=MAX_DISCOVERY_BYTES, follow=2, what="authorization server metadata URL")
        if r.status != 200:
            continue
        try:
            doc = r.json()
        except GatewayError:
            continue
        got = doc.get("issuer")
        ok = _same_issuer(issuer, got)
        if not ok and isinstance(got, str) and _TENANT in got and seg:
            # Entra publishes a TEMPLATE issuer (.../{tenantid}/v2.0); the tenant segment of the
            # authority the metadata was fetched for stands in for the placeholder, nothing else.
            ok = _same_issuer(got.replace(_TENANT, seg[0]), fetch_issuer)
            if ok:
                notes.append("Microsoft publishes a templated issuer; the tenant segment of the "
                             "authority was substituted for {tenantid}")
        if not ok:
            mismatch = True                      # MUST NOT use it (RFC 8414 section 3.3)
            continue
        meta = doc
        break
    if meta is None:
        if mismatch:
            raise _err("oauth_issuer_mismatch", "the authorization server metadata names a "
                       "different issuer than the one it was fetched for; refused")
        raise _err("oauth_discovery_failed", f"no authorization server metadata found for "
                   f"{safe_url(issuer)} (tried the RFC 8414 and OpenID Connect locations)")
    templated = _TENANT in meta["issuer"]
    recorded_issuer = meta["issuer"] if not templated else fetch_issuer
    auth_ep = _endpoint(net, meta, "authorization_endpoint", True)
    token_ep = _endpoint(net, meta, "token_endpoint", True)
    reg_ep = _endpoint(net, meta, "registration_endpoint")
    rev_ep = _endpoint(net, meta, "revocation_endpoint")
    methods = meta.get("code_challenge_methods_supported")
    pkce_unadvertised = methods is None
    if methods is not None and (not isinstance(methods, list) or "S256" not in methods):
        raise _err("oauth_pkce_unsupported", "the authorization server does not advertise PKCE "
                   "S256 (code_challenge_methods_supported); refusing to continue")
    rtypes = meta.get("response_types_supported")
    if isinstance(rtypes, list) and "code" not in rtypes:
        raise _err("oauth_bad_metadata", "the authorization server does not support the code flow")
    scopes = None
    if challenge.get("scope"):
        scopes = challenge["scope"].split()
    elif scopes_supported:
        scopes = [s for s in scopes_supported if isinstance(s, str) and s]
    am = meta.get("token_endpoint_auth_methods_supported")
    hosts = sorted({(urllib.parse.urlsplit(u).hostname or "").lower()
                    for u in (issuer, auth_ep, token_ep, reg_ep, rev_ep) if u})
    return {"resource": resource, "issuer": recorded_issuer, "authorization_endpoint": auth_ep,
            "token_endpoint": token_ep, "registration_endpoint": reg_ep,
            "revocation_endpoint": rev_ep, "scopes": scopes, "prm_url": prm_url,
            "iss_param_supported": meta.get("authorization_response_iss_parameter_supported") is True,
            "cimd_supported": meta.get("client_id_metadata_document_supported") is True,
            "pkce_unadvertised": pkce_unadvertised, "templated_issuer": templated,
            "auth_methods": [m for m in am if isinstance(m, str)] if isinstance(am, list) else None,
            "hosts": hosts, "notes": notes}


# --------------------------------------------------------------------------- token endpoint

def _client_auth(block, secret, form, headers):
    method = block.get("token_endpoint_auth_method") or "none"
    cid = block["client_id"]
    if method == "client_secret_basic" and secret:
        raw = "%s:%s" % (urllib.parse.quote(cid, safe=""), urllib.parse.quote(secret, safe=""))
        headers["Authorization"] = "Basic " + base64.b64encode(raw.encode("ascii")).decode("ascii")
    elif method == "client_secret_post" and secret:
        form["client_id"] = cid
        form["client_secret"] = secret
    else:
        form["client_id"] = cid


def token_request(block, secret, form, net):
    """POST a form to the token endpoint -> (status, json object or None, the Response). None when
    the body is not a JSON object; the Response is kept so a refusal can describe its shape."""
    form = dict(form)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    _client_auth(block, secret, form, headers)
    r = fetch(block["token_endpoint"], net, method="POST",
              body=urllib.parse.urlencode(form), headers=headers, max_bytes=MAX_TOKEN_BYTES,
              what="token endpoint")
    try:
        doc = r.json()
    except GatewayError:
        doc = None
    return r.status, doc, r


_RFC_TOKEN_ERRORS = frozenset({
    "invalid_request", "invalid_client", "invalid_grant", "unauthorized_client",
    "unsupported_grant_type", "invalid_scope", "access_denied", "unsupported_response_type",
    "server_error", "temporarily_unavailable", "invalid_token", "insufficient_scope",
    "invalid_target"})
_VISIBLE_TOKEN_CHAR = re.compile(r"[\x21\x23-\x5b\x5d-\x7e]")
_KEY_NAME = re.compile(r"[a-z_]{1,40}")


def _jtype(v):
    return {str: "string", bool: "boolean", type(None): "null", list: "array",
            dict: "object"}.get(type(v), "number")


def _char_classes(s):
    out = set()
    for c in s:
        o = ord(c)
        if "A" <= c <= "Z":
            out.add("upper")
        elif "a" <= c <= "z":
            out.add("lower")
        elif "0" <= c <= "9":
            out.add("digit")
        elif c in "-._~+/":
            out.add("b64-punct")
        elif c == "=":
            out.add("pad")
        elif c == " ":
            out.add("space")
        elif c in "\"\\":
            out.add("quote-or-backslash")
        elif 0x21 <= o <= 0x7E:
            out.add("other-printable-ascii")
        elif o < 0x20 or o == 0x7F:
            out.add("control")
        else:
            out.add("non-ascii")
    return sorted(out)


def _describe_token_value(name, v):
    """Redacted: length, character classes, and for characters outside the RFC 6749 visible set only
    their code points (a non-ASCII letter or digit is counted, never shown)."""
    if not isinstance(v, str):
        return f"{name}: JSON {_jtype(v)}"
    if not v:
        return f"{name}: empty string"
    parts = [f"{name}: string, {len(v)} chars", "classes " + ",".join(_char_classes(v))]
    if len(v) > MAX_TOKEN_CHARS:
        parts.append(f"over the {MAX_TOKEN_CHARS}-char cap")
    bad = [c for c in v if not _VISIBLE_TOKEN_CHAR.fullmatch(c)]
    if not bad:
        parts.append("every character is RFC 6749 visible")
    else:
        shown = ["U+%04X" % ord(c) for c in bad if not c.isalnum()][:20]
        n_alnum = sum(1 for c in bad if c.isalnum())
        parts.append(f"{len(bad)} char(s) outside RFC 6749 visible")
        if shown:
            parts.append("code points " + " ".join(shown))
        if n_alnum:
            parts.append(f"{n_alnum} of them non-ASCII letters/digits (values not shown)")
    return "; ".join(parts)


def _describe_error_value(name, v):
    if isinstance(v, str) and v in _RFC_TOKEN_ERRORS:
        return f"{name}={v}"
    if not isinstance(v, str):
        return f"{name}: JSON {_jtype(v)}"
    return f"{name}: string, {len(v)} chars, classes " + ",".join(_char_classes(v)) + \
        " (not an RFC 6749 error code; text not shown)"


def token_shape(resp):
    """A REDACTED description of a token endpoint answer, for the owner to read in a refusal: the
    HTTP status, the content type, the byte length, whether the body is JSON and its top-level keys
    with their JSON types, and for the token strings only length, character classes and the code
    points of anything outside the RFC 6749 visible set. Never a value (other than an RFC error code)."""
    ctype = (resp.header("Content-Type") or [""])[0].split(";")[0].strip().lower()[:60] or "none"
    lines = [f"token response: HTTP {resp.status}, content-type {ctype}, {len(resp.body)} bytes"]
    try:
        doc = json.loads(resp.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError, OverflowError, MemoryError):
        return lines + ["body does not parse as JSON"]
    if not isinstance(doc, dict):
        return lines + [f"body parses as JSON, top level is a JSON {_jtype(doc)} (not an object)"]
    lines.append(f"body parses as a JSON object with {len(doc)} top-level key(s)")
    for k in list(doc)[:20]:
        name = k if _KEY_NAME.fullmatch(k) else f"(non-standard key, {len(k)} chars)"
        v = doc[k]
        if k in ("access_token", "refresh_token"):
            lines.append("  " + _describe_token_value(name, v))
        elif k in ("error", "error_description"):
            lines.append("  " + _describe_error_value(name, v))
        else:
            lines.append(f"  {name}: JSON {_jtype(v)}")
    return lines


def _server_error(doc, code_when="oauth_token_refused"):
    """A fixed message plus the server's error CODE when it is a plain identifier (O3)."""
    e = doc.get("error")
    return echo_error(e)


def _b64url_json(seg):
    try:
        pad = "=" * (-len(seg) % 4)
        v = json.loads(base64.urlsafe_b64decode(seg + pad).decode("utf-8"))
        return v if isinstance(v, dict) else None
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None


def check_audience(access_token, resource):
    """Defence in depth, NOT authentication: when the access token is a JWT that carries `aud`,
    it must name this resource (spec: tokens are bound to the MCP server). An opaque token, or
    a JWT without `aud`, cannot be checked here -- the MCP server validates it."""
    segs = access_token.split(".")
    if len(segs) != 3:
        return
    claims = _b64url_json(segs[1])
    if not claims or "aud" not in claims:
        return
    aud = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    for a in aud:
        if isinstance(a, str):
            try:
                if canonical_resource(a) == canonical_resource(resource):
                    return
            except (GatewayError, ValueError, RecursionError):
                pass
            if a == resource:
                return
    raise _err("oauth_audience_mismatch", "the access token was issued for a different "
                                          "audience than this MCP server; refused")


def parse_tokens(status, doc, block, now=None, check_aud=True, refreshing=False, resp=None):
    """A token endpoint answer -> {access_token, expires_at, refresh_token|None, scope|None}.
    Raises needs_login on invalid_grant, oauth_token_refused on any other refusal. `resp` (the
    Response) lets a malformed answer carry its redacted shape in the error's hints."""
    def shape():
        return token_shape(resp) if resp is not None else []
    if doc is None:
        raise _err("oauth_bad_response", "a server answered with something that is not a JSON "
                                         "object", hints=shape())
    if status != 200:
        code = _server_error(doc)
        if code == "invalid_grant" and refreshing:
            raise _err("needs_login", "the sign-in is no longer valid (the authorization server "
                       "refused the refresh token)")
        raise _err("oauth_token_refused", f"the authorization server refused the token request "
                   f"(HTTP {status}, error {code})")
    at = doc.get("access_token")
    if not isinstance(at, str) or not at or len(at) > MAX_TOKEN_CHARS or not _VISIBLE_TOKEN.match(at):
        raise _err("oauth_bad_response", "the token response carries no usable access_token "
                                         "(RFC 6749 visible characters)", hints=shape())
    if str(doc.get("token_type", "")).lower() != "bearer":
        raise _err("oauth_bad_response", "the token response is not a Bearer token (DPoP and "
                                         "other token types are not supported)")
    exp = doc.get("expires_in")
    try:
        if isinstance(exp, bool) or not isinstance(exp, (int, float)) or exp <= 0 or exp != exp:
            exp = DEFAULT_EXPIRES_S
        else:
            exp = float(exp)                             # OverflowError for 10**400
    except (OverflowError, ValueError):
        exp = DEFAULT_EXPIRES_S
    rt = doc.get("refresh_token")
    if rt is not None and (not isinstance(rt, str) or not rt or len(rt) > MAX_TOKEN_CHARS
                           or not _VISIBLE_TOKEN.match(rt)):
        raise _err("oauth_bad_response", "the token response carries an unusable refresh_token "
                                         "(RFC 6749 visible characters)",
                   hints=shape())
    if check_aud:
        check_audience(at, block["resource"])
    sc = doc.get("scope")
    return {"access_token": at, "expires_at": (now if now is not None else clock()) + exp,
            "refresh_token": rt, "scope": sc if isinstance(sc, str) else None}


def refresh(block, refresh_token, client_secret, net=None):
    net = net or NetPolicy.from_block(block)
    form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    if block.get("send_resource", True):
        form["resource"] = block["resource"]
    status, doc, resp = token_request(block, client_secret, form, net)
    try:
        return parse_tokens(status, doc, block, check_aud=block.get("audience_check", True),
                            refreshing=True, resp=resp)
    except GatewayError as e:
        # A 200 that fails validation AFTER the server rotated (an unusable access token, a wrong
        # audience): the old refresh token is spent at the server, so the new one travels with the
        # error for the caller to persist (MAJOR 3 of the independent review).
        rt = doc.get("refresh_token") if status == 200 and isinstance(doc, dict) else None
        if isinstance(rt, str) and rt and len(rt) <= MAX_TOKEN_CHARS and rt != refresh_token:
            e.rotated_refresh_token = rt
        raise


def revoke(block, token, hint, client_secret, net=None):
    """RFC 7009. -> True (revoked), False (refused or failed), None (no revocation endpoint)."""
    ep = block.get("revocation_endpoint")
    if not ep:
        return None
    net = net or NetPolicy.from_block(block)
    form = {"token": token, "token_type_hint": hint}
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    _client_auth(block, client_secret, form, headers)
    try:
        r = fetch(ep, net, method="POST", body=urllib.parse.urlencode(form), headers=headers,
                  max_bytes=MAX_TOKEN_BYTES, what="revocation endpoint")
    except GatewayError:
        return False
    return r.status == 200


# --------------------------------------------------------------------------- client registration

def register_client(disc, net, redirect_uri, *, client_id=None, client_secret=None, cimd_url=None,
                    client_name="gt-lotr"):
    """-> {client_id, client_secret|None, source, token_endpoint_auth_method, notes}. Order
    (spec): a pre-registered id, then a Client ID Metadata Document, then dynamic registration."""
    notes = []
    methods = disc.get("auth_methods")
    if client_id:
        if client_secret:
            m = "client_secret_post" if (methods is None or "client_secret_post" in methods) \
                else "client_secret_basic"
        else:
            m = "none"
        if not client_secret and methods is not None and "none" not in methods:
            notes.append("this server does not list `none` among its token endpoint authentication "
                         "methods: a client without a secret may be refused (pass "
                         "--client-secret-file if the client has one)")
        return {"client_id": client_id, "client_secret": client_secret or None,
                "source": "preregistered", "token_endpoint_auth_method": m, "notes": notes}
    if cimd_url:
        p = urllib.parse.urlsplit(cimd_url)
        if p.scheme != "https" or not p.path or p.path == "/" or p.fragment or p.username:
            raise _err("oauth_bad_client_metadata_url", "a Client ID Metadata Document URL must be "
                       "https with a path component (for example https://you.example/lotr.json)")
        if disc.get("cimd_supported"):
            return {"client_id": cimd_url, "client_secret": None, "source": "cimd",
                    "token_endpoint_auth_method": "none", "notes": notes}
        notes.append("the authorization server does not advertise Client ID Metadata Documents; "
                     "trying dynamic registration")
    ep = disc.get("registration_endpoint")
    if not ep:
        raise _err("oauth_no_client_registration",
                   "this authorization server offers no way to register a client automatically "
                   "(no registration_endpoint, and no Client ID Metadata Document URL given)",
                   hints=["register an OAuth client with the service yourself, then run "
                          "`lotr connect NAME --url URL --client-id ID` (add --client-secret-file "
                          "PATH if it has a secret; use redirect URI "
                          "http://127.0.0.1:PORT/callback with --redirect-port PORT)"])
    body = {"client_name": client_name, "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none",
            "application_type": "native"}
    r = fetch(ep, net, method="POST", body=json.dumps(body),
              headers={"Content-Type": "application/json"}, max_bytes=MAX_TOKEN_BYTES,
              what="registration endpoint")
    if r.status not in (200, 201):
        raise _err("oauth_registration_refused", f"the authorization server refused dynamic "
                   f"client registration (HTTP {r.status})")
    doc = r.json()
    cid = doc.get("client_id")
    if not isinstance(cid, str) or not cid or len(cid) > 2048:
        raise _err("oauth_bad_response", "the registration response carries no client_id")
    ru = doc.get("redirect_uris")
    if ru is not None and ru != [redirect_uri]:
        raise _err("oauth_bad_response", "the registration response changed the redirect URI; "
                                         "refused")
    sec = doc.get("client_secret")
    if sec is not None and (not isinstance(sec, str) or not sec):
        sec = None
    method = doc.get("token_endpoint_auth_method")
    if sec:
        method = method if method in ("client_secret_basic", "client_secret_post") \
            else "client_secret_basic"
    else:
        method = "none"
    return {"client_id": cid, "client_secret": sec, "source": "dcr",
            "token_endpoint_auth_method": method, "notes": notes}


# --------------------------------------------------------------------------- loopback listener

_PAGE = (b"<!doctype html><meta charset=utf-8><title>gt-lotr</title>"
         b"<body style='font-family:sans-serif'><p>%s</p><p>You can close this tab.</p>")


class Loopback:
    """One-shot redirect receiver on 127.0.0.1 (O2). Use as a context manager; `wait` returns
    the authorization code and closes the socket whatever happens."""

    def __init__(self, port=0, host="127.0.0.1"):
        try:
            want = int(port or 0)
        except (TypeError, ValueError, OverflowError):
            want = -1
        if not 0 <= want <= 65535:
            raise _err("oauth_listen_failed", "the redirect port must be a number from 0 to 65535")
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._sock.bind(("127.0.0.1", want))
            self._sock.listen(4)
        except (OSError, OverflowError):
            self._sock.close()
            raise _err("oauth_listen_failed", "cannot listen on 127.0.0.1 for the sign-in "
                                              "redirect" + (f" (port {port} busy?)" if port else ""))
        self.port = self._sock.getsockname()[1]
        # The redirect URI may NAME localhost (Entra registers http://localhost) but the socket is
        # bound to 127.0.0.1 only, whatever the name; the string is exactly what is sent at the
        # authorization request and the token exchange.
        self.redirect_uri = "http://%s:%d%s" % (host, self.port, CALLBACK_PATH)
        self._hosts = {"127.0.0.1:%d" % self.port, "localhost:%d" % self.port}

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    @staticmethod
    def _answer(conn, status, text):
        body = _PAGE % text
        head = ("HTTP/1.1 %s\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: %d\r\n"
                "Cache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nConnection: close\r\n\r\n"
                % (status, len(body))).encode("ascii")
        try:
            conn.sendall(head + body)
        except OSError:
            pass

    @staticmethod
    def _read_request(conn, deadline):
        buf = b""
        while b"\r\n\r\n" not in buf:
            left = deadline - time.monotonic()
            if left <= 0 or len(buf) > 8192:
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

    def _judge(self, req, state, issuer, iss_required):
        """-> None (a stray request), or ("code", value), or (code, message) for a failure
        that ENDS the attempt."""
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
        if host not in self._hosts:                     # a DNS-rebinding page carries another Host
            return None
        u = urllib.parse.urlsplit(parts[1])
        if u.path != CALLBACK_PATH:
            return None
        q = urllib.parse.parse_qs(u.query, keep_blank_values=True)
        got = (q.get("state") or [""])[0]
        if not hmac.compare_digest(got.encode("utf-8", "replace"), state.encode("ascii")):
            return ("oauth_state_mismatch", "the sign-in answer did not carry this login's state")
        # RFC 9207 section 2.4, before the code goes anywhere (spec 2026-07-28 table).
        iss = q.get("iss")
        if iss is not None:
            if len(iss) != 1 or iss[0] != issuer:
                return ("oauth_issuer_mismatch", "the sign-in answer came from a different "
                                                 "authorization server than the one started")
        elif iss_required:
            return ("oauth_issuer_mismatch", "the authorization server advertises RFC 9207 but its "
                                             "answer carried no iss; refused")
        if q.get("error"):
            e = (q.get("error") or [""])[0]
            if e in ("access_denied", "user_cancelled"):
                return ("oauth_access_denied", "sign-in was cancelled or refused")
            return ("oauth_login_failed", "the authorization server refused the sign-in (error "
                                          f"{echo_error(e)})")
        code = q.get("code") or []
        if len(code) != 1 or not code[0]:
            return ("oauth_login_failed", "the sign-in answer carried no authorization code")
        return ("code", code[0])

    def wait(self, state, issuer, iss_required, timeout=LOGIN_TIMEOUT_S):
        """-> the authorization code. Each connection is read on its own short-lived thread, so an
        idle or slow socket (a browser's speculative connection, or an attacker's) cannot stall the
        accept loop. A request with the wrong Host, method, path OR STATE is a stray: answered and
        ignored (a forged callback must not be able to abort the owner's login), up to
        MAX_STRAYS. A state-valid answer that contradicts the recorded issuer, or that is an AS
        error, is terminal. The first terminal verdict wins, the socket closes at once, and any
        later request is refused."""
        import queue
        deadline = time.monotonic() + timeout
        results = queue.Queue()
        lock = threading.Lock()
        st = {"done": False, "strays": 0}

        def handle(conn):
            try:
                req = self._read_request(conn, time.monotonic() + READ_TIMEOUT_S)
                v = self._judge(req, state, issuer, iss_required)
                with lock:
                    if st["done"]:
                        self._answer(conn, "400 Bad Request", b"This sign-in already finished.")
                        return
                    if v is None or v[0] == "oauth_state_mismatch":
                        st["strays"] += 1
                        if st["strays"] > MAX_STRAYS:
                            st["done"] = True
                            results.put(("oauth_login_failed", "too many stray requests on the "
                                                               "sign-in listener"))
                        self._answer(conn, "404 Not Found" if v is None else "400 Bad Request",
                                     b"Not here." if v is None else b"Sign-in was not completed.")
                        return
                    st["done"] = True
                if v[0] == "code":
                    self._answer(conn, "200 OK", b"Signed in. You can return to the terminal.")
                else:
                    self._answer(conn, "400 Bad Request", b"Sign-in was not completed.")
                results.put(v)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

        try:
            while True:
                try:
                    v = results.get_nowait()
                except queue.Empty:
                    v = None
                if v is not None:
                    if v[0] == "code":
                        return v[1]
                    raise _err(v[0], v[1])
                left = deadline - time.monotonic()
                if left <= 0:
                    raise _err("oauth_login_timeout", f"nobody finished signing in within "
                                                      f"{int(timeout)} s")
                ready, _w, _x = select.select([self._sock], [], [], min(left, 0.1))
                if not ready:
                    continue
                try:
                    conn, _a = self._sock.accept()
                except OSError:
                    continue
                threading.Thread(target=handle, args=(conn,), daemon=True).start()
        finally:
            self.close()                                # single use: closed on every exit


def _entra_resource_ok(resource, scopes):
    """Entra v2 rejects a `resource` that does not match the audience of the requested scope
    (AADSTS9010010). Send it only when some URL-shaped scope's audience (the scope minus its last
    path segment) equals the resource or is a prefix of it, or the resource a prefix of it."""
    r = resource.rstrip("/")
    for sc in scopes:
        if "://" not in sc:
            continue
        aud = sc.rsplit("/", 1)[0].rstrip("/")
        if r == aud or r.startswith(aud + "/") or aud.startswith(r + "/"):
            return True
    return False


def _pkce():
    verifier = _stdsecrets.token_urlsafe(64)            # 86 characters, RFC 7636 43..128
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()) \
        .rstrip(b"=").decode("ascii")
    return verifier, challenge


def is_headless():
    """No way to show a browser here: Linux without a display. (macOS and Windows always have
    one; `webbrowser` reports the rest.)"""
    return sys.platform.startswith("linux") and not (os.environ.get("DISPLAY")
                                                      or os.environ.get("WAYLAND_DISPLAY"))


_URL_OK = re.compile(r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=]")


def browser_safe_url(url):
    """`url` with every character outside the RFC 3986 set percent-encoded as UTF-8 (a space,
    quote, ^ | < > backtick { } backslash, control or non-ASCII character); valid %XX escapes
    are kept as they are, so nothing is encoded twice."""
    out, i = [], 0
    while i < len(url):
        c = url[i]
        if c == "%" and re.match(r"[0-9A-Fa-f]{2}", url[i + 1:i + 3]):
            out.append(url[i:i + 3])
            i += 3
            continue
        out.append(c if _URL_OK.match(c) else "".join("%%%02X" % b for b in c.encode("utf-8", "replace")))
        i += 1
    return "".join(out)


def browser_allowed(force=False):
    """The ONE gate in front of `webbrowser`. GT_LOTR_NO_BROWSER=1 (set by every test and child
    process of gt's harness) always wins, `force` (the owner's explicit --open-browser) included.
    Otherwise a browser is opened only for a person: not inside a unittest / pytest run and with a
    terminal on stdin -- unless forced."""
    if os.environ.get("GT_LOTR_NO_BROWSER") == "1":
        return False
    if force:
        return True
    if "unittest" in sys.modules or "pytest" in sys.modules:
        return False
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (ValueError, OSError):
        return False


def default_opener(url, force=False):
    """Open `url` in the system browser, or return False (the caller prints it instead)."""
    if is_headless() or not browser_allowed(force):
        return False
    try:
        import webbrowser
        return bool(webbrowser.open(url, new=1))
    except Exception:                                   # noqa: BLE001
        return False


def login(endpoint, net, *, scope=None, extra_scope=None, client_id=None, client_secret=None,
          cimd_url=None, authorization_server=None, offline_access=False, redirect_port=0,
          opener=None, say=None, timeout=LOGIN_TIMEOUT_S, skip_audience_check=False,
          fetcher=None, confirm=None, send_resource=True, redirect_host="127.0.0.1", force_browser=False):
    """Run the whole authorization-code + PKCE login in the caller's terminal. -> dict:
      block   the registry `auth.oauth` block (no secret in it)
      tokens  {access_token, expires_at, refresh_token|None, scope}  (the caller seals/stores)
      client_secret  str|None (the caller stores it as a ref)
      hosts, notes
    """
    say = say or (lambda m: None)
    if redirect_host not in ("127.0.0.1", "localhost"):
        raise _err("oauth_bad_redirect_host", "--redirect-host must be 127.0.0.1 or localhost")
    disc = discover(endpoint, net, authorization_server=authorization_server, fetcher=fetcher,
                    direct=bool(client_id))
    if disc["pkce_unadvertised"] and not client_id:
        raise _err("oauth_pkce_unsupported", "the authorization server does not advertise PKCE "
                   "(code_challenge_methods_supported is absent); refusing to continue without a "
                   "pre-registered client you vouch for with --client-id")
    entra = disc["templated_issuer"] or any(
        (urllib.parse.urlsplit(u).hostname or "").endswith("login.microsoftonline.com")
        for u in (disc["issuer"], disc["authorization_endpoint"]))
    if entra and not offline_access:
        offline_access = True                     # Entra issues a refresh token only with it
    if scope:
        scopes = [s for s in scope.split() if s]
    else:
        scopes = list(disc["scopes"] or [])
        if not offline_access:                    # spec: a server SHOULD NOT list offline_access;
            scopes = [x for x in scopes if x != "offline_access"]   # the client asks for it itself
    for s in (extra_scope or []):
        if s not in scopes:
            scopes.append(s)
    if offline_access and "offline_access" not in scopes:
        scopes.append("offline_access")
    notes = list(disc["notes"])
    if disc["pkce_unadvertised"]:
        notes.append("the authorization server does not advertise PKCE methods; S256 is sent anyway "
                     "(pre-registered client)")
    use_resource = bool(send_resource)
    if use_resource and entra and not _entra_resource_ok(disc["resource"], scopes):
        use_resource = False
        notes.append("Microsoft rejects a `resource` that does not match the scope's audience "
                     "(AADSTS9010010); it is not sent for this sign-in")
    if not send_resource:
        notes.append("the `resource` parameter is not sent (--no-resource)")
    if confirm is not None:
        # The owner sees what is about to be asked, BEFORE a client is registered or a browser
        # opened (registration creates a record at the server).
        if not confirm({"endpoint": safe_url(endpoint), "resource": clean(disc["resource"]),
                        "issuer": clean(disc["issuer"]), "hosts": [clean(h) for h in disc["hosts"]],
                        "scopes": [clean(x, 80) for x in scopes], "notes": [clean(n, 400) for n in notes],
                        "client": ("pre-registered client " + clean(client_id)) if client_id else
                                  ("your Client ID Metadata Document" if cimd_url and disc["cimd_supported"]
                                   else "a new client registered automatically (dynamic client registration)"
                                   if disc["registration_endpoint"] else "no automatic registration")}):
            raise _err("oauth_declined", "the sign-in was not confirmed; nothing was registered")
    state = _stdsecrets.token_urlsafe(32)
    verifier, challenge = _pkce()
    with Loopback(redirect_port, redirect_host) as lb:
        reg = register_client(disc, net, lb.redirect_uri, client_id=client_id,
                              client_secret=client_secret, cimd_url=cimd_url)
        notes += reg["notes"]
        params = [("response_type", "code"), ("client_id", reg["client_id"]),
                  ("redirect_uri", lb.redirect_uri), ("state", state),
                  ("code_challenge", challenge), ("code_challenge_method", "S256")]
        if use_resource:
            params.append(("resource", disc["resource"]))
        if scopes:
            params.append(("scope", " ".join(scopes)))
        ap = urllib.parse.urlsplit(disc["authorization_endpoint"])
        query = (ap.query + "&" if ap.query else "") + urllib.parse.urlencode(params)
        url = urllib.parse.urlunsplit((ap.scheme, ap.netloc, ap.path, query, ""))
        net.check_literal_host(url, "authorization_endpoint")
        url = browser_safe_url(url)                     # inert in a terminal, a shell, a browser
        # The opener runs on its own thread: a browser launcher that BLOCKS until the page closes
        # (some BROWSER commands do) must not keep the listener from answering the redirect.
        outcome = []

        def _open():
            try:
                outcome.append(bool((opener or (default_opener if not force_browser else
                                                 (lambda u: default_opener(u, True))))(url)))
            except Exception:                           # noqa: BLE001
                outcome.append(False)
        threading.Thread(target=_open, daemon=True).start()
        for _ in range(40):                             # up to 2 s for a "could not open" answer
            if outcome:
                break
            time.sleep(0.05)
        opened = outcome[0] if outcome else True        # still running = a blocking launcher: opened
        if opened:
            say("Opened your browser to sign in. Waiting for the answer on %s ..." % lb.redirect_uri)
        else:
            say("No browser could be opened here. Open this URL in a browser ON THIS MACHINE "
                "(the answer comes back to 127.0.0.1 port %d; from another machine forward that "
                "port, and re-run with --redirect-port %d):\n\n  %s\n" % (lb.port, lb.port, url))
        code = lb.wait(state, disc["issuer"], disc["iss_param_supported"], timeout)
        redirect_uri = lb.redirect_uri
    block = {
        "issuer": disc["issuer"], "resource": disc["resource"],
        "authorization_endpoint": disc["authorization_endpoint"],
        "token_endpoint": disc["token_endpoint"],
        "revocation_endpoint": disc["revocation_endpoint"],
        "registration_endpoint": disc["registration_endpoint"],
        "client_id": reg["client_id"], "client_id_source": reg["source"],
        "token_endpoint_auth_method": reg["token_endpoint_auth_method"],
        "client_secret_ref": None, "scopes": scopes,
        "net": net.to_dict(), "spec": SPEC_REVISION,
        "audience_check": not skip_audience_check, "max_access_cache_s": MAX_ACCESS_CACHE_S,
        "send_resource": use_resource, "redirect_host": redirect_host,
        # A fresh value per sign-in: lotrd's cached tokens are keyed by the whole block, so a
        # disconnect + connect that yields an otherwise identical record (a pre-registered client)
        # can never be served the previous sign-in's access token.
        "login_nonce": _stdsecrets.token_hex(8),
    }
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "code_verifier": verifier}
    if use_resource:
        form["resource"] = disc["resource"]
    code = verifier = None
    status, doc, resp = token_request(block, reg["client_secret"], form, net)
    form = None
    tokens = parse_tokens(status, doc, block, check_aud=not skip_audience_check, resp=resp)
    doc = None
    if tokens["refresh_token"] is None:
        notes.append("the authorization server issued no refresh token: this sign-in lasts only "
                     "as long as the access token and lotrd's memory; `lotr login` renews it "
                     "(some servers issue one only with --offline-access)")
    return {"block": block, "tokens": tokens, "client_secret": reg["client_secret"],
            "hosts": disc["hosts"], "notes": notes}


# --------------------------------------------------------------------------- the token lifecycle

class Context:
    """What the lifecycle needs from its caller (lotrd): `resolve(ref)` for secrets, `write(ref,
    value)` to persist a rotated refresh token, and the `seat` the access token is cached for.
    A seat is the kernel-identified caller (or '-' while unlock is off): an access token minted
    for one seat is never handed to another, so gt unlock's per-seat grants keep their meaning."""

    def __init__(self, resolve, write=None, seat="-"):
        self.resolve, self.write, self.seat = resolve, write, seat


class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self.access = {}           # seat -> (token, expires_at)
        self.pending = {}          # seat -> (ref, value): a rotated token that could not be persisted
        self.notes = []


_STATES = {}
_STATES_LOCK = threading.Lock()


def _state_key(conn):
    auth = conn.get("auth") or {}
    blob = json.dumps([conn.get("endpoint"), auth.get("token_ref"), auth.get("oauth")],
                      sort_keys=True).encode("utf-8")
    return (conn["id"], hashlib.sha256(blob).hexdigest())


def _state(conn):
    key = _state_key(conn)
    with _STATES_LOCK:
        for k in [k for k in _STATES if k[0] == key[0] and k != key]:
            del _STATES[k]                              # the connection was re-registered
        st = _STATES.get(key)
        if st is None:
            st = _STATES[key] = _State()
        return st


def forget(conn_id=None):
    """Drop cached access tokens (all, or one connection's). lotrd calls it on reload."""
    with _STATES_LOCK:
        for k in [k for k in _STATES if conn_id is None or k[0] == conn_id]:
            del _STATES[k]


def prune(live_ids):
    """Forget the cached tokens of every connection no longer in the registry (lotrd calls it on
    each reload, so a `disconnect` done by the CLI takes effect in the daemon's memory too)."""
    live = set(live_ids)
    with _STATES_LOCK:
        for k in [k for k in _STATES if k[0] not in live]:
            del _STATES[k]


def login_command(conn_id, zone=None):
    z = " --zone %s" % zone if zone and zone != "personal" else ""
    return "lotr%s login %s" % (z, conn_id)


def needs_login(conn, why):
    return GatewayError("needs_login", f"{conn['id']}: {why}",
                        hints=["run: " + login_command(conn["id"], conn.get("zone")),
                               "this needs a person at a terminal; the gateway cannot sign in "
                               "by itself"])


# --------------------------------------------------------------------------- sealed-secret binding
#
# registry.json is a file any process of this user can edit, and lotrd (which CAN open a sealed
# secret) posts the refresh token to whatever token endpoint the record names. So a sealed secret
# is stored INSIDE an envelope that names the facts it was issued under; lotrd refuses to use one
# whose envelope does not match the record in front of it. An attacker who edits the record cannot
# re-make the envelope: that needs the token itself, which is what the seal protects.

BOUND_PREFIX = "lotr-bound-v1"
_BOUND_KEYS = ("issuer", "token_endpoint", "revocation_endpoint", "client_id", "resource")


def bind_digest(block):
    blob = json.dumps([block.get(k) for k in _BOUND_KEYS], separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def wrap_secret(block, ref, value):
    """What is written for an OAuth secret `ref`: the bound envelope, for sealed: AND store:. The
    L1 store: value is readable by any process of this user anyway, but a bare copy of it under
    another name must not be usable as a bearer token (second review, MINOR residual): the bearer
    paths refuse any envelope. A store: value written before this (plain) is read as it is and
    wrapped at its next refresh."""
    if (ref or "").startswith(("sealed:", "store:")):
        return "%s.%s.%s" % (BOUND_PREFIX, bind_digest(block), value)
    return value


def unwrap_secret(block, ref, raw):
    """The secret inside what was read for `ref`. A sealed: secret MUST be in an envelope whose
    digest matches this record (else `oauth_binding_mismatch`, and nothing is sent anywhere); an
    envelope found under any other ref is checked the same way."""
    head = BOUND_PREFIX + "."
    if isinstance(raw, str) and raw.startswith(head):
        digest, _, value = raw[len(head):].partition(".")
        if value and hmac.compare_digest(digest.encode("ascii", "replace"),
                                         bind_digest(block).encode("ascii")):
            return value
    elif not (ref or "").startswith("sealed:"):
        return raw
    raise GatewayError(
        "oauth_binding_mismatch",
        "the sealed sign-in secret was issued for a different issuer, token endpoint, client or "
        "resource than this connection's record now names; it is not used",
        hints=["the registry record was changed after sign-in (or the secret predates binding)",
               "sign in again: lotr disconnect <name>, then lotr connect <name> --url ..."])


_OAUTH_NAMED_REF = re.compile(r"^(sealed|store):lotr-oauth-")


def refuse_envelope(value, conn_id=None, ref=None):
    """`value`, unless it is a bound envelope: that is an OAuth sign-in's secret and never a token
    for the wire. The bearer paths call this, so a record edited away from scheme oauth (blocker
    1 of the independent review) cannot hand the sealed refresh token to the host it names.
    Third review: a PLAIN value under an OAuth-named ref (a legacy store: refresh token) is refused
    too, because the name alone says what it is; the value is never sent on a bearer path."""
    if isinstance(ref, str) and _OAUTH_NAMED_REF.match(ref):
        raise GatewayError("oauth_binding_mismatch",
                           f"{conn_id or 'the connection'}: the secret it points at is an OAuth "
                           "sign-in's (lotr-oauth-*), not a bearer token; it is not sent",
                           hints=["the registry record no longer says scheme oauth; sign in again "
                                  "with lotr connect, or point token_ref at the bearer token"])
    if isinstance(value, str) and value.startswith(BOUND_PREFIX + "."):
        raise GatewayError("oauth_binding_mismatch",
                           f"{conn_id or 'the connection'}: the stored secret is an OAuth sign-in's "
                           "sealed envelope, not a bearer token; it is not sent",
                           hints=["the registry record no longer says scheme oauth; sign in again "
                                  "with lotr connect, or restore the record"])
    return value


def access_token(conn, ctx, *, force=False):
    """A valid access token for `conn` and `ctx.seat`, refreshing (and re-persisting a rotated
    refresh token) as needed. Raises needs_login when the sign-in is gone."""
    auth = conn["auth"]
    block = auth["oauth"]
    try:                                         # re-checked at use, not only when the registry loads
        bound = canonical_resource(conn.get("endpoint") or "") == canonical_resource(block["resource"])
    except (GatewayError, KeyError, TypeError):
        bound = False
    if not bound:
        raise GatewayError("oauth_resource_mismatch", f"{conn['id']}: the endpoint is not the "
                           "resource this sign-in was issued for; no token is sent")
    st = _state(conn)
    net = NetPolicy.from_block(block)
    with st.lock:
        now = clock()
        hit = st.access.get(ctx.seat)
        if not force and hit and hit[1] - SKEW_S > now:
            return hit[0]
        st.access.pop(ctx.seat, None)
        ref = auth.get("token_ref")
        held = st.pending.get(ctx.seat)
        legacy = False                                   # a plain L1 value, wrapped below
        if held and held[0] == ref:
            rt = held[1]
        elif not ref:
            raise needs_login(conn, "no refresh token was issued at sign-in, and the access "
                                    "token has expired")
        else:
            stale = [sx for sx, h in st.pending.items() if h[0] == ref]
            if stale:
                # Another seat holds the newest refresh token in memory only: the stored copy is
                # known to be rotated away. Presenting it would make a server with reuse detection
                # (RFC 9700) revoke the whole family, so nothing is sent.
                raise needs_login(conn, "the refresh token was rotated but could not be re-stored "
                                        "by another caller; the stored copy is stale")
            if not _can_persist(ctx, ref):
                # A seat that cannot store the rotated token must not make the server rotate it
                # (the stored copy would die and the next refresh would look like a replay).
                raise GatewayError(
                    "oauth_unattended_refused",
                    f"{conn['id']}: this caller cannot re-store a rotated refresh token for "
                    f"{ref.split(':', 1)[0]}:, so it will not refresh with it",
                    hints=["an unattended hub client cannot use a sealed OAuth sign-in",
                           "connect it with --l1 (a mode-600 file) if unattended reads are wanted",
                           "or refresh from a local session of the owner"])
            try:
                raw = ctx.resolve(ref)
                legacy = ref.startswith("store:") and not (isinstance(raw, str)
                                                          and raw.startswith(BOUND_PREFIX + "."))
                rt = unwrap_secret(block, ref, raw)
            except GatewayError as e:
                if e.code == "secret_missing":
                    raise needs_login(conn, "the stored refresh token is gone")
                raise
        secret = None
        if block.get("client_secret_ref"):
            secret = unwrap_secret(block, block["client_secret_ref"],
                                   ctx.resolve(block["client_secret_ref"]))
        def _keep(new_rt):
            """The rotated refresh token into the store BEFORE anything else (O4); on ANY failure
            to store (a refusal, or a raw OSError: disk full, read-only home, a file locked by a
            scanner) it is kept in memory for this seat: the old one is spent at the server, and
            replaying it would get the whole grant revoked."""
            try:
                if ctx.write is None:
                    raise GatewayError("oauth_persist_failed", "no way to persist")
                ctx.write(ref, wrap_secret(block, ref, new_rt))
                for sx in [sx for sx, h in st.pending.items() if h[0] == ref]:
                    del st.pending[sx]                   # the store now holds the newest
                st.notes = []
            except (GatewayError, OSError) as e:
                st.pending[ctx.seat] = (ref, new_rt)
                st.notes = ["the refresh token rotated but could not be re-stored (%s); it is held "
                            "in lotrd's memory only, so `%s` is needed after a restart"
                            % (getattr(e, "code", None) or type(e).__name__,
                               login_command(conn["id"], conn.get("zone")))]
        try:
            tok = refresh(block, rt, secret, net)
        except GatewayError as e:
            if e.code == "needs_login":
                st.pending.pop(ctx.seat, None)           # only THIS seat's held token
                raise needs_login(conn, "the sign-in expired or was revoked")
            rotated = getattr(e, "rotated_refresh_token", None)
            if rotated and rotated != rt:
                _keep(rotated)                           # the server rotated, then the reply failed
            raise
        finally:
            secret = None
        new_rt = tok["refresh_token"]
        if new_rt and new_rt != rt:                      # rotation (O4): persist BEFORE use
            _keep(new_rt)
        elif held and ctx.write is not None:
            try:                                         # a held token: try to store it again
                ctx.write(held[0], wrap_secret(block, held[0], held[1]))
                st.pending.pop(ctx.seat, None)
                st.notes = []
            except (GatewayError, OSError):
                pass
        elif legacy and ctx.write is not None:
            try:                                         # migrate a pre-envelope L1 value (no rotation)
                ctx.write(ref, wrap_secret(block, ref, rt))
            except (GatewayError, OSError):
                pass                                     # still plain; the next refresh tries again
        cap = float(block.get("max_access_cache_s") or MAX_ACCESS_CACHE_S)
        t = clock()
        for sx in [sx for sx, (_tk, ex) in st.access.items() if ex - SKEW_S <= t]:
            del st.access[sx]                            # expired entries do not accumulate
        while len(st.access) >= MAX_SEATS:
            del st.access[next(iter(st.access))]         # oldest seat first
        st.access[ctx.seat] = (tok["access_token"], min(tok["expires_at"], t + cap))
        return tok["access_token"]


def _can_persist(ctx, ref):
    """Can THIS caller store a rotated refresh token for `ref`? A writer may say (`.can(ref)`);
    one that does not is taken at its word."""
    if ctx.write is None:
        return False
    can = getattr(ctx.write, "can", None)
    return bool(can(ref)) if can else True


def invalidate(conn, seat):
    st = _state(conn)
    with st.lock:
        st.access.pop(seat, None)


def notes_for(conn):
    return list(_state(conn).notes)


def status_of(conn):
    """For `lotr status` in lotrd: what is held in memory (never a value)."""
    st = _state(conn)
    out = {"access_cached": bool(st.access), "refresh_unpersisted": bool(st.pending)}
    ref = (conn.get("auth") or {}).get("token_ref") or ""
    if ref.startswith("store:"):
        legacy = _store_value_is_legacy(ref)
        out["legacy_store_value"] = legacy
        if legacy:
            out["notes"] = ["the refresh token is held in the pre-envelope form; it is wrapped at its "
                            "next refresh (or run `lotr login NAME` to wrap it now)"]
    return out


def _store_value_is_legacy(ref):
    """Is the L1 value for `ref` a plain token (no envelope)? Read here, never returned."""
    from . import secrets as secrets_mod
    try:
        v = secrets_mod.resolve(ref)
    except (GatewayError, OSError):
        return False
    return not (isinstance(v, str) and v.startswith(BOUND_PREFIX + "."))


# --------------------------------------------------------------------------- where tokens rest

def secret_name(conn_id, kind="refresh"):
    """A name usable as a sealed: / store: target (one plain segment, <= 64 chars)."""
    plain = re.sub(r"[^A-Za-z0-9]+", "-", conn_id).strip("-")[:30] or "conn"
    tag = hashlib.sha256(conn_id.encode("utf-8")).hexdigest()[:8]
    return "lotr-oauth-%s-%s-%s" % (plain, tag, "rt" if kind == "refresh" else "cs")


def write_store_secret(name, value, store_dir=None):
    """Write `value` to the L1 store file store:<name> (mode 600, atomic rename; the directory
    is created mode 700). The value never goes through argv."""
    from . import secrets as secrets_mod
    from .util import atomic_write
    d = secrets_mod._store_dir(store_dir)
    # Created 0700 in ONE step (mkdir's mode, which a umask can only narrow): never a moment in
    # which another user could open the new directory. An existing loose one is tightened.
    d.parent.mkdir(parents=True, exist_ok=True)
    d.mkdir(mode=0o700, exist_ok=True)
    try:
        if os.name != "nt":
            os.chmod(d, 0o700)
    except OSError:
        pass
    path = secrets_mod._file_path("store", name, store_dir)
    atomic_write(path, value, 0o600)
    return "store:" + name


def delete_store_secret(name, store_dir=None):
    from . import secrets as secrets_mod
    path = secrets_mod._file_path("store", name, store_dir)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def make_writer(unlock_mod, subject=None, store_dir=None):
    """The `write(ref, value)` the lifecycle uses to persist a rotated refresh token: a sealed:
    ref goes to the authority's seal_put on behalf of `subject` (a public-key seal on macOS: no
    prompt; Windows asks Hello), a store: ref to an atomic mode-600 file."""
    def write(ref, value):
        scheme, _, name = (ref or "").partition(":")
        if scheme == "store":
            try:
                write_store_secret(name, value, store_dir)
            except OSError as e:                         # never a raw OSError out of the writer
                raise GatewayError("oauth_persist_failed",
                                   "could not write the store file (%s)" % type(e).__name__)
            return
        if scheme == "sealed":
            if subject is None:
                raise GatewayError("oauth_persist_failed", "no identified caller to seal for")
            unlock_mod.call("seal_put", {"name": name, "value": value, "request": False,
                                         "subject": {"pid": subject["pid"], "start": subject["start"]}},
                            start=False)
            return
        raise GatewayError("oauth_persist_failed", f"cannot write a {scheme or 'unknown'}: ref")

    def can(ref):
        scheme = (ref or "").partition(":")[0]
        return scheme == "store" or (scheme == "sealed" and subject is not None)
    write.can = can
    return write


def seed_access(conn, seat, token, expires_at):
    """Hand a freshly minted access token to this process's cache (the CLI uses it to list the
    server's tools straight after sign-in, so no second refresh is spent)."""
    st = _state(conn)
    with st.lock:
        st.access[seat] = (token, expires_at)
