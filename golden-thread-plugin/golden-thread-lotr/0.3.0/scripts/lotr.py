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
    connect NAME --url HTTPS_MCP_ENDPOINT [--scope S] [--client-id ID] [--identity S] ...
             (0.3.0: LOTR's own OAuth: signs in at THIS terminal, stores the refresh token
              sealed (gt unlock on) or in a mode-600 store file, registers the connection)
    login NAME [--scope S]        re-authenticate (adds scopes; never narrows)
    disconnect NAME               revoke at the server, delete the stored secrets, unregister
    enroll CLIENT --machine M [--allow GLOB ...] [--max-tier T] --secret-out PATH
    revoke CLIENT
    connections

Stdout is JSON. Exit 0 on success, 1 on a gateway error, 2 on a usage error.
`enroll` writes the new client secret to --secret-out (mode 600) and never prints it.

0.3.0: while gt unlock is on, `enroll` and `revoke` first need scope gt:hub:enroll from gt
core's unlock authority with a STEP-UP (a fresh Touch ID / Windows Hello, raised by the
authority; a TOTP code may be typed at this terminal). Nothing is written before it answers.
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


def _split_cmd(text):
    """A command line -> argv. POSIX shell quoting, or on native Windows the Windows rules
    (backslashes are path separators there, and paths are double-quoted) -- 0.3.0."""
    import shlex
    if os.name != "nt":
        return shlex.split(text)
    out = []
    for tok in shlex.split(text, posix=False):
        if len(tok) >= 2 and tok[0] == tok[-1] == '"':
            tok = tok[1:-1]
        out.append(tok)
    return out


def cmd_add_mcp(ns):
    """Register an SSO/OAuth-protected MCP endpoint (0.2.0): the token by REF, refreshed by the
    MCP client's own helper; its tools discovered now and cached, so find works at once."""
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
        "refresh_cmd": _split_cmd(ns.refresh_cmd) if ns.refresh_cmd else None,
        "network": {"hosts": [host] if host else []}, "trust": ns.trust,
        "policy": {"deny": [], "consent": [], "write": [], "read": []}, "tools": [],
        "enabled": True,
    }
    validate_connection(entry, reg.zone)       # before any network: a literal token stops here
    entry["tools"] = McpConnection(entry, None).discover()
    reg.add_connection(entry)
    reg.save(home / "registry.json")
    return {"ok": True, "added": ns.id, "tools": len(entry["tools"])}


# ------------------------------------------------------------------ OAuth (0.3.0)

def _say(msg):
    sys.stderr.write(msg.rstrip("\n") + "\n")
    sys.stderr.flush()


def _conn_id(ns, reg):
    """The connection id a verb was given, checked BEFORE anything else happens (no network, no
    prompt, no file): the registry's own id rule, this registry's zone, a sane length."""
    from lotrlib.registry import CONN_ID
    cid = ns.name if "@" in ns.name else "%s@%s" % (ns.name, reg.zone)
    if len(cid) > 100 or not CONN_ID.match(cid) or ".." in cid:
        raise GatewayError("bad_connection_name", "a connection name is lowercase letters, digits, "
                           ". _ - (starting with a letter or digit), optionally @zone, at most 100 "
                           "characters")
    if cid.split("@", 1)[1] != reg.zone:
        raise GatewayError("bad_connection_name", f"this gateway's zone is {reg.zone}; a connection "
                           "of another zone cannot be registered here")
    return cid


def _read_secret_file(path):
    from lotrlib.secrets import _read_file
    return _read_file("file:" + str(path), Path(path))


