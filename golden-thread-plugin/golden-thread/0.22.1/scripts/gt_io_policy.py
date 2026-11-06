#!/usr/bin/env python3
"""gt_io_policy -- which paths the I/O brokers open, ask presence for, or never open (0.21.0).

    classify(target)          -> {"class": "allow" | "presence" | "never", "real", "reason",
                                  "route", "source", "mode"}
    allow_roots()             -> [(real folder, why)]   the allow list, as the broker sees it
    current_mode()            -> "open" | "standard" | "strict"   from the lockdown level
    gt_settings.py io show    what is allowed, what needs presence, what is never allowed
    gt_settings.py io allow <folder>   add one folder to the read allow list (io_read_allow)

Request 2026-10-08-io-brokers-disk-read-net-fetch-disk-write, stage 1 (disk read). The network
fetch and disk write brokers (later stages) share this module's shape: a target classifies as
`allow` (no prompt), `presence` (a fresh confirmation naming the target) or `never` (no approval
opens it), and the broker acts on the class. This file decides; it opens nothing.

NEVER. The read never-list is lockdown's own credential deny list, READ from gt_lockdown at every
call (never copied: a path lockdown learns to deny is denied here the same day), plus key files
(private keys, certificates, keystores) and every `.env` (but not `.env.example`) wherever they sit,
inside an allowed repo included, plus the unlock authority's own folder. It holds at every level.
The real path is resolved first (symlinks, `..`, `~`), so a link out of an allowed folder is
classified where it lands; on macOS and Windows the comparison ignores case, because the disk does.

ALLOW. The vault (except a locked folder), every folder in the `io_read_allow` setting, and every
code repo a vault project's source.md names. gt reads source.md read-only: a `**Repo:**` value that
is a local path, and a backticked path in a table row whose cell says `local`. Only a directory that
is a git work tree counts (a `.git` entry), so a row naming `~/`, a config file or a prod path
allows nothing.

MODE. The broker follows the lockdown level the user already chose; there is no second dial:
    insecure                      open      reads allowed broadly (and logged); never still never
    very/mostly/trust-network     standard  allow list, else presence
    (strict: presence for every read outside the vault -- see MODES; which setting selects it is
     pending the owner's decision, 2026-10-08: strict is the DEFAULT lockdown level)

NETWORK FETCH (stage 2). classify_url(url) is the same verdict for a URL: `allow` (a domain in the
`io_fetch_allow` setting, subdomains included), `presence` (any other public name) or `never`
(loopback, private, link-local, metadata addresses, `localhost`, and any name that RESOLVES to one,
unless the owner lists that address or `localhost`). Every DNS answer is checked, and the checked
addresses ride back in the verdict so the broker connects to exactly those (no second lookup for
a rebinding answer to win). Credentials in a URL, numeric host tricks and non-http(s) schemes are
refused. This file decides; the broker (gt_io_broker) connects.

Stdlib only. Python 3.8+. Reads vault-config.json and the lockdown state; writes nothing except
`add_allow`, which goes through the caller's setter so gt unlock gates it like any security key.
"""
import fnmatch
import ipaddress
import os
import re
import socket
import stat
import sys
import unicodedata
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gt_sandbox as sb  # noqa: E402

MODES = ("open", "standard", "strict")
DEFAULT_MODE = "standard"
DEFAULT_MAX_BYTES = 1024 * 1024
FOLD = sys.platform in ("darwin", "win32")          # case-insensitive disks by default

KEY_NAMES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".netrc", ".pgpass")
KEY_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".ppk", ".keystore", ".jks")
ENV_OK = (".env.example", ".env.sample", ".env.template", ".env.dist")
# gt's own secret stores under ~/.claude/golden-thread (the unlock authority's folder)
EXTRA_NEVER = ("~/.claude/golden-thread/unlock/**",)


class PolicyError(ValueError):
    """The target cannot be classified (not an absolute path). Carries a route."""


# ------------------------------------------------------------------ the level -> mode map
def mode_for_level(level):
    if level == "insecure":
        return "open"
    return DEFAULT_MODE


def current_mode(h=None):
    try:
        import gt_lockdown
        return mode_for_level(gt_lockdown.current(h))
    except Exception:                                    # noqa: BLE001 - unreadable = the default
        return DEFAULT_MODE


# ------------------------------------------------------------------ paths
def _home(h=None):
    return sb.home(h)


def _expand(p, h=None):
    p = (p or "").strip()
    if p == "~" or p.startswith("~/") or p.startswith("~\\"):
        return os.path.join(_home(h), p[2:]) if len(p) > 1 else _home(h)
    return p


def _fold(p):
    return p.lower() if FOLD else p


def real_path(target, h=None):
    """The resolved absolute path of `target` (a `~` expanded, symlinks and `..` followed even
    through components that do not exist yet). PolicyError for a relative path."""
    if not isinstance(target, str) or not target.strip() or "\0" in target:
        raise PolicyError("give an absolute path (or one starting with ~/)")
    t = _expand(target, h)
    if not os.path.isabs(t):
        raise PolicyError("%r is not an absolute path: give the full path (or one starting with "
                          "~/); the broker never guesses a working directory" % target)
    return os.path.realpath(t)


def _inside(real, root):
    r, b = _fold(real), _fold(root)
    return r == b or r.startswith(b.rstrip(os.sep) + os.sep)


# ------------------------------------------------------------------ never
def _deny_bases(h=None):
    """-> ([(base path as written, base real)], [basename patterns]) from lockdown's deny list."""
    import gt_lockdown
    bases, names = [], []
    entries = ["Read(%s)" % x for x in EXTRA_NEVER] + list(gt_lockdown.CREDENTIAL_DENY)
    for rule in entries:
        base, pat = gt_lockdown._split_rule(rule)
        if base != "Read" or not pat:
            continue
        if pat.startswith("**/"):
            names.append(pat[3:])
            continue
        p = _expand(pat, h)
        if p.endswith("/**"):
            p = p[:-3]
        elif p.endswith("**"):
            p = p[:-2].rstrip("/")
        bases.append((p, os.path.realpath(p)))
    return bases, names


