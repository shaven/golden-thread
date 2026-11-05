#!/usr/bin/env python3
"""gt_farm_agent_setup.py -- guided setup, status, off switch and health check for agent farming (gt-farm 0.21.3, gt 0.22.0)

Agent farming lets a hosted AI model fix code tasks on a COPY of your repo while you do something else. This script
sets it up in a few plain questions, shows you what it will do and what it can cost, and only then writes anything.

    gt_farm_agent_setup.py start   [--vault DIR] [--services FILE] [--yes] [--answers FILE] [--dry-run]
    gt_farm_agent_setup.py status  [--vault DIR] [--services FILE]
    gt_farm_agent_setup.py off     [--dry-run]
    gt_farm_agent_setup.py doctor  [--vault DIR] [--services FILE] [--engine NAME]

start    asks seven questions, one at a time, each with a safe default in brackets:
           1 which provider (openrouter, together, or openai-compatible with a web address)
           2 which model id
           3 where the file is that holds your API key (a file only you can read, mode 600). The agent loop reads the
             key from that file; the secrets-store route is not supported for agent mode yet, so typed and stored
             keys are refused with a plain sentence. With no such file, start prints how to create one without
             showing the key, writes nothing and exits 2
           4 which class of work to farm, how much of it (default 10 percent) and whether the share grows
           5 the daily budget in dollars (default 2)
           6 which repos it may see (default: none until you name them)
           7 the sandbox check: whether commands the model runs can be walled in on this machine
         then prints a one-screen SUMMARY: where your code goes, what the data policy does and does NOT promise,
         the worst-case cost of one task, and how to turn it off. Only after a final "yes" does it write the engine
         row (agent: yes, tools: yes, local: no) and the farm-share.tsv row, through the vault write queue, and print
         the next commands. It refuses to enable an engine with no price row (the cost would be unknown) and a machine
         with no sandbox. --dry-run asks, checks and shows the summary but writes and stores nothing.
         --yes takes every default and answers the final question yes; a question without a default (the model id)
         must then come from --answers FILE, a JSON object with any of: provider, model, base_url, name, zone,
         key_file (the key file; key_source is accepted only as "file"), task_class, share_target, ramp (yes|no),
         daily_budget, repos (list), confirm ("yes" writes). Unknown keys are refused so a typo cannot pass quietly.
status   one screen: engines in agent mode, share target against effective share, spend today, pauses, and the
         calibration verdict with its age.
off      turns routing off through the farm_share setting (every task stays with Claude). Your rows and history stay.
doctor   checks the sandbox, the key file (present or not, never shown; a row on the secrets-store route or with no
         key file is flagged), the price row, how fresh the calibration is, the share table and the state file, and
         prints one fix sentence for each failure.

The key is never read, printed or written by this script. A key file is only checked for existing, being non-empty
and (on macOS and Linux) being readable by you alone. Every message says what happened and what to do next; no stack trace is shown.

Exit codes: 0 done (or nothing to do, or you answered no) | 1 a step or a check failed | 2 refused or cannot start
(no sandbox, no price row, no answer for a question, bad input, the service table was not found).
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import gt_farm_egress as eg            # noqa: E402
import gt_farm_hosted_setup as hs      # noqa: E402
import gt_farm_send as fs              # noqa: E402
import gt_farm_share as sh             # noqa: E402

GT_VERSION = "0.21.4"
WIDTH = 78
AGENT_CLASS = "agent-code"
SELF = "gt_farm_agent_setup.py"
DEFAULT_SHARE = 10
DEFAULT_BUDGET = 2.0
RAMP_STEP = 5
RAMP_WINDOW = 20
WINDOW_TOKENS = 128000          # the context window assumed for the worst case when none is known
MAX_STEPS = 60                  # the agent loop's own defaults (gt_farm_agent.DEFAULT_LIMITS)
MAX_TOKENS_CALL = 40000
NO_STORE = ("Agent mode needs a key file for now. The secrets-store route is not supported by the agent loop yet.")
ANSWER_KEYS = ("provider", "model", "base_url", "name", "zone", "key_source", "key_file", "task_class",
               "share_target", "ramp", "daily_budget", "repos", "confirm")

PRESETS = {
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "api": "openai-chat",
                   "data_policy": "no-train"},
    "together": {"label": "Together", "base_url": "https://api.together.xyz/v1", "api": "openai-chat",
                 "data_policy": "no-train"},
    "openai-compatible": {"label": "another service", "base_url": "", "api": "openai-chat", "data_policy": ""},
}
CLASS_RE = r"[a-z][a-z0-9-]{0,30}"


class Stop(Exception):
    """The run cannot go on. `what` says what happened, `todo` what to do next."""

    def __init__(self, what, todo, code=2):
        Exception.__init__(self, what)
        self.what, self.todo, self.code = what, todo, code


# ------------------------------------------------------------------------------------------------ output

_TICKS = re.compile(r"`([^`]+)`")
_HOLD = "\x00"


def wrap(text, indent="", sub=None):
    """Fold to WIDTH columns. A command in backticks is kept on one line when it fits, so it can be copied."""
    room = WIDTH - 12
    text = _TICKS.sub(lambda m: m.group(0).replace(" ", _HOLD) if len(m.group(0)) <= room else m.group(0), text)
    out = textwrap.fill(text, width=WIDTH, initial_indent=indent, subsequent_indent=indent if sub is None else sub,
                        break_long_words=False, break_on_hyphens=False)
    return out.replace(_HOLD, " ")


def say(text="", indent="", sub=None):
    print(wrap(text, indent, sub) if text else "")


def raw(text):
    """A line laid out by hand (a table row): kept as it is unless it is too long, then folded with an indent."""
    for line in text.split("\n"):
        if len(line) <= WIDTH:
            print(line)
        else:
            print(textwrap.fill(line, width=WIDTH, subsequent_indent="    ", break_long_words=False,
                                break_on_hyphens=False))


def problem(what, todo):
    sys.stderr.write(wrap("Problem: " + what, "", "         ") + "\n")
    sys.stderr.write(wrap("What to do: " + todo, "", "           ") + "\n")


# ------------------------------------------------------------------------------------------------ injection

class Deps(object):
    """Everything that touches the outside world, so a test can hand in fakes and nothing real is used."""

    def __init__(self, input_fn=None, sandbox_fn=None, price_fn=None, setting_fn=None,
                 queue_fn=None, calibration_fn=None, today=None):
        self.input_fn = input_fn or input
        self.sandbox_fn = sandbox_fn or sandbox_default
        self.price_fn = price_fn or fs.price_row
        self.setting_fn = setting_fn or setting_default
        self.queue_fn = queue_fn or queue_default
        self.calibration_fn = calibration_fn or calibration_default
        self.today = today or datetime.date.today()


def sandbox_default():
    """-> the isolating backend's name, or "" when none can wall in the model's commands here."""
    try:
        import gt_farm_workspace as W
        launch = W._launcher()
        if launch is None:
            return ""
        b = launch.available_backends()[0]
        return "" if b in ("none", "appcontainer") else str(b)     # AppContainer cannot wall in write mode
    except Exception:                                     # noqa: BLE001 -- unknown means no sandbox
        return ""


def setting_default(name, value):
    """-> (ok, message). Runs gt core's settings tool; False when it cannot be found or refuses."""
    core = fs.core_scripts_dir()
    path = os.path.join(core, "gt_settings.py") if core else ""
    if not path or not os.path.isfile(path):
        return False, "gt_settings.py was not found"
    p = subprocess.run([sys.executable, path, "set", name, value], capture_output=True, text=True)
    tail = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
    return p.returncode == 0, (tail[-1] if tail else "")


