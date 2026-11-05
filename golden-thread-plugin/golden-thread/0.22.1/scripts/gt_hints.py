#!/usr/bin/env python3
"""gt_hints.py -- next-step hints (Stop) and prompt hints (UserPromptSubmit) for the USER (0.21.0).

  gt_hints.py next   --hook     Stop hook: after a reply, ONE `Next: <command> -- ...` line when a
                                rule matches what just happened or the vault state
  gt_hints.py prompt --hook     UserPromptSubmit hook: when the user's own words describe something
                                a gt command does, ONE `Tip: <command> -- ...` line
  gt_hints.py plain [--json]    the plain blocks of every skill found (the single source of the
                                hint text AND of /gt:gt-help's side panel)
  gt_hints.py check             exit 1 when a command a hint can name, or a skill, has no plain block
  gt_hints.py choices <command> the next steps after <command> as the options of an AskUserQuestion
                                (JSON; the same `next:` data as the hints; see hints_as_choices);
                                the user's persona's featured commands are preferred (0.21.4); with
                                the gt-workflow module ON, "Build a workflow" takes the slot
                                /gt:gt-close would have (see workflow_enabled / WORKFLOW_OPTION)

Request 2026-10-08-next-step-and-prompt-hints. WHAT THESE ARE NOT: they never enter the model's
context. Both print `{"systemMessage": "..."}` and nothing else -- the field Claude Code shows to
the user -- so they cost no tokens, and they never block, edit or reorder a prompt. They use
deterministic rules only and make no model call.

THE TEXT. Every hint has three parts, read from the `plain` block of the skill that owns the
command (SKILL.md frontmatter), so the words cannot drift from the page /gt:gt-help shows:

    plain:
      - cmd: "/gt:gt-close"           what to type
        does: "Saves ..."             what it does, in everyday words
        how: "Writes ..."             how it works: what it reads or changes
        detail: "..."                 (optional) internals: files, scripts -- shown on request
        when: "..."  next: "..."      (optional) when to use it; commands that follow it
        hear: "a phrase|another"      (optional) what a user types that this command answers

Values are double-quoted JSON strings, so the block is plain YAML and a hand edit stays safe.

LIMITS. At most one hint per reply or prompt; the same hint is not shown again within 10 turns
(a turn is one hook call in that session, counted in ~/.claude/golden-thread/hints-state.json);
nothing when no rule fits. Settings: `next_step_hints`, `prompt_hints` (both on by default); `hints_as_choices` (on) makes the
per-turn hook ask Claude to end a gt command with a clickable question built by `choices`.

`choices` and the workflow module. When the gt-workflow module is ON (its skill is among the
skills found by skill_roots, and the recorded choice in ~/.claude/golden-thread/install-choices.json
-- else the module.json default -- is "on"), one option reads "Build a workflow" and carries a
`command` key, "/gt-workflow:gt-workflow from this session", which starts the authoring flow from the
conversation. It takes the slot /gt:gt-close would have (close is dropped), except where close is the
natural next step (after /gt:gt-work, or when close is the block's own first `next:` command): there
close stays and the option takes the last slot. A block's first own `next:` command is never pushed
out and the cap of three holds. Module off or absent: the output is unchanged, byte for byte.

Budget: GT_HINTS_BUDGET_MS (next 1500, prompt 200). Fails open: any error or a run past its
budget shows nothing and exits 0. Stdlib only.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
REPEAT_TURNS = 10
MAX_SESSIONS = 30
TAIL_BYTES = 768 * 1024
MARKET = "golden-thread-plugin"

# -- the commands a hint can name (a test fails when one has no plain block) --------------------
# Next-step rules, in the order they are tried. The first whose command was not shown in the last
# REPEAT_TURNS turns wins.
NEXT_RULES = (
    ("writeback", "/gt:gt-close"),
    ("tasks_created", "/gt:gt-list tasks mine"),
    ("handoffs_open", "/gt:gt-handle handoff"),
    ("p1_overdue", "/gt:gt-handle task"),
    ("large_session", "/compact"),
    ("calibration_old", "/gt:gt-calibrate"),
)
NEXT_COMMANDS = tuple(c for _r, c in NEXT_RULES)
# Prompt hints that do not come from a skill's description triggers: (phrases, command). They are
# tried before the description triggers, longest phrase first. "/gt:gt-next" replaces the
# gt-list answer to "what's next" once a skill by that name ships.
PROMPT_FIXED = (
    (("whats next", "what should i do", "what do i do next", "what is next"), "/gt:gt-list tasks mine"),
)
PROMPT_COMMANDS = ("/gt:gt-list tasks mine", "/gt:gt-create task", "/gt:gt-create handoff",
                   "/gt:gt-complete", "/gt-usage:gt-usage", "/gt:gt-doctor")


# -- reading plain blocks ------------------------------------------------------------------------
def norm(text):
    """Lower-case words, apostrophes dropped, everything else a single space."""
    t = str(text).lower().replace("’", "'").replace("'", "")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def _value(raw):
    raw = raw.strip()
    if raw.startswith('"'):
        try:
            return json.loads(raw)
        except ValueError:
            return raw.strip('"')
    return raw.strip("'")


def _head(text):
    text = text.replace("\r\n", "\n")
    if not text.startswith("---"):
        return []
    lines = text.split("\n")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[1:i]
    return []


def parse_plain(text):
    """-> [ {cmd, does, how, detail?, when?, next?, hear: [..]} ] from a SKILL.md's frontmatter."""
    head = _head(text)
    out, cur, inside = [], None, False
    for ln in head:
        if not inside:
            if ln.rstrip() == "plain:":
                inside = True
            continue
        if ln and not ln[0].isspace():
            break                                   # the next top-level key
        m = re.match(r"^\s*(-\s+)?([a-z_]+):\s*(.*)$", ln)
        if not m:
            continue
        if m.group(1):
            cur = {}
            out.append(cur)
        if cur is None:
            continue
        cur[m.group(2)] = _value(m.group(3))
    for b in out:
        b["hear"] = [h.strip() for h in str(b.get("hear") or "").split("|") if h.strip()]
    return [b for b in out if b.get("cmd")]