def never_reason(real, h=None):
    """-> a one-sentence reason when `real` is never readable, else None."""
    bases, names = _deny_bases(h)
    for written, resolved in bases:
        if _inside(real, written) or _inside(real, resolved):
            return ("it is under %s, which gt's lockdown keeps closed to Claude (credentials)"
                    % _shorten(written, h))
    name = os.path.basename(real)
    low = name.lower()                   # never matching folds case on EVERY platform (VFAT, casefold)
    for pat in names:
        if fnmatch.fnmatchcase(low, pat.lower()):
            return "it is a %s file (secrets live there)" % pat
    if low == ".env" or low.endswith(".env") or (low.startswith(".env.") and low not in ENV_OK):
        return "it is an .env file (secrets live there)"
    if low in KEY_NAMES or low.endswith(KEY_SUFFIXES):
        return "it is a key or credential file"
    return None


def _shorten(p, h=None):
    home = _home(h)
    return "~" + p[len(home):] if _inside(p, home) and p != home else p


# ------------------------------------------------------------------ settings
def _config(h=None):
    return sb.read_config(h)


def read_allow(h=None):
    """The io_read_allow setting as written: a comma-separated string (or a JSON list)."""
    v = _config(h).get("io_read_allow")
    if isinstance(v, list):
        items = [x for x in v if isinstance(x, str)]
    elif isinstance(v, str):
        items = v.split(",")
    else:
        items = []
    return [x.strip() for x in items if x.strip()]


def max_bytes(h=None):
    v = _config(h).get("io_read_max_bytes")
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return DEFAULT_MAX_BYTES
    return n if n >= 1 else DEFAULT_MAX_BYTES


def vault_root(h=None):
    v = sb.vault_path(h)
    return os.path.realpath(v) if v else None


# ------------------------------------------------------------------ source.md
_BACKTICK = re.compile(r"`([^`\n]+)`")
_REPO_LINE = re.compile(r"^\*\*Repo:\*\*\s*(.+)$", re.M)
SOURCE_MAX = 256 * 1024
_LOCAL_ABS = re.compile(r"^(?:/|~/|[A-Za-z]:[\\/])")        # POSIX, ~/ or a Windows drive path


def _trim_slash(s):
    """Drop trailing separators, but keep a root ("/", "C:\\") as it is."""
    t = s.rstrip("/\\")
    if re.match(r"^[A-Za-z]:$", t):
        return t + s[len(t)]
    return t or "/"


def _is_repo(path):
    return os.path.isdir(path) and os.path.lexists(os.path.join(path, ".git"))


def _span_paths(text):
    """Backticked spans that look like local absolute / ~ paths."""
    for m in _BACKTICK.finditer(text):
        s = m.group(1).strip()
        if _LOCAL_ABS.match(s) and not re.search(r"[<>{}*|]", s):
            yield _trim_slash(s)


def source_repos(text, h=None):
    """The local code repos a source.md text names. Read-only; a path that is not a git work tree
    (a home folder, a config file, a prod path) is not a repo and is dropped."""
    cands = []
    m = _REPO_LINE.search(text)
    if m:
        val = m.group(1).strip()
        cands.extend(_span_paths(val))
        tok = val.split("  ")[0].strip()
        if _LOCAL_ABS.match(tok) and not re.search(r"[<>{}*|`]", tok):
            cands.append(_trim_slash(tok))
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip().lower() for c in line.strip().strip("|").split("|")]
        if any(c == "local" or c.startswith("local ") or c.startswith("local (") for c in cells):
            cands.extend(_span_paths(line))
    out = []
    for c in cands:
        p = os.path.realpath(_expand(c, h))
        if _is_repo(p) and p not in out:
            out.append(p)
    return out


def project_repos(h=None):
    """-> [(real repo, project slug)] across the vault's Projects/**/source.md."""
    v = vault_root(h)
    if not v:
        return []
    out, seen = [], set()
    proj = os.path.join(v, "Projects")
    if not os.path.isdir(proj):
        return out
    base_depth = proj.rstrip(os.sep).count(os.sep)
    for dirpath, dirs, files in os.walk(proj):
        depth = dirpath.count(os.sep) - base_depth
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in
                         ("node_modules", "spool", "memory", "Archive")) if depth < 3 else []
        if "source.md" not in files:
            continue
        slug = os.path.relpath(dirpath, proj).replace(os.sep, "/")
        try:
            with open(os.path.join(dirpath, "source.md"), "r", encoding="utf-8",
                      errors="replace") as fh:
                text = fh.read(SOURCE_MAX)
        except OSError:
            continue
        for r in source_repos(text, h):
            if r not in seen:
                seen.add(r)
                out.append((r, slug))
    return out


def allow_roots(h=None):
    """-> [(real folder, why)] : io_read_allow entries, then each project's source.md repos."""
    out, seen = [], set()
    for e in read_allow(h):
        p = _expand(e, h)
        if not os.path.isabs(p):
            continue
        r = os.path.realpath(p)
        if r not in seen:
            seen.add(r)
            out.append((r, "io_read_allow setting"))
    for r, slug in project_repos(h):
        if r not in seen:
            seen.add(r)
            out.append((r, "project %s, source.md" % slug))
    return out


# ------------------------------------------------------------------ classify
ROUTE_ALLOW_HINT = ("or add the folder to the read allow list: python3 ~/.claude/golden-thread/"
                    "hooks/gt_settings.py io allow <folder>")


def classify(target, h=None, mode=None, level=None):
    """Classify one disk target. -> a verdict dict (see the module doc). Raises PolicyError for
    a target that is not an absolute path."""
    if mode is None:
        mode = mode_for_level(level) if level else current_mode(h)
    if mode not in MODES:
        raise ValueError("mode %r: use one of %s" % (mode, ", ".join(MODES)))
    real = real_path(target, h)
    v = {"target": target, "real": real, "mode": mode, "class": None, "reason": "", "route": "",
         "source": "", "fixed": False}
    why = never_reason(real, h)
    vault = vault_root(h)
    if why is None and vault and _inside(real, vault):
        rel = os.path.relpath(real, vault).replace(os.sep, "/")
        cur = vault
        for part in ([] if rel == "." else rel.split("/")):
            cur = os.path.join(cur, part)
            if os.path.isfile(os.path.join(cur, ".gt-locked")) or part.endswith(".age"):
                why = "it is in a locked vault folder (gt_lock.py open, from a terminal, is the way)"
                break
    if why is not None:
        v.update({"class": "never", "reason": why, "fixed": True,
                  "route": "never readable by the broker, with or without approval: %s. If you "
                           "own this file, read it yourself in a terminal." % why})
        return v
    if vault and _inside(real, vault):
        v.update({"class": "allow", "source": "the vault",
                  "reason": "inside the vault"})
        return v
    for root, src in allow_roots(h):
        if _inside(real, root):
            if mode == "strict":
                v.update({"class": "presence", "source": src, "reason":
                          "strict mode asks for presence for every read outside the vault"})
                v["route"] = ("approve the confirmation naming this path (cover=folder or repo "
                              "for a window); strict mode has no allow list")
            else:
                v.update({"class": "allow", "source": src,
                          "reason": "inside an allowed folder (%s)" % src})
            return v
    if mode == "open":
        v.update({"class": "allow", "source": "mode open (lockdown insecure): reads are allowed "
                  "broadly and logged", "reason": "open mode"})
        return v
    v.update({"class": "presence", "source": "", "reason": "on no allow list",
              "route": "approve the confirmation naming this path (cover=folder for a window), "
                       + ROUTE_ALLOW_HINT})
    return v