def _confirm(ns):
    """The visible consent step of connect/login: show what the server will be asked for and wait
    for a yes at THIS terminal (`--yes` skips the wait for scripted use; the summary still prints)."""
    def ask(info):
        _say("About to sign in to:\n"
             f"  server      {info['resource']}\n"
             f"  hosts       {', '.join(info['hosts'])} (pinned: LOTR will only talk to these)\n"
             f"  issuer      {info['issuer']}\n"
             f"  permissions {' '.join(info['scopes']) or '(none requested: the server decides)'}\n"
             f"  client      {info['client']}\n"
             + "".join(f"  note        {n}\n" for n in info["notes"]))
        if getattr(ns, "yes", False):
            return True
        if not sys.stdin.isatty():
            raise GatewayError("oauth_needs_confirmation", "no terminal to confirm at; run this in "
                               "your own terminal, or pass --yes after reading the summary above")
        sys.stderr.write("Open the browser and sign in? [y/N] ")
        sys.stderr.flush()
        return sys.stdin.readline().strip().lower() in ("y", "yes")
    return ask


def _client_secret_from(ns):
    if getattr(ns, "client_secret_file", None):
        return _read_secret_file(Path(ns.client_secret_file).expanduser())
    if getattr(ns, "client_secret_prompt", False):
        import getpass
        if not sys.stdin.isatty():
            raise _Usage("--client-secret-prompt needs a terminal")
        value = getpass.getpass("client secret (not echoed): ").strip()
        if not value:
            raise _Usage("no client secret was typed")
        return value
    if getattr(ns, "client_secret_stdin", False):
        data = sys.stdin.read().strip()
        if not data:
            raise _Usage("--client-secret-stdin read nothing")
        return data
    return None


def _store_secret(name, value, sealed):
    """-> the ref. sealed: through the authority (a person is at this terminal, so a factor may
    be asked of them); else an L1 store file."""
    from lotrlib import oauth, unlock
    if not sealed:
        return oauth.write_store_secret(name, value)
    unlock.call("seal_put", {"name": name, "value": value, "tty": True},
                answer=unlock.tty_answer())
    return "sealed:" + name


def _drop_secret(ref):
    """Delete what a ref names (only the two kinds LOTR writes). -> True / False."""
    from lotrlib import oauth, unlock
    scheme, _, name = (ref or "").partition(":")
    try:
        if scheme == "store":
            return oauth.delete_store_secret(name)
        if scheme == "sealed":
            unlock.call("seal_rm", {"name": name, "tty": True}, answer=unlock.tty_answer())
            return True
    except GatewayError:
        return False
    return False


def _read_ref(ref):
    """Read a ref in THIS terminal; a sealed one is asked of the authority with the terminal as
    the place a code can be typed."""
    from lotrlib import secrets as secrets_mod, unlock
    if secrets_mod.brokered(ref):
        res = unlock.call("secret", {"ref": ref, "tty": True}, answer=unlock.tty_answer())
        value = res.get("value") if isinstance(res, dict) else None
        if not isinstance(value, str) or not value:
            raise GatewayError("secret_missing", f"secret {ref} resolved to nothing")
        return value
    return secrets_mod.resolve(ref)


def _level(ref):
    if not ref:
        return "none (the server issued no refresh token)"
    if ref.startswith("sealed:"):
        return "L2 (sealed by gt unlock)"
    if os.name == "nt":
        return ("L1 (a file in your profile directory, protected by its ACL, not by mode bits; "
                "any process of this user can read it)")
    return "L1 (a mode-600 file; any process of this user can read it)"


