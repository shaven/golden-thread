#!/usr/bin/env python3
"""gt_unlockd -- the unlock authority: one per user, owned by gt core (0.20.0).

    gt_unlockd.py [--home DIR]

It holds the merged policy, the factor enrolments, the in-memory grant table and an append-only
audit log, and answers every consumer -- gt's hooks, `gt_unlock.py`, gt-lotr's daemon, the
credential broker -- over gt_ipc (a 600 unix socket, or a user-SID-only named pipe on Windows).
Started on demand by gt_unlock_client.ensure(); `gt_unlock.py daemon stop` stops it.

THE INVARIANTS, each enforced where it is commented below:
  I1  Identity comes from the kernel (gt_ipc peer), never from a message. A `subject` named in
      a check is only ever a process the CONSUMER identified by its own kernel peer lookup
      (lotrd naming its socket peer); the answer is about that subject, and asking grants
      nothing to the asker.
  I2  Only the authority collects factors. A client may REQUEST an unlock; it can never
      assert one -- there is no method that takes "the user already approved".
  I3  Grants live in memory only. A daemon restart revokes everything; nothing on disk can be
      forged into a grant. A grant id is an audit handle, not a credential.
  I4  A grant is bound to a SESSION ROOT (the `claude` process, or a terminal's shell) by pid +
      start time, and lotr:* scopes are served only to the session's registered MCP shim when
      the door is mcp_only. Bash, which also descends from `claude`, is refused lotr:*.
  I5  Revocation on: TTL, idle, shim exit, session root exit, SessionEnd, screen lock, sleep (a
      wall-clock jump against the monotonic clock), wall-clock rollback, `lock`, restart.
  I6  Fail closed when on: an unusable policy, an admin floor problem, K above the usable
      factors, or an out-of-band policy edit locks every scope (never "allow").
  I7  Unattended jobs get only allow-listed narrow scopes and never a prompt.
  I8  Every decision is audited: who (pid, executable), what scope, which grant, the verdict.
      Never a secret value, never a TOTP code.
"""
import argparse
import hashlib
import json
import os
import secrets
import signal
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gt_ipc                                    # noqa: E402
import gt_unlock_factors as F                    # noqa: E402
import gt_unlock_policy as P                     # noqa: E402

VERSION = "0.20.0"
IS_WINDOWS = os.name == "nt"
TICK_S = 0.5
SLEEP_JUMP_S = 30
ROLLBACK_S = 5
PROMPT_COOLDOWN_S = 10

# What a `claude` process looks like, for finding a session's root. The native installer runs
# ~/.local/share/claude/versions/<v> with argv[0] "claude" (its comm is the version number).
CLAUDE_NAMES = ("claude", "claude.exe")


def _is_claude(info, names=CLAUDE_NAMES):
    path = gt_ipc.process_path(info["pid"]).replace("\\", "/")
    args = gt_ipc.process_args(info["pid"])
    base = os.path.basename(path).lower()
    a0 = os.path.basename(args[0]).lower() if args else ""
    if base in names or a0 in names or (info.get("comm") or "").lower() in names:
        return True
    if "/claude/versions/" in path.lower():
        return True
    return any("@anthropic-ai/claude-code" in a for a in args[:3])


def installed_plugins_file():
    return os.path.join(os.path.expanduser("~"), ".claude", "plugins", "installed_plugins.json")


