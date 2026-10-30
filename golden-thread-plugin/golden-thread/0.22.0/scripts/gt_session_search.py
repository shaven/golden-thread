#!/usr/bin/env python3
"""gt_session_search.py -- opt-in search over past sessions (0.22.0).

    gt_session_search.py catch-up [--hook] [--max-seconds N]   index what changed since last time
    gt_session_search.py search QUERY [--days N] [--project P] [--limit N] [--json]
    gt_session_search.py status | rebuild

"What did we decide about the scheduler last week?" is answered today by remembering which session it
was. This indexes the TEXT of Claude Code's own transcripts (~/.claude/projects/**/*.jsonl) into a
SQLite FTS5 file and searches it. It is OFF by default (`session_search`): off reads no transcript, writes
nothing, and `search` says so.

WHAT IS INDEXED: the user's typed text and the assistant's text blocks. Never tool calls, tool results,
thinking, or text injected into the prompt (a row opening with "<"). Credential-shaped lines are masked by
the broker's scan BEFORE anything is stored (`[gt: line N withheld ...]`); when that scan cannot run the
text is not indexed at all.

WHERE: ~/.claude/golden-thread/state/session-search.db, mode 600 -- outside the vault and any synced
folder (an index of conversations concentrates whatever slipped into one, so it stays on this machine and
is owner-only). A hit is session (8 characters), time, project folder, and a snippet.

HOW IT STAYS CURRENT: `catch-up` runs at SessionStart (a hook, silent, fail-open, 8 seconds at most) and
reads only the bytes appended to each transcript since the last pass. `search` catches up first. A corrupt
index is deleted and rebuilt; an interpreter without FTS5 turns the feature off with a plain message.
It never raises into a hook.
"""
import argparse
import datetime
import glob
import json
import os
import re
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def state_dir():
    return os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "state")


def db_path():
    return os.path.join(state_dir(), "session-search.db")


def setting():
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import gt_settings
        return str(gt_settings.get("session_search") or "off").strip().lower()
    except Exception:                                        # noqa: BLE001
        return "off"


def fts5_available():
    try:
        sqlite3.connect(":memory:").execute("create virtual table t using fts5(x)")
        return True
    except sqlite3.Error:
        return False


def usable():
    """-> (ok, why-not)."""
    if setting() != "on":
        return False, "session_search is off (gt_settings.py set session_search on)"
    if not fts5_available():
        return False, "this Python's SQLite has no FTS5, so session search is off on this machine"
    return True, ""


def _vault():
    try:
        with open(os.path.join(os.path.expanduser("~"), ".claude", "vault-config.json"), encoding="utf-8") as fh:
            return json.load(fh).get("vault_path") or ""
    except Exception:                                        # noqa: BLE001
        return ""


def _redactor():
    """-> fn(text) -> masked text | None when the credential scan cannot run (then nothing is indexed)."""
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import gt_broker
        from pathlib import Path
        base = Path(_vault() or ".")
        if gt_broker.redact_content(base, "x") is None:
            return None
        return lambda t: (gt_broker.redact_content(base, t) or (None,))[0]
    except Exception:                                        # noqa: BLE001
        return None


def _open():
    os.makedirs(state_dir(), exist_ok=True)
    p = db_path()
    if not os.path.exists(p):
        os.close(os.open(p, os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600))
    con = sqlite3.connect(p, timeout=5)
    try:
        con.execute("create table if not exists files(path text primary key, mtime_ns integer, size integer, off integer)")
        con.execute("create virtual table if not exists msgs using fts5(text, session unindexed, ts unindexed, "
                    "project unindexed, role unindexed, row unindexed, tokenize='porter unicode61')")
    except BaseException:
        con.close()          # an open handle stops Windows deleting the damaged file (found by the Windows suite)
        raise
    return con


def _connect():
    """An open index, rebuilt from nothing when the file is damaged."""
    con = None
    try:
        con = _open()
        con.execute("select count(*) from msgs").fetchone()
        return con
    except sqlite3.DatabaseError:
        if con is not None:
            try:
                con.close()                      # before the delete: Windows cannot delete an open file
            except sqlite3.Error:
                pass
        for suffix in ("", "-journal", "-wal", "-shm"):
            try:
                os.unlink(db_path() + suffix)
            except OSError:
                pass
        return _open()


def _project(path):
    return os.path.basename(os.path.dirname(path)).rsplit("-", 1)[-1] or os.path.basename(os.path.dirname(path))


def _texts(row):
    """-> [(role, text)] for a transcript row: typed text and assistant text blocks only."""
    t = row.get("type")
    if t not in ("user", "assistant"):
        return []
    content = (row.get("message") or {}).get("content")
    out = []
    if isinstance(content, str):
        out.append(content)
    elif isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                out.append(b["text"])
    return [(t, x.strip()) for x in out if x.strip() and not (t == "user" and x.lstrip().startswith("<"))]


