"""The template catalog: templates released on their own schedule (0.5.0).

gt releases are slow and a service's MCP endpoint is not. Templates for named services therefore
live in the repository's top-level `lotr-templates/` folder and are fetched with `lotr template
update`; since gt 0.21.4 a hash-checked snapshot of the same folder is also included in the module
(templates/catalog/, see conn_templates.BUNDLED_CATALOG), so they work with no network, and a fetched
catalog replaces it only when its catalog_version is higher:

    lotr-templates/catalog.json            the list: catalog version, per-template sha256, minimum lotr
    lotr-templates/templates/NAME.json     one template per service
    lotr-templates/facts/NAME.facts.json   its fact sheet (sources and verification state)
    lotr-templates/CHANGELOG.md            what changed in each catalog version

TRUST MODEL, honestly. `update` fetches over HTTPS from the public repository (or a URL the user
names), and for each template verifies the sha256 the catalog lists, parses it, runs the same
checks as `lotr template check` (no secret, no TODO, hosts pinned, env names safe) and requires a
valid fact sheet. What that buys: a truncated or damaged download, a template edited without its
catalog entry, and any template that could carry a credential are rejected. What it does NOT buy:
the hashes come from the same place as the files, so someone who can change the repository can
change both. Since 0.5.1 (phase 112) catalog.json can carry an ssh-keygen signature
(catalog.json.sig, made with the owner's dedicated ed25519 key). Once the public half is pinned in
the module (templates/catalog/catalog-signing.pub) every fetched or bundled catalog must verify
against it before any template file is used; with no key pinned the behaviour is the hash-pinned
one above and `lotr catalog check` and `lotr status` say "unsigned: no signing key is pinned". A template can name a program to run (stdio) or a host to talk
to, and that is why nothing here ever creates a connection: `lotr setup` shows the exact command
and waits for a yes, and the user can pin `--ref` to a commit they have read. TLS (the system trust
store) is the only authentication of the source.

An older catalog_version than the installed one is refused (a replayed, outdated catalog) unless
`--force`. A template that disappears from the catalog is left installed and reported.
"""
import hashlib
import hmac
import json
import re
from pathlib import Path

from . import conn_templates as ct
from . import facts as facts_mod
from . import keyring
from . import oauth
from .errors import GatewayError
from .util import atomic_write, now_iso

