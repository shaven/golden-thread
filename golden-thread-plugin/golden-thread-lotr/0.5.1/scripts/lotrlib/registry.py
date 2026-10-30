"""registry.json: the authority on which connections exist and which clients may use them.

Two halves, both admin-plane:
  connections  what the gateway can reach, as whom, with a *ref* to the credential
  clients      enrolled machines (hub mode), each holding only the sha256 of its secret

Validation is strict on load and on add, because the registry is the security boundary:
a connection whose host is not pinned, whose token is a pasted literal, or whose zone differs
from the registry's would quietly widen what the gateway can do. Zones never mix -- a
personal registry refuses a work connection outright rather than filtering it.
"""
import copy
import difflib
import hashlib
import hmac
import json
import os
import re
# The stdlib module, not lotrlib.secrets: inside a package an absolute import is absolute.
import secrets as _stdsecrets
from urllib.parse import urlsplit

from . import endpoint_health, upstream
from .errors import GatewayError
from .util import now_iso, read_json, write_json

CONN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*@[a-z0-9][a-z0-9._-]*$")
CLIENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# 0.3.0: the brokered schemes, resolved only by gt core's unlock authority (secrets.BROKERED).
OAUTH_TOKEN_REF = re.compile(r"^(sealed|store):[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
OAUTH_SECRET_NAME = re.compile(r"^(sealed|store):lotr-oauth-")     # what oauth.secret_name writes
TOKEN_REF = re.compile(r"^(file|store|keychain|env|sealed|sops|op|bw|vault|wincred):")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")          # fullmatch only: `$` lets a trailing newline in
AUTH_SCHEMES = ("bearer", "basic", "header", "none")
TRUST = ("T0", "T1", "T2")
TIERS = ("read", "write", "consent")
LOCAL_HOSTS = ("127.0.0.1", "localhost")
POLICY_KEYS = ("deny", "consent", "write", "read")


def _bad(msg):
    return GatewayError("registry_invalid", msg)


def _validate_downstream_fields(entry, where):
    """0.5.1: the optional fields for a downstream LLM, on either kind: `limits` (calls per minute,
    calls in flight) and `llm` (what a model op can trigger). Absent means no change."""
    for field, check in (("limits", upstream.limits_problem), ("llm", upstream.llm_problem)):
        if field in entry:
            why = check(entry[field])
            if why:
                raise _bad(f"{where}: {why}")
    why = endpoint_health.problem(entry)               # 0.22.0: health_tracking, sub_endpoints, ...
    if why:
        raise _bad(f"{where}: {why}")


def _validate_trust_ca(entry, where):
    """0.5.1: `trust_ca` = {"name", "sha256"}: the owner's private CA for this connection's https
    endpoint (lotrlib.trustca). Only an https http-kind or http-transport mcp connection may carry
    it; plain http to a loopback host, a stdio server and an OAuth sign-in cannot. Shape only here:
    the file is checked against the sha256 when the connection is used."""
    from . import trustca
    ref = entry["trust_ca"]
    if not (isinstance(ref, dict) and set(ref) == {"name", "sha256"}
            and isinstance(ref["name"], str) and trustca.NAME_RX.fullmatch(ref["name"])
            and isinstance(ref["sha256"], str) and SHA256_HEX.fullmatch(ref["sha256"])):
        raise _bad(f"{where}: trust_ca must be {{\"name\": a CA name, \"sha256\": 64 hex}}")
    url = entry.get("base_url") if entry.get("kind") == "http" else (
        entry.get("endpoint") if entry.get("kind") == "mcp" and entry.get("transport") == "http"
        else None)
    if not (isinstance(url, str) and urlsplit(url).scheme == "https"):
        raise _bad(f"{where}: trust_ca applies only to an https endpoint of an http or "
                   "http-transport mcp connection")
    if (entry.get("auth") or {}).get("scheme") == "oauth":
        raise _bad(f"{where}: trust_ca does not apply to an oauth connection")


def validate_connection(entry, zone):
    """Raise registry_invalid naming the offending field; return None when the entry is sound.

    Messages name fields and ids, never a token_ref's would-be literal value: if someone
    pasted a token where a ref belongs, echoing it back would be the leak.
    """
    if not isinstance(entry, dict):
        raise _bad("connection must be an object")
    cid = entry.get("id")
    if not isinstance(cid, str) or not CONN_ID.match(cid):
        raise _bad(f"connection id {cid!r} must match name@zone in lowercase "
                   "(letters, digits, . _ -)")
    where = f"connection {cid}"
    if entry.get("zone") != zone:
        raise _bad(f"{where}: zone {entry.get('zone')!r} does not match registry zone "
                   f"{zone!r} (zones never mix)")
    _validate_downstream_fields(entry, where)
    if "trust_ca" in entry:
        _validate_trust_ca(entry, where)
    if entry.get("kind") == "mcp":
        return _validate_mcp(entry, where)
    if entry.get("kind") != "http":
        raise _bad(f"{where}: kind must be 'http' or 'mcp'")
    for field in ("identity", "profile"):
        if not isinstance(entry.get(field), str) or not entry.get(field):
            raise _bad(f"{where}: {field} is required")

    base = entry.get("base_url")
    if not isinstance(base, str):
        raise _bad(f"{where}: base_url is required")
    parts = urlsplit(base)
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" and host:
        pass
    elif parts.scheme == "http" and host in LOCAL_HOSTS:
        pass  # plain http only to this machine, for tests and local fakes
    else:
        raise _bad(f"{where}: base_url must be https (or http to 127.0.0.1/localhost)")
    if parts.username or parts.password:
        raise _bad(f"{where}: base_url must not carry credentials")

    network = entry.get("network")
    hosts = network.get("hosts") if isinstance(network, dict) else None
    if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
        raise _bad(f"{where}: network.hosts must be a list of host names")
    if host not in [h.lower() for h in hosts]:
        raise _bad(f"{where}: base_url host {host} is not in network.hosts")

    if entry.get("trust") not in TRUST:
        raise _bad(f"{where}: trust must be one of {', '.join(TRUST)}")
    if entry.get("locality", "remote") not in ("local", "remote"):          # 0.5.1
        raise _bad(f"{where}: locality must be local or remote")
    if "provider" in entry and upstream.provider_problem(entry["provider"]):   # 0.5.1
        raise _bad(f"{where}: {upstream.provider_problem(entry['provider'])}")
    if "timeouts" in entry:                                                 # 0.5.1: timeouts.call_s
        why = upstream.timeouts_problem(entry["timeouts"])
        if why:
            raise _bad(f"{where}: {why}")
    h = entry.get("health")
    if h is not None and not (isinstance(h, str) and re.fullmatch(r"GET /[A-Za-z0-9._/-]{0,80}", h)):
        raise _bad(f"{where}: health must look like 'GET /health' (a read-only path)")

    auth = entry.get("auth")
    if not isinstance(auth, dict):
        raise _bad(f"{where}: auth is required")
    scheme = auth.get("scheme")
    if scheme not in AUTH_SCHEMES:
        raise _bad(f"{where}: auth.scheme must be one of {', '.join(AUTH_SCHEMES)}")
    _not_oauth_in_disguise(auth, where)
    if scheme != "none":
        ref = auth.get("token_ref")
        if not isinstance(ref, str) or not TOKEN_REF.match(ref):
            # Deliberately does not quote the value: it may be a literal secret.
            raise _bad(f"{where}: auth.token_ref must be a secret ref "
                       "(file:, store:, keychain:, env:, sealed:, sops:, op:, bw:, vault:, "
                       "wincred:), never a literal token")
    if scheme == "basic" and not auth.get("user"):
        raise _bad(f"{where}: auth.user is required for scheme basic")
    if scheme == "header" and not auth.get("header"):
        raise _bad(f"{where}: auth.header is required for scheme header")

    headers = entry.get("headers", {})
    if not isinstance(headers, dict):
        raise _bad(f"{where}: headers must be an object")
    policy = entry.get("policy", {})
    if not isinstance(policy, dict):
        raise _bad(f"{where}: policy must be an object")
    for key in POLICY_KEYS:
        globs = policy.get(key, [])
        if not isinstance(globs, list) or not all(isinstance(g, str) for g in globs):
            raise _bad(f"{where}: policy.{key} must be a list of globs")
    if not isinstance(entry.get("enabled", True), bool):
        raise _bad(f"{where}: enabled must be true or false")
    if not isinstance(entry.get("strict_tools", True), bool):
        raise _bad(f"{where}: strict_tools must be true or false")


MCP_TRANSPORTS = ("http", "stdio")   # streamable HTTP (JSON or SSE replies); stdio since 0.5.0; legacy sse later


def _endpoint_ok(where, url, network, field):
    parts = urlsplit(url) if isinstance(url, str) else None
    host = ((parts.hostname if parts else "") or "").lower()
    if not (parts and ((parts.scheme == "https" and host) or
                       (parts.scheme == "http" and host in LOCAL_HOSTS))):
        raise _bad(f"{where}: {field} must be https (or http to 127.0.0.1/localhost)")
    if parts.username or parts.password:
        raise _bad(f"{where}: {field} must not carry credentials")
    hosts = network.get("hosts") if isinstance(network, dict) else None
    if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
        raise _bad(f"{where}: network.hosts must be a list of host names")
    if host not in [h.lower() for h in hosts]:
        raise _bad(f"{where}: {field} host {host} is not in network.hosts")


def _not_oauth_in_disguise(auth, where):
    """A record whose scheme was edited AWAY from oauth (independent review of 7473a24, blocker 1):
    the bearer path would read the OAuth sign-in's sealed envelope and send it raw to whatever
    endpoint the edited record names. Neither an `oauth` block nor an OAuth secret's name may
    appear under any other scheme."""
    if auth.get("scheme") == "oauth":
        return
    if "oauth" in auth:
        raise _bad(f"{where}: auth.oauth belongs to scheme oauth only; a record edited away from "
                   "oauth is refused")
    ref = auth.get("token_ref")
    if isinstance(ref, str) and OAUTH_SECRET_NAME.match(ref):
        raise _bad(f"{where}: auth.token_ref names an OAuth sign-in's secret, which only scheme "
                   "oauth may use")


def _validate_oauth(entry, auth, where):
    """auth.scheme "oauth" (0.3.0): LOTR's own OAuth. `token_ref` is the REFRESH token's ref
    (sealed: when gt unlock is on, else store:), or null when the AS issued none; the access
    token is never stored. The `oauth` block holds only public facts and REFS."""
    block = auth.get("oauth")
    if not isinstance(block, dict):
        raise _bad(f"{where}: auth.oauth must be an object")
    if entry.get("refresh_cmd"):
        raise _bad(f"{where}: refresh_cmd does not apply to an oauth connection")
    ref = auth.get("token_ref")
    if ref is not None and (not isinstance(ref, str) or not OAUTH_TOKEN_REF.match(ref)):
        # Only the two kinds LOTR itself writes: a tampered file:/env:/keychain: ref would be read
        # and POSTed to the token endpoint.
        raise _bad(f"{where}: an oauth connection's auth.token_ref must be a sealed: or store: "
                   "ref or null, never a literal token or another kind of ref")
    net = block.get("net", {})
    if not isinstance(net, dict) or not all(isinstance(net.get(k, False), bool)
                                            for k in ("allow_private", "allow_insecure_localhost")):
        raise _bad(f"{where}: auth.oauth.net must hold booleans allow_private / "
                   "allow_insecure_localhost")
    if "insecure_metadata_hosts" in net:
        # 0.5.0: hosts the owner vouched for with --allow-insecure-metadata; the file is editable,
        # so each is re-checked as the flag checks it (a bare host name, nothing that is a URL)
        from .oauth import parse_metadata_hosts
        hs = net["insecure_metadata_hosts"]
        try:
            if not isinstance(hs, list) or not all(isinstance(h, str) for h in hs):
                raise GatewayError("oauth_bad_metadata_host", "not a list of host names")
            parse_metadata_hosts(hs)
        except GatewayError:
            raise _bad(f"{where}: auth.oauth.net.insecure_metadata_hosts must be a list of bare "
                       "host names (the hosts vouched for with --allow-insecure-metadata)")
    hosts = [h.lower() for h in (entry.get("network") or {}).get("hosts", []) if isinstance(h, str)]
    for key in ("issuer", "authorization_endpoint", "token_endpoint", "resource"):
        if not isinstance(block.get(key), str):
            raise _bad(f"{where}: auth.oauth.{key} is required")
    # The token is for ONE resource: the endpoint it is sent to must be that resource (scheme,
    # host, port and path), not merely a pinned host.
    from .oauth import canonical_resource
    try:
        bound = canonical_resource(entry.get("endpoint") or "") == canonical_resource(block["resource"])
    except GatewayError:
        bound = False
    if not bound:
        raise _bad(f"{where}: endpoint is not the resource this sign-in was issued for "
                   "(auth.oauth.resource); sign in again with lotr connect")
    for key in ("issuer", "authorization_endpoint", "token_endpoint", "revocation_endpoint",
                "registration_endpoint"):
        url = block.get(key)
        if url is None and key in ("revocation_endpoint", "registration_endpoint"):
            continue
        if not isinstance(url, str):
            raise _bad(f"{where}: auth.oauth.{key} must be a URL")
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme == "https" and host:
            pass
        elif parts.scheme == "http" and host in LOCAL_HOSTS + ("::1",) and \
                net.get("allow_insecure_localhost"):
            pass
        else:
            raise _bad(f"{where}: auth.oauth.{key} must be https (plain http to a loopback host "
                       "only when the owner passed --allow-insecure-localhost)")
        if parts.username or parts.password or parts.fragment:
            raise _bad(f"{where}: auth.oauth.{key} must not carry credentials or a fragment")
        if host not in hosts:
            raise _bad(f"{where}: auth.oauth.{key} host {host} is not in network.hosts")
    ep = urlsplit(entry.get("endpoint") or "")
    if ep.scheme == "http" and not net.get("allow_insecure_localhost"):
        raise _bad(f"{where}: a plain-http endpoint needs --allow-insecure-localhost")
    if not isinstance(block.get("client_id"), str) or not block["client_id"]:
        raise _bad(f"{where}: auth.oauth.client_id is required")
    if block.get("client_id_source") not in ("preregistered", "cimd", "dcr"):
        raise _bad(f"{where}: auth.oauth.client_id_source must be preregistered, cimd or dcr")
    if block.get("token_endpoint_auth_method", "none") not in ("none", "client_secret_basic",
                                                               "client_secret_post"):
        raise _bad(f"{where}: auth.oauth.token_endpoint_auth_method is not supported")
    csr = block.get("client_secret_ref")
    if csr is not None and (not isinstance(csr, str) or not OAUTH_TOKEN_REF.match(csr)):
        # The same two kinds as token_ref: anything else (env:, file:, keychain:) an edited record
        # named would be read and POSTed to the token endpoint.
        raise _bad(f"{where}: auth.oauth.client_secret_ref must be a sealed: or store: ref or null, never a "
                   "literal secret")
    if block.get("token_endpoint_auth_method", "none") != "none" and not csr:
        raise _bad(f"{where}: a confidential client needs auth.oauth.client_secret_ref")
    sc = block.get("scopes", [])
    if not isinstance(sc, list) or not all(isinstance(x, str) and x and " " not in x for x in sc):
        raise _bad(f"{where}: auth.oauth.scopes must be a list of scope tokens")
    if not isinstance(block.get("send_resource", True), bool):
        raise _bad(f"{where}: auth.oauth.send_resource must be true or false")
    if block.get("redirect_host", "127.0.0.1") not in ("127.0.0.1", "localhost"):
        raise _bad(f"{where}: auth.oauth.redirect_host must be 127.0.0.1 or localhost")
    # 0.5.0: the identifier the resource metadata advertised, when it is a less specific URI of the
    # endpoint (same origin, path prefix); an edited record cannot point it anywhere else
    rid = block.get("resource_id")
    if rid is not None:
        from .oauth import resource_covers
        if not isinstance(rid, str) or not resource_covers(rid, entry.get("endpoint") or ""):
            raise _bad(f"{where}: auth.oauth.resource_id must be the endpoint's own origin and a "
                       "prefix of its path")
    # 0.5.0: extra authorization parameters, allow-listed (re-checked: the record is editable)
    if block.get("auth_params") is not None:
        from .oauth import check_auth_params
        try:
            check_auth_params(block["auth_params"])
        except GatewayError as e:
            raise _bad(f"{where}: auth.oauth.auth_params: {e.message}")
    if not isinstance(block.get("audience_check", True), bool):
        raise _bad(f"{where}: auth.oauth.audience_check must be true or false")
    mac = block.get("max_access_cache_s", 3600)
    if isinstance(mac, bool) or not isinstance(mac, (int, float)) or mac <= 0:
        raise _bad(f"{where}: auth.oauth.max_access_cache_s must be a positive number")


ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
STDIO_TIMEOUTS = ("start_s", "call_s", "idle_s")


def _validate_mcp_stdio(entry, where):
    """An `mcp` connection over stdio (0.5.0): a local command LOTR launches. `command` is an argv
    list (no shell), `env` maps names to secret REFS (never values), `cwd` is an absolute path."""
    if entry.get("endpoint"):
        raise _bad(f"{where}: a stdio connection has a command, not an endpoint")
    if entry.get("trust") not in TRUST:
        raise _bad(f"{where}: trust must be one of {', '.join(TRUST)}")
    cmd = entry.get("command")
    if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) and x and "\x00" not in x
                                                        for x in cmd):
        raise _bad(f"{where}: command must be a non-empty list of program arguments "
                   "(no shell, so no pipes or quoting)")
    env = entry.get("env", {})
    if not isinstance(env, dict):
        raise _bad(f"{where}: env must be an object of NAME: secret ref")
    for k, ref in env.items():
        if not isinstance(k, str) or not ENV_NAME.match(k):
            raise _bad(f"{where}: env name {str(k)[:40]!r} is not a valid variable name")
        if not isinstance(ref, str) or not TOKEN_REF.match(ref):
            # Deliberately does not quote the value: it may be a literal secret.
            raise _bad(f"{where}: env {k} must be a secret ref "
                       "(file:, store:, keychain:, env:, sealed:, sops:, op:, bw:, vault:, "
                       "wincred:), never a literal value")
    plain = entry.get("env_plain", {})
    if not isinstance(plain, dict):
        raise _bad(f"{where}: env_plain must be an object of NAME: plain value")
    from .secretcheck import looks_like_secret
    from . import conn_templates as _ct
    for k, v in plain.items():
        if not isinstance(k, str) or not ENV_NAME.match(k):
            raise _bad(f"{where}: env_plain name {str(k)[:40]!r} is not a valid variable name")
        if _ct._ENV_FORBIDDEN.match(k) or k in env:
            raise _bad(f"{where}: env_plain {k} may not be set here (it changes what the program "
                       "loads, or is also an env ref)")
        if not isinstance(v, str) or len(v) > 2048 or "\x00" in v:
            raise _bad(f"{where}: env_plain {k} must be a short string")
        if looks_like_secret(k, v):
            # Deliberately does not quote the value: it may be a literal secret.
            raise _bad(f"{where}: env_plain {k} looks like a secret; a credential goes under env "
                       "as a reference (store:, keychain:, ...), never in env_plain")
    rt = entry.get("runtime")
    if rt is not None:
        from . import runtimes
        if rt not in runtimes.RUNTIMES:
            raise _bad(f"{where}: runtime must be one of {', '.join(runtimes.RUNTIMES)}")
    br = entry.get("bridge")
    if br is not None and br not in ("sse",):
        raise _bad(f"{where}: bridge must be 'sse' or absent")
    cwd = entry.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not os.path.isabs(cwd)):
        raise _bad(f"{where}: cwd must be an absolute path, or absent")
    tmo = entry.get("timeouts", {})
    if not isinstance(tmo, dict) or set(tmo) - set(STDIO_TIMEOUTS):
        raise _bad(f"{where}: timeouts may only hold {', '.join(STDIO_TIMEOUTS)}")
    for k, v in tmo.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 1 <= v <= 3600:
            raise _bad(f"{where}: timeouts.{k} must be a number of seconds from 1 to 3600")
    auth = entry.get("auth")
    if auth is not None and (not isinstance(auth, dict) or auth.get("scheme", "none") != "none"):
        raise _bad(f"{where}: a stdio connection has no auth block; its credentials are env refs")
    _validate_common_mcp(entry, where)


