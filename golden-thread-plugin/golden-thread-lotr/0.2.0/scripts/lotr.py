#!/usr/bin/env python3
"""lotr: the gt-lotr command line.

    lotr [--home H | --zone Z] <command> ...

Call plane (goes through the daemon, or the hub in mode client):
    find [QUERY] [--connection C] [--limit N] [--schema]
    read|write|consent CONN OP [--args JSON] [k=v ...] [--select S] [--cursor X]
    status
    catalog

Admin plane (edits the files under the home directly; refused in mode client):
    init --zone Z --mode local|hub|client
    add-http ID --profile P --base-url U --identity S --auth bearer|basic|header|none
             [--token-ref R] [--user U] [--header H] [--description D] [--trust T]
    add-mcp  ID --endpoint URL --identity S [--auth-ref file:/abs/tokens.json#access_token]
             [--refresh-cmd "<the MCP client's refresh helper>"]   (0.2.0: SSO/OAuth MCP)
    enroll CLIENT --machine M [--allow GLOB ...] [--max-tier T] --secret-out PATH
    revoke CLIENT
    connections

Stdout is JSON. Exit 0 on success, 1 on a gateway error, 2 on a usage error.
`enroll` writes the new client secret to --secret-out (mode 600) and never prints it.
"""
import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lotrlib.errors import GatewayError          # noqa: E402
from lotrlib.util import lotr_home, read_json, write_json   # noqa: E402

MODES = ("local", "hub", "client", "hybrid")


def _out(obj):
    sys.stdout.write(json.dumps(obj, indent=2) + "\n")
    sys.stdout.flush()


def _home(ns, zone=None):
    if ns.home:
        return Path(ns.home).expanduser()
    return lotr_home(zone or ns.zone)


def _parse_kv(pairs):
    out = {}
    for p in pairs:
        if "=" not in p:
            raise _Usage(f"expected k=v, got {p!r}")
        k, v = p.split("=", 1)
        if not k:
            raise _Usage(f"empty key in {p!r}")
        try:
            out[k] = json.loads(v)
        except ValueError:
            out[k] = v
    return out


class _Usage(Exception):
    pass


# ------------------------------------------------------------------ call plane

def _client(ns):
    from lotrlib.client import Client
    return Client.from_home(_home(ns))


def cmd_find(ns):
    params = {"query": ns.query or "", "detail": "schema" if ns.schema else "summary"}
    if ns.connection:
        params["connection"] = ns.connection
    if ns.limit is not None:
        params["limit"] = ns.limit
    return _client(ns).request("find", params)


def cmd_call(ns):
    args = {}
    if ns.args:
        try:
            args = json.loads(ns.args)
        except ValueError:
            raise _Usage("--args is not valid JSON")
        if not isinstance(args, dict):
            raise _Usage("--args must be a JSON object")
    args.update(_parse_kv(ns.kv))
    params = {"tool": "call_" + ns.cmd, "connection": ns.conn, "op": ns.op, "args": args}
    if ns.select:
        params["select"] = ns.select
    if ns.cursor:
        params["cursor"] = ns.cursor
    return _client(ns).request("call", params)


def cmd_status(ns):
    return _client(ns).request("status", {})


def cmd_catalog(ns):
    return _client(ns).request("catalog", {})


# ------------------------------------------------------------------ admin plane

def _admin_home(ns):
    home = _home(ns)
    cfg = read_json(home / "gateway.json") if (home / "gateway.json").exists() else None
    if cfg is None:
        raise GatewayError("not_initialized", f"no gateway.json in {home}",
                           hints=[f"lotr --home {home} init --zone personal --mode local"])
    if cfg.get("mode") == "client":
        raise GatewayError("admin_refused",
                           "this home is in mode client; administer the hub instead")
    return home


def _registry(home):
    from lotrlib.registry import Registry       # lazy: only the admin plane needs it
    return Registry, Registry.load(home / "registry.json")


