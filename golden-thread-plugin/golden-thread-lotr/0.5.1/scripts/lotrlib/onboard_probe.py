"""`lotr onboard`, step 1: which connection template fits this MCP? (gt-lotr 0.5.1, plan phase 37)

    probe(target, net=None) -> {"status", "best", "candidates", "observed", "requests", "evidence", "note", "kind"}

`target` is a hosted MCP URL, or a launcher line (`npx ...`, `uvx ...`, `docker run ...`) or a binary download
URL. The answer names one of the nine template archetypes (`lotr template guide`) with the evidence, or
`no_template_fits` -- never a guess.

  * A HOSTED server is classified from what it answers, in at most three requests:
      POST initialize, no credential        401/403 + OAuth discovery        -> mcp-oauth (a registration
                                                                                endpoint) or
                                                                                mcp-oauth-preregistered
                                            401/403 + a bearer challenge, no OAuth metadata
                                                                              -> mcp-http-key, checked by
                                                                                ONE request carrying the fake
                                                                                credential
                                            a JSON-RPC answer                 -> mcp-http-key, scheme none
                                            405/404 then GET text/event-stream
                                            with an `endpoint` event           -> sse-bridge
      anything else                                                            -> no_template_fits
    The fake credential is sent only to a key-style server, where its rejection shows the gate is where the
    template will put the key. For an OAuth server it tells nothing (the server answers the same with or
    without it; measured in phase 36), so none is sent there. A real credential is never sent: the probe has
    no way to read one.
  * A PACKAGE is classified from the launcher and the package NAME alone. Nothing is started, installed or
    fetched: the first run of a package is `lotr setup`, in the user's own terminal.

Every request goes through oauth.fetch, so the owner's network policy applies: https only (plain http only to
a loopback host the owner allowed), no private, link-local or metadata address, no proxy, no redirect, bounded
size and time. The result holds status codes and header NAMES, never a header value, and never a credential-
shaped string from the caller's input.
"""
import json
import re
import shlex
import urllib.parse

from . import oauth, shaping
from .errors import GatewayError

FAKE_CREDENTIAL = "gt-probe-not-a-credential"
MAX_REQUESTS = 3
PROBE_TIMEOUT_S = 6
SSE_TIMEOUT_S = 3
MAX_BYTES = 65536
MAX_SCOPES = 20

NO_FIT_NOTE = ("No MCP answer was found. If this is a REST API, fill the http-rest archetype from its "
               "documentation (lotr template new NAME --archetype http-rest). For a program, name the launcher: "
               "npx, uvx, docker run, or a binary download URL.")

_SHELL = re.compile(r"[;|&<>$`\n\r(){}\\]")
_NPM = re.compile(r"^(@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*(@[A-Za-z0-9._^~<>=*+-]+)?$")
_UVX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*((==|@)[A-Za-z0-9.+!_-]+)?$")
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?(@sha256:[0-9a-f]{64})?$")
_BINARY_EXT = (".tar.gz", ".tgz", ".zip", ".exe", ".bin", ".tar.xz", ".tar.bz2", ".gz")
_BIN_VERSIONED = re.compile(r"(/releases/download/|/v?\d+(\.\d+)+[/._-])")
_DOCKER_VALUE_FLAGS = {"-e", "--env", "-v", "--volume", "--name", "-p", "--publish", "--network", "-w", "--workdir",
                       "-u", "--user", "--entrypoint", "--mount", "--platform", "--env-file", "-l", "--label"}


def _result(kind, status="ok", best=None, candidates=None, observed=None, requests=0, evidence=None, note=""):
    return {"kind": kind, "status": status, "best": best, "candidates": candidates or [],
            "observed": observed or {}, "requests": requests, "evidence": evidence or [], "note": note}


def _candidate(archetype, confidence, state, evidence):
    return {"archetype": archetype, "confidence": confidence, "state": state, "evidence": list(evidence)}


def _refused(kind, why):
    return _result(kind, status="refused", note=why)


# -- packages ----------------------------------------------------------------------------------

