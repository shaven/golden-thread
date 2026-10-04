"""Which tier an operation falls in, and whether a tool and a client may carry it.

The tier is what the MCP tool split hangs on: `call_read` can never perform a write, and
`call_consent` is the only door for operations the owner wants a human to approve. The
classification order is fixed by the SPEC and matters: the owner's explicit per-connection
policy beats everything the profile or the HTTP method would infer, and anything we cannot
place is treated as a write (fail towards the more guarded tier, never towards read).
"""
import fnmatch
import re
import urllib.parse

from .errors import GatewayError

TIERS = ("read", "write", "consent")
_RANK = {t: i for i, t in enumerate(TIERS)}
_TOOL_MAX = {"call_read": "read", "call_write": "write", "call_consent": "consent"}
_GQL_NAME = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_GQL_PAIRS = {"}": "{", ")": "(", "]": "["}
_GQL_IGNORED = " \t\r\n,\ufeff"
_GQL_NUMBER = re.compile(r"-?[0-9][0-9A-Za-z_.+-]*")


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


def _gql_tokens(text):
    """Tokens of a GraphQL document with comments and string literals removed.

    Returns a list of (kind, value, depth_before) or None if the text is not lexically
    valid GraphQL (unterminated string, stray character, unbalanced brackets) so the caller
    can fail closed. Strings become a single "str" token, so a keyword inside one is data.
    """
    toks, stack, i, n = [], [], 0, len(text)
    while i < n:
        c = text[i]
        if c in _GQL_IGNORED:
            i += 1
        elif c == "#":
            while i < n and text[i] not in "\r\n":
                i += 1
        elif c == '"':
            if text.startswith('"""', i):
                j = i + 3
                while True:
                    j = text.find('"""', j)
                    if j < 0:
                        return None
                    if text[j - 1] == "\\":      # \""" is an escaped delimiter in a block string
                        j += 3
                        continue
                    break
                i = j + 3
            else:
                j = i + 1
                while j < n and text[j] != '"':
                    if text[j] in "\r\n":
                        return None
                    j += 2 if text[j] == "\\" else 1
                if j >= n:
                    return None
                i = j + 1
            toks.append(("str", "", len(stack)))
        elif c in "{([":
            toks.append(("open", c, len(stack)))
            stack.append(c)
            i += 1
        elif c in "})]":
            if not stack or stack.pop() != _GQL_PAIRS[c]:
                return None
            toks.append(("close", c, len(stack)))
            i += 1
        elif c in "!$&:=@|":
            toks.append(("punct", c, len(stack)))
            i += 1
        elif text.startswith("...", i):
            toks.append(("punct", "...", len(stack)))
            i += 3
        else:
            m = _GQL_NAME.match(text, i) or _GQL_NUMBER.match(text, i)
            if not m:
                return None     # includes every non-ASCII character outside a string
            toks.append(("name", m.group(0), len(stack)))
            i = m.end()
    return None if stack else toks


def _graphql_tier(query):
    """read only for a document that is provably one query (plus fragment definitions).

    Fail closed: the whole document is lexed (comments and string literals stripped) and its
    top-level definitions are counted. `read` needs exactly one operation, it must be a
    `query` or the anonymous `{...}` shorthand, and every other top-level definition must be
    a `fragment`. A mutation or subscription anywhere at depth 0, several operations, a
    fragment-first document hiding a mutation, or anything unparsable -> `write`.
    """
    if not isinstance(query, str):
        return "write"
    toks = _gql_tokens(query)
    if not toks:
        return "write"
    ops, i = 0, 0
    while i < len(toks):
        kind, val, _ = toks[i]
        if kind == "open" and val == "{":
            ops += 1                               # anonymous query shorthand
        elif kind == "name" and val in ("query", "fragment"):
            if val == "query":
                ops += 1
            i += 1
            while i < len(toks) and not (toks[i][0] == "open" and toks[i][1] == "{"
                                         and toks[i][2] == 0):
                if toks[i][2] == 0 and toks[i][0] == "str":
                    return "write"
                i += 1
            if i >= len(toks):
                return "write"                     # a header with no selection set
        else:
            return "write"     # mutation, subscription, extend, schema ..., or junk
        depth0 = toks[i][2]
        i += 1
        while i < len(toks) and not (toks[i][0] == "close" and toks[i][2] == depth0):
            i += 1
        i += 1                                     # the matching "}"
    return "read" if ops == 1 else "write"


# GraphQL fields that merge a pull request: a mutation naming one needs the owner's consent.
_GQL_MERGE_FIELDS = ("mergePullRequest", "enablePullRequestAutoMerge")


def _graphql_final(query):
    """_graphql_tier plus consent: a document that is not provably a read and names a merge
    mutation is `consent`; every other mutation stays `write`. Unparsable text that mentions a
    merge field anywhere is `consent` too (fail towards the more guarded tier)."""
    tier = _graphql_tier(query)
    if tier == "read" or not isinstance(query, str):
        return tier
    toks = _gql_tokens(query)
    if toks is None:
        return "consent" if any(f in query for f in _GQL_MERGE_FIELDS) else "write"
    if any(k == "name" and v in _GQL_MERGE_FIELDS for k, v, _ in toks):
        return "consent"
    return "write"


# Raw endpoints that do what a curated consent op does, spelled another way. Matched on the
# normalised path (query dropped, percent-decoded, lower-cased, "//" collapsed, trailing "/"
# dropped) for every connection profile, so a lookalike path on a generic REST connection is
# guarded too. Anything else destructive on a generic connection stays `write` unless the owner
# adds `policy.consent` globs.
_TWIN_PATHS = tuple(re.compile(p) for p in (
    r"^/repos/[^/]+/[^/]+/pulls/[^/]+/merge$",                          # GitHub merge
    r"^/repositories/[^/]+/pulls/[^/]+/merge$",                         # ... by repository id
    r"^/(me|users/[^/]+)/sendmail$",                                    # Graph send
    r"^/(me|users/[^/]+)(/mailfolders/[^/]+)*/messages/[^/]+/"
    r"(send|forward|reply|replyall|createreply|createforward)$",        # Graph send-ish
    r"^(/v[0-9.]+)?/\$batch$",                                          # Graph batch can carry sends
))


def _norm_path(path):
    p = urllib.parse.urlsplit(str(path or "")).path
    for _ in range(4):
        q = urllib.parse.unquote(p)
        if q == p:
            break
        p = q
    p = re.sub(r"/+", "/", p.lower())
    return p.rstrip("/") or "/"


def _consent_twin(op):
    method = (op.get("method") or "").upper()
    if method == "DELETE":
        return True
    if method in ("", "GET", "HEAD"):
        return False
    path = _norm_path(op.get("path"))
    return any(rx.match(path) for rx in _TWIN_PATHS)


def classify(conn, op):
    """Return "deny" | "consent" | "write" | "read" for `op` on `conn`, in SPEC order."""
    policy = conn.get("policy") or {}
    forms = _forms(op)
    for verdict in ("deny", "consent", "write", "read"):
        if _hits(policy.get(verdict), forms):
            return verdict
    if op.get("tier") in TIERS:
        return op["tier"]
    if _consent_twin(op):
        return "consent"
    query = op.get("graphql_query")
    if isinstance(query, str) and query.strip():
        return _graphql_final(query)
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
