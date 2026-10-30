#!/usr/bin/env python3
"""gt_voice.py -- the `voice_prompting` setting's apply and preflight (0.22.0, phase 83).

Claude Code has its own `/voice [hold|tap|off]`, which turns voice on and runs the microphone
check. What gt adds is what people get stuck on: a custom push-to-talk key (a second file,
keybindings.json), a merge that backs both files up and can be undone exactly, and a preflight that
says why voice does not work. gt_settings.py calls apply() when voice_prompting, voice_auto_submit
or voice_key changes, and check() for `gt_settings.py voice --check`.

    apply(mode, auto_submit, key)   mode: off | hold | tap     -> (exit code, lines)
    check(mode, key)                read-only preflight         -> lines

What apply() writes, and only this:
  ~/.claude/settings.json      voice.enabled true, voice.mode, voice.autoSubmit
  ~/.claude/keybindings.json   when key is not space: a Chat binding {<key>: voice:pushToTalk}
It never writes the older `voiceEnabled` key and never removes it. Before editing a file it copies
it to <name>.gt-bak-<time>; an unparseable file is refused and nothing is written. What it added
(and the value each key had before) is recorded in ~/.claude/golden-thread/voice-record.json, so
`off` removes only that and leaves anything the user changed since. The same call twice changes
nothing and makes no second backup. Files are rewritten as 2-space JSON; every other key keeps its
value and its order.

The microphone grant is never reported as verified: it cannot be known from a script.
"""
import copy
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ACTION = "voice:pushToTalk"
MODES = ("off", "hold", "tap")
# Claude Code reads the voice requirement from these; their values are never read or shown here.
API_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
           "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
FIELDS = ("enabled", "mode", "autoSubmit")
MISSING = {"missing": True}


class Refuse(Exception):
    pass


def claude_dir():
    return Path.home() / ".claude"


def settings_path():
    return claude_dir() / "settings.json"


def keybindings_path():
    return claude_dir() / "keybindings.json"


def record_path():
    return claude_dir() / "golden-thread" / "voice-record.json"


def _read(path):
    """-> (data, existed). A file that is not a JSON object is refused, never overwritten."""
    if not path.exists():
        return {}, False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise Refuse("%s is not valid JSON (%s); fix or move it, then try again. Nothing was changed."
                     % (path, e.__class__.__name__))
    if not isinstance(data, dict):
        raise Refuse("%s is not a JSON object. Nothing was changed." % path)
    return data, True


