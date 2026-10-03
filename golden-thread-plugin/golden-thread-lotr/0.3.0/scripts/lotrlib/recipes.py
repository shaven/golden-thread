"""Recipes: named, parameterised sequences of curated ops, one variant per profile.

A recipe file is JSON:

    {"name": "jira.my_open_issues", "summary": "...", "tier": "read",
     "params": {"project": {"description": "...", "default": null}},
     "profiles": {"jira-v3": {"steps": [{"op": "search", "args": {...}}]}},
     "select": "issues[].key,..."}

String args are templates: `{{name}}` is replaced by the arg, and `{{#name}}...{{/name}}` is
kept only when the arg is present and not null/empty. A param with no "default" key is
required. A string that is exactly one `{{name}}` takes the arg's value with its type.
"""
import json
import re
from pathlib import Path

from . import profiles as profiles_mod
from .errors import GatewayError

TIERS = ("read", "write", "consent")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_SECTION = re.compile(r"\{\{#([A-Za-z_][A-Za-z0-9_]*)\}\}(.*?)\{\{/\1\}\}", re.S)
_VAR = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
_WHOLE = re.compile(r"^\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}$")


def _invalid(src, msg):
    raise GatewayError("recipe_invalid", f"{src}: {msg}")


def validate(recipe, src="<recipe>"):
    if not isinstance(recipe, dict):
        _invalid(src, "a recipe must be a JSON object")
    name = recipe.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        _invalid(src, "'name' must be a lowercase string like 'jira.my_open_issues'")
    if not isinstance(recipe.get("summary"), str) or not recipe["summary"].strip():
        _invalid(src, "'summary' must be a non-empty string")
    if recipe.get("tier") not in TIERS:
        _invalid(src, f"'tier' must be one of {', '.join(TIERS)}")
    params = recipe.get("params", {})
    if not isinstance(params, dict):
        _invalid(src, "'params' must be an object")
    for p, spec in params.items():
        if not _VAR.fullmatch("{{%s}}" % p):
            _invalid(src, f"param name {p!r} must be an identifier")
        if not isinstance(spec, dict):
            _invalid(src, f"param {p!r} must be an object with a description")
    if "select" in recipe and recipe["select"] is not None and not isinstance(recipe["select"], str):
        _invalid(src, "'select' must be a string")
    profs = recipe.get("profiles")
    if not isinstance(profs, dict) or not profs:
        _invalid(src, "'profiles' must be a non-empty object of profile -> {steps}")
    for pname, variant in profs.items():
        if pname not in profiles_mod.PROFILES:
            _invalid(src, f"unknown profile {pname!r}")
        steps = variant.get("steps") if isinstance(variant, dict) else None
        if not isinstance(steps, list) or not steps:
            _invalid(src, f"profile {pname!r} needs a non-empty 'steps' list")
        for i, s in enumerate(steps):
            if not isinstance(s, dict) or not isinstance(s.get("op"), str):
                _invalid(src, f"profile {pname!r} step {i} needs an 'op' string")
            if not isinstance(s.get("args", {}), dict):
                _invalid(src, f"profile {pname!r} step {i} 'args' must be an object")
            try:
                profiles_mod.resolve_op(profiles_mod.PROFILES[pname], s["op"])
            except GatewayError as e:
                _invalid(src, f"profile {pname!r} step {i}: {e.message}")
            for var in _template_vars(s.get("args", {})):
                if var not in params:
                    _invalid(src, f"profile {pname!r} step {i} uses {{{{{var}}}}} "
                                  "which is not declared in 'params'")
    return recipe


def _template_vars(obj):
    if isinstance(obj, str):
        return set(_VAR.findall(obj)) | {m.group(1) for m in _SECTION.finditer(obj)}
    if isinstance(obj, dict):
        return set().union(*(_template_vars(v) for v in obj.values())) if obj else set()
    if isinstance(obj, list):
        return set().union(*(_template_vars(v) for v in obj)) if obj else set()
    return set()


def load_recipes(dirs):
    """Every *.json recipe under `dirs`; a later dir's recipe replaces an earlier one's name."""
    by_name = {}
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (ValueError, UnicodeDecodeError) as e:
                raise GatewayError("recipe_invalid", f"{f}: not valid JSON: {e}")
            validate(data, str(f))
            data = dict(data, source=str(f))
            by_name[data["name"]] = data
    return [by_name[n] for n in sorted(by_name)]


def applicable(recipe, conn):
    return conn.get("profile") in (recipe.get("profiles") or {})


def _empty(v):
    return v is None or v is False or (isinstance(v, (str, list, dict)) and len(v) == 0)


def _fill(obj, values):
    if isinstance(obj, str):
        whole = _WHOLE.match(obj)
        if whole:
            return values.get(whole.group(1))
        s = _SECTION.sub(lambda m: "" if _empty(values.get(m.group(1))) else m.group(2), obj)
        return _VAR.sub(lambda m: "" if values.get(m.group(1)) is None else str(values[m.group(1)]), s)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            fv = _fill(v, values)
            if fv is not None:      # an unset optional arg is left out, not sent as null
                out[k] = fv
        return out
    if isinstance(obj, list):
        return [_fill(v, values) for v in obj]
    return obj


def expand(recipe, conn, args):
    """The recipe's steps for `conn`'s profile, with every template filled from `args`."""
    if not applicable(recipe, conn):
        raise GatewayError("recipe_not_applicable",
                           f"recipe {recipe.get('name')} has no variant for profile {conn.get('profile')}",
                           [f"it supports: {', '.join(sorted(recipe.get('profiles') or {}))}"])
    args = dict(args or {})
    values = {}
    missing = []
    for p, spec in (recipe.get("params") or {}).items():
        if p in args and args[p] is not None:
            values[p] = args[p]
        elif "default" in spec:
            values[p] = spec["default"]
        else:
            missing.append(p)
    if missing:
        raise GatewayError("missing_param",
                           f"recipe {recipe['name']} needs: {', '.join(missing)}",
                           [f"{p}: {(recipe['params'][p] or {}).get('description', '')}".rstrip(": ")
                            for p in missing])
    steps = recipe["profiles"][conn["profile"]]["steps"]
    return [{"op": s["op"], "args": _fill(s.get("args", {}), values)} for s in steps]