def _validate_common_mcp(entry, where):
    tools = entry.get("tools", [])
    if not isinstance(tools, list) or not all(isinstance(x, dict) and isinstance(x.get("name"), str)
                                              for x in tools):
        raise _bad(f"{where}: tools must be a list of objects with a name")
    policy = entry.get("policy", {})
    if not isinstance(policy, dict):
        raise _bad(f"{where}: policy must be an object")
    for key in POLICY_KEYS:
        globs = policy.get(key, [])
        if not isinstance(globs, list) or not all(isinstance(g, str) for g in globs):
            raise _bad(f"{where}: policy.{key} must be a list of globs")
    if not isinstance(entry.get("enabled", True), bool):
        raise _bad(f"{where}: enabled must be true or false")
    if not isinstance(entry.get("strict_tools", True), bool):
        raise _bad(f"{where}: strict_tools must be true or false")


def _validate_static_headers(entry, auth, where):
    """0.5.1: `headers` on an mcp connection are static and NOT secret (`lotr add-mcp --header`).
    A reserved name, a secret ref or a secret-shaped value is refused; messages never quote the
    value, which may be a literal secret."""
    hdrs = entry.get("headers", {})
    if not isinstance(hdrs, dict):
        raise _bad(f"{where}: headers must be an object of NAME: value")
    from . import conn_templates as _ct
    from .secretcheck import looks_like_secret
    own = (auth.get("header") or "").lower() if auth.get("scheme") == "header" else ""
    for k, v in hdrs.items():
        if not isinstance(k, str) or not _ct._HEADER_NAME.match(k):
            raise _bad(f"{where}: header name {str(k)[:40]!r} is not a plain header name")
        if k.lower() in _ct._HEADER_FORBIDDEN or k.lower() == own:
            raise _bad(f"{where}: header {k} is set by LOTR itself (auth, content type, session) "
                       "and cannot be set here")
        if not isinstance(v, str) or len(v) > 2048 or any(c in v for c in "\r\n\x00"):
            raise _bad(f"{where}: header {k} must be a short single-line string")
        if TOKEN_REF.match(v):
            raise _bad(f"{where}: header {k} holds a secret reference; headers here are plain, "
                       "non-secret values. Use --auth-header with --auth-ref for a key")
        if looks_like_secret(k, v):
            raise _bad(f"{where}: header {k} looks like a secret; headers here are plain, "
                       "non-secret values. Use --auth-header with --auth-ref for a key")


