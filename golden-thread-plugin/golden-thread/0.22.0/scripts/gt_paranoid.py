#!/usr/bin/env python3
"""gt_paranoid -- the `paranoid` settings group: what it contains, which parts are on, and what
each part means in everyday words (0.21.0, request 2026-10-08-installed-code-integrity).

One master switch and one switch per part, grouped behind the master:

  paranoid                    master: off | on. `on` turns every part on unless the part is set
  paranoid.os_lock            1. installed gt code is locked at the operating-system level
  paranoid.code_write_gate    2. a presence check (gate `gt-code-write`) before it is unlocked
  paranoid.bash_guard         3. a shell command that writes to that code is stopped and asked about
  paranoid.signed_manifest    4. a signed (or pinned) list of the code is checked before each hook
  paranoid.plugin_dirs        5. the plugin cache and marketplace folders count as protected paths
  paranoid.require_unlock     refuses to turn `paranoid` on until gt unlock (Touch ID etc.) is set up

  paranoid.action_tokens      single-use action tokens (item #32): the group's own master, and
  paranoid.action_tokens.<action>   one switch per action that can spend a token (ACTIONS below)

RESOLUTION. A part that is not set follows its parent, which follows ITS parent, up to the master;
the master defaults to off. Parts are named by dots, so a part that a later item adds
(`paranoid.action_tokens`, `paranoid.action_tokens.disk_read` -- item #32) needs no change here:
its parent chain is its own name. The stored value of a part is `on`, `off` or `follow` (or
absent, the same as `follow`); anything else counts as unset.

EVERY KEY IS A SECURITY SETTING: `is_paranoid_key` is what gt_settings uses to put the whole group
behind the same step-up as `lockdown`, so a key added later is gated without a list edit.

Standard library only, no imports at module level beyond json/os: hooks and guards read this on
their hot path, and with the group off the cost is one small JSON read.
"""
import json
import os

MASTER = "paranoid"
GROUP = "Paranoid (installed-code integrity)"
CONFIG = os.path.join("~", ".claude", "vault-config.json")
# written by gt_integrity.converge beside the lock, OS-locked with the code: which parts were on
SEALED = "paranoid-sealed.json"

# (key, part number or None, short name) in display order. Registered by gt_settings from this.
PARTS = (
    ("paranoid.os_lock", 1, "OS lock on installed gt files"),
    ("paranoid.code_write_gate", 2, "presence check before the code is unlocked"),
    ("paranoid.bash_guard", 3, "guard for shell commands that write to that code"),
    ("paranoid.signed_manifest", 4, "signed list of the code, checked before each hook runs"),
    ("paranoid.plugin_dirs", 5, "plugin cache and marketplace under protected paths"),
    ("paranoid.require_unlock", None, "refuse to turn paranoid on until unlock is set up"),
)

