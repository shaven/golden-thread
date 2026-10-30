#!/usr/bin/env python3
"""gt_install_questions -- every question the installer asks, in one place (0.21.0, item #33).

Request 2026-10-08-install-through-claude-asks-every-choice. An install that Claude runs through
its Bash tool is not at a terminal, so until now every question silently took its default and the
person never chose a security level. This module is the single list the installer, Claude and the
person all read:

  gt_install_questions.py list [--json]      the questions in order; installs nothing
  gt_install_questions.py plan ...           resolve every answer (flags, --answers, the terminal,
                                             the machine's current values, defaults) and decide
                                             whether the install may start

THE ORDER: the security level (lockdown) first, then sandbox_mode, paranoid, the token profile
(item #43: one choice for the model profile, reply length, expiry guard and four more settings),
model profile, persona, reply length (item #39), expiry_guard, the status line, ast-grep. The three
security questions carry `security: true`. Paranoid (0.21.0) is the top rung of the security level --
strict plus the paranoid switch -- so its own question is listed but never asked: picking the rung
answers it (on), leaving the rung on a paranoid machine answers it (off), and `--paranoid on|off`
still answers it directly, for trust-local plus paranoid. The two answered against each other
(`--lockdown paranoid --paranoid off`) are refused. Then "Add the gt button pane?" (item #41, module gt-pane): not a security
question, yes by default for the plain-mode personas (project-manager, product-manager, executive,
leadership) and no otherwise; `--with pane` / `--without pane` or an `--answers` key `pane` answer
it, and a no leaves the module uninstalled.

Just before that is the telemetry question (phase 107b, key telemetry): off or local, off by default
and in every express install, never turned on silently, and its words say that nothing is sent anywhere
in this release. `share` is not offered: gt_telemetry.py refuses it until a destination exists.

Before it, "Download Obsidian's plugins?" (phase 89b, key obsidian_plugins): yes or no, no by default
and in every express install; a yes makes the installer run `gt_prereqs.py plugin` for the Terminal and
Claudian plugins, each file checked against a pinned sha256 before anything is written.

The very last question is "Open the guide?" (item #36, key open_guide): a yes opens the Getting started
page of help.html for the chosen persona. It is asked only when someone can see a window -- never in CI,
over SSH, on a Linux machine with no display, with --accept-defaults, or when nobody is at a terminal and
no answer was given -- and then it is simply absent. The token profile also decides the DEFAULTS of the
model profile, the reply length and the expiry guard (an answer to one of those still wins).

WHERE THE WORDS LIVE. The explanation of each choice is the `plain` field of the setting's
registry entry in gt_settings.py (who it is for, what Claude does without asking, what it still
asks, an everyday example, the trade-off, "Not sure?", "change it later"). This module reads it;
it keeps no second copy. The two questions that are not settings (the status line, ast-grep) hold
their `plain` text here, in the same shape and rendered by the same function.

THE RULE (item #33). Not at a terminal, a NEW install with a security question unanswered stops
with exit 5 BEFORE anything is installed, naming the flags that answer it. A non-security question
may still take its default, and the summary says "chosen for you". `--accept-defaults` is the
explicit opt-out, and the summary says the defaults were accepted, not chosen. An install over an
existing gt is not a silent default: an unanswered question keeps the machine's current value and
says "kept: current". At a terminal every unanswered question is asked, with the current value
(or the default, on a new machine) pre-selected so Return keeps it.

Standard library only. The installer calls `plan` with `--env-out`, which writes shell assignments
(PLAN_<key>, PLAN_<key>_SRC) for install.sh to read; every value is validated against a closed
list first, and quoted.
"""
import json
import os
import shlex
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gt_tty                     # noqa: E402
import gt_settings as S            # noqa: E402

EXIT_NEEDS_ANSWERS = 5
SCHEMA = 1

# Where each answer came from, as the installer prints it.
SRC_CHOICE, SRC_KEPT, SRC_DEFAULT, SRC_ACCEPTED = "choice", "kept", "default", "accepted"
SRC_WORDS = {SRC_CHOICE: "your choice", SRC_KEPT: "kept: current",
             SRC_DEFAULT: "default, chosen for you",
             SRC_ACCEPTED: "default, accepted with --accept-defaults"}