def cover_root(real, is_dir=False):
    """The folder (or the git work tree around it) a timed grant would cover."""
    return real if is_dir else os.path.dirname(real)


def repo_root(real):
    """The nearest enclosing git work tree of `real`, else None."""
    cur = real if os.path.isdir(real) else os.path.dirname(real)
    for _ in range(40):
        if os.path.lexists(os.path.join(cur, ".git")):
            return cur
        nxt = os.path.dirname(cur)
        if nxt == cur:
            return None
        cur = nxt
    return None


def too_broad(root, h=None):
    """A grant may not cover the filesystem root, the home folder or any ancestor of it."""
    home = os.path.realpath(_home(h))
    r = os.path.realpath(root)
    return os.path.dirname(r) == r or _inside(home, r)


# ------------------------------------------------------------------ network fetch (stage 2)
DEFAULT_FETCH_MAX_BYTES = 1024 * 1024
MAX_REDIRECTS = 5
META_V4 = ("169.254.169.254", "169.254.170.2", "100.100.100.200", "192.0.0.192")
META_V6 = ("fd00:ec2::254",)
META_NAMES = ("metadata.google.internal", "metadata.goog", "metadata", "instance-data")
ALLOW_HINT = ("python3 ~/.claude/golden-thread/hooks/gt_settings.py io allow ")
NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAME_LABEL = re.compile(r"\A[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?\Z")


def fetch_allow(h=None):
    """The io_fetch_allow setting as written: a comma-separated string (or a JSON list)."""
    v = _config(h).get("io_fetch_allow")
    if isinstance(v, list):
        items = [x for x in v if isinstance(x, str)]
    elif isinstance(v, str):
        items = v.split(",")
    else:
        items = []
    return [x.strip() for x in items if x.strip()]


def fetch_max_bytes(h=None):
    v = _config(h).get("io_fetch_max_bytes")
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return DEFAULT_FETCH_MAX_BYTES
    return n if n >= 1 else DEFAULT_FETCH_MAX_BYTES


