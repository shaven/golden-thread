"""gateway.json: where this gateway runs and how its front ends reach it (ADR-4).

Placement is a per-zone setting, not a fixed shape:
  local   the daemon runs on this machine (a one-box work machine)
  hub     the daemon runs here AND serves enrolled client machines over HTTP
  client  no daemon here; the CLI and MCP shim talk to a hub
  hybrid  a local edge in front of a hub -- parsed, refused in 0.1.0

Defaults are merged under whatever the file says, so an old gateway.json keeps working when a
release adds a key.

0.5.0 (gt 0.21.4): two switches under `listen`, both off by default, for the hub's MCP-over-HTTP
front door (`POST /mcp`, lotrlib/server.py):
  mcp_http                  serve /mcp on the hub's HTTP port (otherwise /mcp is a 404)
  mcp_http_trust_loopback   also accept a request from this machine with no secret, naming an
                            enrolled client as `?cid=<id>`; for an MCP connector that cannot send
                            headers. Off unless you turn it on, and it needs mcp_http.
Both must be a real JSON true or false: a string such as "false" is refused, never read as true.
"""
import copy
import os
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
    "listen": {"host": "127.0.0.1", "port": 8765, "tls_cert": None, "tls_key": None,
               "mcp_http": False, "mcp_http_trust_loopback": False},
    "hub": {"url": None, "client_id": None, "credential_ref": None, "timeout_s": 8},
    # 0.5.1 (phase 70): let an unattended hub client write on an --l1 OAuth connection while a
    # person's gt unlock session is open. Off by default: today's refusal stands.
    "presence": {"gate_writes": False, "gate_reads": False, "timeout_s": 1800},
    "local_connections": [],
    # Callers on this machine (the socket, the MCP shim, the CLI) are not exempt from policy:
    # they get an allow list and a tier ceiling like any enrolled client, and consent-tier
    # operations need the owner's confirmation raised by the daemon itself (ADR-5).
    "local": {"allow": ["*"], "max_tier": "consent", "confirm": "auto"},
    "offline": "fail-closed",
    # registry.json / gateway.json / recipes are re-read on the next request after they change;
    # false = only SIGHUP reloads them (the old restart-to-pick-up-a-change behaviour)
    "registry_watch": True,
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
    """The admin plane is files; they must be private, or the gateway refuses to run (ADR-5).

    Native Windows has no mode bits to check (st_mode reports 0o666 / 0o444 whatever the ACL
    says), so this check is skipped there. What protects the home on Windows is where it is:
    under the user's profile (.config/gt-lotr in %USERPROFILE%), whose inherited ACL grants only
    the user, SYSTEM and Administrators -- the same boundary a 700 directory draws on POSIX.
    The local front door is checked separately (a user-SID-only named pipe, gt_ipc)."""
    if os.name == "nt":
        return
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


LISTEN_FLAGS = ("mcp_http", "mcp_http_trust_loopback")


def check_listen(listen):
    """The `listen` switches must be real booleans. Refused, not coerced: bool("false") is True,
    and a switch that opens a door must never be turned on by a misspelt off. lotrd calls this
    too, because it reads gateway.json itself (the daemon builds the engine lazily)."""
    if listen is None:
        return
    if not isinstance(listen, dict):
        raise GatewayError("settings_invalid", "listen must be an object")
    for key in LISTEN_FLAGS:
        if key in listen and not isinstance(listen[key], bool):
            raise GatewayError("settings_invalid", f"listen.{key} must be true or false",
                               hints=[f"write it as a JSON boolean: \"{key}\": false"])


LOOPBACK_WIDENS = ("listen.mcp_http_trust_loopback is ON: any program on this machine can name an "
                   "enrolled client with no secret (?cid=<id>) and act as it, so enrol that client at "
                   "the lowest tier it needs. Turn it off and send the Bearer header to close it.")


MULTIUSER_WIDENS = ("listen.mcp_http_trust_loopback is ON and this Windows host has more than one user session: "
                    "every signed-in account can reach 127.0.0.1 and name a client with no secret. "
                    "Turn it off on a shared or remote-desktop host.")