# The two questions that are not settings keep their words here, in the registry's shape.
EXTRA_PLAIN = {
    "statusline": {
        "intro": ("Claude Code can show a line at the bottom of its window. gt can put your "
                  "plan-allowance meter there: how much of your 5-hour and weekly allowance you "
                  "have used. It changes only that display."),
        "options": {
            "yes": {"summary": "Show the allowance meter in the status line."},
            "no": {"summary": "Leave the status line alone (the default)."},
        },
        "recommended": "no",
        "not_sure": "Choose no. It is only a display, and you can add it any time.",
        "change_later": ("Run gt_usage.py --setup-statusline in a terminal to add the meter; it is "
                         "never changed if you already have a status line of your own."),
    },
    "ast_grep": {
        "intro": ("ast-grep is a free, optional tool that lets gt check code by its structure "
                  "instead of by matching lines of text. gt works without it; a check that needs "
                  "it is reported as skipped, never as passed. gt only ever runs it on your own files."),
        "options": {
            "yes": {"summary": "Install ast-grep now with brew or npm, whichever you have."},
            "no": {"summary": "Do not install it (the default); structural checks are skipped."},
        },
        "recommended": "no",
        "not_sure": "Choose no unless you check code with gt. You can install it later.",
        "change_later": "Run brew install ast-grep (or npm install -g @ast-grep/cli) yourself at any time.",
    },
    "obsidian_plugins": {
        "intro": ("If you work in Obsidian, two plugins put Claude inside it: Terminal (a Claude Code terminal "
                  "in a pane) and Claudian (Claude in a side panel). With yes, gt downloads exactly these two "
                  "versions into your vault's .obsidian/plugins folder, and only if every file matches a "
                  "sha256 fingerprint gt ships; one mismatch and nothing is written. You still switch them on "
                  "in Obsidian's Community plugins. With no, nothing is downloaded."),
        "options": {
            "yes": {"summary": "Download the Terminal and Claudian plugins into your vault, each checked by sha256."},
            "no": {"summary": "Download nothing (the default); install them from Obsidian yourself if you want them."},
        },
        "recommended": "no",
        "not_sure": "Choose no. It only matters if you use Obsidian, and you can add them later.",
        "change_later": ("Run python3 gt_prereqs.py check to see what is missing, and gt_prereqs.py plugin "
                         "terminal (or realclaudian) any time; it asks nothing else and writes nothing on a mismatch."),
    },
    "parallel_max": {
        "intro": ("gt runs independent pieces of work side by side (reviews, scans, installs on several "
                  "machines). This is the most it may run at once. `auto` lets it use as much of this "
                  "machine as it measures it can; a number caps it, which matters when something else here "
                  "needs the machine, such as a local AI model."),
        "options": {
            "auto": {"summary": "As many as this machine allows (the default)."},
            "4": {"summary": "At most 4 at once: leaves room for a local model or other heavy work."},
            "2": {"summary": "At most 2 at once: the machine stays quiet while gt works."},
        },
        "recommended": "auto",
        "not_sure": "Choose auto. If a local model is running here the default is already 4.",
        "change_later": "Run gt_settings.py set parallel_max auto (or a number) any time; it applies at once.",
    },
    "open_guide": {
        "intro": ("gt has an offline guide that opens in your web browser: why gt is worth using, what it "
                  "never does without asking, where to talk to it (Obsidian with Claudian, a terminal, or "
                  "VS Code), three things to say on your first day, and where to get help. It is one "
                  "file on your computer; nothing is sent anywhere."),
        "options": {
            "yes": {"summary": "Open the guide in your browser when the install finishes."},
            "no": {"summary": "Do not open it. You can open it any time with /gt:gt-help start."},
        },
        "recommended": "yes",
        "not_sure": "Choose yes if this is your first time with gt; it takes a couple of minutes to read.",
        "change_later": "Say 'show me around gt' to Claude, or run /gt:gt-help start; it opens the same page.",
    },
    "telemetry": {
        "intro": ("gt can keep simple counts of how it is used on THIS machine: which features ran, which "
                  "checks passed or failed, which error codes appeared. Nothing is sent anywhere in this "
                  "release: there is no sending code and no destination, so even `local` only writes one "
                  "small file here, once a day, so you can look at exactly what sharing would contain. A "
                  "path, a prompt, a note, a name or a password has no place in it. It is off unless you "
                  "turn it on."),
        "options": {
            "off": {"summary": "Count nothing and write nothing (the default)."},
            "local": {"summary": "Keep the counts in one file on this machine only; nothing leaves it."},
        },
        "recommended": "off",
        "not_sure": "Choose off. Nothing is lost, and you can turn it on later.",
        "change_later": ("Run gt_settings.py set telemetry local (or off) any time; "
                         "python3 gt_telemetry.py preview shows exactly what would be written."),
    },
    "pane": {
        "intro": ("The gt pane is a column of twelve buttons beside your conversation. Press one (or its "
                  "number key) and it sends a plain-words request to Claude, such as \"Show my overdue "
                  "tasks\", as if you had typed it. The buttons fit your role, and you can reorder, "
                  "rename, add and remove them by asking Claude. It needs Claude Code 2.1.290 or later; "
                  "in an older Claude Code, or in VS Code, it quietly does not appear. It changes only "
                  "what is drawn on screen; it cannot run anything Claude could not already be asked to do."),
        "options": {
            "yes": {"summary": "Install the pane. It opens when a session starts (change that any time)."},
            "no": {"summary": "Leave it out. You can add it later with install.sh --with pane."},
        },
        "recommended": "yes",
        "not_sure": ("Choose yes if you prefer pressing a button to typing a request, no if you work "
                     "mostly by typing. The default is yes for project managers, product managers, "
                     "executives and leaders, and no for everyone else."),
        "change_later": ("Run install.sh --with pane or --without pane; the pane's own settings "
                         "(pane_at_start, pane_status) are in /gt:gt-settings."),
    },
}

