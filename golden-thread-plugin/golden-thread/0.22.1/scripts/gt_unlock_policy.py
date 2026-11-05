#!/usr/bin/env python3
"""gt_unlock_policy -- what the unlock authority enforces: the user's policy, tightened by an
administrator's floor (0.20.1).

TWO FILES
  user   ~/.claude/golden-thread/unlock/policy.json. Written by `gt_unlock.py policy ...`, which
         asks the authority for a step-up first. Absent = unlock is off.
  admin  a file only an administrator can write:
           macOS    /Library/Application Support/gt/unlock-policy.json
           Linux    /etc/gt/unlock-policy.json
           Windows  %ProgramData%\\gt\\unlock-policy.json (or the registry value
                    HKLM\\SOFTWARE\\Policies\\gt\\UnlockPolicy, a JSON string, for GPO/Intune)
         Its EXISTENCE turns unlock on, whatever the user file says.

THE MERGE ONLY TIGHTENS (invariant): enabled = OR; required factors = max; allowed factors =
intersection; ttl/idle/fresh/consent window/secrets window = min; each scope's level = the
stricter of the user's level and the ADMIN FLOOR's, each found by its own most-specific match
(Effective.level -- a user pattern of any specificity can never loosen an admin pattern of any
other: the review's F2, 2026-10-03); read_without_unlock = AND; the unattended allow-list = the admin's when it sets one, else
the user's (an admin list can only remove entries); SSO settings the admin pins replace the
user's; `locked_keys` names top-level keys the user cannot set at all.

FAIL CLOSED (invariant), only when unlock is ON:
  * an admin file that exists but cannot be read, does not parse, or is not owned by root /
    Administrators (or is writable by anyone else, or sits in a directory that is) ->
    EVERY gated scope is locked, with the reason;
  * K (required factors) larger than the factors usable on this machine -> locked; K is
    never silently lowered;
  * a user file that does not parse -> locked (it is not "off": an agent could otherwise
    switch unlock off by corrupting the file).
With unlock OFF nothing here runs on any hot path but two stat() calls (enabled_fast).

THE ADMIN FLOOR IS BINDING ONLY WHEN THE CODE ENFORCING IT IS ALSO ADMIN-OWNED (L3). At L1/L2
the authority runs as the user, from files the user can change; the floor then stops
accidents and agents, not the user. The docs and `gt_unlock.py status` say so.
"""
import copy
import fnmatch
import json
import os
import re
import stat
import sys

IS_WINDOWS = os.name == "nt"

LEVELS = ("open", "unlocked", "step_up", "deny")          # least to most strict
_RANK = {lv: i for i, lv in enumerate(LEVELS)}
FACTORS = ("totp", "touchid", "hello", "sso", "recovery")
PLATFORM_FACTORS = ("touchid", "hello")
LOCK_ON = ("screen_lock", "sleep", "session_end", "daemon_restart", "shim_exit")
MAX_SECRETS_WINDOW_S = 900          # "a short window": at most 15 minutes
# The merged policy carries the admin's own scope patterns under this key, so the level of a
# scope can be computed against the admin floor separately (F2). Never taken from a user file.
ADMIN_FLOOR_KEY = "admin_floor_scopes"
# Written by the authority when unlock is turned on through it, removed only when it is turned
# off through it. Two copies: one in the unlock home, one beside it, so removing the whole home
# (policy, state and all) still leaves unlock reading as ON -- and then failing closed.
MARKER = "unlock-on"

# Scopes gt itself checks. Anything a user or another tool asks for that is not here falls to
# the default level (`unlocked`).
GT_SCOPES = {
    "gt:unlock:policy": "step_up",      # policy edits, approving an out-of-band edit
    "gt:unlock:enroll": "step_up",      # adding / removing a factor, new recovery codes
    "gt:settings:hooks": "step_up",     # settings.json, the hooks dir, disabling a guard
    "gt:settings:security": "step_up",  # gt_settings keys that switch a guard off
    "gt:action:token": "step_up",  # minting a single-use action token (0.21.0 #32)
    "gt:secrets": "unlocked",           # resolving a secret ref through the broker
    "gt:publish": "unlocked",           # push / tag / release credentials via the broker
    "gt:hub:enroll": "step_up",         # LOTR hub client enrol / revoke
    "gt:lock:*": "unlocked",            # opening a locked vault file
    "gt:vault:read": "unlocked",        # gt sandbox mode's vault MCP reads (read_without_unlock
                                        # opens it like a LOTR read; door mcp_only binds it)
    "gt:vault:accept": "step_up",       # accepting a #conflict version: a fresh factor each time (0.21.0)
    "gt:vault:write": "unlocked",       # the vault MCP's writes and operation tools (0.20.2)
    "gt:io:read": "step_up",            # the disk-read broker, outside the allow list (0.21.0)
    "gt:io:fetch": "step_up",           # the network-fetch broker, outside the allow list (0.21.0)
    "gt:io:write": "step_up",           # the disk-write broker: every write (0.21.0)
}


