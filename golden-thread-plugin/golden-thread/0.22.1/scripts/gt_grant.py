#!/usr/bin/env python3
"""gt_grant -- let one command through for a while: once, N minutes, N hours, or forever (0.21.0).

    gt_grant.py add PATTERN once|<N>m|<N>h|forever   write the grant (needs YOU, in a terminal)
    gt_grant.py list                                 live grants with the time left
    gt_grant.py revoke ID                            end one now
    gt_grant.py sweep [--hook]                       end what has expired (SessionStart)
    gt_grant.py gate --hook                          PreToolUse: ask again once a grant has ended
    gt_grant.py spend --hook                         PostToolUse: log each use; spend a `once`
    gt_grant.py denied --hook                        PermissionDenied: tell the user the cure

WHY. A refusal ("the user can add a permission rule") is right, and a hand-written rule that
lasts forever is the wrong cure for a one-off. A grant is the middle: a real Claude Code
permission rule, with a lifetime. `/gt:gt-settings lockdown <level>` is the broad switch; this is
the exception for one command.

HOW (decision D1). A grant is an `allow` rule in ~/.claude/settings.json, recorded with its
`expires_at` in gt_lockdown's ownership state (~/.claude/golden-thread/lockdown/state.json). Four
things end it: the session-start sweep, the PreToolUse gate when the clock has passed it mid-session
(it makes Claude Code ASK again; it never says "allow"), the PostToolUse spend for `once`, and
`revoke`. gt's guards only ever tighten: nothing here emits an allow decision. `forever` is a plain
allow rule that survives gt being uninstalled; settings.json is backed up before every change.

WHO. Grants are the user's to give. `add` needs a terminal (an agent's shell has none), asks gt
unlock for a fresh confirmation when unlock is on, and refuses what no grant may cover: a LOTR
consent-tier op, a credential path core denies, and a whole tool ("Bash") -- that is the lockdown
level `insecure`, chosen on purpose. Honest limit: matching here is an approximation of Claude
Code's own (a prefix for `Bash(x:*)`, a glob for paths); the rule in settings.json is what Claude
Code enforces, and this tool only decides when to take it out.

Every use is one line in ~/.claude/golden-thread/lockdown/log.jsonl: the grant, its rule, a hash of
the command (never the command, which may carry a secret). Stdlib only. Exit: 0 ok, 1 refused,
2 usage. Hooks fail open and silent.
"""
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import gt_tty  # noqa: E402  (has_terminal: NUL is not a terminal on Windows)



class _LazyLockdown(object):
    """gt_lockdown, imported on first use: the PreToolUse / PostToolUse hooks run on every Bash
    call and must cost nothing when no grant exists."""

    def __getattr__(self, name):
        import gt_lockdown
        return getattr(gt_lockdown, name)


ld = _LazyLockdown()

ONCE_TTL_S = 3600                     # a `once` nobody spent is not left standing
MAX_TIMED_S = 7 * 24 * 3600
_DUR_RE = re.compile(r"^([1-9][0-9]*)([mh])$")


class GrantError(Exception):
    pass


def _now(now=None):
    if now is not None:
        return float(now)
    env = os.environ.get("GT_GRANT_NOW")
    try:
        return float(env) if env else time.time()
    except ValueError:
        return time.time()


# ------------------------------------------------------------------ parsing
def parse_duration(text):
    """-> ("once", None) | ("timed", seconds) | ("forever", None). ValueError otherwise."""
    t = (text or "").strip().lower()
    if t in ("once", "forever"):
        return t, None
    m = _DUR_RE.match(t)
    if not m:
        raise ValueError("duration %r: use once, <N>m, <N>h or forever" % (text,))
    secs = int(m.group(1)) * (60 if m.group(2) == "m" else 3600)
    if secs > MAX_TIMED_S:
        raise ValueError("duration %r is over 7 days; use forever if you mean it" % (text,))
    return "timed", secs