def queue_default(vault, rel, text, services=None, base_text=None):
    return hs.queue_write(vault, rel, text, services=services, base_text=base_text)


def calibration_default(vault, name):
    """-> {verdict, date, stale} for the newest agent-code record of engine `name`, or None."""
    try:
        import gt_farm_calibration as C
        for r in C.vault_records(vault):
            if r.get("engine") == name and isinstance(r.get("kinds"), dict) and C.AGENT_CLASS in r["kinds"]:
                k = C.kind_of(r, C.AGENT_CLASS) or {}
                return {"verdict": k.get("verdict"), "date": (r.get("agent_test") or {}).get("date"),
                        "stale": C.agent_stale(r)}
    except Exception:                                     # noqa: BLE001 -- no record is a state, not a crash
        pass
    return None


# ------------------------------------------------------------------------------------------------ asking

class Asker(object):
    def __init__(self, answers, yes, deps):
        self.answers, self.yes, self.deps = answers, yes, deps

    def ask(self, key, question, default, check, hint=""):
        """-> the checked value. A given answer or the default is checked once; a typed one until it passes."""
        if key in self.answers:
            return self._checked(key, str(self.answers[key]) if not isinstance(self.answers[key], (list, dict))
                                 else self.answers[key], check, key)
        if self.yes:
            if default is None:
                raise Stop("there is no default for: %s" % question,
                           "put \"%s\" in the file you pass with --answers, or run this without --yes to be asked" % key)
            return self._checked(key, default, check, key)
        say()
        say(question)
        if hint:
            say(hint, "  ")
        for _ in range(3):
            try:
                reply = self.deps.input_fn("  [%s] > " % (default if default is not None else "no default"))
            except (EOFError, OSError):
                raise Stop("no answer was given to: %s" % question,
                           "run this in a terminal, or pass --yes with --answers FILE")
            reply = reply.strip() or (default if default is not None else "")
            try:
                return check(reply)
            except ValueError as e:
                say(str(e), "  ")
        raise Stop("three answers in a row did not fit: %s" % question, "run it again and read the hint under the question")

    def _checked(self, key, value, check, label):
        try:
            return check(value)
        except ValueError as e:
            raise Stop("your answer for %s does not fit: %s" % (label, e), "fix it and run this again")