# -- single-use action tokens (0.21.0, item #32) ------------------------------------------------
# One switch per action that can spend a token. `paranoid.action_tokens` is the group's own master
# (it follows `paranoid`); each action follows it unless set. `wired` says whether a chokepoint in
# THIS build asks for the token: a switch whose chokepoint is not yet wired says so in `explain`
# instead of promising a check nobody makes. gt_action_token.py is the one shared check.
ACTION_MASTER = "paranoid.action_tokens"
ACTION_PREFIX = ACTION_MASTER + "."
# (name, what one token covers, where it is checked, wired, mint target looks like)
ACTIONS = (
    ("disk_read", "one disk_read of one file", "gt_io_broker (the gt-vault disk_read tool)", True,
     "the file's full path"),
    ("disk_list", "one disk_list of one folder", "gt_io_broker (the gt-vault disk_list tool)",
     True, "the folder's full path"),
    ("net_fetch", "one net_fetch of one URL", "gt_io_broker (the gt-vault net_fetch tool)", True,
     "the URL"),
    ("disk_write", "one disk_write of exact content to one path",
     "gt_io_broker (the gt-vault disk_write tool)", True,
     "the file's full path, plus the fingerprint of the exact text"),
    ("lotr_read", "one LOTR call_read (connection + op + args)", "the LOTR gateway", True,
     "connection/op, plus the fingerprint of the args"),
    ("lotr_write", "one LOTR call_write", "the LOTR gateway", True,
     "connection/op, plus the fingerprint of the args"),
    ("lotr_consent", "one LOTR call_consent (consent is still asked as well)",
     "the LOTR gateway", True, "connection/op, plus the fingerprint of the args"),
    ("vault_read", "one gt-vault vault_read, vault_search or vault_list",
     "gt_vault_mcp (the gt-vault server)", True,
     "the tool and its path or query, e.g. vault_read:Knowledge/Page.md"),
    ("vault_write", "one queued vault write or batch, or one vault operation tool call",
     "gt_vault_mcp (vault_queue_write), gt_vault_ops (the vault operation tools) and the "
     "gt_write_queue.py command", True,
     "vault_queue_write:<path>, or <tool>:<action> for an operation tool, plus the fingerprint "
     "of what is written"),
    ("secrets_unseal", "one secret unseal through gt unlock",
     "gt_unlockd (the secret method, sealed refs)", True, "the secret's name"),
    ("git_push", "one git push (repo + refs)",
     "gt_push_guard's pre-push hook (the token comes from the GT_ACTION_TOKEN environment variable)",
     True, "the repo and refs: mint git_push <repo> --refs refs/heads/main"),
    ("git_commit", "one git commit (repo + tree hash)",
     "gt_sign's signing step, else the vault pre-commit hook (the token comes from the "
     "GT_ACTION_TOKEN environment variable)", True,
     "the repo and its staged tree: mint git_commit <repo>"),
    ("publish", "one release publish step", "dev/publish.sh (the outward steps)", True,
     "the step: gt-src, or announced when release_announce is post"),
    ("grant_add", "creating one timed grant", "gt_grant (add)", True, "the grant's pattern"),
    ("plan_bound", "one confirmation that mints an approved plan's tokens up front",
     "gt_action_token.py mint-plan (gt-plan / gt-implement)", True, "the plan's action list"),
)
ACTION_NAMES = tuple(a[0] for a in ACTIONS)
ACTION_KEYS = tuple(ACTION_PREFIX + a for a in ACTION_NAMES)
ACTIONS_BY_NAME = {a[0]: {"name": a[0], "covers": a[1], "chokepoint": a[2], "wired": a[3],
                          "target": a[4]} for a in ACTIONS}

KEYS = (MASTER,) + tuple(k for k, _n, _t in PARTS) + (ACTION_MASTER,) + ACTION_KEYS