def pattern_to_rule(pattern):
    """A command pattern (or an explicit rule) -> a Claude Code permission rule. ValueError if
    it is malformed or too broad for a grant."""
    p = (pattern or "").strip()
    if not p or "\n" in p or "\r" in p:
        raise ValueError("a grant needs one non-empty line")
    if p in ld._BARE_TOOLS:
        raise ValueError("%r covers a whole tool; that is a lockdown level (insecure), not a "
                         "grant" % pattern)
    if re.match(r"^[A-Za-z][A-Za-z0-9_.\-]*\(.+\)$", p) or p.startswith("mcp__"):
        rule = p                                            # already a rule
    else:
        if "(" in p or ")" in p:
            raise ValueError("a command pattern cannot contain parentheses; write the rule "
                             "yourself, e.g. Bash(...)")
        if p.endswith("*"):
            prefix = p[:-1].rstrip().rstrip(":")
            if not prefix:
                raise ValueError("a grant for every command is the `insecure` lockdown level, "
                                 "not a grant: gt_settings.py set lockdown insecure")
            rule = "Bash(%s:*)" % prefix
        else:
            rule = "Bash(%s)" % p
    if rule in ld._BARE_TOOLS or re.match(r"^[A-Za-z]+\((\*|:\*|\*\*)?\)$", rule) \
            or rule in ("Bash(*)", "Bash(:*)", "Edit(*)", "Edit(**)"):
        raise ValueError("%r covers a whole tool; that is a lockdown level (insecure), not a "
                         "grant" % pattern)
    if ld._is_consent_allow(rule):
        raise ValueError("%s is a consent-tier op; those always ask and cannot be granted" % rule)
    why = ld._judge_rule(rule, "allow", "insecure")
    if why:
        raise ValueError(why)
    return rule


# ------------------------------------------------------------------ matching
def rule_matches(rule, tool, tool_input):
    """Does the permission rule `rule` cover this tool call? An approximation of Claude Code's
    own matching (see the header), good enough to decide when to take a grant out."""
    base, pat = ld._split_rule(rule)
    if base != tool and not (tool and base == tool):
        return False
    if pat is None:
        return True
    if tool == "Bash":
        cmd = ((tool_input or {}).get("command") or "").strip()
        if pat.endswith(":*"):
            pre = pat[:-2]
            return cmd == pre or cmd.startswith(pre + " ") or cmd.startswith(pre)
        if "*" in pat:
            return fnmatch.fnmatchcase(cmd, pat)
        return cmd == pat
    path = (tool_input or {}).get("file_path") or (tool_input or {}).get("path") or ""
    return bool(path) and (fnmatch.fnmatchcase(path, pat) or fnmatch.fnmatchcase(path, pat + "*"))


def _payload_call(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("tool_name"), str):
        return None
    ti = payload.get("tool_input")
    return payload["tool_name"], ti if isinstance(ti, dict) else {}


def _allow_rules(h=None):
    try:
        d = ld.sb.load_settings(h)
    except Exception:                                    # noqa: BLE001
        return []
    perms = d.get("permissions") if isinstance(d, dict) else None
    lst = perms.get("allow") if isinstance(perms, dict) else None
    return [r for r in lst if isinstance(r, str)] if isinstance(lst, list) else []


# ------------------------------------------------------------------ the verbs
def add(pattern, duration, h=None, now=None, session=None, token=None):
    kind, secs = parse_duration(duration)
    rule = pattern_to_rule(pattern)
    # Single-use token (item #32): paranoid.action_tokens.grant_add. Bound to the pattern. Nothing
    # is required, and nothing changes, while the setting is off.
    import gt_action_token as AT
    try:
        AT.check("grant_add", token, pattern.strip(), None, session=session, h=h)
    except AT.TokenError as e:
        raise ValueError("%s (%s)%s" % (e.message, e.code, "".join("\n  " + x for x in e.hints)))
    t = _now(now)
    g = {"id": uuid.uuid4().hex[:8], "pattern": pattern.strip(), "rule": rule, "kind": kind,
         "created_at": t, "expires_at": None, "uses_left": None}
    if kind == "timed":
        g["expires_at"] = t + secs
    elif kind == "once":
        g["uses_left"] = 1
        g["expires_at"] = t + ONCE_TTL_S
    if session:
        g["created_by_session"] = session
    try:
        return ld.grant_add(g, h)
    except ld.LockdownError as e:
        raise ValueError(str(e))