def address_kind(addr):
    """-> None for a public address, else why it is not one: "metadata", "loopback", "link-local",
    "private" or "reserved". IPv4-in-IPv6 forms (::ffff:a.b.c.d, 6to4, NAT64) are judged by the
    IPv4 address inside; anything that cannot be parsed is "reserved" (never trusted)."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return "reserved"
    if ip.version == 6:
        inner = ip.ipv4_mapped or ip.sixtofour
        if inner is None and ip in NAT64:
            inner = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if inner is not None:
            return address_kind(str(inner))
    if str(ip) in META_V4 or str(ip) in META_V6:
        return "metadata"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_private:
        return "private"
    if ip.is_multicast or ip.is_unspecified or ip.is_reserved or not ip.is_global:
        return "reserved"
    return None


def parse_url(url):
    """-> {"scheme", "host", "port", "path", "query", "url"} or PolicyError. http and https only,
    no credentials in the URL, a whole-number port, the host lower-cased without a trailing dot."""
    if not isinstance(url, str) or not url.strip():
        raise PolicyError("give a full http:// or https:// URL")
    u = url.strip()
    if re.search(r"[\s\x00-\x1f\x7f]", u):
        raise PolicyError("the URL contains a space or a control character")
    try:
        sp = urllib.parse.urlsplit(u)
        port = sp.port
    except ValueError:
        raise PolicyError("%r is not a valid URL (check the host and port)" % u[:80])
    if sp.scheme.lower() not in ("http", "https"):
        raise PolicyError("only http:// and https:// URLs are fetched (got %r)" % (sp.scheme or u[:40]))
    if "@" in sp.netloc:
        raise PolicyError("the URL carries a user name or password; net_fetch sends no "
                          "credentials, so signed-in services stay in LOTR")
    host = (sp.hostname or "").strip().lower()
    if not host:
        raise PolicyError("the URL has no host")
    if host.endswith(".") and len(host) > 1:
        host = host[:-1]
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise PolicyError("the host name %r cannot be encoded" % host[:60])
    if port is not None and port == 0:
        raise PolicyError("port 0 is not a port")
    scheme = sp.scheme.lower()
    return {"scheme": scheme, "host": host, "port": port or (443 if scheme == "https" else 80),
            "explicit_port": port is not None, "path": sp.path or "/", "query": sp.query,
            "url": urllib.parse.urlunsplit((scheme, sp.netloc.lower(), sp.path or "/", sp.query, ""))}


def _is_ip(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _numeric_trick(host):
    """127.1, 0x7f.0.0.1, 2130706433, 0177.0.0.1: forms a resolver reads as an address. A real
    host name never ends in an all-digit or 0x label."""
    return (not _is_ip(host)) and bool(re.fullmatch(r"(?:[0-9a-fx]+\.)*(?:0x[0-9a-f]*|[0-9]+)", host))


def _entries(h=None):
    """-> (names, nets) from io_fetch_allow: lower-cased name entries (a leading `*.` dropped) and
    ip_network objects for address / CIDR entries."""
    names, nets = [], []
    for e in fetch_allow(h):
        e = e.lower().strip()
        if e.startswith("*."):
            e = e[2:]
        try:
            nets.append(ipaddress.ip_network(e.strip("[]"), strict=False))
        except ValueError:
            names.append(e.rstrip("."))
    return names, nets


def _name_listed(host, names):
    return any(host == n or host.endswith("." + n) for n in names)


def _ip_listed(addr, nets):
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    inner = getattr(ip, "ipv4_mapped", None)
    return any(ip in n or (inner is not None and n.version == 4 and inner in n) for n in nets)


def default_resolver(host, port=None):
    """-> [address strings] from the system resolver. Raises socket.gaierror / OSError."""
    out = []
    for fam, _t, _p, _c, sa in socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM):
        a = sa[0].split("%", 1)[0]
        if a not in out:
            out.append(a)
    return out


def _never_url(v, reason, route):
    v.update({"class": "never", "reason": reason, "fixed": False, "route": route})
    return v


def classify_url(url, h=None, mode=None, level=None, resolver=None):
    """Classify one URL for net_fetch. -> a verdict dict {"class", "host", "port", "ips", "scheme",
    "url", "reason", "route", "source", "mode", "fixed", "listed"}. PolicyError for a URL that is
    not fetchable at all (bad scheme, credentials, no host, unresolvable, bad port). `ips` are the
    addresses the broker must connect to: every one was checked, and DNS is asked once."""
    if mode is None:
        mode = mode_for_level(level) if level else current_mode(h)
    if mode not in MODES:
        raise ValueError("mode %r: use one of %s" % (mode, ", ".join(MODES)))
    pu = parse_url(url)
    host = pu["host"]
    v = {"target": url, "mode": mode, "class": None, "reason": "", "route": "", "source": "",
         "fixed": False, "host": host, "port": pu["port"], "scheme": pu["scheme"],
         "path": pu["path"], "query": pu["query"], "url": pu["url"], "ips": [], "listed": False}
    names, nets = _entries(h)
    lift = ("the owner can list it: " + ALLOW_HINT)
    if _numeric_trick(host):
        return _never_url(v, "%s is a numeric host form that a resolver reads as an address "
                          "(%s)" % (host, "loopback and private ranges hide behind these"),
                          "write the address in plain dotted form, e.g. 127.0.0.1; it is "
                          "refused unless the owner lists it: " + ALLOW_HINT + "<address>")
    literal = _is_ip(host)
    always_private = (host == "localhost" or host.endswith(".localhost") or host in META_NAMES)
    listed = _ip_listed(host, nets) if literal else _name_listed(host, names)
    if always_private and not listed:
        return _never_url(v, "%s is this machine or a cloud metadata service, which net_fetch "
                          "never reaches" % host, "refused: it is never fetched unless the owner "
                          "lists it (" + lift.split(": ", 1)[0] + "): " + ALLOW_HINT + host)
    if literal:
        ips = [str(ipaddress.ip_address(host))]
    else:
        try:
            ips = [a for a in (resolver or default_resolver)(host, pu["port"])]
        except (OSError, UnicodeError):
            raise PolicyError("could not resolve %s: check the spelling, or that the machine is "
                              "online" % host)
        ips = list(dict.fromkeys(str(a) for a in ips))
        if not ips:
            raise PolicyError("could not resolve %s: DNS returned no address" % host)
    bad = []
    for a in ips:
        kind = address_kind(a)
        if kind is None:
            continue
        ok = _ip_listed(a, nets) or (kind == "loopback" and not literal and listed
                                     and (host == "localhost" or host.endswith(".localhost")))
        if not ok:
            bad.append((a, kind))
    v["ips"] = ips
    if bad:
        a, kind = bad[0]
        what = ("%s is a %s address" % (a, kind)) if literal else (
            "%s resolves to %s, a %s address" % (host, a, kind))
        return _never_url(v, what + " (private ranges, loopback, link-local and metadata "
                          "addresses are never fetched)",
                          "refused: it is never fetched unless the owner lists the address (a "
                          "name that resolves there is not enough): " + ALLOW_HINT + a)
    v["listed"] = listed
    if listed and mode != "strict":
        v.update({"class": "allow", "source": "io_fetch_allow setting",
                  "reason": "on the fetch allow list"})
    elif not listed and mode == "open":
        v.update({"class": "allow", "source": "mode open (lockdown insecure): fetches are "
                  "allowed broadly and logged", "reason": "open mode"})
    else:
        v.update({"class": "presence", "source": "", "reason": (
            "strict mode asks for presence for every fetch" if listed else "on no allow list"),
            "route": "approve the confirmation naming this URL (cover=domain for a window), "
                     "or add the domain to the fetch allow list: " + ALLOW_HINT + "<domain>"})
    return v


def check_fetch_entry(entry):
    """-> (normalised entry, None) or (None, refusal) for `io allow <domain|address|cidr>`."""
    e = (entry or "").strip().lower()
    if not e or re.search(r"\s", e) or "@" in e:
        return None, "give a bare domain (docs.example.com), an address or a CIDR, not %r" % entry
    if "/" in e:
        try:
            net = ipaddress.ip_network(e, strict=False)
        except ValueError:
            return None, "%r is not a domain, an address or a CIDR range" % entry
        if net.prefixlen < 8:
            return None, "%s is too broad (a range shorter than /8)" % entry
        return str(net), None
    if e.startswith("*."):
        e = e[2:]
    try:
        return str(ipaddress.ip_address(e.strip("[]"))), None
    except ValueError:
        pass
    e = e.rstrip(".")
    if e == "localhost":
        return e, None
    labels = e.split(".")
    if len(labels) < 2 or not all(_NAME_LABEL.match(x) for x in labels):
        return None, ("%r is not a domain name with at least two parts (a bare top-level name "
                      "such as com would open every site under it)" % entry)
    if _numeric_trick(e):
        return None, "%r looks like a numeric address form; write it in dotted form" % entry
    return e, None


def add_fetch_allow(entry, setter, h=None):
    """`io allow <domain>`: validate, skip a duplicate, write io_fetch_allow through the setter."""
    out = sys.stdout.write
    norm, why = check_fetch_entry(entry)
    if why:
        out("refused: %s\n" % why)
        return 1
    have = fetch_allow(h)
    if norm.lower() in [x.lower() for x in have]:
        out("%s is already on the fetch allow list\n" % norm)
        return 0
    if setter is None:
        out("refused: no way to write the setting from here; run gt_settings.py io allow\n")
        return 1
    return setter("io_fetch_allow", ",".join(have + [norm]))


# PRESETS (0.22.0, phase 88f): named bundles of exact hosts for `io allow --preset NAME`. A preset
# only adds names to io_fetch_allow; every other fetch rule (no private address, no cookie, the
# credential scan, the caps) is unchanged. Exact hosts, not their parent domain: a listed name
# covers its subdomains, so `sec.gov` itself would open every host under it.
PRESETS = {"public-data": ("www.sec.gov", "data.sec.gov", "www.federalregister.gov")}

# EDGAR's fair-access policy (sec.gov, stated from memory and to be re-checked at release): a
# descriptive User-Agent that carries contact details, and at most 10 requests a second.
CONTACT_HOSTS = ("www.sec.gov", "data.sec.gov", "sec.gov")
MIN_INTERVAL_S = {"www.sec.gov": 0.11, "data.sec.gov": 0.11, "sec.gov": 0.11}
RATE_BUCKET = {h: "sec.gov" for h in MIN_INTERVAL_S}      # the limit is per client, not per host
_CONTACT_OK = re.compile(r"^[A-Za-z0-9._%+@:/ -]{3,120}$")


def fetch_contact(h=None):
    """-> the contact text for a UA that must carry one, or None. From GT_FETCH_CONTACT, else the
    io_fetch_contact config key. Plain characters only: it goes into a request header."""
    v = os.environ.get("GT_FETCH_CONTACT") or _config(h).get("io_fetch_contact")
    v = v.strip() if isinstance(v, str) else ""
    return v if v and _CONTACT_OK.match(v) else None


def add_preset(name, setter, h=None):
    """`io allow --preset NAME`: add the preset's missing hosts in ONE write through the setter."""
    out = sys.stdout.write
    hosts = PRESETS.get((name or "").strip().lower())
    if hosts is None:
        out("refused: no preset %r (known: %s)\n" % (name, ", ".join(sorted(PRESETS))))
        return 1
    have = fetch_allow(h)
    lower = [x.lower() for x in have]
    add = [x for x in hosts if x not in lower]
    if not add:
        out("preset %s is already on the fetch allow list\n" % name)
        return 0
    if setter is None:
        out("refused: no way to write the setting from here; run gt_settings.py io allow\n")
        return 1
    rc = setter("io_fetch_allow", ",".join(have + add))
    if not rc and any(x in CONTACT_HOSTS for x in add):
        out("EDGAR asks for contact details in the User-Agent: set GT_FETCH_CONTACT (an email or "
            "a name and URL) before fetching sec.gov\n")
    return rc


