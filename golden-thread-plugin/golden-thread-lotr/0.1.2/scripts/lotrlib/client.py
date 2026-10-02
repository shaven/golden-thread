"""Talk to the gateway: the local daemon's unix socket, or a hub over HTTP.

`Client.from_home(home)` reads gateway.json and picks the transport:
- mode local / hub -> the unix socket (gateway.json "socket", default <home>/lotrd.sock)
- mode client      -> HTTP to hub.url, bearer secret resolved from hub.credential_ref on
                      every request (never kept on the object)
- mode hybrid      -> GatewayError("not_implemented") in 0.1.0

A daemon or hub that cannot be reached is GatewayError("daemon_unreachable") with a hint,
never a traceback. Every error the far side reports comes back as a GatewayError.
"""
import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

from .errors import GatewayError
from .util import read_json

DEFAULT_TIMEOUT = 90.0


def _default_resolver(ref):
    from . import secrets                      # lazy: only client mode needs it
    return secrets.resolve(ref)


def _error_from(obj, fallback_code, fallback_msg):
    err = obj.get("error") if isinstance(obj, dict) else None
    if isinstance(err, dict) and isinstance(err.get("code"), str):
        return GatewayError(err["code"], str(err.get("message", "")), err.get("hints") or [])
    return GatewayError(fallback_code, fallback_msg)


class Client:
    def __init__(self, *, sock_path=None, url=None, client_id=None, credential_ref=None,
                 resolver=None, timeout=DEFAULT_TIMEOUT, home=None):
        if (sock_path is None) == (url is None):
            raise ValueError("give exactly one of sock_path or url")
        self.sock_path = str(sock_path) if sock_path is not None else None
        self.url = url.rstrip("/") if url else None
        self.client_id = client_id
        self.credential_ref = credential_ref
        self._resolver = resolver or _default_resolver
        self.timeout = float(timeout)
        self.home = str(home) if home is not None else None

    @classmethod
    def from_home(cls, home, *, resolver=None, timeout=None):
        home = Path(home)
        try:
            cfg = read_json(home / "gateway.json")
        except GatewayError as e:
            if e.code == "missing_file":
                raise GatewayError("not_initialized", f"no gateway.json in {home}",
                                   hints=[f"lotr --home {home} init --zone personal --mode local"])
            raise
        mode = cfg.get("mode", "local")
        if mode in ("local", "hub"):
            sock = cfg.get("socket") or str(home / "lotrd.sock")
            return cls(sock_path=sock, home=home,
                       timeout=timeout if timeout is not None else DEFAULT_TIMEOUT)
        if mode == "client":
            hub = cfg.get("hub") or {}
            if not hub.get("url") or not hub.get("client_id") or not hub.get("credential_ref"):
                raise GatewayError("config_invalid",
                                   "mode client needs hub.url, hub.client_id and hub.credential_ref",
                                   hints=[f"edit {home / 'gateway.json'}"])
            t = timeout if timeout is not None else max(float(hub.get("timeout_s") or 8), 1.0) * 4
            return cls(url=hub["url"], client_id=hub["client_id"],
                       credential_ref=hub["credential_ref"], resolver=resolver, timeout=t, home=home)
        if mode == "hybrid":
            raise GatewayError("not_implemented", "mode hybrid is not implemented in 0.1.0",
                               hints=["use mode local, hub or client"])
        raise GatewayError("config_invalid", f"unknown mode {mode!r} in gateway.json")

    # -------------------------------------------------------------- transport

    def request(self, method, params=None):
        params = params or {}
        if self.sock_path is not None:
            return self._unix(method, params)
        return self._http(method, params)

    def _unreachable(self, what):
        hint = f"start it: lotrd --home {self.home}" if self.home else "start it: lotrd --home <home>"
        return GatewayError("daemon_unreachable", f"no gateway daemon answering on {what}",
                            hints=[hint])

    def _unix(self, method, params):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            try:
                s.connect(self.sock_path)
            except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError):
                raise self._unreachable(self.sock_path)
            try:
                s.sendall((json.dumps({"id": 1, "method": method, "params": params}) + "\n")
                          .encode("utf-8"))
                buf = bytearray()
                while not buf.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
            except socket.timeout:
                raise GatewayError("daemon_timeout",
                                   f"the daemon did not answer within {self.timeout:.0f}s")
            except OSError:
                raise self._unreachable(self.sock_path)
        finally:
            s.close()
        if not buf:
            raise self._unreachable(self.sock_path)
        try:
            resp = json.loads(bytes(buf).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise GatewayError("bad_response", "the daemon sent something that is not JSON")
        if isinstance(resp, dict) and "error" in resp:
            raise _error_from(resp, "bad_response", "malformed error from the daemon")
        if not isinstance(resp, dict) or "result" not in resp:
            raise GatewayError("bad_response", "the daemon's reply has no result")
        return resp["result"]

    def _http(self, method, params):
        secret = self._resolver(self.credential_ref)
        req = urllib.request.Request(
            f"{self.url}/v1/{method}", data=json.dumps(params).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + secret,
                     "X-GT-Client": self.client_id, "User-Agent": "gt-lotr/0.1.0"})
        del secret
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:                   # noqa: BLE001
                body = None
            raise _error_from(body, f"http_{e.code}", f"hub answered HTTP {e.code}")
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            reason = getattr(e, "reason", e)
            raise GatewayError("daemon_unreachable",
                               f"hub {self.url} unreachable ({type(reason).__name__})",
                               hints=["check hub.url in gateway.json and that the hub's lotrd is running"])
        finally:
            req.headers.pop("Authorization", None)
        try:
            resp = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise GatewayError("bad_response", "the hub sent something that is not JSON")
        if isinstance(resp, dict) and "error" in resp:
            raise _error_from(resp, "bad_response", "malformed error from the hub")
        if not isinstance(resp, dict) or "result" not in resp:
            raise GatewayError("bad_response", "the hub's reply has no result")
        return resp["result"]
