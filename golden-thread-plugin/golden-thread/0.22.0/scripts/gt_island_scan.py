#!/usr/bin/env python3
"""gt_island_scan.py -- scan text on an island (0.22.0).

    scan(text, timeout=10.0, max_bytes=2097152, backend=None) -> dict
    probe(backend=None)                                        -> dict
    gt_island_scan.py [FILE|-] [--json]      scan a file or stdin
    gt_island_scan.py --probe                 what the island can and cannot do

An ISLAND is a process with no network, no disk writes, no secrets in its environment, and only a pipe in
and out. SCAN is what it does: it searches the text for what we do not want and reports rule ids and
line numbers -- never the matched text. The island is started by gt_sandbox_launch.py, which says which
backend ran and whether it isolates (isolated=False means the same scan ran in a clean environment but
NOT in a jail).

The result: {status: "scanned" | "not-scanned", why, findings: [{rule, line, hidden}], backend,
isolated}. A scan that cannot run, times out, or is handed more than `max_bytes` is "not-scanned" --
never "clean", and never an exception. Credentials are the secret scanner's job, not this one's; this
finds prompt injection and hidden characters (gt_intake_scan.hostile_findings).
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))     # resolved: a sandbox profile names real paths, not a symlink to them
MAX_BYTES = 2 * 1024 * 1024


def _launcher():
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import gt_sandbox_launch
    return gt_sandbox_launch


def _not_scanned(why, r=None):
    return {"status": "not-scanned", "why": why, "findings": [],
            "backend": (r or {}).get("backend", "none"), "isolated": bool((r or {}).get("isolated"))}


def scan(text, timeout=10.0, max_bytes=MAX_BYTES, backend=None):
    try:
        data = text.encode("utf-8", "replace") if isinstance(text, str) else bytes(text)
        if len(data) > max_bytes:
            return _not_scanned("text over the size cap (%d bytes)" % max_bytes)
        r = _launcher().run([sys.executable, "-I", os.path.join(HERE, "gt_island_scan.py"), "--worker"],
                            stdin=data, timeout=timeout, read_paths=[HERE], max_out=1 << 20, backend=backend)
        if r["timed_out"]:
            return _not_scanned("timed out", r)
        if r["rc"] != 0:
            return _not_scanned("the scanner did not run (exit %s)" % r["rc"], r)
        try:
            found = json.loads(r["out"].decode("utf-8"))["findings"]
            findings = [{"rule": str(f["rule"]), "line": int(f["line"]), "hidden": bool(f["hidden"])}
                        for f in found]
        except (ValueError, KeyError, TypeError):
            return _not_scanned("the scanner's answer could not be read", r)
        return {"status": "scanned", "why": "", "findings": findings,
                "backend": r["backend"], "isolated": r["isolated"]}
    except Exception as exc:                                 # noqa: BLE001
        return _not_scanned("scan error (%s)" % exc.__class__.__name__)


def scan_files(files, timeout=20.0, max_bytes=8 * 1024 * 1024, backend=None):
    """Scan many files in ONE island run (the add-on scan). `files` is [{name, text}]. -> {status, why,
    results: [{findings: [{rule, line, kind}]}] in the same order, backend, isolated}. Code files get the
    code rules as well as the prompt-injection rules. Never raises; failure is "not-scanned"."""
    try:
        files = [{"name": str(f["name"]), "text": str(f["text"])} for f in files]
        payload = json.dumps({"files": files}).encode("utf-8")
        if sum(len(f["text"].encode("utf-8", "replace")) for f in files) > max_bytes:
            return {"status": "not-scanned", "why": "text over the size cap (%d bytes)" % max_bytes,
                    "results": [], "backend": "none", "isolated": False}
        if not files:
            return {"status": "scanned", "why": "", "results": [], "backend": "none", "isolated": False}
        r = _launcher().run([sys.executable, "-I", os.path.join(HERE, "gt_island_scan.py"), "--worker-files"],
                            stdin=payload, timeout=timeout, read_paths=[HERE], max_out=4 << 20, backend=backend)
        base = {"results": [], "backend": r["backend"], "isolated": r["isolated"]}
        if r["timed_out"]:
            return dict(base, status="not-scanned", why="timed out")
        if r["rc"] != 0:
            return dict(base, status="not-scanned", why="the scanner did not run (exit %s)" % r["rc"])
        try:
            res = json.loads(r["out"].decode("utf-8"))["results"]
            res = [{"findings": [{"rule": str(f["rule"]), "line": int(f["line"]), "kind": str(f["kind"])}
                                 for f in item["findings"]]} for item in res]
            if len(res) != len(files):
                raise ValueError("count")
        except (ValueError, KeyError, TypeError):
            return dict(base, status="not-scanned", why="the scanner's answer could not be read")
        return dict(base, status="scanned", why="", results=res)
    except Exception as exc:                                 # noqa: BLE001
        return {"status": "not-scanned", "why": "scan error (%s)" % exc.__class__.__name__,
                "results": [], "backend": "none", "isolated": False}


def probe(backend=None):
    """What the island can do: tries a write, a network connection and a home read from inside it."""
    try:
        import tempfile
        r = _launcher().run([sys.executable, "-I", os.path.join(HERE, "gt_island_scan.py"), "--probe-worker",
                             os.path.expanduser("~"), tempfile.gettempdir()],
                            timeout=15.0, read_paths=[HERE], backend=backend)
        got = json.loads(r["out"].decode("utf-8")) if r["rc"] == 0 else {}
    except Exception:                                        # noqa: BLE001
        r, got = {"backend": "none", "isolated": False}, {}
    return {"backend": r.get("backend", "none"), "isolated": bool(r.get("isolated")),
            "write": got.get("write", "unknown"), "network": got.get("network", "unknown"),
            "read_home": got.get("read_home", "unknown")}


def _worker():
    text = sys.stdin.buffer.read().decode("utf-8", "replace")
    sys.path.insert(0, HERE)
    import gt_intake_scan
    found = [{"rule": r, "line": n, "hidden": h} for r, n, h in gt_intake_scan.hostile_findings(text)]
    sys.stdout.write(json.dumps({"findings": found}))
    return 0


def _worker_files():
    data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
    sys.path.insert(0, HERE)
    import gt_intake_scan
    out = []
    for f in data["files"]:
        out.append({"findings": [{"rule": rule, "line": line, "kind": kind}
                                 for kind, rule, line in gt_intake_scan.file_findings(f["name"], f["text"])]})
    sys.stdout.write(json.dumps({"results": out}))
    return 0


def _verdict(fn):
    """"denied" for what an operating system's refusal looks like: macOS gives EPERM; Linux's sandbox gives
    a read-only file system or an unreachable network. A refused connection is not a denial -- the far end
    answered, so the network was reached."""
    import errno
    try:
        fn()
        return "ALLOWED"
    except PermissionError:
        return "denied"
    except OSError as exc:
        if exc.errno in (errno.EPERM, errno.EACCES, errno.EROFS, errno.ENETUNREACH, errno.ENETDOWN):
            return "denied"
        return "failed-other"
    except Exception:                                        # noqa: BLE001
        return "failed-other"


def _home_verdict(home):
    """Hidden, missing or empty is denied; contents visible is ALLOWED."""
    try:
        return "ALLOWED" if os.listdir(home) else "denied"
    except OSError:
        return "denied"
    except Exception:                                        # noqa: BLE001
        return "failed-other"


def _probe_worker(home, tmp):
    import socket
    res = {}

    def write():
        path = os.path.join(tmp, "gt-island-probe.txt")
        with open(path, "w", newline="\n") as fh:
            fh.write("x")
        os.unlink(path)                                      # only reached when the write was allowed

    def net():
        s = socket.socket()
        s.settimeout(2)
        s.connect(("93.184.216.34", 80))

    res["write"] = _verdict(write)
    res["network"] = _verdict(net)
    res["read_home"] = _home_verdict(home)
    sys.stdout.write(json.dumps(res))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--worker"]:
        return _worker()
    if argv[:1] == ["--worker-files"]:
        return _worker_files()
    if argv[:1] == ["--probe-worker"]:
        return _probe_worker(argv[1], argv[2])
    if argv[:1] == ["--probe"]:
        print(json.dumps(probe()))
        return 0
    src = [a for a in argv if not a.startswith("--")]
    text = sys.stdin.read() if not src or src[0] == "-" else open(src[0], encoding="utf-8", errors="replace").read()
    r = scan(text)
    if "--json" in argv:
        print(json.dumps(r))
    else:
        print("%s (%s)%s" % (r["status"], "isolated: " + r["backend"] if r["isolated"] else "NOT isolated",
                              "" if r["status"] == "scanned" else ": " + r["why"]))
        for f in r["findings"]:
            print("  %s line %d" % (f["rule"], f["line"]))
    return 0 if r["status"] == "scanned" and not r["findings"] else 1


if __name__ == "__main__":
    sys.exit(main())