# ------------------------------------------------------------------ disk write (stage 3)
DEFAULT_WRITE_MAX_BYTES = 1024 * 1024
# shell startup files and the like: matched by NAME, anywhere (a repo's own copy is no safer: a
# file that a shell runs at login is code run as the user)
SHELL_FILES = (".bashrc", ".bash_profile", ".bash_login", ".bash_logout", ".profile", ".zshrc",
               ".zshenv", ".zprofile", ".zlogin", ".zlogout", ".cshrc", ".tcshrc", ".login",
               ".logout", ".kshrc", ".mkshrc", ".inputrc", ".xprofile", ".xinitrc", ".xsession",
               "microsoft.powershell_profile.ps1", "profile.ps1", "config.fish")
# credential-bearing or command-running config that the READ never-list does not name
WRITE_CRED_FILES = (".git-credentials", ".npmrc", ".pypirc", ".gitconfig", "authorized_keys",
                    "known_hosts", "sudoers")
# files a tool runs or follows on its own once they change (direnv, git filters and attributes,
# submodule URLs): writing one is a way to run code later, so none is ever written
HIJACK_FILES = (".envrc", ".gitattributes", ".gitmodules")
LAUNCH_DIRS = ("launchagents", "launchdaemons")
# folders under the home folder that are never written (home-relative, folded, as parts)
HOME_NEVER = ((".claude",), (".ssh",), (".aws",), (".gnupg",), (".config", "gh"),
              (".config", "fish"), (".config", "systemd"), (".config", "autostart"),
              (".config", "git"), ("library", "launchagents"), ("library", "launchdaemons"))
# CI definitions: (parts) must be a prefix of the repo-relative path, folded
CI_PREFIXES = ((".github", "workflows"), (".github", "actions"), (".circleci",), (".buildkite",),
               (".azure-pipelines",), (".woodpecker",), (".gitlab", "ci"))
CI_FILES = (".gitlab-ci.yml", ".gitlab-ci.yaml", "jenkinsfile", "azure-pipelines.yml",
            "azure-pipelines.yaml", ".travis.yml", "bitbucket-pipelines.yml", ".drone.yml",
            ".woodpecker.yml", "cloudbuild.yaml", "cloudbuild.yml", ".appveyor.yml", "appveyor.yml")
CI_HINT = ("python3 ~/.claude/golden-thread/hooks/gt_settings.py set io_write_ci allow")


def write_max_bytes(h=None):
    v = _config(h).get("io_write_max_bytes")
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return DEFAULT_WRITE_MAX_BYTES
    return n if n >= 1 else DEFAULT_WRITE_MAX_BYTES


def write_ci_lifted(h=None):
    """True only when the owner set io_write_ci to the word `allow`. Nothing else lifts it."""
    v = _config(h).get("io_write_ci")
    return isinstance(v, str) and v.strip().lower() == "allow"


def _comp(name):
    """A path component as ANY file system might compare it: compatibility-normalised, invisible
    format characters dropped (HFS+ ignored them), trailing dots and spaces dropped (Windows),
    case folded EVERYWHERE (a case-sensitive disk is refused a little more than it needs: `.GIT`
    or `Jenkinsfile` spelled otherwise is still refused, which costs nothing)."""
    s = unicodedata.normalize("NFKC", name)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf")
    return s.rstrip(". ").lower()


def _parts(path):
    return [_comp(x) for x in path.replace("\\", "/").split("/") if x]


def _starts(parts, prefix):
    return list(parts[:len(prefix)]) == [_comp(x) for x in prefix]


