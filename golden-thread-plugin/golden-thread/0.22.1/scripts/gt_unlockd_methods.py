#!/usr/bin/env python3
"""gt_unlockd_methods -- the authority's wire methods, one function each (0.20.1).

Kept apart from gt_unlockd.py so each method's guard can be read next to what it guards. Every
method receives the KERNEL-identified peer (gt_ipc) and never trusts an identity, a factor
result or an approval carried in `params` (gt_unlockd invariants I1, I2).

Guards, by method:
  register_shim       the caller runs the INSTALLED gt-lotr lotr_mcp.py (realpath; review F7)
                      and its PARENT is a `claude` process; one live shim per session (first
                      wins); a shim REPLACING an earlier one (dead or alive) never inherits the
                      session's grant -- the grant is revoked and the new shim unlocks again
  enroll / unenroll / recovery_new / policy_set / policy_approve
                      "step-up + K": K FRESH factors, ignoring any grant (an agent riding an
                      unlocked session cannot change what unlocking means), with the platform
                      factor among them where one is usable -- its signature over the policy
                      hash IS the policy approval (review F3). Bootstrap (nothing enrolled yet)
                      must start with the platform factor where one exists, because enrolling
                      it needs a physical touch.
  secret / seal_*     a grant for gt:secrets held by the REQUESTING process (under mcp_only: the
                      shim, or lotrd naming the shim; review F4), or for a job the allow-listed
                      secret:<ref>; a sealed value is opened with a fresh platform factor every
                      time, or once per secrets_window_s per subject and grant
  relax_session       a person at a terminal (no claude ancestor, a controlling tty, an interactive
                      chain) AND one fresh factor, for a session that already holds a grant; the flag
                      lives on that session's grants and ends with them (0.22.0). `relax_off` only
                      tightens, so it needs neither
  consent             only gt-lotr's installed lotrd may ask (review M1); the prompt text is
                      composed HERE from the op, never taken from the caller; then a live grant
                      and a platform signature over the op unless inside the consent window
  sign_code           ONE fresh platform factor (Touch ID / Windows Hello) whose signature covers a
                      challenge bound to the digest it was asked to sign (purpose `code-sign`); the
                      prompt names the requester and the first 16 hex of the digest; never for a
                      scheduled job (0.21.0, `paranoid.signed_manifest`)
  stop                a fresh factor while unlock is on (review F1); `lock` needs nothing --
                      it only tightens
  stop_if_stale       nothing, and it stops the authority ONLY when its code on disk changed
                      since it started (an install / upgrade / rollback, M12); it never grants:
                      every grant is revoked
  enroll totp with skip_platform
                      bootstrap without the platform factor only for a person's terminal, by
                      the kernel's process table (no claude ancestor, a tty, a login shell)
"""
import base64
import hashlib
import json
import os
import re
import secrets
import time

import gt_accept_preview as AP
import gt_ipc
import gt_unlock_factors as F
import gt_unlock_policy as P
from gt_unlockd import PROMPT_COOLDOWN_S as D_COOLDOWN
from gt_unlockd import Denied, _has_tty, _sha, approval_bind, seat_of