def list_grants(h=None, now=None):
    t = _now(now)
    rows = []
    for gid, g in sorted(ld.grants(h).items(), key=lambda kv: kv[1].get("created_at") or 0):
        exp = g.get("expires_at")
        rows.append({"id": gid, "pattern": g.get("pattern"), "rule": g.get("rule"),
                     "kind": g.get("kind"), "expires_at": exp,
                     "remaining_s": None if (exp is None or g.get("kind") == "forever")
                     else max(0, int(exp - t)),
                     "uses_left": g.get("uses_left"), "active": ld.grant_active(g, t)})
    return rows


def format_list(rows, now=None):
    if not rows:
        return "no grants"
    out = []
    for r in rows:
        if r["kind"] == "forever":
            left = "forever"
        elif r["kind"] == "once":
            left = "once, %dm left" % ((r["remaining_s"] or 0) // 60) if r["active"] else "spent"
        else:
            left = "%dm left" % ((r["remaining_s"] or 0) // 60) if r["active"] else "ended"
        out.append("%-8s %-40s %s" % (r["id"], r["pattern"], left))
    return "\n".join(out)


def revoke(gid, h=None):
    ids = [k for k in ld.grants(h) if k == gid] or [k for k in ld.grants(h) if k.startswith(gid)]
    if len(ids) != 1 or not gid:
        raise KeyError(gid)
    return ld.grant_remove(ids[0], h, why="revoked")


def sweep(h=None, now=None, plugin_root=None):
    return ld.sweep(h, plugin_root, now=_now(now))


def is_granted(payload, h=None, now=None):
    """Is there a grant still in force that covers this call?"""
    call = _payload_call(payload)
    if not call:
        return False
    t = _now(now)
    return any(ld.grant_active(g, t) and rule_matches(g["rule"], *call)
               for g in ld.grants(h).values())


def spend(payload, h=None, now=None):
    """After a call that ran: log each grant that covered it, and spend a `once`. -> [spent ids]"""
    call = _payload_call(payload)
    if not call:
        return []
    t = _now(now)
    digest = hashlib.sha256(json.dumps(call[1], sort_keys=True).encode("utf-8")).hexdigest()[:12]
    spent = []
    for gid, g in sorted(ld.grants(h).items()):
        if not (ld.grant_active(g, t) and rule_matches(g["rule"], *call)):
            continue
        ld._log({"grant_use": gid, "rule": g["rule"], "tool": call[0], "cmd_sha": digest}, h)
        if g.get("uses_left") is not None:
            left = g["uses_left"] - 1
            if left <= 0:
                ld.grant_remove(gid, h, why="spent")
                spent.append(gid)
            else:
                st = ld.load_state(h)
                st["grants"][gid]["uses_left"] = left
                ld.sb._atomic_write(ld.state_path(h), json.dumps(st, indent=2, sort_keys=True) + "\n")
    release(payload, h)
    return spent


# A `once` grant is RESERVED at PreToolUse and spent at PostToolUse (0.22.0, review A5). Without the
# reservation two parallel calls both passed the allow rule before either was spent, so one `once`
# covered two commands. The reservation lives in its own file under a lock (parallel hooks are
# separate processes) and lapses after RESERVE_TTL_S, because a call that something else denied
# never reaches PostToolUse; a PermissionDenied for the call releases it at once.
RESERVE_TTL_S = 120


def _res_path(h=None):
    return os.path.join(ld._dir(h), "reservations.json")


@contextmanager
def _res_lock(h=None, wait=2.0):
    """Yield True when the lock is held, False when it could not be taken (then: no reservation, the
    behaviour from before 0.22.0, never a refusal). A lock older than 10 s is stale and broken."""
    path = _res_path(h) + ".lock"
    fd = None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        yield False
        return
    deadline = time.time() + wait
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
            break
        except FileExistsError:
            try:
                if time.time() - os.stat(path).st_mtime > 10:
                    os.unlink(path)
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                break
            time.sleep(0.01)
        except OSError:
            break
    try:
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)
            try:
                os.unlink(path)
            except OSError:
                pass


def _res_read(h=None):
    try:
        with open(_res_path(h), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _res_write(res, h=None):
    ld.sb._atomic_write(_res_path(h), json.dumps(res, indent=1, sort_keys=True) + "\n")


def reserve(payload, h=None, now=None):
    """Reserve the `once` grant(s) this call needs for its tool_use_id. -> the grant a still-running
    parallel call already holds (the caller denies), or None (go ahead). A call without an id, or
    one no `once` grant covers, is never held up."""
    call = _payload_call(payload)
    uid = payload.get("tool_use_id") if isinstance(payload, dict) else None
    if not call or not isinstance(uid, str) or not uid:
        return None
    t = _now(now)
    once = [(gid, g) for gid, g in ld.grants(h).items()
            if g.get("uses_left") is not None and ld.grant_active(g, t) and rule_matches(g["rule"], *call)]
    if not once:
        return None
    with _res_lock(h) as held:
        if not held:
            return None
        res = {k: v for k, v in _res_read(h).items()
               if isinstance(v, dict) and t - (v.get("at") or 0) < RESERVE_TTL_S}
        for gid, g in once:
            r = res.get(gid)
            if r and r.get("by") != uid:
                return g
        for gid, _g in once:
            res[gid] = {"by": uid, "at": t}
        _res_write(res, h)
    return None


def release(payload, h=None):
    """A call that will not run (denied): give back what it reserved."""
    uid = payload.get("tool_use_id") if isinstance(payload, dict) else None
    if not isinstance(uid, str) or not uid:
        return
    with _res_lock(h) as held:
        if held:
            res = _res_read(h)
            keep = {k: v for k, v in res.items() if not (isinstance(v, dict) and v.get("by") == uid)}
            if keep != res:
                _res_write(keep, h)


def gate(payload, h=None, now=None):
    """PreToolUse. A call that an ENDED grant used to cover: take the rule out, and if nothing
    else allows the call, make Claude Code ask. -> a hook output dict, or None. Never "allow".
    First, a `once` grant another running call has reserved is not available to this one."""
    call = _payload_call(payload)
    if not call:
        return None
    t = _now(now)
    busy = reserve(payload, h, now)
    if busy:
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": "the once grant for %s is already in use by another call that "
            "has not finished; a `once` covers one command. Run this one after it, or grant it again."
            % busy.get("pattern")}}
    gone = [(gid, g) for gid, g in ld.grants(h).items()
            if not ld.grant_active(g, t) and rule_matches(g["rule"], *call)]
    if not gone:
        return None
    for gid, g in gone:
        ld.grant_remove(gid, h, why="expired" if (g.get("expires_at") is not None
                                                  and t >= g["expires_at"]) else "spent")
    if any(rule_matches(r, *call) for r in _allow_rules(h)):
        return None
    g = gone[0][1]
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "ask",
        "permissionDecisionReason": "the gt grant for %s has %s; this needs a new yes"
        % (g.get("pattern"), "expired" if g.get("kind") != "once" else "been used")}}


