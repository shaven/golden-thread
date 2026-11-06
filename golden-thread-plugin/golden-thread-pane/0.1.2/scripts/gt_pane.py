#!/usr/bin/env python3
"""gt_pane.py -- the user's button list for the gt pane (0.21.0, item #41).

  gt_pane.py list [--json]                    the buttons, numbered from 1
  gt_pane.py move <from> <to>                 take the button at <from> and put it at <to>
  gt_pane.py swap <a> <b>
  gt_pane.py add "label" "prompt" [--at n]    at the end, or at position n
  gt_pane.py remove <n>
  gt_pane.py rename <n> "label"
  gt_pane.py set-prompt <n> "prompt"
  gt_pane.py reset [--persona p]              replace the list with a role's defaults
  gt_pane.py seed                             write the defaults only when there is no list yet
  gt_pane.py --hook                           the SessionStart hook: seed quietly, never fail

THE LIST is ~/.claude/golden-thread/pane/buttons.json -- {"version": 1, "buttons": [{"label",
"prompt"}]} -- the user's own file. It is never in the plugin and nothing here overwrites it
except an explicit edit or `reset` (which keeps the old file beside it). `GT_PANE_FILE` moves it
for tests.

THE RULES, enforced on every write: 1 to 12 buttons; a label of at most 22 characters and a
prompt of at most 500, neither empty; no control character in either (a prompt is one line of
plain words). A refused change leaves the file byte-identical. Writes are atomic: a temp file in
the same folder, fsync, then os.replace.

DEFAULTS come from the persona packs ("pane" in packs/personas/<name>.json: 12 buttons per
persona), the first of the user's personas (vault-config.json `persona`) that has one, else
packs/pane/generic.json. Standard library only, Python 3.8+.

Exit 0 done, 1 refused (the reason is printed), 2 usage.
"""
import glob
import json
import os
import sys
import tempfile
import time
import unicodedata

MAX_BUTTONS = 12
MAX_LABEL = 22
MAX_PROMPT = 500
VERSION = 1
HERE = os.path.dirname(os.path.abspath(__file__))


class PaneError(Exception):
    pass


# ---------------------------------------------------------------------------------------- paths
def home():
    return os.path.expanduser("~")


def list_path():
    return os.environ.get("GT_PANE_FILE") or os.path.join(
        home(), ".claude", "golden-thread", "pane", "buttons.json")


def packs_dirs():
    """Where the packs may be, best first: an override, beside the hooks dir, the installed copy,
    then the newest gt in the plugin cache."""
    out = []
    if os.environ.get("GT_PANE_PACKS"):
        out.append(os.environ["GT_PANE_PACKS"])
    out.append(os.path.join(os.path.dirname(HERE), "packs"))
    out.append(os.path.join(home(), ".claude", "golden-thread", "packs"))
    cache = glob.glob(os.path.join(home(), ".claude", "plugins", "cache",
                                   "golden-thread-plugin", "gt", "*", "packs"))
    out += sorted(cache, key=lambda p: [int(x) if x.isdigit() else 0
                                        for x in os.path.basename(os.path.dirname(p)).split(".")],
                  reverse=True)
    return [d for d in out if os.path.isdir(d)]


def _json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------------------------- validating
def _bad_char(ch):
    cat = unicodedata.category(ch)
    return cat in ("Cc", "Zl", "Zp") or ch in "‪‫‬‭‮⁦⁧⁨⁩"


def check_text(value, what, limit):
    if not isinstance(value, str) or not value.strip():
        raise PaneError("the %s cannot be empty" % what)
    if any(_bad_char(c) for c in value):
        raise PaneError("the %s has a control character in it (it must be one line of plain words)" % what)
    if len(value) > limit:
        raise PaneError("the %s is %d characters; the limit is %d" % (what, len(value), limit))
    return value