# Personas whose install defaults the gt pane to yes (item #41).
PLAIN_PERSONAS = ("project-manager", "product-manager", "executive", "leadership")

# Questions whose DEFAULT follows the token profile that was chosen (item #43): the profile sets these
# settings, so a default that disagreed with it would undo it at install time.
PROFILE_FOLLOWS = ("model_profile", "reply_length", "expiry_guard")


# The token profile and the settings it drives are kept on an upgrade only when the person RECORDED
# them. `gt_settings.get` answers the registry default for a key nothing was stored for (normal,
# warn-only, standard), and that is not a choice: keeping it gave an upgrader from <=0.20.5
# reply_length normal and expiry_guard warn-only where a fresh install gets brief and auto, and left
# the profile itself unwritten (fix B2, 0.21.4). Unrecorded, they take the profile's value.
# model_profile is not among them: a scripted upgrade keeps it as it is (inherit) and does not use
# up its one-time question -- the installer's model-profile rules decide it, as before.
PROFILE_DRIVEN = ("token_profile", "reply_length", "expiry_guard")


def keepable(q, inst):
    """May this question's current value be kept (rather than defaulted or asked)?"""
    if not ((inst or q["recorded"]) and q["current"]):
        return False
    return q["recorded"] or q["key"] not in PROFILE_DRIVEN


def profile_default(key, profile):
    """The value the token profile `profile` gives `key`, or None when it gives none."""
    return (S.TOKEN_PROFILES.get(profile) or {}).get(key)


def can_open_guide(env=None, platform=None):
    """May a browser window be opened for the person? Not in CI, not over SSH, not on a Linux machine
    with no display. (Whether anyone is at a terminal is a separate question.)"""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if env.get("CI"):
        return False
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY"):
        return False
    if platform.startswith("linux") and not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        return False
    return True

LOCAL_SERVERS = ((1234, "LM Studio"), (11434, "Ollama"), (8080, "llama-server"))
LOCAL_SERVER_CAP = "4"


def _probe_port(port, timeout=0.15):
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def detect_local_server(probe=None):
    """-> the name of an inference server already listening on this machine, or None (phase 129).
    A loopback connect per well-known port, 0.15 s at most each. Only ever a DEFAULT: it lowers the
    suggested parallel_max so gt's own parallel work does not starve the engine. GT_NO_LOCAL_PROBE=1
    turns it off (CI, tests)."""
    if os.environ.get("GT_NO_LOCAL_PROBE"):
        return None
    probe = probe or _probe_port
    for port, name in LOCAL_SERVERS:
        try:
            if probe(port):
                return name
        except Exception:                              # noqa: BLE001 - a probe never fails the install
            continue
    return None


# (key, flag, kind, security, default, prompt). `setting` is the registry key; None = not a setting.
QUESTIONS = (
    ("lockdown", "--lockdown", "choice", True, "strict",
     "How much may Claude do without asking? (the security level)"),
    ("sandbox_mode", "--sandbox-mode", "choice", True, "off",
     "Fence Claude's shell commands off your vault and gt's own files? (sandbox mode)"),
    ("paranoid", "--paranoid", "choice", True, "off",
     "Lock gt's own installed files so they cannot be changed behind your back? (paranoid)"),
    ("token_profile", "--token-profile", "choice", False, "standard",
     "How should gt trade answer quality against token cost? (the token profile)"),
    ("model_profile", "--model-profile", "choice", False, "average",
     "Which model size should gt's skills run on?"),
    ("persona", "--persona", "multi", False, "none",
     "What do you do? (a persona adds the files people in that role usually want; pick any, or none)"),
    ("persona_aligned", "--persona-aligned", "choice", False, "on",
     "Shape drafts, briefs and summaries for your role? (yes / no; asked only when you chose a persona)"),
    ("reply_length", "--reply-length", "choice", False, "normal",
     "How long should Claude's replies be?"),
    ("expiry_guard", "--expiry-guard", "choice", False, "warn-only",
     "What should gt do when an idle session is about to go cold? (expiry guard)"),
    ("parallel_max", "--parallel-max", "choice", False, "auto",
     "How many jobs may gt run at once? (parallel_max)"),
    ("statusline", "--statusline", "choice", False, "no",
     "Show your plan-allowance meter in Claude Code's status line?"),
    ("ast_grep", "--ast-grep", "choice", False, "no",
     "Install ast-grep, the optional structural code checker?"),
    ("pane", "--with pane", "choice", False, "no",
     "Add the gt button pane?"),
    ("obsidian_plugins", "--obsidian-plugins", "choice", False, "no",
     "Download Obsidian's Terminal and Claudian plugins into your vault? (pinned, checked by sha256)"),
    ("telemetry", "--telemetry", "choice", False, "off",
     "Keep usage counts on this machine, so you can see what sharing would send? (nothing is sent in this release)"),
    ("open_guide", "--open-guide", "choice", False, "yes",
     "Open the guide?"),
)
KEYS = tuple(q[0] for q in QUESTIONS)
FLAGS = {q[0]: q[1] for q in QUESTIONS}