def _package(spec):
    """-> result for a launcher line or a binary URL, from the text alone. Nothing is run."""
    if _SHELL.search(spec):
        return _result("package", "no_template_fits", note="shell syntax is not accepted. " + NO_FIT_NOTE)
    try:
        tokens = shlex.split(spec)
    except ValueError:
        return _result("package", "no_template_fits", note=NO_FIT_NOTE)
    if not tokens:
        return _result("package", "no_template_fits", note=NO_FIT_NOTE)
    head = tokens[0]
    if head == "npx":
        rest = [t for t in tokens[1:] if not t.startswith("-")]
        pkg = rest[0] if rest else None
        if pkg and _NPM.match(pkg):
            pinned = bool(re.search(r"(?<=.)@[^@/]+$", pkg))
            return _stdio("stdio-npx", "npx", pkg, pinned)
    elif head == "uvx":
        toks = tokens[1:]
        if "--from" in toks and toks.index("--from") + 1 < len(toks):
            pkg = toks[toks.index("--from") + 1]
        else:
            rest = [t for t in toks if not t.startswith("-")]
            pkg = rest[0] if rest else None
        if pkg and _UVX.match(pkg):
            return _stdio("stdio-uvx", "uvx", pkg, bool(re.search(r"(==|@)", pkg)))
    elif head == "docker" and len(tokens) > 2 and tokens[1] == "run":
        toks, i, image = tokens[2:], 0, None
        while i < len(toks):
            t = toks[i]
            if t in _DOCKER_VALUE_FLAGS:
                i += 2
                continue
            if t.startswith("-"):
                i += 1
                continue
            image = t
            break
        if image and _IMAGE.match(image):
            tag = image.split("@sha256:")[0].rpartition(":")[2] if re.search(r":[^/]+$", image.split("@sha256:")[0]) else ""
            pinned = "@sha256:" in image or bool(tag and tag != "latest")
            return _stdio("stdio-docker", "docker", image, pinned)
    elif len(tokens) == 1 and re.match(r"^https://", head, re.I):
        parts = urllib.parse.urlsplit(head)
        path = parts.path.lower()
        if any(path.endswith(e) for e in _BINARY_EXT) and not (parts.username or parts.password):
            obs = {"command": "download", "package": None, "pinned": bool(_BIN_VERSIONED.search(parts.path)),
                   "hosts": [parts.hostname or ""], "url": oauth.safe_url(head)}
            return _result("package", "ok", "stdio-binary", [
                _candidate("stdio-binary", "strong", "unverified",
                           ["a release-style download URL ending in " + next(e for e in _BINARY_EXT if path.endswith(e)),
                            "not started, not downloaded"])], obs)
    return _result("package", "no_template_fits", note=NO_FIT_NOTE)


def _stdio(archetype, command, pkg, pinned):
    obs = {"command": command, "package": pkg, "pinned": pinned}
    ev = ["launcher %s with package %s" % (command, pkg), "pinned: %s" % ("yes" if pinned else "no"),
          "not started, not downloaded, not installed"]
    return _result("package", "ok", archetype, [_candidate(archetype, "strong", "unverified", ev)], obs)


# -- hosted servers ----------------------------------------------------------------------------

class _Run:
    """Counts requests and keeps the evidence. Never stores a header value."""

    def __init__(self, net):
        self.net, self.n, self.evidence = net, 0, []

    def fetch(self, url, what, **kw):
        if self.n >= MAX_REQUESTS:
            raise GatewayError("probe_limit", "the probe's request limit was reached")
        self.n += 1
        kw.setdefault("timeout", PROBE_TIMEOUT_S)
        kw.setdefault("max_bytes", MAX_BYTES)
        return oauth.fetch(url, self.net, what=what, **kw)

    def note(self, text):
        self.evidence.append(text)


def _init_body():
    return json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "gt-lotr-onboard", "version": "0.5.1"}}})


def _is_jsonrpc(resp):
    """True when the body is a JSON-RPC message (plain JSON or the data: lines of an event stream)."""
    text = resp.body.decode("utf-8", errors="replace")
    blobs = [text]
    if "data:" in text:
        blobs = ["\n".join(l[5:].lstrip() for l in blk.splitlines() if l.startswith("data:"))
                 for blk in text.split("\n\n")]
    for b in blobs:
        try:
            doc = json.loads(b)
        except (ValueError, RecursionError):
            continue
        if isinstance(doc, dict) and doc.get("jsonrpc") == "2.0" and ("result" in doc or "error" in doc):
            return True
    return False


def _names(resp):
    seen = []
    for k, _v in resp.headers:
        if k not in seen:
            seen.append(k)
    return ", ".join(seen) or "none"


def _post(run, url, token=None):
    hdrs = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        hdrs["Authorization"] = "Bearer " + token
    return run.fetch(url, "MCP endpoint", method="POST", body=_init_body(), headers=hdrs)


def _same_origin(a, b):
    pa, pb = urllib.parse.urlsplit(a), urllib.parse.urlsplit(b)
    return (pa.scheme, pa.hostname, pa.port) == (pb.scheme, pb.hostname, pb.port)


def _json_doc(resp):
    try:
        return resp.json()
    except GatewayError:
        return None


