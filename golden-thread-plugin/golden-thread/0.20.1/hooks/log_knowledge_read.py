#!/usr/bin/env python3
"""log_knowledge_read.py -- called by log_knowledge_read.sh (PostToolUse, matcher Read).

Appends one line per Read of <vault>/Knowledge/**.md to <vault>/usage/knowledge.jsonl:

    {"page": "Knowledge/Some Page.md", "session": "<first 8 of the session id>",
     "date": "YYYY-MM-DD", "trigger": "read"}

`trigger` is "read": the hook sees the Read, not which skill asked for it, so it does not guess
between /gt:gt-query and /gt:gt-open. The session id is cut to 8 characters -- enough to tell
sessions apart, short enough that the log is not a list of resumable session ids.

The log is a LOCAL access record, not shared content: the first write creates
`usage/.gitignore` containing `*`, so the folder never reaches the vault's git history (and no
root .gitignore is edited). It is JSON Lines, not Markdown, so it is not vault content under the
write queue -- the same standing as the spool files the vault tools append to.

Fails open: every error is swallowed and the hook prints nothing.
"""
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:                                            # noqa: BLE001
        return
    if not isinstance(payload, dict) or payload.get("tool_name") != "Read":
        return
    ti = payload.get("tool_input")
    target = ti.get("file_path") if isinstance(ti, dict) else None
    if not isinstance(target, str) or not target.lower().endswith(".md"):
        return
    try:
        import gt_settings                                       # noqa: PLC0415
        if gt_settings.get("knowledge_access_log") == "off":
            return
    except Exception:                                            # noqa: BLE001
        pass                     # no settings module -> the registered default, on
    try:
        from gt_paths import find_vault                          # noqa: PLC0415
        vault = find_vault()
    except Exception:                                            # noqa: BLE001
        return
    if not vault:
        return
    v = os.path.realpath(str(vault))
    target = os.path.expanduser(target)
    if not os.path.isabs(target):
        cwd = payload.get("cwd")
        target = os.path.join(cwd if isinstance(cwd, str) and os.path.isabs(cwd)
                              else os.getcwd(), target)
    t = os.path.realpath(target)
    knowledge = os.path.join(v, "Knowledge") + os.sep
    if not t.startswith(knowledge):
        return
    rel = os.path.relpath(t, v).replace(os.sep, "/")
    sid = str(payload.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "")[:8]
    usage = os.path.join(v, "usage")
    os.makedirs(usage, exist_ok=True)
    ignore = os.path.join(usage, ".gitignore")
    if not os.path.exists(ignore):
        with open(ignore, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# local access log (gt log_knowledge_read hook): never committed\n*\n")
    line = json.dumps({"page": rel, "session": sid,
                       "date": datetime.date.today().isoformat(), "trigger": "read"})
    # One write() of one short line in append mode: concurrent sessions interleave whole lines.
    with open(os.path.join(usage, "knowledge.jsonl"), "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:                                            # noqa: BLE001
        pass
    sys.exit(0)
