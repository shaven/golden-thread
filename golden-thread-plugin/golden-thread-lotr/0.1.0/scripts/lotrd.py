#!/usr/bin/env python3
"""lotrd: the gt-lotr daemon.

    lotrd [--home H | --zone Z]

Serves the unix socket (<home>/lotrd.sock, or gateway.json "socket") in modes local and hub,
and in mode hub also HTTP on gateway.json "listen" for enrolled clients. Both front doors
share one engine. SIGTERM / SIGINT stop it cleanly and remove the socket. A socket left by a
dead daemon is removed on start; a live one makes this exit 1 rather than steal it.

Mode client runs no daemon; mode hybrid is refused in 0.1.0.
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
                state["reg"] = Registry.load(path)
                state["mtime"] = m
            return state["reg"]
    return get


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

    from lotrlib import server

    def factory():
        from lotrlib.engine import Engine         # lazy: the real engine only in the real daemon
        return Engine(home)

    holder = server.EngineHolder(factory)      # one engine for both front doors
    stop = threading.Event()

    def on_signal(signum, frame):
        stop.set()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    sock = cfg.get("socket") or str(home / "lotrd.sock")
    http_thread = None
    http_err = {}
    if mode == "hub":
        listen = cfg.get("listen") or {}

        def run_http():
            try:
                server.serve_http(holder, listen.get("host", "127.0.0.1"), listen.get("port", 8765),
                                  registry_getter=registry_getter_for(home / "registry.json"),
                                  tls_cert=listen.get("tls_cert"), tls_key=listen.get("tls_key"),
                                  stop_event=stop)
            except GatewayError as e:
                http_err["e"] = e.to_dict()
            except Exception as e:             # noqa: BLE001
                http_err["e"] = {"code": "http_failed", "message": type(e).__name__, "hints": []}
            finally:
                stop.set()                     # the HTTP door failing takes the daemon down
        http_thread = threading.Thread(target=run_http, name="lotrd-http", daemon=True)
        http_thread.start()

    try:
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
