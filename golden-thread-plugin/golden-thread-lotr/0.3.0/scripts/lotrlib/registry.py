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

from .errors import GatewayError
from .util import now_iso, read_json, write_json

CONN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*@[a-z0-9][a-z0-9._-]*$")
CLIENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# 0.3.0: the brokered schemes, resolved only by gt core's unlock authority (secrets.BROKERED).
OAUTH_TOKEN_REF = re.compile(r"^(sealed|store):[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
OAUTH_SECRET_NAME = re.compile(r"^(sealed|store):lotr-oauth-")     # what oauth.secret_name writes
TOKEN_REF = re.compile(r"^(file|store|keychain|env|sealed|sops|op|bw|vault|wincred):")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
AUTH_SCHEMES = ("bearer", "basic", "header", "none")
TRUST = ("T0", "T1", "T2")
TIERS = ("read", "write", "consent")
LOCAL_HOSTS = ("127.0.0.1", "localhost")
POLICY_KEYS = ("deny", "consent", "write", "read")


def _bad(msg):
    return GatewayError("registry_invalid", msg)


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


MCP_TRANSPORTS = ("http",)   # streamable HTTP, JSON or SSE-framed replies; stdio/legacy sse later


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
    if not isinstance(block.get("audience_check", True), bool):
        raise _bad(f"{where}: auth.oauth.audience_check must be true or false")
    mac = block.get("max_access_cache_s", 3600)
    if isinstance(mac, bool) or not isinstance(mac, (int, float)) or mac <= 0:
        raise _bad(f"{where}: auth.oauth.max_access_cache_s must be a positive number")


def _validate_mcp(entry, where):
    """An `mcp` connection (0.2.0): a downstream MCP endpoint behind SSO/OAuth, reached with the
    bearer token its local client already holds -- by REF, never a literal -- and refreshed by
    that client's own helper (`refresh_cmd`, an argv list run with no shell)."""
    if not isinstance(entry.get("identity"), str) or not entry.get("identity"):
        raise _bad(f"{where}: identity is required")
    _endpoint_ok(where, entry.get("endpoint"), entry.get("network"), "endpoint")
    if entry.get("transport") not in MCP_TRANSPORTS:
        raise _bad(f"{where}: transport must be one of {', '.join(MCP_TRANSPORTS)} in this "
                   "release (stdio and the legacy sse transport are not supported yet)")
    if entry.get("trust") not in TRUST:
        raise _bad(f"{where}: trust must be one of {', '.join(TRUST)}")
    auth = entry.get("auth")
    if not isinstance(auth, dict) or auth.get("scheme") not in ("bearer", "none", "oauth"):
        raise _bad(f"{where}: auth.scheme must be bearer, oauth or none for an mcp connection")
    _not_oauth_in_disguise(auth, where)
    if auth.get("scheme") == "oauth":
        _validate_oauth(entry, auth, where)
    if auth.get("scheme") == "bearer":
        ref = auth.get("token_ref")
        if not isinstance(ref, str) or not TOKEN_REF.match(ref):
            raise _bad(f"{where}: auth.token_ref must be a secret ref "
                       "(file:, store:, keychain:, env:, sealed:, sops:, op:, bw:, vault:, "
                       "wincred:), never a literal token")
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
    if not isinstance(c.get("secret_sha256"), str) or not SHA256_HEX.match(c["secret_sha256"]):
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
