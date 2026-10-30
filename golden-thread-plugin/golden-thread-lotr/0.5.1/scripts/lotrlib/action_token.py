"""gt-lotr as a CONSUMER of gt core's single-use action tokens (gt 0.21.0, item #32).

`paranoid.action_tokens.lotr_read | lotr_write | lotr_consent` (settings in ~/.claude/vault-config.json,
resolved here exactly as gt core resolves them: the key, else `paranoid.action_tokens`, else
`paranoid`, else off) make the gateway require a token for that tier of call. The token is minted by
the USER with presence (gt_action_token.py mint lotr_write github@personal/create_issue
--args-sha256 ...), bound to `connection/op` and a fingerprint of the args, and spent once, so a
writer agent that was talked into another op, other args or a second call is refused by the
gateway, not by its own judgement.

The check itself is gt core's (gt_action_token.check, loaded from the gt hooks directory the way
lotrlib.unlock loads the unlock client). LOTR never mints and never sees a signature. Off means the
0.4.0 behaviour exactly. On and gt core cannot be loaded is CLOSED: `token_unavailable`.
Applies to the local caller only (a hub client has its own bearer and revocation model).
"""
import json
import os

from . import unlock as unlock_mod
from .errors import GatewayError

ACTION_OF = {"call_read": "lotr_read", "call_write": "lotr_write", "call_consent": "lotr_consent"}
MASTER = "paranoid"
GROUP = "paranoid.action_tokens"


def _config(path=None):
    try:
        with open(path or os.path.join(os.path.expanduser("~"), ".claude", "vault-config.json"),
                  encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def required(tool, cfg=None):
    action = ACTION_OF.get(tool)
    if action is None:
        return False
    cfg = _config() if cfg is None else cfg
    name = GROUP + "." + action
    while name:
        v = cfg.get(name)
        if v in ("on", "off"):
            return v == "on"
        if name == MASTER:
            return False
        name = name.rpartition(".")[0] or MASTER
    return False


def check(tool, connection, op, args_sha256, token):
    """Raise GatewayError unless `tool` needs no token or `token` is a valid, unspent one for
    exactly this connection/op/args. Spends it."""
    if not required(tool):
        return
    mod = unlock_mod._load("gt_action_token")
    if mod is None:
        raise GatewayError("token_unavailable",
                           "a single-use token is required for %s, but gt core's token check "
                           "cannot be loaded from %s; refusing rather than serving ungated"
                           % (tool, unlock_mod.hooks_dir()),
                           hints=["reinstall gt (./install.sh) so gt_action_token.py is in its "
                                  "hooks directory"])
    try:
        mod.check(ACTION_OF[tool], token, "%s/%s" % (connection, op), args_sha256)
    except mod.TokenError as e:
        raise GatewayError(e.code, e.message, hints=list(e.hints))
    except Exception as e:                               # noqa: BLE001 - never a pass
        raise GatewayError("token_unavailable", "the token check failed (%s); refusing"
                           % type(e).__name__)
