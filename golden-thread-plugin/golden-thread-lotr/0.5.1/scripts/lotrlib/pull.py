"""`lotr pull` (gt-lotr 0.5.0; request 2026-10-05-lotr-admin-pull-downstream-mcp-connections).

The machine already knows its MCP servers: Claude Code keeps them in ~/.claude.json (user scope,
and per project) and ~/.claude/settings.json. `pull` reads those files and says, for each server,
what it is, whether the machine already holds a token for it, whether its endpoint advertises
OAuth metadata, and the exact command that brings it into LOTR. It is READ-ONLY. The one thing
that registers anything is `lotr pull --import NAME` after a yes, and it registers only BY
REFERENCE to an owner-only token file a client already keeps: the same registration
`lotr add-mcp --auth-ref file:/abs/tokens.json#access_token` makes.

What this module will not do, and why each is a test:
  * No value from a config file or a token file is ever kept, printed, put in a command line or
    logged. A config entry is reduced to names the moment it is read: header NAMES, environment
    variable NAMES, the URL without user info, query or fragment. A token file is opened only to
    learn which top-level fields it has and that it is a private regular file; the text is dropped.
  * A command line is built only from names and URLs that carry no credential. An argument or URL
    that looks like one blocks the command instead (the server is listed, with the reason).
  * A key sitting in a config is never copied. The command is `lotr secret put NAME` (typed at a
    hidden prompt in the owner's own terminal) followed by a registration that names `store:NAME`.
  * The OAuth sign-in is not started here, and not from inside Claude Code: `connect` stays the
    owner's act in a terminal of their own.
  * The probe is an unauthenticated GET of the RFC 9728 well-known document, through the same
    bounded, https-only, address-checked fetch `connect` uses; no credential is sent.
"""
import datetime
import json
import os
import re
import shlex
import stat
import sys
import textwrap
from pathlib import Path
from urllib.parse import urlsplit

from . import conn_templates, oauth
from .errors import GatewayError
from .secretcheck import looks_like_secret

MAX_CONFIG_BYTES = 32 * 1024 * 1024        # ~/.claude.json grows with history; more than this is not one
MAX_TOKEN_FILE_BYTES = 64 * 1024
PROBE_TIMEOUT_S = 6
TOKEN_FIELDS = ("access_token", "accessToken")
TOKEN_FILE_SUFFIX = "-mcp-tokens.json"
NEEDS_AUTH_CACHE = "mcp-needs-auth-cache.json"
_HEADER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_UNPRINTABLE = re.compile(r"[^\x21-\x7e]")
STATUS_ORDER = ("ready_to_import", "needs_sign_in", "needs_key", "needs_setup", "already_connected",
                "unsupported", "blocked")
STATUS_WORDS = {"ready_to_import": "ready to import", "needs_sign_in": "needs sign-in",
                "needs_key": "needs a key", "needs_setup": "needs setup",
                "already_connected": "already in LOTR", "unsupported": "unsupported",
                "blocked": "blocked"}


# ------------------------------------------------------------------ small helpers

def _home_dir():
    return Path.home()


def display_path(path):
    """A path for people: the home directory written as ~ ."""
    s, h = str(path), str(_home_dir())
    if s == h:
        return "~"
    if s.startswith(h + os.sep):
        return "~" + s[len(h):].replace(os.sep, "/")
    return s


def command_prefix(home_arg, zone_arg):
    """The `lotr` of a generated command: it names the gateway the pull was run against when the
    owner chose one with --home or --zone, so the command lands in the same place."""
    if home_arg:
        return ["lotr", "--home", str(home_arg)]
    if zone_arg:
        return ["lotr", "--zone", str(zone_arg)]
    return ["lotr"]


def quote_arg(arg):
    """One argument of a command line, quoted for the shell the owner types it into."""
    if os.name == "nt":
        if arg and not re.search(r'[\s"&|<>^%]', arg):
            return arg
        return '"%s"' % arg.replace('"', '\\"')
    return shlex.quote(arg)


def render(argv):
    return " ".join(quote_arg(a) for a in argv)


def join_command(argv):
    """An argv as the single string `lotr add-stdio --command` splits back into the same argv
    (POSIX shell quoting; on native Windows the double-quote rules lotr._split_cmd uses)."""
    if os.name != "nt":
        return shlex.join(argv)
    # Windows: an argument with a space goes in single quotes, so the whole line can be quoted
    # again with double quotes as `--command "..."` (the printed command) and still split back the
    # same; double quotes are the fallback for an argument that holds a single quote.
    def one(a):
        if a and not re.search(r"\s", a):
            return a
        return "'%s'" % a if "'" not in a else '"%s"' % a
    return " ".join(one(a) for a in argv)


def slug(name):
    s = re.sub(r"[^a-z0-9._-]+", "-", str(name).lower()).strip("-._")[:60]
    return s if s and re.match(r"[a-z0-9]", s) else "mcp"