def _probe_url(url, net):
    parts = urllib.parse.urlsplit(url)
    run = _Run(net)
    obs = {"url": oauth.safe_url(url),
           "hosts": [parts.hostname + (":%d" % parts.port if parts.port else "")] if parts.hostname else []}
    try:
        r1 = _post(run, url)
        run.note("POST initialize with no credential: HTTP %d; response headers: %s" % (r1.status, _names(r1)))
        ctype = (r1.header("Content-Type") or [""])[0].lower()
        if 200 <= r1.status < 300 and _is_jsonrpc(r1):
            obs["auth_scheme"] = "none"
            return _result("url", "ok", "mcp-http-key", [_candidate(
                "mcp-http-key", "strong", "self-verified",
                list(run.evidence) + ["answered a JSON-RPC initialize with no credential: auth_scheme none"])],
                obs, run.n, run.evidence)
        if r1.status in (401, 403):
            return _challenged(run, url, parts, r1, obs)
        if not (200 <= r1.status < 300) or "event-stream" not in ctype:
            return _sse_or_nothing(run, url, parts, r1, obs)
    except GatewayError as e:
        return _gateway_failure("url", e, run)
    return _result("url", "no_template_fits", requests=run.n, evidence=run.evidence, observed=obs, note=NO_FIT_NOTE)


def _challenged(run, url, parts, r1, obs):
    challenge = oauth.parse_www_authenticate(r1.header("WWW-Authenticate"))
    names = sorted(challenge) if challenge else []
    basic = any(v.lower().lstrip().startswith(("basic", "digest")) for v in r1.header("WWW-Authenticate"))
    if basic and not challenge:
        run.note("the server asks for Basic or Digest authentication, which no MCP archetype uses")
        return _result("url", "no_template_fits", requests=run.n, evidence=run.evidence, observed=obs, note=NO_FIT_NOTE)
    origin = "%s://%s" % (parts.scheme, parts.netloc)
    prm_url = challenge.get("resource_metadata")
    if prm_url and not _same_origin(prm_url, url):
        run.note("the challenge names resource metadata on another origin; not followed")
        prm_url = None
    elif not prm_url:
        prm_url = origin + "/.well-known/oauth-protected-resource" + (parts.path.rstrip("/") if parts.path != "/" else "")
    prm = None
    try:
        r2 = run.fetch(prm_url, "protected resource metadata", headers={"Accept": "application/json"})
        run.note("GET protected-resource metadata: HTTP %d" % r2.status)
        doc = _json_doc(r2) if r2.status == 200 else None
        if doc and isinstance(doc.get("authorization_servers"), list) and doc["authorization_servers"] \
                and isinstance(doc["authorization_servers"][0], str):
            prm = doc
    except GatewayError as e:
        run.note("protected-resource metadata not reachable (%s)" % e.code)
    if prm:
        issuer = prm["authorization_servers"][0]
        ip = urllib.parse.urlsplit(issuer)
        as_url = "%s://%s/.well-known/oauth-authorization-server%s" % (
            ip.scheme, ip.netloc, ip.path.rstrip("/") if ip.path != "/" else "")
        scopes = [oauth.clean(s, 80) for s in (prm.get("scopes_supported") or []) if isinstance(s, str)][:MAX_SCOPES]
        obs["scopes"] = scopes
        reg = None
        try:
            r3 = run.fetch(as_url, "authorization server metadata", headers={"Accept": "application/json"})
            run.note("GET authorization-server metadata: HTTP %d" % r3.status)
            meta = _json_doc(r3) if r3.status == 200 else None
            if meta is not None:
                reg = bool(meta.get("registration_endpoint"))
        except GatewayError as e:
            run.note("authorization-server metadata not reachable (%s)" % e.code)
        base_ev = list(run.evidence) + ["challenge parameters: " + (", ".join(names) or "none"),
                                        "no credential was sent to this server"]
        if reg is None:
            obs["registration_endpoint"] = False
            cands = [_candidate("mcp-oauth", "weak", "unverified", base_ev + [
                        "the authorization server's metadata was not read, so dynamic registration is unknown"]),
                     _candidate("mcp-oauth-preregistered", "weak", "unverified", base_ev)]
            return _result("url", "ok", "mcp-oauth", cands, obs, run.n, run.evidence)
        obs["registration_endpoint"] = reg
        arche = "mcp-oauth" if reg else "mcp-oauth-preregistered"
        why = ("the authorization server advertises a registration endpoint" if reg else
               "the authorization server advertises no registration endpoint: an OAuth app must be created first")
        return _result("url", "ok", arche, [_candidate(arche, "strong", "self-verified", base_ev + [why])],
                       obs, run.n, run.evidence)
    # a bearer challenge and no OAuth metadata: a key-style server. One request with the fake credential.
    obs["auth_scheme"] = "bearer"
    ev = list(run.evidence) + ["challenge parameters: " + (", ".join(names) or "none")]
    try:
        r4 = _post(run, url, FAKE_CREDENTIAL)
        ev.append("POST initialize with the fake credential: HTTP %d" % r4.status)
        run.note(ev[-1])
        if r4.status in (401, 403):
            ev.append("the fake credential was rejected: the key gate is at this endpoint")
            return _result("url", "ok", "mcp-http-key", [_candidate("mcp-http-key", "strong", "self-verified", ev)],
                           obs, run.n, run.evidence)
        if 200 <= r4.status < 300 and _is_jsonrpc(r4):
            obs["auth_scheme"] = "none"
            ev.append("the server accepted the fake credential, so it does not check one")
            return _result("url", "ok", "mcp-http-key", [_candidate("mcp-http-key", "weak", "self-verified", ev)],
                           obs, run.n, run.evidence)
    except GatewayError as e:
        ev.append("the fake-credential request failed (%s)" % e.code)
    return _result("url", "ok", "mcp-http-key", [_candidate("mcp-http-key", "weak", "unverified", ev)],
                   obs, run.n, run.evidence)