def _write_never(real, h=None):
    """-> (kind, reason, fixed) when `real` may never be written, else None. Order matters only
    for which sentence the refusal gives; every entry holds at every level, approval or not."""
    home = _home(h)
    homes = {os.path.realpath(home), home}
    vault = vault_root(h)
    if vault and _inside(real, vault):
        return ("vault", "it is inside the vault (vault writes keep the write queue)", True)
    for hm in homes:
        if _inside(real, hm):
            rel = _parts(os.path.relpath(real, hm).replace(os.sep, "/")) if real != hm else []
            if rel and rel[0] == _comp(".claude.json") and len(rel) == 1:
                return ("claude", "it is Claude Code's own state file (~/.claude.json)", True)
            for pre in HOME_NEVER:
                if _starts(rel, pre):
                    where = "~/" + "/".join(pre)
                    k = "claude" if pre == (".claude",) else (
                        "launch" if pre[-1].startswith("launch") else "ssh" if pre == (".ssh",)
                        else "credential")
                    return (k, "it is under %s (%s)" % (where, {
                        "claude": "Claude Code's settings, hooks and Golden Thread's own files",
                        "launch": "programs started automatically at login",
                        "ssh": "keys and login settings"}.get(k, "credentials, login or "
                                                                  "start-up settings")), True)
    why = never_reason(real, h)
    if why:
        return ("credential", why, True)
    parts = _parts(real)
    base = parts[-1] if parts else ""
    if base in WRITE_CRED_FILES:
        return ("credential", "it is a credential or command-running settings file (%s)" % base,
                True)
    if base in HIJACK_FILES:
        return ("hijack", "it is a file a tool follows on its own (%s: direnv, git filters or "
                "submodule URLs run or fetch what it names)" % base, True)
    if base in SHELL_FILES:
        return ("shell", "it is a shell start-up file (%s): anything in it runs at every login"
                % base, True)
    if any(p in LAUNCH_DIRS for p in parts):
        return ("launch", "it is in a LaunchAgents / LaunchDaemons folder (programs started "
                "automatically)", True)
    if ".git" in parts:
        return ("git-internal", "it is inside .git (hooks, config and the repository's own "
                "records are never written)", True)
    return None


def _ci_kind(rel_parts):
    if not rel_parts:
        return False
    for pre in CI_PREFIXES:
        if _starts(rel_parts, pre):
            return True
    return len(rel_parts) >= 1 and rel_parts[-1] in CI_FILES and len(rel_parts) <= 6


def walk_start(path, mod=None):
    """-> (anchor, [components]) for an absolute path: the anchor is the filesystem root, or on
    Windows the DRIVE ROOT ("C:\\"), never the bare drive "C:" (which joins as drive-relative)."""
    mod = mod or os.path
    drive, rest = mod.splitdrive(path)
    parts = [x for x in rest.replace("\\", "/").split("/") if x]
    if drive:                                            # "C:" or a UNC share: its root, not itself
        return drive + "\\", parts
    return mod.sep, parts


def has_stream(path, mod=None):
    """True when a component after the drive carries ':' (an NTFS alternate data stream such as
    `file::$DATA` or `.git\\config::$DATA`)."""
    mod = mod or os.path
    _drive, rest = mod.splitdrive(path)
    return ":" in rest


def symlink_escape(target, h=None):
    """-> (link path, where it lands) for the first symbolic link on the way to `target` that
    leaves the git work tree it lives in, else None. The path is walked one component at a time
    (a `..` after a link goes where the OS goes), so a link is judged by WHERE IT LIVES: a link
    inside a repo that points into another repo, or out of the tree, is an escape; a link
    outside every work tree (a symlinked /tmp, a project folder aliased from the home folder)
    is the user's own layout and is followed."""
    t = _expand(target, h)
    if not os.path.isabs(t):
        return None
    cur, first = walk_start(t)
    stack = list(reversed(first))
    hops = 0
    while stack:
        part = stack.pop()
        if part == ".":
            continue
        if part == "..":
            cur = os.path.dirname(cur)
            continue
        nxt = os.path.join(cur, part)
        if os.path.islink(nxt):
            hops += 1
            if hops > 40:
                return nxt, "(a loop of links)"
            dest = os.readlink(nxt)
            base = dest if os.path.isabs(dest) else os.path.join(cur, dest)
            landed = os.path.realpath(base)
            tree = repo_root(cur)
            if tree is not None and not _inside(landed, os.path.realpath(tree)):
                return nxt, landed
            if os.path.isabs(dest):
                cur, dparts = walk_start(dest)
            else:
                dparts = [x for x in dest.replace("\\", "/").split("/") if x]
            stack.extend(reversed(dparts))
            continue
        cur = nxt
    return None


def classify_write(target, h=None, mode=None, level=None):
    """Classify one disk_write target -> {"class": "presence" | "never", "kind", "real", "repo",
    "rel", "repo_id", "reason", "route", "fixed", "mode", "exists"}. Presence is the only way in:
    there is no `allow` for a write. Raises PolicyError for a target that is not an absolute path.
    Resolves the real path first (symlinks, `..`, `~`), so a link is judged where it lands."""
    if mode is None:
        mode = mode_for_level(level) if level else current_mode(h)
    if os.name == "nt" and has_stream(target):
        raise PolicyError("a ':' after the drive letter is an NTFS alternate data stream; "
                          "name the plain file")
    real = real_path(target, h)
    v = {"target": target, "real": real, "mode": mode, "class": "never", "kind": "", "reason": "",
         "route": "", "source": "", "fixed": True, "repo": None, "rel": None, "repo_id": None,
         "exists": os.path.lexists(real)}

    def refuse(kind, reason, fixed, route):
        v.update({"class": "never", "kind": kind, "reason": reason, "fixed": fixed, "route": route})
        return v
    nv = _write_never(real, h)
    if nv:
        kind, why, fixed = nv
        if kind == "vault":
            return refuse(kind, why, True, "use vault_queue_write (the write queue) for vault "
                          "files; disk_write never writes the vault")
        return refuse(kind, why, fixed,
                      "never writable by the broker, with or without approval: %s. This entry "
                      "cannot be lifted; edit the file yourself in an editor or a terminal." % why)
    esc = symlink_escape(target, h)
    if esc:
        return refuse("escape", "%s is a symbolic link that leaves the work tree it sits in "
                      "(it lands on %s)" % (esc[0], esc[1]), True,
                      "name the real path inside the work tree; disk_write never follows a link "
                      "out of the folder it was pointed at")
    root = repo_root(real)
    if root is None:
        return refuse("outside-work-tree", "it is not inside a git work tree (disk_write only "
                      "writes where `git diff` shows the change and git can undo it)", False,
                      "disk_write writes only inside a git work tree. Put the file in a repo "
                      "(git init), or write it yourself in an editor or a terminal.")
    root = os.path.realpath(root)
    if too_broad(root, h):
        return refuse("home-repo", "your home folder is a git repo; disk_write only writes inside "
                      "project repos", True,
                      "your home folder is a git repo; disk_write only writes inside project "
                      "repos. Edit this file yourself in an editor or a terminal.")
    rel = os.path.relpath(real, root).replace(os.sep, "/")
    rel_parts = [] if rel == "." else _parts(rel)
    if _ci_kind(rel_parts) and not write_ci_lifted(h):
        return refuse("ci", "it is a CI workflow definition (it runs with the repository's "
                      "secrets and permissions once pushed)", False,
                      "CI files are never written by default. Only the owner can lift this one "
                      "entry, from a terminal: %s (it asks for a fresh confirmation; an agent "
                      "cannot change it). Otherwise edit the file yourself." % CI_HINT)
    if os.name == "posix":
        try:
            xm = os.lstat(real).st_mode
            if stat.S_ISREG(xm) and xm & 0o111:
                return refuse("executable", "it is an executable file (a rewrite would change "
                              "what runs)", True,
                              "it is executable; edit it yourself (disk_write never rewrites a "
                              "file that can be run)")
        except OSError:
            pass
    try:
        st = os.stat(root)
        v["repo_id"] = (st.st_dev, st.st_ino)
    except OSError:
        return refuse("outside-work-tree", "the work tree cannot be read", False,
                      "check the folder exists and is readable")
    v.update({"class": "presence", "kind": "work-tree", "repo": root, "rel": rel, "fixed": False,
              "reason": "inside the git work tree %s" % root,
              "route": "approve the confirmation naming this path and its size (cover=repo or "
                       "folder asks once for a window)"})
    return v