def _norm_name(name):
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")


def _norm_url(url):
    """A URL as a comparison key: scheme, host, port and path, the path without a trailing slash."""
    try:
        p = urlsplit(url)
        port = p.port
    except (ValueError, TypeError, AttributeError):
        return None
    if not p.scheme or not p.hostname:
        return None
    default = {"https": 443, "http": 80}.get(p.scheme.lower())
    return (p.scheme.lower(), p.hostname.lower(), None if port in (None, default) else port,
            p.path.rstrip("/"))


# ------------------------------------------------------------------ reading Claude Code's files

def default_sources():
    h = _home_dir()
    return [("user", h / ".claude.json"), ("settings", h / ".claude" / "settings.json")]


def desktop_sources():
    """(scope, path) for the desktop apps whose MCP file `setup-all` also reads (N43). The scope
    names the app, so the table says where a server came from. Same `mcpServers` shape as Claude
    Code's files; a file that is not there is reported as not there."""
    h = _home_dir()
    if os.name == "nt":
        appdata = Path(os.environ.get("APPDATA") or (h / "AppData" / "Roaming"))
        claude = appdata / "Claude" / "claude_desktop_config.json"
    elif sys.platform == "darwin":
        claude = h / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    else:
        claude = h / ".config" / "Claude" / "claude_desktop_config.json"
    return [("desktop:Claude Desktop", claude), ("desktop:Cursor", h / ".cursor" / "mcp.json")]


_PACKAGE_SPEC = re.compile(r"^@?[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*(@[A-Za-z0-9._-]+|==[A-Za-z0-9._-]+)?$")


def _secretish_arg(argv):
    """True when any argument of a command looks like a credential (`--token VALUE`,
    `--key=VALUE`, or a bare key). Judged by shape; the answer never contains the argument. A
    package name or version (`@scope/pkg@1.2.3`) is not mistaken for a random-looking key."""
    flag = ""
    for a in argv[1:]:
        name, value = flag, a
        if a.startswith("-"):
            if "=" not in a:
                flag = a.lstrip("-")
                continue
            name, value = a.lstrip("-").split("=", 1)
        flag = ""
        reason = looks_like_secret(name, value)
        if reason and not name and _PACKAGE_SPEC.match(value) and "random-looking" in reason:
            reason = None
        if reason:
            return True
    return False


def _sanitize(name, entry, scope):
    """One config entry -> only what pull may keep: no header value, env value or credential-bearing
    URL part survives this function."""
    typ = entry.get("type")
    if not isinstance(typ, str):
        typ = "http" if isinstance(entry.get("url"), str) else \
            "stdio" if "command" in entry else "unknown"
    typ = typ.lower()[:30]
    if typ == "streamable-http":
        typ = "http"
    d = {"name": oauth.clean_line(name, 100), "scope": scope, "type": typ, "problems": []}
    url = entry.get("url")
    if typ in ("http", "sse") or isinstance(url, str):
        if not isinstance(url, str) or not url:
            d["problems"].append("it names no URL")
        elif _UNPRINTABLE.search(url):
            d["problems"].append("its URL holds characters LOTR will not print")
        else:
            try:
                p = urlsplit(url)
                port = p.port
                d["scheme"], d["host"] = p.scheme.lower(), (p.hostname or "").lower()
                d["carries"] = bool(p.username or p.password or p.query or p.fragment)
                d["templated"] = "${" in url
                d["url"] = oauth.safe_url(url)
                d["port"] = port
            except ValueError:
                d["problems"].append("its URL is not valid")
    headers = entry.get("headers")
    d["header_names"], d["header_count"], d["header_kind"] = [], 0, None
    if isinstance(headers, dict):
        d["header_count"] = len(headers)
        d["header_names"] = sorted(k for k in headers if isinstance(k, str) and _HEADER_NAME.match(k))
        if len(headers) == 1 and len(d["header_names"]) == 1:
            k, v = next(iter(headers.items()))
            d["header_kind"] = "bearer" if (k.lower() == "authorization" and isinstance(v, str)
                                            and v[:7].lower() == "bearer ") else "header"
    d["command"], d["arg_secret"], d["env_names"], d["bad_env"] = None, False, [], []
    if typ == "stdio":
        cmd, args = entry.get("command"), entry.get("args", [])
        if not isinstance(cmd, str) or not cmd or "\x00" in cmd:
            d["problems"].append("it names no command")
        elif not isinstance(args, list) or not all(isinstance(a, str) and "\x00" not in a for a in args):
            d["problems"].append("its arguments are not a list of strings")
        else:
            d["command"] = [cmd] + list(args)
            d["arg_secret"] = _secretish_arg(d["command"])
        env = entry.get("env")
        if isinstance(env, dict):
            d["env_names"] = sorted(k for k in env if isinstance(k, str) and _ENV_NAME.match(k))
            d["bad_env"] = sorted(k for k in d["env_names"] if conn_templates._ENV_FORBIDDEN.match(k))
    return d