# --------------------------------------------------------------------------- relax (0.22.0)
# `relax` lets a person who has proved presence stop being asked for a FRESH factor on every step-up
# request: for the rest of one session (the grant carries a flag; it ends with the grant) or as a
# standing choice in the signed policy (`relax.standing`). It lowers step_up to unlocked for the
# scopes below and for NOTHING else: a grant is still required, an administrator's floor still wins,
# and the scopes that decide what unlocking means, what gt may do to itself, or who may speak for
# the person stay at step_up whatever the person chooses.
#
# The forms are pinned here, one row each, so adding a scope to the relaxable set is a visible edit
# that a test fails on until it is made on purpose (tests/test_unlock_relax.py).
RELAXABLE_SCOPES = (
    "gt:io:read", "gt:io:fetch", "gt:io:write",            # the I/O brokers (fresh confirmation each time)
    "gt:git-push", "gt:git-push:<project>",                # git-push and outbound-push gates
    "gt:git-commit",                                       # the git-commit gate
    "gt:publish",                                          # the publish gate
    "lotr:*:write", "lotr:<connection>:write",             # the mcp-write gate
    "gt:vault:destructive",                                # the vault-destructive gate
)
# Every gate is classified exactly once: relaxable, or never. A new gate that is in neither list fails
# a test, so nobody can add one without deciding.
RELAX_GATES = ("io.read", "io.fetch", "io.write", "git-push", "outbound-push", "git-commit",
               "publish", "mcp-write", "vault-destructive")
NEVER_RELAX_GATES = ("lotr-consent", "lotr-disconnect", "secret", "gt-code-write")
# Scopes outside the gates that are never relaxable: policy, enrolment, settings, hooks, action
# tokens, hub enrolment and the accept of a #conflict version.
NEVER_RELAX_SCOPES = ("gt:unlock:policy", "gt:unlock:enroll", "gt:settings:hooks",
                      "gt:settings:security", "gt:action:token", "gt:hub:enroll", "gt:vault:accept")
_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
_RELAXABLE_RE = re.compile(
    r"gt:io:(?:read|fetch|write)|gt:git-push(?::%s)?|gt:git-commit|gt:publish|gt:vault:destructive"
    r"|lotr:(?:\*|%s):write" % (_NAME, _NAME))
RELAX_MARKER = "relax-standing-off"


def relaxable(scope):
    """Can `relax` lower this scope from step_up to unlocked? Exact forms only (RELAXABLE_SCOPES)."""
    return isinstance(scope, str) and len(scope) <= 200 and _RELAXABLE_RE.fullmatch(scope) is not None


def relax_marker_path(unlock_dir):
    return os.path.join(unlock_dir, RELAX_MARKER)


def relax_marker_present(unlock_dir):
    return os.path.lexists(relax_marker_path(unlock_dir))


def set_relax_marker(unlock_dir, on):
    """Write (on) or remove (off) the tighten-only marker. It can only switch `relax.standing` OFF,
    so it needs no factor and sits outside the signed policy: while a platform factor is enrolled
    any edit of policy.json needs a signed approval, and switching standing off must not."""
    path = relax_marker_path(unlock_dir)
    try:
        if on:
            os.makedirs(unlock_dir, exist_ok=True)
            # Review F4: never write THROUGH whatever sits at the path. A symlink (or a fifo, or any
            # other non-regular file) is removed first -- that touches only the link, never its
            # target -- and the open refuses to follow a link raced in after that.
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                st = None
            if st is not None and not stat.S_ISREG(st.st_mode):
                os.unlink(path)                       # a directory raises: refused below
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_BINARY", 0), 0o600)
            try:
                os.write(fd, b"standing relax was turned off; only a fresh factor turns it back on\n")
            finally:
                os.close(fd)
        else:
            os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True


def apply_relax_marker(policy, unlock_dir):
    """The effective policy with the marker applied: standing is off while the marker exists."""
    if relax_marker_present(unlock_dir):
        policy["relax"] = dict(policy.get("relax") or {}, standing=False)
    return policy