def installed_plugin_dirs(name, plugins_file=None):
    """The install paths Claude Code records for plugin `name` (any marketplace), from
    ~/.claude/plugins/installed_plugins.json -- realpaths, never raises."""
    try:
        with open(plugins_file or installed_plugins_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    plugins = data.get("plugins") if isinstance(data, dict) else None
    out = []
    for key, entries in (plugins or {}).items():
        if not isinstance(key, str) or key.split("@", 1)[0] != name:
            continue
        for e in entries if isinstance(entries, list) else [entries]:
            p = e.get("installPath") if isinstance(e, dict) else None
            if isinstance(p, str) and os.path.isabs(p):
                out.append(os.path.realpath(p))
    return out


def runs_installed_script(peer, plugin, script, plugins_file=None):
    """Is `peer` a python process whose __main__ file IS `<installPath>/scripts/<script>` of an
    installed `plugin` -- compared by realpath, never by file name?

    INVARIANT (review F5, 2026-10-03): a look-alike -- /tmp/x/lotrd.py with a lotrlib/engine.py
    beside it -- passed the old file-name check and received the shim's credential. Now only
    the file Claude Code installed counts. Residual (documented, L1/L2): installed_plugins.json
    and the installed files are the user's own, so a same-user process can edit them or run the
    real file with its own arguments; L3 (admin-owned code) closes that.

    INVARIANT (review L1, 2026-10-04): the right FILE is not enough -- PYTHONPATH pointing at a
    sitecustomize.py ran arbitrary code inside the real lotr_mcp.py before its first line. The
    interpreter must also have been started isolated (python -I; gt_ipc.isolation_problem),
    which is how gt's own manifests start it."""
    pid = (peer or {}).get("pid")
    main = gt_ipc.main_script(pid)
    if not main:
        return False
    real = os.path.realpath(main)
    if not any(real == os.path.realpath(os.path.join(d, "scripts", script))
               for d in installed_plugin_dirs(plugin, plugins_file)):
        return False
    return gt_ipc.isolation_problem(pid) is None


def is_lotr_daemon(peer, plugins_file=None):
    """Is `peer` the INSTALLED gt-lotr's lotrd.py? The one process allowed to resolve a secret,
    or ask for consent, ON BEHALF of another (the MCP shim it serves): it uses the value for the
    downstream call and never returns it to a client (gt-lotr ADR-3)."""
    return runs_installed_script(peer, "gt-lotr", "lotrd.py", plugins_file)


def is_lotr_shim(peer, plugins_file=None):
    """Is `peer` the INSTALLED gt-lotr's lotr_mcp.py -- the only file that may take a session's
    shim seat (review F7)?"""
    return runs_installed_script(peer, "gt-lotr", "lotr_mcp.py", plugins_file)


def is_vault_shim(peer, plugins_file=None):
    """Is `peer` the INSTALLED gt plugin's gt_vault_mcp.py -- the only file that may take a
    session's VAULT seat (gt sandbox mode, 0.20.0)? Same realpath rule as is_lotr_shim."""
    return runs_installed_script(peer, "gt", "gt_vault_mcp.py", plugins_file)


# gt sandbox mode (0.20.0): scopes served, under door mcp_only, only to the session's registered
# vault MCP server -- never to a process from the session's shell, nor to LOTR's shim.
VAULT_PREFIX = "gt:vault:"

# Grants are held PER SEAT (review L4, 2026-10-04): the vault MCP server's unlock (a prompt
# that says "gt vault MCP") must not unlock LOTR's shim or the session's shell for lotr:*, and
# the other way round. Seat "vault" = the session's registered gt vault MCP server; seat
# "main" = everything else in the session (LOTR's shim, the shell, a terminal).
SEATS = ("main", "vault")


def seat_of(kind):
    return "vault" if kind == "vault_shim" else "main"


def _sha(data):
    return hashlib.sha256(data).hexdigest()


# M12 (usability run 2026-10-04): the files whose code the authority runs. An install, upgrade
# or rollback that changes any of them leaves a running authority on OLD code; code_status
# reports that, and stop_if_stale ends it (gt_unlock.py daemon restart-if-stale, run by
# install.sh).
CODE_GLOBS = ("gt_unlock*.py", "gt_ipc.py", "gt_unlock_hello.ps1")


def code_fingerprint(d=None):
    """sha256 over the names and contents of the authority's code files in `d` (default: the
    directory this module runs from). A file added, removed or changed changes it."""
    import glob
    d = d or HERE
    names = sorted({os.path.basename(p) for g in CODE_GLOBS
                    for p in glob.glob(os.path.join(d, g))})
    h = hashlib.sha256()
    for n in names:
        try:
            with open(os.path.join(d, n), "rb") as f:
                data = f.read()
        except OSError:
            data = b"\0unreadable"
        h.update(n.encode("utf-8") + b"\0" + hashlib.sha256(data).digest())
    return h.hexdigest()


class Session:
    def __init__(self, root, kind, session_id=None, job=None):
        self.root = root                  # {"pid", "start"}
        self.kind = kind                  # claude | terminal | job
        self.session_id = session_id
        self.job = job
        self.shim = None                  # {"pid", "start"} -- one live shim per session
        self.had_shim = False             # a registered shim exited; the next one starts bare
        self.vault_shim = None            # the session's gt vault MCP server (0.20.0), same rules
        self.had_vault_shim = False
        self.created = time.time()

    @property
    def key(self):
        return (self.root["pid"], self.root["start"])

    def gkey(self, seat="main"):
        """The grant table key for one of this session's seats (L4)."""
        return (self.root["pid"], self.root["start"], seat)


class Grant:
    def __init__(self, session_key, factors, ttl, idle):
        self.gid = secrets.token_hex(16)  # 128 bits; an audit handle only (I3)
        self.session_key = session_key
        self.factors = list(factors)
        self.created = time.monotonic()
        self.created_wall = time.time()
        self.last_use = self.created
        self.step_up_at = self.created if any(f in ("touchid", "hello") for f in factors) \
            else None
        self.consent_until = 0.0
        self.ttl = ttl
        self.idle = idle

    def expired(self, now):
        if now - self.created >= self.ttl:
            return "ttl"
        if now - self.last_use >= self.idle:
            return "idle"
        return None


class Denied(Exception):
    def __init__(self, code, message, hints=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hints = hints or []


class Authority:
    """The authority's state and decisions, independent of the transport (tests drive it
    directly or over a real socket)."""

    def __init__(self, home, *, factors=None, admin_paths=None, trusted_uids=(0,),
                 claude_names=CLAUDE_NAMES, is_claude=None, screen_locked=None, clock=None,
                 consumer_ok=None, shim_ok=None, vault_shim_ok=None, interactive=None):
        self.home = home
        os.makedirs(home, mode=0o700, exist_ok=True)
        if not IS_WINDOWS:
            os.chmod(home, 0o700)
        self.factors = factors if factors is not None else F.registry()
        # Factors that read policy outside a prove() (SSO's available()) read THIS authority's
        # policy, not whatever ~/.claude happens to hold.
        for fac in self.factors.values():
            if hasattr(fac, "_policy_loader") and getattr(fac, "_policy_loader") is None:
                fac._policy_loader = lambda: (self.eff.policy if self.eff is not None
                                              else P.default_policy())
        self.admin_paths = admin_paths
        self.trusted_uids = trusted_uids
        self.claude_names = tuple(claude_names)
        self.is_claude = is_claude or (lambda info: _is_claude(info, self.claude_names))
        self.consumer_ok = consumer_ok or is_lotr_daemon
        self.shim_ok = shim_ok or is_lotr_shim
        self.vault_shim_ok = vault_shim_ok or is_vault_shim
        # H2: is a "terminal" peer a person's terminal, or an orphan? (tests may stand in)
        self._interactive = interactive or (lambda ident: gt_ipc.interactive_chain(
            gt_ipc.ancestry(ident["pid"])))
        self._screen_locked = screen_locked or screen_locked_now
        self._clock = clock or (lambda: (time.time(), time.monotonic()))
        self.lock = threading.RLock()
        self.prompt_lock = threading.Lock()
        self.sessions = {}                # key -> Session
        self.grants = {}                  # session key -> Grant
        # (subject pid, subject start, grant id, name) -> (value, monotonic expiry). Only while
        # the policy's secrets_window_s > 0; per subject AND grant, never global (review F4);
        # cleared with every revocation.
        self.sealed_cache = {}
        self.cooldown = {}                # requester root key -> monotonic time
        self.started = time.time()
        self.code = code_fingerprint()     # M12: what this process loaded
        # The latest revocation, so `status` can say "revoked at 14:02: screen locked" instead
        # of the grant silently vanishing (usability run 2026-10-04). Never a secret.
        self.last_revoked = None
        w, m = self._clock()
        self._skew = w - m
        self._last_wall = w
        self._policy_stamp = None
        self.eff = None
        self._was_on = False              # unlock was ON at some point in this daemon's life
        self._off_by_policy = False       # ... and was turned off through policy_set
        # M3: the highest policy-approval serial accepted -- in memory for the daemon's life,
        # and persisted beside state.json so a restart keeps it ({"serial", "hash"}).
        self._floor = self._read_floor()
        self.reload()
        self.audit("daemon_start", verdict="ok", reason="grants start empty (I3)")

    # ------------------------------------------------------------------ files / state
    def path(self, name):
        return os.path.join(self.home, name)

    def enrolment(self):
        return F.read_json(self.path("enrolment.json"), {"factors": {}}) or {"factors": {}}

    def save_enrolment(self, data):
        F.write_json(self.path("enrolment.json"), data)

    def state(self):
        return F.read_json(self.path("state.json"), {}) or {}

    def save_state(self, st):
        F.write_json(self.path("state.json"), st)

    # ------------------------------------------------------------------ approval floor (M3)
    def _read_floor(self):
        f = F.read_json(self.path(FLOOR_FILE), {}) or {}
        try:
            return {"serial": max(0, int(f.get("serial") or 0)),
                    "hash": f.get("hash") if isinstance(f.get("hash"), str) else None}
        except (TypeError, ValueError, AttributeError):
            return {"serial": 0, "hash": None}

    def approval_floor(self):
        """The highest approval serial accepted so far: the larger of memory and disk (a
        rolled-back or deleted floor file never lowers what this daemon already saw)."""
        disk = self._read_floor()
        with self.lock:
            if disk["serial"] > self._floor["serial"]:
                self._floor = disk
            return dict(self._floor)

    def raise_floor(self, serial, h):
        with self.lock:
            if serial < self._floor["serial"]:
                return
            self._floor = {"serial": int(serial), "hash": h}
            floor = dict(self._floor)
        F.write_json(self.path(FLOOR_FILE), floor)

    def next_approval(self):
        """-> (serial, ts) for a new approval: one above the floor, stamped now."""
        return self.approval_floor()["serial"] + 1, int(time.time())

    def _policy_file_hash(self):
        try:
            with open(self.path("policy.json"), "rb") as f:
                return _sha(f.read())
        except OSError:
            return None

    def reload(self):
        """Re-read the policy when either file changed (cheap: stats, then a read)."""
        stamp = []
        # enrolment.json and state.json are stamped too: an enrolment changes K's usable count,
        # and state.json carries the policy approval.
        for p in [self.path("policy.json"), self.path("enrolment.json"),
                  self.path("state.json"), self.path(FLOOR_FILE)] \
                + list(self.admin_paths or P.admin_paths()):
            try:
                st = os.stat(p)
                stamp.append((p, st.st_mtime_ns, st.st_size))
            except OSError:
                stamp.append((p, None, None))
        stamp = tuple(stamp)
        if stamp == self._policy_stamp and self.eff is not None:
            return self.eff
        user, uprob = _read_user_at(self.path("policy.json"))
        admin, aprob = P.read_admin(self.admin_paths, self.trusted_uids)
        problems = [x for x in (uprob, aprob) if x]
        admin_exists = any(os.path.lexists(p) for p in (self.admin_paths or P.admin_paths())) \
            or (IS_WINDOWS and P._admin_registry() is not None)
        # An unreadable user policy counts as ON (I6): corrupting the file must not be a way
        # to switch unlock off.
        marker = bool(self.state().get("unlock_on")) or P.marker_present(self.home)
        enabled = bool((user or {}).get("enabled")) or admin_exists or bool(uprob) or marker
        # I6: unlock was on in this daemon's life and its files now say otherwise, without a
        # policy_set turning it off: that is a deletion behind the authority's back.
        vanished = self._was_on and not enabled and not self._off_by_policy
        if vanished:
            enabled = True
            problems.append("unlock was on and its policy files were removed or rewritten "
                            "outside gt_unlock.py; everything gated stays locked")
        try:
            merged = P.merge(user if not uprob else None, admin)
        except (P.PolicyError, TypeError, ValueError) as e:
            problems.append("policy invalid: %s" % e)
            merged = P.default_policy()
        eff = P.Effective(merged, problems, enabled)
        # I6: a user policy edited behind the authority's back (not through policy_set) is
        # not trusted until it is approved with a step-up.
        h = self._policy_file_hash()
        if eff.enabled and h is None and not admin_exists and not vanished:
            eff.problems.append("unlock is on (its marker is present) but policy.json is "
                                "missing; everything gated stays locked until a policy is set "
                                "with `gt_unlock.py policy`")
        elif eff.enabled and h is not None:
            why = self._approval_problem(h)
            if why:
                eff.problems.append(why)
        # I6: K above the usable factors locks; it is never lowered.
        if eff.enabled and not eff.problems:
            prob = self._k_problem(eff.policy)
            if prob:
                eff.problems.append(prob)
        self.eff = eff
        self._policy_stamp = stamp
        if eff.enabled:
            self._was_on = True
            self._off_by_policy = False
        return eff

    # ------------------------------------------------------------------ policy approval (I6)
    def approval_challenge(self, policy_hash, nonce, requester, factor, serial=None, ts=None):
        """The challenge a platform factor signs to approve policy bytes with sha256
        `policy_hash`: the same construction as every other factor challenge, with purpose
        "change" and scope approval_bind(), so the signature binds exactly those bytes AND,
        since review M3 (2026-10-04), the approval's serial and time."""
        return self._challenge(nonce, "change", "policy:" + approval_bind(policy_hash, serial,
                                                                          ts),
                               requester, factor)

    def _serial_problem(self, appr, h):
        """M3: -> None when the approval's serial is not below the floor (equal only for the
        very approval the floor recorded), else why. A serial-less approval (written before
        0.20.0's M3 fix) counts as serial 0: accepted until the first serial'd approval."""
        try:
            serial = int(appr.get("serial") or 0)
        except (TypeError, ValueError):
            return "the policy approval's serial is malformed"
        floor = self.approval_floor()
        if serial < floor["serial"] or (serial == floor["serial"] and floor["serial"] > 0
                                        and floor["hash"] != h):
            return ("the policy approval is older than one already accepted (serial %d, "
                    "highest seen %d): a restored or replayed approval is refused -- approve "
                    "the policy again with `gt_unlock.py policy approve`"
                    % (serial, floor["serial"]))
        return None

    def _accept_serial(self, appr, h):
        try:
            serial = int(appr.get("serial") or 0)
        except (TypeError, ValueError):
            return
        if serial > self.approval_floor()["serial"]:
            self.raise_floor(serial, h)

    def _approval_problem(self, h):
        """-> None when policy.json (sha256 `h`) carries a valid approval, else the reason.

        Review F3 (2026-10-03): the approval used to be a plain sha256 in state.json, which any
        same-user process could write. Where a platform factor (Touch ID / Hello) is enrolled,
        the approval is now that factor's signature over the policy hash, made during the
        step-up that approved it, and verified here under the enrolled public key. Without a
        platform factor (Linux: TOTP only) the hash alone is all there is -- L1, documented.
        Invalid or missing => the caller fails closed (the most restrictive policy, never
        "off")."""
        st = self.state()
        appr = st.get("policy_approval") if isinstance(st.get("policy_approval"), dict) else {}
        enrolled = self.enrolment().get("factors") or {}
        platform = [n for n in P.PLATFORM_FACTORS
                    if n in enrolled and hasattr(self.factors.get(n), "verify")]
        if not platform:
            if appr.get("hash") == h:
                why = self._serial_problem(appr, h)
                if why:
                    return why
                self._accept_serial(appr, h)
                return None
            if h == st.get("policy_approved") and self.approval_floor()["serial"] == 0:
                return None
            return ("policy.json changed outside `gt_unlock.py policy` -- approve it with "
                    "`gt_unlock.py policy approve` (needs your factors)")
        if appr.get("hash") != h:
            return ("policy.json changed outside `gt_unlock.py policy`, or its approval is not "
                    "signed -- approve it with `gt_unlock.py policy approve` (needs Touch ID / "
                    "Windows Hello)")
        name = appr.get("factor")
        if name not in platform:
            return "the policy approval is not signed by an enrolled platform factor"
        # A Secure Enclave key blob is not bound to the binary that made it (verified on this
        # Mac 2026-10-03 18:59): a same-user process holding a NON-biometric key's blob can sign
        # with it at will. Only a key whose use needs a finger / PIN may approve a policy; an
        # enrolment that says otherwise is refused (fail closed), never trusted.
        if (enrolled.get(name) or {}).get("biometric", True) is not True \
                and not getattr(self.factors.get(name), "_test", False):
            return ("the policy approval is signed by a %s key that needs no finger or PIN; "
                    "only a biometric key may approve -- re-enrol %s" % (name, name))
        import base64
        try:
            nonce = base64.b64decode(appr.get("nonce") or "", validate=True)
            sig = base64.b64decode(appr.get("sig") or "", validate=True)
        except (TypeError, ValueError):
            return "the policy approval is malformed"
        serial, ts = appr.get("serial"), appr.get("ts")
        if (serial is None) != (ts is None) or any(
                v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0)
                for v in (serial, ts)):
            return "the policy approval's serial / time is malformed"
        ch = self.approval_challenge(h, nonce, str(appr.get("requester") or ""), name,
                                     serial, ts)
        try:
            ok = bool(self.factors[name].verify(enrolled[name], ch, sig))
        except Exception:                                     # noqa: BLE001
            ok = False
        if not ok:
            return "the policy approval's %s signature does not verify" % name
        # M3: a valid signature is not enough -- an OLDER valid approval (a looser policy the
        # owner once approved, restored with its state.json) is a replay.
        why = self._serial_problem(appr, h)
        if why:
            return why
        self._accept_serial(appr, h)
        return None

    def usable_factors(self, policy=None):
        policy = policy or self.eff.policy
        enrolled = self.enrolment().get("factors") or {}
        allowed = set((policy.get("factors") or {}).get("allowed") or [])
        out = []
        for name in ("touchid", "hello", "sso", "totp"):
            if name in enrolled and name in allowed and name in self.factors:
                ok, _why = self.factors[name].available()
                if ok:
                    out.append(name)
        return out

    def _k_problem(self, policy):
        f = policy.get("factors") or {}
        k = int(f.get("required", 1))
        usable = self.usable_factors(policy)
        # M5 (usability run 2026-10-04): name the supported way out. A machine with only TOTP
        # (a Mac without a sensor or the Command Line Tools, a desktop Mac, a PC without Hello)
        # had no route but a hand-edited policy.
        choose = ("enrol more, or choose the factors on purpose: gt_unlock.py policy enable "
                  "--factors %s" % (",".join(usable) or "totp"))
        if len(usable) < k:
            return ("K = %d (how many factors must agree to unlock) but only %d factor(s) are "
                    "enrolled and usable here (%s); %s -- K is never lowered silently"
                    % (k, len(usable), ", ".join(usable) or "none", choose))
        one_of = f.get("require_one_of") or []
        if one_of and not set(one_of) & set(usable):
            return ("the policy requires one of %s in every unlock, and none is enrolled and "
                    "usable here; %s" % (", ".join(one_of), choose))
        return None

    # ------------------------------------------------------------------ audit (I8)
    def audit(self, event, **fields):
        """Append one line. -> True when it was written. A caller that must not act without a
        record (write / consent / secret verdicts) refuses on False (review, low)."""
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event}
        for k, v in fields.items():
            if v is not None and k not in ("value", "secret", "code", "token", "seed"):
                rec[k] = v
        line = (json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            fd = os.open(self.path("audit.jsonl"),
                         os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0),
                         0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
            return True
        except OSError:
            return False

    # ------------------------------------------------------------------ subjects
    def _ident(self, peer):
        return {"pid": peer["pid"], "start": peer["start"]}

    def describe(self, pid):
        """Who asks, in the words a prompt shows. Usability run 2026-10-04: an MCP shim or a
        hook of Claude Code read "Python (pid N)". Now a direct child of `claude` is "Claude
        Code session (pid <claude>)", a deeper descendant names both, and a python process
        names the script it runs. From the kernel's process table only (I1)."""
        def name(info_pid):
            main = gt_ipc.main_script(info_pid)
            if main:
                return os.path.basename(main)
            return os.path.basename(gt_ipc.process_path(info_pid)) or "?"
        try:
            chain = gt_ipc.ancestry(pid, limit=8)
        except Exception:                                     # noqa: BLE001
            chain = []
        for i, info in enumerate(chain):
            if self.is_claude(info):
                if i <= 1:
                    return "Claude Code session (pid %s)" % info["pid"]
                return "%s (pid %s), a command in Claude Code session (pid %s)" % (
                    name(pid), pid, info["pid"])
        return "%s (pid %s)" % (name(pid), pid)

    def classify(self, peer, job=None):
        """-> (kind, session_root_ident, shim_session_or_None). kind: shim | vault_shim |
        claude | terminal | job. The ancestry walk uses the kernel's parent links (gt_ipc)."""
        chain = gt_ipc.ancestry(peer["pid"])
        if not chain or chain[0]["start"] != peer["start"]:
            raise Denied("peer_gone", "the requesting process could not be identified")
        with self.lock:
            for s in self.sessions.values():
                if s.shim and s.shim["pid"] == peer["pid"] and s.shim["start"] == peer["start"]:
                    return "shim", s.root, s
                v = s.vault_shim
                if v and v["pid"] == peer["pid"] and v["start"] == peer["start"]:
                    return "vault_shim", s.root, s
        for info in chain[1:]:
            if self.is_claude(info):
                return "claude", {"pid": info["pid"], "start": info["start"]}, None
        if job:
            return "job", None, None
        if len(chain) > 1:
            return "terminal", {"pid": chain[1]["pid"], "start": chain[1]["start"]}, None
        return "terminal", {"pid": chain[0]["pid"], "start": chain[0]["start"]}, None

    def interactive(self, ident):
        try:
            return bool(self._interactive(ident))
        except Exception:                                     # noqa: BLE001 - fail closed
            return False

    def session_for(self, root, kind, session_id=None):
        key = (root["pid"], root["start"])
        with self.lock:
            s = self.sessions.get(key)
            if s is None:
                s = Session(root, kind, session_id)
                self.sessions[key] = s
            elif session_id and not s.session_id:
                s.session_id = session_id
            return s

    def active_grant(self, key):
        with self.lock:
            g = self.grants.get(key)
            if g is None:
                return None
            why = g.expired(time.monotonic())
            if why:
                self.revoke_key(key, why)
                return None
            return g

    # ------------------------------------------------------------------ revocation (I5)
    def revoke_key(self, key, why):
        """Revoke one grant (a 3-tuple seat key) or EVERY seat's grant of a session (its
        2-tuple session key). -> a revoked Grant, or None."""
        with self.lock:
            keys = [key] if len(key) == 3 else [k for k in self.grants if k[:2] == tuple(key)]
            gone = [g for g in (self.grants.pop(k, None) for k in keys) if g is not None]
            self.sealed_cache.clear()          # every revocation empties it (sealed I2)
        for g in gone:
            self.audit("revoke", grant=g.gid, reason=why, session=key[0])
        if gone:
            self.last_revoked = {"at": time.time(), "why": why, "grants": len(gone)}
        return gone[0] if gone else None

    def revoke_all(self, why):
        with self.lock:
            keys = list(self.grants)
        for k in keys:
            self.revoke_key(k, why)
        with self.lock:
            self.sealed_cache.clear()
        return len(keys)

    def tick(self):
        """One pass of every time- and process-based revocation trigger."""
        wall, mono = self._clock()
        lock_on = set(((self.eff.policy if self.eff else {}).get("grant") or {})
                      .get("lock_on") or P.LOCK_ON)
        skew = wall - mono
        if wall < self._last_wall - ROLLBACK_S:
            self.revoke_all("clock_rollback")          # always, whatever lock_on says
        elif skew - self._skew > SLEEP_JUMP_S and "sleep" in lock_on:
            self.revoke_all("sleep")
        self._skew = skew
        self._last_wall = wall
        if self.grants and "screen_lock" in lock_on:
            try:
                if self._screen_locked():
                    self.revoke_all("screen_lock")
            except Exception:                           # noqa: BLE001
                pass
        with self.lock:
            sessions = list(self.sessions.values())
        for s in sessions:
            if s.shim and not gt_ipc.alive(s.shim["pid"], s.shim["start"]):
                s.shim = None
                s.had_shim = True
                if "shim_exit" in lock_on:
                    self.revoke_key(s.gkey("main"), "shim_exit")
            if s.vault_shim and not gt_ipc.alive(s.vault_shim["pid"], s.vault_shim["start"]):
                s.vault_shim = None
                s.had_vault_shim = True
                if "shim_exit" in lock_on:
                    self.revoke_key(s.gkey("vault"), "vault_shim_exit")
            if s.kind != "job" and not gt_ipc.alive(s.root["pid"], s.root["start"]):
                self.revoke_key(s.key, "session_root_exit")
                with self.lock:
                    self.sessions.pop(s.key, None)
        with self.lock:
            keys = list(self.grants)
        for k in keys:
            self.active_grant(k)

    # ------------------------------------------------------------------ factor collection
    def _challenge(self, nonce, purpose, scope, requester, factor):
        blob = json.dumps({"v": 1, "purpose": purpose, "scope": scope, "requester": requester,
                           "factor": factor}, sort_keys=True).encode("utf-8")
        return hashlib.sha256(b"gt-unlock-challenge\0" + nonce + b"\0" + blob).digest()

    def collect(self, ctx, purpose, scope, requester, *, k=None, need_platform=False,
                use_recovery=False, evidence=None):
        """Run K factors (I2). -> list of factor names that passed. Raises Denied.
        `evidence` (a dict) receives the nonce and each platform factor's signature, for an
        approval that must be verifiable later (policy approval, F3)."""
        pol = self.eff.policy
        f = pol.get("factors") or {}
        k = int(k if k is not None else f.get("required", 1))
        usable = self.usable_factors()
        enrolled = self.enrolment().get("factors") or {}
        plan = []
        if use_recovery:
            if F.RecoveryFactor().remaining(self.home) <= 0:
                raise Denied("no_recovery", "no unused recovery codes")
            plan.append("recovery")
        platform_usable = [x for x in usable if x in P.PLATFORM_FACTORS]
        one_of = [x for x in (f.get("require_one_of") or []) if x in usable]
        if need_platform:
            if not platform_usable:
                raise Denied("no_platform_factor", "this needs Touch ID or Windows Hello, and "
                             "neither is enrolled and usable")
            plan.append(platform_usable[0])
        elif one_of and not use_recovery:
            plan.append(one_of[0])
        for name in usable:
            if len(plan) >= k:
                break
            if name not in plan:
                plan.append(name)
        if len(plan) < k:
            raise Denied("factors_unavailable", "%d factor(s) required, %d usable" %
                         (k, len(set(plan))))
        nonce = secrets.token_bytes(32)
        done = []
        if evidence is not None:
            evidence["nonce"], evidence["requester"], evidence["sigs"] = nonce, requester, {}
        for name in plan:
            fac = self.factors.get(name) if name != "recovery" else F.RecoveryFactor()
            ch = self._challenge(nonce, purpose, scope, requester, name)
            try:
                proof = fac.prove(enrolled.get(name) or {}, ch, ctx)
                if evidence is not None and name in P.PLATFORM_FACTORS \
                        and isinstance(proof, (bytes, bytearray)):
                    evidence["sigs"][name] = bytes(proof)
            except F.FactorError as e:
                self.save_state(ctx.state)
                self.audit("factor_failed", factor=name, scope=scope, requester=requester,
                           reason=e.code)
                raise Denied("factor_" + e.code, "%s: %s" % (name, e.message))
            done.append(name)
        self.save_state(ctx.state)
        return done

    def _ctx(self, reason, conn, rid, tty):
        def ask(need):
            if conn is None:
                return None
            try:
                return conn.ask(rid, need)
            except (gt_ipc.IpcError, OSError, ValueError):
                return None
        return F.Context(self.home, reason, ask_client=ask, state=self.state(),
                         policy=self.eff.policy, tty=bool(tty))

    def _prompting(self, root_key):
        """One prompt flow at a time, and a short cooldown after a refusal, so a process
        cannot carpet the screen with prompts hoping one is approved by reflex."""
        last = self.cooldown.get(root_key)
        if last and time.monotonic() - last < PROMPT_COOLDOWN_S:
            raise Denied("cooldown", "a recent request was refused; wait %d s before asking "
                         "again" % max(1, int(PROMPT_COOLDOWN_S - (time.monotonic() - last))
                                       + 1))
        if not self.prompt_lock.acquire(timeout=0.1):
            raise Denied("busy", "another unlock prompt is open")
        return self.prompt_lock

    def do_unlock(self, session, requester, scope, reason, conn, rid, tty, use_recovery=False,
                  seat="main"):
        """Collect K factors and grant `session`'s `seat` (L4)."""
        text = prompt_text("%s asks to unlock gt%s." % (requester, " for %s" % scope
                                                          if scope else ""), reason)
        lk = self._prompting(session.key)
        try:
            ctx = self._ctx(text, conn, rid, tty)
            try:
                used = self.collect(ctx, "unlock", scope, requester, use_recovery=use_recovery)
            except Denied:
                self.cooldown[session.key] = time.monotonic()
                raise
        finally:
            lk.release()
        g = self.grant_for(session, used, seat)
        if ctx.state.get("needs_reenrol"):
            self.audit("recovery_used", grant=g.gid, reason="re-enrolment required")
        return g

    def grant_for(self, session, used, seat="main"):
        gp = self.eff.policy.get("grant") or {}
        g = Grant(session.gkey(seat), used, int(gp.get("ttl_s", 28800)),
                  int(gp.get("idle_s", 900)))
        with self.lock:
            self.grants[session.gkey(seat)] = g
        self.audit("grant", grant=g.gid, factors=",".join(used), session=session.root["pid"],
                   kind=session.kind, seat=seat)
        return g

    def satisfies_step_up(self, factors):
        """Would `factors`, collected just now, be what step_up() asks for? The platform
        factor where one is usable; otherwise any factor but a recovery code."""
        plat = [x for x in self.usable_factors() if x in P.PLATFORM_FACTORS]
        if plat:
            return any(f in P.PLATFORM_FACTORS for f in factors)
        return any(f != "recovery" for f in factors)

    def step_up(self, session, g, requester, scope, reason, conn, rid, tty):
        fresh = int((self.eff.policy.get("step_up") or {}).get("fresh_s", 60))
        now = time.monotonic()
        if g.step_up_at is not None and now - g.step_up_at <= fresh:
            return g
        usable = self.usable_factors()
        plat = [x for x in usable if x in P.PLATFORM_FACTORS]
        text = prompt_text("%s asks for a fresh confirmation for %s." % (requester, scope),
                           reason)
        lk = self._prompting(session.key)
        try:
            ctx = self._ctx(text, conn, rid, tty)
            # step-up = one fresh PLATFORM factor where one is enrolled; TOTP otherwise
            # (Linux), which the status output labels as the weaker step-up it is.
            used = self.collect(ctx, "step_up", scope, requester, k=1,
                                need_platform=bool(plat))
        finally:
            lk.release()
        g.step_up_at = time.monotonic()
        self.audit("step_up", grant=g.gid, factors=",".join(used), scope=scope)
        return g

    # ------------------------------------------------------------------ the decision
    def evaluate(self, peer, scope, *, subject=None, request=False, reason="", conn=None,
                 rid=None, tty=False, job=None):
        """-> {"allowed": bool, "code", "message", "grant", "level"}. Never raises."""
        eff = self.reload()
        target = subject or peer
        try:
            if not eff.enabled:
                return self._verdict(True, "disabled", "unlock is off", None, "off", scope,
                                     target)
            if eff.failed_closed:                                    # I6
                return self._verdict(False, "failed_closed", "; ".join(eff.problems), None,
                                     "deny", scope, target)
            level = eff.level(scope)
            if level == "deny":
                return self._verdict(False, "denied", "policy denies %s" % scope, None, level,
                                     scope, target)
            kind, root, shim_session = self.classify(target, job=job)
            if kind == "job":                                        # I7
                allowed = {(e.get("job"), e.get("scope"))
                           for e in (eff.policy.get("unattended") or {}).get("allowed") or []}
                ok = (job, scope) in allowed and P.unattended_scope_ok(scope)
                return self._verdict(ok, "unattended" if ok else "not_allowed_unattended",
                                     "job %s %s %s" % (job, "may use" if ok else "may not use",
                                                       scope), None, level, scope, target,
                                     must_audit=_must_audit(scope))
            # I4: the door. Under mcp_only only the registered shim gets lotr:* -- and, since
            # the review's F4, the broker's scopes too: a secret is bound to the exact
            # requesting process, exactly like lotr:*.
            if (scope.startswith("lotr:") or scope in BROKER_SCOPES) \
                    and eff.policy.get("door") == "mcp_only" and kind in ("claude",
                                                                          "vault_shim"):
                return self._verdict(False, "mcp_only", "%s is served only to the "
                                     "registered MCP shim (door: mcp_only); a process started "
                                     "from the session's shell is refused"
                                     % ("lotr" if scope.startswith("lotr:") else scope),
                                     None, level, scope, target)
            # gt sandbox mode (0.20.0): the vault scopes, the same way -- only the session's
            # registered gt vault MCP server, never Bash and never LOTR's shim.
            if scope.startswith(VAULT_PREFIX) and eff.policy.get("door") == "mcp_only" \
                    and kind in ("claude", "shim"):
                return self._verdict(False, "mcp_only", "%s is served only to the session's "
                                     "registered gt vault MCP server (door: mcp_only); a "
                                     "process started from the session's shell is refused"
                                     % scope, None, level, scope, target)
            # F6 + H2: read_without_unlock is for a person at a terminal. A "terminal" peer
            # with no controlling terminal (a double-forked orphan, a daemon) -- or an ORPHAN
            # that gave itself one (setsid + openpty + TIOCSCTTY; review H2, 2026-10-04): no
            # login shell above it, its chain ending at init/launchd -- gets the level the
            # scope has WITHOUT that convenience.
            if level == "open" and kind == "terminal" \
                    and scope.startswith(("lotr:", VAULT_PREFIX)) \
                    and not (_has_tty(target) and self.interactive(target)):
                raw = P.Effective(dict(eff.policy, read_without_unlock=False), [],
                                  True).level(scope)
                if raw != "open":
                    level = raw
            if level == "open":
                return self._verdict(True, "open", "open scope", None, level, scope, target)
            session = shim_session or self.session_for(root, kind)
            seat = seat_of(kind)
            g = self.active_grant(session.gkey(seat))
            requester = self.describe(peer["pid"]) if request else None
            if g is None:
                if not request:
                    return self._verdict(False, "locked", "gt is locked; unlock first", None,
                                         level, scope, target,
                                         hints=["gt_unlock.py unlock", "or call again and "
                                                "approve the prompt"])
                g = self.do_unlock(session, requester, scope, reason, conn, rid, tty,
                                   seat=seat)
                # M6 (usability run 2026-10-04): the factors just collected FOR THIS VERY
                # REQUEST are its fresh confirmation, when they are what a step-up would ask
                # for (the platform factor where one is usable; else any non-recovery factor).
                # Asking again made TOTP-only machines type two codes from two 30 s windows
                # for one command. A grant made earlier still steps up as before.
                if g.step_up_at is None and self.satisfies_step_up(g.factors):
                    g.step_up_at = g.created
            if level == "step_up":
                if not request:
                    fresh = int((eff.policy.get("step_up") or {}).get("fresh_s", 60))
                    if g.step_up_at is None or time.monotonic() - g.step_up_at > fresh:
                        return self._verdict(False, "step_up", "%s needs a fresh "
                                             "confirmation" % scope, g, level, scope, target)
                else:
                    g = self.step_up(session, g, requester, scope, reason, conn, rid, tty)
            g.last_use = time.monotonic()
            return self._verdict(True, "granted", "granted", g, level, scope, target,
                                 must_audit=_must_audit(scope))
        except Denied as e:
            return self._verdict(False, e.code, e.message, None, "?", scope, target,
                                 hints=e.hints)

    def _verdict(self, ok, code, message, g, level, scope, target, hints=None,
                 must_audit=False):
        if code != "disabled":
            wrote = self.audit("check", scope=scope, verdict="allow" if ok else "deny",
                               reason=code, grant=g.gid if g else None,
                               subject=target.get("pid"),
                               exe=os.path.basename(gt_ipc.process_path(target.get("pid") or 0)))
            if ok and must_audit and not wrote:
                # Never act without the record (review, low): a write / consent / secret
                # allow that cannot be audited is refused.
                return {"allowed": False, "code": "audit_failed",
                        "message": "the audit log could not be written, so %s is refused"
                        % scope, "grant": None, "level": level,
                        "hints": ["check %s" % self.path("audit.jsonl")]}
        return {"allowed": bool(ok), "code": code, "message": message,
                "grant": g.gid if g else None, "level": level, "hints": hints or []}

    # ------------------------------------------------------------------ status / level
    def assurance(self):
        """The level this machine actually runs at -- what doctor and status print."""
        eff = self.reload()
        if not eff.enabled:
            return {"level": "off", "why": "unlock is off (the default)"}
        if eff.failed_closed:
            return {"level": "locked", "why": "; ".join(eff.problems)}
        f = eff.policy.get("factors") or {}
        plat_required = set(f.get("require_one_of") or []) & set(P.PLATFORM_FACTORS)
        plat_usable = [x for x in self.usable_factors() if x in P.PLATFORM_FACTORS]
        if plat_required and plat_usable:
            return {"level": "L2", "why": "every unlock needs %s (a hardware key that a "
                    "process cannot use without your finger or PIN); sealed: refs are "
                    "encrypted under it" % plat_usable[0]}
        return {"level": "L1", "why": "no platform factor is required in every unlock: this "
                "stops agents and accidents, not malware running as you"}

    def status(self):
        eff = self.reload()
        enr = self.enrolment().get("factors") or {}
        avail = {}
        for name, fac in self.factors.items():
            ok, why = fac.available()
            avail[name] = {"enrolled": name in enr, "available": ok, "why": why}
        avail["recovery"] = {"enrolled": F.RecoveryFactor().remaining(self.home) > 0,
                             "available": True,
                             "why": "%d unused code(s)" % F.RecoveryFactor().remaining(self.home)}
        with self.lock:
            sess = [{"root": s.root["pid"], "kind": s.kind, "shim": bool(s.shim),
                     "vault_shim": bool(s.vault_shim),
                     "grant": (self.grants[s.gkey("main")].gid
                               if s.gkey("main") in self.grants else None),
                     "vault_grant": (self.grants[s.gkey("vault")].gid
                                     if s.gkey("vault") in self.grants else None)}
                    for s in self.sessions.values()]
        import gt_unlock_seal as S
        return {"version": VERSION, "enabled": eff.enabled, "problems": eff.problems,
                "assurance": self.assurance(), "factors": avail,
                "required": (eff.policy.get("factors") or {}).get("required"),
                "require_one_of": (eff.policy.get("factors") or {}).get("require_one_of"),
                "door": eff.policy.get("door"), "sessions": sess,
                "grants": len(self.grants), "sealed": S.names(self.home),
                "admin_floor": any(os.path.lexists(p) for p in
                                   (self.admin_paths or P.admin_paths())),
                "needs_reenrol": bool(self.state().get("needs_reenrol")),
                "factors_chosen": (eff.policy.get("factors") or {}).get("chosen"),
                "last_revoked": self.last_revoked,
                "pid": os.getpid(), "since": self.started}

    def code_status(self):
        """M12: is this process running the code now on disk? -> dict (never raises)."""
        now = code_fingerprint()
        return {"pid": os.getpid(), "since": self.started, "version": VERSION,
                "code": self.code, "code_now": now, "stale": now != self.code}


BROKER_SCOPES = ("gt:secrets",)
# M3: the highest policy-approval serial this authority has accepted.
FLOOR_FILE = "approval-floor.json"


def approval_bind(policy_hash, serial=None, ts=None):
    """What a policy approval signs, after "policy:": the policy's sha256, then -- since
    review M3 -- the approval's monotonic serial and its Unix time. A serial-less approval
    (before M3) binds the hash alone."""
    if serial is None:
        return policy_hash
    return "%s:serial=%d:ts=%d" % (policy_hash, int(serial), int(ts or 0))


def prompt_text(head, claim=None):
    """Every prompt the authority raises is composed HERE: `head` names the requester (from
    the kernel) and the scope. A caller's own `reason` is shown only as a quoted, single-line,
    length-capped claim, labelled as unverified -- it can never write the prompt or pose as
    the authority's own words. Residual (documented): a same-user process can still race a
    prompt of its own, or drive the Touch ID helper with its own text."""
    head = head.strip()
    claim = " ".join(str(claim or "").split())
    claim = "".join(ch for ch in claim if ch.isprintable())[:160]
    if not claim:
        return head
    return '%s\nIt says (unverified): "%s"' % (head, claim.replace('"', "'"))


def _must_audit(scope):
    """Scopes whose ALLOW must be on record before anything acts on it."""
    return scope.endswith((":write", ":consent")) or scope in BROKER_SCOPES \
        or scope.startswith(("secret:", "gt:"))


def _has_tty(ident):
    info = gt_ipc.process_info((ident or {}).get("pid"))
    if not info or info.get("start") != (ident or {}).get("start"):
        return False
    # None = the platform cannot say (Windows): treated as a terminal, documented.
    return info.get("tty") is not False


def _read_user_at(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as e:
        return None, "user policy unreadable: %s" % e
    if not isinstance(raw, dict):
        return None, "user policy is not a JSON object"
    return raw, None


# ============================================================================ screen lock

def screen_locked_now():
    """True when the console session is locked. Unknown counts as NOT locked (the TTL and idle
    limits still apply) -- a probe that cannot run must not lock the user out constantly."""
    if sys.platform == "darwin":
        return _mac_screen_locked()
    if IS_WINDOWS:
        return _win_screen_locked()
    return False


_cg = None


def _mac_screen_locked():
    global _cg
    import ctypes
    import ctypes.util
    if _cg is None:
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cg.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p
        cf.CFDictionaryGetValue.restype = ctypes.c_void_p
        cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFBooleanGetValue.restype = ctypes.c_bool
        cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        key = cf.CFStringCreateWithCString(None, b"CGSSessionScreenIsLocked", 0x08000100)
        _cg = (cg, cf, key)
    cg, cf, key = _cg
    d = cg.CGSessionCopyCurrentDictionary()
    if not d:
        return False
    try:
        v = cf.CFDictionaryGetValue(d, key)
        return bool(v) and cf.CFBooleanGetValue(v)
    finally:
        cf.CFRelease(d)


def _win_screen_locked():
    import ctypes
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.OpenInputDesktop.restype = ctypes.c_void_p
    h = u32.OpenInputDesktop(0, False, 0x0100)          # DESKTOP_SWITCHDESKTOP
    if not h:
        # No access to the input desktop: the session is locked (or this is not the
        # console). Over SSH there is never an input desktop, so only report "locked" when a
        # desktop existed earlier in this process's life.
        return _win_had_desktop[0]
    _win_had_desktop[0] = True
    u32.CloseDesktop.argtypes = [ctypes.c_void_p]
    u32.CloseDesktop(h)
    return False


_win_had_desktop = [False]


# ============================================================================ the server

class Server:
    """Method dispatch for one Authority over gt_ipc."""

    def __init__(self, auth):
        self.auth = auth
        import gt_unlockd_methods as M
        self.methods = M.methods(auth)

    def handle(self, conn):
        while True:
            try:
                req = conn.recv_obj()
            except (ValueError, gt_ipc.IpcError):
                conn.send_obj({"id": None, "error": {"code": "bad_request",
                                                     "message": "not a JSON object", "hints": []}})
                return
            if req is None:
                return
            rid = req.get("id")
            fn = self.methods.get(req.get("method"))
            params = req.get("params") if isinstance(req.get("params"), dict) else {}
            if fn is None:
                conn.send_obj({"id": rid, "error": {"code": "unknown_method",
                                                    "message": "no such method", "hints": []}})
                continue
            try:
                res = fn(conn.peer, params, conn=conn, rid=rid)
                conn.send_obj({"id": rid, "result": res})
            except Denied as e:
                conn.send_obj({"id": rid, "error": {"code": e.code, "message": e.message,
                                                    "hints": e.hints}})
            except Exception as e:                    # noqa: BLE001 - never crash the daemon
                conn.send_obj({"id": rid, "error": {"code": "internal",
                                                    "message": type(e).__name__, "hints": []}})


def run(home, stop_event=None, on_ready=None, **kw):
    auth = Authority(home, **kw)
    srv = Server(auth)
    stop = stop_event or threading.Event()
    auth.stop = stop

    def ticker():
        while not stop.wait(TICK_S):
            try:
                auth.tick()
            except Exception:                         # noqa: BLE001
                pass
    threading.Thread(target=ticker, daemon=True).start()
    addr = gt_ipc.default_address(home, "unlockd")
    gt_ipc.serve(addr, srv.handle, stop_event=stop, on_ready=on_ready)
    return auth


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_unlockd", description="gt unlock authority daemon")
    ap.add_argument("--home", help="unlock home (default ~/.claude/golden-thread/unlock)")
    ns = ap.parse_args(argv)
    home = os.path.abspath(ns.home or P.unlock_home())
    stop = threading.Event()

    def on_signal(signum, frame):
        stop.set()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        F.write_private(os.path.join(home, "unlockd.pid"), ("%d\n" % os.getpid()).encode())
        run(home, stop_event=stop)
    except gt_ipc.IpcError as e:
        sys.stderr.write(json.dumps({"ok": False, "error": e.to_dict()}) + "\n")
        return 1
    finally:
        try:
            os.unlink(os.path.join(home, "unlockd.pid"))
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    # Run as the IMPORTED module, not __main__: gt_unlockd_methods imports gt_unlockd, and two
    # copies of the module would mean two Denied classes -- every refusal would then escape
    # the server's `except Denied` as an "internal" error.
    import gt_unlockd
    sys.exit(gt_unlockd.main())
