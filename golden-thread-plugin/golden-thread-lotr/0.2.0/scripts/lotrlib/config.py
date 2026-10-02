"""gateway.json: where this gateway runs and how its front ends reach it (ADR-4).

Placement is a per-zone setting, not a fixed shape:
  local   the daemon runs on this machine (a one-box work machine)
  hub     the daemon runs here AND serves enrolled client machines over HTTP
  client  no daemon here; the CLI and MCP shim talk to a hub
  hybrid  a local edge in front of a hub -- parsed, refused in 0.1.0

Defaults are merged under whatever the file says, so an old gateway.json keeps working when a
release adds a key.
"""
import copy
from pathlib import Path

from .errors import GatewayError
from .util import read_json, write_json

MODES = ("local", "hub", "client", "hybrid")
IMPLEMENTED_MODES = ("local", "hub", "client")

DEFAULTS = {
    "schema": 1,
    "zone": "personal",
    "mode": "local",
    "socket": None,
    "listen": {"host": "127.0.0.1", "port": 8765, "tls_cert": None, "tls_key": None},
    "hub": {"url": None, "client_id": None, "credential_ref": None, "timeout_s": 8},
    "local_connections": [],
    # Callers on this machine (the socket, the MCP shim, the CLI) are not exempt from policy:
    # they get an allow list and a tier ceiling like any enrolled client, and consent-tier
    # operations need the owner's confirmation raised by the daemon itself (ADR-5).
    "local": {"allow": ["*"], "max_tier": "consent", "confirm": "auto"},
    "offline": "fail-closed",
    "limits": {"max_result_chars": 24000, "default_page": 20},
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def check_private(home):
    """The admin plane is files; they must be private, or the gateway refuses to run (ADR-5)."""
    home = Path(home)
    problems = []
    st = home.stat()
    if st.st_mode & 0o077:
        problems.append(f"{home} is mode {oct(st.st_mode & 0o777)} (want 700)")
    for name in ("gateway.json", "registry.json"):
        p = home / name
        if p.exists() and p.stat().st_mode & 0o077:
            problems.append(f"{p} is mode {oct(p.stat().st_mode & 0o777)} (want 600)")
    if problems:
        raise GatewayError("insecure_perms", "; ".join(problems),
                           hints=[f"chmod 700 {home} && chmod 600 {home}/gateway.json {home}/registry.json"])


def load_settings(home):
    home = Path(home)
    check_private(home)
    settings = _merge(DEFAULTS, read_json(home / "gateway.json"))
    validate(settings)
    if not settings.get("socket"):
        settings["socket"] = str(home / "lotrd.sock")
    return settings


def validate(settings):
    mode = settings.get("mode")
    if mode not in MODES:
        raise GatewayError("settings_invalid", f"mode must be one of {', '.join(MODES)}; got {mode!r}")
    if mode not in IMPLEMENTED_MODES:
        raise GatewayError("not_implemented",
                           f"mode {mode!r} is designed but not built in 0.1.0",
                           ["use 'local' or 'client' for now"])
    if mode == "client":
        hub = settings.get("hub") or {}
        missing = [k for k in ("url", "client_id", "credential_ref") if not hub.get(k)]
        if missing:
            raise GatewayError("settings_invalid", "client mode needs hub." + ", hub.".join(missing))
    local = settings.get("local") or {}
    if local.get("max_tier") not in ("read", "write", "consent"):
        raise GatewayError("settings_invalid", "local.max_tier must be read, write or consent")
    if local.get("confirm") not in ("auto", "dialog", "refuse", "none"):
        raise GatewayError("settings_invalid", "local.confirm must be auto, dialog, refuse or none")
    if not isinstance(local.get("allow"), list):
        raise GatewayError("settings_invalid", "local.allow must be a list of connection ids or globs")
    if settings.get("offline") not in ("fail-closed", "local-only"):
        raise GatewayError("settings_invalid", "offline must be 'fail-closed' or 'local-only'")


def init_settings(home, zone, mode):
    """Write a fresh gateway.json (mode 600). Refuses to overwrite an existing one."""
    home = Path(home)
    path = home / "gateway.json"
    if path.exists():
        raise GatewayError("exists", f"{path} already exists")
    settings = _merge(DEFAULTS, {"zone": zone, "mode": mode})
    if mode in IMPLEMENTED_MODES and mode != "client":
        validate(settings)
    write_json(path, settings)
    return settings