# Plain-language text, one entry per key. This is the ONE place the words live: `explain` in
# gt_settings prints it, and the installer questions (item #33) reuse it through explain_text().
# Each says who it is for, what it protects against and what it costs, with jargon glossed in
# the same sentence it appears in.
TEXT = {
    "paranoid": {
        "title": "Paranoid: protect gt's own installed code from being changed behind your back",
        "who": ("Anyone who lets an AI assistant run commands on their machine and wants to be "
                "sure it cannot quietly rewrite the programs that keep it in check. gt's guards "
                "and Core-rule hooks are ordinary files you own; without this, anything running "
                "as you can edit them."),
        "protects": ("An assistant (or something it was tricked into running) editing the hooks "
                     "that guard it, adding code next to them, or swapping the plugin copies "
                     "Claude Code runs skill scripts from. Without this the change is noticed "
                     "only at the next session start, after the changed code has run."),
        "costs": ("Upgrades get slower and ask you once. The files are locked, so an upgrade has "
                  "to unlock them first: one Touch ID (or one typed confirmation) to unlock, and, "
                  "if your code list is signed, one more to sign the new list. Some shell "
                  "commands that touch gt's folders now stop and ask. Hooks do a tiny extra check "
                  "(about a tenth of a millisecond when nothing changed). Off by default."),
        "parts": ("Six parts sit behind this switch. `on` turns on every part you have not set "
                  "yourself; a part you set `off` stays off, and then the status line reads "
                  "`paranoid (partial)` and names it. Turning a part off while paranoid is on "
                  "is allowed, needs the same confirmation as any security setting, and is "
                  "written to a log."),
    },
    "paranoid.os_lock": {
        "title": "OS lock: gt's installed files cannot be changed or deleted",
        "who": ("Everyone who turns paranoid on. This is the part that does the real work: the "
                "operating system itself refuses the write, whatever program tries it."),
        "protects": ("Edits made by the assistant's Write and Edit tools, by `sed -i`, by "
                     "`cp`, by a Python one-liner -- every route. On a Mac the files get the "
                     "`uchg` flag (user-immutable: nothing may change or remove them until the "
                     "flag is cleared); on Linux the write permission is removed; on Windows the "
                     "read-only attribute plus a rule that denies writing."),
        "costs": ("Changing gt's own files (an upgrade, a repair) means unlocking first, which "
                  "asks for your confirmation. Only code is locked: gt's records, settings and "
                  "logs stay writable. gt compiles its Python once at install, before locking, "
                  "because a locked folder cannot store the compiled copies. Honest limit: you "
                  "can clear the flag yourself, so this stops an assistant, not you."),
    },
    "paranoid.code_write_gate": {
        "title": "Presence check: a person must be there to unlock the code",
        "who": ("People who want the unlock step itself protected, so an assistant cannot unlock "
                "the files and then change them."),
        "protects": ("Unlocking for an upgrade or a repair without you. With gt unlock set up it "
                     "is a fresh Touch ID, Windows Hello or authenticator-app check (the gate "
                     "`gt-code-write`). Without it, you type a confirmation at a real terminal "
                     "-- and gt says plainly that this is weaker, because a program that can open "
                     "a terminal could type it."),
        "costs": ("One confirmation per upgrade or repair. A repair that gt would otherwise do by "
                  "itself at session start (replacing a drifted file) now waits for you instead "
                  "and says which command to run."),
    },
    "paranoid.bash_guard": {
        "title": "Shell guard: commands that write to gt's code stop and ask first",
        "who": ("Anyone who has the OS lock on and wants to see the attempt, not only have it "
                "fail. Also useful on its own when you cannot lock files."),
        "protects": ("A shell command that names gt's protected folders together with something "
                     "that writes (`>`, `tee`, `sed -i`, `cp`, `mv`, `rm`, `rsync`, an inline "
                     "`python -c`) or that tries to clear the lock (`chflags`, `chmod`, `chattr`, "
                     "`attrib`, `icacls`). Best effort by design: a clever command can be "
                     "written to slip past a pattern match, which is why the OS lock is the real "
                     "barrier and this makes the attempt visible."),
        "costs": ("An occasional extra prompt on shell commands that mention those folders. "
                  "Reading, listing and running gt's scripts never prompts. With gt unlock on, "
                  "the prompt needs a fresh confirmation."),
    },
    "paranoid.signed_manifest": {
        "title": "Signed code list: each hook checks its own files before it runs",
        "who": ("People who want tampering caught immediately, even when a file's lock was "
                "removed first -- the lock stops a change, this notices one."),
        "protects": ("A hook or a module it uses having been changed since install. Each hook "
                     "checks its own file and the gt modules it imports against a recorded list "
                     "(a manifest: a list of every file with its fingerprint). A guard whose "
                     "files changed refuses to run and blocks the action, naming the file; "
                     "other hooks print a warning naming the file and do not run. With gt unlock "
                     "set up the list is signed by your Touch ID / Windows Hello key; without it "
                     "the list is pinned instead -- stored, locked, but not signed, which is "
                     "weaker, and `status` says so."),
        "costs": ("About a tenth of a millisecond per hook when nothing changed (it remembers "
                  "each file's identity, so it does not re-read them). One extra Touch ID per "
                  "upgrade when the list is signed. If a file really is changed, hooks that "
                  "protect you will refuse until you repair it: the message names the command. "
                  "During an upgrade itself (about a minute, while the files are unlocked and "
                  "being replaced) your other open Claude Code sessions get those refusals too, "
                  "until it finishes."),
    },
    "paranoid.plugin_dirs": {
        "title": "Plugin folders: the cache and marketplace copies count as protected paths",
        "who": ("Everyone who turns paranoid on. Claude Code runs gt's skill scripts from these "
                "folders, not from the hooks folder, so they matter as much."),
        "protects": ("Edits to those copies by the assistant's Write and Edit tools. They get "
                     "the same prompt gt already gives for its hooks folder, and with gt unlock "
                     "on a fresh confirmation."),
        "costs": ("A prompt when something writes there, which normally only an upgrade does. "
                  "Claude Code's own plugin manager can be refused write access to those "
                  "folders while they are locked; unlock for upgrades, then relock."),
    },
    "paranoid.require_unlock": {
        "title": "Require unlock: do not turn paranoid on until gt unlock is set up",
        "who": ("People who want the strong version or nothing: with gt unlock (Touch ID, "
                "Windows Hello or an authenticator app) in place, the presence check and the "
                "signature are real."),
        "protects": ("Believing you are protected when the only confirmation is a typed line "
                     "an assistant with a terminal could in principle supply."),
        "costs": ("You set up gt unlock first (`gt_unlock.py`). If you want paranoid without "
                  "it, set this to `off` first; gt then uses the typed confirmation and the "
                  "pinned list, and says both are weaker."),
    },
}