def methods(auth):
    m = {}

    def method(fn):
        m[fn.__name__] = fn
        return fn

    def tty(params):
        return bool(params.get("tty"))

    def requester(peer):
        return auth.describe(peer["pid"])

    def require_full(peer, purpose, conn, rid, params, bind=None, evidence=None):
        """step-up + K: K fresh factors of those enrolled (never fewer than one when any is
        enrolled), the platform factor among them when one is usable. Returns the factor names
        used, or [] for a permitted bootstrap. `bind` (a policy hash) makes the challenge the
        policy approval's, and `evidence` receives the platform signature over it (F3)."""
        auth.reload()
        usable = auth.usable_factors()
        if not usable:
            return []
        k = max(1, min(int((auth.eff.policy.get("factors") or {}).get("required", 1)),
                       len(usable)))
        plat = any(x in P.PLATFORM_FACTORS for x in usable)
        text = "%s asks to change gt unlock: %s" % (requester(peer), purpose)
        lk = auth._prompting(("full", peer["pid"]))
        try:
            ctx = auth._ctx(text, conn, rid, tty(params))
            return auth.collect(ctx, "change", ("policy:" + bind) if bind else purpose,
                                requester(peer), k=k, need_platform=plat,
                                use_recovery=bool(params.get("recovery")), evidence=evidence)
        finally:
            lk.release()

    def write_approval(h, used, evidence, serial, ts):
        """Record the approval of policy bytes with sha256 `h` in state.json: the platform
        signature when one was made (verified on every load, F3), else the hash (L1) -- both
        with the approval's serial and time (M3), and the serial becomes the floor no older
        approval may go below."""
        st = auth.state()
        st["policy_approved"] = h
        sigs = (evidence or {}).get("sigs") or {}
        name = next((n for n in P.PLATFORM_FACTORS if n in sigs), None)
        if name:
            st["policy_approval"] = {
                "hash": h, "factor": name, "requester": evidence["requester"],
                "nonce": base64.b64encode(evidence["nonce"]).decode("ascii"),
                "sig": base64.b64encode(sigs[name]).decode("ascii"),
                "serial": serial, "ts": ts}
        else:
            st["policy_approval"] = {"hash": h, "serial": serial, "ts": ts}
        auth.raise_floor(serial, h)
        return st

    def consumer_hint(peer):
        """The hint a not_a_consumer refusal carries: what is wrong with the asking process. A courtesy:
        it never raises, so it can never turn a refusal into an error."""
        hint = ("only gt-lotr's installed lotrd.py, started with python -I, may ask: "
                "python3 -I <gt-lotr install>/scripts/lotrd.py --zone <zone>")
        try:
            from gt_unlockd import lotr_daemon_problem
            diag = lotr_daemon_problem(peer)
        except Exception:
            diag = None
        return [hint if not diag else "%s -- diagnosis: %s" % (hint, diag)]

    def need_consumer(peer, what):
        if not auth.consumer_ok(peer):
            auth.audit(what, verdict="deny", reason="not_a_consumer", subject=peer.get("pid"))
            raise Denied("not_a_consumer", "only gt-lotr's installed daemon may ask for %s"
                         % ("a consent" if what == "consent" else "this"), hints=consumer_hint(peer))

    @method
    def ping(peer, params, conn=None, rid=None):
        eff = auth.reload()
        return {"pong": True, "version": auth_version(), "enabled": eff.enabled}

    @method
    def status(peer, params, conn=None, rid=None):
        return auth.status()

    @method
    def register_session(peer, params, conn=None, rid=None):
        kind, root, _s = auth.classify(peer)
        if kind not in ("claude", "terminal"):
            raise Denied("not_a_session", "only a session hook or a terminal can register")
        s = auth.session_for(root, kind, params.get("session_id"))
        auth.audit("register_session", session=root["pid"], kind=kind)
        return {"session": root["pid"], "kind": kind}

    @method
    def register_shim(peer, params, conn=None, rid=None):
        # role "lotr" (default): gt-lotr's lotr_mcp.py; role "vault" (gt sandbox mode, 0.20.1):
        # gt's own gt_vault_mcp.py. Each has its own seat, checked against its own installed
        # file, with the same rules: parent is claude, first wins, a replacement inherits no
        # grant.
        role = params.get("role") or "lotr"
        if role not in ("lotr", "vault"):
            raise Denied("bad_request", "register_shim role is lotr or vault")
        seat, had = ("shim", "had_shim") if role == "lotr" else ("vault_shim", "had_vault_shim")
        what = "gt-lotr lotr_mcp.py" if role == "lotr" else "gt gt_vault_mcp.py"
        chain = gt_ipc.ancestry(peer["pid"], limit=2)
        if len(chain) < 2 or chain[0]["start"] != peer["start"] \
                or not auth.is_claude(chain[1]):
            raise Denied("not_a_shim", "an MCP shim is started by claude itself; this process "
                         "was not")
        ok = auth.shim_ok(peer) if role == "lotr" else auth.vault_shim_ok(peer)
        if not ok:
            auth.audit("register_shim", verdict="deny", subject=peer["pid"],
                       reason="not_the_installed_shim", role=role)
            raise Denied("not_a_shim", "only the installed %s may take a session's %s seat"
                         % (what, "MCP shim" if role == "lotr" else "vault MCP"))
        root = {"pid": chain[1]["pid"], "start": chain[1]["start"]}
        s = auth.session_for(root, "claude", params.get("session_id"))
        me = {"pid": peer["pid"], "start": peer["start"]}
        with auth.lock:
            cur = getattr(s, seat)
            other = s.vault_shim if role == "lotr" else s.shim
            if other == me:
                raise Denied("seat_taken", "this process already holds the session's other seat")
            if cur and cur != me and gt_ipc.alive(cur["pid"], cur["start"]):
                auth.audit("register_shim", verdict="deny", subject=peer["pid"],
                           reason="seat_taken", session=root["pid"], role=role)
                raise Denied("seat_taken", "a shim is already registered for this session")
            replaced = (cur is not None and cur != me) or getattr(s, had)
            setattr(s, seat, me)
            setattr(s, had, False)
        if replaced:
            # F7: a new shim never inherits the grant an earlier one held -- its own seat's
            # (L4: the seats' grants are separate).
            auth.revoke_key(s.gkey("main" if role == "lotr" else "vault"),
                            "shim_replaced" if role == "lotr" else "vault_shim_replaced")
        auth.audit("register_shim", verdict="ok", subject=peer["pid"], session=root["pid"],
                   role=role)
        return {"session": root["pid"], "role": role}

    @method
    def revoke_session(peer, params, conn=None, rid=None):
        kind, root, s = auth.classify(peer)
        if root is None:
            return {"revoked": 0}
        key = (root["pid"], root["start"])
        g = auth.revoke_key(key, "session_end")
        with auth.lock:
            auth.sessions.pop(key, None)
        return {"revoked": 1 if g else 0}

    @method
    def unlock(peer, params, conn=None, rid=None):
        eff = auth.reload()
        if not eff.enabled:
            return {"enabled": False, "grant": None}
        if eff.failed_closed:
            raise Denied("failed_closed", "; ".join(eff.problems))
        kind, root, shim_s = auth.classify(peer, job=params.get("job"))
        if kind == "job":
            raise Denied("unattended", "a scheduled job cannot unlock; use the allow-list")
        target = shim_s
        seat = seat_of(kind)
        sp = params.get("session_pid")
        if sp is not None:
            with auth.lock:
                cands = [s for s in auth.sessions.values() if s.root["pid"] == int(sp)]
            if not cands:
                raise Denied("no_session", "no registered session with root pid %s" % sp)
            target = cands[0]
            # A terminal unlocking a named session (gt_unlock.py unlock --session N) unlocks
            # the seat its scope belongs to: gt:vault:* -> the vault MCP seat, else main (L4).
            seat = "vault" if str(params.get("scope") or "").startswith("gt:vault:") \
                else "main"
        if target is None:
            target = auth.session_for(root, kind)
        g = auth.do_unlock(target, requester(peer), params.get("scope") or "",
                           params.get("reason") or "", conn, rid, tty(params),
                           use_recovery=bool(params.get("recovery")), seat=seat)
        return {"grant": g.gid, "factors": g.factors, "ttl_s": g.ttl, "age_s": g.idle,
                "session": target.root["pid"], "seat": seat, "kind": kind,
                "needs_reenrol": bool(auth.state().get("needs_reenrol"))}

    @method
    def check(peer, params, conn=None, rid=None):
        scope = params.get("scope")
        if not isinstance(scope, str) or not scope or len(scope) > 200:
            raise Denied("bad_request", "check needs a scope")
        subject = params.get("subject")
        if subject is not None:
            # I1: a subject is a process the CONSUMER identified through its own kernel peer
            # lookup. It must be alive with exactly that start time, or it is not that process.
            if not isinstance(subject, dict) or not gt_ipc.alive(subject.get("pid"),
                                                                  subject.get("start")):
                return {"allowed": False, "code": "subject_gone", "message": "the subject "
                        "process is not running", "grant": None, "level": "?", "hints": []}
            subject = {"pid": int(subject["pid"]), "start": subject["start"]}
        return auth.evaluate(peer, scope, subject=subject, request=bool(params.get("request")),
                             reason=str(params.get("reason") or "")[:300], conn=conn, rid=rid,
                             tty=tty(params), job=params.get("job"))

    @method
    def presence(peer, params, conn=None, rid=None):
        need_consumer(peer, "presence")        # only gt-lotr's daemon may ask; it cannot be told
        r = auth.presence()
        auth.audit("presence", verdict="present" if r["active"] else "absent",
                   subject=peer.get("pid"))
        return r

    @method
    def lock(peer, params, conn=None, rid=None):
        n = auth.revoke_all("lock")
        return {"revoked": n}

    def terminal_only(peer, what):
        """-> the peer's classification when it is a person's terminal, else Denied. An agent (a
        Claude Code session, its shim, a scheduled job) cannot start a relax: that is the one thing
        this refusal is for."""
        kind, root, _shim = auth.classify(peer)
        if kind != "terminal" or not (_has_tty(peer) and auth.interactive(peer)):
            raise Denied("terminal_only", "%s needs a person at a terminal; an agent or a "
                         "process without one cannot do it" % what)
        return kind, root

    @method
    def relax_session(peer, params, conn=None, rid=None):
        """Stop asking for a fresh factor, for the rest of one session (0.22.0)."""
        eff = auth.reload()
        if not eff.enabled:
            return {"enabled": False, "relaxed": 0}
        if eff.failed_closed:
            raise Denied("failed_closed", "; ".join(eff.problems))
        if (eff.policy.get("relax") or {}).get("allow", True) is not True:
            raise Denied("relax_not_allowed", "the unlock policy does not allow relax")
        kind, root = terminal_only(peer, "relaxing the fresh-confirmation checks")
        sp = params.get("session_pid")
        if sp is None:
            target = auth.session_for(root, kind)
        else:
            try:
                sp = int(sp)
            except (TypeError, ValueError):
                raise Denied("bad_request", "session_pid must be a process id")
            with auth.lock:
                cands = [x for x in auth.sessions.values() if x.root["pid"] == sp]
            if not cands:
                raise Denied("no_session", "no registered session with root pid %s" % sp)
            target = cands[0]
        key = tuple(target.gkey("main")[:2])
        now = time.monotonic()
        with auth.lock:
            live = [g for k, g in auth.grants.items() if k[:2] == key and g.expired(now) is None]
        if not live:
            raise Denied("locked", "that session holds no grant: unlock it first "
                         "(gt_unlock.py unlock --session %s)" % target.root["pid"])
        used = auth.fresh_confirmation(target, requester(peer), "relax for this session", conn,
                                       rid, tty(params))
        with auth.lock:
            live = [g for k, g in auth.grants.items() if k[:2] == key and g.expired(now) is None]
            for g in live:
                # The relax factor is NOT a step-up: counting it as one (step_up_at = now) opened a
                # fresh_s window on every step_up scope, the never-relaxable ones too (review F1).
                g.relaxed = True
        auth.audit("relax", verdict="session", factors=",".join(used),
                   session=target.root["pid"], grants=",".join(g.gid for g in live))
        return {"relaxed": len(live), "session": target.root["pid"], "factors": used}

    @method
    def relax_off(peer, params, conn=None, rid=None):
        """Turn relax off (0.22.0). It only tightens, so it needs no factor and no terminal: every
        grant's session relax is cleared, and the standing relax is switched off by a marker beside
        the signed policy (a platform-factor approval covers policy.json itself)."""
        with auth.lock:
            n = 0
            for g in auth.grants.values():
                if g.relaxed:
                    g.relaxed = False
                    n += 1
            # Review F3: the marker is unsigned, so the authority latches it: deleting the file
            # later does not bring the standing relax back (only policy_set's re-enable does).
            auth._relax_latched = True
        marked = P.set_relax_marker(auth.home, True)
        auth.reload()
        auth.audit("relax", verdict="off", cleared=n, marker=bool(marked))
        return {"cleared": n, "standing": False}

    @method
    def relax_status(peer, params, conn=None, rid=None):
        eff = auth.reload()
        rx = eff.policy.get("relax") or {}
        with auth.lock:
            n = sum(1 for g in auth.grants.values() if g.relaxed)
        return {"enabled": bool(eff.enabled), "standing": bool(rx.get("standing")),
                "allow": rx.get("allow", True) is True, "sessions_relaxed": n,
                "marker": P.relax_marker_present(auth.home)}

    @method
    def consent(peer, params, conn=None, rid=None):
        need_consumer(peer, "consent")                                   # M1
        eff = auth.reload()
        pol = eff.policy
        if not eff.enabled or pol.get("consent_requires_factor") != "platform":
            return {"mode": "none"}
        op = _consent_op(params.get("op"))
        subject = params.get("subject") or peer
        if not isinstance(subject, dict) or not gt_ipc.alive(subject.get("pid"),
                                                             subject.get("start")):
            raise Denied("subject_gone", "the subject process is not running")
        kind, root, shim_s = auth.classify(subject)
        if kind == "job":
            raise Denied("unattended", "consent can never be given unattended")
        session = shim_s or auth.session_for(root, kind)
        g = auth.active_grant(session.gkey(seat_of(kind)))
        if g is None:
            raise Denied("locked", "gt is locked; unlock first")
        now = time.monotonic()
        if g.consent_until and now < g.consent_until:
            if not auth.audit("consent", grant=g.gid, verdict="allow", reason="window"):
                raise Denied("audit_failed", "the audit log could not be written")
            return {"mode": "platform", "approved": True, "grant": g.gid, "window": True}
        op_hash = op["hash"]
        # M1: the text the person approves is composed here, from the op, never by the caller.
        text = ("gt-lotr asks to run a consent-tier operation:\n  %s %s on %s%s\n%s  arguments "
                "sha256 %s\n\nApprove with Touch ID / Windows Hello."
                % (op["tool"], op["op"], op["connection"],
                   " as %s" % op["identity"] if op.get("identity") else "",
                   "".join("  %s\n" % l for l in op.get("summary") or []),
                   op["args_sha256"][:16]))
        lk = auth._prompting(session.key)
        try:
            ctx = auth._ctx(text, None, rid, False)
            try:
                used = auth.collect(ctx, "consent", "lotr:consent:" + op_hash[:64],
                                    requester(peer), k=1, need_platform=True)
            except Denied as e:
                # Review 2026-10-04: a refused / cancelled / failed consent set no cooldown,
                # so a confused or injected session could raise Touch ID / Hello prompts back
                # to back. The same cooldown an unlock refusal sets now applies (the next
                # consent inside it is refused with code "cooldown", raising no prompt).
                auth.cooldown[session.key] = time.monotonic()
                auth.audit("consent", grant=g.gid, verdict="deny", reason=e.code)
                raise Denied("consent_denied", "the consent was not given (%s); wait %d s "
                             "before asking again" % (e.message, D_COOLDOWN), e.hints)
        finally:
            lk.release()
        if not auth.audit("consent", grant=g.gid, verdict="allow", factors=",".join(used),
                          reason="op " + op_hash[:16]):
            raise Denied("audit_failed", "the audit log could not be written")
        window = int(pol.get("consent_window_s") or 0)
        if window:
            g.consent_until = min(now + window, g.created + g.ttl)
        return {"mode": "platform", "approved": True, "grant": g.gid, "window": False,
                "op_hash": op_hash}

    # ---------------------------------------------------------- accept (phase 96)
    @method
    def accept_presence(peer, params, conn=None, rid=None):
        """A presence approval (ONE fresh platform factor) in place of a terminal, for
        `gt_broker.py accept` and nothing else (0.22.0, setting owner_gate_presence). The caller
        sends the target's current text and the text it would write; THIS side computes the
        changed lines and the hashes and composes the dialog -- no prompt text, summary or hash
        of the caller's is ever shown or trusted. A change that cannot be shown whole (over
        AP.MAX_CHANGED lines, a line over AP.MAX_COLS, a hidden character, credential-shaped text, a
        big text, global-memory/ design.md core-rules/) is `not_previewable` and raises no dialog. Needs unlock on, a live grant for the caller's
        session (a person unlocked gt), a usable platform factor, an unlocked screen with a
        desktop, Touch ID (never Windows Hello alone), fewer than ACCEPT_MAX_PROMPTS dialogs in
        ACCEPT_WINDOW_S and fewer than ACCEPT_DAILY_MAX today. A refusal sets the same cooldown an
        unlock refusal does; a helper-side rejection (no dialog shown) takes no slot and no cooldown. It never answers for a scheduled job."""
        eff = auth.reload()
        if not eff.enabled:
            raise Denied("disabled", "gt unlock is off")
        if eff.failed_closed:
            raise Denied("failed_closed", "; ".join(eff.problems))
        if eff.level(ACCEPT_SCOPE) == "deny":
            raise Denied("denied", "policy denies %s" % ACCEPT_SCOPE)
        acc = _accept_fields(params.get("accept"))
        kind, root, shim_s = auth.classify(peer)
        if kind == "job":
            raise Denied("unattended", "an unattended job cannot approve an accept")
        # Windows Hello takes NO caller text, so the person would approve a change they never saw:
        # only Touch ID (whose sheet shows the composed text) serves this route; Hello-only falls
        # through to the terminal until a gt-owned dialog shows the text first (B1).
        if "touchid" not in auth.usable_factors():
            raise Denied("no_platform_factor", "this needs Touch ID; Windows Hello cannot show "
                         "the change, so accept it at a terminal")
        session = shim_s or auth.session_for(root, kind)
        g = auth.active_grant(session.gkey(seat_of(kind)))
        if g is None:
            raise Denied("locked", "gt is locked; unlock first")
        if not auth.presence()["active"]:
            raise Denied("no_display", "no unlocked desktop to show the approval on")
        op_hash = hashlib.sha256("|".join([acc["target"], acc["conflict"], str(acc["version"]),
                                           acc["cur_sha"], acc["new_sha"],
                                           acc["nonce"]]).encode()).hexdigest()
        now = time.monotonic()
        recent = [t for t in getattr(auth, "_accept_prompts", []) if now - t < ACCEPT_WINDOW_S]
        if len(recent) >= ACCEPT_MAX_PROMPTS:
            auth._accept_prompts = recent
            auth.audit("accept_presence", grant=g.gid, verdict="deny", reason="rate_limited")
            raise Denied("rate_limited", "%d accept approvals were already asked for in the last "
                         "%d s; wait, or accept at a terminal" % (ACCEPT_MAX_PROMPTS,
                                                                   ACCEPT_WINDOW_S))
        try:
            text = AP.compose(requester(peer), acc["target"], acc["conflict"], acc["version"],
                              acc["new_sha"], acc["view"])
        except AP.NotPreviewable as e:
            raise Denied("not_previewable", "%s; accept it at a terminal" % e)
        lk = auth._prompting(session.key)
        try:
            _daily_take(auth.home, ACCEPT_DAILY_MAX)
            recent.append(now)
            auth._accept_prompts = recent
            ctx = auth._ctx(text, None, rid, False)
            try:
                used = auth.collect(ctx, "accept", "gt:vault:accept:" + op_hash[:64],
                                    requester(peer), k=1, need_platform=True)
            except Denied as e:
                if e.code == "factor_malformed":
                    # The helper refused the prompt: no dialog was ever shown, so it costs no
                    # slot (window or day) and sets no cooldown; the caller falls to a terminal.
                    recent.remove(now)
                    auth._accept_prompts = recent
                    _daily_refund(auth.home)
                    auth.audit("accept_presence", grant=g.gid, verdict="deny",
                               reason="helper_rejected", op=op_hash[:16],
                               sha=acc["new_sha"][:16])
                    raise Denied("not_previewable", "the platform helper could not show this "
                                 "text; accept it at a terminal")
                auth.cooldown[session.key] = time.monotonic()
                auth.audit("accept_presence", grant=g.gid, verdict="deny", reason=e.code,
                           op=op_hash[:16], sha=acc["new_sha"][:16])
                raise Denied("accept_denied", "the approval was not given (%s); nothing is "
                             "written" % e.message, e.hints)
        finally:
            lk.release()
        if not auth.audit("accept_presence", grant=g.gid, verdict="allow", factors=",".join(used),
                          reason="conflict %s" % acc["conflict"], op=op_hash[:16],
                          sha=acc["new_sha"][:16]):
            raise Denied("audit_failed", "the audit log could not be written")
        return {"approved": True, "grant": g.gid, "nonce": acc["nonce"], "target": acc["target"],
                "current_sha256": acc["cur_sha"], "new_sha256": acc["new_sha"]}

    # ---------------------------------------------------------- code signing
    @method
    def sign_code(peer, params, conn=None, rid=None):
        """A platform-factor signature over the digest of gt's installed-code manifest (0.21.0,
        `paranoid.signed_manifest`). ONE fresh Touch ID / Windows Hello, whose prompt names the
        requester and the first 16 hex of the digest; the signature covers a challenge bound to
        THIS digest (purpose `code-sign`, scope `code:<digest>`), so it cannot be replayed onto
        another manifest, a policy approval or an unlock. The client verifies it under the
        enrolled public key (gt_integrity.verify_signature). A scheduled job may never ask."""
        digest = params.get("digest")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise Denied("bad_request", "sign_code needs a sha256 hex digest")
        eff = auth.reload()
        if not eff.enabled:
            raise Denied("disabled", "gt unlock is off")
        if eff.failed_closed:
            raise Denied("failed_closed", "; ".join(eff.problems))
        kind, root, _s = auth.classify(peer)
        if kind == "job":
            raise Denied("unattended", "a scheduled job cannot sign the code manifest")
        if not any(x in P.PLATFORM_FACTORS for x in auth.usable_factors()):
            raise Denied("no_platform_factor", "signing needs Touch ID or Windows Hello, and "
                         "neither is enrolled and usable")
        text = ("%s asks you to SIGN the list of gt's installed code (fingerprint %s).\n"
                "Approve with Touch ID / Windows Hello only if you just ran an install or upgrade."
                % (requester(peer), digest[:16]))
        lk = auth._prompting(("sign", peer["pid"]))
        evidence = {}
        try:
            ctx = auth._ctx(text, conn, rid, tty(params))
            auth.collect(ctx, "code-sign", "code:" + digest, requester(peer), k=1,
                         need_platform=True, evidence=evidence)
        finally:
            lk.release()
        name = next((n for n in P.PLATFORM_FACTORS if n in (evidence.get("sigs") or {})), None)
        if name is None:
            raise Denied("no_signature", "the platform factor produced no signature")
        if not auth.audit("sign_code", verdict="ok", reason="digest " + digest[:16]):
            raise Denied("audit_failed", "the audit log could not be written")
        return {"factor": name, "digest": digest, "requester": evidence["requester"],
                "nonce": base64.b64encode(evidence["nonce"]).decode("ascii"),
                "sig": base64.b64encode(evidence["sigs"][name]).decode("ascii")}

    # ---------------------------------------------------------- secrets
    def _secret_gate(peer, params, conn, rid, ref):
        job = params.get("job")
        subject = params.get("subject")
        if subject is not None and not auth.consumer_ok(peer):
            # A VALUE goes back to the caller, so only gt-lotr's daemon may ask on behalf of
            # another process (gt_unlockd.is_lotr_daemon). A shell child naming the shim as
            # subject gets nothing.
            auth.audit("secret_resolve", ref=ref, verdict="deny", reason="not_a_consumer",
                       subject=peer.get("pid"))
            raise Denied("not_a_consumer", "only gt-lotr's daemon may resolve a secret on "
                         "behalf of another process", hints=consumer_hint(peer))
        if subject is not None and not gt_ipc.alive(subject.get("pid"), subject.get("start")):
            raise Denied("subject_gone", "the subject process is not running")
        target = {"pid": int(subject["pid"]), "start": subject["start"]} if subject else None
        scope = ("secret:" + ref) if job else "gt:secrets"
        v = auth.evaluate(peer, scope, subject=target, request=bool(params.get("request", True)),
                          reason="read the secret %s" % ref, conn=conn, rid=rid,
                          tty=tty(params), job=job)
        if not v["allowed"]:
            raise Denied(v["code"], v["message"], v.get("hints"))
        return v

    @method
    def secret(peer, params, conn=None, rid=None):
        ref = params.get("ref")
        if not isinstance(ref, str) or ":" not in ref:
            raise Denied("bad_request", "secret needs a ref (scheme:target)")
        scheme, target = ref.split(":", 1)
        if scheme == "sealed":
            # Single-use token (item #32): paranoid.action_tokens.secrets_unseal. Asked BEFORE the
            # gate, so a refused unseal never costs a Touch ID; the window cache below cannot
            # skip it -- every unseal spends one token.
            #
            # EXEMPTION (0.21.0): gt-lotr's own daemon is not asked for one. It opens a connection's
            # credential only to make a downstream call, and every LOTR call is already gated by its
            # own lotr_read / lotr_write / lotr_consent token, so a second token per unseal would
            # only stop the connection working unattended. The exemption is keyed on WHO is asking
            # -- auth.consumer_ok(peer), i.e. gt_unlockd.is_lotr_daemon: the installed gt-lotr's
            # lotrd.py, identified by the operating system from the socket's peer -- and never on
            # anything the caller sends: no parameter, flag or token field can switch it on. Any
            # other caller (a shell, gt unlock run/secret) still needs the token.
            if not auth.consumer_ok(peer):
                import gt_action_token as AT
                try:
                    AT.check("secrets_unseal", params.get("token"), target)
                except AT.TokenError as e:
                    auth.audit("secret_resolve", ref=ref, verdict="deny", reason=e.code,
                               subject=(params.get("subject") or peer).get("pid"))
                    raise Denied(e.code, e.message, e.hints)
        v = _secret_gate(peer, params, conn, rid, ref)
        if scheme == "sealed":
            import gt_unlock_seal as S
            if not S.NAME.match(target):              # the name goes into the prompt text
                raise Denied("bad_name", "a sealed name is one plain segment")
            # Owner decision 2026-10-03 18:37: a fresh platform factor for EVERY unseal, or
            # once per secrets_window_s -- per subject and per grant, never global (F4).
            win = int(auth.eff.policy.get("secrets_window_s") or 0)
            who = params.get("subject") or peer
            key = (who.get("pid"), who.get("start"), v.get("grant"), target)
            now = time.monotonic()
            value = None
            if win and v.get("grant"):
                with auth.lock:
                    hit = auth.sealed_cache.get(key)
                    if hit and hit[1] > now:
                        value = hit[0]
                    elif hit:
                        auth.sealed_cache.pop(key, None)
            if value is None:
                ctx = auth._ctx("%s asks to open the sealed credential %s"
                                % (requester(peer), target), conn, rid, tty(params))
                try:
                    value = S.get(auth.home, target, auth.enrolment(), auth.factors, ctx)
                except S.SealError as e:
                    raise Denied(e.code, e.message)
                if win and v.get("grant"):
                    with auth.lock:
                        if v.get("grant") in {g.gid for g in auth.grants.values()}:
                            auth.sealed_cache[key] = (value, now + win)
        else:
            import gt_unlock_brokers as B
            try:
                value = B.resolve(ref)
            except B.BrokerError as e:
                raise Denied(e.code, e.message)
        if not auth.audit("secret_resolve", ref=ref, grant=v.get("grant"), verdict="allow",
                          subject=(params.get("subject") or peer).get("pid")):
            raise Denied("audit_failed", "the audit log could not be written, so the secret "
                         "is not released")
        return {"value": value, "grant": v.get("grant")}

    @method
    def seal_put(peer, params, conn=None, rid=None):
        import gt_unlock_seal as S
        name, value = params.get("name"), params.get("value")
        if not isinstance(value, str) or not value:
            raise Denied("bad_request", "seal_put needs a value")
        v = _secret_gate(peer, dict(params, job=None), conn, rid, "sealed:%s" % name)
        try:
            ctx = auth._ctx("%s: seal a credential as sealed:%s" % (requester(peer), name),
                            conn, rid, tty(params))
            out = S.put(auth.home, name, value, auth.enrolment(), auth.factors, ctx)
        except S.SealError as e:
            raise Denied(e.code, e.message)
        _drop_cached(name)
        auth.audit("seal_put", ref="sealed:%s" % name, grant=v.get("grant"), verdict="ok")
        return out

    @method
    def seal_list(peer, params, conn=None, rid=None):
        import gt_unlock_seal as S
        return {"sealed": S.names(auth.home)}

    @method
    def seal_rm(peer, params, conn=None, rid=None):
        import gt_unlock_seal as S
        name = params.get("name")
        v = _secret_gate(peer, dict(params, job=None), conn, rid, "sealed:%s" % name)
        ok = S.remove(auth.home, name)
        _drop_cached(name)
        auth.audit("seal_rm", ref="sealed:%s" % name, grant=v.get("grant"), verdict="ok")
        return {"removed": ok}

    def _drop_cached(name):
        with auth.lock:
            for k in [k for k in auth.sealed_cache if k[3] == name]:
                auth.sealed_cache.pop(k, None)

    # ---------------------------------------------------------- enrolment
    def _bootstrap_ok(name, peer, params):
        """Nothing enrolled yet: start with the platform factor where this machine has one,
        because enrolling it takes a physical touch / PIN, which an agent cannot supply.

        M5 (usability run 2026-10-04): the platform factor is OPTIONAL. A person who does not
        want Touch ID / Windows Hello may enrol TOTP first with `enroll totp
        --without-platform` -- honoured only for a process the KERNEL shows is a person's
        terminal: no `claude` ancestor, a controlling terminal, a login shell above it (the
        same test read_without_unlock uses, F6/H2). An agent's shell is refused, so an agent
        still cannot bootstrap factors it would then hold."""
        for plat in P.PLATFORM_FACTORS:
            f = auth.factors.get(plat)
            if f is not None and f.available()[0] and name != plat:
                if name == "totp" and params.get("skip_platform"):
                    kind, _root, _s = auth.classify(peer)
                    if kind == "terminal" and _has_tty(peer) and auth.interactive(peer):
                        auth.audit("enroll_bootstrap", factor=name, verdict="ok",
                                   reason="without_platform", subject=peer["pid"])
                        return
                    auth.audit("enroll_bootstrap", factor=name, verdict="deny",
                               reason="without_platform_not_a_terminal", subject=peer["pid"])
                    raise Denied("platform_first", "enrolling TOTP without %s is allowed only "
                                 "from your own terminal (not from Claude Code or a script "
                                 "without one): run gt_unlock.py enroll totp --without-platform "
                                 "in a terminal window" % plat)
                raise Denied("platform_first", "enrol %s first: it needs your finger or PIN, "
                             "which is what makes the rest of the enrolment trustworthy. To use "
                             "TOTP without %s, run in your own terminal: gt_unlock.py enroll "
                             "totp --without-platform (then gt_unlock.py policy enable "
                             "--factors totp; SECURITY.md section 3)" % (plat, plat))

    @method
    def enroll(peer, params, conn=None, rid=None):
        name = params.get("factor")
        phase = params.get("phase") or "begin"
        enr = auth.enrolment()
        fac = auth.factors.get(name)
        if name not in ("totp", "touchid", "hello", "sso") or fac is None:
            raise Denied("bad_factor", "enrolable factors: totp, touchid, hello, sso")
        if name == "totp" and phase == "confirm":
            code = params.get("code")
            if not isinstance(code, str):
                raise Denied("bad_request", "confirm needs the code")
            st = auth.state()
            try:
                F.TotpFactor().confirm(auth.home, code, st, auth.eff.policy)
            except F.FactorError as e:
                raise Denied("factor_" + e.code, e.message)
            enr.setdefault("factors", {})["totp"] = {
                "enrolled_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "account": params.get("account") or "gt"}
            st.pop("needs_reenrol", None)
            auth.save_state(st)
            auth.save_enrolment(enr)
            auth.audit("enroll", factor="totp", verdict="ok")
            return {"enrolled": "totp"}
        ok, why = fac.available()
        if not ok:
            raise Denied("unavailable", "%s is not available here: %s" % (name, why))
        used = require_full(peer, "enrol %s" % name, conn, rid, params)
        if not used and not (enr.get("factors") or {}):
            _bootstrap_ok(name, peer, params)
        if name == "totp":
            uri = F.TotpFactor().begin(auth.home, params.get("account") or "gt")
            auth.audit("enroll_begin", factor="totp", verdict="ok")
            return {"uri": uri, "next": "confirm"}
        ctx = auth._ctx("%s: enrol %s for gt unlock" % (requester(peer), name), conn, rid,
                        tty(params))
        try:
            rec = fac.enroll(ctx)
            # A platform factor is proven once before it is saved: the enrolment itself needs
            # the finger / PIN, and a key that cannot sign is caught now, not at first use.
            if name in P.PLATFORM_FACTORS:
                ch = hashlib.sha256(b"gt-unlock-enrol\0" + secrets.token_bytes(32)).digest()
                fac.prove(rec, ch, ctx)
        except F.FactorError as e:
            auth.audit("enroll", factor=name, verdict="deny", reason=e.code)
            raise Denied("factor_" + e.code, e.message)
        rec["enrolled_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        enr.setdefault("factors", {})[name] = rec
        auth.save_enrolment(enr)
        st = auth.state()
        st.pop("needs_reenrol", None)
        auth.save_state(st)
        auth.audit("enroll", factor=name, verdict="ok")
        return {"enrolled": name}

    @method
    def unenroll(peer, params, conn=None, rid=None):
        name = params.get("factor")
        enr = auth.enrolment()
        if name not in (enr.get("factors") or {}):
            raise Denied("not_enrolled", "%s is not enrolled" % name)
        require_full(peer, "remove %s" % name, conn, rid, params)
        enr["factors"].pop(name, None)
        auth.save_enrolment(enr)
        if name == "totp":
            try:
                os.unlink(F.TotpFactor.seed_path(auth.home))
            except OSError:
                pass
        auth.audit("unenroll", factor=name, verdict="ok")
        return {"removed": name}

    @method
    def recovery_new(peer, params, conn=None, rid=None):
        if not (auth.enrolment().get("factors") or {}):
            raise Denied("enrol_first", "enrol a factor before making recovery codes")
        require_full(peer, "new recovery codes", conn, rid, params)
        codes = F.RecoveryFactor().generate(auth.home)
        auth.audit("recovery_new", verdict="ok", count=len(codes))
        return {"codes": codes}

    # ---------------------------------------------------------- policy
    @method
    def policy_get(peer, params, conn=None, rid=None):
        eff = auth.reload()
        user = F.read_json(auth.path("policy.json"))
        return {"effective": eff.policy, "user": user, "problems": eff.problems,
                "enabled": eff.enabled}

    @method
    def policy_set(peer, params, conn=None, rid=None):
        new = params.get("policy")
        if not isinstance(new, dict):
            raise Denied("bad_request", "policy_set needs a policy object")
        try:
            merged = P.merge(new, None)
        except (P.PolicyError, TypeError, ValueError) as e:
            raise Denied("policy_invalid", str(e))
        eff = auth.reload()
        if merged.get("enabled"):
            prob = auth._k_problem(merged)
            if prob:
                raise Denied("enrol_first", prob)
        new = {k: v for k, v in new.items() if k != P.ADMIN_FLOOR_KEY}
        # 0.22.0 relax: turning the STANDING relax on is a person at a terminal with the factors, never
        # an agent that holds them. A tighten-only marker (relax_off) means the person switched it off;
        # a later unrelated policy change does not bring it back, only an explicit `relax standing on`
        # (params["relax_reenable"]) does.
        # Review F2: "was on" is what the AUTHORITY enforced -- unlock on, an approved policy, the
        # standing relax in effect -- never what the file says. A `standing: true` written while
        # unlock was off (no factor) or behind the authority's back is new when unlock goes on.
        erx = eff.policy.get("relax") or {}
        was_on = bool(eff.enabled and not eff.problems and erx.get("standing")
                      and erx.get("allow", True) is True)
        off_marked = P.relax_marker_present(auth.home) or auth._relax_latched
        nrx = new.get("relax") if isinstance(new.get("relax"), dict) else {}
        want_on = bool(nrx.get("standing"))
        if want_on and off_marked and not params.get("relax_reenable"):
            new["relax"] = dict(nrx, standing=False)
            want_on = False
        # The standing value only acts once unlock is on; turning unlock on re-checks it here.
        turns_on = want_on and not was_on and bool(merged.get("enabled"))
        if turns_on:
            terminal_only(peer, "turning the standing relax on")
        data = (json.dumps(new, indent=2, sort_keys=True) + "\n").encode("utf-8")
        h = _sha(data)
        serial, ts = auth.next_approval()
        evidence = {}
        used = []
        if eff.enabled or merged.get("enabled"):
            # Turning unlock on proves the factors work before anything is locked; changing
            # or turning it off needs the same proof (step-up + K) -- and the platform
            # factor's signature over THESE bytes is the approval (F3).
            purpose = "change the unlock policy"
            if turns_on:
                purpose += (" AND turn the standing relax on (pushes, fetches, writes and the other "
                            "relaxable checks stop asking for a fresh factor)")
            used = require_full(peer, purpose, conn, rid, params,
                                bind=approval_bind(h, serial, ts), evidence=evidence)
            if not used:
                raise Denied("enrol_first", "enrol your factors before turning unlock on")
        F.write_private(auth.path("policy.json"), data)
        if off_marked and (want_on or not (new.get("relax") or {}).get("standing")):
            auth._relax_latched = False                   # lifted by a signed write, never by a deletion
            P.set_relax_marker(auth.home, False)          # the signed policy now says what the marker said
        st = write_approval(h, used, evidence, serial, ts)
        st["unlock_on"] = bool(merged.get("enabled"))     # see gt_unlock_policy.enabled_at
        auth.save_state(st)
        P.set_marker(auth.home, bool(merged.get("enabled")))
        if not merged.get("enabled"):
            auth._off_by_policy = True
        auth.reload()
        # M5: a K the owner chose (gt_unlock.py policy enable --factors) is on record twice --
        # in the approved policy bytes and here -- so a lower K is never a silent one.
        chosen = (merged.get("factors") or {}).get("chosen")
        auth.audit("policy_set", verdict="ok", enabled=bool(merged.get("enabled")),
                   k=(merged.get("factors") or {}).get("required"),
                   k_default=(P.default_policy().get("factors") or {}).get("required"),
                   k_chosen_by_owner=",".join(chosen.get("factors") or [])
                   if isinstance(chosen, dict) else None)
        return {"enabled": bool(merged.get("enabled"))}

    @method
    def policy_approve(peer, params, conn=None, rid=None):
        h = auth._policy_file_hash()
        if h is None:
            raise Denied("no_policy", "there is no policy.json to approve")
        serial, ts = auth.next_approval()
        evidence = {}
        used = require_full(peer, "approve a policy.json edited outside gt_unlock.py", conn,
                            rid, params, bind=approval_bind(h, serial, ts), evidence=evidence)
        st = write_approval(h, used, evidence, serial, ts)
        pol = F.read_json(auth.path("policy.json")) or {}
        st["unlock_on"] = bool(pol.get("enabled"))
        auth.save_state(st)
        P.set_marker(auth.home, bool(pol.get("enabled")))
        if not pol.get("enabled"):
            auth._off_by_policy = True
        auth.audit("policy_approve", verdict="ok")
        return {"approved": True}

    @method
    def audit_tail(peer, params, conn=None, rid=None):
        n = max(1, min(int(params.get("n") or 20), 500))
        try:
            with open(auth.path("audit.jsonl"), "r", encoding="utf-8") as f:
                lines = f.readlines()[-n:]
        except OSError:
            lines = []
        return {"lines": [ln.rstrip("\n") for ln in lines]}

    @method
    def code_status(peer, params, conn=None, rid=None):
        """M12: the pid, start time and whether this process runs the code now on disk."""
        return auth.code_status()

    @method
    def stop_if_stale(peer, params, conn=None, rid=None):
        """M12 (usability run 2026-10-04): install / upgrade / rollback left the authority on
        OLD code (the same pid survived all three). This stops it WITHOUT a factor, but only
        when its code on disk has changed since it started -- it never grants anything: every
        grant is revoked and the next request starts the authority again from the new code.
        A same-user process could end the daemon by signal anyway (the F1 residual); this
        only adds an orderly way that drops grants and says so in the audit log."""
        cs = auth.code_status()
        if not cs["stale"]:
            return dict(cs, stopping=False)
        n = auth.revoke_all("code_updated")
        auth.audit("daemon_stop", verdict="ok", reason="code_updated", revoked=n)
        ev = getattr(auth, "stop", None)
        if ev is not None:
            ev.set()
        return dict(cs, stopping=True, revoked=n)

    @method
    def stop(peer, params, conn=None, rid=None):
        # F1: stopping frees the authority's address. While unlock is on that needs a fresh
        # factor (the platform one where usable). It is friction, not a boundary: a same-user
        # process can still kill the daemon -- which is why every client now verifies the
        # server it reaches (gt_unlock_client), and a restart revokes every grant.
        eff = auth.reload()
        usable = auth.usable_factors() if eff.enabled else []
        if usable:
            plat = any(x in P.PLATFORM_FACTORS for x in usable)
            lk = auth._prompting(("stop", peer["pid"]))
            try:
                ctx = auth._ctx("%s asks to stop the gt unlock authority (every grant is "
                                "revoked)" % requester(peer), conn, rid, tty(params))
                auth.collect(ctx, "stop", "gt:unlock:stop", requester(peer), k=1,
                             need_platform=plat)
            finally:
                lk.release()
        auth.revoke_all("daemon_stop")
        ev = getattr(auth, "stop", None)
        if ev is not None:
            ev.set()
        return {"stopping": True}

    return m


_OP_FIELD = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")


CONSENT_ARGS_MAX = 65536

# ---- accept_presence (phase 96): what may be shown whole in a dialog, and how it is checked
# The preview bounds, the dialog's header/footer and the hidden-character rule live in ONE module
# (gt_accept_preview), shared with the client, so the composed prompt always fits the macOS helper.
ACCEPT_SCOPE = "gt:vault:accept"
ACCEPT_WINDOW_S = 600
ACCEPT_MAX_PROMPTS = 3
ACCEPT_DAILY_MAX = 12         # dialogs per local day, kept in a file so a restart does not reset it
ACCEPT_STATE = "accept_presence.json"
_ACCEPT_CONFLICT = re.compile(r"^\d{4}-\d{2}-\d{2}-[0-9a-f]{12}$")


# The count this process kept, per home: {home: (day, n)}. The cap is the larger of this and the
# file's, so deleting or rewriting the file lower does not reset a running authority.
_daily_mem: dict = {}


def _daily_read(home):
    """-> the state file's {"day", "n"}, or None when there is no file (a first-ever run). A file
    that exists but cannot be read or does not hold a well-formed count is Denied(daily_cap): a
    broken count is no cap, so it fails closed (a terminal accept)."""
    try:
        with open(os.path.join(home, ACCEPT_STATE), encoding="utf-8") as fh:
            d = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        d = None
    n = d.get("n") if isinstance(d, dict) else None
    if not (isinstance(d, dict) and isinstance(d.get("day"), str) and isinstance(n, int)
            and not isinstance(n, bool) and n >= 0):
        raise Denied("daily_cap", "the daily accept count cannot be read; accept it at a terminal")
    return d


def _today():
    import datetime
    return datetime.date.today().isoformat()


def _daily_used(home, today=None):
    day = today or _today()
    d = _daily_read(home)
    on_file = d["n"] if d is not None and d["day"] == day else 0
    mem = _daily_mem.get(home)
    return max(on_file, mem[1] if mem and mem[0] == day else 0)


def _daily_write(home, day, n):
    F.write_private(os.path.join(home, ACCEPT_STATE),
                    (json.dumps({"day": day, "n": max(0, n)}) + "\n").encode("utf-8"))
    _daily_mem[home] = (day, max(0, n))


def _daily_take(home, cap, today=None):
    """Count one more dialog today and -> the new count; Denied(daily_cap) at the cap. A file that
    cannot be written, or that exists but cannot be read, refuses (no count kept = no cap kept)."""
    day = today or _today()
    n = _daily_used(home, day)
    if n >= cap:
        raise Denied("daily_cap", "%d accept approvals were already asked for today; accept it at "
                     "a terminal" % cap)
    try:
        _daily_write(home, day, n + 1)
    except OSError:
        raise Denied("daily_cap", "the daily accept count could not be kept; accept it at a "
                     "terminal")
    return n + 1


def _daily_refund(home, today=None):
    day = today or _today()
    try:
        n = _daily_used(home, day)
    except Denied:
        return
    if n:
        try:
            _daily_write(home, day, n - 1)
        except OSError:
            pass


def _scan_shown(texts):
    """N2: the credential-shape scan again, HERE, over every text the dialog would show. Needs the
    shipped detectors; none loaded, or any error, is `not_previewable` (a terminal accept)."""
    try:
        import gt_secrets
        rules, _problems, _ret = gt_secrets.rules_from_slot(None)
        if not rules or gt_secrets.scan_text("\n".join(texts), rules):
            return False
    except Exception:                                           # noqa: BLE001 - cannot check = no
        return False
    return True


def _accept_fields(a):
    """Validate the fields of an accept approval and compute, HERE, everything the dialog shows.
    -> {target, conflict, version, nonce, cur_sha, new_sha, view}."""
    if not isinstance(a, dict):
        raise Denied("bad_request", "accept_presence needs the accept fields")
    target, cid, ver, nonce = a.get("target"), a.get("conflict"), a.get("version"), a.get("nonce")
    if not isinstance(target, str) or not _OP_FIELD.match(target) or ".." in target.split("/"):
        raise Denied("bad_request", "accept target must be a plain vault path")
    if not isinstance(cid, str) or not _ACCEPT_CONFLICT.match(cid):
        raise Denied("bad_request", "accept conflict must be a conflict id")
    if ver is not None and (not isinstance(ver, int) or isinstance(ver, bool) or not 1 <= ver <= 999):
        raise Denied("bad_request", "accept version must be a number or none")
    if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9]{16,64}", nonce):
        raise Denied("bad_request", "accept nonce must be 16-64 letters or digits")
    cur, new = a.get("cur_text"), a.get("new_text")
    if cur is not None and not isinstance(cur, str) or not isinstance(new, str):
        raise Denied("bad_request", "accept needs the current and the new text")
    if AP.terminal_only_name(target):
        raise Denied("not_previewable", "this target is accepted at a terminal, never from a "
                     "dialog")
    try:
        view = AP.build(cur, new)
        if len(target) > AP.MAX_TARGET:
            raise AP.NotPreviewable("the target path is too long to show whole")
        if AP.hidden_count(target, keep=""):
            raise AP.NotPreviewable("the target path holds hidden characters")
    except AP.NotPreviewable as e:
        raise Denied("not_previewable", "%s; accept it at a terminal" % e)
    if not _scan_shown(view["texts"]):
        raise Denied("not_previewable", "the change holds credential-shaped text or it could not "
                     "be checked; accept it at a terminal")
    return {"target": target, "conflict": cid, "version": ver, "nonce": nonce, "view": view,
            "cur_sha": hashlib.sha256((cur or "").encode("utf-8")).hexdigest(),
            "new_sha": hashlib.sha256(new.encode("utf-8")).hexdigest()}

_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")


def _clean(v, n=120):
    """One line of at most `n` characters: control characters and line breaks made spaces."""
    t = " ".join(_CTRL.sub(" ", str(v)).split())
    return t if len(t) <= n else t[:n - 1] + "\u2026"


def _addresses(recips, n=5):
    got = []
    for r in recips if isinstance(recips, list) else []:
        a = ((r or {}).get("emailAddress") or {}).get("address") if isinstance(r, dict) else None
        if a:
            got.append(_clean(a, 80))
    more = " (+%d more)" % (len(got) - n) if len(got) > n else ""
    return ", ".join(got[:n]) + more


def consent_summary(op, args):
    """-> [line] saying WHAT a known consent op will do, from its verified arguments: who a mail
    goes to and its subject, which PR is merged. [] for an op it does not know. Mirrored in
    gt-lotr lotrlib/confirm.summarize (the dialog route); tests pin the two to one output."""
    lines = []
    if op == "send_mail":
        m = args.get("message") if isinstance(args.get("message"), dict) else {}
        for label, key in (("To", "toRecipients"), ("Cc", "ccRecipients"),
                           ("Bcc", "bccRecipients")):
            a = _addresses(m.get(key))
            if a:
                lines.append("%s: %s" % (label, a))
        if not any(l.startswith("To:") for l in lines):
            lines.insert(0, "To: (no recipients)")
        lines.append("Subject: %s" % _clean(m.get("subject") or "(none)"))
    elif op == "merge_pull":
        target = "%s/%s#%s" % (_clean(args.get("owner") or "?", 60),
                               _clean(args.get("repo") or "?", 80),
                               _clean(args.get("pull_number") or "?", 12))
        how = _clean(args.get("merge_method") or "merge", 12)
        sha = _clean(args.get("sha"), 12) if args.get("sha") else "any"
        lines.append("Merge: %s (%s, head %s)" % (target, how, sha))
    return lines


def _consent_op(op):
    """Validate the op a consent is for and compute its hash HERE (M1). -> dict."""
    if not isinstance(op, dict):
        raise Denied("bad_request", "consent needs the op: {tool, connection, op, args_sha256}")
    out = {}
    for k in ("tool", "connection", "op", "args_sha256"):
        v = op.get(k)
        if not isinstance(v, str) or not _OP_FIELD.match(v):
            raise Denied("bad_request", "consent op.%s must be a plain string" % k)
        out[k] = v
    ident = op.get("identity")
    if isinstance(ident, str) and _OP_FIELD.match(ident):
        out["identity"] = ident
    # The op's arguments, optional: when sent they must be EXACTLY what args_sha256 covers --
    # recomputed here -- and only then does the person see a summary of them (recipient,
    # subject, PR). The summary is composed here from verified data, never caller prose (M1).
    args = op.get("args")
    if args is not None:
        blob = json.dumps(args, sort_keys=True, default=str)
        if not isinstance(args, dict) or len(blob) > CONSENT_ARGS_MAX:
            raise Denied("bad_request", "consent op.args must be an object of at most %d "
                         "bytes" % CONSENT_ARGS_MAX)
        if hashlib.sha256(blob.encode()).hexdigest() != out["args_sha256"]:
            raise Denied("bad_request", "consent op.args do not match op.args_sha256")
        out["summary"] = consent_summary(out["op"], args)
    out["hash"] = hashlib.sha256("|".join([out["tool"], out["connection"], out["op"],
                                           out["args_sha256"]]).encode("utf-8")).hexdigest()
    return out


def auth_version():
    import gt_unlockd
    return gt_unlockd.VERSION