def description(text):
    for ln in _head(text):
        if ln.startswith("description:"):
            return _value(ln.split(":", 1)[1])
    return ""


def triggers(desc):
    """The phrases after 'Use when the user says:' (or 'Use when:'), placeholders dropped."""
    m = re.search(r"Use when(?: the user says)?:\s*(.+)$", desc or "", re.S)
    if not m:
        return []
    body = re.split(r"\.\s+[A-Z]|\.$", m.group(1).strip())[0]
    out = []
    for ph in body.split(","):
        ph = re.sub(r"\b[XY]\b", "", ph.replace("’", "'"))
        ph = norm(ph)
        if len(ph.split()) >= 2 and len(ph) >= 8:
            out.append(ph)
    return out


def _versions(parent):
    try:
        names = [d for d in os.listdir(parent) if re.fullmatch(r"\d+\.\d+\.\d+", d)]
    except OSError:
        return []
    return sorted(names, key=lambda s: tuple(int(x) for x in s.split(".")), reverse=True)


def skill_roots():
    """The skills/ folders to read: GT_HINTS_SKILLS (tests), else this release and its sibling
    modules in a source checkout, else the newest of each plugin in the plugin cache."""
    env = os.environ.get("GT_HINTS_SKILLS")
    if env is not None:
        return [r for r in env.split(os.pathsep) if r]
    roots = []
    if (HERE.parent / "skills").is_dir():
        roots.append(str(HERE.parent / "skills"))
        repo = HERE.parent.parent.parent
        for m in sorted(repo.glob("golden-thread-*")):
            vs = _versions(m)
            if vs and (m / vs[0] / "skills").is_dir():
                roots.append(str(m / vs[0] / "skills"))
    cache = Path(os.path.expanduser("~")) / ".claude" / "plugins" / "cache" / MARKET
    try:
        plugins = sorted(os.listdir(cache))
    except OSError:
        plugins = []
    for p in plugins:
        vs = _versions(cache / p)
        if vs and (cache / p / vs[0] / "skills").is_dir():
            r = str(cache / p / vs[0] / "skills")
            if r not in roots:
                roots.append(r)
    return roots