# --------------------------------------------------------------------------- presence gates
# The presence-gate registry (0.21.0, request 2026-10-05-gt-unlock-presence-gate-registry). A gate
# is one on/off switch that auto-wires a presence check for an ACTION: a scope raised to step_up in
# the user policy (so the authority wants a fresh confirmation) and, for the git gates, a hook in
# the repo that asks for it. Off removes exactly that wiring. `feasibility` is FEASIBLE when gt can
# see and gate the action with a primitive that exists today; `enforcement` says where the check is
# actually made, because a gate nobody consults is a switch that lies. Nothing here claims to gate
# what does not pass through gt, LOTR, the vault or a git hook gt owns.
GATE_LEVEL = "step_up"
GATES = {
    "git-push": {
        "scope": "gt:git-push", "param": None, "wiring": "hook", "hook": "pre-push",
        "feasibility": "FEASIBLE",
        "summary": "a fresh confirmation before `git push` from the repos it is turned on in",
        "enforcement": "a pre-push hook gt installs in the repo: tier 1 of the ladder, "
                       "`git push --no-verify` skips it. The bypass-resistant form is the sealed "
                       "push credential (gt_settings.py set push_fingerprint_seal_token on); the "
                       "ceiling is a server-side rule (branch protection, required signatures)"},
    "git-commit": {
        "scope": "gt:git-commit", "param": None, "wiring": "hook", "hook": "pre-commit",
        "feasibility": "FEASIBLE",
        "summary": "a fresh confirmation before `git commit` in the repos it is turned on in",
        "enforcement": "a pre-commit hook gt installs in the repo (`--no-verify` skips it)"},
    "publish": {
        "scope": "gt:publish", "param": None, "wiring": "scope", "feasibility": "FEASIBLE",
        "summary": "a fresh confirmation before gt-visualize / artifact publishing and release pushes",
        "enforcement": "the gt:publish scope, asked by gt's publish paths and by push_fingerprint"},
    "lotr-consent": {
        "scope": "lotr:*:consent", "param": None, "wiring": "scope", "feasibility": "FEASIBLE",
        "summary": "presence on top of LOTR's own consent step (send, merge, delete)",
        "enforcement": "LOTR asks the scope for its consent-tier ops (lotrlib/policy.py)"},
    "mcp-write": {
        "scope": "lotr:*:write", "param": "connection", "param_required": False,
        "param_scope": "lotr:{p}:write", "wiring": "scope", "feasibility": "FEASIBLE",
        "summary": "a fresh confirmation for LOTR writes -- all connections, or one "
                   "(`mcp-write:jira`)",
        "enforcement": "LOTR asks the lotr:<connection>:write scope for a tier:write op"},
    "lotr-disconnect": {
        "scope": "gt:lotr:disconnect", "param": None, "wiring": "scope",
        "feasibility": "FEASIBLE",
        "summary": "a fresh confirmation for `lotr disconnect` and client revoke",
        "enforcement": "the gt:lotr:disconnect scope; LOTR's disconnect path must ask it "
                       "(the LOTR side ships in the gt-lotr module)"},
    "secret": {
        "scope": "gt:secrets", "param": None, "wiring": "scope", "feasibility": "FEASIBLE",
        "summary": "a fresh confirmation each time a secret ref is resolved or handed to a process",
        "enforcement": "the gt:secrets scope, asked by gt_unlock.py run / secret get"},
    "vault-destructive": {
        "scope": "gt:vault:destructive", "param": None, "wiring": "scope",
        "feasibility": "FEASIBLE",
        "summary": "a fresh confirmation for archive / retire / supersede / demote in the vault",
        "enforcement": "the gt:vault:destructive scope; gt's vault tools must ask it "
                       "(the vault side lands with the operations that name it)"},
    # Installed-code integrity (0.21.0, request 2026-10-08-installed-code-integrity; the `paranoid`
    # group's part 2). Wired by `paranoid.code_write_gate`; gt's installed code is OS-locked, and
    # unlocking it for an upgrade or a repair is the action this gates.
    "gt-code-write": {
        "scope": "gt:code:write", "param": None, "wiring": "scope", "feasibility": "FEASIBLE",
        "summary": "a fresh confirmation before gt's installed code is unlocked for an upgrade or "
                   "a repair",
        "enforcement": "the gt:code:write scope, asked by gt_integrity.py (unlock --for-upgrade, "
                       "which install.sh and component self-repair both go through); with unlock "
                       "off it is a typed confirmation on a real terminal, which says it is weaker"},
    # The I/O brokers (0.21.0, request 2026-10-08-io-brokers-disk-read-net-fetch-disk-write). The
    # scopes are step_up in GT_SCOPES above, so each is ALWAYS a fresh confirmation whenever
    # unlock is on: `always_on` records that and makes the gate impossible to switch off (a
    # switch that could be turned off would lie about what is enforced).
    "io.read": {
        "scope": "gt:io:read", "param": None, "wiring": "scope", "feasibility": "FEASIBLE",
        "always_on": True,
        "summary": "a fresh confirmation, naming the path, before disk_read / disk_list opens "
                   "anything outside the allow list (always on)",
        "enforcement": "the gt:io:read scope, asked by gt_io_broker.py inside the gt-vault MCP "
                       "server; never-paths are refused without asking"},
    "io.fetch": {
        "scope": "gt:io:fetch", "param": None, "wiring": "scope", "feasibility": "FEASIBLE",
        "always_on": True,
        "summary": "a fresh confirmation, naming the host, before net_fetch reaches a domain "
                   "outside the allow list (always on)",
        "enforcement": "the gt:io:fetch scope, asked by the network-fetch broker (net_fetch)"},
    "io.write": {
        "scope": "gt:io:write", "param": None, "wiring": "scope", "feasibility": "FEASIBLE",
        "always_on": True,
        "summary": "a fresh confirmation before EVERY disk_write, or a live timed grant for "
                   "that repo (always on; io_write_presence cannot be set off)",
        "enforcement": "the gt:io:write scope, asked by the disk-write broker (disk_write)"},
    "outbound-push": {
        "scope": None, "param": "project", "param_required": True,
        "param_scope": "gt:git-push:{p}", "wiring": "hook", "hook": "pre-push",
        "feasibility": "FEASIBLE",
        "summary": "a fresh confirmation before pushing ONE project's repo (`outbound-push:NAME`, "
                   "with the repo named)",
        "enforcement": "the same pre-push hook, asking gt:git-push:<project>"},
}
_GATE_PARAM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def parse_gate(text):
    """'mcp-write:jira' -> ('mcp-write', 'jira'). ValueError names the valid gates."""
    name, sep, param = (text or "").partition(":")
    if name not in GATES:
        raise ValueError("unknown gate %r: use one of %s" % (text, ", ".join(sorted(GATES))))
    g = GATES[name]
    if g["param"] is None:
        if sep:
            raise ValueError("gate %s takes no parameter" % name)
        return name, None
    if not sep:
        if g.get("param_required"):
            raise ValueError("gate %s is per %s: write %s:<%s>" % (name, g["param"], name, g["param"]))
        return name, None
    if not _GATE_PARAM_RE.match(param):
        raise ValueError("gate %s: %r is not a valid %s name" % (name, param, g["param"]))
    return name, param


