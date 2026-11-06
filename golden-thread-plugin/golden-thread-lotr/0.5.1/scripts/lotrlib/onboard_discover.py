"""`lotr onboard discover ID`: what does this connection expose? (gt-lotr 0.5.1, plan phase 39)

    manifest(conn, scan=None) -> the per-connection manifest the generated agent and the call script are built from
    discover(home, reg, cid)  -> read the connection's cached MCP tools, scan their descriptions, write the manifest

WHERE THE OPERATIONS COME FROM. The registry holds the tool list the connection discovered when it was added
(`conn["tools"]`), and the gateway rates it with `profiles.mcp_profile`, so a manifest's tiers are exactly the tiers
the gateway enforces. `find` is not the source: it is search-based and budgeted, and carries no argument types.

WHAT THE MANIFEST HOLDS. Per operation: the NAME, the tier, and each argument's name, type and required flag. It holds
NO description text. A description is written by whoever runs the server, so it is untrusted: it is hashed (the
pin: a changed description changes the hash), and the descriptions are sent to the sandboxed reader (gt's
`gt_sandbox_reader.py`: a headless model with no tools) so that a NUMBER of instruction-like sentences is kept.
An operation or argument name outside `[A-Za-z0-9_.-]{1,64}`, or shaped like a credential, is dropped and counted;
more than 200 operations are cut, with the count.

If the reader cannot run (gt not installed, no `claude`, a timeout) the manifest says `not-scanned`. It never says
clean on the strength of nothing.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import profiles, shaping
from .errors import GatewayError
from .util import atomic_write

MAX_OPS = 200
NAME_OK = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
CONN_OK = re.compile(r"^[A-Za-z0-9_.-]{1,64}@[A-Za-z0-9_.-]{1,64}$")
TYPES = ("string", "integer", "number", "boolean", "array", "object")
READER_BUDGET = 190_000
READER_TIMEOUT_S = 120


def _digest(text):
    return hashlib.sha256(str(text or "").encode("utf-8", "replace")).hexdigest()


def _schema_digest(schema):
    try:
        return _digest(json.dumps(schema, sort_keys=True, separators=(",", ":")))
    except (TypeError, ValueError):
        return _digest("")


def _name_ok(name):
    return isinstance(name, str) and bool(NAME_OK.match(name)) and not shaping.scan_credentials(name)


def _type(spec):
    if not isinstance(spec, dict):
        return "unknown"
    t = spec.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null" and isinstance(x, str)), None)
    return t if t in TYPES else "unknown"


def _scan_block(scan):
    if not isinstance(scan, dict) or scan.get("status") != "read":
        return {"status": "not-scanned", "instructions": None, "withheld": None}
    out = {"status": "read", "instructions": int(scan.get("instructions") or 0), "withheld": int(scan.get("withheld") or 0)}
    if scan.get("truncated"):
        out["truncated"] = int(scan["truncated"])
    return out


def manifest(conn, scan=None):
    tools = {t["name"]: t for t in (conn.get("tools") or []) if isinstance(t, dict) and isinstance(t.get("name"), str)}
    ops, dropped_ops, dropped_args, cut = [], 0, 0, 0
    for o in profiles.mcp_profile(conn)["ops"]:
        name = o["name"]
        if not _name_ok(name):
            dropped_ops += 1
            continue
        if len(ops) >= MAX_OPS:
            cut += 1
            continue
        t = tools.get(name, {})
        schema = t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {}
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = {r for r in schema.get("required", []) if isinstance(r, str)} if isinstance(schema.get("required"), list) else set()
        args = []
        for an, spec in props.items():
            if not _name_ok(an):
                dropped_args += 1
                continue
            args.append({"name": an, "type": _type(spec), "required": an in required})
        ops.append({"name": name, "tier": o["tier"], "args": args,
                    "description_sha256": _digest(t.get("description")), "schema_sha256": _schema_digest(schema)})
    return {"schema": 1, "connection": conn.get("id"), "ops": ops, "dropped": {"ops": dropped_ops, "args": dropped_args},
            "cut": cut, "scan": _scan_block(scan)}


# -- the sandboxed reader ----------------------------------------------------------------------

def locate_reader():
    """gt_sandbox_reader.py: in $LOTR_GT_SCRIPTS when that is set (and only there), else beside the installed gt hooks."""
    override = os.environ.get("LOTR_GT_SCRIPTS")
    folders = [Path(override)] if override else [Path.home() / ".claude" / "golden-thread" / "hooks"]
    for f in folders:
        p = f / "gt_sandbox_reader.py"
        if p.is_file():
            return str(p)
    return None


def _not_scanned(why):
    return {"status": "not-scanned", "why": why}


def scan_descriptions(texts):
    """-> {"status": "read", "instructions": n, "withheld": n} or {"status": "not-scanned", "why"}. Counts only."""
    texts = [t for t in texts if isinstance(t, str) and t.strip()]
    if not texts:
        return {"status": "read", "instructions": 0, "withheld": 0}
    reader = locate_reader()
    if not reader:
        return _not_scanned("the sandboxed reader (gt_sandbox_reader.py) was not found; install gt 0.22.0 or later")
    kept, size, truncated = [], 0, 0
    for t in texts:
        n = len(t.encode("utf-8", "replace")) + 2
        if size + n > READER_BUDGET:
            truncated += 1
            continue
        kept.append(t)
        size += n
    try:
        p = subprocess.run([sys.executable, reader, "-"], input="\n\n".join(kept), capture_output=True, text=True,
                           timeout=READER_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        return _not_scanned("the reader could not run (%s)" % type(e).__name__)
    if p.returncode != 0:
        return _not_scanned("the reader exited with status %d" % p.returncode)
    try:
        doc = json.loads(p.stdout)
    except ValueError:
        return _not_scanned("the reader's answer could not be read")
    if not isinstance(doc, dict):
        return _not_scanned("the reader's answer could not be read")
    if doc.get("status") != "read":
        return _not_scanned(str(doc.get("why") or "the reader did not read the text")[:160])
    ins = doc.get("instructions")
    res = {"status": "read", "instructions": len(ins) if isinstance(ins, list) else 0,
           "withheld": int(doc.get("withheld") or 0) if isinstance(doc.get("withheld"), (int, float)) else 0}
    if truncated:
        res["truncated"] = truncated
    return res


# -- writing -----------------------------------------------------------------------------------

def write_manifest(home, cid, m):
    if not isinstance(cid, str) or not CONN_OK.match(cid):
        raise GatewayError("bad_request", "the connection id must look like name@zone (letters, digits, - _ .)")
    folder = Path(home) / "onboard"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(folder, 0o700)
    except OSError:
        pass
    target = folder / ("%s.manifest.json" % cid)
    atomic_write(target, json.dumps(m, indent=2) + "\n", 0o600)
    return target


def _texts(conn):
    out = []
    for t in conn.get("tools") or []:
        if not isinstance(t, dict):
            continue
        out.append(t.get("description"))
        schema = t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {}
        for spec in (schema.get("properties") or {}).values() if isinstance(schema.get("properties"), dict) else []:
            if isinstance(spec, dict):
                out.append(spec.get("description"))
    return out


def discover(home, reg, cid, *, scan=True):
    conn = reg.connection(cid)                              # the registry's own error for an unknown id
    if not (conn.get("tools") or []):
        return {"status": "no_tools", "connection": cid, "ops": 0,
                "next": ["this connection has no cached tools yet; run: lotr setup %s (it signs in and lists them)" % cid]}
    result = scan_descriptions(_texts(conn)) if scan else None
    m = manifest(conn, result)
    path = write_manifest(home, cid, m)
    return {"status": "ok", "connection": cid, "ops": len(m["ops"]), "dropped": m["dropped"], "cut": m["cut"],
            "scan": m["scan"], "file": str(path),
            "by_tier": {t: sum(1 for o in m["ops"] if o["tier"] == t) for t in ("read", "write", "consent")}}
