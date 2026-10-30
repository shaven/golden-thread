"""The `agent_split` setting (gt 0.21.0): read/write agents separate, or one combined agent.

`split` (default) is the 0.4.0 design: readers hold find + call_read, writers hold find +
call_write (+ call_consent), so text inside a read result can never reach a tool that acts.
`combined` ships one agent, gt-lotr:gateway, that holds all four tools: fewer hops, cheaper, and
the split's protection gone. So `combined` takes effect ONLY at the loosest lockdown level
(`insecure`); anywhere else the effective mode is `split` and the reason is stated. Irreversible
operations (send mail, merge, delete) go through call_consent in both modes.

The values live in ~/.claude/vault-config.json (gt settings: `agent_split`, `lockdown`). This
module reads that file directly, stdlib only, and fails closed to `split`: a missing, unreadable
or corrupt file, an unknown value, or no recorded lockdown level (the default is strict).
"""
import json
import os

MODES = ("split", "combined")
DEFAULT = "split"
LEVELS = ("strict", "trust-local", "trust-network", "insecure")
DEFAULT_LEVEL = "strict"
COMBINED_LEVELS = ("insecure",)       # the request's "relaxed": the loosest preset


def config_path():
    return os.path.join(os.path.expanduser("~"), ".claude", "vault-config.json")


def read_config(path=None):
    try:
        with open(path or config_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _word(cfg, key, allowed, default):
    v = cfg.get(key)
    v = v.strip().lower() if isinstance(v, str) else ""
    return v if v in allowed else default


def refusal(mode, level):
    """-> None when `mode` may be in effect at lockdown `level`, else the reason it may not."""
    if mode != "combined" or level in COMBINED_LEVELS:
        return None
    return ("agent_split combined removes the read/write split that stops text in a read result "
            "from steering a write, so it needs lockdown %s; the level is %s"
            % (" or ".join(COMBINED_LEVELS), level))


def effective(cfg=None):
    """-> (mode, reason). `reason` is "" unless a requested combined mode was refused."""
    cfg = read_config() if cfg is None else cfg
    mode = _word(cfg, "agent_split", MODES, DEFAULT)
    level = _word(cfg, "lockdown", LEVELS, DEFAULT_LEVEL)
    why = refusal(mode, level)
    if why:
        return DEFAULT, why
    return mode, ""