# The starter install (0.22.0, phase 116) asks the security level and the model size and answers the
# rest for the person; the vault location is the installer's own third question. The answers below are
# the full install's defaults, except that nothing optional is turned on. An explicit flag still wins.
STARTER_CHOICES = {"sandbox_mode": "off", "paranoid": "off", "token_profile": "standard",
                   "persona": "none", "parallel_max": "auto", "statusline": "no", "ast_grep": "no",
                   "pane": "no", "obsidian_plugins": "no", "telemetry": "off", "open_guide": "yes"}


def apply_starter(given):
    """`given` with every starter answer filled in where the person gave none. Returns `given`."""
    for k, v in STARTER_CHOICES.items():
        given.setdefault(k, v)
    defaults = {q[0]: q[4] for q in QUESTIONS}
    for k in ("reply_length", "expiry_guard"):
        given.setdefault(k, profile_default(k, given["token_profile"]) or defaults[k])
    return given


class AnswerError(Exception):
    pass


# ---------------------------------------------------------------------------------------- reading
def _release():
    return os.path.basename(os.path.dirname(HERE))


def installed_before():
    """Has this machine had a gt installed? (installed_plugins.json names one.)"""
    try:
        with open(os.path.expanduser("~/.claude/plugins/installed_plugins.json"),
                  encoding="utf-8") as fh:
            ent = json.load(fh)["plugins"]["gt@golden-thread-plugin"]
        return bool(ent)
    except Exception:                                  # noqa: BLE001
        return False