def _validate_mcp(entry, where):
    """An `mcp` connection (0.2.0): a downstream MCP endpoint behind SSO/OAuth, reached with the
    bearer token its local client already holds -- by REF, never a literal -- and refreshed by
    that client's own helper (`refresh_cmd`, an argv list run with no shell)."""
    if not isinstance(entry.get("identity"), str) or not entry.get("identity"):
        raise _bad(f"{where}: identity is required")
    if entry.get("transport") not in MCP_TRANSPORTS:
        raise _bad(f"{where}: transport must be one of {', '.join(MCP_TRANSPORTS)} in this "
                   "release (the legacy sse transport is not supported)")
    if entry.get("transport") == "stdio":
        return _validate_mcp_stdio(entry, where)
    _endpoint_ok(where, entry.get("endpoint"), entry.get("network"), "endpoint")
    if entry.get("trust") not in TRUST:
        raise _bad(f"{where}: trust must be one of {', '.join(TRUST)}")
    auth = entry.get("auth")
    if not isinstance(auth, dict) or auth.get("scheme") not in ("bearer", "header", "none", "oauth"):
        raise _bad(f"{where}: auth.scheme must be bearer, header, oauth or none for an mcp "
                   "connection")
    _not_oauth_in_disguise(auth, where)
    if auth.get("scheme") == "oauth":
        _validate_oauth(entry, auth, where)
    if auth.get("scheme") in ("bearer", "header"):
        ref = auth.get("token_ref")
        if not isinstance(ref, str) or not TOKEN_REF.match(ref):
            raise _bad(f"{where}: auth.token_ref must be a secret ref "
                       "(file:, store:, keychain:, env:, sealed:, sops:, op:, bw:, vault:, "
                       "wincred:), never a literal token")
    if auth.get("prefix") is not None:
        # 0.5.0 (kind mcp-http): Authorization: <prefix><key>, e.g. "Token token=" for PagerDuty
        from . import conn_templates as _ct
        why = None if auth.get("scheme") == "bearer" else "applies to scheme bearer only"
        why = why or _ct.check_auth_prefix(auth.get("prefix"))
        if why:
            raise _bad(f"{where}: auth.prefix {why}")
    if auth.get("scheme") == "header":
        # 0.5.0 (kind mcp-http): the static key goes in a named header instead of Authorization
        from . import conn_templates as _ct
        h = auth.get("header")
        if not isinstance(h, str) or not _ct._HEADER_NAME.match(h) or \
                h.lower() in _ct._HEADER_FORBIDDEN:
            raise _bad(f"{where}: auth.header must be a plain header name LOTR does not set "
                       "itself (not Authorization, Content-Type, Host, ...)")
    _validate_static_headers(entry, auth, where)
    cmd = entry.get("refresh_cmd")
    if cmd is not None and (not isinstance(cmd, list) or not cmd
                            or not all(isinstance(x, str) and x for x in cmd)):
        raise _bad(f"{where}: refresh_cmd must be a list of program arguments, or absent")
    tools = entry.get("tools", [])
    if not isinstance(tools, list) or not all(isinstance(x, dict) and isinstance(x.get("name"), str)
                                              for x in tools):
        raise _bad(f"{where}: tools must be a list of objects with a name")
    policy = entry.get("policy", {})
    if not isinstance(policy, dict):
        raise _bad(f"{where}: policy must be an object")
    for key in POLICY_KEYS:
        globs = policy.get(key, [])
        if not isinstance(globs, list) or not all(isinstance(g, str) for g in globs):
            raise _bad(f"{where}: policy.{key} must be a list of globs")
    if not isinstance(entry.get("enabled", True), bool):
        raise _bad(f"{where}: enabled must be true or false")
    if not isinstance(entry.get("strict_tools", True), bool):
        raise _bad(f"{where}: strict_tools must be true or false")
    return None