def c_provider(v):
    v = str(v).strip().lower()
    if v not in PRESETS:
        raise ValueError("choose one of: %s" % ", ".join(PRESETS))
    return v


def c_model(v):
    v = str(v).strip()
    if not v or not hs.TOKEN_RE.fullmatch(v) or hs._has_control(v):
        raise ValueError("a model id has no spaces, for example <maker/model-name>")
    return v


def c_base_url(v):
    v = str(v).strip().rstrip("/")
    if not hs.URL_RE.fullmatch(v):
        raise ValueError("give an https web address such as https://api.example.com/v1")
    why = hs.host_problem(hs.host_of(v), False)
    if why:
        raise ValueError(why.replace("--base-url", "the address"))
    return v


def c_name(v):
    v = str(v).strip()
    if not hs.NAME_RE.fullmatch(v):
        raise ValueError("a name is one lower-case word: letters, digits, . _ - (at most 40)")
    return v


def c_zone(v):
    v = str(v).strip()
    if not hs.ZONE_RE.fullmatch(v):
        raise ValueError("a zone is one word: letters, digits, _ -")
    return v


def key_file_howto(path=""):
    """Plain steps to make a key file without showing the key. -> text for say()."""
    path = path or "~/.gt-farm-api-key"
    return ("Create the file yourself: open a terminal, run `umask 077; read -rs K; printf %%s \"$K\" > %s` and paste "
            "the key when asked. On Windows, save the key in a plain text file inside your own user folder, which "
            "other users cannot open, and give that path." % path)


def mode_problem(p):
    """-> a sentence when the key file at p can be read by other users (macOS, Linux), else ''."""
    if os.name == "nt":
        return ""
    try:
        mode = os.stat(p).st_mode & 0o777
    except OSError:
        return "the file at %s cannot be read" % p
    if mode & 0o077:
        return "the key file at %s can be read by other users (mode %o); run `chmod 600 %s`" % (p, mode, p)
    return ""


def c_key_file(v):
    raw_v = str(v).strip()
    if not raw_v or raw_v.lower() == "none" or hs._has_control(raw_v):
        raise Stop("there is no key file to use.", key_file_howto())
    p = os.path.expanduser(raw_v)
    if not os.path.isfile(p):
        raise Stop("there is no file at %s." % p, key_file_howto(raw_v))
    try:
        if os.path.getsize(p) == 0:
            raise ValueError("the file at %s is empty; put your key in it" % p)
    except OSError:
        raise ValueError("the file at %s cannot be read" % p)
    why = mode_problem(p)
    if why:
        raise ValueError(why)
    return p


def c_class(v):
    v = str(v).strip().lower()
    if not re.fullmatch(CLASS_RE, v):
        raise ValueError("a class is one lower-case word, for example %s" % AGENT_CLASS)
    return v


def c_percent(v):
    try:
        f = float(str(v).strip().rstrip("%"))
    except ValueError:
        raise ValueError("give a number from 0 to 100")
    if not 0 <= f <= 100:
        raise ValueError("give a number from 0 to 100")
    return f


def c_yesno(v):
    v = str(v).strip().lower()
    if v in ("y", "yes", "true", "on"):
        return True
    if v in ("n", "no", "false", "off"):
        return False
    raise ValueError("answer yes or no")


def c_budget(v):
    try:
        f = float(str(v).strip().lstrip("$"))
    except ValueError:
        raise ValueError("give dollars per day, for example 2")
    if not 0 < f <= 1000:
        raise ValueError("give dollars per day above 0 and up to 1000")
    return f


def c_repos(v):
    items = v if isinstance(v, list) else [x.strip() for x in str(v).split(",")]
    items = [x for x in items if x and x.lower() != "none"]
    out = []
    for x in items:
        p = os.path.abspath(os.path.expanduser(str(x)))
        if not os.path.isdir(p):
            raise ValueError("%s is not a folder" % p)
        out.append(p)
    return out


# ------------------------------------------------------------------------------------------------ tables

