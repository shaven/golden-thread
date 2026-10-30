"""Append-only call log: who called what, as whom, at which tier, with what outcome.

The log exists to answer "what did the gateway do" after the fact, so it must be safe to
keep forever and to show anyone. That rules out arguments: a call's args can hold a
message body, a JQL string naming a customer, or a token someone pasted. The log records
`args_sha256` (enough to match a call to a replay) and refuses the raw fields outright --
a caller passing `args` gets it hashed and dropped, never written.

0.3.0: every line carries `grant` -- the gt unlock grant id the call ran under (null when unlock
is off, the scope was open, or the call was refused before a grant was found), so the
authority's audit and this one can be joined line for line. A grant id is an audit handle,
not a credential (gt_unlockd I3).

0.5.0 (gt 0.21.4): three more scalar fields, all optional. `ref` is twelve hex digits of the hash
of an op reference a call was made by (the reference itself is never written). `detail` is on an
error line: the full text of the failure, message and hints on one line, at most 400 characters,
withheld entirely if it holds credential-shaped text. The model never sees it (lotrlib.redact shows
it the gateway's words only), so this is where a downstream's own message and address live; it can
echo a downstream's words but is still no place for arguments. A `describe`, and a `find` that hid
or dropped entries, write a line too (tool "describe" / "find"; the query is hashed like args).

On native Windows there are no mode bits: the log lives under the gateway home in the user's
profile, whose ACL (owner, SYSTEM, Administrators) is what keeps other users out.
"""
import hashlib
import json
import os
import threading
from pathlib import Path

from .util import now_iso

FIELDS = ("ts", "client", "connection", "identity", "op", "tier", "tool", "verdict",
          "status", "args_sha256", "grant")
# Keys that carry request or response content. Never written, whatever the caller passes.
FORBIDDEN = frozenset({"args", "body", "query", "params", "data", "result", "headers",
                       "secret", "token", "authorization", "password", "credential"})
_SCALAR = (str, int, float, bool, type(None))
# One append at a time in this process. POSIX O_APPEND is atomic per write; the Windows C runtime
# emulates it (seek to the end, then write), so two threads appending at once could land on the
# same offset and one line was lost (40 calls, 39 lines on Windows, gt 0.22.0).
_APPEND = threading.Lock()
_BINARY = getattr(os, "O_BINARY", 0)          # Windows: no "\n" -> "\r\n" text-mode rewrite


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


def writable(home):
    """True when an audit line could be appended now (opened for append, nothing written).
    The engine refuses a write / consent op up front when it is False (review, low)."""
    try:
        state = Path(home) / "state"
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(state / "audit.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.close(fd)
        return True
    except OSError:
        return False


def record(home, **fields):
    """Append one JSON line to <home>/state/audit.jsonl, created mode 600 (dir 700)."""
    state = Path(home) / "state"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)   # mode ignored on Windows
    path = state / "audit.jsonl"
    line = _line(dict(fields))
    with _APPEND:
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT | _BINARY, 0o600)
        try:
            # A pre-existing file with wider permissions is tightened, not trusted.
            if os.name != "nt" and os.fstat(fd).st_mode & 0o077:
                os.fchmod(fd, 0o600)
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
