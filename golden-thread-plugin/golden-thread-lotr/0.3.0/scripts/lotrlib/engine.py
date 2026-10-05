"""The engine: one registry, one index, one policy -- behind every front end.

Both the MCP shim and the `lotr` CLI reach this object through the daemon, so identity
stamping, tier enforcement, shaping and the audit log are written once (ADR-1).

The four verbs the outside world sees map onto two methods:
  find(query)                         -> search the index (or list connections on "")
  call(tool, connection, op, args)    -> tool is call_read | call_write | call_consent

The GATEWAY assigns an op's tier, never the model. The model only picks which tool to use; an
op sent through a tool below its tier is refused with a hint naming the right one.

0.3.0 (gt unlock consumer): `call` takes the caller's `subject` -- the KERNEL peer of the local
socket / pipe, never anything a caller says about itself; None for hub HTTP clients, which
keep their bearer + revocation model. While gt unlock is on, a local caller is checked against
the authority for scope `lotr:<connection>:<tier>` BEFORE policy.check_client, and the grant id
goes into every audit line, refusals included. While it is off, nothing here changes.
"""
import hashlib
import json
import re
import os
import secrets as _pysecrets
import threading
import time
from pathlib import Path

from . import audit, confirm as confirm_mod, policy, profiles, recipes as recipes_mod, shaping
from . import unlock as unlock_mod
from .config import load_settings
from .conn_http import HttpConnection
from .conn_mcp import McpConnection
from .errors import GatewayError
from .index import Index, build_docs
from .registry import Registry

TOOLS = ("call_read", "call_write", "call_consent")
TIER_RANK = {"read": 0, "write": 1, "consent": 2}
SHIPPED_RECIPES = Path(__file__).resolve().parents[2] / "templates" / "recipes"
LOCAL_CLIENT = "local"
CURSOR_TTL = 600        # seconds a next-page cursor stays valid
CURSOR_MAX = 256        # cursors held in memory at once


def _max_tier(a, b):
    return a if TIER_RANK[a] >= TIER_RANK[b] else b


