#!/usr/bin/env python3
"""gt_mcp_inventory -- every MCP server Claude Code may load here, and which go through gt (0.20.1).

    gt_mcp_inventory.py [--json] [--cwd DIR] [--home DIR]
    gt_mcp_inventory.py managed            print the enterprise managed-settings recipe (guidance
                                           only: gt never writes managed settings)

gt unlock, LOTR's tiers and consent, and gt sandbox mode cover what passes through LOTR
(gt-lotr) and the vault server (gt-vault). An MCP server Claude Code connects to DIRECTLY is
outside all of them: it is not gated by unlock, and the sandbox does not contain it ("Claude's
file tools, MCP servers, and hooks run outside it" -- code.claude.com/docs, sandboxing). This
tool lists those servers so the doctor and `gt_unlock.py verify` can say so instead of implying
a boundary that is not there. It is read-only and writes nothing.

WHERE IT LOOKS (code.claude.com/docs: mcp, managed-mcp, plugins, settings -- 2026-10-04):
  user       ~/.claude.json  mcpServers                       (or $CLAUDE_CONFIG_DIR/.claude.json)
  local      ~/.claude.json  projects[<folder>].mcpServers    every folder; `here` marks the
                                                              ones that apply to --cwd
  project    .mcp.json in --cwd and each parent folder; approval state from ~/.claude.json and
             settings (enabledMcpjsonServers / disabledMcpjsonServers / enableAllProjectMcpServers)
  plugin     every plugin enabled in user, project or managed settings (enabledPlugins), at the
             installPath ~/.claude/plugins/installed_plugins.json records: mcpServers in
             .claude-plugin/plugin.json (inline, a file path, or a list), else .mcp.json
  claude.ai  connectors the account has connected (claudeAiMcpEverConnected)
  built-in   Claude in Chrome, when the extension or its default is recorded in ~/.claude.json
  managed    managed-mcp.json beside managed-settings.json: /Library/Application Support/
             ClaudeCode (macOS), /etc/claude-code (Linux, WSL), C:\\Program Files\\ClaudeCode
             (Windows). allowManagedMcpServersOnly / allowedMcpServers / deniedMcpServers are
             read from managed and user settings. MDM profiles and the Windows registry are not.

WHAT IT REPORTS per server: name, source, transport, a coarse endpoint (a command's base name, a
URL's scheme and host -- never its path, query or user info), state, GATED or UNGATED, and the
KIND of place its credentials live: headers-in-config, headers-helper, env-in-config,
args-in-config, url-in-config, oauth-store (a remote server Claude Code may hold a login for),
server-side (claude.ai), browser-profile (Chrome) or none. `plaintext` says a literal value sits
in a config file (not a ${VAR} reference). It NEVER prints a header, env, argument or URL value.

GATED means: named gt-vault or gt-lotr AND running gt's own server script (gt_vault_mcp.py,
lotr_mcp.py) over stdio. A server merely named like one of gt's is UNGATED, with a note.

Stdlib only, Python 3.8+. Exit 0, or 2 on a usage error.
"""
import argparse
import fnmatch
import json
import os
import re
import sys

VERSION = "0.20.3"
SCHEMA = 1

# server name -> the gt script it must run to count as gated
GATED_SCRIPTS = {"gt-vault": "gt_vault_mcp.py", "gt-lotr": "lotr_mcp.py"}

# An argument that names a credential (--token X, --api-key=X, password=...). Only the fact is
# reported; the argument itself never is.
_SECRET_ARG = re.compile(r"(?i)(token|api[-_]?key|secret|passw|bearer|auth|credential)")
_REF = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(:-[^}]*)?\}$")

SECURITY_REF = 'SECURITY.md, "MCP servers outside LOTR"'


# ------------------------------------------------------------------ paths
def _home(h=None):
    return os.path.abspath(h or os.path.expanduser("~"))


