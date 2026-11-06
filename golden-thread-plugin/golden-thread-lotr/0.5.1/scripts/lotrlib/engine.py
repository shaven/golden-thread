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

0.5.0 (gt 0.21.4), the capability index and contracts (lotrlib/contracts.py, visibility.py, refs.py,
redact.py, ledger.py):
  find       entries are outcome-worded and carry a risk class, each within ~50 tokens, the whole
             result within a budget; what the caller could not use is not listed (visibility.py)
  describe   the planning contract of one operation and a single-use reference to run it by
  call       `op` may be that reference; every check still runs on the operation it names
  errors     the full text of a failure goes to the audit log (`detail`); the MCP doors show the
             model lotrlib.redact.public_error
  ledger     a find records its cost against full-schema injection in gt-usage's savings ledger
"""
import difflib
import hashlib
import json
import re
import os
import secrets as _pysecrets
import threading
import time
from pathlib import Path

from . import audit, confirm as confirm_mod, policy, profiles, recipes as recipes_mod, shaping
from . import action_token, contracts, endpoint_health, ledger as ledger_mod, redact, refs, visibility
from . import unlock as unlock_mod
from . import upstream
from . import windows
from .config import load_settings
from .conn_http import HttpConnection
from .conn_mcp import McpConnection
from .conn_stdio import McpStdioConnection
from . import conn_stdio

# Connection health (0.4.0, owner 2026-10-06: see what is connected and whether it works, like
# Claude Code's /mcp). Only failures that say something about the CONNECTION count; caller and
# policy errors (wrong_tool, op_denied, bad_request, ...) leave its state as it was.
SIGN_IN_CODES = {"needs_login", "auth_invalid", "auth_failed", "unauthorized", "secret_missing",
                 "secret_unreadable", "oauth_binding_mismatch", "oauth_resource_mismatch"}
DOWN_CODES = {"unreachable", "network_error", "downstream_error", "bad_response", "server_refused",
              "redirect_refused", "tls_untrusted", "tls_error", "tls_ca_missing", "tls_ca_changed"}
# 0.22.0: what counts against one SUB-ENDPOINT of a dispatching connection. A backend answering 5xx
# or timing out is that backend failing; a rate limit (a busy provider) and the caller's own errors are not.
SUB_FAILURE_CODES = SIGN_IN_CODES | DOWN_CODES | {"upstream_error", "upstream_timeout"}
from .errors import GatewayError
from .index import Index, build_docs
from .registry import Registry

TOOLS = ("call_read", "call_write", "call_consent")
TIER_RANK = {"read": 0, "write": 1, "consent": 2}
SHIPPED_RECIPES = Path(__file__).resolve().parents[2] / "templates" / "recipes"
LOCAL_CLIENT = "local"
CURSOR_TTL = 600        # seconds a next-page cursor stays valid
CURSOR_MAX = 256        # cursors held in memory at once
REF_MAX = 512           # op references held in memory at once
REF_RX = re.compile(r"^gtr_[A-Za-z0-9_-]{32}$")      # what an op reference looks like (and only that)
SEARCH_DEPTH = 2000     # hits scored before the caller's view and limit are applied
DETAIL_CHARS = 400      # an error's text kept in the audit line
RESULT_CHARS_CEILING = 200000   # the most a caller's max_result_chars can raise the cap to (limits.max_result_chars_ceiling)


def _probe_no_redirect():
    """A urllib redirect handler for the health probe that follows nothing (security review M2)."""
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise GatewayError("redirect_refused", f"the health path answered {code} (a redirect); "
                               "a probe follows none, so the connection counts as down")
    return _NoRedirect()


def _max_tier(a, b):
    return a if TIER_RANK[a] >= TIER_RANK[b] else b


def _with_llm(conn, tier):
    """0.5.1: a connection that declares `llm: {tools}` says what a model op can trigger, so every
    op on it is at least that tier. Absent, or `none`, adds nothing; deny stays deny."""
    tools = (conn.get("llm") or {}).get("tools")
    if tier == "deny" or tools not in ("read", "write"):
        return tier
    return _max_tier(tier, tools)


def _args_hash(args):
    blob = json.dumps(args or {}, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()



_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")


def _one_line(text):
    """`text` with every newline, tab and other control character made one space."""
    return " ".join(_CONTROL.sub(" ", str(text)).split())

def _detail(err):
    """An error's full text for the audit log: message and hints on one line, credential-shaped
    text withheld, at most DETAIL_CHARS. The model never reads it (lotrlib.redact)."""
    text = _one_line(" | ".join([str(err.message)] + [h for h in err.hints if isinstance(h, str)]))
    if shaping.scan_credentials(text):
        return "(withheld: credential-shaped text)"
    return text[:DETAIL_CHARS]


class Engine:
    def __init__(self, home, *, secret_resolver=None, connection_factory=None, dialog=None,
                 unlock=None):
        self.home = Path(home)
        self._health = {}                # connection id -> {last_ok, last_error}
        self._dialog = dialog            # tests inject a fake; None = the real macOS dialog
        self._unlock = unlock or unlock_mod   # gt core's authority, through lotrlib.unlock
        self._secret_resolver = secret_resolver
        self._connection_factory = connection_factory or HttpConnection
        self._clock = time.time          # tests inject a fake
        self._limiter = upstream.CallLimiter(lambda: self._clock())      # 0.5.1: per-connection limits
        self._lock = threading.RLock()
        # One store class for every opaque id the gateway hands out (lotrlib/refs.py): next-page
        # cursors and op references. `_cursors` is the cursor store's own dict, kept under its
        # 0.4.0 name.
        self._cursor_store = refs.RefStore("gtc_", CURSOR_TTL, CURSOR_MAX)
        self._cursors = self._cursor_store.items
        self._opr = refs.RefStore("gtr_", refs.DEFAULT_TTL, REF_MAX)
        self._inject = {}        # connection id -> tokens of its full tool definitions; per load
        self._stamp = None
        self._confirm_mode = None        # local.confirm, read ONCE at start (0.22.0 F9)
        self._load()
        self._migrate_approvals()
        self._confirm_mode = (self.settings.get("local") or {}).get("confirm", "auto")
        if self._confirm_mode == "none":
            self._audit_event("start", None, verdict="confirm_none")

    def _migrate_approvals(self):
        """Phase 112, owner ruling 2026-10-25: the stdio connections already registered when the
        gateway first starts on this release are trusted and recorded as approved, once."""
        n = conn_stdio.migrate_approvals(self.home, self.registry.connections)
        if n:
            self._audit_event("approvals", None, op="approvals_migrated: %d" % n)

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
            self._opr.ttl = refs.ttl_from(self.settings.get("limits"))
            self._inject = {}
            from . import oauth
            oauth.prune(self.registry.connections)      # a removed connection's tokens go too
            conn_stdio.retain(self.registry.connections)   # and its stdio server stops now
            self._stamp = self._current_stamp()

    def reload_if_changed(self):
        """Hot reload: adding a connection never needs a restart (ADR-1). `registry_watch: false`
        turns the per-request check off; `reload()` (SIGHUP) still works."""
        if not self.settings.get("registry_watch", True):
            return False
        if self._current_stamp() != self._stamp:
            self._load()
            return True
        return False

    def reload(self):
        """Re-read gateway.json, registry.json and the recipes now (lotrd's SIGHUP)."""
        self._load()

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

    # -- the caller's view (visibility.py) ----------------------------------------------------

    @staticmethod
    def _seat(subject):
        return "pid:%s:%s" % (subject["pid"], subject["start"]) if subject else "-"

    @staticmethod
    def _short(ref):
        """What the audit log keeps of a reference: twelve hex digits of its hash, never the id."""
        return hashlib.sha256(ref.encode("utf-8")).hexdigest()[:12]

    def _lookup(self, connection, client_id, remote):
        """registry.connection with an unknown-connection hint that names only connections this
        caller may see (the registry's own hint lists the closest ids of all of them)."""
        try:
            return self.registry.connection(connection)
        except GatewayError as e:
            if e.code != "unknown_connection":
                raise
            try:
                client = self._client(client_id, remote)
            except GatewayError:
                client = None
            raise GatewayError(e.code, e.message, [h for h in e.hints
                                                   if client is None or self._visible(client, h)])

    def _recipe_tier(self, conn, prof, recipe):
        """A recipe's tier as a call with no arguments: its own, raised to its steps'; "deny" if any
        step is denied."""
        tier = recipe.get("tier", "read")
        for st in recipe["profiles"][conn["profile"]]["steps"]:
            t = self._classify(conn, profiles.resolve_op(prof, st["op"]), st.get("args") or {})
            if t == "deny":
                return "deny"
            tier = _max_tier(tier, t)
        return tier

    def _hit_tier(self, conn, prof, hit):
        """The tier an index hit has as a call with no arguments, or "deny". A GraphQL document's
        tier depends on its text, so that one op lists as the guarded tier (write)."""
        if hit.get("kind") == "recipe":
            r = self._recipe(hit["op"], conn)
            if r is None:
                raise GatewayError("unknown_op", "no such recipe")
            return self._recipe_tier(conn, prof, r)
        return self._classify(conn, profiles.resolve_op(prof, hit["op"]), {})

    def _view(self, client, remote, subject, role):
        tiers, note = visibility.role_tiers(role)
        authority = None
        if not remote and subject is not None and self._unlock.enabled():
            seen = {}

            def authority(cid, tier):
                if (cid, tier) not in seen:
                    seen[(cid, tier)] = self._authority_refusal(cid, tier, subject)
                return seen[(cid, tier)]
        return {"client": client, "remote": remote, "role_tiers": tiers, "role_note": note,
                "authority": authority}

    def _authority_refusal(self, cid, tier, subject):
        """A hard refusal code from gt unlock's authority for `lotr:<cid>:<tier>`, else None.
        Asked with request=False: finding things out never raises a prompt."""
        try:
            self._unlock.check("lotr:%s:%s" % (cid, tier), subject, request=False,
                               reason="lotr find")
        except GatewayError as e:
            return e.code if e.code in visibility.AUTHORITY_HARD else None
        except Exception:                                   # noqa: BLE001 - not knowing is not hiding
            return None
        return None

    def _hidden(self, view, conn, tier):
        return visibility.hidden_reason(
            tier="read" if tier == "deny" else tier, denied=(tier == "deny"), client=view["client"],
            conn_id=conn["id"], remote=view["remote"],
            oauth=(conn.get("auth") or {}).get("scheme") == "oauth",
            role_tiers=view["role_tiers"], authority=view["authority"])

    def _visible_op_names(self, view, conn, prof):
        out = []
        for o in prof.get("ops", []):
            try:
                tier = self._classify(conn, o, {})
            except GatewayError:
                continue
            if not self._hidden(view, conn, tier):
                out.append(o["name"])
        return out

    def _unknown_op(self, view, conn, prof, op):
        """The one answer for an operation that does not exist AND for one the caller may not see,
        so the difference cannot be told. Its hints name only operations the caller can see."""
        names = self._visible_op_names(view, conn, prof)
        hints = difflib.get_close_matches(str(op), names, n=3, cutoff=0.5) or names[:12]
        if isinstance(op, str) and op.startswith("gtr_"):
            hints = ["a reference is gtr_ followed by 32 characters, exactly as find(connection, "
                     "op) gave it"] + hints
        return GatewayError("unknown_op", f"{op!r} is not an operation of profile {prof.get('name')}",
                            hints)

    # -- find ------------------------------------------------------------------------------

    def find(self, query="", connection=None, limit=8, detail="summary", client_id=None,
             remote=False, subject=None, role=None):
        remote = remote or client_id not in (None, "", LOCAL_CLIENT)     # a hub client is remote
        try:
            with self._lock:
                self.reload_if_changed()
                client = self._client(client_id, remote)
                if connection:
                    self._lookup(connection, client_id, remote)   # unknown -> error with hints
                view = self._view(client, remote, subject, role)
                if not query:
                    return self._catalog(client, connection)
                limit = min(max(int(limit or 8), 1), 200)
                hits = self.index.search(query, connection=connection, limit=SEARCH_DEPTH)
                profs, shown, hidden = {}, [], 0
                for h in hits:
                    if len(shown) >= limit:
                        break                        # enough the caller can see; `hidden` is a floor
                    cid = h["connection"]
                    if not self._visible(client, cid):
                        continue
                    if h.get("kind") not in ("op", "recipe"):
                        shown.append((h, None))
                        continue
                    conn = self.registry.connection(cid)
                    if cid not in profs:
                        profs[cid] = profiles.for_connection(conn)
                    try:
                        tier = self._hit_tier(conn, profs[cid], h)
                    except GatewayError:
                        continue                     # an index entry that no longer resolves
                    if self._hidden(view, conn, tier):
                        hidden += 1
                        continue
                    shown.append((h, tier))
                items, unfit = [], 0
                for h, tier in shown[:limit]:
                    if tier is None:
                        item = {"connection": h["connection"], "kind": "connection",
                                "summary": contracts.one_line(h.get("summary"), 200)}
                        if h.get("stale"):
                            item["stale"] = True
                        items.append(item)
                        continue
                    item = {"connection": h["connection"], "op": h["op"], "kind": h["kind"],
                            "summary": h.get("summary"), "tier": tier, "risk": contracts.risk_of(tier)}
                    llm = (self.registry.connection(h["connection"]).get("llm") or {}).get("tools")
                    if llm:                                # 0.5.1: what a model op on it can trigger
                        item["llm_tools"] = llm
                    if h.get("stale"):
                        item["stale"] = True
                    fit = contracts.fit_item(item)
                    if fit is None:
                        unfit += 1
                    else:
                        items.append(fit)
                budget = int(self._limits().get("find_budget_tokens", contracts.FIND_BUDGET_TOKENS))
                kept, left = contracts.fit_budget(items, budget)
                notes = []
                if view["role_note"]:
                    notes.append(view["role_note"])
                if unfit:
                    notes.append("%d operation%s could not be summarized within %d tokens and %s left "
                                 "out; name the connection and search for it by its id"
                                 % (unfit, "" if unfit == 1 else "s", contracts.ITEM_TOKENS,
                                    "was" if unfit == 1 else "were"))
                if left:
                    notes.append("%d more match%s left out to stay within the %d-token budget; narrow "
                                 "the query or name a connection"
                                 % (left, " was" if left == 1 else "es were", budget))
                if not kept and not notes:
                    notes.append("no match; try other words, or find('') to list connections")
                if detail == "schema":
                    for it in kept:
                        if it.get("kind") in ("op", "recipe"):
                            it["params"] = self._params(it)
                self._note_find(client_id, connection, query, kept, notes, hidden, unfit, left, client)
                return {"ok": True, "zone": self.registry.zone, "results": kept, "notes": notes}
        except GatewayError as e:
            return {"ok": False, "error": e.to_dict()}

    def _catalog(self, client, connection):
        """find(""): the connections the caller may see, each with its state (0.4.0)."""
        hits = self.index.search("", connection=connection, limit=200)
        results = []
        for h in hits:
            if not self._visible(client, h["connection"]):
                continue
            item = {k: h.get(k) for k in ("connection", "op", "kind", "summary", "tier", "stale")
                    if h.get(k) is not None}
            try:
                conn = self.registry.connection(h["connection"])
                st, fix = self._state(conn)
                item["state"] = st
                item["locality"] = conn.get("locality") or "remote"   # 0.5.1: explicit, for every row
                if conn.get("provider"):
                    item["provider"] = conn["provider"]
                if (conn.get("llm") or {}).get("tools"):
                    item["llm_tools"] = conn["llm"]["tools"]
                if fix:
                    item["fix"] = fix
            except GatewayError:
                pass
            results.append(item)
        return {"ok": True, "zone": self.registry.zone, "results": results,
                "notes": ["this is the connection catalog; call find with a query to search operations"]}

    def _params(self, hit):
        conn = self.registry.connection(hit["connection"])
        if hit.get("kind") == "recipe":
            r = self._recipe(hit["op"], conn)
            return (r or {}).get("params", {})
        prof = profiles.for_connection(conn)
        try:
            return profiles.resolve_op(prof, hit["op"]).get("params", {})
        except GatewayError:
            return {}

    def _injection(self, ids):
        """Estimated tokens of loading these connections' operations as full tool definitions."""
        total = 0
        for cid in ids:
            if cid not in self._inject:
                try:
                    conn = self.registry.connection(cid)
                    self._inject[cid] = contracts.injection_tokens(
                        conn, profiles.for_connection(conn),
                        [r for r in self.recipes if recipes_mod.applicable(r, conn)])
                except Exception:                           # noqa: BLE001 - a measure, never a failure
                    self._inject[cid] = 0
            total += self._inject[cid]
        return total

    def _note_find(self, client_id, connection, query, kept, notes, hidden, unfit, left, client):
        """After a find with a query: an audit line when something was hidden or left out, and the
        ledger line (find cost against full-schema injection)."""
        if hidden or unfit or left:
            self._audit_event("find", client_id, connection=connection, query=query, hidden=hidden,
                              unfit=unfit, left_out=left, shown=len(kept))
        ids = [connection] if connection else [c["id"] for c in self.registry.active()
                                               if self._visible(client, c["id"])]
        ops = sum(1 for d in self.index.docs if d.get("kind") in ("op", "recipe")
                  and d["connection"] in ids)
        ledger_mod.record_find(contracts.tokens({"results": kept, "notes": notes}),
                               self._injection(ids), len(ids), ops, len(kept), unfit + left)

    def _audit_event(self, tool, client_id, connection=None, op=None, tier=None, verdict="ok",
                     ref=None, query=None, detail=None, **extra):
        """An audit line for something that is not a downstream call (describe; a find that hid or
        dropped entries). Arguments and queries are hashed, never written."""
        try:
            fields = dict(extra)
            if ref:
                fields["ref"] = ref
            if detail:
                fields["detail"] = detail
            audit.record(self.home, connection=connection, identity=None, op=op, tier=tier, tool=tool,
                         client=client_id or LOCAL_CLIENT, verdict=verdict, status=None,
                         args_sha256=_args_hash({"query": query} if query is not None else {}),
                         grant=None, **fields)
        except Exception:                                   # noqa: BLE001
            pass

    # -- describe (find with an op) ----------------------------------------------------------

    def describe(self, connection, op, client_id=None, remote=False, subject=None, role=None):
        """The planning contract of ONE operation and a single-use reference to run it by. Answers
        for an operation the caller may not see exactly as for one that does not exist."""
        ref = None
        tier = None
        remote = remote or client_id not in (None, "", LOCAL_CLIENT)     # a hub client is remote
        try:
            with self._lock:
                self.reload_if_changed()
                client = self._client(client_id, remote)
                conn = self._lookup(connection, client_id, remote)
                if not self._visible(client, conn["id"]):
                    raise GatewayError("unknown_connection", f"unknown connection {connection!r}", [])
                view = self._view(client, remote, subject, role)
                prof = profiles.for_connection(conn)
                name = op.strip() if isinstance(op, str) else ""
                recipe = self._recipe(name, conn) if name else None
                if recipe:
                    kind, canonical, summary = "recipe", recipe["name"], recipe["summary"]
                    tier = self._recipe_tier(conn, prof, recipe)
                    params = recipe.get("params") or {}
                    required = {p for p, sp in params.items() if "default" not in (sp or {})}
                else:
                    try:
                        opd = profiles.resolve_op(prof, name)
                    except GatewayError as e:
                        if e.code == "unknown_op":
                            raise self._unknown_op(view, conn, prof, op)
                        raise
                    kind, canonical, summary = "op", opd.get("name") or name, opd["summary"]
                    tier = self._classify(conn, opd, {})
                    params = opd.get("params") or {}
                    required = set(opd.get("required") or []) | set(profiles.placeholders(opd["path"])
                                                                    if conn.get("kind") != "mcp" else [])
                if self._hidden(view, conn, tier):
                    raise self._unknown_op(view, conn, prof, op)
                tool = {"read": "call_read", "write": "call_write", "consent": "call_consent"}[tier]
                local = not remote and client_id in (None, LOCAL_CLIENT)
                needs_token = local and action_token.required(tool)
                ref = self._opr.issue({"connection": conn["id"], "op": canonical, "tool": tool,
                                       "tier": tier, "client": client_id or LOCAL_CLIENT,
                                       "seat": self._seat(subject)})
                notes = [f"run it with {tool}: connection {conn['id']}, op = the ref, args = the inputs "
                         "above; the reference works once and expires"]
                if view["role_note"]:
                    notes.append(view["role_note"])
                out = {"ok": True, "zone": self.registry.zone, "connection": conn["id"],
                       "op": canonical, "kind": kind,
                       "outcome": contracts.one_line(redact.scrub_text(summary),
                                                     contracts.DESCRIBE_OUTCOME_CHARS),
                       "tier": tier, "risk": contracts.risk_of(tier),
                       "effects": contracts.effects_text(tier),
                       "confirmation": contracts.confirmation_text(tier, needs_token),
                       "inputs": contracts.inputs_list(params, required), "call_with": tool,
                       "ref": ref, "expires_in_s": int(self._opr.ttl), "single_use": True,
                       "notes": notes, "locality": conn.get("locality") or "remote"}
                if conn.get("provider"):         # 0.5.1: only when the connection declares one
                    out["provider"] = conn["provider"]
                self._audit_event("describe", client_id, connection=conn["id"], op=canonical,
                                  tier=tier, ref=self._short(ref))
                return out
        except GatewayError as e:
            self._audit_event("describe", client_id, connection=connection if isinstance(connection, str)
                              else None, op=op if isinstance(op, str) else None, tier=tier,
                              verdict="error:" + e.code, detail=_detail(e))
            return {"ok": False, "error": e.to_dict()}

    # -- call ------------------------------------------------------------------------------

    def call(self, tool, connection, op, args=None, select=None, cursor=None, client_id=None,
             subject=None, remote=False, token=None, timeout_s=None, max_result_chars=None):
        args = dict(args or {})
        base = {"tool": tool, "connection": connection, "op": op,
                "client": client_id or LOCAL_CLIENT, "args_sha256": _args_hash(args),
                "grant": None, "pid": (subject or {}).get("pid")}
        local = not remote and client_id in (None, LOCAL_CLIENT)
        tier = None
        identity = None
        ref_id = None
        slot = None
        conn = None
        synth = False
        try:
            with self._lock:
                self.reload_if_changed()
                if tool not in TOOLS:
                    raise GatewayError("bad_tool", f"tool must be one of {', '.join(TOOLS)}")
                call_timeout, result_cap = self._per_call(timeout_s, max_result_chars)
                if cursor and tool != "call_read":
                    raise GatewayError("cursor_needs_read", "a cursor continues a read, so only "
                                       "call_read accepts one",
                                       ["call again without a cursor, or use call_read to page"])
                conn = self._lookup(connection, client_id, remote)
                identity = conn.get("identity")
                client = self._client(client_id, remote)
                prof = profiles.for_connection(conn)
                if isinstance(op, str) and REF_RX.match(op):
                    # 0.5.0: `op` is a reference from find(connection, op). Checked here, in the
                    # action tokens' order, spent only once everything else has agreed (below), so a
                    # refusal does not burn it. The name it stands for replaces it from here on.
                    ref_id = op
                    base["ref"] = self._short(ref_id)
                    base["op"] = "ref:" + base["ref"]
                    op = self._resolve_ref(ref_id, connection, client_id, subject)
                    base["op"] = op
                recipe = self._recipe(op, conn)
                synth = op == endpoint_health.OP_HEALTH and endpoint_health.tracked(conn)
                if synth:                       # 0.22.0: answered here from the health record, never downstream
                    tier = "read"
                    plan = []
                elif recipe:
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
                    try:
                        opd = profiles.resolve_op(prof, op)
                    except GatewayError as e:
                        if e.code == "unknown_op":      # its hint lists every curated op; trim it
                            raise self._unknown_op(self._view(client, remote, None, None), conn,
                                                   prof, op)
                        raise
                    tier = self._classify(conn, opd, args)
                    if tier == "deny":
                        raise GatewayError("op_denied", f"{op} is denied on {connection} by policy")
                    plan = [(opd, args)]
                if not policy.allowed_through(tool, tier, policy.strict_tools(conn)):
                    right = {"read": "call_read", "write": "call_write", "consent": "call_consent"}[tier]
                    raise GatewayError("wrong_tool", f"{op} on {connection} is a {tier} operation"
                                       f"; {tool} does not carry it", [f"use {right}"])
                if local:
                    # Before the client policy: the authority decides whether THIS process may
                    # use this connection at this tier at all (mcp_only, locked, step-up).
                    base["grant"] = self._gate(connection, tier, subject, tool, op)
                    # 0.4.1 (gt 0.21.0, item #32): a single-use token when the paranoid setting
                    # for this tier is on; spent here, before anything runs downstream
                    action_token.check(tool, connection, op, base["args_sha256"], token)
                policy.check_client(client, connection, tier)
                if (conn.get("auth") or {}).get("scheme") == "oauth" and not local:
                    # 0.3.0: an OAuth sign-in is a person's. A hub client (unattended) may use
                    # it to read, never to write or consent. Refused BEFORE any cursor is spent
                    # or any consent dialog raised: nobody is asked to approve something that
                    # cannot happen. 0.5.1 (phase 70): with presence.gate_writes on, an --l1
                    # sign-in may be written to while a person's unlock session is open.
                    pres = self._presence_gate(conn, connection, tier)
                    if pres:
                        base["grant"] = base["grant"] or pres
                entry = self._take_cursor(cursor, connection, client_id or LOCAL_CLIENT,
                                          op) if cursor else None
                if entry and tier != "read":
                    raise GatewayError("bad_cursor", "a cursor can only continue a read")
                need_consent = tier == "consent"

            # 0.5.1: a person's dialog is not held under the lock: it can stay open for minutes
            # and every other call would wait for it. Nothing below depends on state it could change.
            if need_consent:
                # 0.5.1: an owner-opened window (`lotr grant`) stands in for the dialog, only
                # for a local caller and only while no gt unlock authority is deciding
                win = (windows.active(self.home, connection, op, self._clock())
                       if local and not self._unlock.enabled() else None)
                if win:
                    base["grant"] = "window:" + win["id"]
                else:
                    mode = self._confirm_mode
                    text = confirm_mod.describe(connection, identity, op, args,
                                                client_id or LOCAL_CLIENT)
                    g = self._consent(mode, text, base, subject if local else None, identity,
                                      args=args)
                    base["grant"] = base["grant"] or g
            with self._lock:
                if tier in ("write", "consent") and not audit.writable(self.home):
                    # Never act without the record (review, low): the audit line is written
                    # after the call, so an unwritable log refuses a write BEFORE it runs.
                    raise GatewayError("audit_failed", "the audit log cannot be written, so "
                                       f"this {tier} operation is refused",
                                       hints=[str(self.home / "state" / "audit.jsonl")])
                slot = None if synth else self._limiter.acquire(conn["id"], conn.get("limits"))   # 0.5.1; before the ref is spent
                if ref_id:
                    try:                    # the reference is used up, once all agreed; it was
                        self._opr.claim(ref_id)   # checked before the lock was let go, so claim it
                    except refs.RefError as err:
                        raise GatewayError(err.code, err.message,
                                           ["ask for a fresh reference with find(connection, op)"])
                run = None if synth else self._prepare_plan(conn, prof, entry, subject, local, client_id)
            # 0.5.1: the wait on the downstream is the only long step, and it is made with no lock
            # held, so a slow model call never blocks another call (or a read on another connection)
            try:
                if synth:
                    result = {"data": endpoint_health.read_view(self.home, conn, self._clock())}
                else:
                    result = self._run_plan(conn, run, plan, entry, call_timeout)
            finally:
                if slot:
                    slot.release()
            with self._lock:
                result["next_cursor"] = self._issue_cursor(
                    result.get("next_cursor"), tier, connection, client_id or LOCAL_CLIENT, op)
            data, notes, cut = self._shape(result.get("data"), prof, select,
                                           bool(result.get("next_cursor")), result_cap)
            notes = list(result.get("notes") or []) + list(notes)
            env = {"ok": True, "connection": connection, "identity": identity,
                   "client": client_id or LOCAL_CLIENT, "op": op, "tier": tier,
                   "data": data, "next_cursor": result.get("next_cursor"),
                   "notes": notes, "untrusted": True}
            env.update(cut)                          # 0.5.1: truncated, original_chars; only when cut
            found = shaping.scan_credentials(data)
            if found:
                env["data"] = None
                env["withheld"] = found
                env["notes"].append("result withheld: it contains credential-shaped text "
                                    "(paths and rule ids listed, values never shown)")
            self._audit(base, identity, tier, "ok", result.get("status"))
            if not synth:
                self._note_health(connection, None)
                self._note_sub_endpoint(conn, op, args, None)
            if base["grant"] and (local or not str(base["grant"]).startswith("presence:")):
                env["grant"] = base["grant"]          # a hub caller is never told how long you have been here
            return env
        except GatewayError as e:
            self._audit(base, identity, tier, "error:" + e.code, getattr(e, "upstream_status", None),
                        detail=_detail(e), retry_after_s=getattr(e, "retry_after_s", None))
            err = e.to_dict()
            sub = self._note_sub_endpoint(conn, op, args, e)
            if sub:                                  # 0.22.0: the failure belongs to one backend, not the connection
                err["hints"] = list(err.get("hints") or []) + [sub.pop("hint")]
                err.update(sub)
            else:
                self._note_health(connection, e.code)
            return {"ok": False, "error": err}
        finally:
            if slot:                    # a refusal between the slot and the request frees it too
                slot.release()

    def _per_call(self, timeout_s, max_result_chars):
        """(timeout seconds or None, result cap or None) for ONE call, each held to its gateway
        ceiling. Nothing asked is nothing changed. A client can lower either, never raise past it."""
        lim = self._limits()
        t = cap = None
        if timeout_s is not None:
            try:
                t = upstream.clamp_timeout(timeout_s, upstream.call_ceiling(lim))
            except ValueError:
                raise GatewayError("bad_request", "timeout_s must be a number of seconds")
        if max_result_chars is not None:
            if isinstance(max_result_chars, bool) or not isinstance(max_result_chars, int) \
                    or max_result_chars < 1:
                raise GatewayError("bad_request", "max_result_chars must be a whole number, 1 or more")
            base_cap = lim.get("max_result_chars", 24000)
            cap = min(max_result_chars, max(base_cap, lim.get("max_result_chars_ceiling",
                                                              RESULT_CHARS_CEILING)))
            cap = (cap, cap < max_result_chars)
        return t, cap

    def _prepare_plan(self, conn, prof, entry, subject, local, client_id):
        """Build the downstream connection object (secrets resolved, cursor target checked).
        Called under the engine lock; it makes no request."""
        factory = self._connection_factory
        if conn.get("kind") == "mcp" and factory is HttpConnection:
            # 0.2.0: an SSO/OAuth MCP downstream; 0.5.0: a local stdio command
            factory = (self._stdio_factory() if conn.get("transport") == "stdio"
                       else McpConnection)
        is_oauth = (conn.get("auth") or {}).get("scheme") == "oauth"
        resolver = self._secret_resolver or self._brokered_resolver(
            subject, None if local else client_id)
        kw = {"secret_resolver": resolver} if resolver else {}
        if conn.get("trust_ca") and factory in (HttpConnection, McpConnection):
            from . import trustca            # 0.5.1: the approved private CA text, or a refusal
            kw["ca_pem"] = trustca.verified_pem(self.home, conn)
        if is_oauth and factory is McpConnection:
            kw.update(self._oauth_kwargs(subject, None if local else client_id))
        http = factory(conn, prof, **kw)
        if entry:
            self._check_cursor_target(conn, http, entry["raw"])
        return http

    def _stdio_factory(self):
        """The stdio connection class with this home bound: an unapproved command is refused
        (phase 112), never launched."""
        home = self.home
        return lambda conn, prof, **kw: McpStdioConnection(conn, prof, home=home, **kw)

    def _run_plan(self, conn, http, plan, entry, call_timeout):
        """Run the plan's steps downstream and return the last step's result. Takes NO lock: it
        is the wait on the downstream, which may last minutes. The caller swaps the result's
        cursor for an opaque one, under the lock."""
        # 0.5.1: a per-call timeout reaches http and stdio connections (an http MCP connection
        # keeps its own timeouts.call_s); asked for nothing, the call is made exactly as before
        takes = call_timeout is not None and (
            conn.get("kind") == "http" or conn.get("transport") == "stdio")
        ckw = {"timeout_s": call_timeout} if takes else {}
        result = None
        for i, (opd, a) in enumerate(plan):
            try:
                result = http.call(opd, a, cursor=entry["raw"] if entry and i == len(plan) - 1
                                   else None, **ckw)
            except GatewayError as e:
                raise upstream.map_downstream(conn, e)      # an MCP tool's rate-limit text
        return dict(result)

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

    def _consent(self, mode, text, base, subject, identity=None, args=None):
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
            # The arguments themselves, so the authority can show WHO/WHAT (recipient, PR) --
            # it re-hashes them against args_sha256 and refuses a mismatch. Not sent when they
            # hold credential-shaped text or are too large: the prompt then shows the hash only.
            blob = json.dumps(args or {}, sort_keys=True, default=str)
            if len(blob) <= 65536 and not shaping.scan_credentials(args or {}):
                op["args"] = json.loads(blob)
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
        to (connection, seat, op) and an expiry. Only a read ever gets one. The store is the one
        class the op references use too (lotrlib/refs.py)."""
        if not raw or tier != "read":
            return None
        return self._cursor_store.issue({"raw": raw, "conn": connection, "client": client, "op": op})

    def _take_cursor(self, cursor, connection, client, op):
        bad = GatewayError("bad_cursor", "the cursor is not one this gateway issued to you "
                           "for this operation, or it expired",
                           ["call again without a cursor to start from the first page"])
        try:
            e = self._cursor_store.get(cursor)
        except refs.RefError:
            if isinstance(cursor, str):
                self._cursors.pop(cursor, None)
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
        return _with_llm(conn, policy.classify(conn, probe))

    def _shape(self, data, prof, select, has_cursor=False, cap=None):
        """-> (data, notes, cut). `cap` is a per-call (chars, held_to_ceiling) from _per_call;
        `cut` is {} unless shaping cut something: then {"truncated": True, "original_chars": N}."""
        lim = self._limits()
        data, notes, cut = shaping.shape_info(
            data, noise_keys=prof.get("noise_keys", ()), select=select,
            max_chars=cap[0] if cap else lim.get("max_result_chars", 24000),
            default_page=lim.get("default_page", 20), has_cursor=has_cursor)
        if cap and cap[1]:
            notes = list(notes) + ["max_result_chars was held to the gateway ceiling of %d" % cap[0]]
        return data, notes, cut

    def _resolve_ref(self, ref_id, connection, client_id, subject):
        """-> the operation name a reference stands for, or the refusal (unknown, spent, expired,
        wrong_target, wrong_session). Spends nothing."""
        hints = {"wrong_target": "a reference works only for the connection it was issued for",
                 "wrong_session": "a reference works only for the caller it was issued to"}
        try:
            e = self._opr.verify(ref_id, {"connection": connection,
                                          "client": client_id or LOCAL_CLIENT,
                                          "seat": self._seat(subject)})
        except refs.RefError as err:
            raise GatewayError(err.code, err.message, [hints.get(
                err.code, "ask for a fresh reference with find(connection, op)")])
        return e["op"]

    def _audit(self, base, identity, tier, verdict, status, detail=None, retry_after_s=None):
        try:
            extra = {"pid": base["pid"]} if base.get("pid") is not None else {}
            if retry_after_s is not None:
                extra["retry_after_s"] = retry_after_s
            if base.get("ref"):
                extra["ref"] = base["ref"]
            if detail:
                extra["detail"] = detail
            audit.record(self.home, connection=base["connection"], identity=identity,
                         op=base["op"], tier=tier, tool=base["tool"], client=base["client"],
                         verdict=verdict, status=status, args_sha256=base["args_sha256"],
                         grant=base.get("grant"), **extra)
        except Exception:
            # An audit failure must be visible, but never turn a served call into a crash.
            pass

    # -- status / catalog ------------------------------------------------------------------

    def _note_health(self, cid, code):
        """Record a connection's latest outcome: code None is a success; a sign-in or down code
        is a failure; any other code (the caller's or a policy's) is not about the connection."""
        if not cid:
            return
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:        # 0.5.1: calls now finish concurrently; the counters move together
            h = self._health.setdefault(cid, {"last_ok": None, "last_error": None, "ok_seq": 0,
                                              "err_seq": 0})
            self._seq = getattr(self, "_seq", 0) + 1
            if code is None:
                h["last_ok"], h["ok_seq"] = now, self._seq
            elif code in SIGN_IN_CODES or code in DOWN_CODES:
                h["last_error"], h["err_seq"] = {"code": code, "at": now}, self._seq

    def _note_sub_endpoint(self, conn, op, args, err):
        """0.22.0: write one call's result against the sub-endpoint it went to, for a connection that
        tracks health. -> None, or, for a failure that is that sub-endpoint's own, the fields the
        error response adds ({sub_endpoint, health, hint}); the connection itself is then not marked
        down. A health record never breaks a call: any trouble writing it is dropped."""
        if not conn or not endpoint_health.tracked(conn):
            return None
        sub = endpoint_health.resolve(conn, op, args)
        if not sub or (err is not None and err.code not in SUB_FAILURE_CODES):
            return None
        try:
            rec = endpoint_health.note(
                self.home, conn["id"], sub, err is None, now=self._clock(),
                hours=endpoint_health.expires_h(conn),
                code=None if err is None else (getattr(err, "upstream_status", None) or err.code),
                message="" if err is None else "%s: %s" % (err.code, err.message))
        except Exception:                                   # noqa: BLE001
            return None
        if err is None:
            return None
        fields, hint = endpoint_health.error_fields(sub, rec)
        fields["hint"] = hint
        return fields

    def _state(self, c):
        state, fix = self._connection_state(c)
        if state in ("connected", "not used yet") and endpoint_health.tracked(c):
            recs = endpoint_health.view(self.home, c["id"], self._clock(), endpoint_health.expires_h(c))
            if endpoint_health.degraded(c, recs):         # one healthy sub-endpoint keeps it from here
                return "error", ("every sub-endpoint is failing: lotr read %s endpoint_health" % c["id"])
        return state, fix

    def _connection_state(self, c):
        if not c.get("enabled", True):
            return "disabled", None
        h = self._health.get(c["id"]) or {}
        err = h.get("last_error")
        if err and h.get("err_seq", 0) > h.get("ok_seq", 0):
            if err["code"] in SIGN_IN_CODES:
                oauth = (c.get("auth") or {}).get("scheme") == "oauth"
                return "needs sign-in", ("run: lotr login %s" % c["id"] if oauth else
                                         "sign in again with the client that owns its token")
            return "error", "check the server is up and reachable, then: lotr status --check"
        if h.get("last_ok"):
            return "connected", None
        return "not used yet", None

    def _probe_health(self, c):
        """0.5.1: one GET of the connection's `health` path (no credential is sent; only keyless
        connections carry one). 200 is up; anything else, or no answer, is down. No redirect is
        followed: a 3xx is down as redirect_refused (a probe that followed one could land on plain
        http and call the connection healthy)."""
        import urllib.error
        import urllib.request
        try:
            path = c["health"].split(" ", 1)[1]
            req = urllib.request.Request(c["base_url"].rstrip("/") + path, method="GET")
            extra = []
            if c.get("trust_ca"):            # 0.5.1: the same private-CA rules as a call
                from . import trustca
                extra = [trustca.https_handler(trustca.verified_pem(self.home, c), c["id"])]
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _probe_no_redirect(), *extra)
            with opener.open(req, timeout=5) as r:
                self._note_health(c["id"], None if r.status == 200 else "downstream_error")
        except urllib.error.HTTPError:
            self._note_health(c["id"], "downstream_error")
        except GatewayError as e:                               # a private CA gone or changed
            self._note_health(c["id"], e.code if e.code in DOWN_CODES else "network_error")
        except Exception as e:                                  # noqa: BLE001
            tls = None
            if c.get("trust_ca"):
                from . import trustca
                tls = trustca.tls_error(e, c)
            self._note_health(c["id"], tls.code if tls else "network_error")

    def _probe(self, c):
        """One read-only check of an mcp connection: its tools/list (network and sign-in in one)."""
        if c.get("kind") == "http":
            return self._probe_health(c)
        try:
            prof = profiles.for_connection(c)
            resolver = self._secret_resolver or self._brokered_resolver(None, None)
            kw = {"secret_resolver": resolver} if resolver else {}
            if c.get("trust_ca") and c.get("transport") != "stdio":
                from . import trustca
                kw["ca_pem"] = trustca.verified_pem(self.home, c)
            if (c.get("auth") or {}).get("scheme") == "oauth":
                kw.update(self._oauth_kwargs(None, None))
            (self._stdio_factory() if c.get("transport") == "stdio" else McpConnection)(
                c, prof, **kw).discover()
            self._note_health(c["id"], None)
        except GatewayError as e:
            self._note_health(c["id"], e.code if (e.code in SIGN_IN_CODES or e.code in DOWN_CODES)
                              else "downstream_error")
        except Exception:                                       # noqa: BLE001
            self._note_health(c["id"], "network_error")

    def status(self, check=False, remote=False):
        from . import secrets as secrets_mod
        if check:
            # 0.5.1: the probes wait on the downstream (an MCP server may take a minute to start),
            # so they run with no lock held; the rows below are built under it, from what they found
            with self._lock:
                self.reload_if_changed()
                todo = [c for c in self.registry.connections.values()
                        if c.get("enabled", True) and (c.get("kind") == "mcp" or c.get("health"))]
            for c in todo:
                self._probe(c)
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
                wins = [{"id": w["id"], "op": w.get("op_filter"), "expires_at": w["expires_at"],
                         "expires": time.strftime("%Y-%m-%dT%H:%M:%S",
                                                  time.localtime(w["expires_at"]))}
                        for w in self._live_windows() if w.get("connection") == c["id"]]
                if wins:                         # 0.5.1: an open consent window is never silent
                    row["consent_windows"] = wins
                if c.get("headers"):             # 0.5.1: names only, never values
                    row["static_headers"] = sorted(c["headers"])
                if c.get("trust_ca"):            # 0.5.1: a private CA is never silent
                    from . import trustca
                    row["trust_ca"] = trustca.status_info(self.home, c)
                if (c.get("auth") or {}).get("scheme") == "oauth":
                    from . import oauth
                    ob = c["auth"]["oauth"]
                    row["oauth"] = dict(
                        {"issuer": ob.get("issuer"), "scopes": ob.get("scopes"),
                         "client_source": ob.get("client_id_source")},
                        **oauth.status_of(c))       # adds refresh_storage / refresh_token wording
                    vouched = sorted(oauth.NetPolicy.from_block(ob).insecure_metadata_hosts)
                    if vouched:                  # 0.5.0: an exemption is never silent
                        row["oauth"]["insecure_metadata_hosts"] = vouched
                if c.get("locality"):            # 0.5.1: a local engine is labelled as one
                    row["locality"] = c["locality"]
                if c.get("provider"):
                    row["provider"] = c["provider"]
                if (c.get("llm") or {}).get("tools"):    # 0.5.1: what a model op can trigger
                    row["llm_tools"] = c["llm"]["tools"]
                if c.get("limits"):
                    row["limits"] = dict(c["limits"])
                if (c.get("timeouts") or {}).get("call_s") and c.get("kind") == "http":
                    row["call_timeout_s"] = c["timeouts"]["call_s"]
                checked = bool(check and c.get("enabled", True)
                               and (c.get("kind") == "mcp" or c.get("health")))
                if c.get("transport") == "stdio":
                    row["transport"] = "stdio"
                    row["process"] = conn_stdio.process_info(c["id"])
                state, fix = self._state(c)
                if endpoint_health.tracked(c):   # 0.22.0: which backend works, not only whether the connection does
                    recs = endpoint_health.view(self.home, c["id"], self._clock(),
                                                endpoint_health.expires_h(c))
                    row["sub_endpoints"] = endpoint_health.rows(c, recs)
                    row["degraded"] = endpoint_health.degraded(c, recs)
                h = self._health.get(c["id"]) or {}
                row.update(state=state, last_ok=h.get("last_ok"), last_error=h.get("last_error"),
                           checked=checked if check else None)
                if fix:
                    row["fix"] = fix
                conns.append(row)
            pg = self.settings.get("presence") or {}
            gate = {"writes": bool(pg.get("gate_writes")), "reads": bool(pg.get("gate_reads"))}
            gate["present"] = (self._present(pg) is not None) if (gate["writes"] or gate["reads"]) \
                else False
            out = {"ok": True, "zone": self.registry.zone, "mode": self.settings["mode"],
                   "connections": conns, "clients": len(self.registry.clients),
                   "recipes": len(self.recipes), "index_docs": len(self.index.docs)
                   if hasattr(self.index, "docs") else None}
            if not remote:                  # a hub client is not told whether you are at the machine
                mig = conn_stdio.migration_state(self.home)
                if mig and mig.get("count"):        # phase 112: trusted at upgrade, never silent
                    out["approvals_migrated"] = mig["count"]
                out["presence_gate"] = gate
                out["confirm"] = self._confirm_mode
                notes = []
                if self._confirm_mode == "none":
                    notes.append("local.confirm is none: consent-tier operations run with no "
                                 "prompt (set at start; recorded in the audit log)")
                now = (self.settings.get("local") or {}).get("confirm", "auto")
                if now != self._confirm_mode:
                    notes.append("gateway.json local.confirm changed after start and is ignored; "
                                 "restart the daemon to apply it")
                if notes:
                    out["notes"] = notes
                from . import config as _config
                notes = _config.listen_notes(self.settings.get("listen"))
                if notes:                   # phase 120: a switch that widens access is never silent
                    out["notes"] = out.get("notes", []) + list(notes)
            return out

    def _presence_gate(self, conn, connection, tier):
        """-> None (nothing to do), or "presence:<age_s>" when a person's unlock session lets this
        hub call through; raises the refusal otherwise. Only the authority is asked, never the caller."""
        pg = self.settings.get("presence") or {}
        if tier == "read":
            if not pg.get("gate_reads"):
                return None
        elif not pg.get("gate_writes"):
            raise GatewayError("oauth_unattended_refused",
                               f"{connection} is an OAuth sign-in; {tier} operations are "
                               "never available to an unattended hub client")
        ls = self.settings.get("listen") or {}
        if ls.get("mcp_http") and ls.get("mcp_http_trust_loopback"):
            # That route lets any program on this machine name an enrolled client with no secret,
            # so "a person is here" says nothing about who is calling (review, 2026-10-24).
            raise GatewayError("oauth_unattended_refused",
                               f"{connection}: presence cannot gate a hub client while "
                               "listen.mcp_http_trust_loopback is on",
                               hints=["any program on this machine can then name a client with no "
                                      "secret; turn trust_loopback off and send the Bearer header"])
        if tier != "read" and not str((conn.get("auth") or {}).get("token_ref") or "").startswith("store:"):
            raise GatewayError("oauth_unattended_refused",
                               f"{connection} is a sealed OAuth sign-in; a hub client cannot use it "
                               "even while you are here",
                               hints=["reconnect it with --l1 (a mode-600 file) to allow "
                                      "presence-gated writes"])
        idle = self._present(pg)
        if idle is None:
            raise GatewayError("presence_required",
                               f"{connection}: a {tier} call from a hub client needs you at this "
                               "machine, and gt unlock does not show an open session",
                               hints=["open your terminal and run `gt_unlock.py unlock`, "
                                      "then repeat the call"])
        return "presence:%d" % idle

    def _present(self, pg):
        """-> idle seconds when the authority says a session is open and was used within
        presence.timeout_s; None on anything else, including any error."""
        try:
            r = self._unlock.presence()
            idle = r.get("age_s")
            if (r.get("active") is True and isinstance(idle, int) and not isinstance(idle, bool)
                    and 0 <= idle <= int(pg.get("timeout_s", 1800))):
                return idle
        except Exception:                                   # noqa: BLE001 - fail closed
            pass
        return None

    def _live_windows(self):
        try:
            return windows.list_windows(self.home, self._clock())
        except GatewayError:
            return []

    def consent_tier_of(self, connection, op):
        """The tier `op` runs at on `connection` (for `lotr grant open --op`); raises if unknown."""
        conn = self.registry.connection(connection)
        opd = profiles.resolve_op(profiles.for_connection(conn), op)
        return self._classify(conn, opd, {})

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