def gate_scope(name, param):
    g = GATES[name]
    return g["param_scope"].format(p=param) if param else g["scope"]


def gate_label(name, param):
    return "%s:%s" % (name, param) if param else name


def gates_of(policy):
    """-> the gate entries a user policy carries ([] when none)."""
    g = (policy or {}).get("gates")
    return [e for e in g if isinstance(e, dict)] if isinstance(g, list) else []


def apply_gate(policy, name, param, on, repos=()):
    """The user policy with gate `name[:param]` turned on or off. Pure: returns a copy. On raises
    the gate's scope to step_up and remembers what the user had there; off puts that back."""
    if not on and GATES[name].get("always_on"):
        raise ValueError("gate %s is always on and cannot be turned off" % name)
    p = copy.deepcopy(policy or {"schema": 1})
    scope = gate_scope(name, param)
    gates = gates_of(p)
    mine = [e for e in gates if e.get("gate") == name and e.get("param") == param]
    rest = [e for e in gates if e not in mine]
    scopes = p.setdefault("scopes", {})
    if on:
        if mine:
            entry = mine[0]
        else:
            entry = {"gate": name, "param": param, "scope": scope, "was": scopes.get(scope),
                     "repos": []}
        entry["repos"] = sorted(set(entry.get("repos") or []) | set(repos))
        scopes[scope] = GATE_LEVEL
        rest.append(entry)
    else:
        for entry in mine:
            if entry.get("was") is None:
                scopes.pop(scope, None)
            else:
                scopes[scope] = entry["was"]
    if rest:
        p["gates"] = rest
    else:
        p.pop("gates", None)
    if not scopes:
        p.pop("scopes", None)
    return p


def default_policy(platform=None):
    """The policy unlock starts from when it is switched on (owner decisions 2026-10-03):
    TOTP + Touch ID on macOS, TOTP + Windows Hello on Windows, TOTP on Linux."""
    platform = platform or sys.platform
    if platform == "darwin":
        req, one_of = 2, ["touchid"]
    elif platform.startswith("win"):
        req, one_of = 2, ["hello"]
    else:
        req, one_of = 1, []
    return {
        "schema": 1,
        "enabled": False,
        "factors": {"allowed": list(FACTORS), "required": req,
                    "require_one_of": one_of, "require_any_of": []},
        "totp": {"skew_steps": 1, "max_failures": 5, "lockout_minutes": [1, 5, 15],
                 "prompt": "auto"},
        "sso": {"provider": "entra", "issuer": None, "tenant": "organizations",
                "client_id": None, "require_mfa": False, "max_auth_age_s": 300,
                "flow": "pkce_loopback", "allow_device_code": False, "pin_subject": None,
                "allow_es256": False},
        "grant": {"ttl_s": 28800, "idle_s": 900, "lock_on": list(LOCK_ON)},
        "scopes": dict(GT_SCOPES, **{"lotr:*:read": "unlocked", "lotr:*:write": "unlocked",
                                     "lotr:*:consent": "unlocked"}),
        "default_scope_level": "unlocked",
        "read_without_unlock": True,
        "step_up": {"factors": list(PLATFORM_FACTORS), "fresh_s": 60},
        # Consent-tier LOTR ops always get LOTR's own confirmation; "platform" replaces it with
        # a Touch ID / Hello signature over the op (owner decision 5: an OPTION), valid for
        # consent_window_s seconds within the grant.
        "consent_requires_factor": "none",
        "consent_window_s": 0,                   # 0 = every consent op asks again
        # Owner decision 2026-10-03 18:37: a sealed value is opened with a fresh Touch ID /
        # Hello EVERY time (0, the default), or once per this many seconds per subject and
        # grant -- never cached beyond it, never shared between processes.
        "secrets_window_s": 0,
        "door": "mcp_only",
        # 0.22.0: standing = stop asking for a fresh factor on the relaxable scopes (RELAXABLE_SCOPES);
        # allow = false forbids relax altogether (session and standing). Both tighten-only in the merge.
        "relax": {"standing": False, "allow": True},
        "unattended": {"allowed": []},           # [{"job": name, "scope": scope}, ...]
        "locked_keys": [],
    }


class PolicyError(ValueError):
    pass


