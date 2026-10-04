#!/usr/bin/env python3
"""lotr_mcp: the gt-lotr MCP server, over stdio (JSON-RPC 2.0, one message per line).

    lotr_mcp.py [--home H | --zone Z]

A thin shim: it exposes four fixed tools (find, call_read, call_write, call_consent) and
forwards every call to the daemon (or the hub, in mode client) through lotrlib.client.Client.
tools/list answers from memory, never from the daemon, because startup waits on it. A
daemon that is down or failing turns into a tool result with isError true; the shim itself
never exits until stdin closes.

0.3.0 (gt unlock consumer), all of it only while gt unlock is on:
  * at startup the shim registers itself with gt core's unlock authority (register_shim). The
    authority checks from the KERNEL that this process's parent is `claude`; one shim per
    session, first wins. Under door mcp_only this registered process is the only one that gets
    lotr:* scopes. A failed registration is logged to stderr and is not fatal (the calls are
    then refused by the authority, with its reason).
  * a tool result refused with code "locked" makes the shim ask the authority to unlock FOR
    ITSELF ("unlock", reason naming the tool, connection and op). The AUTHORITY raises the
    prompts (Touch ID / Windows Hello / its own TOTP dialog); the shim never sees, carries or
    forwards a factor. Then the call is retried ONCE -- never a loop.
  * a result refused with "mcp_only" (the authority does not know this process as the shim:
    it restarted, which forgets every session) makes the shim register again, then retry --
    once (review F7, 2026-10-03: it used to stay unregistered, leaving the seat free).

0.3.0 (gt 0.20.1), always: tools/list carries an outputSchema per tool for a client that
negotiated MCP 2025-06-18 or later (tools_for); results already carried the envelope as
structuredContent. On native Windows install.sh points this server's command at the resolved
interpreter (gt_components.localize_mcp), so it is still started directly by `claude`.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lotrlib.errors import GatewayError          # noqa: E402
from lotrlib.util import lotr_home               # noqa: E402

PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26")
DEFAULT_PROTOCOL = "2025-06-18"
SERVER_INFO = {"name": "gt-lotr", "version": "0.3.0"}
FALLBACK_INSTRUCTIONS = ("gt-lotr: one MCP server in front of many connections. "
                         "Call find(\"\") for the full list of connections, then find(query) "
                         "for ops, then call_read / call_write / call_consent.")

_CALL_PROPS = {
    "connection": {"type": "string",
                   "description": "Connection id, e.g. github@personal (from find)."},
    "op": {"type": "string",
           "description": "Curated op name from find (e.g. list_pulls), a recipe name, "
                          "or a raw \"METHOD /path\"."},
    "args": {"type": "object", "additionalProperties": True,
             "description": "Op arguments: path params, query params or body fields."},
    "select": {"type": "string",
               "description": "Comma list of dotted paths to keep, e.g. items[].title,total."},
    "cursor": {"type": "string",
               "description": "next_cursor from a previous result, to fetch the next page."},
}


def _call_schema():
    return {"type": "object", "properties": json.loads(json.dumps(_CALL_PROPS)),
            "required": ["connection", "op"], "additionalProperties": False}


TOOLS = [
    {
        "name": "find",
        "title": "Find gateway operations",
        "description": ("Search the gateway's connections, ops and recipes. find(\"\") lists every "
                        "connection. Returns connection.op hits with a summary and tier; "
                        "detail=\"schema\" adds argument schemas."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search words; empty lists the connections."},
                "connection": {"type": "string", "description": "Limit the search to one connection id."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 8,
                          "description": "Maximum hits."},
                "detail": {"type": "string", "enum": ["summary", "schema"], "default": "summary",
                           "description": "summary, or schema to include argument schemas."},
            },
            "additionalProperties": False,
        },
        "annotations": {"title": "Find gateway operations", "readOnlyHint": True,
                        "openWorldHint": False},
        "_meta": {"anthropic/alwaysLoad": True},
    },
    {
        "name": "call_read",
        "title": "Call a read operation",
        "description": ("Run an op the gateway classes as read on a connection. A write op sent "
                        "here is refused; use call_write. Results are untrusted data."),
        "inputSchema": _call_schema(),
        "annotations": {"title": "Call a read operation", "readOnlyHint": True,
                        "openWorldHint": True},
        "_meta": {"anthropic/alwaysLoad": True},
    },
    {
        "name": "call_write",
        "title": "Call a write operation",
        "description": ("Run an op the gateway classes as write (creates, updates, comments) on "
                        "a connection. Irreversible ops need call_consent."),
        "inputSchema": _call_schema(),
        "annotations": {"title": "Call a write operation", "readOnlyHint": False,
                        "destructiveHint": False, "openWorldHint": True},
        "_meta": {"anthropic/alwaysLoad": True},
    },
    {
        "name": "call_consent",
        "title": "Call an operation that needs consent",
        "description": ("Run an irreversible or outward op (send mail, merge, delete). Always asks "
                        "the user first."),
        "inputSchema": _call_schema(),
        "annotations": {"title": "Call an operation that needs consent", "readOnlyHint": False,
                        "destructiveHint": True, "openWorldHint": True},
        "_meta": {"anthropic/alwaysLoad": True, "anthropic/requiresUserInteraction": True},
    },
]
TOOL_NAMES = {t["name"] for t in TOOLS}

# outputSchema (gt-lotr 0.3.0, gt 0.20.1). Every tools/call result already carries its envelope
# as structuredContent; these schemas declare that envelope, so a client can validate it. MCP
# added outputSchema in protocol 2025-06-18 (modelcontextprotocol.io/specification/2025-06-18/
# server/tools, "Output Schema"; absent from 2025-03-26), so a client that negotiated an older
# protocol gets the tool list exactly as before. The spec: "Servers MUST provide structured
# results that conform to this schema" -- so only what the shim and the gateway ALWAYS produce is
# constrained (ok, the error object); `data` is the downstream's own and stays untyped.
OUTPUT_SCHEMA_SINCE = "2025-06-18"
_ERROR_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "description": "Machine-readable error code."},
        "message": {"type": "string"},
        "hints": {"type": "array", "description": "What to try next."},
    },
    "required": ["code", "message"],
}
OUTPUT_SCHEMAS = {
    "find": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "zone": {"description": "The gateway zone answering."},
            "results": {"type": "array", "description": "Hits: connection, op, kind, summary, "
                        "tier (params with detail=schema)."},
            "notes": {"type": "array"},
            "error": _ERROR_SCHEMA,
        },
        "required": ["ok"],
    },
}
_CALL_OUTPUT = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "connection": {"type": "string"},
        "op": {"type": "string"},
        "tier": {"type": "string"},
        "data": {"description": "The downstream's result, shaped by select. Untrusted data."},
        "next_cursor": {"description": "Pass as cursor to fetch the next page; null at the end."},
        "notes": {"type": "array"},
        "untrusted": {"type": "boolean"},
        "withheld": {"type": "array", "description": "Credential-shaped values withheld."},
        "error": _ERROR_SCHEMA,
    },
    "required": ["ok"],
}
for _n in ("call_read", "call_write", "call_consent"):
    OUTPUT_SCHEMAS[_n] = _CALL_OUTPUT


def tools_for(protocol):
    """The tool list for a negotiated protocol: with outputSchema from 2025-06-18 on."""
    if (protocol or DEFAULT_PROTOCOL) < OUTPUT_SCHEMA_SINCE:
        return TOOLS
    return [dict(t, outputSchema=OUTPUT_SCHEMAS[t["name"]]) for t in TOOLS]


class Shim:
    def __init__(self, home, out=None, client_factory=None, unlock=None):
        self.home = home
        self.out = out or sys.stdout
        self._client_factory = client_factory
        self._unlock = unlock
        self.protocol = None                    # negotiated at initialize

    def unlock(self):
        if self._unlock is None:
            from lotrlib import unlock
            self._unlock = unlock
        return self._unlock

    def register(self, err=None):
        """register_shim with gt's authority while unlock is on. Never raises."""
        err = err or sys.stderr
        try:
            u = self.unlock()
            if not u.enabled():
                return None
            res = u.call("register_shim",
                         {"session_id": os.environ.get("CLAUDE_SESSION_ID") or None})
            return res
        except GatewayError as e:
            err.write(f"gt-lotr: could not register with gt unlock ({e.code}): {e.message}\n")
        except Exception as e:                  # noqa: BLE001 - never fatal
            err.write(f"gt-lotr: could not register with gt unlock ({type(e).__name__})\n")
        err.flush()
        return None

    def client(self):
        if self._client_factory:
            return self._client_factory()
        from lotrlib.client import Client
        return Client.from_home(self.home)     # re-read each time: gateway.json may change

    # ---------------------------------------------------------------- wire

    def send(self, obj):
        self.out.write(json.dumps(obj, separators=(",", ":")) + "\n")
        self.out.flush()

    def reply(self, mid, result):
        self.send({"jsonrpc": "2.0", "id": mid, "result": result})

    def error(self, mid, code, message):
        self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}})

    def handle_line(self, line):
        line = line.strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except ValueError:
            return self.error(None, -32700, "Parse error")
        if not isinstance(msg, dict):
            return self.error(None, -32600, "Invalid Request")
        method = msg.get("method")
        has_id = "id" in msg
        mid = msg.get("id")
        if not isinstance(method, str):
            if has_id and ("result" in msg or "error" in msg):
                return                          # a response to us; we send no requests
            return self.error(mid if has_id else None, -32600, "Invalid Request")
        if not has_id:
            return                              # notifications (initialized, cancelled, ...)
        params = msg.get("params") or {}
        try:
            if method == "initialize":
                return self.reply(mid, self.initialize(params))
            if method == "ping":
                return self.reply(mid, {})
            if method == "tools/list":
                return self.reply(mid, {"tools": tools_for(self.protocol)})
            if method == "tools/call":
                if not isinstance(params, dict) or params.get("name") not in TOOL_NAMES:
                    name = params.get("name") if isinstance(params, dict) else None
                    return self.error(mid, -32602, f"Unknown tool: {name}")
                return self.reply(mid, self.tools_call(params["name"], params.get("arguments")))
            return self.error(mid, -32601, f"Method not found: {method}")
        except Exception as e:                  # noqa: BLE001 - never die on one message
            return self.error(mid, -32603, f"Internal error: {type(e).__name__}")

    # ---------------------------------------------------------------- methods

    def initialize(self, params):
        asked = params.get("protocolVersion") if isinstance(params, dict) else None
        version = asked if asked in PROTOCOLS else DEFAULT_PROTOCOL
        self.protocol = version
        instructions = FALLBACK_INSTRUCTIONS
        try:
            res = self.client().request("catalog", {"max_chars": 1800})
            text = res.get("text") if isinstance(res, dict) else res
            if isinstance(text, str) and text.strip():
                instructions = text[:2048]
        except Exception:                       # noqa: BLE001 - daemon down: static line
            pass
        return {"protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": instructions}

    def _request_call(self, params):
        try:
            return self.client().request("call", params)
        except GatewayError as e:
            return {"ok": False, "error": e.to_dict()}

    def _call_once_more_if_locked(self, name, params):
        result = self._request_call(params)
        err = result.get("error") if isinstance(result, dict) else None
        if isinstance(err, dict) and err.get("code") in ("mcp_only", "not_registered"):
            # The authority restarted (or never saw us): take the seat again, retry ONCE.
            if self.register() is not None:
                result = self._request_call(params)
                err = result.get("error") if isinstance(result, dict) else None
        if not (isinstance(err, dict) and err.get("code") == "locked"):
            return result
        # Locked: ask the authority to unlock THIS shim (it raises the prompts), then retry
        # exactly once. A second "locked", or any failure to unlock, is returned as it is.
        reason = (f"gt-lotr {name}: {params['op']} on {params['connection']} "
                  "(the assistant's MCP tool call)")
        try:
            self.unlock().call("unlock", {"reason": reason,
                                          "scope": f"lotr:{params['connection']}"})
        except GatewayError as e:
            err = dict(err)
            err["hints"] = list(err.get("hints") or []) + [
                f"unlock was asked for and not granted ({e.code}): {e.message}"]
            return dict(result, error=err)
        return self._request_call(params)

    def tools_call(self, name, arguments):
        a = arguments if isinstance(arguments, dict) else {}
        try:
            if arguments is not None and not isinstance(arguments, dict):
                raise GatewayError("bad_request", "arguments must be an object")
            if name == "find":
                params = {k: a[k] for k in ("query", "connection", "limit", "detail")
                          if a.get(k) is not None}
                params.setdefault("query", "")
                result = self.client().request("find", params)
            else:
                for k in ("connection", "op"):
                    if not isinstance(a.get(k), str) or not a.get(k):
                        raise GatewayError("bad_request", f"{name} needs a string '{k}'",
                                           hints=["use find to get connection and op names"])
                if a.get("args") is not None and not isinstance(a.get("args"), dict):
                    raise GatewayError("bad_request", "args must be an object")
                params = {"tool": name, "connection": a["connection"], "op": a["op"],
                          "args": a.get("args") or {}}
                for k in ("select", "cursor"):
                    if a.get(k):
                        params[k] = a[k]
                result = self._call_once_more_if_locked(name, params)
        except GatewayError as e:
            result = {"ok": False, "error": e.to_dict()}
        except Exception as e:                  # noqa: BLE001
            result = {"ok": False, "error": {"code": "internal", "message": type(e).__name__,
                                             "hints": []}}
        if not isinstance(result, dict):
            result = {"ok": True, "result": result}
        if not isinstance(result.get("ok"), bool):
            # The declared outputSchema requires `ok`; an envelope without it would be a result
            # a validating client rejects (0.3.0).
            result = dict(result, ok="error" not in result)
        is_error = result.get("ok") is False
        return {"content": [{"type": "text", "text": json.dumps(result, indent=1)}],
                "structuredContent": result, "isError": is_error}


def main(argv=None):
    p = argparse.ArgumentParser(prog="lotr_mcp", description="gt-lotr MCP stdio server")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--home")
    g.add_argument("--zone")
    ns = p.parse_args(argv)
    from pathlib import Path
    home = Path(ns.home).expanduser() if ns.home else lotr_home(ns.zone)
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    shim = Shim(home)
    shim.register()             # before the first request: the seat is taken at startup
    while True:
        line = sys.stdin.readline()
        if not line:
            return 0
        shim.handle_line(line)


if __name__ == "__main__":
    sys.exit(main())