def edit_service_table(text, vals):
    """-> (new_text, action, problem). Adds any column of `vals` the table lacks (blank in the other rows), then
    updates the row with this name or adds it after the last row. Columns this script does not set are kept."""
    loc = hs.locate_table(text)
    if loc is None:
        return None, None, "the service table has no Markdown table with a `name` column"
    hdr_idx, keys, rows, _last = loc
    lines = text.split("\n")
    missing = [k for k in vals if k not in keys]
    if missing:
        lines[hdr_idx] = "| " + " | ".join(hs._cells(lines[hdr_idx]) + missing) + " |"
        sep = hdr_idx + 1
        if sep < len(lines) and lines[sep].strip().startswith("|") and \
                all(re.fullmatch(r":?-{2,}:?", c) for c in hs._cells(lines[sep]) if c):
            cs = hs._cells(lines[sep])
            lines[sep] = "| " + " | ".join(cs + ["---"] * len(missing)) + " |"
        for i, cs in rows:
            cs = cs + [""] * (len(keys) - len(cs))
            lines[i] = "| " + " | ".join(cs + [""] * len(missing)) + " |"
        text = "\n".join(lines)
        loc = hs.locate_table(text)
        hdr_idx, keys, rows, _last = loc
        lines = text.split("\n")
    at = keys.index("name")
    for i, cs in rows:
        if len(cs) > at and cs[at].strip("` ") == vals["name"]:
            old = dict(zip(keys, cs))
            merged = [vals[k] if k in vals else old.get(k, "") for k in keys]
            if [old.get(k, "") for k in keys] == merged:
                return text, "unchanged", None
            lines[i] = "| " + " | ".join(merged) + " |"
            return "\n".join(lines), "updated", None
    new = "| " + " | ".join(vals.get(k, "") for k in keys) + " |"
    lines.insert(loc[3] + 1, new)
    return "\n".join(lines), "added", None


def edit_share_table(text, cells):
    """-> (new_text, action). Replaces the line of this class, else adds one just before the `default` line."""
    lines = text.split("\n")
    found = default_at = None
    seen_header = False
    for i, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not seen_header:
            seen_header = True
            continue
        first = line.split("\t")[0].strip().lower()
        if first == cells[0]:
            found = i
        if first == "default":
            default_at = i
    row = "\t".join(cells)
    if found is not None:
        if lines[found] == row:
            return text, "unchanged"
        lines[found] = row
        return "\n".join(lines), "updated"
    if default_at is not None:
        lines.insert(default_at, row)
    else:
        if lines and lines[-1] == "":
            lines.insert(len(lines) - 1, row)
        else:
            lines.append(row)
    return "\n".join(lines), "added"


def money(x):
    return ("%.2f" % x) if x >= 0.01 else ("%.4f" % x)


# ------------------------------------------------------------------------------------------------ locating

def locate(a):
    """-> (vault, services path). Stops with a plain message when either is missing."""
    vault = fs.vault_path(a.vault)
    if not vault or not os.path.isdir(vault):
        raise Stop("I could not find your vault%s." % (" at " + vault if vault else ""),
                   "pass --vault DIR, or set vault_path in ~/.claude/vault-config.json")
    services = fs.services_path(a.services, vault)
    if not services or not os.path.isfile(services):
        raise Stop("I could not find your service table%s." % (" at " + services if services else ""),
                   "pass --services FILE, or set farm_services in ~/.claude/vault-config.json. A service table is "
                   "the Markdown file with one row per AI engine; the gt-farm skill shows an example")
    return vault, services


def agent_rows(services):
    try:
        rows = fs._parse_table(services)
    except SystemExit as e:
        raise Stop("the service table could not be read (%s)." % e, "open %s and check its table" % services)
    return [r for r in rows if fs._truthy(r.get("agent"))]


def host_of_row(row):
    m = re.match(r"https?://([^/:]+)", row.get("base_url") or "")
    return m.group(1) if m else (row.get("connection") or "an unknown host")


# ------------------------------------------------------------------------------------------------ start

def read_answers(path):
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        raise Stop("the answers file %s could not be read (%s)." % (path, getattr(e, "strerror", None) or "not valid JSON"),
                   "give a JSON object such as {\"provider\": \"openrouter\", \"model\": \"<maker/model-name>\"}")
    if not isinstance(data, dict):
        raise Stop("the answers file %s is not a JSON object." % path, "make it {\"key\": \"value\", ...}")
    bad = sorted(set(data) - set(ANSWER_KEYS))
    if bad:
        raise Stop("the answers file has keys I do not know: %s." % ", ".join(bad),
                   "the keys are: %s" % ", ".join(ANSWER_KEYS))
    return data