def _deep(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "scopes":
            out[k] = _deep(out[k], v)
        elif k == "scopes" and isinstance(v, dict):
            s = dict(out.get("scopes") or {})
            s.update(v)
            out[k] = s
        else:
            out[k] = v
    return out


def validate(p):
    """Raise PolicyError naming the first problem. Validates shape, not intent."""
    if not isinstance(p, dict):
        raise PolicyError("policy is not a JSON object")
    f = p.get("factors") or {}
    for name in f.get("allowed") or []:
        if name not in FACTORS:
            raise PolicyError("unknown factor %r" % name)
    req = f.get("required")
    if not isinstance(req, int) or isinstance(req, bool) or req < 1 or req > 4:
        raise PolicyError("factors.required must be 1-4")
    for k in ("ttl_s", "idle_s"):
        v = (p.get("grant") or {}).get(k)
        if not isinstance(v, int) or isinstance(v, bool) or v < 30:
            raise PolicyError("grant.%s must be an integer >= 30" % k)
    for scope, level in (p.get("scopes") or {}).items():
        if level not in LEVELS:
            raise PolicyError("scope %s: level must be one of %s" % (scope, ", ".join(LEVELS)))
    if p.get("default_scope_level") not in LEVELS:
        raise PolicyError("default_scope_level must be one of %s" % ", ".join(LEVELS))
    sso = p.get("sso") or {}
    if (sso.get("provider") or "entra") != "entra":
        raise PolicyError("sso.provider must be entra (the only provider in 0.20.1)")
    ten = sso.get("tenant") or "organizations"
    if ten not in ("organizations", "common", "consumers") and not re.match(
            r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
            str(ten)):
        raise PolicyError("sso.tenant must be organizations, common, consumers or a tenant GUID")
    if sso.get("client_id") is not None and not isinstance(sso.get("client_id"), str):
        raise PolicyError("sso.client_id must be a string")
    age = sso.get("max_auth_age_s", 300)
    if not isinstance(age, int) or isinstance(age, bool) or not 30 <= age <= 3600:
        raise PolicyError("sso.max_auth_age_s must be 30-3600")
    if p.get("door") not in ("mcp_only", "session"):
        raise PolicyError("door must be mcp_only or session")
    if p.get("consent_requires_factor") not in ("none", "platform"):
        raise PolicyError("consent_requires_factor must be none or platform")
    w = p.get("consent_window_s")
    if not isinstance(w, int) or isinstance(w, bool) or w < 0:
        raise PolicyError("consent_window_s must be an integer >= 0")
    w = p.get("secrets_window_s")
    if not isinstance(w, int) or isinstance(w, bool) or not 0 <= w <= MAX_SECRETS_WINDOW_S:
        raise PolicyError("secrets_window_s must be an integer 0-%d (0 = a fresh factor for "
                          "every unseal)" % MAX_SECRETS_WINDOW_S)
    for e in (p.get("unattended") or {}).get("allowed") or []:
        if not isinstance(e, dict) or not e.get("job") or not e.get("scope"):
            raise PolicyError("unattended.allowed entries are {job, scope}")
        if not unattended_scope_ok(e["scope"]):
            raise PolicyError("scope %s can never be allowed unattended (write, consent, "
                              "publish, secrets and policy scopes are refused)" % e["scope"])
    rx = p.get("relax")
    if not isinstance(rx, dict):
        raise PolicyError("relax must be an object {standing, allow}")
    for k in sorted(set(rx) - {"standing", "allow"}):
        raise PolicyError("relax: unknown field %r" % str(k)[:40])
    for k in ("standing", "allow"):
        if not isinstance(rx.get(k), bool):
            raise PolicyError("relax.%s must be true or false" % k)
    gates = p.get("gates")
    if gates is not None and not isinstance(gates, list):
        raise PolicyError("gates must be a list")
    for e in gates or []:
        if not isinstance(e, dict):
            raise PolicyError("gates entries are objects")
        try:
            name, param = parse_gate(gate_label(str(e.get("gate")), e.get("param")))
        except ValueError as ex:
            raise PolicyError("gates: %s" % ex)
        if e.get("scope") != gate_scope(name, param):
            raise PolicyError("gates: %s must carry the scope %s"
                              % (gate_label(name, param), gate_scope(name, param)))
    return p


def unattended_scope_ok(scope):
    """INVARIANT (owner, 2026-10-03): an unattended job may hold one NARROW, mostly read-only
    scope. Write, consent and publish can never be allowed, nor any wildcard, nor gt's own
    policy, enrolment, settings, hub or broker scopes."""
    if "*" in scope or "?" in scope or "[" in scope:
        return False
    if scope.startswith("lotr:"):
        parts = scope.split(":")
        return len(parts) == 3 and parts[2] == "read"
    if scope.startswith("gt:"):
        return False
    if scope.startswith("secret:"):
        return True            # one named secret ref, e.g. a reminder job's send credential
    return scope.endswith(":read")


# ---------------------------------------------------------------- files

def unlock_home(home=None):
    base = home or os.path.expanduser("~")
    return os.path.join(base, ".claude", "golden-thread", "unlock")


def admin_paths():
    if IS_WINDOWS:
        pd = os.environ.get("ProgramData") or r"C:\ProgramData"
        return [os.path.join(pd, "gt", "unlock-policy.json")]
    if sys.platform == "darwin":
        return ["/Library/Application Support/gt/unlock-policy.json"]
    return ["/etc/gt/unlock-policy.json"]


def _admin_registry():
    """HKLM\\SOFTWARE\\Policies\\gt\\UnlockPolicy (REG_SZ JSON), or None. Windows only; HKLM
    policy keys are writable by administrators only, so no ownership check is needed."""
    if not IS_WINDOWS:
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Policies\gt") as k:
            v, _t = winreg.QueryValueEx(k, "UnlockPolicy")
            return v
    except OSError:
        return None


def _posix_admin_owned(path, trusted_uids):
    """INVARIANT: the admin file and EVERY directory above it are owned by a trusted uid (root)
    and writable by no one else -- otherwise a user could swap the file or a parent. Checked on
    the path as written (a symlink there must itself be root's, like macOS's /var) AND on the
    resolved path the file is really read from."""
    def chain(p):
        while True:
            yield p
            parent = os.path.dirname(p)
            if parent == p:
                return
            p = parent
    for p in chain(os.path.abspath(path)):
        st = os.lstat(p)
        if st.st_uid not in trusted_uids:
            return "%s is not owned by root" % p
        if not os.path.islink(p) and st.st_mode & 0o022:
            return "%s is writable by group or others" % p
    for p in chain(os.path.realpath(path)):
        st = os.lstat(p)
        if st.st_uid not in trusted_uids:
            return "%s is not owned by root" % p
        # group/other write is refused; the sticky bit on a dir (e.g. /tmp) does not make a
        # world-writable directory safe for a policy file
        if st.st_mode & 0o022:
            return "%s is writable by group or others" % p
    return None


_ADMIN_SIDS = ("S-1-5-32-544", "S-1-5-18", "S-1-5-80-956008885-3418522649-1831038044-"
               "1853292631-2271478464")     # Administrators, SYSTEM, TrustedInstaller


def _windows_admin_owned(path):
    """INVARIANT: owner is Administrators / SYSTEM / TrustedInstaller and no ACE grants write
    to the current user, Users, Authenticated Users, INTERACTIVE or Everyone."""
    import ctypes
    from ctypes import wintypes as wt
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psid_owner, pdacl, psd = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    # SE_FILE_OBJECT = 1; OWNER (1) | DACL (4)
    adv.GetNamedSecurityInfoW.restype = wt.DWORD
    adv.GetNamedSecurityInfoW.argtypes = [wt.LPCWSTR, ctypes.c_int, wt.DWORD,
                                          ctypes.POINTER(ctypes.c_void_p),
                                          ctypes.POINTER(ctypes.c_void_p),
                                          ctypes.POINTER(ctypes.c_void_p),
                                          ctypes.POINTER(ctypes.c_void_p),
                                          ctypes.POINTER(ctypes.c_void_p)]
    rc = adv.GetNamedSecurityInfoW(path, 1, 1 | 4, ctypes.byref(psid_owner), None,
                                   ctypes.byref(pdacl), None, ctypes.byref(psd))
    if rc != 0:
        return "cannot read the security descriptor of %s (error %d)" % (path, rc)
    try:
        adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.LPWSTR)]
        k32.LocalFree.argtypes = [ctypes.c_void_p]

        def sid_str(psid):
            out = wt.LPWSTR()
            if not adv.ConvertSidToStringSidW(psid, ctypes.byref(out)):
                return None
            try:
                return out.value
            finally:
                k32.LocalFree(ctypes.cast(out, ctypes.c_void_p))
        owner = sid_str(psid_owner)
        if owner not in _ADMIN_SIDS:
            return "%s is owned by %s, not Administrators or SYSTEM" % (path, owner)
        if not pdacl.value:
            return "%s has a NULL DACL (everyone may write it)" % path
        import gt_ipc
        untrusted = {gt_ipc.own_sid(), "S-1-5-32-545", "S-1-5-11", "S-1-5-4", "S-1-1-0"}

        class ACL(ctypes.Structure):
            _fields_ = [("rev", ctypes.c_ubyte), ("sbz1", ctypes.c_ubyte),
                        ("size", wt.WORD), ("count", wt.WORD), ("sbz2", wt.WORD)]

        class HDR(ctypes.Structure):
            _fields_ = [("type", ctypes.c_ubyte), ("flags", ctypes.c_ubyte), ("size", wt.WORD),
                        ("mask", wt.DWORD)]
        adv.GetAce.argtypes = [ctypes.c_void_p, wt.DWORD, ctypes.POINTER(ctypes.c_void_p)]
        acl = ACL.from_address(pdacl.value)
        WRITE = 0x40000000 | 0x10000000 | 0x2 | 0x4 | 0x100 | 0x40000 | 0x80000 | 0x10000
        for i in range(acl.count):
            pace = ctypes.c_void_p()
            if not adv.GetAce(pdacl, i, ctypes.byref(pace)):
                return "cannot read an ACE of %s" % path
            hdr = HDR.from_address(pace.value)
            if hdr.type != 0:            # ACCESS_ALLOWED_ACE_TYPE only grants
                continue
            sid = sid_str(pace.value + ctypes.sizeof(HDR))
            if sid in untrusted and hdr.mask & WRITE:
                return "%s grants write access to %s" % (path, sid)
        return None
    finally:
        k32.LocalFree(psd)


