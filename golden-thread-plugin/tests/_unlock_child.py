"""A client process for the unlock tests (0.20.0): the authority identifies callers by their
KERNEL identity and ancestry, so each role (a shim, a Bash-spawned process, a fake `claude`)
must be a real process of its own.

    python _unlock_child.py <scripts-dir> <address>

Reads one JSON command per line on stdin and writes one JSON reply per line:
    {"call": METHOD, "params": {...}, "answers": [..]}  -> {"result": ...} | {"error": {...}}
    {"spawn": true}        -> starts a child of THIS process running the same loop and
                              relays further lines to it until {"unspawn": true}
    {"pid": true}          -> {"pid": N, "start": S}
    {"exit": true}         -> exits
`answers` are handed, in order, to the authority's mid-request questions (a stand-in for
the person typing at a terminal).
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, sys.argv[1])
import gt_ipc  # noqa: E402

ADDR = sys.argv[2]


def main():
    child = None
    for line in sys.stdin:
        cmd = json.loads(line)
        if child is not None:
            if cmd.get("exit"):
                child.stdin.write(json.dumps({"exit": True}) + "\n")
                child.stdin.flush()
                child.wait(10)
                return 0
            if cmd.get("unspawn"):
                child.stdin.write(json.dumps({"exit": True}) + "\n")
                child.stdin.flush()
                child.wait(10)
                child = None
                out = {"ok": True}
            else:
                child.stdin.write(line)
                child.stdin.flush()
                out = json.loads(child.stdout.readline())
        elif cmd.get("exit"):
            return 0
        elif cmd.get("pid"):
            info = gt_ipc.process_info(os.getpid())
            out = {"pid": os.getpid(), "start": info["start"]}
        elif cmd.get("spawn"):
            child = subprocess.Popen([sys.executable, __file__] + sys.argv[1:],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            out = {"ok": True}
        else:
            answers = list(cmd.get("answers") or [])

            def answer(need):
                return answers.pop(0) if answers else None
            try:
                c = gt_ipc.connect(ADDR, answer=answer)
                try:
                    out = {"result": c.call(cmd["call"], cmd.get("params") or {})}
                finally:
                    c.close()
            except gt_ipc.IpcError as e:
                out = {"error": e.to_dict()}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