# The repository the catalog is fetched from, as "account/repo". EMPTY in the source tree on purpose (no account name is
# committed); the PUBLISH step stamps it here, in this one line. Empty means no default source: `lotr template update` then
# needs --base (a URL you name), and the bundled catalog keeps working.
REPO = "shaven/golden-thread"
DEFAULT_BASE = ("https://raw.githubusercontent.com/" + REPO + "/{ref}/lotr-templates") if REPO else ""
MAX_BYTES = 512 * 1024
SCHEMA = 1
_FILE = re.compile(r"^templates/([a-z][a-z0-9_.-]*)\.json$")
_FACTS = re.compile(r"^facts/([a-z][a-z0-9_.-]*)\.facts\.json$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_VER = re.compile(r"^\d+\.\d+\.\d+$")
RESERVED = set(ct.ARCHETYPES) | {"example"}

# Phase 112: the catalog signature. One namespace and one identity, so a signature made for another
# purpose with the same key (a release tag, git's own "git" namespace) never verifies here.
SIG_NAME = "catalog.json.sig"
SIG_NAMESPACE = "gt-lotr-catalog"
UNSIGNED_TEXT = "unsigned: no signing key is pinned"
PIN_FILE = ct.BUNDLED_CATALOG / "catalog-signing.pub"
# The pin file holds one or more keys (and `#` comments); keyring.py reads them, adds the keys a
# verified rollover brings and drops the revoked. Accepted key types are data there.
KEY_TYPES = keyring.KEY_TYPES


def lotr_version():
    """The running module's version, from its module.json (the one place it is written)."""
    try:
        m = json.loads((Path(__file__).resolve().parents[2] / "module.json").read_text("utf-8"))
        return m["version"]
    except (OSError, ValueError, KeyError):
        return "0.0.0"


def vtuple(v):
    return tuple(int(x) for x in v.split("."))


def base_url(base=None, ref="main"):
    if base:
        return base.rstrip("/")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$", ref or "") or ".." in ref:
        raise GatewayError("bad_ref", "--ref must be a branch, tag or commit name")
    if not DEFAULT_BASE:
        raise GatewayError("no_template_source", "no template source is set in this build: pass --base <URL> "
                           "(the folder that holds catalog.json); the bundled catalog is used until then")
    return DEFAULT_BASE.format(ref=ref)


def state_path(home):
    return Path(home) / "catalog" / "state.json"


def read_state(home):
    try:
        return json.loads(state_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _get(url, net, what):
    try:
        r = oauth.fetch(url, net, headers={"Accept": "application/json, text/plain"},
                        max_bytes=MAX_BYTES, timeout=20, what=what)
    except GatewayError as e:
        raise GatewayError("catalog_unreachable", f"cannot fetch {what}: {e.message}",
                           hints=["check the network, or pass --url to a mirror you trust"])
    if r.status != 200:
        raise GatewayError("catalog_unreachable", f"{what} answered HTTP {r.status}",
                           hints=["the catalog may not be published at this ref yet"])
    return r.body


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def pinned_key():
    """-> the pinned public key's text, or None when no key is pinned (the file does not exist)."""
    try:
        return PIN_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return ""                                   # present but unreadable: enforced, fails


def verify_signature(data, sig, key):
    """Raise catalog_bad_signature unless `sig` (ssh-keygen -Y sign output) is a valid signature of
    `data` by ANY of the trusted ed25519 keys: `key` is a pin file's text (one key, or several lines,
    `#` comments allowed) or a list of keys. Needs ssh-keygen; without it nothing can be verified,
    which is a refusal, not a pass."""
    keys = list(key) if isinstance(key, (list, tuple)) else keyring.parse_keys(key)
    keyring.verify(data, sig, keys, SIG_NAMESPACE, "catalog")


def check_signature(data, sig, home=None):
    """-> {"state", "enforced", "text"}. Refuses (catalog_unsigned / catalog_bad_signature) when a
    key is pinned and `sig` is missing or does not verify against any trusted key (the shipped pin
    plus the home's verified rollover chain, minus revoked keys; keyring.py); with no key pinned,
    reports unsigned. `home` defaults to the LOTR home."""
    key = pinned_key()
    if key is None:
        return {"state": "unsigned_no_key", "enforced": False, "text": UNSIGNED_TEXT}
    keys = keyring.trusted_keys(key, home)           # also fails closed on a damaged catalog-keys.json
    if not sig:
        raise GatewayError("catalog_unsigned", "the catalog carries no signature, and a signing key "
                           "is pinned, so it is refused",
                           hints=["the owner signs it with: dev/lotr_catalog.py sign"])
    verify_signature(data, sig, keys)
    return {"state": "verified", "enforced": True, "text": "signed: verified against the pinned key"}


def bundled_state(home=None):
    """The bundled catalog's signature state; raises like check_signature. A bundle with no
    catalog.json has nothing to check."""
    try:
        data = (ct.BUNDLED_CATALOG / "catalog.json").read_bytes()
    except OSError:
        return {"state": "no_bundle", "enforced": False, "text": "no catalog is bundled"}
    try:
        sig = (ct.BUNDLED_CATALOG / SIG_NAME).read_bytes()
    except OSError:
        sig = None
    return check_signature(data, sig, home)


def bundled_refusal(home=None):
    """-> the GatewayError that keeps the bundled templates out of use, or None."""
    try:
        bundled_state(home)
    except GatewayError as e:
        return e
    return None


def apply_rollover(home, msg_file, sig_file):
    """`lotr catalog rollover apply FILE.json FILE.sig` (no network): see keyring.py."""
    try:
        data, sig = Path(msg_file).read_bytes(), Path(sig_file).read_bytes()
    except OSError as e:
        raise GatewayError("rollover_invalid", f"cannot read {e.filename}: {e.strerror}")
    return keyring.apply_rollover(home, data, sig, pinned_key())


def signature_text(home):
    """One plain line for `lotr status`: the bundled catalog's state, then the fetched one's."""
    try:
        text = bundled_state(home)["text"]
    except GatewayError as e:
        text = f"{e.code}: {e.message}"
    fetched = read_state(home).get("signature")
    return text + (f"; last fetched catalog: {fetched}" if fetched else "")


def validate_catalog(cat):
    """Raise catalog_invalid with a plain sentence; return the cleaned entry list."""
    def bad(msg):
        raise GatewayError("catalog_invalid", msg)
    if not isinstance(cat, dict) or cat.get("schema") != SCHEMA:
        bad(f"the catalog is not schema {SCHEMA}")
    cv = cat.get("catalog_version")
    if isinstance(cv, bool) or not isinstance(cv, int) or cv < 1:
        bad("catalog_version must be a whole number from 1")
    if not isinstance(cat.get("min_lotr", "0.0.0"), str) or not _VER.match(cat.get("min_lotr", "0.0.0")):
        bad("min_lotr must look like 0.5.0")
    ents = cat.get("templates")
    if not isinstance(ents, list):
        bad("templates must be a list")
    names = set()
    for e in ents:
        if not isinstance(e, dict):
            bad("each template entry must be an object")
        m = _FILE.match(e.get("file") or "")
        if not m or m.group(1) != e.get("name"):
            bad(f"entry {str(e.get('name'))[:40]!r}: file must be templates/<name>.json")
        if e["name"] in names:
            bad(f"template {e['name']} is listed twice")
        names.add(e["name"])
        if not _SHA.match(e.get("sha256") or ""):
            bad(f"entry {e['name']}: sha256 must be 64 lowercase hex characters")
        fm = _FACTS.match(e.get("facts") or "")
        if not fm or fm.group(1) != e["name"] or not _SHA.match(e.get("facts_sha256") or ""):
            bad(f"entry {e['name']}: facts must be facts/<name>.facts.json with its facts_sha256")
        if e.get("min_lotr") is not None and not _VER.match(str(e["min_lotr"])):
            bad(f"entry {e['name']}: min_lotr must look like 0.5.0")
    return ents


def _skip(name, why):
    return {"name": name, "action": "skipped", "reason": why}


def update(home, *, base=None, ref="main", dry_run=False, force=False, only=None,
           allow_insecure_localhost=False, fetch=None):
    """Fetch, verify and install the catalog's templates. Never creates a connection."""
    from . import template_tool
    net = oauth.NetPolicy(False, allow_insecure_localhost)
    root = base_url(base, ref)
    custom = bool(base)
    get = fetch or (lambda path: _get(f"{root}/{path}", net, path))
    raw = get("catalog.json")
    sig_raw = None
    if pinned_key() is not None:                     # phase 112: before anything is parsed or used
        try:
            sig_raw = get(SIG_NAME)
        except GatewayError:
            sig_raw = None
    sig_state = check_signature(raw, sig_raw, home)
    try:
        cat = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise GatewayError("catalog_invalid", "catalog.json is not valid JSON")
    entries = validate_catalog(cat)
    here = lotr_version()
    if vtuple(cat.get("min_lotr", "0.0.0")) > vtuple(here):
        raise GatewayError("catalog_needs_newer_lotr",
                           f"this catalog needs gt-lotr {cat['min_lotr']} or newer; this is {here}",
                           hints=["update gt, then run lotr template update again"])
    state = read_state(home)
    if not force and isinstance(state.get("catalog_version"), int) \
            and cat["catalog_version"] < state["catalog_version"]:
        raise GatewayError("catalog_older",
                           f"the catalog is version {cat['catalog_version']} but version "
                           f"{state['catalog_version']} is installed; an older catalog is refused",
                           hints=["pass --force only if you mean to go back"])
    rows, installed = [], dict(state.get("templates") or {})
    todo_write = []
    for e in entries:
        name = e["name"]
        if only and name not in only:
            continue
        if name in RESERVED:
            rows.append(_skip(name, "the name is reserved for a shipped archetype"))
            continue
        if e.get("min_lotr") and vtuple(e["min_lotr"]) > vtuple(here):
            rows.append(_skip(name, f"needs gt-lotr {e['min_lotr']} or newer"))
            continue
        try:
            body = get(e["file"])
            if not hmac.compare_digest(sha256(body), e["sha256"]):
                rows.append(_skip(name, "the template does not match the hash in the catalog"))
                continue
            tpl = json.loads(body.decode("utf-8"))
            ct.validate(tpl, e["file"])
            if tpl.get("name") != name:
                rows.append(_skip(name, "the file's name is not the catalog entry's name"))
                continue
            if tpl.get("archetype") == name:
                rows.append(_skip(name, "a catalog template cannot be an archetype"))
                continue
            left = template_tool.todo_paths(tpl)
            if left:
                rows.append(_skip(name, f"{left[0]} still says TODO"))
                continue
            fbody = get(e["facts"])
            if not hmac.compare_digest(sha256(fbody), e["facts_sha256"]):
                rows.append(_skip(name, "the fact sheet does not match the hash in the catalog"))
                continue
            sheet = json.loads(fbody.decode("utf-8"))
            fr = facts_mod.check(sheet, tpl)
            if not fr["ok"]:
                rows.append(_skip(name, "its fact sheet is not valid: " + fr["problems"][0]))
                continue
        except GatewayError as ex:
            if ex.code in ("catalog_unreachable",):
                rows.append(_skip(name, ex.message))
            else:
                rows.append(_skip(name, "refused: " + ex.message.split(": ", 1)[-1][:200]))
            continue
        except (ValueError, UnicodeDecodeError):
            rows.append(_skip(name, "the template or its fact sheet is not valid JSON"))
            continue
        old = (installed.get(name) or {}).get("sha256")
        action = "unchanged" if old == e["sha256"] else ("updated" if old else "new")
        exists = (ct.catalog_dir(home) / f"{name}.json").is_file()
        if action == "unchanged" and exists:
            rows.append({"name": name, "action": "unchanged", "kind": tpl["kind"]})
            continue
        if action == "unchanged":
            action = "new"
        rows.append({"name": name, "action": action, "kind": tpl["kind"],
                     "weakest_fact": fr["weakest"], "summary": tpl["summary"]})
        todo_write.append((name, body, fbody, e["sha256"]))
    if not dry_run and todo_write:
        cdir = ct.catalog_dir(home)
        fdir = Path(home) / "catalog" / "facts"
        for name, body, fbody, digest in todo_write:
            atomic_write(cdir / f"{name}.json", body.decode("utf-8"), 0o644)
            atomic_write(fdir / f"{name}.facts.json", fbody.decode("utf-8"), 0o644)
            installed[name] = {"sha256": digest, "installed_at": now_iso()}
    if not dry_run:
        in_cat = {e["name"] for e in entries}
        atomic_write(state_path(home), json.dumps({
            "schema": 1, "catalog_version": cat["catalog_version"], "source": root,
            "fetched_at": now_iso(), "signature": sig_state["text"],
            "templates": installed}, indent=2) + "\n", 0o644)
        gone = sorted(set(installed) - in_cat)
    else:
        gone = sorted(set(installed) - {e["name"] for e in entries})
    counts = {}
    for r in rows:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
    out = {"ok": True, "dry_run": dry_run, "source": root, "custom_source": custom,
           "catalog_version": cat["catalog_version"], "signature": sig_state["text"],
           "counts": counts, "templates": rows,
           "no_longer_in_catalog": gone,
           "next": ["No connection was created or changed. Set one up with: lotr setup NAME",
                    "Templates that changed apply to NEW connections only; to apply one to an "
                    "existing connection, disconnect it and run lotr setup again."]}
    bv = ct.bundled_version()
    if bv:
        out["bundled_catalog_version"] = bv
        if cat["catalog_version"] <= bv:
            out["next"].insert(0, f"The catalog included with gt-lotr is version {bv}, as new as "
                                  f"this one or newer, so it stays in use; a template here only "
                                  f"adds a name it lacks.")
    if custom:
        out["warning"] = (f"templates came from {root}, not the public repository: you are "
                          "trusting that source")
    return out