# ------------------------------------------------------------------ factor (for show)
def factor_state(h=None, client=None):
    """-> (state, factor). state: "off" (gt unlock is off), "none" (on, but no factor enrolled),
    "ok" (factor names the strongest one the authority can use), "error" (on, and the authority
    could not be asked: a caller must treat that as a refusal). Never starts a daemon."""
    try:
        c = client
        if c is None:
            import gt_unlock_client as c
        if not c.enabled():
            return "off", None
    except Exception:                                    # noqa: BLE001
        return "error", None
    try:
        st = c.call("status", {}, start=False, timeout=2.0)
    except Exception:                                    # noqa: BLE001
        return "error", None
    facs = (st or {}).get("factors") or {}
    for name in ("touchid", "hello", "totp", "sso"):
        f = facs.get(name) or {}
        if f.get("enrolled") and f.get("available", True):
            return "ok", name
    return "none", None


def current_factor(h=None, client=None):
    """-> (factor name, caveat line) for display: the strongest factor the unlock authority could
    use, else Claude Code's own permission prompt with its caveat."""
    state, name = factor_state(h, client)
    if state == "ok":
        return name, CAVEATS.get(name, "")
    why = {"off": " (gt unlock is off)", "none": " (no factor is enrolled)",
           "error": " (gt unlock is on but its authority is not answering)"}[state]
    return "permission_prompt", CAVEATS["permission_prompt"] + why


# ------------------------------------------------------------------ unseen writes
def _settings_layers(cwd, h=None):
    home = _home(h)
    out = [os.path.join(home, ".claude", "settings.json"),
           os.path.join(home, ".claude", "settings.local.json")]
    if cwd:
        out += [os.path.join(cwd, ".claude", "settings.json"),
                os.path.join(cwd, ".claude", "settings.local.json")]
    seen, res = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            res.append(p)
    return res


def preallowed_tool(tool, server, cwd=None, h=None):
    """-> None, or (rule, file) for the first Claude Code settings layer that lets `tool` run with
    no prompt: an allow rule naming it, its server, or a wildcard covering it, or the bypass
    permission mode. A layer that exists but cannot be read or parsed counts as pre-allowed
    (rule "(unreadable settings)"): fail closed. The files are only read."""
    import fnmatch
    try:                                                  # 0.22.0, 89e: a bypass launch has no prompts either
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import gt_bypass
        flag = gt_bypass.launch_flag()
    except Exception:                                     # noqa: BLE001 - unknown is not a claim
        flag = None
    if flag:
        return "launched with " + flag, "(process arguments)"
    for path in _settings_layers(cwd, h):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                d = __import__("json").load(fh)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return "(settings that could not be read or parsed)", path
        if not isinstance(d, dict):
            return "(settings that are not a JSON object)", path
        perms = d.get("permissions")
        if perms is None:
            continue
        if not isinstance(perms, dict):
            return "(a permissions entry that is not an object)", path
        if perms.get("defaultMode") == "bypassPermissions":
            return "defaultMode: bypassPermissions", path
        allow = perms.get("allow")
        if allow is None:
            continue
        if not isinstance(allow, list):
            return "(an allow list that is not a list)", path
        for rule in allow:
            if not isinstance(rule, str):
                return "(an allow rule that is not text)", path
            r = rule.strip().split("(", 1)[0].strip()
            if r == tool or r == server or fnmatch.fnmatchcase(tool, r):
                return rule, path
    return None


CAVEATS = {
    "touchid": "",
    "hello": "",
    "totp": "approved by a one-time code: weaker than Touch ID or Windows Hello, because anyone "
            "holding the authenticator can approve",
    "sso": "approved by single sign-on: as strong as that sign-in, not a person at this machine",
    "permission_prompt": "approved by Claude Code's permission prompt: anyone at this keyboard "
                         "could approve",
}