def catalog(roots=None):
    """-> {cmd: block}: every plain block, with skill, key (the gt-help page), triggers and root.
    The first block for a command wins."""
    out = {}
    for root in (roots if roots is not None else skill_roots()):
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            f = Path(root) / name / "SKILL.md"
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            blocks = parse_plain(text)
            trig = triggers(description(text))
            for b in blocks:
                b["skill"] = name
                b["key"] = name[3:] if name.startswith("gt-") else name
                b["root"] = str(root)
                b["triggers"] = trig
                out.setdefault(b["cmd"], b)
    return out


# -- the line ------------------------------------------------------------------------------------
def _soft(s):
    s = " ".join(str(s).split()).rstrip(".")
    if len(s) > 1 and s[0].isupper() and s[1].islower():
        s = s[0].lower() + s[1:]
    return s


def _first(s, cap=110):
    """The first sentence, cut at a word boundary: a hint is one short line."""
    s = " ".join(str(s).split())
    s = re.split(r"(?<=[a-z0-9\)])\.\s", s)[0]
    if len(s) > cap:
        cut = max(s.rfind(sep, 0, cap) for sep in (", ", "; ", " and "))
        s = s[:cut] if cut >= 35 else s[:cap].rsplit(" ", 1)[0]
        s = s.rstrip(",;: ")
    return s


def line(prefix, cmd, cat):
    b = cat.get(cmd)
    if not b:
        return None
    text = "%s %s — %s (%s)" % (prefix, cmd, _soft(_first(b["does"], 90)), _soft(_first(b["how"])))
    return text + " — see it run: /gt:gt-help %s" % b["key"]


# -- settings, state -----------------------------------------------------------------------------
def home():
    return os.path.expanduser("~")


def _config():
    try:
        with open(os.path.join(home(), ".claude", "vault-config.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:                               # noqa: BLE001
        return {}


def enabled(cfg, name):
    return str(cfg.get(name, "on")).strip().lower() != "off"


def _state_path():
    return os.path.join(home(), ".claude", "golden-thread", "hints-state.json")


def _load_state():
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict) and isinstance(d.get("sessions"), dict):
            return d
    except Exception:                               # noqa: BLE001
        pass
    return {"v": 1, "sessions": {}}


STALE_TMP_SECONDS = 3600


def _save_state(st):
    """Write the state through a temp file and a rename. The text is built first and written with
    one os.write, so the window between creating the temp file and the rename is as short as it can
    be; a temp file that still outlives it (the hook killed at its timeout) is removed by a later run
    once it is an hour old (_clean_tmp). Called from the hook's MAIN thread only (0.21.4)."""
    sessions = st["sessions"]
    if len(sessions) > MAX_SESSIONS:
        for k in sorted(sessions, key=lambda k: sessions[k].get("at", 0))[:len(sessions) - MAX_SESSIONS]:
            del sessions[k]
    path = _state_path()
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        data = json.dumps(st).encode("utf-8")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o644)
        try:
            while data:
                data = data[os.write(fd, data):]
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except (OSError, ValueError, TypeError):
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _clean_tmp(now=None):
    """Remove hints-state.json.<pid>.tmp files older than STALE_TMP_SECONDS: left by a run killed
    between the write and the rename (0.21.4: 81 of them were found on one machine). A newer one may
    belong to a run in progress and is left alone. Never raises."""
    now = time.time() if now is None else now
    path = _state_path()
    d, base = os.path.dirname(path), os.path.basename(path) + "."
    try:
        names = os.listdir(d)
    except OSError:
        return 0
    n = 0
    for name in names:
        if not (name.startswith(base) and name.endswith(".tmp")):
            continue
        p = os.path.join(d, name)
        try:
            if now - os.stat(p).st_mtime > STALE_TMP_SECONDS:
                os.unlink(p)
                n += 1
        except OSError:
            pass
    return n


class Turns:
    """The turn counter and the 'shown at' marks of one session and one hook kind."""

    def __init__(self, kind, sid):
        self.kind, self.sid = kind, str(sid or "-")[:80]
        self.st = _load_state()
        self.rec = self.st["sessions"].setdefault(self.sid, {"t": {}, "seen": {}})
        self.rec.setdefault("t", {})
        self.rec.setdefault("seen", {})
        self.turn = int(self.rec["t"].get(kind, 0)) + 1
        self.rec["t"][kind] = self.turn

    def recent(self, cmd):
        last = self.rec["seen"].get("%s:%s" % (self.kind, cmd))
        return last is not None and self.turn - int(last) < REPEAT_TURNS

    def shown(self, cmd):
        self.rec["seen"]["%s:%s" % (self.kind, cmd)] = self.turn

    def save(self):
        self.rec["at"] = time.time()
        _save_state(self.st)
        _clean_tmp()