def _collect(block, scope, out):
    if not isinstance(block, dict):
        return
    for name, entry in block.items():
        if isinstance(name, str) and isinstance(entry, dict):
            out.append(_sanitize(name, entry, scope))


def read_config(path, kind):
    """-> (entries, source). `source` says whether the file was there and, if it could not be used,
    why (the reason never quotes the file)."""
    src = {"path": display_path(path), "scope": kind, "found": False, "servers": 0, "problem": None}
    out = []
    try:
        st = os.stat(str(path))
    except FileNotFoundError:
        return out, src
    except OSError as e:
        src.update(found=True, problem="cannot be read (%s)" % (e.strerror or "error"))
        return out, src
    src["found"] = True
    if st.st_size > MAX_CONFIG_BYTES:
        src["problem"] = "is too large to be a Claude Code configuration"
        return out, src
    try:
        doc = json.loads(Path(path).read_bytes().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        src["problem"] = "is not valid JSON"
        return out, src
    if not isinstance(doc, dict):
        src["problem"] = "is not a JSON object"
        return out, src
    _collect(doc.get("mcpServers"), kind, out)
    projects = doc.get("projects") if kind == "user" else None
    if isinstance(projects, dict):
        for ppath, pv in projects.items():
            if isinstance(ppath, str) and isinstance(pv, dict):
                _collect(pv.get("mcpServers"), "local:" + oauth.clean_line(ppath, 200), out)
    src["servers"] = len(out)
    return out, src


def needs_auth_names(claude_dir):
    """The server names Claude Code's own sidecar says need a sign-in. Tolerant of its shape."""
    try:
        doc = json.loads((claude_dir / NEEDS_AUTH_CACHE).read_bytes().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return set()
    if isinstance(doc, dict) and isinstance(doc.get("servers"), (dict, list)):
        doc = doc["servers"]
    if isinstance(doc, dict):
        return {k for k in doc if isinstance(k, str)}
    if isinstance(doc, list):
        return {x if isinstance(x, str) else (x.get("name") or x.get("serverName"))
                for x in doc if isinstance(x, (str, dict))
                and isinstance(x if isinstance(x, str) else (x.get("name") or x.get("serverName")), str)}
    return set()


# ------------------------------------------------------------------ token files

def inspect_token_file(path):
    """What this machine knows about a token file a client keeps -> {state, file, ref, field,
    modified, detail}. state: `file` (a private regular file with an access token field; `ref` is
    the reference to it), `file_not_private` (readable by group or others), `file_unusable`.
    The file is opened to learn its field NAMES and mode; its text is dropped at once."""
    out = {"state": "file_unusable", "file": display_path(path), "ref": None, "field": None,
           "modified": None, "detail": None}
    p = str(path)
    if "#" in p or not os.path.isabs(p):
        out["detail"] = "its path cannot be written as a reference"
        return out
    try:
        if stat.S_ISLNK(os.lstat(p).st_mode):
            out["detail"] = "is a symbolic link; a reference must point at the file itself"
            return out
        fd = os.open(p, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    except OSError as e:
        out["detail"] = "cannot be opened (%s)" % (e.strerror or "error")
        return out
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            out["detail"] = "is not a regular file"
            return out
        out["modified"] = datetime.datetime.fromtimestamp(st.st_mtime, tz=datetime.timezone.utc) \
            .strftime("%Y-%m-%dT%H:%MZ")
        if os.name != "nt":
            if hasattr(os, "getuid") and st.st_uid != os.getuid():
                out["detail"] = "is owned by another user"
                return out
            if st.st_mode & 0o077:
                out["state"] = "file_not_private"
                out["detail"] = "is readable by group or others (mode %o)" % stat.S_IMODE(st.st_mode)
                return out
        data = os.read(fd, MAX_TOKEN_FILE_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_TOKEN_FILE_BYTES:
        out["detail"] = "is too large to be a token file"
        return out
    try:
        doc = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        out["detail"] = "is not JSON"
        return out
    finally:
        data = None
    field = None
    if isinstance(doc, dict):
        field = next((f for f in TOKEN_FIELDS if isinstance(doc.get(f), str) and doc[f]), None)
    doc = None
    if field is None:
        out["detail"] = "has no access_token field"
        return out
    out.update(state="file", field=field, ref="file:%s#%s" % (p, field))
    return out


def token_files(claude_dir):
    try:
        return sorted(f for f in Path(claude_dir).iterdir()
                      if f.name.endswith(TOKEN_FILE_SUFFIX) and len(f.name) > len(TOKEN_FILE_SUFFIX))
    except OSError:
        return []


# ------------------------------------------------------------------ the OAuth metadata probe

def probe_oauth(url, *, allow_private=False, loopback_http=False):
    """Does this endpoint advertise OAuth protected-resource metadata (RFC 9728)? An unauthenticated
    GET of the well-known document, path form then root form, through oauth.fetch (https only unless
    the owner's own loopback server, address-checked, bounded, same-origin redirects)."""
    net = oauth.NetPolicy(allow_private=allow_private, allow_insecure_localhost=loopback_http)
    refused = unreachable = None
    for cand in oauth._prm_candidates(url):
        try:
            r = oauth.fetch(cand, net, follow=2, what="resource metadata URL",
                            max_bytes=oauth.MAX_DISCOVERY_BYTES, timeout=PROBE_TIMEOUT_S)
        except GatewayError as e:
            if e.code in ("oauth_ssrf_refused", "oauth_insecure_url"):
                refused = refused or e.code
            else:
                unreachable = unreachable or e.code
            continue
        if r.status == 200:
            try:
                doc = r.json()
            except GatewayError:
                continue
            servers = doc.get("authorization_servers")
            if isinstance(servers, list) and servers and all(isinstance(s, str) for s in servers):
                res = doc.get("resource")
                low = res.strip().lower() if isinstance(res, str) else ""
                return {"state": "advertised", "issuers": [oauth.safe_url(s) for s in servers[:5]],
                        "resource_scheme": "http" if low.startswith("http://") else
                        "https" if low.startswith("https://") else None, "detail": None}
        elif r.status not in (404, 410):
            unreachable = unreachable or "http_%d" % r.status
    base = {"issuers": [], "resource_scheme": None}
    if refused:
        why = ("a private or local address; pass --allow-private-network to pull if you trust this "
               "server" if refused == "oauth_ssrf_refused" else "plain http")
        return dict(base, state="refused", detail="the probe was refused: " + why)
    if unreachable:
        return dict(base, state="unreachable", detail="did not answer (%s)" % unreachable)
    return dict(base, state="not_found", detail=None)


# ------------------------------------------------------------------ templates

def _tpl_url(t):
    c = t.get("connect") or {}
    return _norm_url(c.get("url")) if isinstance(c.get("url"), str) else None


def _strip_version(tok):
    if not tok or tok[0] in "-/.~\\" or "{" in tok or re.match(r"^[A-Za-z]:[\\/]", tok):
        return tok                          # a flag, a path or a placeholder, not a package
    if "==" in tok:
        return tok.split("==", 1)[0]
    m = re.match(r"^(@[^/@]+/[^@]+|[^@]+)@(.+)$", tok)
    return m.group(1) if m else tok


def _norm_tok(tok):
    return "-y" if tok == "--yes" else _strip_version(tok)


def _program(tok):
    base = os.path.basename(tok.replace("\\", "/")).lower()
    return re.sub(r"\.(cmd|exe|bat)$", "", base)


def match_stdio_template(connect, argv):
    """Is `argv` the command a stdio template runs? -> {param: value} for its {placeholders}, or
    None. The program is compared by name (a full path to npx is npx), `--yes` is `-y`, and a
    package's version is ignored (the template pins its own)."""
    tcmd = list(connect.get("command") or [])
    if not tcmd or len(tcmd) != len(argv) or _program(tcmd[0]) != _program(argv[0]):
        return None
    params = {}
    for t, a in zip(tcmd[1:], argv[1:]):
        if "{" in t:
            rx = "".join("(?P<%s>.+)" % m.group(1) if m else re.escape(part)
                         for part, m in _split_placeholders(t))
            m = re.fullmatch(rx, a)
            if not m:
                return None
            for k, v in m.groupdict().items():
                if params.setdefault(k, v) != v:
                    return None
        elif _norm_tok(t) != _norm_tok(a):
            return None
    return params


def _split_placeholders(text):
    pos = 0
    for m in re.finditer(r"\{([a-z][a-z0-9_]{0,31})\}", text):
        if m.start() > pos:
            yield text[pos:m.start()], None
        yield "", m
        pos = m.end()
    if pos < len(text):
        yield text[pos:], None


def load_templates(home):
    """-> ({name: template}, problem|None). One broken template file must not hide the listing."""
    try:
        return conn_templates.load_templates(conn_templates.template_dirs(home), home), None
    except GatewayError as e:
        return {}, "the templates could not be read (%s), so none was matched" % e.code


def _own(home, t):
    src = str(t.get("source") or "")
    return src.startswith(str(Path(home) / "connections"))


def match_http_template(home, templates, url):
    want = _norm_url(url)
    if want is None:
        return None
    hits = [t for t in templates.values()
            if t.get("kind") in ("mcp-oauth", "mcp-http") and _tpl_url(t) == want]
    hits.sort(key=lambda t: (not _own(home, t), t["name"]))
    return hits[0] if hits else None


def match_stdio(home, templates, argv):
    hits = []
    for t in templates.values():
        if t.get("kind") == "mcp-stdio":
            p = match_stdio_template(t.get("connect") or {}, argv)
            if p is not None:
                hits.append((not _own(home, t), t["name"], t, p))
    hits.sort(key=lambda h: h[:2])
    return (hits[0][2], hits[0][3]) if hits else (None, None)


# ------------------------------------------------------------------ the scan

def _merge(entries):
    """One candidate per target. The same server configured in two places is one candidate that
    lists both; the same name pointing at two different servers is two candidates."""
    by_key, order = {}, []
    for e in entries:
        if e.get("url") and e["type"] in ("http", "sse"):
            key = (e["type"], _norm_url(e["url"]) or e["url"])
        elif e.get("command"):
            key = ("stdio", tuple(e["command"]))
        else:
            key = ("x", e["type"], e["name"], tuple(e["problems"]))
        if key in by_key:
            c = by_key[key]
            if e["scope"] not in c["scopes"]:
                c["scopes"].append(e["scope"])
            if e["name"] != c["name"] and e["name"] not in c["also_named"]:
                c["also_named"].append(e["name"])
            continue
        c = dict(e, scopes=[e["scope"]], also_named=[])
        by_key[key] = c
        order.append(c)
    return order


def _registry_index(reg):
    http, stdio = {}, {}
    for cid, c in reg.connections.items():
        if c.get("kind") != "mcp":
            continue
        if c.get("transport") == "stdio" and isinstance(c.get("command"), list):
            stdio[tuple(c["command"])] = cid
        elif isinstance(c.get("endpoint"), str):
            k = _norm_url(c["endpoint"])
            if k:
                http[k] = cid
    return http, stdio


def _identity(c):
    return "%s @ %s" % (c["name"][:40], c["host"]) if c.get("host") else "%s (local)" % c["name"][:40]


def scan(home, reg, *, extra_files=(), probe=True, allow_private=False, prefix=("lotr",), desktop=False):
    """The read-only report. Touches no file but to read it; contacts a server only for the
    unauthenticated metadata probe, and not at all with probe=False."""
    prefix = list(prefix)
    claude_dir = _home_dir() / ".claude"
    entries, sources, problems = [], [], []
    for kind, path in default_sources():
        got, src = read_config(path, kind)
        entries += got
        sources.append(src)
    for kind, path in (desktop_sources() if desktop else ()):
        got, src = read_config(path, kind)
        entries += got
        sources.append(src)
    for extra in extra_files or ():
        got, src = read_config(Path(extra).expanduser(), "file")
        entries += got
        sources.append(src)
    for s in sources:
        if s["problem"]:
            problems.append("%s %s" % (s["path"], s["problem"]))
    templates, tproblem = load_templates(home)
    if tproblem:
        problems.append(tproblem)
    reg_http, reg_stdio = _registry_index(reg)
    needs_auth = needs_auth_names(claude_dir)
    files = token_files(claude_dir)
    matched_files = set()
    taken = set(reg.connections)
    cands = []
    for e in _merge(entries):
        c = _candidate(e, home, reg, templates, reg_http, reg_stdio, needs_auth, files,
                       matched_files, taken, probe, allow_private, prefix)
        cands.append(c)
    unmatched = [f.name for f in files if f not in matched_files]
    counts = {}
    for c in cands:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    word = "server" if len(cands) == 1 else "servers"
    summary = "%d MCP %s found" % (len(cands), word) + (": " + ", ".join(
        "%s %d" % (STATUS_WORDS[s], counts[s]) for s in STATUS_ORDER if counts.get(s)) if cands else "")
    nxt = []
    ready = [c for c in cands if c["status"] == "ready_to_import"]
    import_ready = None
    if ready:
        import_ready = render(prefix + ["pull"] + sum((["--import", c["name"]] for c in ready), []))
        nxt.append("bring in the ones that are ready, by reference to the token file they already "
                   "have (it asks first): see import_ready")
    if any(c["commands"] for c in cands):
        nxt.append("every other command listed is ready to run; `connect` and `secret put` are for "
                   "your own terminal, not Claude Code's")
    nxt.append("nothing here registers until you run a command or confirm an import; connections "
               "go into zone %s (zones never mix: ask before importing a work server into a "
               "personal gateway)" % reg.zone)
    return {"ok": True, "dry_run": True, "zone": reg.zone, "sources": sources, "candidates": cands,
            "unmatched_token_files": unmatched, "problems": problems, "summary": summary,
            "import_ready": import_ready, "next": nxt}


def _candidate(e, home, reg, templates, reg_http, reg_stdio, needs_auth, files, matched_files,
               taken, probe, allow_private, prefix):
    zone = reg.zone
    base = slug(e["name"])
    cid = "%s@%s" % (base, zone)
    n = 1
    while cid in taken:
        n += 1
        cid = "%s-%d@%s" % (base, n, zone)
    c = {"name": e["name"], "id": cid, "transport": e["type"], "scopes": e["scopes"],
         "also_named": e["also_named"], "status": "blocked", "template": None, "commands": [],
         "import": None, "notes": [], "identity": None}
    notes, cmds = c["notes"], c["commands"]
    http = e["type"] == "http"
    if e["type"] in ("http", "sse"):
        c.update(url=e.get("url"), host=e.get("host"), pinned_hosts=[e["host"]] if e.get("host") else [],
                 header_names=e["header_names"])
    if http:
        c.update(token={"state": "none", "file": None, "ref": None, "needs_auth": e["name"] in needs_auth},
                 oauth_metadata={"state": "not_probed", "issuers": [], "resource_scheme": None,
                                 "detail": None})
    elif e["type"] == "stdio":
        c.update(command=e["command"] if not e["arg_secret"] else None, env_names=e["env_names"])
    c["identity"] = _identity(dict(e, host=e.get("host")))
    for p in e["problems"]:
        notes.append("this entry cannot be used: " + p)
    if e["problems"]:
        return c
    # -- already in LOTR
    existing = None
    if e["type"] == "http" and e.get("url") and _norm_url(e["url"]) in reg_http:
        existing = reg_http[_norm_url(e["url"])]
    elif e["type"] == "stdio" and e.get("command") and tuple(e["command"]) in reg_stdio:
        existing = reg_stdio[tuple(e["command"])]
    if existing:
        c.update(id=existing, status="already_connected")
        notes.append("already registered as %s; see it with: %s" % (existing, render(prefix + ["status"])))
        return c
    taken.add(cid)
    # -- transports LOTR cannot take
    if e["type"] == "sse":
        c["status"] = "unsupported"
        notes.append("LOTR does not speak the legacy sse transport. If the server also offers "
                     "streamable http, use that URL; or put a bridge in front of it: "
                     "lotr template new NAME --archetype sse-bridge")
        return c
    if e["type"] not in ("http", "stdio"):
        c["status"] = "unsupported"
        notes.append("the transport %r is not one LOTR speaks (http and stdio are)" % oauth.clean(e["type"], 30))
        return c
    if e["type"] == "http":
        return _http(c, e, home, templates, files, matched_files, probe, allow_private, prefix)
    return _stdio(c, e, home, templates, prefix)


def _http(c, e, home, templates, files, matched_files, probe, allow_private, prefix):
    notes, cmds = c["notes"], c["commands"]
    loop = e["scheme"] == "http" and e["host"] in oauth.LOOPBACK_HOSTS
    if e["scheme"] != "https" and not loop:
        c["status"] = "unsupported"
        notes.append("LOTR talks https only (or plain http to this machine); this URL is %s"
                     % oauth.clean(e["scheme"] or "not a web address", 20))
        return c
    if e["carries"] or e["templated"]:
        c["status"] = "blocked"
        notes.append("the URL carries %s, which can hold a credential; LOTR does not copy or print "
                     "it. Put the credential in the store (lotr secret put NAME) and register the "
                     "server yourself with lotr add-mcp" %
                     ("a ${VARIABLE}" if e["templated"] and not e["carries"] else
                      "user info, a query or a fragment"))
        return c
    if e["header_count"] > 1 or (e["header_count"] == 1 and not e["header_names"]):
        c["status"] = "blocked"
        notes.append("its configuration sends %d headers; LOTR sends one credential header. Register "
                     "it yourself with lotr add-mcp --auth-header NAME --auth-ref store:KEY"
                     % e["header_count"])
        return c
    # -- the token this machine holds for it
    wanted = _norm_name(e["name"])
    hits = [f for f in files if _norm_name(f.name[:-len(TOKEN_FILE_SUFFIX)]) == wanted]
    matched_files.update(hits)
    if len(hits) > 1:
        # Two files reduce to this server's name: never pick one (2026-10-15 review finding).
        c["token"].update(state="file_ambiguous", file=None, ref=None)
        notes.append("%d token files match this server's name (%s); LOTR will not guess which is "
                     "its token. Remove or rename the ones that are not, then run pull again"
                     % (len(hits), ", ".join(sorted(f.name for f in hits))))
    elif hits:
        info = inspect_token_file(hits[0])
        c["token"].update(state=info["state"], file=info["file"], ref=info["ref"],
                          modified=info["modified"])
        if info["detail"]:
            c["token"]["detail"] = info["detail"]
    if probe:
        c["oauth_metadata"] = probe_oauth(e["url"], allow_private=allow_private, loopback_http=loop)
    md = c["oauth_metadata"]
    if md["resource_scheme"] == "http":
        notes.append("the server names itself with an http:// resource, so lotr connect will stop at "
                     "oauth_insecure_url unless you vouch for the host: add --allow-insecure-metadata "
                     "%s, and only if you trust it on this network (it relaxes one document; the "
                     "token and authorize endpoints must still be https)" % e["host"])
    tpl = match_http_template(home, templates, e["url"])
    if tpl:
        c["template"] = tpl["name"]
        notes.append("matches the %s template (%s)" % (tpl["name"], tpl.get("summary", "")[:80]))
    tok = c["token"]
    if tok["state"] == "file":
        c["status"] = "ready_to_import"
        c["import"] = render(prefix + ["pull", "--import", c["name"]])
        cmds.append(render(prefix + ["add-mcp", c["id"], "--endpoint", e["url"], "--identity",
                                     c["identity"], "--auth-ref", tok["ref"]]))
        notes.append("a client keeps a token for it at %s (private to you; matched to this server by "
                     "name alone, so check it is this server's). Registering stores only the "
                     "reference; LOTR reads the file each time it calls and never copies the token. "
                     "LOTR does not renew it: when it expires LOTR answers auth_failed; add "
                     "--refresh-cmd '<the helper your MCP client renews it with>' if there is one"
                     % tok["file"])
        return c
    if tok["state"] == "file_not_private":
        notes.append("a token file exists (%s) but %s; LOTR will not point at it. Fix: chmod 600 %s"
                     % (tok["file"], tok.get("detail", "is not private"), tok["file"]))
    elif tok["state"] == "file_unusable":
        notes.append("a token file exists (%s) but %s" % (tok["file"], tok.get("detail", "cannot be used")))
    if tok["needs_auth"]:
        notes.append("Claude Code's own record says this server needs a sign-in")
    loop_flags = ["--allow-insecure-localhost"] if loop else []
    if tpl:
        c["status"] = "needs_key" if tpl["kind"] == "mcp-http" else "needs_sign_in"
        cmds.append(render(prefix + ["setup", tpl["name"], "--id", c["id"]]))
        notes.append("setup checks the template, asks for what it needs, adds the connection and tests it"
                     + ("; the sign-in is yours, in your own terminal" if tpl["kind"] == "mcp-oauth" else ""))
        return c
    if e["header_count"] == 1:
        key = "%s-key" % slug(e["name"])
        c["status"] = "needs_key"
        cmds.append(render(prefix + ["secret", "put", key]))
        add = prefix + ["add-mcp", c["id"], "--endpoint", e["url"], "--identity", c["identity"],
                        "--auth-ref", "store:" + key]
        if e["header_kind"] != "bearer":
            add += ["--auth-header", e["header_names"][0]]
        cmds.append(render(add))
        notes.append("its configuration sends a key in the %s header. The key is not copied: "
                     "`secret put` takes it at a hidden prompt, in your own terminal" % e["header_names"][0])
        return c
    connect = prefix + ["connect", c["id"], "--url", e["url"], "--identity", c["identity"]] + loop_flags
    if allow_private:
        connect.append("--allow-private-network")
    add_open = prefix + ["add-mcp", c["id"], "--endpoint", e["url"], "--identity", c["identity"]]
    c["status"] = "needs_sign_in"
    if md["state"] == "not_found":
        cmds.append(render(add_open))
        cmds.append(render(connect))
        notes.append("no OAuth metadata at the usual address, so the first command registers it with no "
                     "credential; if the server does ask for a sign-in, use the connect command instead "
                     "(a sign-in is yours, in your own terminal)")
    else:
        cmds.append(render(connect))
        notes.append("the sign-in opens a browser: run it in your own terminal, not inside Claude Code")
    return c


def _stdio(c, e, home, templates, prefix):
    notes, cmds = c["notes"], c["commands"]
    if e["arg_secret"]:
        c["status"] = "blocked"
        notes.append("an argument of its command looks like a credential (not shown). Move it into an "
                     "environment variable the server reads, then register with lotr add-stdio and "
                     "--env NAME=store:KEY, or use a template")
        return c
    if e["bad_env"]:
        c["status"] = "blocked"
        notes.append("its configuration sets %s, which changes what runs; LOTR does not pass those on"
                     % ", ".join(e["bad_env"]))
        return c
    argv = e["command"]
    c["status"] = "needs_setup"
    tpl, params = match_stdio(home, templates, argv)
    if tpl and not any(looks_like_secret(k, v) for k, v in (params or {}).items()):
        c["template"] = tpl["name"]
        cmd = prefix + ["setup", tpl["name"], "--id", c["id"]]
        for k in sorted(params):
            cmd += ["--param", "%s=%s" % (k, params[k])]
        cmds.append(render(cmd))
        notes.append("matches the %s template; setup prints the command it will run and waits for your "
                     "yes" % tpl["name"])
        if e["env_names"]:
            notes.append("your configuration passes %s to it; setup asks for what the template needs, "
                         "as references" % ", ".join(e["env_names"]))
        return c
    refs = []
    for name in e["env_names"]:
        key = "%s-%s" % (slug(e["name"]), name.lower().replace("_", "-"))
        refs.append((name, key[:64].strip("-._")))
        cmds.append(render(prefix + ["secret", "put", refs[-1][1]]))
    add = prefix + ["add-stdio", c["id"], "--command", join_command(argv), "--identity", c["identity"]]
    for name, key in refs:
        add += ["--env", "%s=store:%s" % (name, key)]
    cmds.append(render(add))
    notes.append("this runs a program on this machine, started by LOTR when the connection is used")
    if refs:
        notes.append("its environment is not copied: each variable becomes a reference, and `secret put` "
                     "takes the value at a hidden prompt in your own terminal (a variable that is not "
                     "a secret can be a plain --env-plain NAME=VALUE instead)")
    return c


# ------------------------------------------------------------------ import

def select(report, names):
    """The candidates named for import -> a list, or a GatewayError saying why not. Only names the
    owner typed: there is no 'all'."""
    chosen = []
    cands = report["candidates"]
    for raw in names:
        n = str(raw)
        hits = [c for c in cands if n == c["name"] or n == c["id"] or n in c["also_named"]]
        if not hits:
            raise GatewayError("pull_unknown_candidate",
                               "no MCP server named %s in the configuration pull read" % oauth.clean_line(repr(n), 80),
                               hints=["see them: lotr pull", "names: " + ", ".join(c["name"] for c in cands[:20])])
        if len(hits) > 1:
            raise GatewayError("pull_ambiguous", "%s names more than one server" % oauth.clean_line(repr(n), 80),
                               hints=["name one by its id: " + ", ".join(c["id"] for c in hits)])
        c = hits[0]
        if c["status"] != "ready_to_import":
            tok = c.get("token") or {}
            why = {"already_connected": "it is already registered"}.get(
                c["status"], "no private token file that a client keeps was found for it")
            hints = []
            if tok.get("state") == "file_not_private":
                why = "its token file is readable by others"
                hints.append("chmod 600 %s" % tok.get("file"))
            raise GatewayError("pull_not_importable", "%s cannot be imported by reference: %s" % (c["id"], why),
                               hints=hints + list(c["commands"]) + c["notes"][:2])
        if c not in chosen:
            chosen.append(c)
    return chosen


def plan_of(c):
    return {"name": c["name"], "id": c["id"], "endpoint": c["url"], "ref": c["token"]["ref"],
            "identity": c["identity"]}


# ------------------------------------------------------------------ the readable view

def _cell(text, width):
    text = str(text)
    return (text if len(text) <= width else text[:width - 1] + "+").ljust(width)


def _wrapped(prefix, text, width=76):
    return textwrap.fill(text, width=width, initial_indent=prefix, subsequent_indent=" " * len(prefix),
                         break_long_words=False, break_on_hyphens=False)


def render_table(report):
    out = ["Claude Code MCP servers on this machine (gateway zone %s)" % report["zone"]]
    out.append(_wrapped("looked in: ", ", ".join("%s (%s)" % (s["path"], "found" if s["found"] else "not there")
                                                for s in report["sources"])))
    cands = report["candidates"]
    if not cands:
        out += ["", "none found: nothing is configured in the files above."]
    else:
        out += ["", "  %s %s %s %s %s" % (_cell("server", 16), _cell("kind", 6), _cell("token", 12),
                                           _cell("oauth", 12), "status")]
        for c in cands:
            tok = c.get("token") or {}
            tcell = {"file": "file", "file_not_private": "not private", "file_unusable": "unusable",
                     "none": "none"}.get(tok.get("state"), "-")
            if tok.get("needs_auth") and tok.get("state") == "none":
                tcell = "needs sign-in"
            ocell = (c.get("oauth_metadata") or {}).get("state", "-").replace("_", " ") \
                if c["transport"] in ("http", "sse") else "-"
            out.append("  %s %s %s %s %s" % (_cell(c["name"], 16), _cell(c["transport"], 6), _cell(tcell, 12),
                                             _cell(ocell, 12), STATUS_WORDS[c["status"]]))
        for c in cands:
            if not (c["commands"] or c["notes"]):
                continue
            where = c.get("url") or " ".join(c.get("command") or [])
            out += ["", _wrapped("%s -> %s  " % (c["name"], c["id"]), oauth.clean_line(where, 300))]
            for n in c["notes"]:
                out.append(_wrapped("  note: ", n))
            for cmd in c["commands"]:
                out.append("    " + cmd)
    for p in report["problems"]:
        out.append(_wrapped("problem: ", p))
    if report["unmatched_token_files"]:
        out.append(_wrapped("token files with no server of that name: ", ", ".join(report["unmatched_token_files"])))
    out += ["", _wrapped("", "Nothing was changed. " + report["summary"] + ".")]
    for n in report["next"]:
        out.append(_wrapped("- ", n.replace(": see import_ready", ":")))
        if report.get("import_ready") and "import_ready" in n:
            out.append("    " + report["import_ready"])
    return "\n".join(out) + "\n"