# ------------------------------------------------------------------ show / allow
def show(h=None, out=None):
    out = out or sys.stdout.write
    try:
        import gt_lockdown
        level = gt_lockdown.current(h)
    except Exception:                                    # noqa: BLE001
        level = "strict"
    mode = mode_for_level(level)
    name, caveat = current_factor(h)
    L = []
    L.append("Golden Thread I/O brokers -- disk read   (lockdown %s -> mode %s)" % (level, mode))
    L.append("")
    L.append("Allowed without a prompt:" if mode != "open" else
             "Allowed without a prompt (mode open: any path not listed under Never):")
    v = vault_root(h)
    L.append("  %-60s the vault" % (v or "(no vault configured)"))
    for r, why in allow_roots(h):
        L.append("  %-60s %s" % (r, why))
    L.append("")
    L.append("Needs your presence (a confirmation naming the path):")
    L.append("  everything else" if mode == "standard" else
             ("  everything outside the vault, the allow list included" if mode == "strict"
              else "  nothing (mode open logs instead)"))
    L.append("")
    L.append("Never (no approval opens these, at any level):")
    bases, names = _deny_bases(h)
    for written, _r in bases:
        L.append("  %s/**" % _shorten(written, h))
    for n in names:
        L.append("  **/%s" % n)
    L.append("  **/.env.*  (except .env.example)")
    L.append("  key files: %s, *%s" % (", ".join(KEY_NAMES), ", *".join(KEY_SUFFIXES)))
    L.append("  locked vault folders and *.age")
    L.append("")
    L.append("Golden Thread I/O brokers -- network fetch (net_fetch; GET only, no cookies or "
             "credentials)")
    L.append("")
    L.append("Allowed without a prompt:" if mode != "open" else
             "Allowed without a prompt (mode open: any public domain not listed under Never):")
    fa = fetch_allow(h)
    for e in fa:
        L.append("  %-60s io_fetch_allow (subdomains included)" % e)
    if not fa:
        L.append("  (no domain listed)")
    L.append("")
    L.append("Needs your presence (a confirmation naming the URL):")
    L.append("  any other public domain" if mode == "standard" else
             ("  every domain, the allow list included" if mode == "strict"
              else "  nothing (mode open logs instead)"))
    L.append("")
    L.append("Never (unless you list the address yourself with io allow):")
    L.append("  localhost, *.localhost, loopback (127.0.0.0/8, ::1)")
    L.append("  private ranges (10/8, 172.16/12, 192.168/16, fc00::/7), link-local (169.254/16, fe80::/10)")
    L.append("  cloud metadata (169.254.169.254, fd00:ec2::254, metadata.google.internal)")
    L.append("  any name that resolves to one of these, and every redirect hop that lands on one")
    L.append("Caps: %d bytes per fetch (io_fetch_max_bytes); text types only; at most %d redirects"
             % (fetch_max_bytes(h), MAX_REDIRECTS))
    L.append("")
    L.append("Golden Thread I/O brokers -- disk write (disk_write; text, inside a git work tree)")
    L.append("")
    L.append("Allowed without a prompt:")
    L.append("  nothing. Every write needs your presence (Touch ID / Windows Hello / a code, or")
    L.append("  Claude Code's own prompt where none is enrolled), at every lockdown level; the")
    L.append("  prompt names the path and the byte count. cover=repo|folder asks once for a window")
    L.append("  (not on the permission-prompt route). io_write_presence cannot be turned off.")
    L.append("")
    L.append("Never (no approval opens these, at any level):")
    L.append("  anything outside a git work tree; links that leave the tree they sit in")
    L.append("  ~/.claude (settings, hooks, golden-thread), ~/.claude.json")
    L.append("  shell start-up files (.zshrc, .bashrc, .profile, ...), LaunchAgents / LaunchDaemons")
    L.append("  ~/.ssh, ~/.aws, ~/.gnupg, other credential files, .env and key files")
    L.append("  .envrc, .gitattributes, .gitmodules (any folder: a tool follows them on its own)")
    L.append("  everything under .git (hooks, config, HEAD ...), the vault (use the write queue)")
    L.append("CI workflow files (.github/workflows, .gitlab-ci.yml, ...): %s" % (
        "ALLOWED by the owner (io_write_ci allow)" if write_ci_lifted(h) else
        "never; only the owner can lift this one entry: gt_settings.py set io_write_ci allow"))
    L.append("Caps: %d bytes per write (io_write_max_bytes); an overwritten file is backed up to "
             "~/.claude/golden-thread/io/backups; never executable; never committed or pushed"
             % write_max_bytes(h))
    L.append("")
    L.append("Current factor: %s" % name)
    if caveat:
        L.append("  caveat: %s" % caveat)
    L.append("Size cap per read: %d bytes (io_read_max_bytes)" % max_bytes(h))
    L.append("")
    L.append("Add a folder or a domain: python3 gt_settings.py io allow <folder|domain>")
    out("\n".join(L) + "\n")
    return 0


def check_folder(folder, h=None):
    """-> (absolute folder, None) or (None, refusal)."""
    f = _expand(folder, h)
    if not os.path.isabs(f):
        return None, "give an absolute folder (or one starting with ~/): %r" % folder
    r = os.path.realpath(f)
    if not os.path.isdir(r):
        return None, "%s is not a folder that exists" % f
    if too_broad(r, h) or os.path.realpath(_home(h)) == r:
        return None, ("%s is too broad (the filesystem root, your home folder or a parent of it); "
                      "name the project folder instead" % f)
    why = never_reason(r, h)
    if why:
        return None, "%s can never be on the allow list: %s" % (f, why)
    return f.rstrip(os.sep) or f, None


def _path_like(a):
    return a.startswith(("/", "~", ".", "\\")) or bool(re.match(r"\A[A-Za-z]:[\\/]", a))


def cli(argv, setter=None, h=None):
    """`io show` | `io allow <folder|domain>`. `setter(name, value)` is gt_settings.set_value, so the
    unlock gate and the validation apply to the write."""
    out = sys.stdout.write
    if not argv or argv[0] == "show":
        return show(h)
    if argv[0] == "allow" and len(argv) == 3 and argv[1] == "--preset":
        return add_preset(argv[2], setter, h)
    if argv[0] == "allow" and len(argv) == 2 and not _path_like(argv[1]):
        return add_fetch_allow(argv[1], setter, h)
    if argv[0] == "allow" and len(argv) == 2:
        folder, why = check_folder(argv[1], h)
        if why:
            out("refused: %s\n" % why)
            return 1
        have = read_allow(h)
        if _fold(folder) in [_fold(x) for x in have] or \
                _fold(os.path.realpath(folder)) in [_fold(r) for r, _s in allow_roots(h)]:
            out("%s is already on the read allow list\n" % folder)
            return 0
        if setter is None:
            out("refused: no way to write the setting from here; run gt_settings.py io allow\n")
            return 1
        return setter("io_read_allow", ",".join(have + [folder]))
    out("usage: gt_settings.py io [show | allow <folder|domain> | allow --preset NAME]\n")
    return 2


def main(argv=None):
    return cli(list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())
