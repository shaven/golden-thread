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
import re
# The stdlib module, not lotrlib.secrets: inside a package an absolute import is absolute.
import secrets as _stdsecrets
from urllib.parse import urlsplit

from .errors import GatewayError
from .util import now_iso, read_json, write_json

CONN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*@[a-z0-9][a-z0-9._-]*$")
CLIENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TOKEN_REF = re.compile(r"^(file|store|keychain|env):")
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
    if scheme != "none":
        ref = auth.get("token_ref")
        if not isinstance(ref, str) or not TOKEN_REF.match(ref):
            # Deliberately does not quote the value: it may be a literal secret.
            raise _bad(f"{where}: auth.token_ref must be a secret ref "
                       "(file:, store:, keychain:, env:), never a literal token")
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
    if not isinstance(auth, dict) or auth.get("scheme") not in ("bearer", "none"):
        raise _bad(f"{where}: auth.scheme must be bearer or none for an mcp connection")
    if auth.get("scheme") == "bearer":
        ref = auth.get("token_ref")
        if not isinstance(ref, str) or not TOKEN_REF.match(ref):
            raise _bad(f"{where}: auth.token_ref must be a secret ref "
                       "(file:, store:, keychain:, env:), never a literal token")
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


def _validate_client(c, zone):
    if not isinstance(c, dict):
        raise _bad("client must be an object")
    cid = c.get("id")
    if not isinstance(cid, str) or not CLIENT_ID.match(cid):
        raise _bad(f"client id {cid!r} is invalid (letters, digits, . _ -)")
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
    def load(cls, path):
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
        if not isinstance(client_id, str) or not CLIENT_ID.match(client_id):
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