def _finish_login(ns, reg, home, cid, res, existing=None):
    """Persist what a login returned and build the registry entry (the caller saves it).
    -> (entry, old_refs_to_drop). Nothing is written to the registry here; secrets are written
    only after the entry validated, and are removed again if the tool listing then fails on a
    NEW connection."""
    from lotrlib import oauth, unlock
    from lotrlib.conn_mcp import McpConnection
    from lotrlib.registry import validate_connection
    block, tokens = res["block"], res["tokens"]
    rt, at, exp = tokens["refresh_token"], tokens["access_token"], tokens["expires_at"]
    tokens["refresh_token"] = tokens["access_token"] = None
    old = (existing or {}).get("auth") or {}
    old_refs = [r for r in (old.get("token_ref"),
                            (old.get("oauth") or {}).get("client_secret_ref")) if r]
    sealed = (unlock.enabled() and not getattr(ns, "l1", False)) or \
        (existing is not None and (old.get("token_ref") or "").startswith("sealed:"))
    prefix = "sealed:" if sealed else "store:"
    rt_name, cs_name = oauth.secret_name(cid), oauth.secret_name(cid, "client")
    rt_ref = prefix + rt_name if rt else None
    cs_ref = prefix + cs_name if res["client_secret"] else None
    block["client_secret_ref"] = cs_ref
    ep = (existing or {}).get("endpoint") or ns.url
    host = urlparse(ep).hostname
    entry = existing or {
        "id": cid, "identity": ns.identity or ("oauth @ %s" % host), "zone": reg.zone,
        "kind": "mcp", "profile": "mcp", "description": getattr(ns, "description", None) or "",
        "endpoint": ep, "transport": "http", "refresh_cmd": None, "trust": ns.trust,
        "policy": {"deny": [], "consent": [], "write": [], "read": []}, "tools": [],
        "enabled": True}
    entry["network"] = {"hosts": sorted(set([host] + list(res["hosts"])))}
    entry["auth"] = {"scheme": "oauth", "token_ref": rt_ref, "oauth": block}
    validate_connection(entry, reg.zone)           # before any secret is written
    written = []
    try:
        if rt:
            _store_secret(rt_name, oauth.wrap_secret(block, rt_ref, rt), sealed)
            written.append(rt_ref)
        if res["client_secret"]:
            _store_secret(cs_name, oauth.wrap_secret(block, cs_ref, res["client_secret"]), sealed)
            written.append(cs_ref)
        rt = None
        oauth.forget(cid)
        oauth.seed_access(entry, "-", at, exp)
        at = None
        try:
            entry["tools"] = McpConnection(entry, None, secret_resolver=_read_ref).discover()
        except GatewayError:
            if existing is None:
                raise                               # a connection that lists nothing is no use
    except BaseException:
        if existing is None:
            for r in written:
                _drop_secret(r)
        raise
    finally:
        oauth.forget(cid)
    return entry, [r for r in old_refs if r not in (rt_ref, cs_ref)]


def cmd_connect(ns):
    from lotrlib import oauth
    home = _admin_home(ns)
    _, reg = _registry(home)
    cid = _conn_id(ns, reg)
    _own_terminal_gate(f"connect {cid}")
    if cid in reg.connections:
        raise GatewayError("duplicate_connection", f"connection {cid} already exists",
                           hints=[f"re-authenticate it with: lotr login {cid}",
                                  f"or remove it with: lotr disconnect {cid}"])
    net = oauth.NetPolicy(ns.allow_private_network, ns.allow_insecure_localhost)
    res = oauth.login(ns.url, net, scope=" ".join(ns.scope or []) or None, client_id=ns.client_id,
                      client_secret=_client_secret_from(ns), cimd_url=ns.client_metadata_url,
                      authorization_server=ns.authorization_server,
                      offline_access=ns.offline_access, redirect_port=ns.redirect_port or 0,
                      send_resource=not ns.no_resource, redirect_host=ns.redirect_host,
                      force_browser=ns.open_browser,
                      say=_say, skip_audience_check=ns.skip_audience_check, confirm=_confirm(ns))
    entry, _drop = _finish_login(ns, reg, home, cid, res)
    ref = entry["auth"]["token_ref"]
    try:
        reg.add_connection(entry)
        reg.save(home / "registry.json")
    except BaseException:
        for r in (ref, entry["auth"]["oauth"].get("client_secret_ref")):
            if r:
                _drop_secret(r)                    # no orphan secret for a connection never saved
        raise
    return {"ok": True, "added": cid, "tools": len(entry["tools"]), "spec": oauth.SPEC_REVISION,
            "issuer": entry["auth"]["oauth"]["issuer"], "scopes": entry["auth"]["oauth"]["scopes"],
            "client_id_source": entry["auth"]["oauth"]["client_id_source"],
            "refresh_token": _level(ref), "pinned_hosts": entry["network"]["hosts"],
            "notes": res["notes"]}