def user_sessions():
    """Ids of the Windows user sessions that are active or disconnected (services' session 0 and the
    listener excluded); None where that cannot be read (not Windows, or the call failed)."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes as wt

        class Info(ctypes.Structure):
            _fields_ = [("SessionId", wt.DWORD), ("Name", ctypes.c_wchar_p), ("State", wt.DWORD)]
        w = ctypes.WinDLL("wtsapi32")
        base = ctypes.c_void_p()
        n = wt.DWORD()
        if not w.WTSEnumerateSessionsW(None, 0, 1, ctypes.byref(base), ctypes.byref(n)):
            return None
        try:
            arr = ctypes.cast(base, ctypes.POINTER(Info))
            return [arr[i].SessionId for i in range(n.value)
                    if arr[i].SessionId not in (0, 65536) and arr[i].State in (0, 4)]
        finally:
            w.WTSFreeMemory(base)
    except Exception:                                      # noqa: BLE001
        return None


def listen_notes(listen, sessions=None):
    """What a `listen` switch that is on widens, one line each; [] when nothing is widened. The trust
    switch only applies with `mcp_http`, so it is silent without it. Said at daemon start-up and in
    `lotr status` so turning it on is never silent (phase 120)."""
    if not isinstance(listen, dict):
        return []
    if listen.get("mcp_http") is True and listen.get("mcp_http_trust_loopback") is True:
        notes = [LOOPBACK_WIDENS]
        sessions = user_sessions() if sessions is None else sessions
        if sessions is not None and len(sessions) > 1:         # a warning only, never a refusal (D7)
            notes.append(MULTIUSER_WIDENS)
        return notes
    return []


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
    if not isinstance(settings.get("registry_watch"), bool):
        raise GatewayError("settings_invalid", "registry_watch must be true or false",
                           hints=['write it as a JSON boolean: "registry_watch": false'])
    local = settings.get("local") or {}
    if local.get("max_tier") not in ("read", "write", "consent"):
        raise GatewayError("settings_invalid", "local.max_tier must be read, write or consent")
    if local.get("confirm") not in ("auto", "dialog", "refuse", "none", "biometric"):
        raise GatewayError("settings_invalid",
                           "local.confirm must be auto, dialog, refuse, none or biometric")
    if not isinstance(local.get("allow"), list):
        raise GatewayError("settings_invalid", "local.allow must be a list of connection ids or globs")
    if settings.get("offline") not in ("fail-closed", "local-only"):
        raise GatewayError("settings_invalid", "offline must be 'fail-closed' or 'local-only'")
    check_listen(settings.get("listen"))
    check_limits(settings.get("limits"))
    check_presence(settings.get("presence"))


def check_presence(block):
    """presence.gate_writes / gate_reads are booleans; timeout_s is 60 to 28800 seconds."""
    if block is None:
        return
    if not isinstance(block, dict) or set(block) - {"gate_writes", "gate_reads", "timeout_s"}:
        raise GatewayError("settings_invalid",
                           "presence may hold only gate_writes, gate_reads and timeout_s")
    for k in ("gate_writes", "gate_reads"):
        if k in block and not isinstance(block[k], bool):
            raise GatewayError("settings_invalid", f"presence.{k} must be true or false")
    t = block.get("timeout_s")
    if "timeout_s" in block and (not isinstance(t, int) or isinstance(t, bool)
                                 or not 60 <= t <= 28800):
        raise GatewayError("settings_invalid", "presence.timeout_s must be 60 to 28800 seconds")


def check_limits(limits):
    """The 0.5.0 limits (gt 0.21.4) are optional; present, each must be a plain number: the find
    budget in tokens (50 to 1,000,000) and the reference lifetime in seconds (above 0; the engine
    holds it to the action tokens' ceiling, 900)."""
    if limits is None:
        return
    if not isinstance(limits, dict):
        raise GatewayError("settings_invalid", "limits must be an object")
    for key, lo, hi in (("find_budget_tokens", 50, 1_000_000), ("ref_ttl_s", 1, None),
                        ("max_call_timeout_s", 1, 3600),               # 0.5.1: ceilings a caller cannot
                        ("max_result_chars_ceiling", 1000, 10_000_000)):   # raise (lotrlib/upstream.py)
        if key in limits:
            v = limits[key]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < lo \
                    or (hi is not None and v > hi):
                raise GatewayError("settings_invalid", "limits.%s must be a number%s"
                                   % (key, " from %d to %d" % (lo, hi) if hi else " of at least %d" % lo))


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