def _args_hash(args):
    blob = json.dumps(args or {}, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")


def _one_line(text):
    """`text` with every newline, tab and other control character made one space."""
    return " ".join(_CONTROL.sub(" ", str(text)).split())

class Engine:
    def __init__(self, home, *, secret_resolver=None, connection_factory=None, dialog=None,
                 unlock=None):
        self.home = Path(home)
        self._dialog = dialog            # tests inject a fake; None = the real macOS dialog
        self._unlock = unlock or unlock_mod   # gt core's authority, through lotrlib.unlock
        self._secret_resolver = secret_resolver
        self._connection_factory = connection_factory or HttpConnection
        self._lock = threading.RLock()
        self._cursors = {}       # opaque id -> {"raw", "conn", "client", "op", "exp"}; memory only
        self._stamp = None
        self._load()

    # -- loading ---------------------------------------------------------------------------

    def _watched(self):
        paths = [self.home / "gateway.json", self.home / "registry.json"]
        rdir = self.home / "recipes"
        if rdir.is_dir():
            paths.extend(sorted(rdir.glob("*.json")))
        return paths

    def _current_stamp(self):
        out = []
        for p in self._watched():
            try:
                st = p.stat()
                out.append((str(p), st.st_mtime_ns, st.st_size))
            except FileNotFoundError:
                out.append((str(p), None, None))
        return tuple(out)

    def _load(self):
        with self._lock:
            self.settings = load_settings(self.home)
            self.registry = Registry.load(self.home / "registry.json")
            if self.registry.zone != self.settings["zone"]:
                raise GatewayError("zone_mismatch",
                                   f"registry zone {self.registry.zone!r} != gateway zone "
                                   f"{self.settings['zone']!r}; one daemon serves one zone")
            dirs = [SHIPPED_RECIPES, self.home / "recipes"]
            self.recipes = recipes_mod.load_recipes([d for d in dirs if d.is_dir()])
            self.index = Index(build_docs(self.registry.active(), self.recipes))
            from . import oauth
            oauth.prune(self.registry.connections)      # a removed connection's tokens go too
            self._stamp = self._current_stamp()

    def reload_if_changed(self):
        """Hot reload: adding a connection never needs a restart (ADR-1)."""
        if self._current_stamp() != self._stamp:
            self._load()
            return True
        return False

    # -- helpers ---------------------------------------------------------------------------

    def _client(self, client_id, remote=False):
        """The policy subject for a caller. Local callers are NOT exempt (ADR-5): they get the
        allow list and tier ceiling from gateway.json `local`, like an enrolled client.
        INVARIANT (review M2): a REMOTE caller is never the local one, whatever id it carries."""
        if remote and client_id in (None, "", LOCAL_CLIENT):
            raise GatewayError("unauthorized", "a hub client is never the local caller")
        if client_id in (None, LOCAL_CLIENT):
            loc = self.settings.get("local") or {}
            return {"id": LOCAL_CLIENT, "allow": loc.get("allow", ["*"]),
                    "max_tier": loc.get("max_tier", "consent"), "revoked": None}
        return self.registry.client(client_id)

    def _visible(self, client, conn_id):
        if client is None:
            return True
        try:
            policy.check_client(client, conn_id, "read")
            return True
        except GatewayError:
            return False

    def _recipe(self, name, conn):
        if name.startswith("recipe:"):
            name = name[len("recipe:"):]
        for r in self.recipes:
            if r["name"] == name and recipes_mod.applicable(r, conn):
                return r
        return None

    def _limits(self):
        return self.settings.get("limits") or {}

    # -- find ------------------------------------------------------------------------------

    def find(self, query="", connection=None, limit=8, detail="summary", client_id=None,
             remote=False):
        try:
            with self._lock:
                self.reload_if_changed()
                client = self._client(client_id, remote)
                if connection:
                    self.registry.connection(connection)  # unknown -> error with hints
                hits = self.index.search(query or "", connection=connection,
                                         limit=(limit or 8) if query else 200)
                results = []
                for h in hits:
                    if not self._visible(client, h["connection"]):
                        continue
                    item = {k: h.get(k) for k in ("connection", "op", "kind", "summary", "tier", "stale")
                            if h.get(k) is not None}
                    if detail == "schema" and h.get("kind") in ("op", "recipe"):
                        item["params"] = self._params(h)
                    results.append(item)
                notes = []
                if not query:
                    notes.append("this is the connection catalog; call find with a query to search operations")
                elif not results:
                    notes.append("no match; try other words, or find('') to list connections")
                return {"ok": True, "zone": self.registry.zone, "results": results, "notes": notes}
        except GatewayError as e:
            return {"ok": False, "error": e.to_dict()}

    def _params(self, hit):
        conn = self.registry.connection(hit["connection"])
        if hit.get("kind") == "recipe":
            r = self._recipe(hit["op"], conn)
            return (r or {}).get("params", {})
        prof = profiles.get(conn["profile"])
        try:
            return profiles.resolve_op(prof, hit["op"]).get("params", {})
        except GatewayError:
            return {}

    # -- call ------------------------------------------------------------------------------

    def call(self, tool, connection, op, args=None, select=None, cursor=None, client_id=None,
             subject=None, remote=False):
        args = dict(args or {})
        base = {"tool": tool, "connection": connection, "op": op,
                "client": client_id or LOCAL_CLIENT, "args_sha256": _args_hash(args),
                "grant": None, "pid": (subject or {}).get("pid")}
        local = not remote and client_id in (None, LOCAL_CLIENT)
        tier = None
        identity = None
        try:
            with self._lock:
                self.reload_if_changed()
                if tool not in TOOLS:
                    raise GatewayError("bad_tool", f"tool must be one of {', '.join(TOOLS)}")
                if cursor and tool != "call_read":
                    raise GatewayError("cursor_needs_read", "a cursor continues a read, so only "
                                       "call_read accepts one",
                                       ["call again without a cursor, or use call_read to page"])
                conn = self.registry.connection(connection)
                identity = conn.get("identity")
                client = self._client(client_id, remote)
                prof = profiles.for_connection(conn)
                recipe = self._recipe(op, conn)
                if recipe:
                    steps = recipes_mod.expand(recipe, conn, args)
                    plan = []
                    tier = recipe.get("tier", "read")
                    for s in steps:
                        opd = profiles.resolve_op(prof, s["op"])
                        t = self._classify(conn, opd, s["args"])
                        if t == "deny":
                            raise GatewayError("op_denied", f"{s['op']} is denied on {connection}")
                        tier = _max_tier(tier, t)
                        plan.append((opd, s["args"]))
                    select = select or recipe.get("select")
                else:
                    opd = profiles.resolve_op(prof, op)
                    tier = self._classify(conn, opd, args)
                    if tier == "deny":
                        raise GatewayError("op_denied", f"{op} is denied on {connection} by policy")
                    plan = [(opd, args)]
                if not policy.allowed_through(tool, tier):
                    right = {"read": "call_read", "write": "call_write", "consent": "call_consent"}[tier]
                    raise GatewayError("wrong_tool", f"{op} on {connection} is a {tier} operation",
                                       [f"use {right}"])
                if local:
                    # Before the client policy: the authority decides whether THIS process may
                    # use this connection at this tier at all (mcp_only, locked, step-up).
                    base["grant"] = self._gate(connection, tier, subject, tool, op)
                policy.check_client(client, connection, tier)
                if (conn.get("auth") or {}).get("scheme") == "oauth" and not local and tier != "read":
                    # 0.3.0: an OAuth sign-in is a person's. A hub client (unattended) may use
                    # it to read, never to write or consent. Refused BEFORE any cursor is spent
                    # or any consent dialog raised: nobody is asked to approve something that
                    # cannot happen.
                    raise GatewayError("oauth_unattended_refused",
                                       f"{connection} is an OAuth sign-in; {tier} operations are "
                                       "never available to an unattended hub client")
                entry = self._take_cursor(cursor, connection, client_id or LOCAL_CLIENT,
                                          op) if cursor else None
                if entry and tier != "read":
                    raise GatewayError("bad_cursor", "a cursor can only continue a read")
                if tier == "consent":
                    mode = (self.settings.get("local") or {}).get("confirm", "auto")
                    text = confirm_mod.describe(connection, identity, op, args,
                                                client_id or LOCAL_CLIENT)
                    g = self._consent(mode, text, base, subject if local else None, identity)
                    base["grant"] = base["grant"] or g
                if tier in ("write", "consent") and not audit.writable(self.home):
                    # Never act without the record (review, low): the audit line is written
                    # after the call, so an unwritable log refuses a write BEFORE it runs.
                    raise GatewayError("audit_failed", "the audit log cannot be written, so "
                                       f"this {tier} operation is refused",
                                       hints=[str(self.home / "state" / "audit.jsonl")])
                factory = self._connection_factory
                if conn.get("kind") == "mcp" and factory is HttpConnection:
                    factory = McpConnection           # 0.2.0: an SSO/OAuth MCP downstream
                is_oauth = (conn.get("auth") or {}).get("scheme") == "oauth"
                resolver = self._secret_resolver or self._brokered_resolver(
                    subject, None if local else client_id)
                kw = {"secret_resolver": resolver} if resolver else {}
                if is_oauth and factory is McpConnection:
                    kw.update(self._oauth_kwargs(subject, None if local else client_id))
                http = factory(conn, prof, **kw)
                if entry:
                    self._check_cursor_target(conn, http, entry["raw"])
                result = None
                for i, (opd, a) in enumerate(plan):
                    result = http.call(opd, a, cursor=entry["raw"] if entry and i == len(plan) - 1
                                       else None)
                result = dict(result)
                result["next_cursor"] = self._issue_cursor(
                    result.get("next_cursor"), tier, connection, client_id or LOCAL_CLIENT, op)
            data, notes = self._shape(result.get("data"), prof, select,
                                       bool(result.get("next_cursor")))
            notes = list(result.get("notes") or []) + list(notes)
            env = {"ok": True, "connection": connection, "identity": identity,
                   "client": client_id or LOCAL_CLIENT, "op": op, "tier": tier,
                   "data": data, "next_cursor": result.get("next_cursor"),
                   "notes": notes, "untrusted": True}
            found = shaping.scan_credentials(data)
            if found:
                env["data"] = None
                env["withheld"] = found
                env["notes"].append("result withheld: it contains credential-shaped text "
                                    "(paths and rule ids listed, values never shown)")
            self._audit(base, identity, tier, "ok", result.get("status"))
            if base["grant"]:
                env["grant"] = base["grant"]
            return env
        except GatewayError as e:
            self._audit(base, identity, tier, "error:" + e.code, None)
            return {"ok": False, "error": e.to_dict()}

    # -- gt unlock (0.3.0) ---------------------------------------------------------------

    def _gate(self, connection, tier, subject, tool, op):
        """-> the grant id, or None when unlock is off / the scope is open. Raises.
        INVARIANT: a local caller with no kernel identity is refused while unlock is on --
        "unidentified" is never treated as "the shim"."""
        u = self._unlock
        if not u.enabled():
            return None
        if subject is None:
            raise GatewayError("peer_unidentified",
                               "gt unlock is on and this caller's process could not be "
                               "identified from the kernel, so it is refused")
        return u.check(f"lotr:{connection}:{tier}", subject, request=False,
                       reason=f"{tool} {op} on {connection}")

    def _consent(self, mode, text, base, subject, identity=None):
        """The consent step. With the authority's consent_requires_factor "platform", a Touch
        ID / Windows Hello signature over THIS op (raised by the authority) is the confirmation;
        otherwise the 0.2.0 path (local.confirm: dialog / refuse / none) runs unchanged.
        `biometric` requires the platform route and refuses when it is not available.
        -> the grant id the authority approved under, or None."""
        u = self._unlock
        if subject is not None and u.enabled():
            # The authority composes the prompt text and the op hash itself from these fields
            # (review M1): nothing a caller writes is shown to the person approving.
            op = {"tool": base["tool"], "connection": base["connection"], "op": base["op"],
                  "args_sha256": base["args_sha256"]}
            if identity:
                op["identity"] = str(identity)
            try:
                res = u.call("consent", {"subject": {"pid": subject["pid"],
                                                     "start": subject["start"]},
                                         "op": op})
            except GatewayError as e:
                if e.code in ("locked", "failed_closed", "unreachable", "unlock_unavailable",
                              "subject_gone", "unattended"):
                    raise
                # A failed / cancelled / forged platform factor is a DENIAL, never a fallback
                # to the weaker dialog.
                raise GatewayError("consent_denied", f"the platform confirmation failed "
                                   f"({e.code}): {e.message}", e.hints)
            res = res if isinstance(res, dict) else {}
            if res.get("mode") == "platform":
                if res.get("approved"):
                    return res.get("grant")
                raise GatewayError("consent_denied", "the owner did not approve this operation")
        if mode == "biometric":
            raise GatewayError("consent_refused",
                               "local.confirm is 'biometric': consent-tier operations need gt "
                               "unlock's platform confirmation (consent_requires_factor: "
                               "platform), which is not available to this caller",
                               hints=["turn gt unlock on with consent_requires_factor "
                                      "'platform'", "or set local.confirm to 'dialog'"])
        confirm_mod.confirm(mode, text, dialog=self._dialog)
        return None

    def _brokered_resolver(self, subject, hub_client=None):
        """None (the connection's default resolver, as 0.2.0) unless a subject or a hub client
        is known. A LOCAL caller's brokered refs are asked for ON BEHALF of its kernel-identified
        subject. A HUB client's are asked as the unattended job `lotr-hub:<client>` (review M3:
        they were resolved under lotrd's own identity) -- so only an allow-list entry
        {"job": "lotr-hub:<client>", "scope": "secret:<ref>"} in the unlock policy opens one."""
        if subject is None and not hub_client:
            return None
        from . import secrets as secrets_mod
        subj = {"pid": subject["pid"], "start": subject["start"]} if subject else None
        job = None if subj else "lotr-hub:%s" % hub_client

        def resolve(ref):
            if secrets_mod.brokered(ref):
                return secrets_mod.resolve(ref, subject=subj, job=job)
            return secrets_mod.resolve(ref)
        return resolve

    # -- cursors: the next-page request lives here, never in the caller's hands ------------

    def _issue_cursor(self, raw, tier, connection, client, op):
        """Swap a connection's next-page token for an opaque random id held in memory, bound
        to (connection, seat, op) and an expiry. Only a read ever gets one."""
        if not raw or tier != "read":
            return None
        now = time.time()
        for k in [k for k, v in self._cursors.items() if v["exp"] <= now]:
            del self._cursors[k]
        while len(self._cursors) >= CURSOR_MAX:
            del self._cursors[next(iter(self._cursors))]
        cid = "gtc_" + _pysecrets.token_urlsafe(24)
        self._cursors[cid] = {"raw": raw, "conn": connection, "client": client, "op": op,
                              "exp": now + CURSOR_TTL}
        return cid

    def _take_cursor(self, cursor, connection, client, op):
        bad = GatewayError("bad_cursor", "the cursor is not one this gateway issued to you "
                           "for this operation, or it expired",
                           ["call again without a cursor to start from the first page"])
        e = self._cursors.get(cursor) if isinstance(cursor, str) else None
        if not e or e["exp"] <= time.time():
            self._cursors.pop(cursor, None) if isinstance(cursor, str) else None
            raise bad
        if (e["conn"], e["client"], e["op"]) != (connection, client, op):
            raise bad
        return e

    def _check_cursor_target(self, conn, http, raw):
        """The cursor's target gets the same checks as any call: host/base (normalised, no
        string prefix) and policy as a GET of that path -- deny, or any non-read, refuses."""
        target = getattr(http, "cursor_target", None)
        if target is None:
            return
        rel = target(raw)
        if policy.classify(conn, {"method": "GET", "path": rel}) != "read":
            raise GatewayError("op_denied", f"the cursor points at GET {rel}, which policy on "
                               f"{conn.get('id')} does not allow as a read")

    def _oauth_kwargs(self, subject, hub_client):
        """The seat (whose access token this is) and the writer that persists a rotated refresh
        token for that seat. A hub client has a seat of its own and cannot seal (no subject)."""
        if subject is not None:
            seat = "pid:%s:%s" % (subject["pid"], subject["start"])
        elif hub_client:
            seat = "job:%s" % hub_client
        else:
            seat = "-"
        from . import oauth
        return {"oauth_seat": seat, "oauth_writer": oauth.make_writer(self._unlock, subject)}

    def _classify(self, conn, opd, args):
        probe = {"name": opd.get("name"), "method": opd["method"], "path": opd["path"],
                 "tier": opd.get("tier")}
        if opd.get("graphql"):
            # The query that is SENT is the body's when an explicit `body` is given (conn_http
            # merges body over args), so classify that one; an opaque body is a write.
            a = args or {}
            if "body" in a:
                b = a["body"]
                probe["graphql_query"] = b.get("query", "") if isinstance(b, dict) else "\0"
            else:
                probe["graphql_query"] = a.get("query", "")
        return policy.classify(conn, probe)

    def _shape(self, data, prof, select, has_cursor=False):
        lim = self._limits()
        return shaping.shape(data, noise_keys=prof.get("noise_keys", ()), select=select,
                             max_chars=lim.get("max_result_chars", 24000),
                             default_page=lim.get("default_page", 20),
                             has_cursor=has_cursor)

    def _audit(self, base, identity, tier, verdict, status):
        try:
            extra = {"pid": base["pid"]} if base.get("pid") is not None else {}
            audit.record(self.home, connection=base["connection"], identity=identity,
                         op=base["op"], tier=tier, tool=base["tool"], client=base["client"],
                         verdict=verdict, status=status, args_sha256=base["args_sha256"],
                         grant=base.get("grant"), **extra)
        except Exception:
            # An audit failure must be visible, but never turn a served call into a crash.
            pass

    # -- status / catalog ------------------------------------------------------------------

    def status(self):
        from . import secrets as secrets_mod
        with self._lock:
            self.reload_if_changed()
            conns = []
            for c in self.registry.connections.values():
                ref = (c.get("auth") or {}).get("token_ref")
                cred = None
                if ref:
                    try:
                        d = secrets_mod.describe(ref)
                        cred = {"scheme": d.get("scheme"), "present": d.get("present"),
                                "level": d.get("level")
                                or secrets_mod.LOCAL_LEVEL.get(d.get("scheme"))}
                        if d.get("note"):
                            cred["note"] = d["note"]
                    except GatewayError as e:
                        cred = {"error": e.code}
                row = {"id": c["id"], "identity": c.get("identity"), "profile": c.get("profile"),
                       "trust": c.get("trust"), "enabled": c.get("enabled", True),
                       "credential": cred}
                if (c.get("auth") or {}).get("scheme") == "oauth":
                    from . import oauth
                    ob = c["auth"]["oauth"]
                    row["oauth"] = dict(
                        {"issuer": ob.get("issuer"), "scopes": ob.get("scopes"),
                         "client_source": ob.get("client_id_source"),
                         "refresh_token": "none issued" if not c["auth"].get("token_ref")
                         else "stored as " + c["auth"]["token_ref"].split(":", 1)[0] + ":"},
                        **oauth.status_of(c))
                conns.append(row)
            return {"ok": True, "zone": self.registry.zone, "mode": self.settings["mode"],
                    "connections": conns, "clients": len(self.registry.clients),
                    "recipes": len(self.recipes), "index_docs": len(self.index.docs)
                    if hasattr(self.index, "docs") else None}

    def catalog_text(self, max_chars=1800):
        """Server instructions for MCP: what exists, in one glance, under the 2,048-char cap."""
        with self._lock:
            self.reload_if_changed()
            head = (f"gt-lotr, zone {self.registry.zone}. Every downstream system sits behind "
                    "four tools. Call find(query) to search operations across all connections; "
                    "find('') lists every connection. Then call_read / call_write / call_consent "
                    "with the connection and op it returned. The gateway decides the tier.\n"
                    "Connections:\n")
            lines, used, shown = [], len(head), 0
            active = self.registry.active()
            for c in active:
                # One line per connection: a description is owner-written but lands in every
                # session's instructions, so a newline or control character in it must not
                # start a line of its own there (review m7, 2026-10-04).
                text = _one_line(f"{c['id']}: {c.get('description') or c.get('identity') or ''}")
                line = f"- {text}"[:140] + "\n"
                if used + len(line) > max_chars - 40:
                    break
                lines.append(line)
                used += len(line)
                shown += 1
            tail = f"(+{len(active) - shown} more: find(''))" if shown < len(active) else ""
            return (head + "".join(lines) + tail).rstrip()