def _oauth_conn(reg, cid):
    conn = reg.connections.get(cid)
    if conn is None:
        raise GatewayError("unknown_connection", f"no connection {cid!r}")
    if (conn.get("auth") or {}).get("scheme") != "oauth":
        raise GatewayError("not_oauth", f"{cid} is not an OAuth connection")
    return conn


def cmd_login(ns):
    from lotrlib import oauth
    home = _admin_home(ns)
    _, reg = _registry(home)
    cid = _conn_id(ns, reg)
    conn = _oauth_conn(reg, cid)
    _own_terminal_gate(f"login {cid}")
    block = conn["auth"]["oauth"]
    net = oauth.NetPolicy.from_block(block)
    pre_id = pre_secret = cimd = None
    if block["client_id_source"] == "preregistered":
        pre_id = block["client_id"]
        if block.get("client_secret_ref"):
            pre_secret = oauth.unwrap_secret(block, block["client_secret_ref"],
                                             _read_ref(block["client_secret_ref"]))
    elif block["client_id_source"] == "cimd":
        cimd = block["client_id"]
    old_rt = old_secret = None
    if conn["auth"].get("token_ref"):
        try:                                       # kept to revoke it once the new login is saved
            old_rt = oauth.unwrap_secret(block, conn["auth"]["token_ref"],
                                         _read_ref(conn["auth"]["token_ref"]))
            if block.get("client_secret_ref"):
                old_secret = oauth.unwrap_secret(block, block["client_secret_ref"],
                                                 _read_ref(block["client_secret_ref"]))
        except GatewayError:
            old_rt = None
    old_block = dict(block)
    res = oauth.login(conn["endpoint"], net, scope=" ".join(block.get("scopes") or []) or None,
                      extra_scope=" ".join(ns.scope or []).split(), client_id=pre_id,
                      client_secret=pre_secret, cimd_url=cimd,
                      authorization_server=block["issuer"], offline_access=False,
                      redirect_port=ns.redirect_port or 0, say=_say,
                      force_browser=ns.open_browser,
                      send_resource=block.get("send_resource", True),
                      redirect_host=block.get("redirect_host", "127.0.0.1"),
                      skip_audience_check=not block.get("audience_check", True),
                      confirm=_confirm(ns))
    ns.l1 = False
    ns.url = conn["endpoint"]
    ns.identity = conn.get("identity")
    ns.trust = conn.get("trust")
    res["block"]["max_access_cache_s"] = block.get("max_access_cache_s", 3600)
    entry, drop = _finish_login(ns, reg, home, cid, res, existing=dict(conn))
    ref = entry["auth"]["token_ref"]
    reg.connections[cid] = entry
    reg.save(home / "registry.json")
    for r in drop:                                  # a ref the new login no longer uses
        _drop_secret(r)
    revoked = None
    if old_rt and old_rt != "":
        revoked = oauth.revoke(old_block, old_rt, "refresh_token", old_secret)
        old_rt = old_secret = None
    notes = list(res["notes"])
    if revoked is False:
        notes.append("the previous refresh token could not be revoked at the server")
    return {"ok": True, "logged_in": cid, "tools": len(entry["tools"]),
            "scopes": entry["auth"]["oauth"]["scopes"], "refresh_token": _level(ref),
            "old_token_revoked": revoked, "notes": notes}