# -- plain text for the action tokens ------------------------------------------------------------
_TOKEN_GLOSS = ("A token is a one-time permit you create yourself, with Touch ID (or a typed "
                "confirmation if gt unlock is not set up). It names one action, one target and an "
                "expiry a few minutes away, and it works at most once: using it a second time, on "
                "another target, with changed content, or after it expires is refused.")
_MINT = ("Create it in a terminal: `python3 ~/.claude/golden-thread/hooks/gt_action_token.py "
         "mint %s <target>`; the assistant is given only the token's id, which is useless for "
         "anything else.")
_NOT_YET = (" Not enforced yet: this switch is recorded and shown, but the check in %s is not "
            "made in this build, so turning it on changes nothing until that check is wired.")

_ACTION_WORDS = {
    "disk_read": (
        "Read one file off disk",
        "Anyone who lets an assistant read files outside the vault and wants each read approved, "
        "recorded and unrepeatable.",
        "A single approval being stretched to cover more files than you meant, or the same read "
        "being repeated later. Even files on the allow list need a token.",
        "One Touch ID per file read (no prompt for a file on the allow list: that token is "
        "created without asking). The assistant waits for you to create the token."),
    "disk_list": (
        "List one folder on disk",
        "Anyone who wants to know, and approve, which folders an assistant looks inside.",
        "An approval to list one folder turning into a tour of the whole disk.",
        "One Touch ID per folder listed (none for an allow-listed folder)."),
    "net_fetch": (
        "Fetch one web address",
        "Anyone who wants each network request an assistant makes to be approved and logged.",
        "A page the assistant was told to fetch being swapped for another address, or the same "
        "address being fetched again and again. The token names the exact URL.",
        "One Touch ID per fetch (none for a domain on the fetch allow list). Each redirect to a "
        "new address still goes through the broker's own check."),
    "disk_write": (
        "Write one file to disk",
        "Anyone who wants to see and approve each file change an assistant makes outside the "
        "vault. This is the most valuable switch in the group.",
        "The assistant writing different text from what you approved, to a different file, or "
        "writing twice. The token is tied to the file and to a fingerprint of the exact text.",
        "One Touch ID per write; the text must be known before the token is created. A timed "
        "window (cover=folder) is not available while this is on."),
    "lotr_read": (
        "Read once through the LOTR gateway",
        "Anyone who wants a record of who read what through GitHub, Jira or Microsoft 365, and "
        "each read approved.",
        "Reads being made quietly in bulk. Mostly an audit aid: it records each read against a "
        "request.",
        "One token per read call; every follow-up page of a read needs its own. The most "
        "frequent switch, so the most prompts."),
    "lotr_write": (
        "Change something once through the LOTR gateway",
        "Anyone who uses writer agents and wants the gateway, not the agent's own judgement, to "
        "hold it to the one change you asked for.",
        "A writer agent that was talked into another operation, other arguments, or a second "
        "call. The token is tied to the connection, the operation and a fingerprint of the "
        "arguments, so only that exact change goes through, once.",
        "One Touch ID per change you want made."),
    "lotr_consent": (
        "Approve one irreversible LOTR action",
        "Anyone who sends mail, merges or deletes through LOTR and wants those steps doubly "
        "approved.",
        "A send, merge or delete happening without the exact request having been approved up "
        "front. Consent is still asked as well, so this is a second, earlier check.",
        "A token and the usual consent for each irreversible action: two confirmations."),
    "vault_read": (
        "Read the vault once",
        "Anyone who keeps sensitive notes in the vault and wants each read approved.",
        "An assistant paging through the vault. One token covers one vault_read, one "
        "vault_search or one vault_list.",
        "One token per read or search: a long vault session asks many times. Off by default for "
        "that reason."),
    "vault_write": (
        "Write the vault once",
        "Anyone who wants each change to their notes approved before it is queued.",
        "A different note, or different text, being queued from the one you approved. The token "
        "is tied to the file and to a fingerprint of the text; a batch is one token for the "
        "whole batch.",
        "One token per queued write or batch. The write queue's own checks still apply after."),
    "secrets_unseal": (
        "Unseal one secret",
        "Anyone who stores credentials sealed by gt unlock and wants each use approved.",
        "A credential being used more times than you approved.",
        "One Touch ID per use. The unseal asks for the token: gt unlock run/secret commands read it "
        "from the GT_ACTION_TOKEN environment variable. Credentials the installed gt-lotr gateway "
        "opens for its own connections are exempt: each LOTR call is gated by its own lotr_* token."),
    "git_push": (
        "Push once",
        "Anyone who wants each push to a shared repository approved with its exact refs.",
        "A push of different refs than the ones you approved. The pre-push hook that "
        "push_fingerprint installs reads the token from the GT_ACTION_TOKEN environment variable "
        "(never stored), so there is no hook, and nothing enforced, in a repository where "
        "push_fingerprint is not on.",
        "One Touch ID per push, and the variable set for that one push."),
    "git_commit": (
        "Commit once",
        "Anyone who wants each commit approved against the exact files in it.",
        "A commit that contains more than what you reviewed. Checked when gt signs the commit "
        "(commit_fingerprint) and otherwise by the vault's pre-commit hook, which reads the token "
        "from the GT_ACTION_TOKEN environment variable (never stored); one commit spends one token.",
        "One Touch ID per commit, and the variable set for that one commit."),
    "publish": (
        "Publish once",
        "Anyone who releases software and wants each publishing step approved.",
        "A release step running without you, or running twice. dev/publish.sh asks for the token "
        "before the steps that reach outside the checkout (the gt-src sync, and the announcement "
        "when release_announce is post); a dry run asks for nothing.",
        "One Touch ID per publish step."),
    "grant_add": (
        "Create one timed grant",
        "Anyone who uses timed grants and wants each new window approved by name.",
        "An assistant opening itself a wider or longer window than you meant.",
        "One Touch ID per grant created."),
    "plan_bound": (
        "Approve a whole plan's tokens at once",
        "Anyone who turns on several of these switches and does not want a prompt per step of "
        "work they have already approved in a plan.",
        "Prompt fatigue: tokens for exactly the actions the plan lists are created together, "
        "under one confirmation, and an action the plan does not list still has no token.",
        "One Touch ID per approved plan instead of one per step. A plan's tokens expire together, "
        "so run the plan soon after approving it."),
}


