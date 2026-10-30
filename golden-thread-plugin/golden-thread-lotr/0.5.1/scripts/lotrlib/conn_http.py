"""HTTP connections: one call against a REST API, with auth, host allow-list and paging.

The credential is resolved at request time, placed on the outgoing request only, and
stripped from it again before anything can be raised; it is never stored on the
connection object and never appears in an exception. Every URL the gateway requests --
the first one, a redirect target, a next-page link, a cursor -- is checked against the
connection's `network.hosts`.
"""
import base64
import binascii
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

from . import upstream
from .errors import GatewayError
from .shaping import scan_credentials

USER_AGENT = "gt-lotr/0.4.0"
QUERY_METHODS = ("GET", "HEAD", "DELETE")
MAX_BODY_BYTES = 16 * 1024 * 1024
ERROR_MESSAGE_CHARS = 300
DRIFT_HINT = "operation may have moved; run lotr find again"
_PARAM_RX = re.compile(r"\{([A-Za-z0-9_.-]+)\}")
_LINK_RX = re.compile(r'<([^>]*)>\s*((?:;\s*[^;,]*)*)')
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")          # registry.LOCAL_HOSTS: where plain http may go
CREDENTIAL_HEADERS = ("authorization", "proxy-authorization", "cookie")


def _encode_cursor(url):
    raw = json.dumps({"u": url}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor):
    try:
        s = str(cursor).strip()
        s += "=" * (-len(s) % 4)
        obj = json.loads(base64.urlsafe_b64decode(s.encode("ascii")).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeError, TypeError):
        raise GatewayError("bad_cursor", "the cursor is not one this gateway issued",
                           ["call again without a cursor to start from the first page"])
    if not isinstance(obj, dict) or not isinstance(obj.get("u"), str):
        raise GatewayError("bad_cursor", "the cursor is not one this gateway issued",
                           ["call again without a cursor to start from the first page"])
    return obj["u"]


def _query_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, dict):
        return json.dumps(v)
    return v


def _encode_query(q):
    pairs = []
    for k, v in q.items():
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            pairs.extend((k, _query_value(x)) for x in v if x is not None)
        else:
            pairs.append((k, _query_value(v)))
    # '$' stays literal for Graph's $select/$top; ',' stays literal for Jira's fields=a,b.
    return urllib.parse.urlencode(pairs, safe="$,")


def _with_param(url, name, value):
    """`url` with query parameter `name` set to `value` (replacing any existing)."""
    parts = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
         if k != name]
    q.append((name, str(value)))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q, safe="$,")))


def _parse_link_next(header):
    if not header:
        return None
    for m in _LINK_RX.finditer(header):
        url, params = m.group(1), m.group(2)
        for p in params.split(";"):
            k, _, v = p.strip().partition("=")
            if k.strip().lower() == "rel" and "next" in v.strip().strip('"').split():
                return url
    return None


def _error_message(text, data):
    """The most useful human message in an error body."""
    if isinstance(data, dict):
        for key in ("message", "error_description", "errorMessage"):
            if isinstance(data.get(key), str):
                return data[key]
        err = data.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return err["message"]
        if isinstance(err, str):
            return err
        msgs = data.get("errorMessages")
        if isinstance(msgs, list) and msgs:
            extra = data.get("errors")
            out = "; ".join(str(m) for m in msgs)
            if isinstance(extra, dict) and extra:
                out += "; " + "; ".join(f"{k}: {v}" for k, v in extra.items())
            return out
        if isinstance(data.get("errors"), dict) and data["errors"]:
            return "; ".join(f"{k}: {v}" for k, v in data["errors"].items())
    return text or ""


def _origin(url):
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        port = -1
    return scheme, (parts.hostname or "").lower(), port or {"http": 80, "https": 443}.get(scheme)