def claude_dir(h=None, environ=None):
    env = os.environ if environ is None else environ
    if env.get("CLAUDE_CONFIG_DIR") and h is None:
        return os.path.abspath(os.path.expanduser(env["CLAUDE_CONFIG_DIR"]))
    return os.path.join(_home(h), ".claude")


def claude_json_path(h=None, environ=None):
    env = os.environ if environ is None else environ
    if env.get("CLAUDE_CONFIG_DIR") and h is None:
        return os.path.join(claude_dir(h, env), ".claude.json")
    return os.path.join(_home(h), ".claude.json")


def managed_dir(platform=None):
    """The managed-settings folder for `platform` (sys.platform spelling)."""
    platform = platform or sys.platform
    if platform.startswith("win"):
        return r"C:\Program Files\ClaudeCode"
    if platform == "darwin":
        return "/Library/Application Support/ClaudeCode"
    return "/etc/claude-code"


def _join(base, name, platform=None):
    platform = platform or sys.platform
    sep = "\\" if platform.startswith("win") else "/"
    return base.rstrip("\\/") + sep + name


def managed_files(platform=None, base=None):
    """-> (managed-mcp.json path, [managed-settings.json, managed-settings.d/*.json ...])."""
    base = base or managed_dir(platform)
    settings = [_join(base, "managed-settings.json", platform)]
    d = _join(base, "managed-settings.d", platform)
    try:
        settings += sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".json"))
    except OSError:
        pass
    return _join(base, "managed-mcp.json", platform), settings


def path_key(p):
    """A folder as Claude Code's ~/.claude.json keys it, compared loosely: forward slashes, no
    trailing slash, and on a drive-letter path (Windows) case-folded. Pure string work, so a
    Windows key is matched correctly on any machine."""
    s = str(p).replace("\\", "/")
    if re.match(r"^[A-Za-z]:(/|$)", s):
        s = s.lower()
    if len(s) > 1 and not re.match(r"^[a-z]:/$", s):
        s = s.rstrip("/")
    return s


def ancestors(p):
    """`p` and every parent, as path_key()s, nearest first."""
    k = path_key(p)
    out = [k]
    while True:
        if re.match(r"^[a-z]:/?$", k) or k in ("/", ""):
            break
        parent = k.rsplit("/", 1)[0]
        if re.match(r"^[a-z]:$", parent):
            parent += "/"
        elif parent == "":
            parent = "/"
        if parent == k:
            break
        out.append(parent)
        k = parent
    return out


# ------------------------------------------------------------------ reading
class Reader:
    """JSON files read once; an unreadable or unparsable file becomes a problem (by path), never
    an exception and never its content."""

    def __init__(self):
        self.problems = []
        self._cache = {}

    def json(self, path):
        if path in self._cache:
            return self._cache[path]
        val = None
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    val = json.load(f)
            except (OSError, ValueError, UnicodeDecodeError) as e:
                self.problems.append("%s could not be read (%s)" % (path, type(e).__name__))
        self._cache[path] = val
        return val


def _dict(v):
    return v if isinstance(v, dict) else {}


def _list(v):
    return v if isinstance(v, list) else []


# ------------------------------------------------------------------ classification
def _literal(v):
    return isinstance(v, str) and v != "" and not _REF.match(v.strip())


def transport(cfg):
    t = cfg.get("type")
    if isinstance(t, str) and t:
        return t
    if cfg.get("command"):
        return "stdio"
    if cfg.get("url"):
        return "http"
    return "unknown"


def endpoint(cfg):
    """A coarse, value-free endpoint: a command's base name, or a URL's scheme://host[:port]."""
    if cfg.get("command"):
        c = str(cfg["command"]).replace("\\", "/").rsplit("/", 1)[-1]
        return c[:60]
    u = cfg.get("url")
    if isinstance(u, str):
        m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://(?:[^@/?#]*@)?([^/?#:]+)(:\d+)?", u)
        if m:
            return "%s://%s%s" % (m.group(1).lower(), m.group(2).lower(), m.group(3) or "")
        return "(unparsed url)"
    return ""


