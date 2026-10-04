#!/usr/bin/env python3
"""gt_unlockd_methods -- the authority's wire methods, one function each (0.20.0).

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
  consent             only gt-lotr's installed lotrd may ask (review M1); the prompt text is
                      composed HERE from the op, never taken from the caller; then a live grant
                      and a platform signature over the op unless inside the consent window
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

    def need_consumer(peer, what):
        if not auth.consumer_ok(peer):
            auth.audit(what, verdict="deny", reason="not_a_consumer", subject=peer.get("pid"))
            raise Denied("not_a_consumer", "only gt-lotr's installed daemon may ask for %s"
                         % ("a consent" if what == "consent" else "this"))

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
        # role "lotr" (default): gt-lotr's lotr_mcp.py; role "vault" (gt sandbox mode, 0.20.0):
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
        return {"grant": g.gid, "factors": g.factors, "ttl_s": g.ttl, "idle_s": g.idle,
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
    def lock(peer, params, conn=None, rid=None):
        n = auth.revoke_all("lock")
        return {"revoked": n}

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
        text = ("gt-lotr asks to run a consent-tier operation:\n  %s %s on %s%s\n  arguments "
                "sha256 %s\n\nApprove with Touch ID / Windows Hello."
                % (op["tool"], op["op"], op["connection"],
                   " as %s" % op["identity"] if op.get("identity") else "",
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
                         "behalf of another process")
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
        v = _secret_gate(peer, params, conn, rid, ref)
        scheme, target = ref.split(":", 1)
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
        data = (json.dumps(new, indent=2, sort_keys=True) + "\n").encode("utf-8")
        h = _sha(data)
        serial, ts = auth.next_approval()
        evidence = {}
        used = []
        if eff.enabled or merged.get("enabled"):
            # Turning unlock on proves the factors work before anything is locked; changing
            # or turning it off needs the same proof (step-up + K) -- and the platform
            # factor's signature over THESE bytes is the approval (F3).
            used = require_full(peer, "change the unlock policy", conn, rid, params,
                                bind=approval_bind(h, serial, ts), evidence=evidence)
            if not used:
                raise Denied("enrol_first", "enrol your factors before turning unlock on")
        F.write_private(auth.path("policy.json"), data)
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
    out["hash"] = hashlib.sha256("|".join([out["tool"], out["connection"], out["op"],
                                           out["args_sha256"]]).encode("utf-8")).hexdigest()
    return out


def auth_version():
    import gt_unlockd
    return gt_unlockd.VERSION