class _GuardedRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to an allowed host, never from https down to http, and over plain
    http only to loopback. urllib copies every header to the new request, so a redirect to another
    origin (scheme, host or port) drops the credential headers (Authorization, the connection's own
    auth header, Cookie); the same origin keeps them."""

    def __init__(self, check, cred_headers=()):
        super().__init__()
        self._check = check
        self._cred = set(CREDENTIAL_HEADERS) | {str(h).lower() for h in cred_headers if h}

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        self._check(target)
        old, new = _origin(req.full_url), _origin(target)
        if old[0] == "https" and new[0] != "https":
            raise GatewayError("redirect_refused", f"{old[1]} redirected from https to plain "
                               f"{new[0]}; the call is refused so nothing travels unencrypted",
                               ["the endpoint must stay on https; fix its redirect, or point "
                                "base_url at the final https address"])
        if new[0] == "http" and new[1] not in LOOPBACK_HOSTS:
            raise GatewayError("redirect_refused", f"a redirect to plain http on {new[1]} is "
                               "refused; plain http stays on this machine",
                               ["point base_url at the final https address"])
        nreq = super().redirect_request(req, fp, code, msg, headers, newurl)
        if nreq is not None and new != old:
            for k in list(nreq.headers):
                if k.lower() in self._cred:
                    del nreq.headers[k]
            for k in list(nreq.unredirected_hdrs):
                if k.lower() in self._cred:
                    del nreq.unredirected_hdrs[k]
        return nreq


class HttpConnection:
    def __init__(self, conn, profile, *, secret_resolver=None, timeout=20, ca_pem=None):
        if secret_resolver is None:
            from .secrets import resolve as secret_resolver   # lazy: optional at import time
        self.conn = conn
        self.profile = profile or {}
        self.timeout = timeout
        call_s = (conn.get("timeouts") or {}).get("call_s")       # connect.timeouts.call_s (validated)
        if upstream._number(call_s) and not upstream.timeouts_problem(conn["timeouts"]):
            self.timeout = call_s
        self._resolve = secret_resolver
        self.base_url = str(conn.get("base_url", "")).rstrip("/")
        base = urllib.parse.urlsplit(self.base_url)
        self._base_scheme = base.scheme.lower()
        self._base_prefix = base.path.rstrip("/")
        self.hosts = [str(h).lower() for h in (conn.get("network") or {}).get("hosts", [])]
        self._handlers = []
        if conn.get("trust_ca"):
            # 0.5.1: the owner's private CA is this connection's only trust anchor, and no proxy
            # sits in the way. A connection built without the verified CA text fails closed rather
            # than falling back to the system store (the engine passes it after checking its sha256).
            if not ca_pem:
                raise GatewayError("tls_ca_missing", f"{conn.get('id')}: its private CA was not "
                                   "resolved, so the connection is refused")
            from . import trustca
            self._handlers = [urllib.request.ProxyHandler({}),
                              trustca.https_handler(ca_pem, conn.get("id"))]

    def __repr__(self):
        return f"<HttpConnection {self.conn.get('id')} {self.base_url}>"

    # -- guards ----------------------------------------------------------------------
    def _check_host(self, url):
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme.lower() not in ("http", "https") or host not in self.hosts:
            raise GatewayError(
                "host_not_allowed",
                f"{host or url[:80]!s} is not in network.hosts of {self.conn.get('id')}",
                [f"allowed hosts: {', '.join(self.hosts) or '(none)'}"])

    def _check_cursor_url(self, url):
        """Normalise and check a next-page target; returns its path relative to the base URL.

        Never a string-prefix test: the path is split into segments (percent-decoding until
        stable) and any `.`/`..`, empty (`//`), backslash or control character is refused, as is
        userinfo, another scheme, host, or port. What is left is provably under the base URL.
        """
        bad = GatewayError("bad_cursor",
                           f"the cursor does not point under {self.conn.get('id')}'s base URL",
                           ["call again without a cursor to start from the first page"])
        try:
            parts = urllib.parse.urlsplit(url)
            port = parts.port
        except ValueError:
            raise bad
        host = (parts.hostname or "").lower()
        base = urllib.parse.urlsplit(self.base_url)
        default = {"http": 80, "https": 443}
        if (parts.scheme.lower() != self._base_scheme or host not in self.hosts
                or "@" in parts.netloc or (port or default.get(parts.scheme.lower())) !=
                (base.port or default.get(self._base_scheme))):
            raise bad
        raw = parts.path or "/"
        text = raw
        for _ in range(4):
            nxt = urllib.parse.unquote(text)
            if nxt == text:
                break
            text = nxt
        if "\\" in text or any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise bad
        segs = text.split("/")[1:] if text.startswith("/") else None
        if segs is None or any(s in (".", "..") for s in segs):
            raise bad
        if any(s == "" for s in segs[:-1]):
            raise bad
        pre = [s for s in self._base_prefix.split("/") if s]
        if segs[:len(pre)] != pre:
            raise bad
        rel = "/" + "/".join(segs[len(pre):])
        return rel

    def cursor_target(self, cursor):
        """The relative path a cursor would fetch (raises bad_cursor), for the policy check."""
        return self._check_cursor_url(_decode_cursor(cursor))

    # -- request building --------------------------------------------------------------
    def _build(self, op, args):
        method = str(op.get("method", "GET")).upper()
        path = str(op.get("path", "/"))
        args = dict(args or {})

        def fill(m):
            name = m.group(1)
            if args.get(name) is None:
                raise GatewayError(
                    "missing_param", f"{op.get('name') or path} needs '{name}'",
                    [f"params: {', '.join(sorted((op.get('params') or {}).keys())) or name}"])
            return urllib.parse.quote(str(args.pop(name)), safe="")

        path = _PARAM_RX.sub(fill, path)
        if not path.startswith("/"):
            path = "/" + path

        explicit_query = args.pop("query") if isinstance(args.get("query"), dict) else None
        has_body = "body" in args
        explicit_body = args.pop("body") if has_body else None

        query = dict(op.get("query_defaults") or {})
        body = None
        if method in QUERY_METHODS:
            query.update(args)
            if has_body:
                body = explicit_body
        else:
            if has_body:
                body = explicit_body
                if args:
                    if isinstance(body, dict):
                        body = {**args, **body}
                    else:
                        query.update(args)
            elif args:
                body = args
        if explicit_query:
            query.update(explicit_query)

        url = self.base_url + path
        qs = _encode_query(query)
        if qs:
            url += ("&" if "?" in url else "?") + qs
        return method, url, body

    # -- the request -----------------------------------------------------------------
    def _auth_header(self):
        """(header name, value) or None. Resolved now, returned to the caller, kept nowhere."""
        auth = self.conn.get("auth") or {}
        scheme = auth.get("scheme", "none")
        if scheme == "none":
            return None
        ref = auth.get("token_ref")
        if not ref:
            raise GatewayError("auth_invalid", f"{self.conn.get('id')} has no auth.token_ref")
        from .oauth import refuse_envelope
        token = refuse_envelope(self._resolve(ref), self.conn.get("id"), ref=ref)   # never a sealed envelope
        if scheme == "bearer":
            return "Authorization", "Bearer " + token
        if scheme == "basic":
            pair = f"{auth.get('user') or ''}:{token}".encode("utf-8")
            return "Authorization", "Basic " + base64.b64encode(pair).decode("ascii")
        if scheme == "header":
            if not auth.get("header"):
                raise GatewayError("auth_invalid",
                                   f"{self.conn.get('id')} uses scheme header but names no auth.header")
            return auth["header"], token
        raise GatewayError("auth_invalid", f"unknown auth scheme {scheme!r} on {self.conn.get('id')}")

    def _effective_timeout(self, timeout_s=None):
        """Seconds for this call: the connection's own, or a per-call value held to [1, ceiling]."""
        if timeout_s is None:
            return self.timeout
        try:
            return upstream.clamp_timeout(timeout_s, upstream.HARD_CALL_TIMEOUT_S)
        except ValueError:
            return self.timeout

    def _request(self, method, url, body, op_name, timeout_s=None):
        timeout = self._effective_timeout(timeout_s)
        self._check_host(url)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        for k, v in (self.conn.get("headers") or {}).items():
            headers[str(k)] = str(v)
        data = None
        if body is not None:
            if isinstance(body, (bytes, bytearray)):
                data = bytes(body)
            elif isinstance(body, str):
                data = body.encode("utf-8")
            else:
                data = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        auth = self._auth_header()
        if auth:
            req.add_header(auth[0], auth[1])
        auth = None
        cred = ((self.conn.get("auth") or {}).get("header"),)
        opener = urllib.request.build_opener(_GuardedRedirects(self._check_host, cred), *self._handlers)
        status, resp_headers, raw, err = None, {}, b"", None
        try:
            try:
                with opener.open(req, timeout=timeout) as resp:
                    status = resp.status
                    resp_headers = resp.headers
                    raw = resp.read(MAX_BODY_BYTES) if method != "HEAD" else b""
            except urllib.error.HTTPError as e:
                status = e.code
                resp_headers = e.headers
                try:
                    raw = e.read(MAX_BODY_BYTES)
                except Exception:
                    raw = b""
                finally:
                    e.close()
            except GatewayError as e:          # a redirect refused (host, https->http, off loopback)
                err = e
            except (socket.timeout, TimeoutError):
                err = GatewayError("network_error",
                                   f"{method} {urllib.parse.urlsplit(url).hostname}: "
                                   f"timed out after {timeout:g}s")
            except (urllib.error.URLError, OSError, ValueError) as e:
                reason = getattr(e, "reason", None) or e
                tls = None
                if self.conn.get("trust_ca"):
                    from . import trustca
                    tls = trustca.tls_error(e, self.conn)
                err = tls or GatewayError("network_error",
                                   f"{method} {urllib.parse.urlsplit(url).hostname}: "
                                   f"{type(reason).__name__}: {str(reason)[:200]}")
        finally:
            # Strip the credential off the request before anything can be raised from here.
            req.headers = {}
            req.unredirected_hdrs = {}
            del req
        if err is not None:
            if scan_credentials(err.message):
                err.message = "the error text was withheld: it contained credential-shaped text"
            raise err

        text = raw.decode("utf-8", errors="replace") if raw else ""
        parsed = None
        if text.strip():
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = text

        if status is not None and status >= 400:
            msg = _error_message(text, parsed if isinstance(parsed, (dict, list)) else None)
            found = scan_credentials(msg)
            if found:
                rules = ", ".join(sorted({f["rule"] for f in found}))
                msg = (f"the error body was withheld: it contained credential-shaped text "
                       f"({rules})")
            else:
                msg = msg.strip()
                if len(msg) > ERROR_MESSAGE_CHARS:
                    msg = msg[:ERROR_MESSAGE_CHARS - 1] + "…"
                if not msg:
                    msg = f"HTTP {status}"
            hints = []
            if status == 404 and op_name:
                hints.append(DRIFT_HINT)
            elif status in (401, 403):
                hints.append(f"check the credential behind {self.conn.get('id')} "
                             f"({(self.conn.get('auth') or {}).get('token_ref')}) and its scopes")
            retry = upstream.parse_retry_after(resp_headers.get("Retry-After")
                                               if resp_headers is not None else None)
            if status == 429:
                hints.append("rate limited by the provider; the gateway does not retry, so wait "
                             + (f"{retry}s and " if retry is not None else "and ") + "try again")
            mapped = upstream.from_status(status, msg, retry, hints)
            if mapped is not None:                  # the status and Retry-After are kept (upstream.py)
                raise mapped
            raise GatewayError(f"http_{status}", msg, hints)
        return status, resp_headers, parsed

    # -- pagination --------------------------------------------------------------------
    def _next_url(self, url, headers, data):
        style = self.profile.get("pagination")
        nxt = None
        if style == "link-header":
            nxt = _parse_link_next(headers.get("Link") if headers is not None else None)
        elif style == "odata":
            if isinstance(data, dict) and isinstance(data.get("@odata.nextLink"), str):
                nxt = data["@odata.nextLink"]
        elif style == "jira-token":
            if isinstance(data, dict) and data.get("nextPageToken") and not data.get("isLast"):
                nxt = _with_param(url, "nextPageToken", data["nextPageToken"])
        elif style == "jira-startat":
            if isinstance(data, dict):
                try:
                    start = int(data.get("startAt", 0))
                    size = int(data.get("maxResults", 0))
                    total = int(data.get("total"))
                except (TypeError, ValueError):
                    size, total, start = 0, 0, 0
                if size > 0 and start + size < total:
                    nxt = _with_param(url, "startAt", start + size)
        if not nxt:
            return None
        nxt = urllib.parse.urljoin(url, nxt)
        self._check_host(nxt)                  # an off-host next link is refused, loudly
        return nxt

    # -- public ------------------------------------------------------------------------
    def call(self, op, args, *, cursor=None, timeout_s=None):
        """Run one operation. Returns {"status", "data", "next_cursor"}. `timeout_s` is this
        call's own limit (the engine has already held it to the gateway ceiling)."""
        if cursor:
            url = _decode_cursor(cursor)
            self._check_cursor_url(url)
            method, body = "GET", None
        else:
            method, url, body = self._build(op, args)
        status, headers, data = self._request(method, url, body, op.get("name"), timeout_s)
        out = {"status": status, "data": data, "next_cursor": None}
        try:
            nxt = self._next_url(url, headers, data)
        except GatewayError as e:
            # Page 1 was fetched from an allowed host and is good. Only the NEXT link points
            # somewhere the registry does not allow: keep the page, withhold the cursor, say why.
            out["notes"] = [f"next page withheld: {e.message}"]
            return out
        out["next_cursor"] = _encode_cursor(nxt) if nxt else None
        return out