def _claude_settings():
    try:
        with open(os.path.expanduser("~/.claude/settings.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:                                  # noqa: BLE001
        return {}


def persona_options(packs):
    """The persona choices from pack records ({name, title, description, recommends?}).

    A pack's `recommends` field (a later release may add one; none has it today) is shown as
    "Recommended for your role: ..." and carried as `recommended_for`."""
    out = [{"value": "none", "label": "No persona",
            "explain": "No persona: a new project is the standard structure."}]
    for p in packs:
        o = {"value": p["name"], "label": p.get("title") or p["name"],
             "explain": (p.get("description") or p.get("title") or p["name"]).strip()}
        rec = p.get("recommends")
        if isinstance(rec, str) and rec.strip():
            o["recommended_for"] = rec.strip()
            o["explain"] += "  Recommended for your role: %s" % rec.strip()
        out.append(o)
    return out


def _packs():
    try:
        import gt_persona
    except ImportError:
        return []
    out = []
    for p in gt_persona.list_packs():
        rec = None
        try:
            rec = (gt_persona.load_pack(p["name"]) or {}).get("recommends")
        except Exception:                              # noqa: BLE001
            pass
        out.append(dict(p, recommends=rec) if rec else p)
    return out


def _current(key):
    if key == "lockdown":
        try:
            import gt_lockdown
            return gt_lockdown.shown_current()
        except Exception:                              # noqa: BLE001
            return "strict"
    if key == "persona":
        return S.get("persona") or "none"
    if key == "statusline":
        return "yes" if _claude_settings().get("statusLine") else "no"
    if key == "ast_grep":
        return "yes" if (shutil.which("ast-grep") or shutil.which("sg")) else "no"
    if key == "pane":
        return _pane_choice()
    if key == "open_guide":
        return ""
    if key == "obsidian_plugins":
        return "no"                  # a download is never kept or assumed: each install asks again
    return S.get(key) or ""


def _pane_choice():
    """'yes' / 'no' when this machine has a recorded choice for the pane module, else ''."""
    try:
        with open(os.path.expanduser("~/.claude/golden-thread/install-choices.json"),
                  encoding="utf-8") as fh:
            v = (json.load(fh).get("choices") or {}).get("pane")
    except Exception:                                  # noqa: BLE001
        return ""
    return {"on": "yes", "off": "no"}.get(v, "")


def pane_default(persona_value):
    """'yes' when any persona in the (comma) list is a plain-mode one, else 'no'."""
    names = [n.strip().lower() for n in str(persona_value or "").replace("[", "").replace("]", "")
             .replace("'", "").replace('"', "").split(",")]
    return "yes" if any(n in PLAIN_PERSONAS for n in names) else "no"


def _pane_in_tree():
    """Does the tree this script belongs to ship the pane module? (<root>/golden-thread/<v>/scripts)"""
    root = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
    return os.path.isdir(os.path.join(root, "golden-thread-pane"))


def recorded(key):
    """Has the person already recorded a value for this setting on this machine? A recorded
    value is a choice that was made, so it is kept like a re-install keeps it -- even when no gt
    is registered yet (a vault-config.json carried over, a fixture)."""
    try:
        if key == "pane":
            return _pane_choice() != ""
        if key in ("sandbox_mode", "paranoid", "expiry_guard", "persona", "token_profile", "reply_length",
                   "persona_aligned"):
            import gt_paranoid
            v = gt_paranoid.read_config().get(key)
            return isinstance(v, str) and v != ""
        if key == "model_profile":
            return S._model_profile() is not None
    except Exception:                                  # noqa: BLE001
        pass
    return False


def _applies(key):
    """(applies, note). A question that cannot change anything is listed but never asked."""
    if key == "statusline" and _claude_settings().get("statusLine"):
        return False, "you already have a status line of your own; gt never replaces it"
    if key == "ast_grep" and (shutil.which("ast-grep") or shutil.which("sg")):
        return False, "ast-grep is already on this machine"
    if key == "pane" and not _pane_in_tree():
        return False, "this release does not ship the gt-pane module"
    if key == "pane":
        import gt_components
        gap = gt_components.module_cc_gap("pane")
        if gap:
            return False, "the gt pane " + gap
    if key == "paranoid":
        return False, ("chosen on the security level: its top rung, paranoid, turns it on "
                       "(or answer --paranoid on|off)")
    if key == "open_guide":
        if not can_open_guide():
            return False, "not opened in CI, over SSH or on a machine with no display"
        if not (gt_tty.has_terminal(sys.stdin) and gt_tty.has_terminal(sys.stdout)):
            return False, "nobody is at a terminal to see it open (answer with --open-guide yes)"
    return True, ""


def _plain_spec(key):
    if key in EXTRA_PLAIN:
        return EXTRA_PLAIN[key]
    return (S.SETTINGS.get(key) or {}).get("plain") or {}


def _options(key):
    pl = _plain_spec(key)
    if key == "persona":
        return persona_options(_packs())
    if key == "lockdown":
        import gt_lockdown
        vals = list(gt_lockdown.CHOICES)
    elif key in EXTRA_PLAIN:
        vals = list(pl["options"])
    elif key == "token_profile":
        vals = [v for v in S.SETTINGS[key]["values"] if v != "manual"]   # manual is a later choice
    else:
        vals = list(S.SETTINGS[key]["values"])
    out = []
    for v in vals:
        o = (pl.get("options") or {}).get(v) or {}
        item = {"value": v, "label": v.replace("-", " "), "explain": o.get("summary", v)}
        plain = {k: o[k] for k in ("who", "without_asking", "still_asks", "example", "trade_off")
                 if o.get(k)}
        if plain:
            item["plain"] = plain
        if v == pl.get("recommended"):
            item["recommended"] = True
        out.append(item)
    return out


def plain_text(key):
    """The text shown for a question: the registry's rendering (the single source)."""
    if key in EXTRA_PLAIN:
        return S.render_plain(EXTRA_PLAIN[key])
    if key == "persona":
        pl = _plain_spec(key)
        opts = {o["value"]: {"summary": o["explain"]} for o in _options(key)}
        return S.render_plain(pl, opts)
    return S.plain_text(key)


def questions(installed=None):
    """Every question, in order, as plain dicts (the --json shape)."""
    inst = installed_before() if installed is None else installed
    out = []
    for key, flag, kind, security, default, prompt in QUESTIONS:
        pl = _plain_spec(key)
        cur = _current(key)
        applies, note = _applies(key)
        if key == "pane":
            default = pane_default(_current("persona"))
        server = detect_local_server() if key == "parallel_max" else None
        if server:
            default = LOCAL_SERVER_CAP
        q = {"key": key, "flag": flag, "kind": kind, "prompt": prompt, "security": security,
             "options": _options(key), "default": default, "current": cur,
             "recorded": recorded(key),
             "preselected": default,
             "applies": applies, "plain_text": plain_text(key),
             "recommended": pl.get("recommended", ""), "not_sure": pl.get("not_sure", ""),
             "change_later": pl.get("change_later", "")}
        if server:
            q["plain_text"] += ("\n\nA local AI server (%s) is running on this machine, so the suggested "
                               "cap is %s, not auto." % (server, LOCAL_SERVER_CAP))
        if keepable(q, inst):
            q["preselected"] = cur
        if not applies:
            q["applies_note"] = note
        out.append(q)
    return out


# ------------------------------------------------------------------------------------ validating
def normalise(key, value):
    """-> the canonical value, or raise AnswerError naming the flag and the valid values."""
    flag = FLAGS[key]
    v = ("" if value is None else str(value)).strip().lower()
    if key == "persona":
        if v in ("", "none"):
            return "none"
        valid = [p["name"] for p in _packs()]
        names = [x.strip() for x in v.split(",") if x.strip()]
        bad = [n for n in names if n not in valid]
        if bad:
            raise AnswerError("%s: '%s' is not an installed persona; valid: none, %s"
                              % (flag, ",".join(bad), ", ".join(valid)))
        return ",".join(names)
    if key == "parallel_max":
        if v == "auto" or (v.isdigit() and int(v) >= 1):
            return v
        raise AnswerError("%s: '%s' is not auto or a whole number of 1 or more" % (flag, value))
    if key == "persona_aligned":
        v = {"yes": "on", "y": "on", "true": "on", "no": "off", "n": "off", "false": "off"}.get(v, v)
        valid = ["on", "off"]
    elif key in ("statusline", "ast_grep", "pane", "open_guide", "obsidian_plugins"):
        v = {"y": "yes", "true": "yes", "n": "no", "false": "no"}.get(v, v)
        valid = ["yes", "no"]
    elif key == "lockdown":
        import gt_lockdown
        valid = list(gt_lockdown.CHOICES)
    else:
        valid = list(S.SETTINGS[key]["values"])
        if key == "paranoid":
            valid = ["off", "on"]
        if key == "token_profile":
            valid = [x for x in valid if x != "manual"]
    if v not in valid:
        raise AnswerError("%s: '%s' is not one of %s" % (flag, value, ", ".join(valid)))
    return v


def read_answers_file(path):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError) as e:
        raise AnswerError("--answers: cannot read %s as JSON (%s)" % (path, e))
    if isinstance(d, dict) and isinstance(d.get("answers"), dict):
        d = d["answers"]
    if not isinstance(d, dict):
        raise AnswerError("--answers: %s must hold a JSON object of {question: answer}" % path)
    out = {}
    for k, v in d.items():
        if k not in KEYS:
            raise AnswerError("--answers: '%s' is not a question; the questions are: %s"
                              % (k, ", ".join(KEYS)))
        out[k] = normalise(k, ",".join(v) if isinstance(v, list) else v)
    return out


# ------------------------------------------------------------------------------------- the plan
def resolve(given, accept_defaults=False, tty=False, installed=None, ask=None):
    """-> (values, sources, missing). `given` = {key: value} from flags/--answers (validated).

    `ask(question, preselected) -> value` is called for each unanswered, applicable question when
    `tty` is true. `missing` lists the security questions that were left unanswered."""
    inst = installed_before() if installed is None else installed
    qs = questions(inst)
    values, sources, missing = {}, {}, []
    for q in qs:
        k = q["key"]
        if k == "pane":
            persona = values.get("persona") or _current("persona")
            q = dict(q, default=pane_default(persona))
            if not keepable(q, inst):
                q["preselected"] = q["default"]
        if k == "persona_aligned":
            # asked only when a persona was chosen (this run's answer, else the current one); an
            # explicit --persona-aligned still wins
            persona = values.get("persona") or _current("persona")
            has = persona not in ("", "none")
            q = dict(q, applies=has)
            if not has:
                q["applies_note"] = "asked only when you chose a persona"
        if k in PROFILE_FOLLOWS:
            want = profile_default(k, values.get("token_profile"))
            if want:
                q = dict(q, default=want)
                if not keepable(q, inst):
                    q["preselected"] = want
        if k == "open_guide":
            # a window opens only for a person who can see one; --accept-defaults never opens it, and
            # with no terminal and no answer the question is simply absent
            if accept_defaults or not can_open_guide():
                continue
            if k in given:
                values[k], sources[k] = given[k], SRC_CHOICE
            elif tty and ask:
                values[k], sources[k] = ask(q, q["preselected"]), SRC_CHOICE
            continue
        if k in given:
            values[k], sources[k] = given[k], SRC_CHOICE
        elif not q["applies"]:
            continue
        elif accept_defaults:
            if keepable(q, inst):
                values[k], sources[k] = q["current"], SRC_KEPT
            else:
                values[k], sources[k] = q["default"], SRC_ACCEPTED
        elif tty and ask:
            values[k], sources[k] = ask(q, q["preselected"]), SRC_CHOICE
        elif keepable(q, inst):
            values[k], sources[k] = q["current"], SRC_KEPT
        elif q["security"]:
            missing.append(q)
        else:
            values[k], sources[k] = q["default"], SRC_DEFAULT
    _paranoid_from_the_ladder(given, values, sources)
    return values, sources, missing


def _paranoid_from_the_ladder(given, values, sources):
    """The paranoid rung answers the paranoid question (0.21.0): picking it turns the switch on,
    leaving it turns the switch off. A kept level leaves the switch alone; an explicit --paranoid
    wins, unless it contradicts the rung."""
    lvl, src = values.get("lockdown"), sources.get("lockdown")
    if lvl is None or src == SRC_KEPT:
        return
    if lvl == "paranoid":
        if given.get("paranoid") == "off":
            raise AnswerError("--lockdown paranoid and --paranoid off contradict each other: the "
                              "paranoid level is strict with the paranoid switch on. Choose one.")
        values["paranoid"], sources["paranoid"] = "on", src
    elif "paranoid" not in given and _current("lockdown") == "paranoid":
        values["paranoid"], sources["paranoid"] = "off", src


def review_lines(values, sources):
    return ["  %s: %s (%s)" % (k, values[k], SRC_WORDS[sources[k]]) for k in KEYS if k in values]


def summary_text(values, sources):
    """The closing summary: what was defaulted for the person, or accepted with --accept-defaults."""
    out = []
    chosen = [k for k in KEYS if sources.get(k) == SRC_DEFAULT]
    accepted = [k for k in KEYS if sources.get(k) == SRC_ACCEPTED]
    if chosen:
        out.append("Chosen for you (defaults; you were not asked, and none of these is a security level):")
        out += ["  %s: %s    change it: %s" % (k, values[k], _change_hint(k, FLAGS[k])) for k in chosen]
        out.append("")
    if accepted:
        out.append("Defaults accepted with --accept-defaults (you did not choose these):")
        out += ["  %s: %s    change it: %s" % (k, values[k], _change_hint(k, FLAGS[k])) for k in accepted]
        out.append("")
    return "\n".join(out).rstrip()


def _change_hint(key, flag):
    if key in ("statusline", "ast_grep", "open_guide", "obsidian_plugins"):
        return "re-run the installer with %s yes|no" % flag
    if key == "telemetry":
        return "gt_settings.py set telemetry off|local  (or the installer's --telemetry)"
    if key == "pane":
        return "re-run the installer with --with pane or --without pane"
    return "gt_settings.py set %s <value>  (or the installer's %s)" % (
        "model_profile" if key == "model_profile" else key, flag)


def missing_message(missing):
    out = ["Needs answers: this install is not at a terminal, and these security questions have",
           "no answer. Nothing was installed. Ask the person, then run the installer again with",
           "the answers as flags (or in a JSON file given to --answers):", ""]
    for q in missing:
        opts = " | ".join(o["value"] for o in q["options"])
        out.append("  %s <%s>    %s" % (q["flag"], opts, q["prompt"]))
    out += ["", "See every question and what each choice means:  install.sh --questions --json",
            "For an unattended run (CI) that should take the defaults, say so on purpose:",
            "  install.sh --accept-defaults    (the summary will state the defaults were accepted)"]
    return "\n".join(out)


# ------------------------------------------------------------------------------ the terminal ask
class _Tty:
    """The controlling terminal, read and written separately (a text-mode r+ handle on a tty is
    not seekable and Python refuses it)."""

    def __init__(self):
        self._r = open("/dev/tty", "r", encoding="utf-8", newline="\n")
        self._w = open("/dev/tty", "w", encoding="utf-8", buffering=1, newline="\n")

    def write(self, text):
        self._w.write(text)

    def flush(self):
        self._w.flush()

    def readline(self):
        return self._r.readline()


def _open_tty():
    try:
        return _Tty()
    except OSError:
        return None


def _numbered(q):
    return {str(i + 1): o["value"] for i, o in enumerate(q["options"])}


# Two rows of asterisks open every question and the review screen, so on screen it is plain where
# one question ends and the next begins (owner 2026-10-10: the long question texts ran together).
QUESTION_BREAK = "\n" + ("*" * 72 + "\n") * 2


def ask_one(q, preselected, tty, number, total):
    """Show a question with its plain text, and read the answer from `tty`. Return keeps the
    pre-selected value. -> the canonical value."""
    w = tty.write
    w(QUESTION_BREAK)
    w("[%d/%d] %s%s\n\n" % (number, total, q["prompt"], "   (security question)" if q["security"] else ""))
    w(q["plain_text"] + "\n")
    nums = _numbered(q)
    for n, v in nums.items():
        o = next(x for x in q["options"] if x["value"] == v)
        mark = []
        if o.get("recommended"):
            mark.append("recommended")
        if q["current"] == v:
            mark.append("current")
        w("  %s) %s%s\n" % (n, v, "   (%s)" % ", ".join(mark) if mark else ""))
    while True:
        w("Choose %s%s (Return keeps %s): " % (
            "numbers or names, comma-separated, " if q["kind"] == "multi" else "a number or name, ",
            "1-%d" % len(nums), preselected))
        tty.flush()
        line = tty.readline()
        if line == "":
            raise AnswerError("the terminal closed before %s was answered" % q["flag"])
        ans = line.strip()
        if ans == "":
            return preselected
        parts = [nums.get(p.strip(), p.strip()) for p in ans.split(",")] if q["kind"] == "multi" \
            else [nums.get(ans, ans)]
        try:
            return normalise(q["key"], ",".join(parts))
        except AnswerError as e:
            w("  %s\n" % e)


def interactive(given, installed, accept_defaults):
    tty = _open_tty()
    if tty is None:
        return None
    qs = [q for q in questions(installed) if q["applies"] and q["key"] not in given]
    total = len(qs)
    counter = {"n": 0}

    def ask(q, pre):
        counter["n"] += 1
        return ask_one(q, pre, tty, counter["n"], total)

    values, sources, missing = resolve(given, accept_defaults, True, installed, ask)
    # the one review screen: confirm or change one
    while True:
        tty.write(QUESTION_BREAK + "Your install choices:\n" + "\n".join(
            "  %d) %s" % (i + 1, l.strip()) for i, l in enumerate(review_lines(values, sources))) + "\n")
        tty.write("Return installs with these; a number changes that one; q stops without installing: ")
        tty.flush()
        ans = tty.readline().strip().lower()
        if ans == "":
            return values, sources
        if ans == "q":
            raise AnswerError("stopped at the review; nothing was installed")
        shown = [k for k in KEYS if k in values]
        if ans.isdigit() and 1 <= int(ans) <= len(shown):
            k = shown[int(ans) - 1]
            q = next(x for x in questions(installed) if x["key"] == k)
            note = review_change(k, ask_one(q, values[k], tty, int(ans), len(shown)), given,
                                 values, sources)
            if note:
                tty.write("  %s\n" % note)


def review_change(key, value, given, values, sources):
    """One answer changed at the review screen. The level and the paranoid switch move together
    (0.21.0): a new level re-derives the switch, and a switch that contradicts the paranoid level
    is put back. -> a note for the person, or ''."""
    values[key], sources[key] = value, SRC_CHOICE
    if key not in ("lockdown", "paranoid"):
        return ""
    if key == "lockdown" and "paranoid" not in given:
        values.pop("paranoid", None)
        sources.pop("paranoid", None)
    asked = dict(given, paranoid=value) if key == "paranoid" else given
    try:
        _paranoid_from_the_ladder(asked, values, sources)
    except AnswerError as e:
        values["paranoid"], sources["paranoid"] = "on", SRC_CHOICE
        return "%s Kept paranoid on." % e
    return ""


# ---------------------------------------------------------------------------------------- the CLI
def cmd_list(argv):
    as_json = "--json" in argv
    qs = questions()
    if as_json:
        print(json.dumps({"schema": SCHEMA, "version": _release(), "installed_before": installed_before(),
                          "order": list(KEYS), "questions": qs}, indent=2))
        return 0
    inst = installed_before()
    print("Golden Thread install questions (release %s). Nothing is installed by listing them.\n"
          "A new install answers them by flag, by --answers FILE, or at a terminal.\n" % _release())
    for i, q in enumerate(qs, 1):
        print("%d. %s%s" % (i, q["prompt"], "   [security question]" if q["security"] else ""))
        print("   flag: %s <%s>    default: %s    current: %s%s" % (
            q["flag"], " | ".join(o["value"] for o in q["options"][:8]) if len(q["options"]) <= 5 else "name",
            q["default"], q["current"] or "(not set)", "" if inst else "  (new machine)"))
        if not q["applies"]:
            print("   (not asked: %s)" % q["applies_note"])
        print()
        print("\n".join("   " + l if l else "" for l in q["plain_text"].splitlines()))
        print()
    return 0


def _flag_value(argv, name):
    """Every value after `name`, in order (the flag may repeat)."""
    return [argv[i + 1] for i, a in enumerate(argv) if a == name and i + 1 < len(argv)]


def cmd_plan(argv):
    inst = installed_before()
    if _flag_value(argv, "--new-install"):
        inst = _flag_value(argv, "--new-install")[-1] != "yes"
    try:
        given = {}
        for f in _flag_value(argv, "--answers"):
            given.update(read_answers_file(f))
        for item in _flag_value(argv, "--set"):
            k, _, v = item.partition("=")
            if k not in KEYS:
                raise AnswerError("--set: '%s' is not a question" % k)
            given[k] = normalise(k, v)
        if "--starter" in argv:
            apply_starter(given)
        accept = "--accept-defaults" in argv
        tty = "--tty" in argv and not accept
        res = None
        if tty:
            res = interactive(given, inst, accept)
        if res is None:
            values, sources, missing = resolve(given, accept, False, inst)
            if missing:
                print(missing_message(missing))
                return EXIT_NEEDS_ANSWERS
        else:
            values, sources = res
    except AnswerError as e:
        print("✗ %s" % e)
        return 1
    review = "Install choices:\n" + "\n".join(review_lines(values, sources))
    ro = _flag_value(argv, "--review-out")
    if ro:
        with open(ro[-1], "w", encoding="utf-8", newline="\n") as fh:
            fh.write(review + "\n")
    else:
        print(review)
    out = _flag_value(argv, "--env-out")
    if out:
        lines = []
        for k in KEYS:
            if k in values:
                lines.append("PLAN_%s=%s" % (k, shlex.quote(values[k])))
                lines.append("PLAN_%s_SRC=%s" % (k, shlex.quote(sources[k])))
        with open(out[-1], "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
    sm = _flag_value(argv, "--summary-out")
    if sm:
        with open(sm[-1], "w", encoding="utf-8", newline="\n") as fh:
            fh.write(summary_text(values, sources) + "\n")
    return 0


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    if a and a[0] == "list":
        return cmd_list(a[1:])
    if a and a[0] == "plan":
        return cmd_plan(a[1:])
    print(__doc__.strip().split("\n\n")[0])
    print("usage: gt_install_questions.py list [--json] | plan [--tty] [--answers F] [--set key=value]\n"
          "         [--accept-defaults] [--starter] [--new-install yes|no] [--env-out F] [--summary-out F] [--review-out F]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