# -- next-step rules -----------------------------------------------------------------------------
def last_turn(path):
    """-> (tools, tokens): the (name, input) of every tool call since the user's last prompt, and
    the context size (input + cache read + cache write) of the newest reply that reports one."""
    tools, tokens = [], 0
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - TAIL_BYTES))
        raw = fh.read().decode("utf-8", "replace")
    rows = []
    for ln in raw.splitlines():
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict):
            rows.append(r)
    start = 0
    for i, r in enumerate(rows):
        if r.get("type") != "user" or r.get("isSidechain") or r.get("isMeta"):
            continue
        c = (r.get("message") or {}).get("content")
        real = isinstance(c, str) or (isinstance(c, list) and any(
            isinstance(x, dict) and x.get("type") == "text" for x in c))
        if real:
            start = i + 1
    for r in rows[start:]:
        if r.get("type") != "assistant" or r.get("isSidechain"):
            continue
        msg = r.get("message") or {}
        for x in msg.get("content") or []:
            if isinstance(x, dict) and x.get("type") == "tool_use":
                tools.append((str(x.get("name") or ""), x.get("input") if isinstance(x.get("input"), dict) else {}))
    for r in reversed(rows):
        u = (r.get("message") or {}).get("usage") if r.get("type") == "assistant" and not r.get("isSidechain") else None
        if isinstance(u, dict):
            try:
                tokens = sum(int(u.get(k) or 0) for k in
                             ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            except (TypeError, ValueError):
                tokens = 0
            break
    return tools, tokens


def _skill_called(tools, *names):
    for n, inp in tools:
        if n == "Skill":
            s = str(inp.get("skill") or "")
            if s.split(":")[-1] in names:
                return inp
    return None


class Ctx:
    def __init__(self, payload, cfg, deadline):
        self.cfg, self.deadline = cfg, deadline
        self.vault = os.environ.get("GT_VAULT") or cfg.get("vault_path") or ""
        self.tools, self.tokens = [], 0
        tp = (payload or {}).get("transcript_path")
        if isinstance(tp, str) and tp:
            try:
                self.tools, self.tokens = last_turn(tp)
            except Exception:                       # noqa: BLE001
                pass

    def remaining(self):
        return max(0.05, self.deadline - time.time())


def rule_writeback(c):
    return bool(_skill_called(c.tools, "gt-work")) and not _skill_called(c.tools, "gt-close")


def rule_tasks_created(c):
    for n, inp in c.tools:
        if n == "Bash" and "gt_task.py" in str(inp.get("command") or "") \
                and re.search(r"\badd\b", str(inp.get("command") or "")):
            return True
    s = _skill_called(c.tools, "gt-create")
    return bool(s and re.match(r"\s*task\b", str(s.get("args") or "")))


def rule_handoffs_open(c):
    if not c.vault or not (Path(c.vault) / "Projects").is_dir():
        return False
    sys.path.insert(0, str(HERE))
    import gt_handoff_status as hs
    return bool(hs.waiting(Path(c.vault)))


_CHILDREN = []                             # helper processes a rule started


def _reap_children():
    """The hook ends when its budget does, which can be before a rule's own timeout fires: kill what
    a rule started so no helper outlives the hook (it held the working folder open on Windows)."""
    for ch in _CHILDREN:
        try:
            if ch.poll() is None:
                ch.kill()
        except OSError:
            pass


def rule_p1_overdue(c):
    tool = Path(c.vault) / "Projects" / "golden-thread" / "tools" / "gt_task.py" if c.vault else None
    if not tool or not tool.is_file():
        return False
    p = subprocess.Popen([sys.executable, "-B", str(tool), "count", "--vault", c.vault, "--json"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _CHILDREN.append(p)                    # main() kills it if the budget ends first (0.21.4)
    try:
        out, _err = p.communicate(timeout=c.remaining())
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise
    d = json.loads(out) if p.returncode == 0 else {}
    return int(d.get("overdue") or 0) > 0


def rule_large_session(c):
    raw = str(c.cfg.get("expiry_guard.min_tokens") or "150000").strip()
    return c.tokens >= (int(raw) if raw.isdigit() and int(raw) >= 1 else 150000)


def rule_calibration_old(c):
    raw = str(c.cfg.get("model_calibration_remind_days") or "60").strip()
    days = int(raw) if raw.isdigit() else 60
    if days <= 0:
        return False
    with open(os.path.join(home(), ".claude", "golden-thread", "model-calibration", "latest.json"),
              encoding="utf-8") as fh:
        upd = str(json.load(fh).get("updated") or "")
    import datetime
    when = datetime.datetime.fromisoformat(upd[:19])
    return (datetime.datetime.now() - when).days >= days


RULES = {"writeback": rule_writeback, "tasks_created": rule_tasks_created,
         "handoffs_open": rule_handoffs_open, "p1_overdue": rule_p1_overdue,
         "large_session": rule_large_session, "calibration_old": rule_calibration_old}


def next_hint(payload, cfg, turns, cat, deadline):
    """-> the Next: line or None. Rules are tried in table order; one whose command was shown in
    the last REPEAT_TURNS turns is passed over, so the next rule can speak."""
    c = Ctx(payload, cfg, deadline)
    for name, cmd in NEXT_RULES:
        if turns.recent(cmd):
            continue
        try:
            hit = RULES[name](c)
        except Exception:                           # noqa: BLE001 - a broken rule is no rule
            hit = False
        if hit:
            text = line("Next:", cmd, cat)
            if text:
                turns.shown(cmd)
                return text
    return None


# -- prompt rules --------------------------------------------------------------------------------
def prompt_candidates(cat):
    """-> [(phrase, command, order)]: fixed phrases first, then the hear / description phrases of
    each skill. '/gt:gt-next' takes over the 'what's next' phrases once that skill exists."""
    out, order = [], 0
    nxt = cat.get("/gt:gt-next")
    for phrases, cmd in PROMPT_FIXED:
        for ph in phrases:
            out.append((norm(ph), "/gt:gt-next" if nxt else cmd, order))
            order += 1
    by_skill = {}
    for b in cat.values():
        by_skill.setdefault(b["skill"], []).append(b)
    for b in cat.values():
        own = b["hear"] or (b["triggers"] if len(by_skill[b["skill"]]) == 1 else [])
        for ph in own:
            if len(norm(ph).split()) >= 2:
                out.append((norm(ph), b["cmd"], order))
                order += 1
    return out


def prompt_match(prompt, cat):
    text = " %s " % norm(prompt[:600])
    best = None
    for ph, cmd, order in prompt_candidates(cat):
        if " %s " % ph in text:
            key = (-len(ph.split()), order)
            if best is None or key < best[0]:
                best = (key, cmd)
    return best[1] if best else None


def prompt_hint(prompt, turns, cat):
    if not isinstance(prompt, str):
        return None
    p = prompt.strip()
    # a slash command is never a tip trigger: typed raw (`/gt:gt-next ...`, what Claude Code 2.1.299 and
    # later hand this hook) or in the expanded form older versions handed it (0.22.0)
    if not p or p.startswith(("/", "<command-name>", "<command-message>")):
        return None
    cmd = prompt_match(p, cat)
    if not cmd or turns.recent(cmd):
        return None
    text = line("Tip:", cmd, cat)
    if text:
        turns.shown(cmd)
    return text


# -- the hooks -----------------------------------------------------------------------------------
def _budget(default_ms):
    try:
        return max(0.0, float(os.environ.get("GT_HINTS_BUDGET_MS", default_ms)) / 1000.0)
    except ValueError:
        return default_ms / 1000.0


def run_hook(kind):
    try:
        payload = json.load(sys.stdin)
    except Exception:                               # noqa: BLE001
        return
    if not isinstance(payload, dict):
        return
    cfg = _config()
    name = "next_step_hints" if kind == "next" else "prompt_hints"
    if not enabled(cfg, name):
        return
    budget = _budget(1500 if kind == "next" else 200)
    if budget <= 0:
        return
    deadline = time.time() + budget
    box = {}

    def work():
        cat = catalog()
        turns = Turns(kind, payload.get("session_id"))
        if kind == "next":
            msg = next_hint(payload, cfg, turns, cat, deadline)
        else:
            msg = prompt_hint(payload.get("prompt"), turns, cat)
        box["turns"] = turns
        box["msg"] = msg

    # The worker never writes the state: it is abandoned at the budget (a daemon thread dies with
    # the process), and one stopped between its temp file and the rename left that file behind for
    # good (0.21.4). The main thread saves, and only when the work finished; a turn whose work ran
    # past the budget is not counted.
    t = threading.Thread(target=lambda: _safely(work), daemon=True)
    t.start()
    t.join(budget)
    alive = t.is_alive()
    if alive:
        _reap_children()
    elif "turns" in box:
        _safely(box["turns"].save)
    msg = box.get("msg") if not alive else None
    if msg:
        sys.stdout.write(json.dumps({"systemMessage": msg}) + "\n")
        sys.stdout.flush()


def _safely(fn):
    try:
        fn()
    except Exception:                               # noqa: BLE001
        pass


# -- CLI -----------------------------------------------------------------------------------------
def missing():
    """-> the problems `check` reports: commands a hint can name, and skills, without a block."""
    cat = catalog()
    bad = ["%s: no plain block" % c for c in NEXT_COMMANDS + PROMPT_COMMANDS if c not in cat]
    for root in skill_roots():
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for n in names:
            f = Path(root) / n / "SKILL.md"
            if f.is_file() and not parse_plain(f.read_text(encoding="utf-8")):
                bad.append("%s: skill has no plain block" % n)
    return bad


# -- next steps as the options of a question (hints_as_choices) --------------------------------------
MAX_CHOICES, MIN_CHOICES = 3, 2


def _find_block(text, cat):
    """The plain block for what the person or Claude typed: '/gt:gt-close', 'gt-close' or a variant
    with arguments ('/gt:gt-list tasks mine'). -> (command, block) or (None, None)."""
    t = " ".join(str(text or "").split())
    if t in cat:
        return t, cat[t]
    word = t.split(" ")[0].lstrip("/").split(":")[-1] if t else ""
    if not word:
        return None, None
    same = [c for c in cat if c.lstrip("/").split(":")[-1].split(" ")[0] == word]
    if not same:
        return None, None
    typed = [c for c in same if t.startswith(c)]
    pick = max(typed, key=len) if typed else same[0]          # else the skill's first (main) variant
    return pick, cat[pick]


def persona_featured():
    """-> the user's personas' featured gt commands (gt_persona, 0.21.4), or [] -- no persona, no
    gt_persona beside this script, or any failure. Preference only: it never adds or hides a rule."""
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import gt_persona                                         # noqa: PLC0415
        names = gt_persona.resolve()
        return list(gt_persona.featured_skills(names)) if names else []
    except Exception:                                             # noqa: BLE001
        return []


WORKFLOW_SKILL = "gt-workflow"
WORKFLOW_COMMAND = "/gt-workflow:gt-workflow from this session"
WORKFLOW_OPTION = {"label": "Build a workflow",
                   "description": "Save what we just did as a workflow you can run again by name (/gt-workflow)",
                   "command": WORKFLOW_COMMAND}


def workflow_enabled(roots=None):
    """True when the gt-workflow module is ON: its skill is in a skills root (installed or source,
    the roots skill_roots finds) and the module is chosen on -- the choice recorded in
    ~/.claude/golden-thread/install-choices.json, else the default in the module's module.json
    (off when neither says). Never raises."""
    try:
        for root in (roots if roots is not None else skill_roots()):
            if not (Path(root) / WORKFLOW_SKILL / "SKILL.md").is_file():
                continue
            state = None
            try:
                with open(os.path.join(home(), ".claude", "golden-thread", "install-choices.json"),
                          encoding="utf-8") as fh:
                    ch = (json.load(fh) or {}).get("choices") or {}
                state = ch.get("workflow") if isinstance(ch, dict) else None
            except Exception:                                     # noqa: BLE001
                state = None
            if state not in ("on", "off"):
                try:
                    with open(Path(root).parent / "module.json", encoding="utf-8") as fh:
                        state = (json.load(fh) or {}).get("default")
                except Exception:                                 # noqa: BLE001
                    state = None
            if state == "on":
                return True
    except Exception:                                             # noqa: BLE001
        pass
    return False


def _with_workflow(opts, cmd, first_own):
    """opts (commands) -> the option list with "Build a workflow" in close's slot. See the module
    docstring. Returns a list of commands and the WORKFLOW_OPTION marker."""
    close = "/gt:gt-close"
    keep_close = close == first_own or str(cmd).split(" ")[0] == "/gt:gt-work"
    if close in opts and not keep_close:
        i = opts.index(close)
        return opts[:i] + [WORKFLOW_OPTION] + opts[i + 1:]
    if len(opts) < MAX_CHOICES:
        return opts + [WORKFLOW_OPTION]
    return opts[:MAX_CHOICES - 1] + [WORKFLOW_OPTION]


def choices_for(text, cat, featured=None, workflow=False):
    """-> the AskUserQuestion payload for the steps after `text`: the block's own `next:` commands
    first, then the vault-state rules' commands as padding up to two, at most three, never the
    command that just ran. An unknown command has no options.

    `featured` (the persona's featured commands, 0.21.4) is a preference where this CHOOSES: among
    the block's own `next:` commands the featured ones go first (so they survive the cap of three),
    and padding takes featured commands before the rule commands. None or [] changes nothing.

    `workflow` (True when workflow_enabled()) puts the "Build a workflow" option in close's slot;
    False changes nothing."""
    cmd, block = _find_block(text, cat)
    opts = []
    feat = []
    first_own = None
    for f in featured or []:
        f = _find_block(f, cat)[0]
        if f and f not in feat:
            feat.append(f)
    if block:
        own = [c.strip() for c in str(block.get("next") or "").split(",") if c.strip()]
        pad = [c for _r, c in NEXT_RULES]
        if feat:
            own = ([c for c in own if _find_block(c, cat)[0] in feat]
                   + [c for c in own if _find_block(c, cat)[0] not in feat])
            pad = feat + [c for c in pad if c not in feat]
        for c in own:
            c = _find_block(c, cat)[0]                         # '/gt:gt-list' -> its main variant
            if c and c != cmd and first_own is None:
                first_own = c
            if c and c != cmd and c not in opts:
                opts.append(c)
        for c in pad:
            if len(opts) >= MIN_CHOICES:
                break
            if c in cat and c != cmd and c not in opts:
                opts.append(c)
        opts = opts[:MAX_CHOICES]
        if workflow and WORKFLOW_SKILL not in cmd:
            opts = _with_workflow(opts, cmd, first_own)
    return {"question": "What would you like to do next?", "header": "Next step",
            "multiSelect": False,
            "options": [dict(c) if isinstance(c, dict) else
                        {"label": c, "description": _soft_cap(cat[c]["does"])} for c in opts],
            "note": "The person can also type their own answer instead of picking one."}


def _soft_cap(s, cap=120):
    s = " ".join(str(s).split())
    return s if len(s) <= cap else s[:cap].rsplit(" ", 1)[0].rstrip(",;: ") + "..."


def main(argv):
    cmd = argv[0] if argv else ""
    if cmd == "choices":
        print(json.dumps(choices_for(" ".join(argv[1:]), catalog(), persona_featured(),
                                     workflow_enabled()), indent=1))
        return 0
    if cmd in ("next", "prompt"):
        if "--hook" in argv:
            run_hook(cmd)
            return 0
        print("gt_hints.py %s --hook  (reads the hook payload on stdin)" % cmd)
        return 0
    if cmd == "plain":
        cat = catalog()
        if "--json" in argv:
            print(json.dumps(list(cat.values()), indent=1))
        else:
            for c, b in cat.items():
                print("%-34s %s" % (c, b["does"]))
        return 0
    if cmd == "check":
        bad = missing()
        for b in bad:
            print(b)
        print("gt_hints: %s" % ("%d problem(s)" % len(bad) if bad else "every command has a plain block"))
        return 1 if bad else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    # paranoid.signed_manifest (0.21.0): a hook checks its own files first (one stat() when off).
    try:
        import gt_integrity_gate as _gig
    except ImportError:
        _gig = None
    if _gig is not None and "--hook" in sys.argv:
        _gig.check(__file__, "reporter", when=True)
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:                               # noqa: BLE001
        sys.exit(0 if "--hook" in sys.argv else 1)  # a hook fails open, silently