def cmd_disconnect(ns):
    from lotrlib import oauth
    home = _admin_home(ns)
    _, reg = _registry(home)
    cid = _conn_id(ns, reg)
    conn = _oauth_conn(reg, cid)
    _own_terminal_gate(f"disconnect {cid}")
    block = conn["auth"]["oauth"]
    ref = conn["auth"].get("token_ref")
    revoked, notes = None, []
    if ref:
        try:
            secret = oauth.unwrap_secret(block, block["client_secret_ref"],
                                         _read_ref(block["client_secret_ref"])) \
                if block.get("client_secret_ref") else None
            rt = oauth.unwrap_secret(block, ref, _read_ref(ref))
            revoked = oauth.revoke(block, rt, "refresh_token", secret)
            rt = secret = None
        except GatewayError as e:
            revoked = False
            notes.append(f"could not read the refresh token to revoke it ({e.code}); the "
                         "connection is removed locally only")
    if revoked is None and ref is None:
        notes.append("no refresh token was stored, so nothing was revoked at the server; the "
                     "access token expires on its own")
    elif revoked is None:
        notes.append("the authorization server has no revocation endpoint (RFC 7009); the "
                     "token was deleted here but not invalidated there")
    elif revoked is False and not notes:
        notes.append("the authorization server did not confirm the revocation")
    removed = []
    for r in (ref, block.get("client_secret_ref")):
        if r:
            ok = _drop_secret(r)
            removed.append({"ref": r, "deleted": ok})
            if not ok:
                notes.append(f"{r} could not be deleted; remove it by hand")
    del reg.connections[cid]
    reg.save(home / "registry.json")
    oauth.forget(cid)
    return {"ok": True, "disconnected": cid, "revoked": revoked, "secrets": removed,
            "notes": notes}


def _hub_admin_gate(what, why="hub client enrolment"):
    """gt:hub:enroll with step-up, asked by THIS process (the authority identifies it from the
    kernel). Off = nothing happens (0.2.0). Raises GatewayError on any refusal."""
    from lotrlib import unlock
    if not unlock.enabled():
        return None
    return unlock.check("gt:hub:enroll", None, request=True,
                        reason=f"lotr {what} ({why})", answer=unlock.tty_answer())


def _looks_claude(ipc, info):
    """Is this process a Claude Code `claude`? The same marks gt core's authority uses."""
    try:
        path = ipc.process_path(info["pid"]).replace("\\", "/")
        args = ipc.process_args(info["pid"])
    except Exception:                                 # noqa: BLE001
        return False
    base = os.path.basename(path).lower()
    a0 = os.path.basename(args[0]).lower() if args else ""
    names = ("claude", "claude.exe")
    if base in names or a0 in names or (info.get("comm") or "").lower() in names:
        return True
    if "/claude/versions/" in path.lower():
        return True
    return any("@anthropic-ai/claude-code" in a for a in (args or [])[:3])


def _stdin_is_tty():
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (ValueError, OSError):
        return False


def _own_terminal_gate(what):
    """A sign-in is a person's act: refuse to run `connect` / `login` / `disconnect` from inside
    Claude Code's process tree (a model must not be able to start a login it could then ride),
    and, while gt unlock is on, require the step-up the registry already uses for hub
    administration. Honest limit: a same-user process can detach from the tree, so this stops a
    model's Bash tool, not a determined local program. Without gt core installed the process
    ancestry cannot be read: then Claude Code's CLAUDECODE marker (a convenience: `env -u
    CLAUDECODE` removes it) and whether stdin is a terminal are all that can be checked, and a
    sign-in with no terminal on stdin is refused."""
    from lotrlib import unlock
    hint = ["open your own terminal (not Claude Code's) and run the same command there"]
    if os.environ.get("CLAUDECODE"):
        raise GatewayError("oauth_not_a_terminal", f"lotr {what} was started from inside Claude "
                           "Code; a sign-in must be started by you, in your own terminal", hint)
    ipc = unlock.ipc()
    if ipc is None and not _stdin_is_tty():
        raise GatewayError("oauth_not_a_terminal", f"lotr {what} needs a terminal on stdin (gt "
                           "core is not installed here, so the process tree cannot be inspected "
                           "and the environment marker alone is not trusted)", hint)
    if ipc is not None:
        # FAIL CLOSED: if the process table cannot be read (any platform, Windows included), the
        # sign-in is refused rather than guessed at.
        try:
            chain = ipc.ancestry(os.getpid())
            tree_has_claude = any(_looks_claude(ipc, i) for i in chain[1:])
            person = ipc.interactive_chain(chain)
        except Exception:                             # noqa: BLE001
            raise GatewayError("oauth_not_a_terminal", f"lotr {what}: the process tree could not "
                               "be inspected, so it is refused", hint)
        if tree_has_claude:
            raise GatewayError("oauth_not_a_terminal", f"lotr {what} was started from inside "
                               "Claude Code's process tree; run it in your own terminal", hint)
        if not person:
            raise GatewayError("oauth_not_a_terminal", f"lotr {what} needs a person's "
                               "terminal (no interactive shell above this process)", hint)
    _hub_admin_gate(what, "OAuth sign-in administration")


