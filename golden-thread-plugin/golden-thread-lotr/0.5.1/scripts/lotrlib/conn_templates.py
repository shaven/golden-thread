"""Shipped and per-home connection templates.

A connection template is a small, secret-free descriptor that pre-fills the inputs a human
would otherwise type on `lotr connect`: the MCP endpoint, the scopes, the audience/resource
posture, the trust tier, and the host(s) the connection is pinned to. It is NOT a registry
entry and NOT an `auth.oauth` block -- the issuer, the OAuth endpoints, the client_id and every
token/ref are discovered or minted live, per user, at connect time (see oauth.login). A template
therefore carries ZERO secret material, and that is enforced here on load, not merely documented.

Templates are read LIVE from three dirs, the exact idiom lotr already uses for recipes
(engine.py SHIPPED_RECIPES + home/recipes, recipes.load_recipes): the plugin's shipped
`templates/connections/`, the catalog bundled with the module (`templates/catalog/`, gt 0.21.4,
hash-checked) and the catalog folder `<home>/catalog/templates/` that `lotr template
update` fills (0.5.0) -- whichever has the higher catalog_version wins --, and the per-home `~/.config/gt-lotr/<zone>/connections/`, with a later
dir's template replacing an earlier one's by name (yours always wins). Nothing is ever copied
into a vault or anywhere else; a template is only consulted when creating a brand-new connection.

0.5.0 adds the kind `mcp-http` (a hosted MCP server with a static key), the pre-registered OAuth
client posture, plain `env` and `runtime` for stdio, and the `bridge` flag for the legacy
HTTP+SSE bridge. See template_tool.GUIDE for the field-by-field explanation.
"""
import json
import re
from pathlib import Path
from urllib.parse import urlparse

from .errors import GatewayError
from . import endpoint_health, runtimes, upstream
from .secretcheck import looks_like_secret

# where the plugin's shipped templates live, relative to this file (mirrors engine.SHIPPED_RECIPES):
#   lotrlib/conn_templates.py  ->  parents[2] is the version dir  ->  templates/connections
SHIPPED_CONNECTIONS = Path(__file__).resolve().parents[2] / "templates" / "connections"

_NAME = re.compile(r"^[a-z][a-z0-9_.-]*$")
_SCOPE = re.compile(r"^\S+$")          # a scope token carries no spaces (mirrors registry scopes)
_KIND = ("mcp-oauth", "mcp-stdio", "http", "mcp-http")   # stdio and http: 0.5.0; mcp-http: 0.5.0
ARCHETYPES = ("stdio-npx", "stdio-uvx", "stdio-docker", "stdio-binary", "mcp-oauth",
              "mcp-oauth-preregistered", "mcp-http-key", "http-rest", "sse-bridge")

# keys that could smuggle a credential into a shipped file -- refused outright
_SECRET_KEYS = ("token_ref", "client_secret", "client_secret_ref", "client_secret_file",
                "client_id", "access_token", "refresh_token", "secret", "password", "token",
                "auth", "oauth")

_TOP_KEYS = {"schema", "name", "summary", "kind", "connect", "network", "notes", "source",
             "archetype", "docs", "smoke"}
# kind-specific connect.* keys (0.5.0). "credential_ref" is a SUGGESTED secret ref name, never a value.
_STDIO_CONNECT_KEYS = {"command", "env_refs", "cwd", "trust", "identity", "description", "timeouts",
                       "policy", "env", "runtime", "bridge", "params",   # env, runtime, bridge, params: 0.5.0
                       "llm", "limits",                                  # llm, limits: 0.5.1
                       "health_tracking", "sub_endpoints", "sub_endpoint_arg", "health_expires_h"}   # sub-endpoint health: 0.22.0
_MCP_HTTP_CONNECT_KEYS = {"url", "auth_scheme", "auth_header", "auth_prefix", "credential_ref", "trust",
                          "identity", "description", "policy", "llm", "limits",     # llm, limits: 0.5.1
                          "health_tracking", "sub_endpoints", "sub_endpoint_arg", "health_expires_h"}   # sub-endpoint health: 0.22.0
_MCP_HTTP_SCHEMES = ("bearer", "header", "prefix", "none")    # prefix: 0.5.0 (Authorization: Token token=KEY)
# The text before the key in `Authorization: <prefix><key>` (0.5.0). One scheme word, then either a
# space or one `name=` parameter: "Token token=", "Token ", "Bearer ". Nothing else -- no quotes, no
# second parameter, no line break -- so the prefix can never carry a value of its own.
AUTH_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9._~+-]{0,31}( | [A-Za-z][A-Za-z0-9_-]{0,31}=)$")


