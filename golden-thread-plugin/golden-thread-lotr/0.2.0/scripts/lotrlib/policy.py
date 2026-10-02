"""Which tier an operation falls in, and whether a tool and a client may carry it.

The tier is what the MCP tool split hangs on: `call_read` can never perform a write, and
`call_consent` is the only door for operations the owner wants a human to approve. The
classification order is fixed by the SPEC and matters: the owner's explicit per-connection
policy beats everything the profile or the HTTP method would infer, and anything we cannot
place is treated as a write (fail towards the more guarded tier, never towards read).
"""
import fnmatch
import re

from .errors import GatewayError

TIERS = ("read", "write", "consent")
_RANK = {t: i for i, t in enumerate(TIERS)}
_TOOL_MAX = {"call_read": "read", "call_write": "write", "call_consent": "consent"}
_GQL_COMMENT = re.compile(r"#[^\n]*")
_GQL_WORD = re.compile(r"[A-Za-z_]+")


def _forms(op):
    """The strings policy globs are matched against: the op name and "METHOD /path"."""
    forms = []
    if op.get("name"):
        forms.append(op["name"])
    method = (op.get("method") or "").upper()
    path = op.get("path")
    if method and path:
        forms.append(f"{method} {path}")
    return forms


def _hits(globs, forms):
    return any(fnmatch.fnmatchcase(f, g) for g in globs or () for f in forms)


def _graphql_tier(query):
    """mutation -> write; query, subscription, `{` shorthand or anything else -> read."""
    text = _GQL_COMMENT.sub("", query).lstrip()
    m = _GQL_WORD.match(text)
    return "write" if m and m.group(0) == "mutation" else "read"


def classify(conn, op):
    """Return "deny" | "consent" | "write" | "read" for `op` on `conn`, in SPEC order."""
    policy = conn.get("policy") or {}
    forms = _forms(op)
    for verdict in ("deny", "consent", "write", "read"):
        if _hits(policy.get(verdict), forms):
            return verdict
    if op.get("tier") in TIERS:
        return op["tier"]
    query = op.get("graphql_query")
    if isinstance(query, str) and query.strip():
        return _graphql_tier(query)
    method = (op.get("method") or "").upper()
    if method in ("GET", "HEAD"):
        return "read"
    return "write"   # POST/PUT/PATCH/DELETE, and anything unknown


def allowed_through(tool, tier):
    """True if `tool` may carry an operation of `tier`. "deny" passes through nothing."""
    ceiling = _TOOL_MAX.get(tool)
    if ceiling is None or tier not in _RANK:
        return False
    return _RANK[tier] <= _RANK[ceiling]


def check_client(client, conn_id, tier):
    """Raise unless `client` may use `conn_id` at `tier` (allow globs, then max_tier)."""
    cid = client.get("id", "?")
    if client.get("revoked"):
        raise GatewayError("client_revoked", f"client {cid} was revoked")
    if not any(fnmatch.fnmatchcase(conn_id, g) for g in client.get("allow") or ()):
        raise GatewayError("client_not_allowed",
                           f"client {cid} is not allowed to use connection {conn_id}")
    ceiling = client.get("max_tier")
    if tier not in _RANK or ceiling not in _RANK or _RANK[tier] > _RANK[ceiling]:
        raise GatewayError("tier_ceiling",
                           f"client {cid} is capped at {ceiling!r}; this operation is {tier!r}")