def collect(asker):
    """The seven questions. -> cfg dict (no key value is ever in it)."""
    cfg = {}
    say("Agent farming: a hosted AI model fixes code tasks on a COPY of your repo, and you review what it sends back.")
    say("Seven short questions. Press Enter to take the safe answer in brackets.")
    cfg["provider"] = asker.ask("provider", "1. Which provider will run the model? (%s)" % ", ".join(PRESETS),
                                "openrouter", c_provider)
    preset = PRESETS[cfg["provider"]]
    if preset["base_url"]:
        cfg["base_url"] = asker._checked("base_url", asker.answers["base_url"], c_base_url, "base_url") \
            if "base_url" in asker.answers else preset["base_url"]
    else:
        cfg["base_url"] = asker.ask("base_url", "   Its web address (https, ending in /v1 for most services).", None,
                                    c_base_url)
    cfg["host"] = hs.host_of(cfg["base_url"])
    cfg["model"] = asker.ask("model", "2. Which model id? Copy it from the provider's model page.", None, c_model,
                             "It looks like <maker/model-name>. There is no default: the price differs a lot.")
    cfg["name"] = asker.ask("name", "   A short name for this engine.", "agent-" + cfg["provider"], c_name)
    cfg["zone"] = asker._checked("zone", asker.answers["zone"], c_zone, "zone") if "zone" in asker.answers \
        else "personal"
    if str(asker.answers.get("key_source", "file")).strip().lower() != "file":
        raise Stop(NO_STORE, key_file_howto())
    cfg["key_source"] = "file"
    cfg["key_file"] = asker.ask("key_file", "3. Where is the file that holds your API key? (a file only you can read, "
                                "mode 600)", None, c_key_file,
                                "I only check that it exists, has content and is mode 600. I never read or print "
                                "the key. Type none if you have no such file.")
    cfg["task_class"] = asker.ask("task_class", "4. Which class of work may go to it?", AGENT_CLASS, c_class,
                                  "%s is code fixes checked by your tests." % AGENT_CLASS)
    cfg["share_target"] = asker.ask("share_target", "   What percent of those tasks should it get, at most?",
                                    str(DEFAULT_SHARE), c_percent, "Start small. 10 means one task in ten.")
    cfg["ramp"] = asker.ask("ramp", "   Should that share start lower and grow only while audited results stay good?",
                            "yes", c_yesno)
    cfg["daily_budget"] = asker.ask("daily_budget", "5. The most it may spend in a day, in dollars?",
                                    "%g" % DEFAULT_BUDGET, c_budget, "Routing stops for the day when this is reached.")
    cfg["repos"] = asker.ask("repos", "6. Which repos may it see? Folders, separated by commas.", "none", c_repos,
                             "none means none until you name one each time you run a task.")
    cfg["preset"] = preset
    cfg["data_policy"] = preset["data_policy"]
    cfg["provider_label"] = cfg["model"].split("/", 1)[0].lower() if "/" in cfg["model"] else cfg["provider"]
    return cfg


def service_values(cfg):
    vals = {"name": cfg["name"], "model": cfg["model"], "provider": cfg["provider_label"], "tier": "remote",
            "trust": "read-only", "api": cfg["preset"]["api"], "local": "no", "agent": "yes", "tools": "yes",
            "data_policy": cfg["data_policy"]}
    vals.update({"transport": "chat-completions", "base_url": cfg["base_url"], "key_file": cfg["key_file"]})
    return vals


def share_cells(cfg):
    return [cfg["task_class"], "%g" % cfg["share_target"], str(RAMP_STEP if cfg["ramp"] else 0), str(RAMP_WINDOW),
            "0.3", "%g" % cfg["daily_budget"], "0.2", "3", cfg["name"]]


def worst_case(price):
    """Dollars for one task if every step used the full window and the full reply cap."""
    return MAX_STEPS * (WINDOW_TOKENS * float(price["in_per_mtok"]) + MAX_TOKENS_CALL * float(price["out_per_mtok"])) / 1e6


def summary(cfg, engine, price, backend):
    say()
    raw("=" * WIDTH)
    say("SUMMARY: please read this before you say yes")
    raw("=" * WIDTH)
    for s in eg.data_policy_summary(engine):
        say("* " + s, "", "  ")
    if cfg["data_policy"]:
        say("* A data policy is a request to the provider. gt asks for it on every request where the provider "
            "allows it, but it cannot see what the provider does with your code after it arrives.", "", "  ")
    else:
        say("* No data policy is asked for. Only the provider's own terms protect your code.", "", "  ")
    say("* Files that look like secrets are never sent. The model works on a copy of your repo and your real files "
        "are never changed. Its result comes back as a patch that you apply.", "", "  ")
    ceil = worst_case(price)
    say("* Worst case for ONE task: about $%s (assumes a %s-token window and every step at the limit). A normal task "
        "costs far less." % (money(ceil), format(WINDOW_TOKENS, ",")), "", "  ")
    say("* Daily budget: $%g. Past that, routing stops for the day and tasks go to Claude." % cfg["daily_budget"],
        "", "  ")
    if ceil > cfg["daily_budget"]:
        say("Note: one worst-case task is more than your daily budget, so the budget will end that day early.", "  ")
    say("* Share: %g%% of %s tasks%s." % (cfg["share_target"], cfg["task_class"],
                                         ", starting lower and growing by %d points after each clean run of %d tasks"
                                         % (RAMP_STEP, RAMP_WINDOW) if cfg["ramp"] else ""), "", "  ")
    say("* Repos it may see: %s." % (", ".join(cfg["repos"]) if cfg["repos"] else
                                     "none yet; you name one each time with --repo DIR"), "", "  ")
    say("* Sandbox: %s will wall in the commands the model runs." % backend, "", "  ")
    say("* To turn it off at any time: %s off" % SELF, "", "  ")
    raw("=" * WIDTH)