def read_admin(paths=None, trusted_uids=(0,)):
    """-> (policy_or_None, problem_or_None). A problem means FAIL CLOSED (when unlock is on).
    None, None = no admin floor on this machine."""
    reg = _admin_registry()
    if reg is not None:
        try:
            return validate_partial(json.loads(reg)), None
        except (ValueError, PolicyError) as e:
            return None, "the registry admin policy does not parse: %s" % e
    for path in paths or admin_paths():
        if not os.path.lexists(path):
            continue
        try:
            problem = (_windows_admin_owned(path) if IS_WINDOWS
                       else _posix_admin_owned(path, set(trusted_uids)))
        except OSError as e:
            problem = "cannot check the owner of %s (%s)" % (path, e.strerror)
        if problem:
            return None, "admin policy refused: " + problem
        try:
            with open(path, "r", encoding="utf-8") as f:
                return validate_partial(json.load(f)), None
        except (OSError, ValueError, PolicyError) as e:
            return None, "admin policy %s unreadable: %s" % (path, e)
    return None, None


def validate_partial(p):
    if not isinstance(p, dict):
        raise PolicyError("not a JSON object")
    return p


def read_user(home=None):
    """-> (policy_or_None, problem_or_None)."""
    path = os.path.join(unlock_home(home), "policy.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as e:
        return None, "user policy %s unreadable: %s" % (path, e)
    if not isinstance(raw, dict):
        return None, "user policy is not a JSON object"
    return raw, None


def enabled_at(unlock_dir, admin=None):
    """Is unlock on for the unlock home `unlock_dir`? Never raises.

    ON when: an admin floor exists; or policy.json says enabled; or state.json carries the
    authority's `unlock_on` marker (written when the policy was turned on THROUGH the
    authority, cleared only when it is turned off the same way). The marker is what stops
    the one-line bypass of editing policy.json to `"enabled": false` -- the file then
    disagrees with the marker and the authority fails closed instead of standing down. An
    unreadable policy counts as ON (fail closed); a missing one as off."""
    try:
        for path in admin or admin_paths():
            if os.path.lexists(path):
                return True
        if IS_WINDOWS and _admin_registry() is not None:
            return True
        if marker_present(unlock_dir):
            return True
        try:
            with open(os.path.join(unlock_dir, "state.json"), "r", encoding="utf-8") as f:
                if json.load(f).get("unlock_on"):
                    return True
        except (OSError, ValueError, AttributeError):
            pass
        path = os.path.join(unlock_dir, "policy.json")
        if not os.path.exists(path):
            return False
        with open(path, "r", encoding="utf-8") as f:
            return bool(json.load(f).get("enabled", False))
    except Exception:                                  # noqa: BLE001
        return True


def marker_paths(unlock_dir):
    """The two `unlock-on` markers for an unlock home: inside it, and beside it."""
    unlock_dir = os.path.abspath(unlock_dir)
    return [os.path.join(unlock_dir, MARKER),
            os.path.join(os.path.dirname(unlock_dir), "." + os.path.basename(unlock_dir)
                         + "-" + MARKER)]


def marker_present(unlock_dir):
    """True when either marker exists. Deleting policy.json and state.json therefore never
    reads as "off" (review F3): the authority sees unlock on with no policy and fails closed.
    A same-user process can delete the markers too -- that is friction, documented at L1/L2."""
    return any(os.path.lexists(p) for p in marker_paths(unlock_dir))


def set_marker(unlock_dir, on):
    """Write (on) or remove (off) both markers. Called by the authority only."""
    for p in marker_paths(unlock_dir):
        try:
            if on:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                fd = os.open(p, os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
                try:
                    os.write(fd, b"unlock was turned on through the gt unlock authority\n")
                finally:
                    os.close(fd)
            else:
                os.unlink(p)
        except OSError:
            pass


def enabled_fast(home=None, admin=None):
    """The hot-path question every hook asks first: is unlock on at all? A few stat() calls
    and small JSON reads (see enabled_at)."""
    return enabled_at(unlock_home(home), admin)


def _stricter(a, b):
    return a if _RANK.get(a, 1) >= _RANK.get(b, 1) else b


def merge(user, admin):
    """The effective policy: defaults <- user <- (admin, tightening only). Pure function."""
    base = default_policy()
    user = {k: v for k, v in (user or {}).items() if k != ADMIN_FLOOR_KEY}
    eff = _deep(base, user)
    eff.pop(ADMIN_FLOOR_KEY, None)
    if admin is None:
        return validate(eff)
    locked = set(admin.get("locked_keys") or [])
    for k in locked:
        if k in admin:
            eff[k] = copy.deepcopy(admin[k])
        elif k in base:
            eff[k] = copy.deepcopy(base[k])
    eff["enabled"] = True                            # an admin file's existence = on
    af = admin.get("factors") or {}
    ef = eff["factors"]
    if "allowed" in af:
        ef["allowed"] = [x for x in ef["allowed"] if x in af["allowed"]]
    if "required" in af:
        ef["required"] = max(ef["required"], int(af["required"]))
    for key in ("require_one_of", "require_any_of"):
        if af.get(key):
            ef[key] = list(af[key])
    ag = admin.get("grant") or {}
    for key in ("ttl_s", "idle_s"):
        if key in ag:
            eff["grant"][key] = min(eff["grant"][key], int(ag[key]))
    if "lock_on" in ag:
        eff["grant"]["lock_on"] = sorted(set(eff["grant"]["lock_on"]) | set(ag["lock_on"]))
    for scope, level in (admin.get("scopes") or {}).items():
        eff["scopes"][scope] = _stricter(eff["scopes"].get(scope, eff["default_scope_level"]),
                                         level)
    if "default_scope_level" in admin:
        eff["default_scope_level"] = _stricter(eff["default_scope_level"],
                                               admin["default_scope_level"])
    # F2: keep the admin's own patterns (and its default) so level() can find the admin's
    # most-specific match on its own and never let a more specific USER pattern loosen it.
    floor = {k: v for k, v in (admin.get("scopes") or {}).items() if v in LEVELS}
    eff[ADMIN_FLOOR_KEY] = {"scopes": floor,
                            "default": admin.get("default_scope_level")
                            if admin.get("default_scope_level") in LEVELS else None}
    if "read_without_unlock" in admin:
        eff["read_without_unlock"] = bool(eff["read_without_unlock"]) and \
            bool(admin["read_without_unlock"])
    st = admin.get("step_up") or {}
    if "fresh_s" in st:
        eff["step_up"]["fresh_s"] = min(eff["step_up"]["fresh_s"], int(st["fresh_s"]))
    if "consent_window_s" in admin:
        eff["consent_window_s"] = min(eff["consent_window_s"], int(admin["consent_window_s"]))
    if "secrets_window_s" in admin:
        eff["secrets_window_s"] = min(eff["secrets_window_s"], int(admin["secrets_window_s"]))
    if admin.get("consent_requires_factor") == "platform":
        eff["consent_requires_factor"] = "platform"
    if admin.get("door") == "mcp_only":
        eff["door"] = "mcp_only"
    ar = admin.get("relax")
    ar = ar if isinstance(ar, dict) else {}
    for key in ("standing", "allow"):
        if key in ar:
            eff["relax"][key] = bool(eff["relax"].get(key)) and bool(ar[key])
    # Review gap: an admin file that does not say `relax.allow` (every file written before 0.22.0)
    # forbids relax. Its floor was written without knowing relax exists, and the built-in step_up
    # defaults (gt:io:* and the gates) are not part of that floor, so the conservative reading wins.
    if "allow" not in ar:
        eff["relax"]["allow"] = False
    if "sso" in admin:
        eff["sso"] = _deep(eff["sso"], admin["sso"])
    au = admin.get("unattended")
    if isinstance(au, dict) and "allowed" in au:
        allowed = {(e.get("job"), e.get("scope")) for e in au["allowed"] or []}
        eff["unattended"]["allowed"] = [e for e in eff["unattended"]["allowed"]
                                        if (e.get("job"), e.get("scope")) in allowed]
    return validate(eff)


class Effective:
    """The merged policy plus the fail-closed verdict, as the authority consults it."""

    def __init__(self, policy, problems, enabled):
        self.policy = policy
        self.problems = list(problems)     # non-empty + enabled = everything gated is locked
        self.enabled = enabled

    @property
    def failed_closed(self):
        return self.enabled and bool(self.problems)

    def level(self, scope):
        """The level for `scope`, with the standing relax applied (RELAXABLE_SCOPES; 0.22.0)."""
        lv, alv = self._resolve(scope)
        if lv == "step_up" and self.relax_standing(scope, floor_level=alv):
            lv = "unlocked"
        return lv

    def unrelaxed_level(self, scope):
        """The level the policy and the admin floor give `scope`, before any relax."""
        return self._resolve(scope)[0]

    def _resolve(self, scope):
        """-> (level, admin_floor_level_or_None). The level for `scope`. Two answers, each by its OWN most-specific match (among
        equally specific patterns, the stricter): the merged policy's, and the admin floor's.
        The stricter of the two wins, so no user pattern of any specificity loosens an admin
        pattern of any other (review F2). With read_without_unlock, lotr reads are `open`
        unless a pattern makes them stricter than `unlocked`."""
        p = self.policy
        lv = _most_specific(p.get("scopes") or {}, scope) or p.get("default_scope_level",
                                                                  "unlocked")
        floor = p.get(ADMIN_FLOOR_KEY) or {}
        alv = _most_specific(floor.get("scopes") or {}, scope) or floor.get("default")
        if alv:
            lv = _stricter(lv, alv)
        if (scope.startswith("lotr:") and scope.endswith(":read") or scope == "gt:vault:read") \
                and lv == "unlocked" and p.get("read_without_unlock"):
            lv = "open"
        return lv, alv

    def relax_standing(self, scope, floor_level="?"):
        """Does the standing relax lower `scope` from step_up? Only for a relaxable scope, only when
        the person turned it on and policy allows relax, and never where the administrator's floor
        holds the scope at step_up or deny. A grant is still required (the level is `unlocked`)."""
        if floor_level == "?":
            floor = self.policy.get(ADMIN_FLOOR_KEY) or {}
            floor_level = _most_specific(floor.get("scopes") or {}, scope) or floor.get("default")
        rx = self.policy.get("relax") or {}
        return bool(rx.get("standing")) and rx.get("allow", True) is True and relaxable(scope) \
            and floor_level not in ("step_up", "deny")

    def relax_floor_ok(self, scope):
        """May a SESSION relax lower `scope`? The same tests as the standing form, minus the
        standing switch (the flag lives on the grant)."""
        rx = self.policy.get("relax") or {}
        floor = self.policy.get(ADMIN_FLOOR_KEY) or {}
        alv = _most_specific(floor.get("scopes") or {}, scope) or floor.get("default")
        return rx.get("allow", True) is True and relaxable(scope) and alv not in ("step_up", "deny")


def _most_specific(scopes, scope):
    best, best_spec = None, -1
    for pat, lv in scopes.items():
        if fnmatch.fnmatchcase(scope, pat):
            spec = len(pat.replace("*", ""))
            if spec > best_spec:
                best, best_spec = lv, spec
            elif spec == best_spec:
                best = _stricter(best, lv)
    return best


def load(home=None, admin_file_paths=None, trusted_uids=(0,)):
    """Read both files and merge them. Never raises: problems are reported in Effective."""
    user, uprob = read_user(home)
    admin, aprob = read_admin(admin_file_paths, trusted_uids)
    problems = [x for x in (uprob, aprob) if x]
    admin_exists = any(os.path.lexists(p) for p in (admin_file_paths or admin_paths())) \
        or (IS_WINDOWS and _admin_registry() is not None)
    enabled = bool((user or {}).get("enabled")) or admin_exists or bool(uprob)
    try:
        eff = merge(user if not uprob else None, admin)
    except (PolicyError, TypeError, ValueError) as e:
        problems.append("policy invalid: %s" % e)
        eff = default_policy()
    if aprob:
        eff["enabled"] = True
    apply_relax_marker(eff, unlock_home(home))
    return Effective(eff, problems, enabled)
