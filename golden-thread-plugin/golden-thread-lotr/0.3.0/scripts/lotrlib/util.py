"""Small shared helpers: atomic writes, timestamps, JSON reads, the gateway home."""
import datetime
import json
import os
import tempfile
from pathlib import Path

from .errors import GatewayError


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