def _suggest_pattern(call):
    tool, ti = call
    if tool == "Bash":
        cmd = " ".join(((ti.get("command") or "")).split())
        if cmd and len(cmd) <= 80:
            return cmd
        w = cmd.split(" ")[0] if cmd else ""
        return (w[:60] + "*") if w else ""
    return tool


def denied_guidance(payload, h=None):
    """PermissionDenied: the user's way out, in one message. -> text or None."""
    call = _payload_call(payload)
    if not call:
        return None
    tool, ti = call
    pat = _suggest_pattern(call)
    low = None
    for lvl in ld.LEVELS[1:]:
        if any(rule_matches(r, tool, ti) for r in ld.rules_for(lvl, h)["allow"]):
            low = lvl
            break
    lines = ["Claude Code refused %s%s. Only you can lift that; Claude cannot and should not try "
             "another route." % (tool, (" `%s`" % pat) if tool == "Bash" and pat else ""),
             "  one time or for a while (run in your terminal):",
             "    python3 ~/.claude/golden-thread/hooks/gt_grant.py add \"%s\" once" % pat,
             "    python3 ~/.claude/golden-thread/hooks/gt_grant.py add \"%s\" 30m   (or 2h, or forever)"
             % pat]
    if low:
        lines.append("  or for everything like it: python3 ~/.claude/golden-thread/hooks/"
                     "gt_settings.py set lockdown %s   (/gt:gt-settings lockdown)" % low)
    lines.append("  /gt:gt-grant explains each choice; /gt:gt-grant list shows what is live.")
    return "\n".join(lines)