def _add_action_text():
    TEXT[ACTION_MASTER] = {
        "title": "Action tokens: each listed action needs a single-use permit you create",
        "who": ("People who want every read, write or network action by an assistant approved, "
                "recorded and unrepeatable, on a machine that holds things they care about."),
        "protects": ("An approval being stretched to cover something it was not given for. "
                     + _TOKEN_GLOSS + " Each use is written to a log whose entries are chained "
                     "together with a secret key, so a deleted or edited entry is detected. The "
                     "assistant never sees anything it could reuse: only the token's id."),
        "costs": ("More prompts and slower assistants: this is for high-sensitivity machines. "
                  "Only actions that go through gt's brokers, the vault tools and LOTR can carry "
                  "tokens; Claude Code's own Read, Write, WebFetch and Bash cannot, so while "
                  "this is on, lockdown should deny those and send the assistant to the brokers "
                  "(the settings below do not do that for you). Each action below has its own "
                  "switch; the ones you leave unset follow this one, which follows `paranoid`."),
        "parts": ("Fifteen switches, one per action: " + ", ".join(ACTION_NAMES) + ". `on` "
                  "turns on every one you have not set yourself."),
    }
    for name in ACTION_NAMES:
        info = ACTIONS_BY_NAME[name]
        title, who, protects, costs = _ACTION_WORDS[name]
        costs = costs + " " + _MINT % name
        extra = "" if info["wired"] else _NOT_YET % info["chokepoint"]
        TEXT[ACTION_PREFIX + name] = {
            "title": "Token for %s: %s" % (name, title.lower()),
            "who": who,
            "protects": protects + " Covers: " + info["covers"] + ". Target: " + info["target"]
                        + ". Checked in: " + info["chokepoint"] + "." + extra,
            "costs": costs,
        }


_add_action_text()

LABELS = (("who", "Who it is for"), ("protects", "What it protects against"),
          ("costs", "What it costs"))


def is_paranoid_key(name):
    """True for the master and everything dotted under it, including keys later items add."""
    return isinstance(name, str) and (name == MASTER or name.startswith(MASTER + "."))