def catch_up(max_seconds=8.0):
    """Index what changed. -> (sessions touched, messages added). Never raises."""
    ok, _why = usable()
    if not ok:
        return 0, 0
    try:
        redact = _redactor()
        if redact is None:
            return 0, 0
        con = _connect()
        deadline = time.time() + max_seconds
        sessions = added = 0
        for path in sorted(glob.glob(os.path.join(os.path.expanduser("~"), ".claude", "projects", "*", "*.jsonl"))):
            if time.time() > deadline:
                break
            try:
                st = os.stat(path)
            except OSError:
                continue
            got = con.execute("select mtime_ns, size, off from files where path=?", (path,)).fetchone()
            if got and got[0] == st.st_mtime_ns and got[1] == st.st_size:
                continue
            off = got[2] if got and st.st_size >= got[1] else 0
            sid = os.path.basename(path)[:-len(".jsonl")]
            if off == 0:
                con.execute("delete from msgs where session=?", (sid[:8],))
            n = 0
            with open(path, "rb") as fh:
                fh.seek(off)
                data = fh.read()
            end = data.rfind(b"\n") + 1                      # whole lines only: a half-written last line waits
            for k, raw in enumerate(data[:end].split(b"\n")):
                try:
                    r = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if not isinstance(r, dict):
                    continue
                for role, text in _texts(r):
                    masked = redact(text)
                    if masked is None:
                        continue
                    con.execute("insert into msgs(text, session, ts, project, role, row) values (?,?,?,?,?,?)",
                                (masked, sid[:8], str(r.get("timestamp") or ""), _project(path), role, off + k))
                    n += 1
            con.execute("insert or replace into files values (?,?,?,?)", (path, st.st_mtime_ns, st.st_size, off + end))
            con.commit()
            sessions += 1
            added += n
        con.close()
        return sessions, added
    except Exception:                                        # noqa: BLE001
        return 0, 0


def search(query, days=None, project=None, limit=10):
    """-> (results, why-not)."""
    ok, why = usable()
    if not ok:
        return [], why
    catch_up()
    words = re.findall(r"\w+", query or "")
    if not words:
        return [], "no words to search for"
    q = " ".join('"%s"' % w for w in words)
    sql = ("select session, ts, project, role, row, snippet(msgs, 0, '[', ']', '…', 14) from msgs "
           "where msgs match ?")
    args = [q]
    if days is not None:
        cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).strftime(
            "%Y-%m-%dT%H:%M:%S")
        sql += " and ts >= ?"
        args.append(cutoff)
    if project:
        sql += " and lower(project) like ?"
        args.append("%" + project.lower() + "%")
    sql += " order by rank limit ?"
    args.append(int(limit))
    try:
        con = _connect()
        rows = con.execute(sql, args).fetchall()
        con.close()
    except sqlite3.Error as exc:
        return [], "the index could not be read (%s)" % exc.__class__.__name__
    return [{"session": r[0], "ts": r[1], "project": r[2], "role": r[3], "row": r[4], "snippet": r[5]}
            for r in rows], ""


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_session_search.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("catch-up")
    c.add_argument("--hook", action="store_true")
    c.add_argument("--max-seconds", type=float, default=8.0)
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--days", type=int)
    s.add_argument("--project")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--json", action="store_true")
    sub.add_parser("status")
    sub.add_parser("rebuild")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "catch-up":
            ok, _ = usable()
            if not ok:
                return 0
            n_s, n_m = catch_up(a.max_seconds)
            if not a.hook:
                print("indexed %d session(s), %d message(s)" % (n_s, n_m))
            return 0
        if a.cmd == "search":
            res, why = search(a.query, a.days, a.project, a.limit)
            if a.json:
                print(json.dumps({"results": res, "why": why or None}))
            elif why:
                print(why)
            else:
                for r in res:
                    print("%s  %s  %s  %s: %s" % (r["session"], r["ts"][:16], r["project"], r["role"], r["snippet"]))
                if not res:
                    print("no match")
            return 0
        if a.cmd == "rebuild":
            for suffix in ("", "-journal", "-wal", "-shm"):
                try:
                    os.unlink(db_path() + suffix)
                except OSError:
                    pass
            print("index removed; the next catch-up rebuilds it")
            return 0
        ok, why = usable()
        print("session_search: %s" % ("on, index at " + db_path() if ok else why))
        return 0
    except Exception:                                        # noqa: BLE001
        return 0


if __name__ == "__main__":
    sys.exit(main())
