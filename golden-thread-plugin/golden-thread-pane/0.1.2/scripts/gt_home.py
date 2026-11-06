#!/usr/bin/env python3
"""Golden Thread home: the pane's buttons as a vault note and as a terminal menu.

  gt_home.py note [--vault V]     write `GT Home.md` into the vault, only when it is absent
  gt_home.py menu [--list]        numbered menu; the number you type runs `claude "<prompt>"`
  gt_home.py panel [--vault V]    copy the GT Panel plugin into <vault>/.obsidian/plugins/gt-panel
  gt_home.py refresh [--vault V]  rewrite the panel's status.json (allowance line, warnings) and profile.json
                                  (the model profile for the Claudian sync) in that folder

One menu source: the user's pane list (~/.claude/golden-thread/pane/buttons.json), else the
persona defaults gt_pane.py seeds from. There is no second menu file. The note holds every
prompt in a copyable block, so a user in Obsidian can paste it into any chat. The menu hands
the prompt to `claude` as one argument, never through a shell, so a prompt may hold any
character. `note` never overwrites: a note you have edited stays yours.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import gt_pane  # noqa: E402

NOTE = "GT Home.md"
PANEL_ID = "gt-panel"
PANEL_FILES = ("manifest.json", "main.js", "styles.css")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def menu_buttons():
    try:
        return gt_pane.read_buttons(gt_pane.list_path())
    except gt_pane.PaneError:
        return gt_pane.default_buttons()


def find_vault(explicit):
    v = explicit or os.environ.get("GT_VAULT")
    if not v:
        try:
            with open(os.path.join(gt_pane.home(), ".claude", "vault-config.json"),
                      encoding="utf-8") as fh:
                v = json.load(fh).get("vault_path")
        except (OSError, ValueError, AttributeError):
            v = None
    return os.path.abspath(os.path.expanduser(v)) if v else None


def note_text(buttons):
    out = ["# GT Home", "",
           "Your Golden Thread buttons, as words you can copy into any chat. This note is yours: "
           "gt writes it once and never overwrites it. Change the buttons themselves by asking "
           "Claude (\"move Overdue to the top\"), or in a terminal run `gt-home`.", ""]
    for b in buttons:
        fence = "````" if "```" in b["prompt"] else "```"
        out += ["## " + b["label"], "", fence, b["prompt"], fence, ""]
    return "\n".join(out)


def cmd_note(vault):
    vault = find_vault(vault)
    if not vault or not os.path.isdir(vault):
        print("✗ no vault found; pass --vault or set GT_VAULT")
        return 1
    path = os.path.join(vault, NOTE)
    if os.path.lexists(path):
        print("%s already exists; left as it is" % path)
        return 0
    text = note_text(menu_buttons())
    with open(path, "x", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print("wrote %s" % path)
    return 0


def panel_source():
    """The shipped plugin folder: GT_PANEL_SRC, beside this script's module, or the plugin cache."""
    cands = [os.environ.get("GT_PANEL_SRC"),
             os.path.join(HERE, "..", "templates", "obsidian-gt-panel")]
    cands += sorted(glob.glob(os.path.join(gt_pane.home(), ".claude", "plugins", "cache", "*",
                                           "gt-pane", "*", "templates", "obsidian-gt-panel")),
                    reverse=True)
    for c in cands:
        if c and all(os.path.isfile(os.path.join(c, f)) for f in PANEL_FILES):
            return os.path.abspath(c)
    return None


def _write_json(path, data):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
        fh.write("\n")


# ---- phase 89f: the allowance line's data ---------------------------------------------------
# The plugin reads only its own folder, so the saved reading and the two allowance settings are
# copied into status.json here; the plugin works out the age when it draws, so the line never
# claims a reading is fresher than it is. Defaults repeat the registry's (gt_turn_notes.py pins
# them in its own test).
READING_KEYS = ("ts", "five_hour", "seven_day", "five_hour_resets", "seven_day_resets")
TAIL_BYTES = 128 * 1024
DEFAULT_THRESHOLD = 90


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _usable(row):
    return isinstance(row, dict) and _num(row.get("ts")) and (
        _num(row.get("five_hour")) or _num(row.get("seven_day")))


def newest_reading():
    """The usage module's newest saved reading (latest.json, else the ledger's tail), reduced to
    the numeric fields the line needs, or None."""
    d = os.path.join(gt_pane.home(), ".claude", "golden-thread", "usage")
    row = gt_pane._json(os.path.join(d, "latest.json"))
    if not _usable(row):
        row = None
        try:
            with open(os.path.join(d, "readings.jsonl"), "rb") as fh:
                try:
                    fh.seek(-TAIL_BYTES, os.SEEK_END)
                except OSError:
                    fh.seek(0)
                tail = fh.read().decode("utf-8", "replace")
        except OSError:
            tail = ""
        for text in reversed(tail.splitlines()):
            try:
                cand = json.loads(text)
            except ValueError:
                continue
            if _usable(cand):
                row = cand
                break
    if row is None:
        return None
    return {k: row[k] for k in READING_KEYS if _num(row.get(k))}