def cmd_init(ns):
    zone = ns.init_zone or ns.zone or "personal"
    if ns.mode == "hybrid":
        raise GatewayError("not_implemented", "mode hybrid is not implemented in 0.1.0",
                           hints=["use mode local, hub or client"])
    home = _home(ns, zone)
    gw = home / "gateway.json"
    if gw.exists():
        raise GatewayError("already_initialized", f"{gw} already exists")
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    cfg = {
        "schema": 1, "zone": zone, "mode": ns.mode, "socket": None,
        "listen": {"host": "127.0.0.1", "port": 8765, "tls_cert": None, "tls_key": None},
        "hub": {"url": None, "client_id": None, "credential_ref": None, "timeout_s": 8},
        "local_connections": [], "offline": "fail-closed",
        "limits": {"max_result_chars": 24000, "default_page": 20},
    }
    write_json(gw, cfg)
    reg = home / "registry.json"
    wrote = [str(gw)]
    if not reg.exists():
        write_json(reg, {"schema": 1, "zone": zone, "connections": [], "clients": []})
        wrote.append(str(reg))
    return {"ok": True, "home": str(home), "zone": zone, "mode": ns.mode, "wrote": wrote}


def cmd_add_http(ns):
    home = _admin_home(ns)
    _, reg = _registry(home)
    if ns.auth != "none" and not ns.token_ref:
        raise _Usage(f"--auth {ns.auth} needs --token-ref")
    host = urlparse(ns.base_url).hostname
    entry = {
        "id": ns.id, "identity": ns.identity, "zone": reg.zone, "kind": "http",
        "profile": ns.profile, "description": ns.description or "",
        "base_url": ns.base_url,
        "auth": {"scheme": ns.auth, "token_ref": ns.token_ref, "user": ns.user,
                 "header": ns.header},
        "headers": {}, "network": {"hosts": [host] if host else []},
        "trust": ns.trust, "policy": {"deny": [], "consent": [], "write": [], "read": []},
        "enabled": True,
    }
    reg.add_connection(entry)
    reg.save(home / "registry.json")
    return {"ok": True, "added": ns.id}


def cmd_add_mcp(ns):
    """Register an SSO/OAuth-protected MCP endpoint (0.2.0): the token by REF, refreshed by the
    MCP client's own helper; its tools discovered now and cached, so find works at once."""
    import shlex
    from lotrlib.conn_mcp import McpConnection
    from lotrlib.registry import validate_connection
    home = _admin_home(ns)
    _, reg = _registry(home)
    host = urlparse(ns.endpoint).hostname
    entry = {
        "id": ns.id, "identity": ns.identity, "zone": reg.zone, "kind": "mcp",
        "profile": "mcp", "description": ns.description or "",
        "endpoint": ns.endpoint, "transport": ns.transport,
        "auth": {"scheme": "bearer" if ns.auth_ref else "none", "token_ref": ns.auth_ref},
        "refresh_cmd": shlex.split(ns.refresh_cmd) if ns.refresh_cmd else None,
        "network": {"hosts": [host] if host else []}, "trust": ns.trust,
        "policy": {"deny": [], "consent": [], "write": [], "read": []}, "tools": [],
        "enabled": True,
    }
    validate_connection(entry, reg.zone)       # before any network: a literal token stops here
    entry["tools"] = McpConnection(entry, None).discover()
    reg.add_connection(entry)
    reg.save(home / "registry.json")
    return {"ok": True, "added": ns.id, "tools": len(entry["tools"])}


