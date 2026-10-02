"""The engine: one registry, one index, one policy -- behind every front end.

Both the MCP shim and the `lotr` CLI reach this object through the daemon, so identity
stamping, tier enforcement, shaping and the audit log are written once (ADR-1).

The four verbs the outside world sees map onto two methods:
  find(query)                         -> search the index (or list connections on "")
  call(tool, connection, op, args)    -> tool is call_read | call_write | call_consent

The GATEWAY assigns an op's tier, never the model. The model only picks which tool to use; an
op sent through a tool below its tier is refused with a hint naming the right one.
"""
import hashlib
import json
import os
import threading
from pathlib import Path

from . import audit, confirm as confirm_mod, policy, profiles, recipes as recipes_mod, shaping
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


def _max_tier(a, b):
    return a if TIER_RANK[a] >= TIER_RANK[b] else b


def _args_hash(args):
    blob = json.dumps(args or {}, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


class Engine:
    def __init__(self, home, *, secret_resolver=None, connection_factory=None, dialog=None):
        self.home = Path(home)
        self._dialog = dialog            # tests inject a fake; None = the real macOS dialog
        self._secret_resolver = secret_resolver
        self._connection_factory = connection_factory or HttpConnection
        self._lock = threading.RLock()
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
            self._stamp = self._current_stamp()

    def reload_if_changed(self):
        """Hot reload: adding a connection never needs a restart (ADR-1)."""
        if self._current_stamp() != self._stamp:
            self._load()
            return True
        return False

    # -- helpers ---------------------------------------------------------------------------

    def _client(self, client_id):
        """The policy subject for a caller. Local callers are NOT exempt (ADR-5): they get the
        allow list and tier ceiling from gateway.json `local`, like an enrolled client."""
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

    def find(self, query="", connection=None, limit=8, detail="summary", client_id=None):
        try:
            with self._lock:
                self.reload_if_changed()
                client = self._client(client_id)
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

    def call(self, tool, connection, op, args=None, select=None, cursor=None, client_id=None):
        args = dict(args or {})
        base = {"tool": tool, "connection": connection, "op": op,
                "client": client_id or LOCAL_CLIENT, "args_sha256": _args_hash(args)}
        tier = None
        identity = None
        try:
            with self._lock:
                self.reload_if_changed()
                if tool not in TOOLS:
                    raise GatewayError("bad_tool", f"tool must be one of {', '.join(TOOLS)}")
                conn = self.registry.connection(connection)
                identity = conn.get("identity")
                client = self._client(client_id)
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
                policy.check_client(client, connection, tier)
                if tier == "consent":
                    mode = (self.settings.get("local") or {}).get("confirm", "auto")
                    text = confirm_mod.describe(connection, identity, op, args,
                                                client_id or LOCAL_CLIENT)
                    confirm_mod.confirm(mode, text, dialog=self._dialog)
                factory = self._connection_factory
                if conn.get("kind") == "mcp" and factory is HttpConnection:
                    factory = McpConnection           # 0.2.0: an SSO/OAuth MCP downstream
                http = factory(
                    conn, prof, **({"secret_resolver": self._secret_resolver}
                                   if self._secret_resolver else {}))
                result = None
                for i, (opd, a) in enumerate(plan):
                    result = http.call(opd, a, cursor=cursor if i == len(plan) - 1 else None)
            data, notes = self._shape(result.get("data"), prof, select)
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
            return env
        except GatewayError as e:
            self._audit(base, identity, tier, "error:" + e.code, None)
            return {"ok": False, "error": e.to_dict()}

    def _classify(self, conn, opd, args):
        probe = {"name": opd.get("name"), "method": opd["method"], "path": opd["path"],
                 "tier": opd.get("tier")}
        if opd.get("graphql"):
            probe["graphql_query"] = (args or {}).get("query", "")
        return policy.classify(conn, probe)

    def _shape(self, data, prof, select):
        lim = self._limits()
        return shaping.shape(data, noise_keys=prof.get("noise_keys", ()), select=select,
                             max_chars=lim.get("max_result_chars", 24000),
                             default_page=lim.get("default_page", 20))

    def _audit(self, base, identity, tier, verdict, status):
        try:
            audit.record(self.home, connection=base["connection"], identity=identity,
                         op=base["op"], tier=tier, tool=base["tool"], client=base["client"],
                         verdict=verdict, status=status, args_sha256=base["args_sha256"])
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
                        cred = {"scheme": d.get("scheme"), "present": d.get("present")}
                    except GatewayError as e:
                        cred = {"error": e.code}
                conns.append({"id": c["id"], "identity": c.get("identity"), "profile": c.get("profile"),
                              "trust": c.get("trust"), "enabled": c.get("enabled", True),
                              "credential": cred})
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
                line = f"- {c['id']}: {c.get('description') or c.get('identity') or ''}"[:140] + "\n"
                if used + len(line) > max_chars - 40:
                    break
                lines.append(line)
                used += len(line)
                shown += 1
            tail = f"(+{len(active) - shown} more: find(''))" if shown < len(active) else ""
            return (head + "".join(lines) + tail).rstrip()