# ------------------------------------------------------------------ CLI and hooks
def _unlock_on():
    try:
        import gt_unlock_policy
        return bool(gt_unlock_policy.enabled_fast())
    except Exception:                            # noqa: BLE001
        return False


def terminal_refusal(has_terminal=None):
    """-> the refusal text when no person is at a terminal, else None. No environment variable
    stands in for a person (phase 108): a test passes `has_terminal` or uses a pseudo-terminal."""
    if (has_terminal or gt_tty.has_terminal)(sys.stdin):
        return None
    return ("refused: a grant is yours to give, and there is no terminal here. Run this "
            "in your own terminal.")


def _real_settings():
    """True when this process would write the invoking account's own ~/.claude/settings.json, the one
    Claude Code reads. A scratch HOME (the tests, or a shell trying to dodge the check) edits a file no
    session reads, so no challenge is made there."""
    try:
        import pwd
        home = pwd.getpwuid(os.getuid()).pw_dir
    except Exception:                            # noqa: BLE001 -- Windows has no pwd
        return False
    return os.path.realpath(os.path.expanduser("~")) == os.path.realpath(home)


def typed_challenge(reason):
    """-> None when the person at the terminal typed back the code shown there, else the refusal text.
    With gt unlock off, isatty() alone was the whole check and `script -q /dev/null ...` satisfied it
    (validation F2). The code is shown on, and read from, /dev/tty, so a bare wrapper that cannot read
    its own output fails. LIMIT, stated in SECURITY.md: a program that drives a pseudo-terminal and
    answers can still pass; only gt unlock's enrolled factors prove a person."""
    import random
    code = "%04d" % random.SystemRandom().randrange(10000)
    try:
        fd = os.open("/dev/tty", os.O_RDWR | getattr(os, "O_BINARY", 0))
    except OSError:
        return "refused: a grant needs a terminal it can show a code on; run this in your own terminal."
    try:
        os.write(fd, ("\ngt grant: %s\nType %s to confirm (anything else cancels): " % (reason, code)).encode())
        buf = b""
        while b"\n" not in buf:
            chunk = os.read(fd, 64)
            if not chunk:
                break
            buf += chunk
    finally:
        os.close(fd)
    return None if buf.decode("utf-8", "replace").strip() == code else "refused: the code did not match; nothing changed."


def _confirm(reason):
    """-> None when the user may proceed. gt unlock, when on, wants a fresh confirmation."""
    cli = os.environ.get("GT_GRANT_UNLOCK") or os.path.join(HERE, "gt_unlock.py")
    if not os.path.isfile(cli):
        return None
    rc = subprocess.run([sys.executable, cli, "check", "--scope", "gt:settings:security",
                         "--request", "--reason", reason],
                        capture_output=True, text=True).returncode \
        if not cli.endswith(".sh") else subprocess.run(
            [cli, "check", "--scope", "gt:settings:security", "--request", "--reason", reason],
            capture_output=True, text=True).returncode
    return None if rc == 0 else rc


def _read_payload():
    try:
        d = json.loads(sys.stdin.read() or "")
        return d if isinstance(d, dict) else None
    except ValueError:
        return None


def _no_grants():
    """True when there is certainly no grant on this machine (state absent or without grants)."""
    try:
        p = os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "lockdown",
                         "state.json")
        with open(p, encoding="utf-8") as fh:
            return '"grants"' not in fh.read()
    except OSError:
        return True