def read_config(path=None):
    """vault-config.json as a dict; {} when absent or unreadable. Never raises."""
    try:
        with open(os.path.expanduser(path or CONFIG), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:                                      # noqa: BLE001
        return {}


def raw(name, cfg=None):
    """The stored value: `on`, `off` or `follow` (unset or unrecognised)."""
    cfg = read_config() if cfg is None else cfg
    v = cfg.get(name)
    return v if isinstance(v, str) and v in ("on", "off") else "follow"


def effective(name, cfg=None):
    """Is `name` on? Explicit on/off wins; otherwise the parent decides; the master defaults off."""
    cfg = read_config() if cfg is None else cfg
    n = name
    while n:
        v = raw(n, cfg)
        if v != "follow":
            return v == "on"
        if n == MASTER:
            return False
        n = n.rpartition(".")[0] or MASTER
    return False


def sealed_parts(home=None):
    """{part key: was it on} as recorded when the code was last locked or sealed, or {} when it
    never was. The record is written by gt_integrity (an install, `relock`, `gt_settings.py set`)
    and locked with the code; editing vault-config.json by hand does not change it."""
    try:
        path = os.path.join(os.path.expanduser(home or "~"), ".claude", "golden-thread",
                            SEALED)
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        parts = d.get("parts") if isinstance(d, dict) else None
        return parts if isinstance(parts, dict) else {}
    except Exception:                                      # noqa: BLE001
        return {}


def enforced(name, cfg=None, home=None):
    """Is `name` on, going by the live config OR by what the sealed record says was on? The live
    config can switch a part on; only a person at a terminal (gt_settings.py set, which re-seals)
    can switch off what was sealed -- so a hand edit of vault-config.json cannot quietly relax it."""
    if effective(name, cfg):
        return True
    return bool(sealed_parts(home).get(name))


def state(cfg=None):
    """-> {label, on: [keys], off: [keys], master}. label is `off`, `on` or `paranoid (partial)`.

    `partial` is any mix of the five protective parts (1-5): the master on with a part off, or the
    master off with a part on. The `off` list names the parts that are off. `require_unlock` is a
    precondition and is left out of the mix."""
    cfg = read_config() if cfg is None else cfg
    # require_unlock is a precondition for turning the group on, not a protection: with unlock not
    # set up (the owner's Mac) it is off by design, and that must not read as `partial` forever
    on = [k for k, n, _t in PARTS if n is not None and effective(k, cfg)]
    off = [k for k, n, _t in PARTS if n is not None and not effective(k, cfg)]
    master = effective(MASTER, cfg)
    # the action tokens (item #32) join the mix only once the group is in play -- the master on, or
    # any token switch on -- so a machine that never touches them keeps the label it always had
    tok_on = [k for k in ACTION_KEYS if effective(k, cfg)]
    if master or tok_on:
        on += tok_on
        off += [k for k in ACTION_KEYS if k not in tok_on]
    if not on and not master:
        label = "off"
    elif not off and master:
        label = "on"
    else:
        label = "paranoid (partial)"
    return {"label": label, "master": master, "on": on, "off": off}


def explicit(cfg=None):
    """{key: 'on'|'off'} for the parts the user set, so a status line can say `(set)` / `(follows)`."""
    cfg = read_config() if cfg is None else cfg
    return {k: raw(k, cfg) for k in KEYS if raw(k, cfg) != "follow"}


def active(cfg=None):
    """Is any part on? The gate for every code path that exists only because of this group."""
    cfg = read_config() if cfg is None else cfg
    return any(effective(k, cfg) for k, _n, _t in PARTS)


def explain_text(name):
    """The plain-language text for a key ('' for a key this group does not own). Reused by the
    installer's questions (item #33): who it is for, what it protects against, what it costs."""
    t = TEXT.get(name)
    if not t:
        return ""
    lines = [t["title"], ""]
    for key, label in LABELS:
        lines += ["%s:" % label, "  " + t[key], ""]
    if t.get("parts"):
        lines += ["How the parts fit:", "  " + t["parts"], ""]
    return "\n".join(lines).rstrip() + "\n"


def explain_all():
    """The master's text followed by every part's -- what `explain paranoid` prints."""
    out = [explain_text(MASTER)]
    for key, num, _t in PARTS:
        head = "%s%s" % ("Part %d -- " % num if num else "Also -- ", key)
        out.append(head + "\n" + "-" * len(head) + "\n" + explain_text(key))
    for key in (ACTION_MASTER,) + ACTION_KEYS:
        head = "Action tokens -- %s" % key
        out.append(head + "\n" + "-" * len(head) + "\n" + explain_text(key))
    return "\n".join(out)