def status_data():
    cfg = gt_pane._json(os.path.join(gt_pane.home(), ".claude", "vault-config.json"))
    cfg = cfg if isinstance(cfg, dict) else {}
    on = str(cfg.get("allowance_note") or "on").strip().lower() != "off"
    try:
        thr = float(str(cfg.get("allowance_note_threshold") or DEFAULT_THRESHOLD).strip())
    except ValueError:
        thr = float(DEFAULT_THRESHOLD)
    thr = int(thr) if thr == int(thr) else thr
    base = {"version": 1, "written": int(time.time()), "warnings": gt_warnings()}
    if not on:
        base["enabled"] = False                      # no figures leave the usage module when off
        return base
    base.update({"enabled": True, "threshold": thr, "reading": newest_reading()})
    return base


def gt_warnings():
    """phase 89g: gt's session warnings for the panel's status strip. Today the one from gt_bypass
    (permission prompts are off). Empty when the core scripts or the check are not found; the
    strip then says nothing rather than guess."""
    scripts = core_scripts()
    if not scripts:
        return []
    try:
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import gt_bypass
        text = gt_bypass.line(home=gt_pane.home())
    except Exception:                                    # noqa: BLE001 - no warning beats a wrong one
        return []
    return [CONTROL.sub(" ", text).strip()] if text else []


# ---- phase 89h: gt's model profile, as data for the plugin's Claudian sync -------------------
def core_scripts():
    """The core gt scripts folder: GT_CORE_SCRIPTS, beside this module in a checkout, or the
    plugin cache's newest copy. None when none is found."""
    if "GT_CORE_SCRIPTS" in os.environ:                  # explicit means only that folder
        cands = [os.environ["GT_CORE_SCRIPTS"]]
    else:
        cands = []
    if not cands:
        cands += sorted(glob.glob(os.path.join(HERE, "..", "..", "..", "golden-thread", "*",
                                               "scripts")), reverse=True)
        cands += sorted(glob.glob(os.path.join(gt_pane.home(), ".claude", "plugins", "cache", "*",
                                               "gt", "*", "scripts")), reverse=True)
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "gt_model_policy.py")):
            return os.path.abspath(c)
    return None


def profile_data():
    """What gt's model profile means for the main chat model: the recorded profile and the model
    and effort it implies (very-high: opus xhigh; average: the balanced intent). inherit, no
    recorded profile, or no readable gt means no model: the sync then refuses."""
    out = {"version": 1, "profile": None, "model": None, "effort": None}
    scripts = core_scripts()
    if not scripts:
        return out
    try:
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import gt_model_policy
        P = gt_model_policy.paths(gt_pane.home())
        profile = gt_model_policy.model_choices(P)["profile"]
        if profile not in gt_model_policy.PROFILES:
            return out
        out["profile"] = profile
        if profile == "very-high":
            out["model"], out["effort"] = gt_model_policy.VERY_HIGH
        elif profile == "average":
            out["model"], out["effort"], _src = gt_model_policy.intent_settings("balanced")
    except Exception:                                    # noqa: BLE001 - no model beats a wrong one
        return {"version": 1, "profile": None, "model": None, "effort": None}
    return out


def write_state(dest):
    _write_json(os.path.join(dest, "status.json"), status_data())
    _write_json(os.path.join(dest, "profile.json"), profile_data())


def cmd_refresh(vault):
    """Rewrite status.json and profile.json in the installed plugin folder (the saved reading
    moves on, and so can a setting or the profile). Touches nothing else."""
    vault = find_vault(vault)
    dest = os.path.join(vault, ".obsidian", "plugins", PANEL_ID) if vault else None
    if not dest or not os.path.isdir(dest):
        print("✗ the GT Panel plugin is not installed in that vault; run: gt_home.py panel")
        return 1
    write_state(dest)
    print("refreshed status.json and profile.json in %s" % dest)
    return 0


# ---- the SessionStart hook's step: keep status.json current -----------------------------------
REFRESH_EVERY = 600                                      # seconds between automatic refreshes


def stamp_path():
    return os.path.join(gt_pane.home(), ".claude", "golden-thread", "pane", "panel-refresh.stamp")