def start(a, deps):
    vault, services = locate(a)
    asker = Asker(read_answers(a.answers), a.yes, deps)
    cfg = collect(asker)
    # 7. the sandbox check
    say()
    say("7. Sandbox check")
    backend = deps.sandbox_fn()
    if not backend:
        raise Stop("this machine has no sandbox that can wall in the commands a model runs, so agent farming would "
                   "run them with no walls.",
                   "nothing was changed. On Linux install bubblewrap (the bwrap program); macOS has its sandbox "
                   "built in; Windows is not supported yet. Run `python3 gt_farm_workspace.py check` to see what was "
                   "found, then run this again")
    say("Found: %s. Commands the model runs get no network and can only touch the copy." % backend, "  ")
    vals = service_values(cfg)
    price = deps.price_fn(cfg["provider_label"], cfg["model"])
    if not price:
        raise Stop("I cannot tell what %s would cost (the price table has no row for provider %s and model %s), so I "
                   "will not turn it on." % (cfg["model"], cfg["provider_label"], cfg["model"]),
                   "nothing was changed. Check the model id spelling; or add the model to the price table (gt-usage "
                   "keeps it) and run this again. A cost that cannot be measured cannot be capped")
    try:
        engine = fs.engine_from_row(dict(vals))
    except SystemExit as e:
        raise Stop("the engine row would not be valid (%s)." % e, "check the provider and model, then run this again")
    engine["base_url"] = cfg["base_url"]
    summary(cfg, engine, price, backend)
    if a.dry_run:
        say("Dry run: nothing was written, no key was asked for or stored. A real run would add the engine row "
            "%s to %s and a line for %s to farm-share.tsv." % (cfg["name"], services, cfg["task_class"]))
        return 0
    if "confirm" in asker.answers:
        sure = str(asker.answers["confirm"]).strip().lower() == "yes"
    elif a.yes:
        sure = True
    else:
        try:
            sure = deps.input_fn("Type yes to set this up, anything else to stop [no] > ").strip().lower() == "yes"
        except (EOFError, OSError):
            sure = False
    if not sure:
        say("Nothing was written. Run it again when you are ready.")
        return 0
    return write_all(a, deps, cfg, vals, vault, services)


def write_all(a, deps, cfg, vals, vault, services):
    say()
    say("Key: your key file will be read only when a task is sent, and never printed.")
    rel_services = os.path.relpath(os.path.abspath(services), os.path.abspath(vault)).replace(os.sep, "/")
    if rel_services.startswith(".."):
        raise Stop("the service table is outside the vault (%s)." % services,
                   "move it inside %s, or add this row by hand: %s" % (vault, "| " + " | ".join(
                       "%s=%s" % kv for kv in sorted(vals.items())) + " |"))
    with open(services, encoding="utf-8") as fh:
        base = fh.read()
    new, action, why = edit_service_table(base, vals)
    if why:
        raise Stop(why + ".", "nothing was written. Fix the table's header row and run this again", 1)
    by_hand = "| " + " | ".join("%s=%s" % kv for kv in sorted(vals.items())) + " |"
    if action != "unchanged":
        ok, msg = deps.queue_fn(vault, rel_services, new, services=services, base_text=base)
        if not ok:
            raise Stop("the engine row could not be written (%s)." % msg,
                       "your service table was not changed. Add this row by hand: " + by_hand, 1)
    say("Engine row: %s (%s)." % (cfg["name"], action))
    share_path = os.path.join(vault, "farm-share.tsv")
    if os.path.isfile(share_path):
        with open(share_path, encoding="utf-8") as fh:
            sbase = fh.read()
        exists = True
    else:
        with open(os.path.normpath(sh.TEMPLATE), encoding="utf-8") as fh:
            sbase = fh.read()
        exists = False
    snew, saction = edit_share_table(sbase, share_cells(cfg))
    try:
        sh.parse_table(snew)
    except ValueError as e:
        raise Stop("the farm-share table would not be valid (%s)." % e,
                   "the engine row is in place; add the %s line to %s by hand" % (cfg["task_class"], share_path), 1)
    if saction != "unchanged" or not exists:
        ok, msg = deps.queue_fn(vault, "farm-share.tsv", snew, services=share_path if exists else None,
                                base_text=sbase if exists else None)
        if not ok:
            raise Stop("the share line could not be written (%s)." % msg,
                       "the engine row is in place; add this line to %s by hand: %s"
                       % (share_path, "\t".join(share_cells(cfg))), 1)
    say("Share line: %s %g%% (%s)." % (cfg["task_class"], cfg["share_target"], saction))
    say()
    say("Done. Routing is still off until you test the engine and turn it on. Next, in this order:")
    for line in ("python3 gt_farm_calibrate.py %s --kind agent-code --dry-run    # see the cost first" % cfg["name"],
                 "python3 gt_farm_calibrate.py %s --kind agent-code --yes-spend  # spends real money" % cfg["name"],
                 "python3 gt_settings.py set farm_share on                       # then turn routing on",
                 "python3 %s status                          # look at it" % SELF,
                 "python3 %s off                             # turn it off" % SELF):
        raw("  " + line)
    return 0