def check_auth_prefix(prefix):
    """None when `prefix` is a usable Authorization prefix, else a plain-words reason."""
    if not isinstance(prefix, str) or not AUTH_PREFIX.match(prefix):
        return ("must be a scheme word followed by a space or by one name= parameter, e.g. "
                "\"Token token=\" or \"Token \" (the key is appended to it)")
    # each word on its own: the whole "Token token=" reads like a header value to the shape check
    if any(looks_like_secret("scheme", w) for w in prefix.replace("=", " ").split()):
        return "looks like a secret; the key itself comes from credential_ref"
    return None
_BRIDGES = ("sse",)
# environment names a plain `env` may never set: they change what a program loads or runs
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_ENV_FORBIDDEN = re.compile(r"^(LD_.*|DYLD_.*|PATH|NODE_OPTIONS|NODE_PATH|NODE_EXTRA_CA_CERTS|PYTHON.*|"
                            r"BASH_ENV|ENV|IFS|SHELL|HOME|USERPROFILE|SSL_CERT_.*|CURL_CA_BUNDLE|"
                            r"REQUESTS_CA_BUNDLE|HTTPS?_PROXY|ALL_PROXY|NO_PROXY)$", re.I)
_HEADER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
_HEADER_FORBIDDEN = {"host", "content-length", "content-type", "accept", "mcp-session-id",
                     "mcp-protocol-version", "transfer-encoding", "connection", "cookie", "user-agent",
                     "authorization"}
_HTTP_CONNECT_KEYS = {"base_url", "profile", "auth_scheme", "auth_header", "auth_user",
                      "credential_ref", "trust", "identity", "description", "policy",
                      "health", "locality", "provider",             # health, locality, provider: 0.5.1
                      "timeouts", "llm", "limits",                  # timeouts.call_s, llm, limits: 0.5.1
                      "health_tracking", "sub_endpoints", "sub_endpoint_arg", "health_expires_h"}   # sub-endpoint health: 0.22.0
_AUTH_SCHEMES = ("bearer", "basic", "header", "none")
_REF = re.compile(r"^(file|store|keychain|env|sealed|sops|op|bw|vault|wincred):\S+$")
_PROFILES = ("generic", "github", "jira-v3", "jira-v2", "graph")
_CONNECT_KEYS = {"url", "scopes", "offline_access", "skip_audience_check", "send_resource",
                 "redirect_host", "trust", "identity", "description",
                 "client_registration", "client_secret_required", "app_setup_url",
                 "redirect_port", "policy",                        # 0.5.0 (pre-registered app)
                 "auth_params",                                    # 0.5.0: allow-listed extra sign-in params
                 "health_tracking", "sub_endpoints", "sub_endpoint_arg", "health_expires_h"}   # sub-endpoint health: 0.22.0
_CLIENT_REGISTRATION = ("auto", "preregistered")
_LOOPBACK = ("127.0.0.1", "localhost", "::1")
_TRUST = ("T0", "T1", "T2")
_REDIRECT_HOSTS = ("127.0.0.1", "localhost")


def _invalid(src, msg):
    raise GatewayError("template_invalid", f"{src}: {msg}")