def _spawn(argv):
    """Run `argv` detached, silent, never waited for (the hook must not slow a session)."""
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200    # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(argv, **kw)


def auto_refresh(now=None, runner=None):
    """SessionStart: start `refresh` for the configured vault when the GT Panel is installed there,
    allowance_note is not off, and none started in the last REFRESH_EVERY seconds. Fail-open and
    silent; only the vault in vault-config.json is ever written (not GT_VAULT)."""
    try:
        cfg = gt_pane._json(os.path.join(gt_pane.home(), ".claude", "vault-config.json"))
        if not isinstance(cfg, dict) or not cfg.get("vault_path"):
            return None
        if str(cfg.get("allowance_note") or "on").strip().lower() == "off":
            return None
        vault = os.path.abspath(os.path.expanduser(str(cfg["vault_path"])))
        if not os.path.isdir(os.path.join(vault, ".obsidian", "plugins", PANEL_ID)):
            return None
        now = time.time() if now is None else now
        stamp = stamp_path()
        try:
            with open(stamp, encoding="utf-8") as fh:
                last = float(fh.read().strip())
        except (OSError, ValueError):
            last = None
        if last is not None and 0 <= now - last < REFRESH_EVERY:
            return None
        os.makedirs(os.path.dirname(stamp), exist_ok=True)
        with open(stamp, "w", encoding="utf-8") as fh:   # stamp first: a slow worker is not doubled
            fh.write("%d\n" % now)
        (runner or _spawn)([sys.executable, os.path.join(HERE, "gt_home.py"),
                            "refresh", "--vault", vault])
    except Exception:                                    # noqa: BLE001 - never blocks a session
        pass
    return None


def cmd_panel(vault):
    """Install or refresh the GT Panel plugin files. Only ever run on the owner's command.

    .obsidian is app configuration, not vault content, so this is not a vault write. It writes
    only inside plugins/gt-panel, never enables the plugin (that stays your click in Obsidian's
    Community plugins), and replaces only the files it ships plus menu.json."""
    vault = find_vault(vault)
    if not vault or not os.path.isdir(os.path.join(vault, ".obsidian")):
        print("✗ no Obsidian vault found (no .obsidian folder); pass --vault or set GT_VAULT")
        return 1
    src = panel_source()
    if not src:
        print("✗ the GT Panel plugin files are not installed with this module")
        return 1
    kept = [b for b in menu_buttons()
            if not CONTROL.search(b["prompt"]) and not CONTROL.search(b["label"])]
    dest = os.path.join(vault, ".obsidian", "plugins", PANEL_ID)
    os.makedirs(dest, exist_ok=True)
    for f in PANEL_FILES:
        shutil.copyfile(os.path.join(src, f), os.path.join(dest, f))
    _write_json(os.path.join(dest, "menu.json"), {"version": 1, "buttons": kept})
    write_state(dest)
    print("wrote %s with %d buttons" % (dest, len(kept)))
    print("next: Obsidian > Settings > Community plugins > turn on GT Panel "
          "(turn off Restricted mode first if it is on)")
    return 0


def cmd_menu(list_only):
    buttons = menu_buttons()
    for i, b in enumerate(buttons, 1):
        print("%d) %s" % (i, b["label"]))
    if list_only:
        return 0
    try:
        raw = input("Number: ").strip()
    except EOFError:
        raw = ""
    if not raw.isdigit() or not 1 <= int(raw) <= len(buttons):
        print("✗ pick a number from 1 to %d" % len(buttons))
        return 1
    claude = os.environ.get("GT_HOME_CLAUDE") or "claude"
    try:
        return subprocess.call([claude, buttons[int(raw) - 1]["prompt"]])
    except OSError as exc:
        print("✗ could not start %s (%s)" % (claude, exc.__class__.__name__))
        return 1


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    try:
        if a[:1] == ["note"] and (len(a) == 1 or (len(a) == 3 and a[1] == "--vault")):
            return cmd_note(a[2] if len(a) == 3 else None)
        if a[:1] == ["panel"] and (len(a) == 1 or (len(a) == 3 and a[1] == "--vault")):
            return cmd_panel(a[2] if len(a) == 3 else None)
        if a[:1] == ["refresh"] and (len(a) == 1 or (len(a) == 3 and a[1] == "--vault")):
            return cmd_refresh(a[2] if len(a) == 3 else None)
        if a[:1] == ["menu"] and a[1:] in ([], ["--list"]):
            return cmd_menu(a[1:] == ["--list"])
    except gt_pane.PaneError as exc:
        print("✗ %s" % exc)
        return 1
    print(__doc__.strip().split("\n\n")[0])
    return 2


if __name__ == "__main__":
    sys.exit(main())
