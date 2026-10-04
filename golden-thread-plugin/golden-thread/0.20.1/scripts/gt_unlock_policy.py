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
    "gt:secrets": "unlocked",           # resolving a secret ref through the broker
    "gt:publish": "unlocked",           # push / tag / release credentials via the broker
    "gt:hub:enroll": "step_up",         # LOTR hub client enrol / revoke
    "gt:lock:*": "unlocked",            # opening a locked vault file
    "gt:vault:read": "unlocked",        # gt sandbox mode's vault MCP reads (read_without_unlock
                                        # opens it like a LOTR read; door mcp_only binds it)
}


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
        """The level for `scope`. Two answers, each by its OWN most-specific match (among
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
        return lv


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
    return Effective(eff, problems, enabled)