def _load_record():
    try:
        d = json.loads(record_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".gt-tmp")
    tmp.write_bytes((json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    os.replace(str(tmp), str(path))


def _backup(path):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest, n = path.with_name("%s.gt-bak-%s" % (path.name, stamp)), 1
    while dest.exists():
        n += 1
        dest = path.with_name("%s.gt-bak-%s-%d" % (path.name, stamp, n))
    shutil.copy2(str(path), str(dest))
    return dest.name


def _chat_block(kb, create=False):
    blocks = kb.get("bindings")
    if not isinstance(blocks, list):
        if not create:
            return None
        blocks = kb["bindings"] = []
    for b in blocks:
        if isinstance(b, dict) and b.get("context") == "Chat" and isinstance(b.get("bindings"), dict):
            return b
    if not create:
        return None
    b = {"context": "Chat", "bindings": {}}
    blocks.append(b)
    return b


# ---- undo what a previous apply recorded -----------------------------------------------------
def _revert(s, kb, rec):
    """Undo, in memory, what the record says gt wrote -- but only values still as gt wrote them."""
    voice = s.get("voice")
    if isinstance(voice, dict):
        for f, r in (rec.get("settings") or {}).items():
            if f in voice and voice[f] == r.get("wrote"):
                if r.get("had"):
                    voice[f] = r.get("prior")
                else:
                    del voice[f]
        if not voice and rec.get("created_voice"):
            del s["voice"]
    key = (rec.get("binding") or {}).get("key")
    if key:
        blk = _chat_block(kb)
        if blk and blk["bindings"].get(key) == ACTION:
            del blk["bindings"][key]
            if not blk["bindings"] and rec["binding"].get("created_block"):
                kb["bindings"].remove(blk)


def apply(mode, auto_submit, key):
    """-> (exit code, lines). 0 = done (or nothing to do); 2 = refused, nothing written."""
    if mode not in MODES:
        return 2, ["voice_prompting must be one of: %s" % ", ".join(MODES)]
    try:
        s0, s_existed = _read(settings_path())
        k0, k_existed = _read(keybindings_path())
        rec = _load_record()
        s, kb = copy.deepcopy(s0), copy.deepcopy(k0)
        _revert(s, kb, rec)
        new = {"settings": {}, "binding": None}
        if mode != "off":
            voice = s.get("voice")
            if voice is None:
                voice = s["voice"] = {}
                new["created_voice"] = True
            elif not isinstance(voice, dict):
                raise Refuse("settings.json has a `voice` entry that is not an object; "
                             "fix it by hand, then try again. Nothing was changed.")
            for f, want in zip(FIELDS, (True, mode, bool(auto_submit))):
                if voice.get(f) != want:
                    new["settings"][f] = {"wrote": want, "had": f in voice,
                                          "prior": voice.get(f)}
                    voice[f] = want
            if key != "space":
                blk0 = _chat_block(kb)
                blk = _chat_block(kb, create=True)
                held = blk["bindings"].get(key)
                if held not in (None, ACTION):
                    raise Refuse("the key %s is already bound to %s in your keybindings.json; "
                                 "pick another voice_key. Nothing was changed." % (key, held))
                if held is None:
                    blk["bindings"][key] = ACTION
                    new["binding"] = {"key": key, "created_block": blk0 is None}
        # The record is relative to the state AFTER the undo above, which is the user's own state
        # again, so it is the whole truth about what this apply added; nothing merges with the old.
        if new["binding"]:
            new["created_kb_file"] = bool(rec.get("created_kb_file")) or not k_existed
        lines = []
        for path, before, after, existed, label in (
                (settings_path(), s0, s, s_existed, "settings.json"),
                (keybindings_path(), k0, kb, k_existed, "keybindings.json")):
            if before == after or (not existed and not after):
                continue
            if label == "keybindings.json" and existed and after == {"bindings": []} \
                    and rec.get("created_kb_file") and not new["binding"]:
                path.unlink()
                lines.append("  removed keybindings.json (gt had created it)")
                continue
            note = " (backup: %s)" % _backup(path) if existed else ""
            _write(path, after)
            lines.append("  updated %s%s" % (label, note))
        if new["settings"] or new["binding"]:
            record_path().parent.mkdir(parents=True, exist_ok=True)
            _write(record_path(), new)
        elif record_path().exists():
            record_path().unlink()
        if not lines:
            lines.append("  nothing to change: %s already matches" % ("voice is off" if mode == "off"
                                                                     else "your files"))
        if mode != "off":
            lines += [""] + check(mode, key)
        return 0, lines
    except Refuse as e:
        return 2, ["refused: %s" % e]
    except OSError as e:
        return 1, ["refused: could not write the Claude Code settings (%s). Nothing else was changed." % e]


# ---- the preflight -------------------------------------------------------------------------------
def _audio_probe():
    """-> (state, text) for an audio input device. Skipped under GT_VOICE_NO_PROBE (tests)."""
    if os.environ.get("GT_VOICE_NO_PROBE") == "1":
        return "note", "audio input: not probed"
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["system_profiler", "SPAudioDataType"], capture_output=True,
                                 text=True, timeout=15).stdout
            return ("ok", "audio input: a device is listed") if "Input" in out \
                else ("WARN", "audio input: no input device listed")
        if sys.platform.startswith("linux"):
            out = subprocess.run(["arecord", "-l"], capture_output=True, text=True,
                                 timeout=10).stdout
            return ("ok", "audio input: a capture device is listed") if "card" in out \
                else ("WARN", "audio input: arecord lists no capture device")
    except (OSError, subprocess.SubprocessError):
        return "WARN", "audio input: could not be probed"
    return "note", "audio input: the check is platform-specific here; test it with /voice"


def check(mode, key):
    """-> the preflight lines. Read-only; never reads or prints a credential's value."""
    lines = ["Voice preflight (read-only)"]
    try:
        s, _ = _read(settings_path())
        kb, _ = _read(keybindings_path())
    except Refuse as e:
        return lines + ["  FAIL  %s" % e]
    found = shutil.which("claude")
    lines.append("  %-5s Claude Code: %s" % (("ok", "found on PATH") if found else
                                             ("WARN", "not found on PATH from here")))
    voice = s.get("voice") if isinstance(s.get("voice"), dict) else {}
    on = voice.get("enabled") is True
    lines.append("  %-5s settings.json voice: %s" % (("ok", "enabled, mode %s, autoSubmit %s"
                 % (voice.get("mode", "hold"), voice.get("autoSubmit", False))) if on
                 else ("WARN", "not enabled (voice_prompting is %s)" % mode)))
    if key == "space":
        lines.append("  ok    push-to-talk key: the default, Space")
    else:
        blk = _chat_block(kb)
        bound = bool(blk and blk["bindings"].get(key) == ACTION)
        lines.append("  %-5s push-to-talk key: %s" % (("ok", "%s is bound in keybindings.json" % key)
                     if bound else ("WARN", "%s is not bound in keybindings.json" % key)))
    for name in API_ENV:
        if os.environ.get(name):
            lines.append("  FAIL  %s is set: voice needs a claude.ai sign-in, and this takes "
                         "precedence over it (then /voice is an unknown command)" % name)
    if s.get("apiKeyHelper"):
        lines.append("  FAIL  apiKeyHelper in settings.json: voice needs a claude.ai sign-in, and "
                     "an API-key helper takes precedence over it")
    if not any(l.startswith("  FAIL") for l in lines[1:]):
        lines.append("  note  sign-in: no API-key route found; voice needs a claude.ai sign-in, "
                     "which cannot be checked from a script")
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        lines.append("  WARN  this looks like an SSH session: voice needs a local microphone")
    state, text = _audio_probe()
    lines.append("  %-5s %s" % (state, text))
    lines.append("  note  Microphone: cannot be verified from a script. Run /voice or hold %s and "
                 "click Allow." % key)
    app = os.environ.get("TERM_PROGRAM") or "your terminal"
    lines.append("Next: allow the microphone for %s when macOS asks, then hold %s to talk "
                 "(or type /voice to confirm in the session)." % (app, key))
    return lines


def main(argv):
    """gt_voice.py check [MODE] [KEY] | apply MODE AUTO KEY -- gt_settings.py is the usual caller."""
    if argv and argv[0] == "check":
        print("\n".join(check(argv[1] if len(argv) > 1 else "off", argv[2] if len(argv) > 2 else "space")))
        return 0
    if len(argv) == 4 and argv[0] == "apply":
        rc, lines = apply(argv[1], argv[2] == "on", argv[3])
        print("\n".join(lines))
        return rc
    print(main.__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