def _hook(fn):
    """Run a hook body: any exception is silence (fail open)."""
    try:
        out = fn()
        if out:
            sys.stdout.write(json.dumps(out) + "\n")
    except Exception:                                    # noqa: BLE001
        pass
    return 0


def main(argv=None, has_terminal=None):
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if a else 2
    cmd, rest = a[0], [x for x in a[1:] if x != "--hook"]
    if cmd in ("gate", "spend") and _no_grants():
        return 0
    if cmd == "gate":
        return _hook(lambda: gate(_read_payload()))
    if cmd == "spend":
        return _hook(lambda: spend(_read_payload()) and None)
    if cmd == "denied":
        payload = _read_payload()
        try:
            release(payload)                             # a denied call gives back its reserved once
        except Exception:                                # noqa: BLE001 - fail open
            pass

        def _denied():
            msg = denied_guidance(payload)
            return {"systemMessage": msg,
                    "hookSpecificOutput": {"hookEventName": "PermissionDenied",
                                           "additionalContext": "The user was shown how to "
                                           "grant this. Ask them; do not look for a way round "
                                           "the refusal."}} if msg else None
        return _hook(_denied)
    if cmd == "sweep" and "--hook" in a:
        def _sw():
            rep = sweep()
            parts = (["gt grants: ended %d (expired or spent)" % len(rep["ended"])]
                     if rep["ended"] else []) + list(rep.get("testing") or [])
            return {"systemMessage": "; ".join(parts)} if parts else None
        return _hook(_sw)
    try:
        if cmd == "add":
            if len(rest) != 2:
                print("usage: gt_grant.py add PATTERN once|<N>m|<N>h|forever", file=sys.stderr)
                return 2
            parse_duration(rest[1])
            refusal = terminal_refusal(has_terminal)
            if refusal:
                print(refusal)
                return 1
            rule = pattern_to_rule(rest[0])
            if has_terminal is None and _real_settings() and not _unlock_on():
                bad = typed_challenge("grant %s %s" % (rule, rest[1]))
                if bad:
                    print(bad)
                    return 1
            refused = _confirm("grant %s %s" % (rule, rest[1]))
            if refused is not None:
                print("refused: a grant needs a fresh confirmation (gt unlock exit %s); nothing "
                      "changed." % refused)
                return 1
            import gt_action_token as AT
            g = add(rest[0], rest[1], token=AT.token_from_env())
            print("granted %s: %s (%s)" % (g["id"], g["rule"],
                  "until %s" % time.strftime("%H:%M", time.localtime(g["expires_at"]))
                  if g["kind"] == "timed" else g["kind"]))
            print("settings.json backed up first. Claude Code applies the rule when it reloads "
                  "settings; restart it if it still asks.")
            return 0
        if cmd == "list":
            print(format_list(list_grants()))
            return 0
        if cmd == "revoke":
            if len(rest) != 1:
                print("usage: gt_grant.py revoke ID", file=sys.stderr)
                return 2
            g = revoke(rest[0])
            print("revoked %s: %s" % (g["id"], g["rule"]))
            return 0
        if cmd == "sweep":
            rep = sweep()
            print("ended %d grant(s)" % len(rep["ended"]))
            for m in rep.get("testing") or []:
                print(m)
            return 0
    except KeyError:
        print("no such grant: %s (gt_grant.py list)" % rest[0], file=sys.stderr)
        return 1
    except ValueError as e:
        print("refused: %s" % e)
        return 1
    except ld.LockdownError as e:
        print("refused: %s" % e)
        return 1
    print("usage: gt_grant.py add|list|revoke|sweep|gate|spend|denied", file=sys.stderr)
    return 2


if __name__ == "__main__":
    # paranoid.signed_manifest (0.21.0): when gt's code is sealed, a hook checks its own files
    # first (gt_integrity_gate: one stat() when the group is off). Only as a hook -- the CLI
    # forms of this script stay usable, and are the repair route.
    try:
        import gt_integrity_gate as _gig
    except ImportError:
        _gig = None
    if _gig is not None:
        _gig.check(__file__, "guard" if sys.argv[1:2] == ["gate"] else "reporter", when="--hook" in sys.argv)
    sys.exit(main())
