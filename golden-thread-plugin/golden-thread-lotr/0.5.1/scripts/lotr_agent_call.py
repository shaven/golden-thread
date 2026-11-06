#!/usr/bin/env python3
"""lotr_agent_call.py -- the one script a generated gt-lotr agent runs (gt-lotr 0.5.1, plan phase 40).

    lotr_agent_call.py [--home DIR | --zone Z] CONNECTION_ID OPERATION [--args JSON] [--select PATHS]

It builds a read call from the connection's manifest (`lotr onboard discover`), submits it through the gateway, and
routes the response by size: a small result is printed as it is; a large one is stored whole in a private file and
only a summary, the file path and the sizes are printed. See lotrlib/onboard_route.py for the rules.

Exit: 0 ok | 2 refused before anything was sent | 3 the gateway answered with an error (its code is printed).
It prints no credential and sends none: signing happens in the gateway, and the result is scanned there and again here.
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lotrlib import client as client_mod, onboard_discover, onboard_route   # noqa: E402
from lotrlib.errors import GatewayError                                      # noqa: E402
from lotrlib.util import lotr_home                                            # noqa: E402

_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _refuse(why):
    sys.stdout.write("REFUSED: %s\n" % why)
    return 2


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lotr_agent_call.py")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--home")
    g.add_argument("--zone")
    ap.add_argument("connection")
    ap.add_argument("op")
    ap.add_argument("--args", default="{}")
    ap.add_argument("--select")
    ns = ap.parse_args(argv)
    if not onboard_discover.CONN_OK.match(ns.connection or ""):
        return _refuse("the connection id must look like name@zone")
    if not _NAME.match(ns.op or ""):
        return _refuse("the operation name has characters that are not allowed")
    home = os.path.expanduser(ns.home) if ns.home else str(lotr_home(ns.zone))
    mf = os.path.join(home, "onboard", "%s.manifest.json" % ns.connection)
    try:
        with open(mf, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        return _refuse("no manifest for %s; run: lotr onboard discover %s" % (ns.connection, ns.connection))
    try:
        args = json.loads(ns.args)
    except ValueError:
        return _refuse("--args is not valid JSON")
    why = onboard_route.check_call(manifest, ns.op, args)
    if why:
        return _refuse(why)
    params = {"tool": "call_read", "connection": ns.connection, "op": ns.op, "args": args}
    if ns.select:
        params["select"] = ns.select
    try:
        reply = client_mod.Client.from_home(home).request("call", params)
    except GatewayError as e:
        sys.stdout.write("GATEWAY ERROR: %s: %s\n" % (e.code, e.message))
        for h in e.hints:
            sys.stdout.write("  hint: %s\n" % h)
        return 3
    if isinstance(reply, dict) and reply.get("ok") is False:
        err = reply.get("error") or {}
        sys.stdout.write("GATEWAY ERROR: %s: %s\n" % (err.get("code", "error"), err.get("message", "")))
        for h in err.get("hints") or []:
            sys.stdout.write("  hint: %s\n" % h)
        return 3
    result = onboard_route.shape_reply(reply)
    text, _route = onboard_route.route_result(home, ns.connection, ns.op, sorted(args), result)
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
