"""Templates suggested for the user's gt persona(s) (0.5.0, gt ADR-18).

gt (the Golden Thread plugin) lets a user name one or more personas -- `persona` in
~/.claude/vault-config.json, a comma list or a JSON array -- and each persona pack
(packs/personas/<name>.json) may list `lotr_templates`: catalog templates worth suggesting to that
role. LOTR reads both FILES directly and never imports gt, so it works the same with gt absent,
older, or half-installed: anything missing or malformed simply means no suggestion.

A suggestion is a line of text and nothing else: it installs, connects and enables nothing, and it
never changes what LOTR allows. With no persona the output of every command is unchanged.
"""
import json
import os
import re
from pathlib import Path

from .util import wrap_list

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
_MAX_PACK = 64 * 1024
LABEL = "suggested for your persona(s): "


def _user_home():
    return Path(os.path.expanduser("~"))


def user_personas(user_home=None):
    """-> the persona names gt recorded for this user, in order; [] when none or unreadable."""
    try:
        cfg = json.loads((Path(user_home or _user_home()) / ".claude" / "vault-config.json")
                         .read_text(encoding="utf-8"))
        value = cfg.get("persona") if isinstance(cfg, dict) else None
    except (OSError, ValueError, UnicodeDecodeError):
        return []
    if isinstance(value, str):
        value = value.replace("[", "").replace("]", "").split(",")
    out = []
    for v in value if isinstance(value, list) else []:
        v = str(v).strip().strip("'\"").lower()
        if v and v != "none" and _NAME.match(v) and v not in out:
            out.append(v)
    return out


def _version_key(name):
    try:
        return tuple(int(x) for x in name.split("."))
    except ValueError:
        return (-1,)


def pack_dirs(user_home=None):
    """Where gt keeps its persona packs: beside its installed hooks, else the newest gt release in
    Claude Code's plugin cache. Only folders that exist."""
    h = Path(user_home or _user_home()) / ".claude"
    out = [h / "golden-thread" / "packs" / "personas"]
    cache = h / "plugins" / "cache" / "golden-thread-plugin" / "gt"
    try:
        vers = sorted((d.name for d in cache.iterdir() if d.is_dir()), key=_version_key, reverse=True)
    except OSError:
        vers = []
    out += [cache / v / "packs" / "personas" for v in vers[:1]]
    return [d for d in out if d.is_dir()]


def _pack_templates(name, dirs):
    for d in dirs:
        f = d / (name + ".json")
        try:
            if f.stat().st_size > _MAX_PACK:
                continue
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict) or data.get("name") != name:
            continue
        lt = data.get("lotr_templates")
        return [t for t in lt if isinstance(t, str) and _NAME.match(t)] if isinstance(lt, list) else []
    return []


def suggested(user_home=None):
    """-> (personas, template names): the union of the personas' lotr_templates, first listed first."""
    names = user_personas(user_home)
    if not names:
        return [], []
    dirs = pack_dirs(user_home)
    out = []
    for n in names:
        for t in _pack_templates(n, dirs):
            if t not in out:
                out.append(t)
    return names, out


def suggest_lines(available, user_home=None, width=76):
    """The plain-words lines for `lotr setup` / `lotr template list`, each at most `width` columns;
    [] with no persona or nothing suggested. `available` is the template names here now: a
    suggestion not yet installed is named with the command that fetches it."""
    _, sug = suggested(user_home)
    if not sug:
        return []
    have = set(available or ())
    lines = wrap_list(LABEL, sug, width)
    missing = [t for t in sug if t not in have]
    if missing and len(missing) == len(sug):
        lines.append("none of them is here yet; refresh the catalog: lotr template update")
    elif missing:
        lines += wrap_list("not here yet (lotr template update): ", missing, width)
    return lines


def suggestion(available, user_home=None):
    """-> the JSON form for `lotr template list`, or None with no persona or nothing suggested."""
    personas, sug = suggested(user_home)
    if not sug:
        return None
    have = set(available or ())
    return {"personas": personas, "templates": sug,
            "not_installed": [t for t in sug if t not in have],
            "say": suggest_lines(available, user_home)}