def check_button(b, n=None):
    where = "" if n is None else "button %d: " % n
    if not isinstance(b, dict) or set(b) != {"label", "prompt"}:
        raise PaneError(where + "a button is {label, prompt}")
    try:
        check_text(b["label"], "label", MAX_LABEL)
        check_text(b["prompt"], "prompt", MAX_PROMPT)
    except PaneError as exc:
        raise PaneError(where + str(exc))
    return {"label": b["label"], "prompt": b["prompt"]}


def check_list(buttons):
    if not isinstance(buttons, list) or not buttons:
        raise PaneError("the pane needs at least one button")
    if len(buttons) > MAX_BUTTONS:
        raise PaneError("the pane holds at most %d buttons" % MAX_BUTTONS)
    return [check_button(b, i + 1) for i, b in enumerate(buttons)]


# --------------------------------------------------------------------------------- read and write
def read_buttons(path):
    """-> the validated list. PaneError names the problem and how to fix it."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise PaneError("there is no button list yet (run: gt_pane.py seed)")
    except (OSError, ValueError):
        raise PaneError("%s is damaged (not valid JSON); `gt_pane.py reset` replaces it and keeps "
                        "the damaged file beside it" % path)
    try:
        if not isinstance(data, dict) or data.get("version") != VERSION:
            raise PaneError("version must be %d" % VERSION)
        return check_list(data.get("buttons"))
    except PaneError as exc:
        raise PaneError("%s is not usable (%s); `gt_pane.py reset` replaces it and keeps the old "
                        "file beside it" % (path, exc))


def write_buttons(path, buttons):
    """Validate, then write atomically. The old file is untouched unless the whole write succeeds."""
    buttons = check_list(buttons)
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    text = json.dumps({"version": VERSION, "buttons": buttons}, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".buttons-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------------------- defaults
def _names(value):
    if isinstance(value, str):
        value = value.replace("[", "").replace("]", "").split(",")
    out = []
    for v in value if isinstance(value, list) else []:
        v = str(v).strip().strip("'\"").lower()
        if v and v != "none" and v not in out:
            out.append(v)
    return out


def user_personas():
    d = _json(os.path.join(home(), ".claude", "vault-config.json"))
    return _names(d.get("persona")) if isinstance(d, dict) else []


def _pack_pane(name):
    for d in packs_dirs():
        data = _json(os.path.join(d, "personas", name + ".json"))
        if isinstance(data, dict) and "pane" in data:
            try:
                return check_list(data["pane"])
            except PaneError:
                continue
    return None


def valid_personas():
    names = set()
    for d in packs_dirs():
        for p in glob.glob(os.path.join(d, "personas", "*.json")):
            n = os.path.basename(p)[:-5]
            if not n.endswith(".schema") and _pack_pane(n):
                names.add(n)
    return sorted(names)


def generic_pane():
    for d in packs_dirs():
        data = _json(os.path.join(d, "pane", "generic.json"))
        if isinstance(data, dict):
            try:
                return check_list(data.get("pane"))
            except PaneError:
                continue
    return None


def default_buttons(persona=None):
    if persona:
        got = _pack_pane(persona)
        if got is None:
            raise PaneError("no pane defaults for persona '%s'; valid: %s"
                            % (persona, ", ".join(valid_personas()) or "(no packs installed)"))
        return got
    for p in user_personas():
        got = _pack_pane(p)
        if got:
            return got
    got = generic_pane()
    if got is None:
        raise PaneError("no persona packs are installed, so there are no defaults to seed from")
    return got


def seed(path):
    """Write the defaults when there is no list at all. Never touches an existing file."""
    if os.path.lexists(path):
        return False
    write_buttons(path, default_buttons())
    return True


def _keep_old(path):
    """Put the file being replaced beside the new one. -> its new name, or None."""
    if not os.path.lexists(path):
        return None
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    dest, n = "%s.before-reset-%s" % (path, stamp), 1
    while os.path.lexists(dest):
        n += 1
        dest = "%s.before-reset-%s-%d" % (path, stamp, n)
    os.replace(path, dest)
    return dest


# ------------------------------------------------------------------------------------ the commands
def _pos(text, n, what="position", extra=0):
    try:
        i = int(text)
    except (TypeError, ValueError):
        raise PaneError("%s must be a number, got '%s'" % (what, text))
    if i < 1 or i > n + extra:
        raise PaneError("%s %d is out of range; the pane has %d button%s"
                        % (what, i, n, "" if n == 1 else "s"))
    return i - 1


def show(buttons):
    return "\n".join("%2d. %-22s  %s" % (i + 1, b["label"], b["prompt"]) for i, b in enumerate(buttons))


def run(argv):
    path = list_path()
    if not argv:
        raise Usage()
    cmd, args = argv[0], argv[1:]
    if cmd not in ("list", "seed", "reset", "move", "swap", "add", "remove", "rename", "set-prompt"):
        raise Usage()
    if cmd == "list":
        bs = read_buttons(path)
        if "--json" in args:
            print(json.dumps({"file": path, "buttons": bs}, indent=2, ensure_ascii=False))
        else:
            print(show(bs))
        return 0
    if cmd == "seed":
        print("seeded %s" % path if seed(path) else "kept your list: %s" % path)
        return 0
    if cmd == "reset":
        persona = None
        if args[:1] == ["--persona"] and len(args) == 2:
            persona = args[1].strip().lower()
        elif args:
            raise Usage()
        fresh = default_buttons(persona)
        old = _keep_old(path) if os.path.lexists(path) else None
        try:
            write_buttons(path, fresh)
        except BaseException:
            if old:
                os.replace(old, path)
            raise
        if old:
            print("kept the old list as %s" % old)
        print(show(fresh))
        return 0
    bs = read_buttons(path)
    n = len(bs)
    if cmd == "move" and len(args) == 2:
        a, b = _pos(args[0], n, "from"), _pos(args[1], n, "to")
        out = bs[:]
        out.insert(b, out.pop(a))
    elif cmd == "swap" and len(args) == 2:
        a, b = _pos(args[0], n), _pos(args[1], n)
        out = bs[:]
        out[a], out[b] = out[b], out[a]
    elif cmd == "add" and len(args) in (2, 4) and (len(args) == 2 or args[2] == "--at"):
        at = _pos(args[3], n, "position", extra=1) if len(args) == 4 else n
        out = bs[:]
        out.insert(at, {"label": args[0], "prompt": args[1]})
    elif cmd == "remove" and len(args) == 1:
        i = _pos(args[0], n)
        if n == 1:
            raise PaneError("the pane needs at least one button; add another first, or reset")
        out = bs[:]
        del out[i]
    elif cmd == "rename" and len(args) == 2:
        i = _pos(args[0], n)
        out = bs[:]
        out[i] = {"label": args[1], "prompt": bs[i]["prompt"]}
    elif cmd == "set-prompt" and len(args) == 2:
        i = _pos(args[0], n)
        out = bs[:]
        out[i] = {"label": bs[i]["label"], "prompt": args[1]}
    else:
        raise Usage()
    write_buttons(path, out)
    print(show(out))
    return 0


class Usage(Exception):
    pass


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    if a == ["--hook"]:
        # SessionStart: make sure a list exists. Silent; never blocks a session.
        try:
            seed(list_path())
        except Exception:                                  # noqa: BLE001
            pass
        try:                                               # keep the Obsidian panel's allowance line current
            import gt_home
            gt_home.auto_refresh()
        except Exception:                                  # noqa: BLE001
            pass
        return 0
    try:
        return run(a)
    except Usage:
        print(__doc__.strip().split("\n\n")[0])
        return 2
    except PaneError as exc:
        print("✗ %s" % exc)
        return 1
    except OSError as exc:
        print("✗ could not write the list (%s); the old list is unchanged" % exc.__class__.__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