def cmd_enroll(ns):
    home = _admin_home(ns)
    out = Path(ns.secret_out).expanduser()
    if os.path.lexists(out):
        raise GatewayError("secret_out_exists", f"{out} already exists; refusing to overwrite")
    _, reg = _registry(home)
    _hub_admin_gate(f"enroll {ns.client}")
    secret = reg.enroll(ns.client, ns.machine, ns.allow or ["*"], ns.max_tier)
    fd = os.open(str(out), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if os.name != "nt":                   # no mode bits on Windows: the profile ACL
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
    _hub_admin_gate(f"revoke {ns.client}")
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

    c = sub.add_parser("connect")
    c.add_argument("name", help="connection id (name@zone), or a bare name in this zone")
    c.add_argument("--url", required=True, help="the https MCP endpoint")
    c.add_argument("--scope", action="append",
                   help="a scope; repeat the flag for several (default: what the server asks for)")
    c.add_argument("--client-id", help="a pre-registered OAuth client id")
    c.add_argument("--client-secret-file", help="file holding that client's secret (never argv)")
    c.add_argument("--client-secret-stdin", action="store_true")
    c.add_argument("--client-secret-prompt", action="store_true",
                   help="type the client's secret at a hidden prompt")
    c.add_argument("--open-browser", action="store_true",
                   help="open the browser even without a terminal on stdin (never under "
                        "GT_LOTR_NO_BROWSER=1)")
    c.add_argument("--no-resource", action="store_true",
                   help="do not send the RFC 8707 `resource` parameter (for a provider that "
                        "rejects it)")
    c.add_argument("--redirect-host", default="127.0.0.1", choices=("127.0.0.1", "localhost"),
                   help="host NAME in the redirect URI (the listener binds 127.0.0.1 either way)")
    c.add_argument("--client-metadata-url", help="https URL of a Client ID Metadata Document "
                                                 "you host (used when the server supports them)")
    c.add_argument("--authorization-server", help="issuer to use when the server lists several")
    c.add_argument("--redirect-port", type=int, help="fixed loopback port (default: ephemeral)")
    c.add_argument("--offline-access", action="store_true",
                   help="also ask for a refresh token via the offline_access scope")
    c.add_argument("--l1", action="store_true",
                   help="store the refresh token as a mode-600 file even while gt unlock is on")
    c.add_argument("--allow-insecure-localhost", action="store_true",
                   help="allow plain http to a loopback server you name (tests, local servers)")
    c.add_argument("--allow-private-network", action="store_true",
                   help="allow servers on private/loopback addresses (SSRF guard off for THIS "
                        "connection)")
    c.add_argument("--skip-audience-check", action="store_true",
                   help="do not compare a JWT access token's aud with the resource")
    c.add_argument("--yes", action="store_true",
                   help="do not wait for the confirmation (the summary still prints)")
    c.add_argument("--identity")
    c.add_argument("--description")
    c.add_argument("--trust", default="T1", choices=("T0", "T1", "T2"))
    c.set_defaults(fn=cmd_connect)

    lg = sub.add_parser("login")
    lg.add_argument("name")
    lg.add_argument("--scope", action="append",
                    help="an extra scope to add to those already granted; repeat for several")
    lg.add_argument("--redirect-port", type=int)
    lg.add_argument("--yes", action="store_true")
    lg.add_argument("--open-browser", action="store_true")
    lg.set_defaults(fn=cmd_login)

    dc = sub.add_parser("disconnect")
    dc.add_argument("name")
    dc.set_defaults(fn=cmd_disconnect)

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