def credentials(cfg):
    """-> (kinds, plaintext). Looks at keys and at whether values are literals; returns no value."""
    kinds, plain = [], False
    headers = _dict(cfg.get("headers"))
    if headers:
        kinds.append("headers-in-config")
        plain = plain or any(_literal(v) for v in headers.values())
    if cfg.get("headersHelper"):
        kinds.append("headers-helper")
    env = _dict(cfg.get("env"))
    if env:
        kinds.append("env-in-config")
        plain = plain or any(_literal(v) for v in env.values())
    args = [a for a in _list(cfg.get("args")) if isinstance(a, str)]
    if any(_SECRET_ARG.search(a) for a in args):
        kinds.append("args-in-config")
        plain = plain or any(_literal(a) for a in args)
    u = cfg.get("url") if isinstance(cfg.get("url"), str) else ""
    if u and (re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/?#]*@", u) or "?" in u):
        kinds.append("url-in-config")
        plain = plain or _literal(u)
    t = transport(cfg)
    if (t in ("http", "sse", "ws", "streamable-http") or cfg.get("oauth")) \
            and not headers and not cfg.get("headersHelper"):
        kinds.append("oauth-store")
    return (kinds or ["none"]), plain


def gated(name, cfg):
    """-> (gated, note)."""
    script = GATED_SCRIPTS.get(name)
    if not script:
        return False, ""
    words = [str(cfg.get("command") or "")] + [str(a) for a in _list(cfg.get("args"))]
    runs = any(w.replace("\\", "/").rsplit("/", 1)[-1] == script for w in words)
    if runs and transport(cfg) == "stdio":
        return True, ""
    return False, "named like gt's %s server but does not run %s" % (name, script)


# ------------------------------------------------------------------ policy
def _policy_entries(settings_list, key):
    out, seen = [], False
    for d in settings_list:
        if key in _dict(d):
            seen = True
            out += [e for e in _list(d[key]) if isinstance(e, dict)]
    return out, seen


def _matches(entry, name, cfg):
    if "serverName" in entry and entry["serverName"] == name:
        return True
    if "serverUrl" in entry and isinstance(cfg.get("url"), str):
        return fnmatch.fnmatchcase(cfg["url"], str(entry["serverUrl"]))
    if "serverCommand" in entry and cfg.get("command"):
        cmd = [str(cfg["command"])] + [str(a) for a in _list(cfg.get("args"))]
        return entry["serverCommand"] == cmd
    return False


# ------------------------------------------------------------------ the inventory
def inventory(h=None, cwd=None, platform=None, managed_base=None, environ=None):
    """-> {"schema", "cwd", "servers": [...], "policy": {...}, "summary": {...}, "notes",
    "problems"}. Pure reading."""
    env = os.environ if environ is None else environ
    cwd = os.path.abspath(cwd or os.getcwd())
    rd = Reader()
    cj_path = claude_json_path(h, env)
    cj = _dict(rd.json(cj_path))
    cdir = claude_dir(h, env)
    here = ancestors(cwd)
    projects = _dict(cj.get("projects"))
    # the ~/.claude.json project entries that apply here: cwd and its parents
    proj_here = [(k, _dict(v)) for k, v in projects.items() if path_key(k) in here]
    proj_here.sort(key=lambda kv: here.index(path_key(kv[0])))
    disabled_here = set()
    for _k, pc in proj_here:
        disabled_here.update(x for x in _list(pc.get("disabledMcpServers")) if isinstance(x, str))

    user_settings = _dict(rd.json(os.path.join(cdir, "settings.json")))
    proj_settings = []
    for a in reversed(here):                              # farthest first, nearest wins
        for name in ("settings.json", "settings.local.json"):
            if os.sep == "/" and re.match(r"^[a-z]:", a):  # a Windows key on a POSIX host
                continue
            p = os.path.join(a, ".claude", name)
            d = rd.json(p) if os.path.normcase(os.path.abspath(p)) != os.path.normcase(
                os.path.join(cdir, "settings.json")) else None
            if isinstance(d, dict):
                proj_settings.append(d)
    mcp_path, managed_settings_paths = managed_files(platform, managed_base)
    managed_settings = [d for d in (rd.json(p) for p in managed_settings_paths)
                        if isinstance(d, dict)]
    all_settings = [user_settings] + proj_settings + managed_settings

    denied, _ = _policy_entries([user_settings] + managed_settings, "deniedMcpServers")
    allowed, allow_set = _policy_entries([user_settings] + managed_settings,
                                         "allowedMcpServers")
    managed_only = any(_dict(d).get("allowManagedMcpServersOnly") is True
                       for d in managed_settings)
    servers = []
    notes = []

    def add(name, display, source, origin, cfg, state="enabled", applies=True, **extra):
        cfg = _dict(cfg)
        g, gnote = gated(name, cfg)
        kinds, plain = credentials(cfg)
        if state == "enabled" and display in disabled_here:
            state = "disabled-here"
        if any(_matches(e, name, cfg) for e in denied):
            state = "denied-by-policy"
        elif allow_set and source not in ("built-in", "claude.ai") and \
                not any(_matches(e, name, cfg) for e in allowed):
            state = "not-allowed-by-policy"
        rec = {"name": display, "server": name, "source": source, "origin": origin,
               "transport": transport(cfg), "endpoint": endpoint(cfg),
               "credentials": kinds, "plaintext": plain, "gated": g, "state": state,
               "here": applies}
        if gnote:
            rec["note"] = gnote
        rec.update(extra)
        servers.append(rec)

    # user scope
    for n, c in _dict(cj.get("mcpServers")).items():
        add(n, n, "user", cj_path, c)
    # local scope, every folder
    for k, pc in projects.items():
        pc = _dict(pc)
        applies = path_key(k) in here
        for n, c in _dict(pc.get("mcpServers")).items():
            add(n, n, "local", "%s projects[%s]" % (cj_path, k), c,
                state="enabled" if applies else "other-folder", applies=applies)
    # project .mcp.json, cwd and parents
    enable_all = any(pc.get("enableAllProjectMcpServers") is True for _k, pc in proj_here) or \
        any(_dict(d).get("enableAllProjectMcpServers") is True for d in all_settings)
    approved, rejected = set(), set()
    for _k, pc in proj_here:
        approved.update(x for x in _list(pc.get("enabledMcpjsonServers")) if isinstance(x, str))
        rejected.update(x for x in _list(pc.get("disabledMcpjsonServers")) if isinstance(x, str))
    for d in all_settings:
        approved.update(x for x in _list(_dict(d).get("enabledMcpjsonServers"))
                        if isinstance(x, str))
        rejected.update(x for x in _list(_dict(d).get("disabledMcpjsonServers"))
                        if isinstance(x, str))
    seen_mcpjson = set()
    for a in here:
        if re.match(r"^[a-z]:", a) and os.sep == "/":
            continue
        p = os.path.join(a, ".mcp.json")
        if p in seen_mcpjson:
            continue
        seen_mcpjson.add(p)
        d = _dict(rd.json(p))
        for n, c in _dict(d.get("mcpServers")).items():
            st = "rejected" if n in rejected else (
                "enabled" if (n in approved or enable_all) else "needs-approval")
            add(n, n, "project", p, c, state=st)
    # plugins
    enabled = {}
    for d in all_settings:
        for key, on in _dict(_dict(d).get("enabledPlugins")).items():
            enabled[key] = on
    inst = _dict(rd.json(os.path.join(cdir, "plugins", "installed_plugins.json")))
    records = _dict(inst.get("plugins"))
    for key in sorted(k for k, on in enabled.items() if on is True):
        recs = records.get(key)
        recs = recs if isinstance(recs, list) else ([recs] if isinstance(recs, dict) else [])
        paths = [r.get("installPath") for r in recs if isinstance(r, dict)
                 and isinstance(r.get("installPath"), str)]
        if not paths:
            notes.append("plugin %s is enabled but has no install record" % key)
            continue
        root = paths[-1]
        pname = key.split("@", 1)[0]
        for n, c, origin in plugin_servers(root, rd):
            add(n, "plugin:%s:%s" % (pname, n), "plugin", "%s (%s)" % (key, origin), c)
    # managed-mcp.json
    for n, c in _dict(_dict(rd.json(mcp_path)).get("mcpServers")).items():
        add(n, n, "managed", mcp_path, c)
    # claude.ai connectors
    connectors_off = any(_dict(d).get("disableClaudeAiConnectors") is True
                         for d in all_settings) or \
        str(env.get("ENABLE_CLAUDEAI_MCP_SERVERS", "")).lower() in ("0", "false", "no")
    for n in _list(cj.get("claudeAiMcpEverConnected")):
        if isinstance(n, str):
            add(n, n, "claude.ai", "%s claudeAiMcpEverConnected" % cj_path,
                {"type": "claude.ai"}, state="disabled" if connectors_off else "enabled",
                credentials_override=True)
    # Claude in Chrome
    chrome = _dict(cj.get("chromeExtension"))
    if cj.get("claudeInChromeDefaultEnabled") is not None or cj.get(
            "cachedChromeExtensionInstalled") or chrome:
        add("claude-in-chrome", "claude-in-chrome", "built-in", cj_path, {"type": "built-in"},
            state="enabled" if cj.get("claudeInChromeDefaultEnabled") is True
            else "off-by-default", credentials_override=True)
    for s in servers:                                    # kinds no config can show
        if s.pop("credentials_override", False):
            s["credentials"] = ["server-side"] if s["source"] == "claude.ai" \
                else ["browser-profile"]
            s["transport"] = "remote" if s["source"] == "claude.ai" else "built-in"
    if managed_only:          # claude.ai and Chrome: not documented as covered, so not claimed
        for s in servers:
            if s["source"] not in ("managed", "built-in", "claude.ai") \
                    and s["state"] == "enabled":
                s["state"] = "not-allowed-by-policy"
    notes.append("MDM profiles (macOS) and the Windows registry policy are not read")
    outside = [s for s in servers if not s["gated"] and s["state"] != "denied-by-policy"]
    return {
        "schema": SCHEMA, "tool": "gt_mcp_inventory", "version": VERSION, "cwd": cwd,
        "servers": servers,
        "policy": {"allowManagedMcpServersOnly": managed_only,
                   "allowedMcpServers": len(allowed) if allow_set else None,
                   "deniedMcpServers": len(denied),
                   "managed_mcp_json": mcp_path if os.path.isfile(mcp_path) else None},
        "summary": {"total": len(servers),
                    "gated": sum(1 for s in servers if s["gated"]),
                    "ungated": len(outside),
                    "ungated_here": sum(1 for s in outside if s["here"]
                                        and s["state"] in ("enabled", "needs-approval")),
                    "plaintext": sum(1 for s in servers if s["plaintext"])},
        "notes": notes, "problems": rd.problems}


def plugin_servers(root, rd):
    """-> [(name, cfg, origin)] for one installed plugin. plugin.json's mcpServers may be an
    object, a path (relative to the plugin root) or a list of either; without it, .mcp.json."""
    out = []
    pj_path = os.path.join(root, ".claude-plugin", "plugin.json")
    pj = _dict(rd.json(pj_path))
    spec = pj.get("mcpServers")
    items = spec if isinstance(spec, list) else ([spec] if spec is not None else [])
    for it in items:
        if isinstance(it, dict):
            out += [(n, c, "plugin.json") for n, c in it.items()]
        elif isinstance(it, str):
            p = os.path.normpath(os.path.join(root, it.replace("${CLAUDE_PLUGIN_ROOT}", ".")))
            d = _dict(rd.json(p))
            d = _dict(d.get("mcpServers")) if "mcpServers" in d else d
            out += [(n, c, os.path.basename(p)) for n, c in d.items()]
    if spec is None:
        d = _dict(rd.json(os.path.join(root, ".mcp.json")))
        d = _dict(d.get("mcpServers")) if "mcpServers" in d else d
        out += [(n, c, ".mcp.json") for n, c in d.items()]
    return out


def ungated(inv):
    """The servers outside LOTR and gt-vault (a policy-denied server never starts)."""
    return [s for s in inv["servers"] if not s["gated"] and s["state"] != "denied-by-policy"]


def describe(s):
    """One value-free line for a server."""
    tail = [] if s["state"] == "enabled" else [s["state"]]
    if s["plaintext"]:
        tail.append("literal credential in config")
    if s.get("note"):
        tail.append(s["note"])
    return "%s (%s, %s, credentials: %s)%s" % (
        s["name"], s["source"], s["transport"], "/".join(s["credentials"]),
        (" [%s]" % "; ".join(tail)) if tail else "")


# ------------------------------------------------------------------ managed recipe
MANAGED_RECIPE = {
    "allowManagedMcpServersOnly": True,
    "allowedMcpServers": [{"serverName": "gt-vault"}, {"serverName": "gt-lotr"}],
    "deniedMcpServers": [{"serverName": "claude-in-chrome"}],
}


def managed_text(platform=None):
    base = managed_dir(platform)
    return "\n".join([
        "Enterprise recipe -- GUIDANCE ONLY. gt never writes managed settings; an administrator",
        "merges this into %s" % _join(base, "managed-settings.json", platform),
        "(macOS /Library/Application Support/ClaudeCode, Linux/WSL /etc/claude-code, Windows",
        "C:\\Program Files\\ClaudeCode). Managed servers themselves go in managed-mcp.json there.",
        "",
        json.dumps(MANAGED_RECIPE, indent=2),
        "",
        "serverName is a label, not a security control (docs): an allowlist entry also matches",
        "any server a user names gt-lotr. Prefer serverCommand / serverUrl entries, and test",
        "with `claude mcp list` after the change. A deniedMcpServers match always wins.",
        "See %s." % SECURITY_REF])


# ------------------------------------------------------------------ CLI
def _print_text(inv):
    print("MCP servers Claude Code may load (cwd: %s)" % inv["cwd"])
    if not inv["servers"]:
        print("  none found")
    for s in inv["servers"]:
        print("  %-8s %s" % ("GATED" if s["gated"] else "UNGATED", describe(s)))
    n = inv["summary"]["ungated"]
    if n:
        print("%d server(s) outside LOTR and gt-vault: not gated by gt unlock, not contained by "
              "sandbox mode (%s)." % (n, SECURITY_REF))
    for p in inv["problems"]:
        print("  could not check: %s" % p)
    for x in inv["notes"]:
        print("  note: %s" % x)


def main(argv=None):
    ap = argparse.ArgumentParser(description="List the MCP servers Claude Code may load, and "
                                             "which go through gt (read-only).")
    ap.add_argument("cmd", nargs="?", choices=("list", "managed"), default="list")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--cwd", help="the folder to evaluate project scope for (default: cwd)")
    ap.add_argument("--home", help="the home folder (default: yours)")
    ap.add_argument("--managed-dir", help=argparse.SUPPRESS)       # tests
    ap.add_argument("--platform", help=argparse.SUPPRESS)          # tests
    a = ap.parse_args(argv)
    if a.cmd == "managed":
        if a.json:
            print(json.dumps({"path": _join(managed_dir(a.platform), "managed-settings.json",
                                            a.platform), "settings": MANAGED_RECIPE}, indent=2))
        else:
            print(managed_text(a.platform))
        return 0
    inv = inventory(a.home, a.cwd, a.platform, a.managed_dir)
    if a.json:
        print(json.dumps(inv, indent=2))
    else:
        _print_text(inv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
