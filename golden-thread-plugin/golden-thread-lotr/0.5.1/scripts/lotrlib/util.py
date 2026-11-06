"""Small shared helpers: atomic writes, timestamps, JSON reads, the gateway home."""
import datetime
import json
import os
import tempfile
from pathlib import Path

from .errors import GatewayError


def has_terminal(stream):
    """True when `stream` is an interactive terminal. On Windows isatty() is also True for NUL,
    so a real console (GetConsoleMode succeeds) is required there (0.5.0)."""
    if stream is None:
        return False
    try:
        if not stream.isatty():
            return False
        if os.name != "nt":
            return True
        import ctypes
        import msvcrt
        from ctypes import wintypes
        mode = wintypes.DWORD()
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetConsoleMode.restype = wintypes.BOOL
        return bool(k32.GetConsoleMode(wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno())),
                                       ctypes.byref(mode)))
    except Exception:                                       # noqa: BLE001 -- unknown means no
        return False


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def atomic_write(path, text, mode=0o600):
    """Write `text` to `path` atomically (temp file in the same directory, then rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())                 # the bytes are on disk before the rename
            except OSError:
                pass
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read_json(path):
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise GatewayError("missing_file", f"{path} does not exist")
    except (ValueError, UnicodeDecodeError) as e:
        raise GatewayError("bad_json", f"{path} is not valid JSON: {e}")


def write_json(path, obj, mode=0o600):
    atomic_write(path, json.dumps(obj, indent=2, sort_keys=False) + "\n", mode)


def lotr_home(zone=None):
    env = os.environ.get("LOTR_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "gt-lotr" / (zone or "personal")


def wrap_list(prefix, names, width=76):
    """`prefix` (ending in a space) then `a, b, c`, as lines of at most `width` columns, later lines
    indented under the first name (0.5.0: the owner rule is that fixed-width text never wraps; the
    one-line list of every template ran to 143 columns). A name longer than a line gets its own."""
    pad = " " * len(prefix)
    lines, cur = [], prefix
    names = list(names)
    for i, n in enumerate(names):
        piece = n + ("," if i < len(names) - 1 else "")
        fresh = cur in (prefix, pad)
        if not fresh and len(cur) + 1 + len(piece) > width:
            lines.append(cur)
            cur, fresh = pad, True
        cur += ("" if fresh else " ") + piece
    lines.append(cur.rstrip() if names else prefix.rstrip())
    return lines
