#!/usr/bin/env python3
"""oauth_diag.py -- what `lotr connect` would see at an MCP server, printed with NO secret in it.

    python3 dev/oauth_diag.py <https MCP endpoint> [--lotr-scripts DIR] [--register]
                              [--home GATEWAY_HOME --name NAME] [--allow-insecure-localhost]

Prints a redacted structure: each request's method, URL without query, status, content type and
size; the WWW-Authenticate parameters; the protected-resource and authorization-server facts
(issuer, endpoint paths, PKCE methods, registration / metadata-document support, `iss` support,
scopes); and the error CODE of any refusal. It makes only the unauthenticated requests discovery
makes. With --register it also performs dynamic client registration (a harmless client record at
the server) and prints the status and whether the answer is acceptable -- never the client id's
full value. It never starts a login, never asks for or prints a token, code, state or verifier.
With --home/--name it adds the registered connection's shape (ref SCHEMES only, level, pinned
hosts) and whether the stored refresh-token file exists and its mode.
"""
import argparse
import json
import os
import platform
import sys
import urllib.parse


def _scripts(arg):
    if arg:
        return arg
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.join(os.path.dirname(here), "golden-thread-lotr")
    best = None
    for v in sorted(os.listdir(root)):
        d = os.path.join(root, v, "scripts")
        if os.path.isfile(os.path.join(d, "lotrlib", "oauth.py")):
            best = d
    if not best:
        sys.exit("oauth_diag: no golden-thread-lotr/<version>/scripts/lotrlib/oauth.py here; "
                 "pass --lotr-scripts DIR")
    return best


def _show(text, limit=200):
    """Printable ASCII only (a space is fine); everything else becomes ?."""
    import re
    return re.sub(r"[^\x20-\x7e]", "?", str(text))[:limit]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("endpoint")
    ap.add_argument("--lotr-scripts")
    ap.add_argument("--register", action="store_true")
    ap.add_argument("--home")
    ap.add_argument("--name")
    ap.add_argument("--allow-insecure-localhost", action="store_true")
    ap.add_argument("--allow-private-network", action="store_true")
    ns = ap.parse_args()
    sys.path.insert(0, _scripts(ns.lotr_scripts))
    from lotrlib import oauth
    from lotrlib.errors import GatewayError

    out = {"python": platform.python_version(), "platform": platform.platform(),
           "spec": oauth.SPEC_REVISION, "requests": []}
    net = oauth.NetPolicy(ns.allow_private_network, ns.allow_insecure_localhost)

    def traced(url, net_, **kw):
        r = oauth.fetch(url, net_, **kw)
        ch = oauth.parse_www_authenticate(r.header("WWW-Authenticate"))
        out["requests"].append({
            "method": kw.get("method", "GET"), "url": oauth.safe_url(url), "status": r.status,
            "content_type": oauth.clean((r.header("Content-Type") or [""])[0], 80),
            "bytes": len(r.body),
            "www_authenticate": {k: _show(v) for k, v in ch.items()
                                 if k in ("realm", "resource_metadata", "scope", "error")} or None})
        return r

    try:
        d = oauth.discover(ns.endpoint, net, fetcher=traced)
        out["discovery"] = {k: (oauth.clean(d[k]) if isinstance(d[k], str) else d[k])
                            for k in ("issuer", "resource", "scopes", "iss_param_supported",
                                      "cimd_supported", "auth_methods", "hosts", "notes",
                                      "authorization_endpoint", "token_endpoint",
                                      "registration_endpoint", "revocation_endpoint", "prm_url")}
        out["discovery"]["pkce_s256"] = True        # discovery refuses an AS without it
        out["verdict"] = ("registration: dynamic" if d["registration_endpoint"] else
                          "registration: NONE -- needs --client-id") + \
                         ("; metadata documents supported" if d["cimd_supported"] else "")
        if ns.register:
            try:
                reg = oauth.register_client(d, net, "http://127.0.0.1:1/callback")
                out["registration"] = {"ok": True, "source": reg["source"],
                                       "auth_method": reg["token_endpoint_auth_method"],
                                       "client_id_chars": len(reg["client_id"]),
                                       "client_id_prefix": reg["client_id"][:6] + "...",
                                       "has_secret": bool(reg["client_secret"])}
            except GatewayError as e:
                out["registration"] = {"ok": False, "code": e.code, "message": oauth.clean(e.message, 300)}
    except GatewayError as e:
        out["error"] = {"code": e.code, "message": oauth.clean(e.message, 300),
                        "hints": [oauth.clean(h, 200) for h in e.hints]}
    if ns.home and ns.name:
        try:
            home = os.path.expanduser(ns.home)
            reg = json.load(open(os.path.join(home, "registry.json")))
            cid = ns.name if "@" in ns.name else "%s@%s" % (ns.name, reg.get("zone"))
            c = next((x for x in reg["connections"] if x["id"] == cid), None)
            if c is None:
                out["connection"] = "not registered"
            else:
                a = c.get("auth") or {}
                ob = a.get("oauth") or {}
                ref = a.get("token_ref") or ""
                info = {"scheme": a.get("scheme"), "refresh_ref_scheme": ref.split(":", 1)[0] or None,
                        "client_source": ob.get("client_id_source"), "scopes": ob.get("scopes"),
                        "pinned_hosts": (c.get("network") or {}).get("hosts"),
                        "tools_cached": len(c.get("tools") or [])}
                if ref.startswith("store:"):
                    sd = os.environ.get("LOTR_STORE_DIR") or os.path.expanduser("~/.secrets")
                    p = os.path.join(sd, ref.split(":", 1)[1])
                    info["store_file"] = {"exists": os.path.isfile(p),
                                          "mode": oct(os.stat(p).st_mode & 0o777) if os.path.isfile(p) else None}
                out["connection"] = info
        except (OSError, ValueError, KeyError) as e:
            out["connection"] = "unreadable (%s)" % type(e).__name__
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
