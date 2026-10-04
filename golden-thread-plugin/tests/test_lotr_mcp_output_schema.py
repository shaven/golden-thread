"""gt-lotr 0.3.0 (gt 0.20.1): outputSchema on the four MCP tools.

Every tools/call result already carried its envelope as structuredContent; the tools now
declare it. MCP added outputSchema in protocol 2025-06-18 ("Servers MUST provide structured
results that conform to this schema"), so:

  * a client that negotiated 2025-06-18 or later sees an outputSchema on every tool, and one
    that negotiated 2025-03-26 sees the tool list exactly as before;
  * EVERY result the shim can produce -- a hit list, a call, a gateway error, a refused
    argument, an unreachable daemon, an internal error, a bare non-object answer -- conforms to
    its tool's schema (checked with a small validator below: type, required, properties,
    enum, items -- the keywords the schemas use).

In-process: the shim is driven with a fake gateway client, so no daemon and no unlock prompt.
"""
import io
import json
import sys
import unittest

from _harness import REPO, latest_version_dir

GW = latest_version_dir(REPO / "golden-thread-lotr")
sys.path.insert(0, str(GW / "scripts"))

import lotr_mcp                                          # noqa: E402
from lotrlib.errors import GatewayError                  # noqa: E402

TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int,
         "number": (int, float), "null": type(None)}


def conforms(value, schema, where="$"):
    """-> [problems]. The subset of JSON Schema the tool schemas use."""
    out = []
    t = schema.get("type")
    if t is not None:
        ok = any(isinstance(value, TYPES[x]) and not (x in ("integer", "number")
                                                       and isinstance(value, bool))
                 for x in (t if isinstance(t, list) else [t]))
        if not ok:
            return ["%s: %r is not %s" % (where, value, t)]
    if "enum" in schema and value not in schema["enum"]:
        out.append("%s: %r not in %s" % (where, value, schema["enum"]))
    if isinstance(value, dict):
        for k in schema.get("required", []):
            if k not in value:
                out.append("%s: missing %s" % (where, k))
        for k, sub in (schema.get("properties") or {}).items():
            if k in value:
                out += conforms(value[k], sub, "%s.%s" % (where, k))
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, v in enumerate(value):
            out += conforms(v, schema["items"], "%s[%d]" % (where, i))
    return out


class _NoUnlock:
    @staticmethod
    def enabled():
        return False


class FakeClient:
    def __init__(self, answers):
        self.answers = answers

    def request(self, method, params):
        a = self.answers.get(method)
        if isinstance(a, Exception):
            raise a
        return a


class OutputSchemaTests(unittest.TestCase):
    def shim(self, answers):
        out = io.StringIO()
        s = lotr_mcp.Shim("/nonexistent", out=out, client_factory=lambda: FakeClient(answers),
                          unlock=_NoUnlock)
        return s, out

    def session(self, version, answers, calls=()):
        s, out = self.shim(answers)
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": version, "capabilities": {},
                            "clientInfo": {"name": "t", "version": "0"}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        for i, (name, args) in enumerate(calls, 3):
            msgs.append({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                         "params": {"name": name, "arguments": args}})
        for m in msgs:
            s.handle_line(json.dumps(m))
        return {m["id"]: m for m in map(json.loads, out.getvalue().splitlines())}

    def test_declared_from_2025_06_18_on_and_absent_before(self):
        for version, has in (("2025-03-26", False), ("2025-06-18", True), ("2025-11-25", True)):
            with self.subTest(version=version):
                by = self.session(version, {"catalog": {"text": "x"}})
                self.assertEqual(by[1]["result"]["protocolVersion"], version)
                tools = by[2]["result"]["tools"]
                self.assertEqual([t["name"] for t in tools],
                                 ["find", "call_read", "call_write", "call_consent"])
                for t in tools:
                    self.assertEqual("outputSchema" in t, has, t["name"])
                    if has:
                        self.assertEqual(t["outputSchema"]["type"], "object")
                        self.assertIn("ok", t["outputSchema"]["required"])

    def test_every_result_shape_conforms_to_its_tool_schema(self):
        schemas = {t["name"]: t["outputSchema"] for t in lotr_mcp.tools_for("2025-06-18")}
        call = {"connection": "github@personal", "op": "list_pulls"}
        cases = [
            ("find", {"query": "pulls"}, {"find": {"ok": True, "zone": "personal",
                                                   "results": [{"connection": "c", "op": "o"}],
                                                   "notes": []}}),
            ("find", {}, {"find": GatewayError("daemon_unreachable", "no daemon")}),
            ("find", {}, {"find": RuntimeError("boom")}),
            ("call_read", call, {"call": {"ok": True, "connection": "github@personal",
                                          "op": "list_pulls", "tier": "read", "data": [1],
                                          "next_cursor": None, "notes": [],
                                          "untrusted": True}}),
            ("call_read", call, {"call": {"ok": False, "error": {"code": "denied",
                                                                 "message": "no", "hints": []}}}),
            ("call_write", {"connection": "c"}, {}),                       # refused argument
            ("call_consent", dict(call, args="x"), {}),                    # args not an object
            ("call_read", call, {"call": ["a bare list"]}),               # not an object
            ("call_read", call, {"call": {"data": 1}}),                   # no `ok`
        ]
        for name, args, answers in cases:
            with self.subTest(name=name, answers=str(answers)[:60]):
                by = self.session("2025-06-18", dict(answers, catalog={"text": "x"}),
                                  [(name, args)])
                res = by[3]["result"]
                sc = res["structuredContent"]
                self.assertEqual([], conforms(sc, schemas[name]))
                self.assertEqual(res["isError"], sc["ok"] is False)
                self.assertEqual(json.loads(res["content"][0]["text"]), sc)

    def test_the_validator_itself_catches_a_bad_envelope(self):
        schema = lotr_mcp.OUTPUT_SCHEMAS["call_read"]
        self.assertTrue(conforms({"data": 1}, schema))
        self.assertTrue(conforms({"ok": False, "error": {"code": 1}}, schema))


if __name__ == "__main__":
    unittest.main()