# The engine's name for the LOCAL caller (engine.LOCAL_CLIENT). An enrolled hub client by this
# name would be treated as the local caller -- gateway.json's `local` rules instead of its own
# allow list and tier ceiling (review M2, 2026-10-03) -- so it is refused everywhere.
RESERVED_CLIENT_IDS = ("local",)


def _validate_client(c, zone):
    if not isinstance(c, dict):
        raise _bad("client must be an object")
    cid = c.get("id")
    if not isinstance(cid, str) or not CLIENT_ID.match(cid):
        raise _bad(f"client id {cid!r} is invalid (letters, digits, . _ -)")
    if cid.lower() in RESERVED_CLIENT_IDS:
        raise _bad(f"client id {cid!r} is reserved for the local caller")
    if c.get("zone") != zone:
        raise _bad(f"client {cid}: zone {c.get('zone')!r} does not match registry zone {zone!r}")
    if not isinstance(c.get("secret_sha256"), str) or not SHA256_HEX.fullmatch(c["secret_sha256"]):
        raise _bad(f"client {cid}: secret_sha256 must be 64 lowercase hex characters")
    allow = c.get("allow")
    if not isinstance(allow, list) or not all(isinstance(a, str) for a in allow):
        raise _bad(f"client {cid}: allow must be a list of connection globs")
    if c.get("max_tier") not in TIERS:
        raise _bad(f"client {cid}: max_tier must be one of {', '.join(TIERS)}")
    if c.get("revoked") is not None and not isinstance(c.get("revoked"), str):
        raise _bad(f"client {cid}: revoked must be null or a timestamp")