def _has_secret_key(obj):
    """True if any key anywhere in the structure is a credential-shaped key."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _SECRET_KEYS:
                return k
            found = _has_secret_key(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _has_secret_key(v)
            if found:
                return found
    return None


def validate(tpl, src="<template>"):
    if not isinstance(tpl, dict):
        _invalid(src, "a connection template must be a JSON object")
    # the zero-secret guarantee, enforced -- refuse a file that could carry a credential
    bad = _has_secret_key(tpl)
    if bad:
        raise GatewayError("template_has_secret",
                           f"{src}: a connection template may not contain {bad!r}; the client_id "
                           "and every token/ref are minted per user at connect time")
    extra = set(tpl) - _TOP_KEYS
    if extra:
        _invalid(src, f"unknown key(s): {', '.join(sorted(extra))}")
    if tpl.get("schema") != 1:
        _invalid(src, "'schema' must be 1")
    name = tpl.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        _invalid(src, "'name' must be a lowercase id like 'jira'")
    if not isinstance(tpl.get("summary"), str) or not tpl["summary"].strip():
        _invalid(src, "'summary' must be a non-empty string")
    if tpl.get("kind") not in _KIND:
        _invalid(src, f"'kind' must be one of {', '.join(_KIND)}")
    conn = tpl.get("connect")
    if not isinstance(conn, dict) or not conn:
        _invalid(src, "'connect' must be a non-empty object")
    _validate_top_extras(tpl, src)
    if tpl["kind"] == "mcp-stdio":
        return _validate_stdio(tpl, conn, src)
    if tpl["kind"] == "http":
        return _validate_http(tpl, conn, src)
    if tpl["kind"] == "mcp-http":
        return _validate_mcp_http(tpl, conn, src)
    extra = set(conn) - _CONNECT_KEYS
    if extra:
        _invalid(src, f"connect.* unknown key(s): {', '.join(sorted(extra))}")
    url = conn.get("url")
    if not isinstance(url, str):
        _invalid(src, "connect.url must be a string")
    u = urlparse(url)
    # https only; plain http is admitted for a loopback host alone (a local MCP server, and the
    # test world), and `lotr connect` still needs --allow-insecure-localhost to use it
    if not u.hostname or not (u.scheme == "https"
                              or (u.scheme == "http" and u.hostname in _LOOPBACK)):
        _invalid(src, "connect.url must be an https URL with a host (http only for loopback)")
    scopes = conn.get("scopes", [])
    if not isinstance(scopes, list) or not all(isinstance(s, str) and _SCOPE.match(s) for s in scopes):
        _invalid(src, "connect.scopes must be a list of scope tokens (no spaces)")
    for b in ("offline_access", "skip_audience_check", "send_resource"):
        if b in conn and not isinstance(conn[b], bool):
            _invalid(src, f"connect.{b} must be true or false")
    if "redirect_host" in conn and conn["redirect_host"] not in _REDIRECT_HOSTS:
        _invalid(src, f"connect.redirect_host must be one of {', '.join(_REDIRECT_HOSTS)}")
    if "trust" in conn and conn["trust"] not in _TRUST:
        _invalid(src, f"connect.trust must be one of {', '.join(_TRUST)}")
    for s in ("identity", "description"):
        if s in conn and not isinstance(conn[s], str):
            _invalid(src, f"connect.{s} must be a string")
    _validate_registration(conn, src)
    if "auth_params" in conn:                             # 0.5.0: access_type=offline, prompt=consent
        from .oauth import check_auth_params
        try:
            check_auth_params(conn["auth_params"])
        except GatewayError as e:
            _invalid(src, f"connect.{e.message}")
    _health_fields(conn, src)
    if "policy" in conn:                                  # 0.5.0: consent globs for an OAuth server too
        pol = conn["policy"]
        if not isinstance(pol, dict) or set(pol) - {"deny", "consent", "write", "read"}:
            _invalid(src, "connect.policy may only hold deny, consent, write, read (lists of globs)")
        for k, g in pol.items():
            if not isinstance(g, list) or not all(isinstance(x, str) for x in g):
                _invalid(src, f"connect.policy.{k} must be a list of glob strings")
        _check_read_globs(pol, src)
    net = tpl.get("network", {})
    if not isinstance(net, dict):
        _invalid(src, "'network' must be an object")
    hosts = net.get("hosts", [])
    if not isinstance(hosts, list) or not all(isinstance(h, str) and h for h in hosts):
        _invalid(src, "network.hosts must be a list of non-empty host strings")
    # the url's host must be one the template vouches for
    if hosts and u.hostname not in hosts:
        _invalid(src, f"connect.url host {u.hostname!r} is not in network.hosts")
    notes = tpl.get("notes", [])
    if not isinstance(notes, list) or not all(isinstance(n, str) for n in notes):
        _invalid(src, "'notes' must be a list of strings")
    return tpl


# A `policy.read` entry makes a tool a read whatever its name says, so it may not be a net: an exact
# name, or one trailing `*` after a read word (browse_*). Anything wider (`*`, `get*`, `*_read`)
# would also clear a destructive tool. 0.22.0 phase 139.
_READ_GLOB = re.compile(r"^(get|list|search|find|read|fetch|query|describe|show|browse)_\*$")


def _check_read_globs(pol, src):
    for g in pol.get("read", []):
        if any(c in g for c in "*?[") and not _READ_GLOB.match(g):
            _invalid(src, f"connect.policy.read entry {g!r} is too wide: name the tool exactly, or use "
                          "one trailing * after a read word (browse_*)")


def _health_fields(conn, src):
    why = endpoint_health.problem(conn)             # 0.22.0: health_tracking, sub_endpoints, ...
    if why:
        _invalid(src, f"connect.{why}")


def _common(tpl, conn, src):
    _health_fields(conn, src)
    for k, check in (("llm", upstream.llm_problem), ("limits", upstream.limits_problem)):
        if k in conn and check(conn[k]):                    # 0.5.1: one definition, upstream.py
            _invalid(src, f"connect.{k}: {check(conn[k])}")
    pol = conn.get("policy", {})
    if not isinstance(pol, dict) or set(pol) - {"deny", "consent", "write", "read"}:
        _invalid(src, "connect.policy may only hold deny, consent, write, read (lists of globs)")
    for k, globs in pol.items():
        if not isinstance(globs, list) or not all(isinstance(g, str) for g in globs):
            _invalid(src, f"connect.policy.{k} must be a list of glob strings")
    _check_read_globs(pol, src)
    if "trust" in conn and conn["trust"] not in _TRUST:
        _invalid(src, f"connect.trust must be one of {', '.join(_TRUST)}")
    for k in ("identity", "description"):
        if k in conn and not isinstance(conn[k], str):
            _invalid(src, f"connect.{k} must be a string")
    notes = tpl.get("notes", [])
    if not isinstance(notes, list) or not all(isinstance(n, str) for n in notes):
        _invalid(src, "'notes' must be a list of strings")
    if not isinstance(tpl.get("network", {}), dict):
        _invalid(src, "'network' must be an object")


def _validate_top_extras(tpl, src):
    arch = tpl.get("archetype")
    if arch is not None and arch not in ARCHETYPES:
        _invalid(src, f"'archetype' must be one of {', '.join(ARCHETYPES)}")
    docs = tpl.get("docs")
    if docs is not None:
        u = urlparse(docs) if isinstance(docs, str) else None
        if not u or u.scheme != "https" or not u.hostname or u.username or u.password:
            _invalid(src, "'docs' must be an https URL (the vendor's own documentation page)")
    smoke = tpl.get("smoke")
    if smoke is not None:
        if not isinstance(smoke, dict) or set(smoke) - {"op", "args"} or \
                not isinstance(smoke.get("op"), str) or not smoke["op"].strip():
            _invalid(src, "'smoke' must be {\"op\": \"<read operation>\", \"args\": {...}}: the one "
                          "harmless read `lotr setup` makes to prove the connection works")
        if not isinstance(smoke.get("args", {}), dict):
            _invalid(src, "smoke.args must be an object")


def _validate_registration(conn, src):
    reg = conn.get("client_registration", "auto")
    if reg not in _CLIENT_REGISTRATION:
        _invalid(src, f"connect.client_registration must be one of {', '.join(_CLIENT_REGISTRATION)}")
    extras = [k for k in ("client_secret_required", "app_setup_url", "redirect_port") if k in conn]
    if extras and reg != "preregistered":
        _invalid(src, f"connect.{extras[0]} only applies when client_registration is "
                      "\"preregistered\" (this server's app is created by you, in its settings)")
    if "client_secret_required" in conn and not isinstance(conn["client_secret_required"], bool):
        _invalid(src, "connect.client_secret_required must be true or false")
    if "app_setup_url" in conn:
        u = urlparse(conn["app_setup_url"]) if isinstance(conn["app_setup_url"], str) else None
        if not u or u.scheme != "https" or not u.hostname or u.username or u.password:
            _invalid(src, "connect.app_setup_url must be an https URL (where you create the app)")
    if "redirect_port" in conn:
        p = conn["redirect_port"]
        if isinstance(p, bool) or not isinstance(p, int) or not 1024 <= p <= 65535:
            _invalid(src, "connect.redirect_port must be a port from 1024 to 65535 (the loopback "
                          "port you register as the redirect address)")


_PARAM_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]{0,31})\}")
PARAM_KINDS = ("path", "string")


def _validate_params(conn, cmd, src):
    """connect.params: values only the user can supply (a folder the server may use, a repository).
    The command names each as {name}; `lotr setup` asks for them and substitutes them. They are NOT
    for secrets: a credential is an env_refs reference."""
    params = conn.get("params", {})
    if not isinstance(params, dict):
        _invalid(src, "connect.params must be an object of NAME: {prompt, kind, default}")
    for k, v in params.items():
        if not isinstance(k, str) or not _PARAM_NAME.match(k):
            _invalid(src, f"connect.params name {str(k)[:40]!r} must be a lowercase word like repo_dir")
        if not isinstance(v, dict) or set(v) - {"prompt", "kind", "default", "resolver"}:
            _invalid(src, f"connect.params.{k} may only hold prompt, kind, default and resolver")
        if v.get("resolver", "gt-scripts") != "gt-scripts":          # 0.5.1: the only resolver
            _invalid(src, f"connect.params.{k}.resolver must be gt-scripts")
        if not isinstance(v.get("prompt"), str) or not v["prompt"].strip():
            _invalid(src, f"connect.params.{k}.prompt must say, in a sentence, what to enter")
        if v.get("kind", "string") not in PARAM_KINDS:
            _invalid(src, f"connect.params.{k}.kind must be one of {', '.join(PARAM_KINDS)}")
        if "default" in v and (not isinstance(v["default"], str) or looks_like_secret(k, v["default"])):
            _invalid(src, f"connect.params.{k}.default must be a plain string (not a secret)")
    env = conn.get("env") if isinstance(conn.get("env"), dict) else {}
    # 0.5.0: a plain env VALUE may use {name} too (a region, a workspace URL); an env_refs value
    # never may -- a secret is a fixed reference the template names, not something typed in
    for k, v in (conn.get("env_refs") or {}).items():
        if isinstance(v, str) and _PLACEHOLDER.search(v):
            _invalid(src, f"connect.env_refs.{k} may not use a {{param}}: a secret is a reference")
    used = set(_PLACEHOLDER.findall(" ".join(cmd + [v for v in env.values() if isinstance(v, str)])))
    for u in sorted(used - set(params)):
        _invalid(src, f"connect.command or connect.env uses {{{u}}} but connect.params does not declare it")
    for d in sorted(set(params) - used):
        _invalid(src, f"connect.params declares {d} but neither connect.command nor connect.env "
                      f"uses {{{d}}}")


def apply_params(command, params, values):
    """-> the command with every {name} replaced. `values` is name -> string from the user; a missing
    value falls back to the declared default, else GatewayError(param_missing). A path must exist
    and becomes absolute. Never shells out: the result is still an argv list."""
    vals = param_values(params, values)
    return [_PLACEHOLDER.sub(lambda m: vals[m.group(1)], x) for x in command]


def apply_env_params(env, params, values):
    """-> the plain env with every {name} in a VALUE replaced (0.5.0). Each finished value is checked
    again: a variable whose value now looks like a secret is refused (param_invalid), because a
    credential belongs in env_refs as a reference, never typed into a parameter."""
    vals = param_values(params, values)
    out = {}
    for k, v in (env or {}).items():
        nv = _PLACEHOLDER.sub(lambda m: vals[m.group(1)], v) if isinstance(v, str) else v
        if nv != v and looks_like_secret(k, nv):
            raise GatewayError("param_invalid", f"env {k}: the value looks like a secret; a credential "
                               "is an env_refs reference, never a parameter")
        out[k] = nv
    return out


def param_values(params, values):
    """name -> checked value for every declared param (see apply_params)."""
    import os
    vals = {}
    for k, spec in (params or {}).items():
        v = (values or {}).get(k, spec.get("default"))
        if (v is None or v == "") and spec.get("resolver") == "gt-scripts":
            from . import gt_locate                    # 0.5.1: the newest installed gt's scripts
            v = str(gt_locate.scripts_dir())
        if v is None or v == "":
            raise GatewayError("param_missing", f"{k}: {spec['prompt']}",
                               hints=[f"pass --param {k}=VALUE"])
        if not isinstance(v, str) or "\x00" in v or "\n" in v or len(v) > 500:
            raise GatewayError("param_invalid", f"{k}: the value must be one short line")
        if looks_like_secret(k, v):
            raise GatewayError("param_invalid", f"{k}: that looks like a secret; a parameter is for "
                               "a folder or a name, a credential is a reference")
        if spec.get("kind") == "path":
            full = os.path.abspath(os.path.expanduser(v))
            if not os.path.exists(full):
                raise GatewayError("param_invalid", f"{k}: {v} does not exist on this machine")
            v = full
        vals[k] = v
    return vals


def _validate_plain_env(conn, src):
    env = conn.get("env", {})
    if not isinstance(env, dict):
        _invalid(src, "connect.env must be an object of VARIABLE: plain value (not a secret)")
    for k, v in env.items():
        if not isinstance(k, str) or not _ENV_NAME.match(k):
            _invalid(src, f"connect.env name {str(k)[:40]!r} is not a valid variable name")
        if _ENV_FORBIDDEN.match(k):
            _invalid(src, f"connect.env.{k} may not be set by a template (it changes what the "
                          "program loads or where its traffic goes)")
        if not isinstance(v, str):
            _invalid(src, f"connect.env.{k} must be a string")
        why = looks_like_secret(k, v)
        if why:
            raise GatewayError("template_has_secret",
                               f"{src}: connect.env.{k} looks like a secret ({why}); a credential "
                               "goes under env_refs as a reference like store:name")
    both = set(env) & set(conn.get("env_refs") or {})
    if both:
        _invalid(src, f"connect.env and connect.env_refs both set {sorted(both)[0]}")


def _validate_stdio(tpl, conn, src):
    extra = set(conn) - _STDIO_CONNECT_KEYS
    if extra:
        _invalid(src, f"connect.* unknown key(s) for kind mcp-stdio: {', '.join(sorted(extra))}")
    cmd = conn.get("command")
    if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) and x for x in cmd):
        _invalid(src, "connect.command must be a list of program arguments, e.g. "
                      '["npx", "-y", "some-mcp-server"] (no shell, no quoting)')
    env = conn.get("env_refs", {})
    if not isinstance(env, dict):
        _invalid(src, "connect.env_refs must be an object of VARIABLE: secret ref")
    for k, v in env.items():
        if not isinstance(v, str) or not _REF.match(v):
            _invalid(src, f"connect.env_refs.{k} must be a secret ref like store:name, "
                          "never the value itself")
    _validate_plain_env(conn, src)
    _validate_params(conn, cmd, src)
    if "runtime" in conn:
        rt = conn["runtime"]
        if rt not in runtimes.RUNTIMES:
            _invalid(src, f"connect.runtime must be one of {', '.join(runtimes.RUNTIMES)}")
        if not runtimes.consistent(rt, cmd):
            _invalid(src, f"connect.runtime is {rt!r} but the command starts with {cmd[0]!r}")
    if "bridge" in conn and conn["bridge"] not in _BRIDGES:
        _invalid(src, f"connect.bridge must be one of {', '.join(_BRIDGES)} (a stdio command that "
                      "bridges a legacy HTTP+SSE server)")
    if "cwd" in conn and not isinstance(conn["cwd"], str):
        _invalid(src, "connect.cwd must be a string path")
    if "timeouts" in conn and not isinstance(conn["timeouts"], dict):
        _invalid(src, "connect.timeouts must be an object (start_s, call_s, idle_s)")
    _common(tpl, conn, src)
    return tpl


def _validate_http(tpl, conn, src):
    extra = set(conn) - _HTTP_CONNECT_KEYS
    if extra:
        _invalid(src, f"connect.* unknown key(s) for kind http: {', '.join(sorted(extra))}")
    url = conn.get("base_url")
    u = urlparse(url) if isinstance(url, str) else None
    if not u or not u.hostname or not (u.scheme == "https"
                                       or (u.scheme == "http" and u.hostname in _LOOPBACK)):
        _invalid(src, "connect.base_url must be an https URL with a host (http only for loopback)")
    if u.username or u.password:
        _invalid(src, "connect.base_url must not carry credentials")
    if conn.get("profile", "generic") not in _PROFILES:
        _invalid(src, f"connect.profile must be one of {', '.join(_PROFILES)}")
    if "health" in conn and not (isinstance(conn["health"], str)
                                 and re.fullmatch(r"GET /[A-Za-z0-9._/-]{0,80}", conn["health"])):
        _invalid(src, "connect.health must look like 'GET /health' (a read-only path)")
    if conn.get("locality", "remote") not in ("local", "remote"):
        _invalid(src, "connect.locality must be local or remote")
    if "provider" in conn and upstream.provider_problem(conn["provider"]):
        _invalid(src, f"connect.{upstream.provider_problem(conn['provider'])}")
    if "timeouts" in conn and upstream.timeouts_problem(conn["timeouts"]):
        _invalid(src, f"connect.{upstream.timeouts_problem(conn['timeouts'])}")
    scheme = conn.get("auth_scheme")
    if scheme not in _AUTH_SCHEMES:
        _invalid(src, f"connect.auth_scheme must be one of {', '.join(_AUTH_SCHEMES)}")
    if scheme == "header" and not conn.get("auth_header"):
        _invalid(src, "connect.auth_header (the header name) is required for auth_scheme header")
    if scheme == "basic" and not conn.get("auth_user"):
        _invalid(src, "connect.auth_user is required for auth_scheme basic")
    ref = conn.get("credential_ref")
    if ref is not None and (not isinstance(ref, str) or not _REF.match(ref)):
        _invalid(src, "connect.credential_ref must be a secret ref like store:name, never a value")
    hosts = (tpl.get("network") or {}).get("hosts", [])
    if not isinstance(hosts, list) or not all(isinstance(h, str) and h for h in hosts):
        _invalid(src, "network.hosts must be a list of non-empty host strings")
    if hosts and u.hostname not in hosts:
        _invalid(src, f"connect.base_url host {u.hostname!r} is not in network.hosts")
    _common(tpl, conn, src)
    return tpl


def _validate_mcp_http(tpl, conn, src):
    """A hosted MCP server (streamable HTTP) with a static key: bearer or a named header, the
    credential a REFERENCE only. Applied with `lotr add-mcp NAME --from-template T`."""
    extra = set(conn) - _MCP_HTTP_CONNECT_KEYS
    if extra:
        _invalid(src, f"connect.* unknown key(s) for kind mcp-http: {', '.join(sorted(extra))}")
    url = conn.get("url")
    u = urlparse(url) if isinstance(url, str) else None
    if not u or not u.hostname or not (u.scheme == "https"
                                       or (u.scheme == "http" and u.hostname in _LOOPBACK)):
        _invalid(src, "connect.url must be an https URL with a host (http only for loopback)")
    if u.username or u.password:
        _invalid(src, "connect.url must not carry credentials")
    scheme = conn.get("auth_scheme")
    if scheme not in _MCP_HTTP_SCHEMES:
        _invalid(src, f"connect.auth_scheme must be one of {', '.join(_MCP_HTTP_SCHEMES)}")
    if scheme == "header":
        h = conn.get("auth_header")
        if not isinstance(h, str) or not _HEADER_NAME.match(h):
            _invalid(src, "connect.auth_header (the header name, e.g. X-Api-Key) is required for "
                          "auth_scheme header")
        if h.lower() in _HEADER_FORBIDDEN:
            _invalid(src, f"connect.auth_header {h!r} is a header LOTR sets itself; use "
                          "auth_scheme bearer for Authorization")
    elif "auth_header" in conn:
        _invalid(src, "connect.auth_header only applies to auth_scheme header")
    if scheme == "prefix":
        why = check_auth_prefix(conn.get("auth_prefix"))
        if why:
            _invalid(src, f"connect.auth_prefix (required for auth_scheme prefix) {why}")
    elif "auth_prefix" in conn:
        _invalid(src, "connect.auth_prefix only applies to auth_scheme prefix")
    ref = conn.get("credential_ref")
    if ref is not None and (not isinstance(ref, str) or not _REF.match(ref)):
        _invalid(src, "connect.credential_ref must be a secret ref like store:name, never a value")
    if scheme == "none" and ref:
        _invalid(src, "connect.credential_ref is set but auth_scheme is none")
    hosts = (tpl.get("network") or {}).get("hosts", [])
    if not isinstance(hosts, list) or not all(isinstance(h, str) and h for h in hosts):
        _invalid(src, "network.hosts must be a list of non-empty host strings")
    if hosts and u.hostname not in hosts:
        _invalid(src, f"connect.url host {u.hostname!r} is not in network.hosts")
    _common(tpl, conn, src)
    return tpl


def catalog_dir(home):
    """Where `lotr template update` puts catalog templates (and facts/ beside it)."""
    return Path(home) / "catalog" / "templates"


# The catalog BUNDLED with the module (gt 0.21.4, owner decision 2026-10-14): a byte-for-byte
# snapshot of the repository's lotr-templates/ (catalog.json, templates/, facts/), written by
# `dev/lotr_catalog.py build|bundle` and checked by `lotr_catalog.py check`, so the two cannot
# drift. It lives under templates/ because that is a folder the installer copies. Right after an
# install, with no network, `lotr template list` and `lotr setup NAME` find these templates. A
# bundled template is used only when its file AND its fact sheet match the sha256 the bundled
# catalog.json lists; one that does not is not offered at all.
BUNDLED_CATALOG = Path(__file__).resolve().parents[2] / "templates" / "catalog"


def bundled_catalog():
    """-> the bundled catalog.json as a dict, or None when there is none (or it is unreadable)."""
    try:
        cat = json.loads((BUNDLED_CATALOG / "catalog.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return cat if isinstance(cat, dict) and isinstance(cat.get("templates"), list) else None


def bundled_version():
    cat = bundled_catalog() or {}
    v = cat.get("catalog_version")
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def bundled_verified(home=None):
    """-> {name: template path} for the bundled templates whose file and fact sheet match the
    hashes in the bundled catalog.json. Anything else in the folder is ignored. `home` is the LOTR
    home whose rollover chain (catalog-keys.json) decides which signing keys are trusted; None keeps
    the old lookup (LOTR_HOME, else the personal zone)."""
    import hashlib
    import hmac
    cat = bundled_catalog()
    out = {}
    if not cat:
        return out
    from . import catalog as _catalog               # lazy: catalog imports this module
    if _catalog.bundled_refusal(home) is not None:      # phase 112: a pinned key and no valid signature
        return out
    for e in cat["templates"]:
        if not isinstance(e, dict):
            continue
        name, f, fs = e.get("name"), e.get("file"), e.get("facts")
        if not (isinstance(name, str) and _NAME.match(name) and f == f"templates/{name}.json"
                and fs == f"facts/{name}.facts.json"):
            continue
        tp, fp = BUNDLED_CATALOG / f, BUNDLED_CATALOG / fs
        try:
            ok = (hmac.compare_digest(hashlib.sha256(tp.read_bytes()).hexdigest(),
                                      str(e.get("sha256") or ""))
                  and hmac.compare_digest(hashlib.sha256(fp.read_bytes()).hexdigest(),
                                          str(e.get("facts_sha256") or "")))
        except OSError:
            ok = False
        if ok:
            out[name] = tp
    return out


def fetched_version(home):
    """The catalog_version `lotr template update` last installed in this home, or 0."""
    try:
        v = json.loads((Path(home) / "catalog" / "state.json").read_text(encoding="utf-8")).get(
            "catalog_version")
    except (OSError, ValueError, UnicodeDecodeError, AttributeError):
        return 0
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def catalog_dirs(home):
    """The bundled and the fetched catalog folders, the winner LAST: a fetched catalog with a
    higher catalog_version wins over the bundled one; a lower or equal one never replaces it."""
    bundled, fetched = BUNDLED_CATALOG / "templates", catalog_dir(home)
    return [bundled, fetched] if fetched_version(home) > bundled_version() else [fetched, bundled]


def template_dirs(home):
    """shipped < catalog (bundled and fetched, newer catalog_version wins) < yours: a later dir's
    template wins by name."""
    return [SHIPPED_CONNECTIONS, *catalog_dirs(home), Path(home) / "connections"]


def _is_bundled(d):
    try:
        return Path(d).resolve() == (BUNDLED_CATALOG / "templates").resolve()
    except OSError:
        return False


def template_files(d, home=None):
    """The template files in one folder: every *.json except a fact sheet (NAME.facts.json), which
    a research run may leave beside its template and is not a template (0.5.0). In the bundled
    catalog folder, only the templates that match the bundled catalog's hashes."""
    files = [f for f in sorted(Path(d).glob("*.json")) if not f.name.endswith(".facts.json")]
    if _is_bundled(d):
        ok = {p.name for p in bundled_verified(home).values()}
        files = [f for f in files if f.name in ok]
    return files


def load_templates(dirs, home=None):
    """Every *.json template under `dirs`; a later dir's template replaces an earlier one by name.
    `home` picks the rollover chain that vouches for the bundled catalog (see bundled_verified)."""
    by_name = {}
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for f in template_files(d, home):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (ValueError, UnicodeDecodeError) as e:
                raise GatewayError("template_invalid", f"{f}: not valid JSON: {e}")
            validate(data, str(f))
            data = dict(data, source=str(f))
            by_name[data["name"]] = data
    return by_name


def get_template(home, name):
    """Resolve one template by name across shipped + per-home dirs, or raise with hints."""
    import difflib
    templates = load_templates(template_dirs(home), home)
    tpl = templates.get(name)
    if tpl is None:
        hints = difflib.get_close_matches(name, list(templates), n=3)
        from .util import wrap_list
        raise GatewayError("unknown_template", f"no connection template {name!r}",
                           hints=([f"did you mean: {', '.join(hints)}"] if hints else
                                  wrap_list("available: ", sorted(templates)) if templates else
                                  ["available: (none)"]))
    return tpl