def _sse_or_nothing(run, url, parts, r1, obs):
    """The POST was not an MCP answer. A legacy HTTP+SSE server answers the POST with 404/405 and streams
    an `endpoint` event to a GET."""
    ev = list(run.evidence)
    try:
        r2 = run.fetch(url, "MCP endpoint", headers={"Accept": "text/event-stream"}, timeout=SSE_TIMEOUT_S, max_bytes=4096)
        ctype = (r2.header("Content-Type") or [""])[0].lower()
        ev.append("GET with Accept text/event-stream: HTTP %d, content type %s" % (r2.status, ctype.split(";")[0] or "none"))
        run.note(ev[-1])
        text = r2.body.decode("utf-8", errors="replace")
        if r2.status == 200 and "event-stream" in ctype and re.search(r"(^|\n)event:\s*endpoint\b", text):
            return _result("url", "ok", "sse-bridge", [_candidate(
                "sse-bridge", "strong", "self-verified", ev + ["the stream announced an endpoint event: legacy HTTP+SSE"])],
                obs, run.n, run.evidence)
    except GatewayError as e:
        if e.code == "unreachable" and parts.path.rstrip("/").endswith("/sse"):
            ev.append("the GET was held open past %d s on a path ending in /sse: consistent with a legacy SSE stream, "
                      "not confirmed" % SSE_TIMEOUT_S)
            return _result("url", "ok", "sse-bridge", [_candidate("sse-bridge", "weak", "unverified", ev)],
                           obs, run.n, run.evidence)
        ev.append("the GET failed (%s)" % e.code)
        run.note(ev[-1])
    return _result("url", "no_template_fits", requests=run.n, evidence=run.evidence, observed=obs, note=NO_FIT_NOTE)


def _gateway_failure(kind, e, run):
    if e.code == "unreachable":
        return _result(kind, "unreachable", requests=run.n, evidence=run.evidence,
                       note="the server could not be reached (%s)" % oauth.clean(e.message, 120))
    return _result(kind, "refused", requests=run.n, evidence=run.evidence,
                   note="refused by the network policy or the connection checks (%s): %s" % (e.code, oauth.clean(e.message, 160)))


# -- entry point -------------------------------------------------------------------------------

def probe(target, *, net=None):
    """Classify `target`. Never raises for a bad target; never returns a credential-shaped string."""
    spec = target.strip() if isinstance(target, str) else ""
    if not spec:
        return _result("package", "no_template_fits", note=NO_FIT_NOTE)
    if shaping.scan_credentials(spec):
        return _refused("package", "the text looks like a credential and was not used; a credential is never part of a target")
    net = net or oauth.NetPolicy()
    if re.match(r"^https?://", spec, re.I):
        try:
            u = urllib.parse.urlsplit(spec)
            u.port                                                    # noqa: B018 (ValueError on a bad port)
        except ValueError:
            return _refused("url", "the address is not a valid URL")
        if u.username or u.password:
            return _refused("url", "the address carries a user name or password; none is accepted")
        path = u.path.lower()
        if len(spec.split()) == 1 and any(path.endswith(e) for e in _BINARY_EXT):
            return _package(spec)
        try:
            net.check_url(spec, "MCP endpoint")
        except GatewayError as e:
            return _refused("url", "%s: %s" % (e.code, oauth.clean(e.message, 160)))
        return _probe_url(spec, net)
    if re.match(r"^[a-z][a-z0-9+.-]*://", spec, re.I):
        return _result("url", "no_template_fits", note="only http(s) addresses are probed. " + NO_FIT_NOTE)
    return _package(spec)