def _check_private_file(path):
    """registry.json is the hub's authority on who may call: it must be this user's and mode
    600 (review M2: the hub reloaded it with no check). POSIX only -- native Windows has no mode
    bits; the profile directory's ACL is what protects it there (config.check_private)."""
    if os.name == "nt":
        return
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return                      # read_json names the missing file
    except OSError as e:
        raise GatewayError("insecure_perms", f"cannot check {path}: {e.strerror}")
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise GatewayError("insecure_perms",
                           f"{path} must be owned by this user and mode 600 (it is "
                           f"{oct(st.st_mode & 0o777)}); the gateway refuses to trust it",
                           hints=[f"chmod 600 {path}"])


def _sha256(secret):
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class Registry:
    """In-memory registry. Mutations (add/enroll/revoke) change memory; save() persists."""

    def __init__(self, zone, connections=None, clients=None, extra=None):
        self.zone = zone
        self.connections = dict(connections or {})
        self.clients = dict(clients or {})
        self._extra = dict(extra or {})   # unknown top-level keys, kept across save()

    def __repr__(self):
        # Never dump entries: clients hold hashes, and a repr ends up in logs.
        return (f"<Registry zone={self.zone} connections={len(self.connections)} "
                f"clients={len(self.clients)}>")

    @classmethod
    def load(cls, path, check_perms=False):
        """`check_perms` (the hub's live reload, lotrd.registry_getter_for): refuse a registry
        that is not this user's mode-600 file. The engine's own load already passed
        config.check_private for the whole home."""
        if check_perms:
            _check_private_file(path)
        data = read_json(path)
        if not isinstance(data, dict):
            raise _bad(f"{path}: top level must be an object")
        if data.get("schema") != 1:
            raise _bad(f"{path}: schema must be 1")
        zone = data.get("zone")
        if not isinstance(zone, str) or not zone:
            raise _bad(f"{path}: zone is required")
        reg = cls(zone, extra={k: v for k, v in data.items()
                               if k not in ("schema", "zone", "connections", "clients")})
        conns = data.get("connections", [])
        clients = data.get("clients", [])
        if not isinstance(conns, list):
            raise _bad(f"{path}: connections must be a list")
        if not isinstance(clients, list):
            raise _bad(f"{path}: clients must be a list")
        for entry in conns:
            validate_connection(entry, zone)
            if entry["id"] in reg.connections:
                raise _bad(f"duplicate connection id {entry['id']}")
            reg.connections[entry["id"]] = entry
        for c in clients:
            _validate_client(c, zone)
            if c["id"] in reg.clients:
                raise _bad(f"duplicate client id {c['id']}")
            reg.clients[c["id"]] = c
        return reg

    # -- connections -------------------------------------------------------------------

    def active(self):
        return [c for c in self.connections.values() if c.get("enabled", True)]

    def connection(self, cid):
        """The entry for `cid`. A disabled connection is refused, not returned: callers on the
        call plane must not reach it, and admin listing reads .connections directly."""
        entry = self.connections.get(cid)
        if entry is None:
            hints = difflib.get_close_matches(str(cid), list(self.connections), n=3, cutoff=0.4)
            raise GatewayError("unknown_connection", f"no connection {cid!r} in zone {self.zone}",
                               hints=hints)
        if not entry.get("enabled", True):
            raise GatewayError("connection_disabled", f"connection {cid} is disabled")
        return entry

    def add_connection(self, entry):
        validate_connection(entry, self.zone)
        if entry["id"] in self.connections:
            raise GatewayError("duplicate_connection", f"connection {entry['id']} already exists")
        self.connections[entry["id"]] = copy.deepcopy(entry)

    # -- clients -----------------------------------------------------------------------

    def client(self, client_id):
        c = self.clients.get(client_id)
        if c is None:
            raise GatewayError("unknown_client", f"no client {client_id!r} enrolled")
        return c

    def authenticate(self, client_id, secret):
        """Return the client entry if `secret` is its enrolled secret.

        Comparison is constant-time over the hex digests. A revoked client is refused even
        with the right secret (checked after the secret, so a wrong guess learns nothing
        about revocation state)."""
        c = self.client(client_id)
        if not isinstance(secret, str) or not hmac.compare_digest(
                _sha256(secret), c["secret_sha256"]):
            raise GatewayError("auth_failed", f"client {client_id} failed authentication")
        if c.get("revoked"):
            raise GatewayError("client_revoked", f"client {client_id} was revoked at {c['revoked']}")
        return c

    def enroll(self, client_id, machine, allow=None, max_tier="write"):
        """Create (or re-create a revoked) client; return its NEW secret exactly once.

        Only the sha256 is stored. The caller must hand the secret to the machine and drop it.
        """
        if not isinstance(client_id, str) or not CLIENT_ID.match(client_id) \
                or client_id.lower() in RESERVED_CLIENT_IDS:
            raise GatewayError("registry_invalid", f"client id {client_id!r} is invalid")
        existing = self.clients.get(client_id)
        if existing is not None and not existing.get("revoked"):
            raise GatewayError("duplicate_client",
                               f"client {client_id} is already enrolled; revoke it first")
        if max_tier not in TIERS:
            raise GatewayError("registry_invalid", f"max_tier must be one of {', '.join(TIERS)}")
        allow = ["*"] if allow is None else list(allow)
        secret = _stdsecrets.token_urlsafe(32)
        entry = {"id": client_id, "machine": machine, "zone": self.zone,
                 "secret_sha256": _sha256(secret), "allow": allow,
                 "max_tier": max_tier, "revoked": None}
        _validate_client(entry, self.zone)
        self.clients[client_id] = entry
        return secret

    def revoke(self, client_id):
        c = self.client(client_id)
        if not c.get("revoked"):
            c["revoked"] = now_iso()

    # -- persistence -------------------------------------------------------------------

    def to_dict(self):
        out = {"schema": 1, "zone": self.zone}
        out.update(self._extra)
        out["connections"] = list(self.connections.values())
        out["clients"] = list(self.clients.values())
        return out

    def save(self, path):
        write_json(path, self.to_dict(), mode=0o600)
