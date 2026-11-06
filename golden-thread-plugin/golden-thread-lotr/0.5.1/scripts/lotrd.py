#!/usr/bin/env python3
"""lotrd: the gt-lotr daemon.

    lotrd [--home H | --zone Z]

Serves the unix socket (<home>/lotrd.sock, or gateway.json "socket") in modes local and hub,
and in mode hub also HTTP on gateway.json "listen" for enrolled clients. Both front doors
share one engine. SIGTERM / SIGINT stop it cleanly and remove the socket. A socket left by a
dead daemon is removed on start; a live one makes this exit 1 rather than steal it.

Mode client runs no daemon; mode hybrid is refused in 0.1.0.

0.5.0 (gt 0.21.4): in mode hub, gateway.json `listen.mcp_http: true` also serves POST /mcp on the
same HTTP port, MCP over HTTP for a plain MCP client (any MCP-over-HTTP connector); and
`listen.mcp_http_trust_loopback: true` additionally lets a request from this machine name an
enrolled client as ?cid=<id> with no secret. Both are off by default, must be a JSON true or false
(anything else stops the daemon with settings_invalid, never reads as true), and need a restart
to change. See lotrlib/server.py.

0.3.0: on native Windows the local front door is a named pipe (gt core's gt_ipc: user-SID-only
DACL, remote clients rejected, first-instance only) instead of the unix socket; the
gateway.json "socket" setting does not apply there. Both doors identify every caller from the
kernel and, while gt unlock is on, hand that identity to the engine (lotrlib.unlock).
"""
import argparse
import json
import os
import signal
import sys
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lotrlib.errors import GatewayError          # noqa: E402
from lotrlib.util import lotr_home, read_json    # noqa: E402


def _fail(err):
    sys.stderr.write(json.dumps({"ok": False, "error": err}) + "\n")
    return 1


def registry_getter_for(path):
    """A getter that reloads the registry when registry.json's mtime changes."""
    state = {"mtime": None, "reg": None}
    lock = threading.Lock()

    def get():
        from lotrlib.registry import Registry
        with lock:
            m = os.stat(path).st_mtime_ns
            if state["reg"] is None or m != state["mtime"]:
                # the hub's live reload checks ownership and mode every time (review M2)
                state["reg"] = Registry.load(path, check_perms=True)
                state["mtime"] = m
            return state["reg"]
    return get


def _warn_listen(listen):
    """One stderr line per listen switch that widens access (phase 120). Silent when none is on."""
    try:
        from lotrlib import config
        for note in config.listen_notes(listen):
            sys.stderr.write("lotrd: WARNING " + note + "\n")
    except Exception:                      # noqa: BLE001 - advisory
        pass


def main(argv=None):
    p = argparse.ArgumentParser(prog="lotrd", description="gt-lotr daemon")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--home")
    g.add_argument("--zone")
    ns = p.parse_args(argv)
    home = Path(ns.home).expanduser() if ns.home else lotr_home(ns.zone)

    try:
        cfg = read_json(home / "gateway.json")
    except GatewayError as e:
        return _fail(e.to_dict())
    mode = cfg.get("mode", "local")
    if mode == "hybrid":
        return _fail(GatewayError("not_implemented", "mode hybrid is not implemented in 0.1.0").to_dict())
    if mode == "client":
        return _fail(GatewayError("no_daemon_in_client_mode",
                                  "mode client talks to the hub; it runs no daemon").to_dict())
    if mode not in ("local", "hub"):
        return _fail(GatewayError("config_invalid", f"unknown mode {mode!r}").to_dict())

    from lotrlib import config, server

    try:
        config.check_listen(cfg.get("listen"))     # this file is read raw, not through load_settings
    except GatewayError as e:
        return _fail(e.to_dict())

    def factory():
        from lotrlib.engine import Engine         # lazy: the real engine only in the real daemon
        return Engine(home)

    holder = server.EngineHolder(factory)      # one engine for both front doors
    stop = threading.Event()

    def on_signal(signum, frame):
        stop.set()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    # SIGHUP: re-read the registry now. The handler only sets an event; a thread does the work,
    # because the holder's lock is not reentrant and a handler runs on the thread that may hold it.
    hup = threading.Event()
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, lambda signum, frame: hup.set())

        def reload_on_hup():
            while not stop.is_set():
                if hup.wait(0.5):
                    hup.clear()
                    try:
                        holder.run(lambda e: e.reload())
                        sys.stderr.write("lotrd: reloaded on SIGHUP\n")
                    except Exception as e:             # noqa: BLE001
                        sys.stderr.write(f"lotrd: reload on SIGHUP failed: {type(e).__name__}\n")
        threading.Thread(target=reload_on_hup, name="lotrd-hup", daemon=True).start()

    sock = cfg.get("socket") or str(home / "lotrd.sock")
    http_thread = None
    http_err = {}
    if mode == "hub":
        listen = cfg.get("listen") or {}
        _warn_listen(listen)

        def run_http():
            try:
                server.serve_http(holder, listen.get("host", "127.0.0.1"), listen.get("port", 8765),
                                  registry_getter=registry_getter_for(home / "registry.json"),
                                  tls_cert=listen.get("tls_cert"), tls_key=listen.get("tls_key"),
                                  stop_event=stop,
                                  mcp_http=listen.get("mcp_http", False),
                                  mcp_http_trust_loopback=listen.get("mcp_http_trust_loopback",
                                                                     False))
            except GatewayError as e:
                http_err["e"] = e.to_dict()
            except Exception as e:             # noqa: BLE001
                http_err["e"] = {"code": "http_failed", "message": type(e).__name__, "hints": []}
            finally:
                stop.set()                     # the HTTP door failing takes the daemon down
        http_thread = threading.Thread(target=run_http, name="lotrd-http", daemon=True)
        http_thread.start()

    try:
        if os.name == "nt":
            server.serve_pipe(holder, home, stop_event=stop)
        else:
            server.serve_unix(holder, sock, stop_event=stop)
    except GatewayError as e:
        stop.set()
        return _fail(e.to_dict())
    except OSError as e:
        stop.set()
        return _fail({"code": "socket_failed", "message": f"{type(e).__name__} on {sock}", "hints": []})
    finally:
        if http_thread is not None:
            http_thread.join(10)
    if http_err:
        return _fail(http_err["e"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