# ------------------------------------------------------------------------------------------------ status

def age_words(date_text, today):
    try:
        d = (today - datetime.date.fromisoformat(str(date_text))).days
    except (TypeError, ValueError):
        return "measured on an unknown date"
    return "measured today" if d <= 0 else "measured %d day%s ago" % (d, "" if d == 1 else "s")


def status(a, deps):
    vault, services = locate(a)
    rows = agent_rows(services)
    say("Agent farming status")
    raw("-" * WIDTH)
    say("Engines in agent mode: %s" % ("none. Run `%s start` to set one up." % SELF if not rows else ""))
    for r in rows:
        raw("  %s  model %s  at %s  results: %s" % (r["name"], r.get("model") or "?", host_of_row(r),
                                                    r.get("trust") or "read-only"))
    say()
    for r in rows:
        c = deps.calibration_fn(vault, r["name"])
        if not c:
            say("Calibration of %s: not measured yet. Run `python3 gt_farm_calibrate.py %s --kind agent-code "
                "--dry-run`." % (r["name"], r["name"]))
        else:
            tail = " It is OUT OF DATE (%s); measure again." % c["stale"] if c.get("stale") else ""
            say("Calibration of %s: %s, %s.%s" % (r["name"], c.get("verdict") or "no verdict",
                                                  age_words(c.get("date"), deps.today), tail))
    say()
    try:
        table = sh.load_share_table(sh.find_table(None, vault))
    except ValueError as e:
        raise Stop("the share table cannot be used (%s)." % e,
                   "run `python3 gt_farm_share.py check-table` and fix the lines it names", 1)
    state = sh.load_state(sh.state_path())
    now = datetime.datetime.combine(deps.today, datetime.datetime.now().time())
    cal = sh._calibrated_from_disk(vault)
    raw(sh.status_text(state, table, now=now, calibrated=cal))
    pauses = []
    for cls in sorted(table):
        cs = state.get("classes", {}).get(cls)
        why = (sh.active_pause(cs, now) or sh.budget_pause(state, cs, table[cls], now)) if cs else None
        if why:
            pauses.append("%s: %s" % (cls, why))
    say()
    say("Pauses: %s" % ("; ".join(pauses) if pauses else "none."))
    say("To turn it off: python3 %s off" % SELF)
    return 0


# ------------------------------------------------------------------------------------------------ off

def off(a, deps):
    cmd = "python3 gt_settings.py set farm_share off"
    if a.dry_run:
        say("Dry run: I would run `%s`. Nothing was changed." % cmd)
        return 0
    ok, msg = deps.setting_fn("farm_share", "off")
    env = os.environ.get("GT_FARM_SHARE", "").strip().lower()
    if not ok:
        problem("I could not change the setting myself (%s)." % (msg or "no reason given"),
                "run this in your own terminal: %s   (or tell Claude: /gt:gt-settings farm_share off)" % cmd)
        return 1
    say("Agent farming is off. Every task stays with Claude. Your engine rows, history and calibration are kept.")
    if env == "on":
        say("But the environment variable GT_FARM_SHARE is set to on and overrides this. Remove it from your shell "
            "settings (or run `unset GT_FARM_SHARE`) to make the switch take effect.")
    say("To turn it on again later: python3 gt_settings.py set farm_share on")
    return 0


# ------------------------------------------------------------------------------------------------ doctor