def cmd_enroll(ns):
    home = _admin_home(ns)
    out = Path(ns.secret_out).expanduser()
    if os.path.lexists(out):
        raise GatewayError("secret_out_exists", f"{out} already exists; refusing to overwrite")
    _, reg = _registry(home)
    secret = reg.enroll(ns.client, ns.machine, ns.allow or ["*"], ns.max_tier)
    fd = os.open(str(out), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (secret + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    del secret
    try:
        reg.save(home / "registry.json")
    except BaseException:
        os.unlink(out)                        # no orphan secret for a client never saved
        raise
    return {"ok": True, "client": ns.client, "secret_out": str(out),
            "next": f"copy {out} to the client machine and set hub.credential_ref to file:<path>"}


def cmd_revoke(ns):
    home = _admin_home(ns)
    _, reg = _registry(home)
    reg.revoke(ns.client)
    reg.save(home / "registry.json")
    return {"ok": True, "revoked": ns.client}


def cmd_connections(ns):
    home = _admin_home(ns)
    _, reg = _registry(home)
    conns = [{k: c.get(k) for k in ("id", "identity", "profile", "base_url", "trust", "enabled")}
             for c in reg.connections.values()]
    clients = [{k: c.get(k) for k in ("id", "machine", "allow", "max_tier", "revoked")}
               for c in reg.clients.values()]
    return {"ok": True, "zone": reg.zone, "connections": conns, "clients": clients}


# ------------------------------------------------------------------ parser

class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write(f"lotr: {message}\n")
        sys.exit(2)


def build_parser():
    p = _Parser(prog="lotr", description="gt-lotr command line")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--home", help="gateway home (default ~/.config/gt-lotr/<zone>)")
    g.add_argument("--zone", help="zone name for the default home (default personal)")
    sub = p.add_subparsers(dest="cmd", parser_class=_Parser)
    sub.required = True

    f = sub.add_parser("find")
    f.add_argument("query", nargs="?", default="")
    f.add_argument("--connection")
    f.add_argument("--limit", type=int)
    f.add_argument("--schema", action="store_true")
    f.set_defaults(fn=cmd_find)

    for name in ("read", "write", "consent"):
        c = sub.add_parser(name)
        c.add_argument("conn")
        c.add_argument("op")
        c.add_argument("kv", nargs="*", help="k=v args (v parsed as JSON when it parses)")
        c.add_argument("--args", help="JSON object of args")
        c.add_argument("--select")
        c.add_argument("--cursor")
        c.set_defaults(fn=cmd_call)

    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("catalog").set_defaults(fn=cmd_catalog)

    i = sub.add_parser("init")
    i.add_argument("--zone", dest="init_zone")
    i.add_argument("--mode", choices=MODES, default="local")
    i.set_defaults(fn=cmd_init)

    a = sub.add_parser("add-http")
    a.add_argument("id")
    a.add_argument("--profile", required=True)
    a.add_argument("--base-url", required=True)
    a.add_argument("--identity", required=True)
    a.add_argument("--auth", required=True, choices=("bearer", "basic", "header", "none"))
    a.add_argument("--token-ref")
    a.add_argument("--user")
    a.add_argument("--header")
    a.add_argument("--description")
    a.add_argument("--trust", default="T0", choices=("T0", "T1", "T2"))
    a.set_defaults(fn=cmd_add_http)

    m = sub.add_parser("add-mcp")
    m.add_argument("id")
    m.add_argument("--endpoint", required=True)
    m.add_argument("--transport", default="http", choices=("http",))
    m.add_argument("--identity", required=True)
    m.add_argument("--auth-ref", help="a secret REF to the bearer token, e.g. "
                                      "file:/abs/tokens.json#access_token -- never the token")
    m.add_argument("--refresh-cmd", help="the MCP client's own refresh helper, run on HTTP 401")
    m.add_argument("--description")
    m.add_argument("--trust", default="T1", choices=("T0", "T1", "T2"))
    m.set_defaults(fn=cmd_add_mcp)

    e = sub.add_parser("enroll")
    e.add_argument("client")
    e.add_argument("--machine", required=True)
    e.add_argument("--allow", nargs="+")
    e.add_argument("--max-tier", default="read", choices=("read", "write", "consent"))
    e.add_argument("--secret-out", required=True)
    e.set_defaults(fn=cmd_enroll)

    r = sub.add_parser("revoke")
    r.add_argument("client")
    r.set_defaults(fn=cmd_revoke)

    sub.add_parser("connections").set_defaults(fn=cmd_connections)
    return p


def main(argv=None):
    parser = build_parser()
    ns, extra = parser.parse_known_args(argv)
    if extra:
        # k=v pairs given after an option (`read C OP --select S k=v`) land here
        if ns.cmd in ("read", "write", "consent") and all(
                "=" in x and not x.startswith("-") for x in extra):
            ns.kv = list(ns.kv) + extra
        else:
            parser.error("unrecognized arguments: " + " ".join(extra))
    try:
        result = ns.fn(ns)
    except _Usage as e:
        sys.stderr.write(f"lotr: {e}\n")
        return 2
    except GatewayError as e:
        _out({"ok": False, "error": e.to_dict()})
        return 1
    except Exception as e:                    # noqa: BLE001 - JSON out, never a traceback
        _out({"ok": False, "error": {"code": "internal", "message": type(e).__name__, "hints": []}})
        return 1
    _out(result)
    if isinstance(result, dict) and result.get("ok") is False:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
