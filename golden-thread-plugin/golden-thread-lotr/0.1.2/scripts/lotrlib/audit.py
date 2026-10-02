"""Append-only call log: who called what, as whom, at which tier, with what outcome.

The log exists to answer "what did the gateway do" after the fact, so it must be safe to
keep forever and to show anyone. That rules out arguments: a call's args can hold a
message body, a JQL string naming a customer, or a token someone pasted. The log records
`args_sha256` (enough to match a call to a replay) and refuses the raw fields outright --
a caller passing `args` gets it hashed and dropped, never written.
"""
import hashlib
import json
import os
from pathlib import Path

from .util import now_iso

FIELDS = ("ts", "client", "connection", "identity", "op", "tier", "tool", "verdict",
          "status", "args_sha256")
# Keys that carry request or response content. Never written, whatever the caller passes.
FORBIDDEN = frozenset({"args", "body", "query", "params", "data", "result", "headers",
                       "secret", "token", "authorization", "password", "credential"})
_SCALAR = (str, int, float, bool, type(None))


def args_sha256(args):
    """Stable digest of call args: sorted-key compact JSON, sha256 hex."""
    blob = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _line(fields):
    raw_args = fields.pop("args", None)
    rec = {"ts": fields.pop("ts", None) or now_iso()}
    if fields.get("args_sha256") is None and raw_args is not None:
        fields["args_sha256"] = args_sha256(raw_args)
    for key in FIELDS[1:]:
        value = fields.pop(key, None)
        if key == "op" and isinstance(value, dict):
            # A resolved op dict: keep only its identity, never anything filled from args.
            value = value.get("name") or " ".join(
                str(value.get(k) or "") for k in ("method", "path")).strip() or None
        elif not isinstance(value, _SCALAR):
            value = None   # a nested value in a pinned field is not trusted to be args-free
        rec[key] = value
    # Extra fields (e.g. an error code, a duration) are kept only as scalars under
    # non-content names: a nested object is exactly where raw args would hide.
    for key, value in fields.items():
        if key.lower() in FORBIDDEN or not isinstance(value, _SCALAR):
            continue
        rec[key] = value
    return json.dumps(rec, sort_keys=False, separators=(",", ":")) + "\n"


def record(home, **fields):
    """Append one JSON line to <home>/state/audit.jsonl, created mode 600 (dir 700)."""
    state = Path(home) / "state"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = state / "audit.jsonl"
    line = _line(dict(fields))
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        # A pre-existing file with wider permissions is tightened, not trusted.
        if os.fstat(fd).st_mode & 0o077:
            os.fchmod(fd, 0o600)
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