def doctor(a, deps):
    vault, services = locate(a)
    rows = agent_rows(services)
    if a.engine:
        rows = [r for r in rows if r["name"] == a.engine]
    results = []        # (ok, label, sentence, fix)

    def check(ok, label, good, bad, fix):
        results.append((ok, label, good if ok else bad, fix))

    backend = deps.sandbox_fn()
    check(bool(backend), "sandbox", "%s walls in the model's commands." % backend,
          "no sandbox can wall in the model's commands on this machine.",
          "run `python3 gt_farm_workspace.py check`; on Linux install bubblewrap (bwrap); until then leave "
          "agent farming off")
    if not rows:
        check(False, "engine", "", "no engine is set up for agent mode%s." % (" under the name " + a.engine if a.engine else ""),
              "run `python3 %s start`" % SELF)
    for r in rows:
        n = r["name"]
        local = fs._truthy(r.get("local"))
        kf = os.path.expanduser(r.get("key_file") or "")
        lotr_row = (r.get("transport") or "") == "lotr"
        if lotr_row:
            check(False, "key (%s)" % n, "", "the row uses the secrets-store route (transport lotr), which the agent "
                  "loop cannot use, so a task would stop at the first call.",
                  "run `python3 %s start` again and give a key file, or set the row to transport chat-completions "
                  "with a base_url and a key_file. %s" % (SELF, key_file_howto()))
        elif kf:
            have = os.path.isfile(kf) and os.path.getsize(kf) > 0
            why = mode_problem(kf) if have else ""
            check(have and not why, "key (%s)" % n, "the key file exists, has content and is private (not read)."
                  , ("the key file %s is missing or empty." % kf) if not have else why + ".",
                  "put your key in that file, or run `python3 %s start` again and give a different key file" % SELF
                  if not have else "run `chmod 600 %s`, or on Windows keep the file in your own user folder" % kf)
        elif not local:
            check(False, "key (%s)" % n, "", "the row has no key_file, and the agent loop reads the key only from there.",
                  "run `python3 %s start` again and give a key file. %s" % (SELF, key_file_howto()))
        if not local:
            price = deps.price_fn((r.get("provider") or "").lower(), r.get("model"))
            check(bool(price), "price (%s)" % n, "a price row exists, so cost is measured and capped.",
                  "no price row for provider %s, model %s, so the cost is unknown." % (r.get("provider") or "?",
                                                                                      r.get("model") or "?"),
                  "check the model id in the service table, or add the model to the price table")
        c = deps.calibration_fn(vault, n)
        if not c:
            check(False, "calibration (%s)" % n, "", "the engine has not been measured on code tasks.",
                  "run `python3 gt_farm_calibrate.py %s --kind agent-code --dry-run`, then again with --yes-spend" % n)
        elif c.get("stale"):
            check(False, "calibration (%s)" % n, "", "the measurement is out of date (%s)." % c["stale"],
                  "run `python3 gt_farm_calibrate.py %s --kind agent-code --force --yes-spend`" % n)
        else:
            check(True, "calibration (%s)" % n, "%s, %s." % (c.get("verdict"), age_words(c.get("date"), deps.today)),
                  "", "")
    try:
        table = sh.load_share_table(sh.find_table(None, vault))
        check(True, "share table", "it reads fine (%d class(es))." % len(table), "", "")
    except ValueError as e:
        check(False, "share table", "", "it cannot be read (%s)." % e,
              "run `python3 gt_farm_share.py check-table` and fix the lines it names")
    sp = sh.state_path()
    if not os.path.exists(sp):
        check(True, "state file", "none yet; it is made when the first task is routed.", "", "")
    else:
        try:
            with open(sp, encoding="utf-8") as fh:
                ok = isinstance(json.load(fh).get("classes"), dict)
        except (OSError, ValueError, AttributeError):
            ok = False
        check(ok, "state file", "it reads fine.", "%s cannot be read, so spend and pauses would restart from zero." % sp,
              "move it aside with `mv %s %s.bad`; a fresh one starts at the next task" % (sp, sp))
    bad = 0
    for ok, label, text, fix in results:
        say("%s %s: %s" % ("ok  " if ok else "FAIL", label, text), "", "     ")
        if not ok:
            bad += 1
            say("Fix: " + fix, "     ", "     ")
    say()
    say("All checks passed." if not bad else "%d problem%s found. Fix them in order, then run doctor again."
        % (bad, "" if bad == 1 else "s"))
    return 0 if not bad else 1


# ------------------------------------------------------------------------------------------------ main

def build_parser():
    p = argparse.ArgumentParser(prog=SELF, description=(__doc__ or "").split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd")
    for name, helptext in (("start", "guided setup"), ("status", "one-screen status"), ("off", "turn routing off"),
                           ("doctor", "check the pieces and say how to fix each failure")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--vault", help="the vault (default: vault-config)")
        s.add_argument("--services", help="the service table (default: farm_services in vault-config)")
        s.add_argument("--dry-run", action="store_true", help="show what would happen; write nothing")
        if name == "start":
            s.add_argument("--yes", action="store_true", help="take every default and answer the final question yes")
            s.add_argument("--answers", metavar="FILE", help="a JSON file of answers (no prompts for those keys)")
        if name == "doctor":
            s.add_argument("--engine", help="check one engine")
    return p


def main(argv=None, **inject):
    p = build_parser()
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if not a.cmd:
        p.print_usage(sys.stderr)
        sys.stderr.write("Say what to do: start, status, off or doctor.\n")
        return 2
    deps = Deps(**inject)
    try:
        if a.cmd == "off":
            return off(a, deps)
        return {"start": start, "status": status, "doctor": doctor}[a.cmd](a, deps)
    except Stop as s:
        problem(s.what, s.todo)
        return s.code
    except KeyboardInterrupt:
        sys.stderr.write("Stopped. Nothing further was written.\n")
        return 2
    except OSError as e:
        problem("a file could not be read or written (%s)." % (e.strerror or e), "check the path and the folder's "
                "permissions, then run this again")
        return 1


if __name__ == "__main__":
    sys.exit(main())
