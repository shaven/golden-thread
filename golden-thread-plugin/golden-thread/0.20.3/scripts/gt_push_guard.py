#!/usr/bin/env python3
"""gt_push_guard.py -- a fingerprint before every push (gt 0.20.3, owner 2026-10-06).

The setting `push_fingerprint` (gt_settings.py) drives this; nothing else should. One switch
instead of four manual steps:

  on     needs one enrolled factor (Touch ID / Windows Hello / an authenticator: the one human
         step, by design). Turns gt unlock on in a PUSH-ONLY profile -- every scope `open`
         except gt:publish (push / tag / release) and the policy and enrollment scopes, which
         need a fresh confirmation -- and installs a git pre-push hook in each repo listed in
         `push_fingerprint_repos`. The policy that was in force is saved, for `off`.
  off    removes exactly the hooks it installed and restores the saved policy.
  check  proves it: unlock on, gt:publish at step-up, every listed repo carrying our hook.
  hook   what the pre-push hook runs. Docs gate first (a repo that ships dev/release-check.sh
         runs it --quick before a push of main or a tag), then
         `gt_unlock.py check --scope gt:publish --request`: the push goes only on exit 0.

Honest limit: a git hook is a guard against accidents, not a lock -- `git push --no-verify`
skips it, and a process running as you can edit it. The lock is a push credential sealed
behind gt:publish; that is the next step, not this one (SECURITY.md).

State: ~/.claude/golden-thread/push-guard.json  {"repos": [...], "saved_policy": {...}}.
Exit codes: 0 ok, 1 refused / check failed, 2 usage, 3 no factor enrolled (enroll first).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

MARK = "# gt push_fingerprint"
HERE = os.path.dirname(os.path.abspath(__file__))
STEP_UP = ("gt:publish", "gt:unlock:policy", "gt:unlock:enroll")


def _home():
    return os.path.join(os.path.expanduser("~"), ".claude", "golden-thread")


def _state_path():
    return os.path.join(_home(), "push-guard.json")


def _load_state():
    try:
        with open(_state_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(d):
    os.makedirs(_home(), exist_ok=True)
    tmp = _state_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, _state_path())


def _unlock_cli():
    return os.environ.get("GT_PUSH_GUARD_UNLOCK") or os.path.join(HERE, "gt_unlock.py")


def _unlock(*args, capture=True):
    p = subprocess.run([sys.executable, _unlock_cli()] + list(args),
                       capture_output=capture, text=True)
    return p.returncode, (p.stdout or "") if capture else ""


def _status():
    rc, out = _unlock("status", "--json")
    try:
        return json.loads(out)
    except ValueError:
        return {}


def _enrolled(st):
    f = st.get("factors") or {}
    return [n for n, v in f.items() if isinstance(v, dict) and v.get("enrolled")]


def _policy():
    rc, out = _unlock("policy", "show", "--json")
    try:
        return json.loads(out)
    except ValueError:
        return {}


def _setting(name, default="off"):
    cfg = os.path.join(os.path.expanduser("~"), ".claude", "vault-config.json")
    try:
        with open(cfg, encoding="utf-8") as f:
            return json.load(f).get(name, default)
    except (OSError, ValueError):
        return default


def _settings_repos():
    cfg = os.path.join(os.path.expanduser("~"), ".claude", "vault-config.json")
    try:
        with open(cfg, encoding="utf-8") as f:
            raw = json.load(f).get("push_fingerprint_repos") or ""
    except (OSError, ValueError):
        raw = ""
    return [os.path.abspath(os.path.expanduser(r.strip())) for r in raw.split(",") if r.strip()]


def _hooks_dir(repo):
    p = subprocess.run(["git", "-C", repo, "rev-parse", "--path-format=absolute",
                        "--git-common-dir"], capture_output=True, text=True)
    if p.returncode != 0:
        return None
    return os.path.join(p.stdout.strip(), "hooks")


def _hook_text():
    # the installed copy beside gt_unlock.py is stable across upgrades; the cache path is not
    stable = os.path.join(_home(), "hooks", "gt_push_guard.py")
    guard = stable if os.path.isfile(stable) else os.path.join(HERE, "gt_push_guard.py")
    return ("#!/bin/sh\n%s (installed by gt_push_guard.py; remove with: "
            "gt_settings.py set push_fingerprint off)\n"
            "exec \"%s\" -I \"%s\" hook \"$@\"\n" % (MARK, sys.executable, guard))


def _ours(path):
    try:
        with open(path, encoding="utf-8") as f:
            return MARK in f.read()
    except OSError:
        return False


def _usable(st):
    f = st.get("factors") or {}
    return [n for n in ("touchid", "hello", "sso", "totp")
            if (f.get(n) or {}).get("enrolled") and (f.get(n) or {}).get("available", True)]


def push_only_policy(current, usable=None):
    """The policy `on` writes: the current user policy, enabled, with every scope open except
    the ones a push and the policy itself need a fresh confirmation for."""
    p = dict(current or {})
    p.setdefault("schema", 1)
    p["enabled"] = True
    scopes = {}
    for name in (p.get("scopes") or {}):
        scopes[name] = "open"
    for name in ("gt:hub:enroll", "gt:lock:*", "gt:secrets", "gt:settings:hooks",
                 "gt:settings:security", "gt:vault:read", "gt:vault:write",
                 "lotr:*:read", "lotr:*:write", "lotr:*:consent"):
        scopes.setdefault(name, "open")
    for name in STEP_UP:
        scopes[name] = "step_up"
    p["scopes"] = scopes
    # every step-up a FRESH touch (live test 2026-10-06: the default fresh_s=60 let a second push
    # 26 s after the first through on the earlier touch, with no dialog)
    p["step_up"] = dict(p.get("step_up") or {}, fresh_s=0)
    # K must be reachable with what is enrolled here (live test 2026-10-06: the default K=2 with only
    # Touch ID enrolled refused the policy). Record the choice the way `policy enable --factors`
    # does -- never lowering a K the owner set that the enrolled factors can already meet.
    usable = list(usable or [])
    f = dict(p.get("factors") or {})
    if usable and int(f.get("required") or 2) > len(usable):
        import time as _t
        f["required"] = len(usable)
        f["require_one_of"] = [n for n in usable if n in ("touchid", "hello")][:1]
        f["chosen"] = {"factors": usable, "by": "push_fingerprint",
                       "at": _t.strftime("%Y-%m-%dT%H:%M:%S%z")}
        p["factors"] = f
    return p


def _set_policy(p):
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(p, f)
        rc, _ = _unlock("policy", "set", path, capture=False)
        return rc
    finally:
        os.unlink(path)


# ---------------------------------------------------------------- the sealed push token (0.20.3 part 2) ----
# The lock behind the hook: the GitHub token lives in gt unlock's sealed store, and git gets it only
# through gt_unlock.py git-credential, which releases it under a gt:publish grant -- so a push from a
# guarded repo needs the fingerprint even with --no-verify. The value is piped from `gh auth token`
# straight into `seal put`; nothing here reads, prints or logs it (Core rule 3).
CRED_KEY = "credential.https://github.com.helper"
GITHUB_MAP = {"protocol": "https", "host": "github.com", "username": "x-access-token",
              "ref": "sealed:github"}


def _unlock_home():
    if os.environ.get("GT_PUSH_GUARD_UNLOCK_HOME"):
        return os.environ["GT_PUSH_GUARD_UNLOCK_HOME"]
    sys.path.insert(0, HERE)
    import gt_unlock_client                                    # noqa: PLC0415
    return gt_unlock_client.home()


def _gh():
    env = os.environ.get("GT_PUSH_GUARD_GH")
    if env is not None:
        return env if os.path.isfile(env) else None
    return shutil.which("gh")


def seal_token():
    """-> (sealed?, message). Pipes gh's token into the sealed store; never sees the value."""
    gh = _gh()
    if not gh:
        return False, "gh is not installed: no GitHub token to seal (the hook still asks)"
    src = subprocess.Popen([gh, "auth", "token"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    dst = subprocess.run([sys.executable, _unlock_cli(), "seal", "put", "--name", "github"],
                         stdin=src.stdout, capture_output=True, text=True)
    src.stdout.close()
    src_rc = src.wait()
    if src_rc != 0 or dst.returncode != 0:
        return False, "sealing the GitHub token failed (gh %s, seal %s); nothing was mapped" % (
            src_rc, dst.returncode)
    return True, "GitHub token sealed (sealed:github); git gets it only under gt:publish"


def _cred_path():
    return os.path.join(_unlock_home(), "credentials.json")


def map_github(state):
    path = _cred_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    state.setdefault("saved_credentials", data if data else None)
    rows = [m for m in (data.get("git") or []) if m.get("host") != "github.com"]
    data["git"] = rows + [dict(GITHUB_MAP)]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o600)


def unmap_github(state):
    path = _cred_path()
    saved = state.get("saved_credentials")
    if saved:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(saved, f, indent=2)
    elif os.path.exists(path):
        os.unlink(path)


def _helper_cmd():
    stable = os.path.join(_home(), "hooks", "gt_unlock.py")
    cli = stable if os.path.isfile(stable) else _unlock_cli()
    return '!"%s" "%s" git-credential' % (sys.executable.replace(os.sep, "/"),
                                          cli.replace(os.sep, "/"))


def set_helper(repo):
    # an empty value resets git's helper list (dropping gh's global helper for THIS repo only)
    subprocess.run(["git", "-C", repo, "config", "--local", "--replace-all", CRED_KEY, ""], check=True)
    subprocess.run(["git", "-C", repo, "config", "--local", "--add", CRED_KEY, _helper_cmd()], check=True)


def unset_helper(repo):
    subprocess.run(["git", "-C", repo, "config", "--local", "--unset-all", CRED_KEY],
                   capture_output=True)


def helper_guards(repo, url):
    if not (url or "").startswith("https://github.com"):
        return False
    p = subprocess.run(["git", "-C", repo, "config", "--local", "--get-all", CRED_KEY],
                       capture_output=True, text=True)
    return "git-credential" in (p.stdout or "")


def cmd_on(repos):
    repos = repos or _settings_repos()
    if not repos:
        print("refused: no repository to guard. Name it first:\n"
              "  gt_settings.py set push_fingerprint_repos /path/to/repo[,/another]")
        return 1
    st = _status()
    if not _enrolled(st):
        print("one step first (it needs you, by design): enroll a factor, then run this again\n"
              "  %s %s enroll touchid      (Windows: hello; no biometrics: totp)"
              % (sys.executable, _unlock_cli()))
        return 3
    bad = []
    for r in repos:
        hd = _hooks_dir(r)
        if not hd:
            bad.append("%s is not a git repository" % r)
        elif os.path.exists(os.path.join(hd, "pre-push")) and not _ours(os.path.join(hd, "pre-push")):
            bad.append("%s already has its own pre-push hook (left alone)" % r)
    if bad:
        print("refused, nothing changed:\n  " + "\n  ".join(bad))
        return 1
    state = _load_state()
    if "saved_policy" not in state:
        state["saved_policy"] = (_policy().get("user") or {})
    if _set_policy(push_only_policy(state["saved_policy"], _usable(st))) != 0:
        print("refused: gt unlock did not accept the push-only policy; nothing else changed")
        return 1
    for r in repos:
        hd = _hooks_dir(r)
        os.makedirs(hd, exist_ok=True)
        path = os.path.join(hd, "pre-push")
        with open(path, "w", encoding="utf-8") as f:
            f.write(_hook_text())
        os.chmod(path, 0o755)
        print("pre-push hook installed: %s" % r)
    # owner 2026-10-06 (option 2): sealing the token is OPT-IN. gt unlock serves secrets only to its
    # MCP shim, so it must run from the owner's own terminal, and once sealed an assistant session
    # can no longer push. Default: the fingerprint hook only.
    if _setting("push_fingerprint_seal_token") == "on":
        sealed, msg = seal_token()
        print(msg)
        if sealed:
            map_github(state)
            state["sealed_by_guard"] = True
            for r in repos:
                set_helper(r)
                print("git credential helper set (gt:publish): %s" % r)
    state["repos"] = sorted(set((state.get("repos") or []) + repos))
    _save_state(state)
    print("push_fingerprint on: a push from these repos needs a fresh confirmation; "
          "everything else in gt unlock stays open")
    return 0


def cmd_off():
    state = _load_state()
    for r in state.get("repos") or []:
        hd = _hooks_dir(r)
        path = os.path.join(hd, "pre-push") if hd else None
        if path and _ours(path):
            os.unlink(path)
            print("pre-push hook removed: %s" % r)
        if hd:
            unset_helper(r)
    if state.get("sealed_by_guard"):
        unmap_github(state)
        _unlock("seal", "rm", "github")
        print("GitHub token mapping removed and the sealed copy deleted (gh keeps its own)")
    if "saved_policy" in state:
        saved = state["saved_policy"] or {"schema": 1, "enabled": False}
        if _set_policy(saved) != 0:
            print("the hooks are removed, but restoring the saved unlock policy failed; "
                  "state kept in %s" % _state_path())
            state["repos"] = []
            _save_state(state)
            return 1
    try:
        os.unlink(_state_path())
    except OSError:
        pass
    print("push_fingerprint off: hooks removed, the earlier unlock policy restored")
    return 0


def cmd_check():
    state, ok = _load_state(), True
    repos = state.get("repos") or []
    st = _status()
    lines = [("unlock on", bool(st.get("enabled")))]
    eff = (_policy().get("effective") or {}).get("scopes") or {}
    lines.append(("gt:publish needs a fresh confirmation", eff.get("gt:publish") == "step_up"))
    lines.append(("at least one guarded repo", bool(repos)))
    for r in repos:
        hd = _hooks_dir(r)
        lines.append(("hook in %s" % r, bool(hd) and _ours(os.path.join(hd, "pre-push"))))
    for what, good in lines:
        print("%s  %s" % ("PASS" if good else "FAIL", what))
        ok = ok and good
    return 0 if ok else 1


def cmd_hook(argv):
    updates = [l.split() for l in sys.stdin.read().splitlines() if l.strip()]
    if not updates:
        return 0
    if not _status().get("enabled"):
        sys.stderr.write("push refused: push_fingerprint is on but gt unlock is off. Turn it back "
                         "on (gt_settings.py set push_fingerprint on) or switch the guard off.\n")
        return 1
    refs = [u[2] for u in updates if len(u) >= 4]
    release_push = any(r == "refs/heads/main" or r.startswith("refs/tags/") for r in refs)
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                         text=True).stdout.strip()
    rc_script = os.path.join(top, "golden-thread-plugin", "dev", "release-check.sh")
    if release_push and top and os.path.isfile(rc_script):
        sys.stderr.write("push_fingerprint: docs gate (release-check --quick) ...\n")
        if subprocess.call(["bash", rc_script, "--quick"], cwd=os.path.dirname(os.path.dirname(rc_script)),
                           stdout=subprocess.DEVNULL) != 0:
            sys.stderr.write("push refused: release-check --quick failed; run it to see why\n")
            return 1
    remote = argv[0] if argv else "?"
    if top and len(argv) > 1 and helper_guards(top, argv[1]):
        return 0      # the credential helper asks for gt:publish when git needs the token
    reason = "push %s to %s" % (", ".join(r.replace("refs/heads/", "").replace("refs/", "")
                                          for r in refs) or "?", remote)
    rc, _ = _unlock("check", "--scope", "gt:publish", "--request", "--reason", reason,
                    capture=False)
    if rc != 0:
        sys.stderr.write("push refused: no confirmation for gt:publish\n")
        return 1
    return 0


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if a else 2
    cmd, rest = a[0], a[1:]
    if cmd == "on":
        return cmd_on([os.path.abspath(r) for r in rest if not r.startswith("-")])
    if cmd == "off":
        return cmd_off()
    if cmd == "check":
        return cmd_check()
    if cmd == "hook":
        return cmd_hook(rest)
    print("usage: gt_push_guard.py on [REPO ...] | off | check | hook REMOTE URL")
    return 2


if __name__ == "__main__":
    sys.exit(main())
