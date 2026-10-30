#!/usr/bin/env python3
"""gt_connect.py -- pull items from a connector into Sources/ (0.22.0, phase 86).

    gt_connect.py pull CONNECTOR --project SLUG [--vault V] [--limit N] [--json]
                  [--endpoint URL] [--app ID] [--secret-file F]
    gt_connect.py list --project SLUG [--vault V] [--json]

A connector is one row of `Projects/<slug>/connectors.md`, a Markdown table the owner writes:

    | connector | connection | selector | limit | zone | access |
    | notes | github@personal | owner/repo:docs | 20 | personal | read |
    | files | m365@personal | Documents/notes | 20 | personal | read |

`access` is optional and must be `read`. The first connectors are GitHub contents (selector
`owner/repo:path`, one folder, not recursive) and OneDrive files through Microsoft 365 (selector a
folder path); mail comes later. PERSONAL ZONE ONLY: a connector whose zone is `work`, or has no
zone, or whose zone disagrees with its connection id is REFUSED -- the work-zone ruling has been
open since 2026-10-02 and no work endpoint is in scope until the owner gives it.

EVERYTHING FETCHED IS UNTRUSTED DATA. Each item enters through the same path as a binary document
(gt_ingest_binary.py, reused): the original is stored byte for byte as `Sources/YYYY-MM-DD <name>`
(exclusive create, never overwritten), a text item is scanned by gt_intake_scan.py BEFORE anything
is written (a flagged item is held and reported by its location only, never its text), a PDF or
an image goes through gt_ingest_binary.ingest, and anything else is refused with its type named.
Beside each original goes `<name> (origin).md`, marked `derived: true`, with the origin, the
connection, the fetch date and the content hash; that sidecar is how an unchanged item is
recognised (stored nothing) and how a changed one is stored new with `supersedes:` naming the old
original (gt-refresh already understands it). A listing of more items than the limit (the table's
limit is a ceiling; --limit can only lower it) stops BEFORE any item is fetched.

WHERE IT WRITES. Only `Sources/`, by exclusive create, as gt_ingest_binary.py does: the write queue
refuses Sources/ (immutable), and a pull queues no note, so no queue request is made. Nothing is
ever ingested or promoted by the pull; the owner starts the ingest from the stored files.

HOW IT REACHES LOTR. Only through the read tier (`call_read`), only `GET` operations, through an
enrolled client of its own (phase 71: `lotr app-connect gt-connect`, least privilege, read tier).
The service credential stays inside LOTR; this script holds only its own client secret file, read
at call time and never printed. With gt unlock on in mcp_only mode a Bash-run script is refused by
the authority: use this enrolled client, or run it with unlock off. A real pull needs lotrd's
`listen.mcp_http` on; the operations used (`GET /repos/{owner}/{repo}/contents/{path}`,
`GET /me/drive/root:/{folder}:/children`, `GET /me/drive/items/{id}/content`) are raw LOTR
operations whose payload shape has not been proved against a live gateway (unverified).

Exit: 0 pulled (items held or refused are reported) | 1 refused before or during the listing | 2 usage.
"""
import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import gt_ingest_binary as ibin                           # noqa: E402

SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}(/[A-Za-z0-9][A-Za-z0-9._-]{0,79})?$")
CONN = re.compile(r"^([a-z0-9][a-z0-9-]{0,30})@([a-z]{1,16})$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PART = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
SAFE_LOC = re.compile(r"^[A-Za-z0-9 ._()/\-]{1,200}$")
DEFAULT_LIMIT = 20
MAX_LIMIT = 200
MAX_REPLY = 40 * 1024 * 1024
ZONE_RULING = ("the work-zone ruling is still open (since 2026-10-02): only a connector in the "
               "personal zone, whose connection id ends @personal, may pull into Sources/")
KINDS = {"github": "github", "m365": "onedrive"}


class LotrError(Exception):
    """The gateway refused or could not be reached. The message never holds a secret."""


def kind_of(connection):
    m = CONN.match(connection or "")
    return KINDS.get(m.group(1)) if m else None


# -- the registry -----------------------------------------------------------------------------

def parse_registry(text):
    """-> (rows, problems). Rows of the first Markdown table with a `connector` column."""
    rows, problems, head = [], [], None
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        low = [c.lower() for c in cells]
        if head is None:
            if "connector" in low and "connection" in low:
                head = low
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue
        row = dict(zip(head, cells + [""] * (len(head) - len(cells))))
        try:
            row["limit"] = int(row.get("limit") or DEFAULT_LIMIT)
        except ValueError:
            problems.append("connector %r: the limit is not a number" % row.get("connector"))
            continue
        rows.append(row)
    return rows, problems


def check_row(row):
    """-> a refusal reason, or None when the connector may pull."""
    conn = row.get("connection", "")
    m = CONN.match(conn)
    if not m:
        return "the connection id must look like name@zone"
    zone = (row.get("zone") or "").strip().lower()
    if zone != "personal" or m.group(2) != "personal":
        return ZONE_RULING + (" (this connector has %s)" % ("no zone" if not zone else "zone %r" % zone))
    if kind_of(conn) is None:
        return "no connector kind for %s: GitHub (github@) and OneDrive (m365@) only for now" % conn
    if (row.get("access") or "read").lower() != "read":
        return "a connector pulls through the read tier only (access must be read)"
    if not 1 <= row["limit"] <= MAX_LIMIT:
        return "the limit must be 1 to %d" % MAX_LIMIT
    return None


def parse_selector(kind, selector):
    """-> a dict of the parts, or None when the selector is malformed or hostile."""
    if kind == "github":
        m = re.match(r"^([^/:\s]+)/([^/:\s]+):(.*)$", selector or "")
        if not m:
            return None
        owner, repo, path = m.groups()
        segs = [s for s in path.strip("/").split("/") if s]
        if not (PART.match(owner) and PART.match(repo)) or any(
                not PART.match(s) or s in (".", "..") for s in segs):
            return None
        return {"owner": owner, "repo": repo, "path": "/".join(segs)}
    segs = [s for s in (selector or "").strip("/").split("/") if s]
    if not segs or any(s in (".", "..") or re.search(r"[\x00-\x1f:'\"*?<>|\\]", s) for s in segs):
        return None
    return {"folder": "/".join(segs)}


# -- reaching LOTR ----------------------------------------------------------------------------

class AppClient:
    """The enrolled app client (phase 71). Reads only: every request is `call_read`."""

    def __init__(self, endpoint, app, secret_file, timeout=60):
        self.endpoint, self.app, self.secret_file, self.timeout = endpoint, app, secret_file, timeout

    def read(self, connection, op, args):
        try:
            with open(self.secret_file, encoding="utf-8") as fh:
                secret = fh.read().strip()
        except OSError:
            raise LotrError("no client secret file; run, in your own terminal: lotr app-connect %s "
                            "--vault <vault> --allow <connection> --max-tier read" % self.app)
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "call_read",
                           "arguments": {"connection": connection, "op": op, "args": args}}}
        req = urllib.request.Request(
            self.endpoint, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": "Bearer " + secret, "X-GT-Client": self.app,
                     "Content-Type": "application/json"})
        send = urllib.request.build_opener(urllib.request.ProxyHandler({})).open     # no proxy
        try:
            with send(req, timeout=self.timeout) as r:
                raw = r.read(MAX_REPLY + 1)
        except urllib.error.HTTPError as e:
            raise LotrError("the hub answered HTTP %d" % e.code)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise LotrError("could not call the hub (%s); is lotrd running with listen.mcp_http on?"
                            % type(e).__name__)
        finally:
            del secret
        if len(raw) > MAX_REPLY:
            raise LotrError("the reply is over the size cap")
        try:
            result = json.loads(raw.decode("utf-8")).get("result") or {}
            env = json.loads((result.get("content") or [{}])[0].get("text") or "{}")
        except (ValueError, AttributeError, IndexError):
            raise LotrError("the hub's reply could not be read")
        if not isinstance(env, dict) or env.get("ok") is False or result.get("isError"):
            err = (env.get("error") or {}) if isinstance(env, dict) else {}
            raise LotrError("%s: %s" % (err.get("code", "error"), str(err.get("message", ""))[:200]))
        return env.get("data")


# -- listing and fetching ---------------------------------------------------------------------

def _loc(path, i):
    return path if SAFE_LOC.match(path or "") else "item %d" % i


def list_items(kind, sel, client, connection, limit):
    """-> [{"name", "path", "id", "url", "size"}] files only. The listing is one read."""
    if kind == "github":
        data = client.read(connection, "GET /repos/{owner}/{repo}/contents/{path}", dict(sel))
        entries = data if isinstance(data, list) else [data]
        out = []
        for e in entries:
            if isinstance(e, dict) and e.get("type", "file") == "file":
                out.append({"name": str(e.get("name", "")), "path": str(e.get("path", "")),
                            "url": str(e.get("html_url") or ""), "size": int(e.get("size") or 0),
                            "origin": "github:%s/%s/%s" % (sel["owner"], sel["repo"], e.get("path", ""))})
        return out
    data = client.read(connection, "GET /me/drive/root:/{folder}:/children",
                       {"folder": sel["folder"], "$top": limit + 1,
                        "$select": "id,name,size,file,webUrl,lastModifiedDateTime"})
    entries = (data or {}).get("value", []) if isinstance(data, dict) else []
    return [{"name": str(e.get("name", "")), "path": sel["folder"] + "/" + str(e.get("name", "")),
             "id": str(e.get("id", "")), "url": str(e.get("webUrl") or ""),
             "size": int(e.get("size") or 0), "origin": "onedrive:" + str(e.get("id", ""))}
            for e in entries if isinstance(e, dict) and "file" in e]


def fetch_bytes(kind, sel, item, client, connection):
    if kind == "github":
        data = client.read(connection, "GET /repos/{owner}/{repo}/contents/{path}",
                           dict(sel, path=item["path"]))
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            raise LotrError("the file came back in a shape that is not understood")
        return base64.b64decode(data.get("content", ""), validate=False)
    data = client.read(connection, "GET /me/drive/items/{id}/content", {"id": item["id"]})
    if isinstance(data, dict) and "content_base64" in data:
        return base64.b64decode(data["content_base64"])
    if isinstance(data, dict) and isinstance(data.get("content"), str):
        return data["content"].encode("utf-8")
    raise LotrError("the file came back in a shape that is not understood")


# -- the store --------------------------------------------------------------------------------

def _fm(text, key):
    m = re.search(r"^%s: (.*)$" % re.escape(key), text, re.M)
    if not m:
        return None
    v = m.group(1).strip()
    try:
        return json.loads(v) if v.startswith('"') else v
    except ValueError:
        return v


def known(sources):
    """origin -> (fetched, sidecar-covered original file, sha256) of the newest pulled copy."""
    seen = {}
    try:
        names = sorted(os.listdir(sources))
    except OSError:
        return seen
    for n in names:
        if not n.endswith(" (origin).md"):
            continue
        try:
            with open(os.path.join(sources, n), encoding="utf-8") as fh:
                t = fh.read(4000)
        except OSError:
            continue
        origin, sha = _fm(t, "origin"), _fm(t, "source_sha256")
        if origin and sha:
            cur = (_fm(t, "fetched") or "", n)
            if origin not in seen or cur >= seen[origin][:2]:
                seen[origin] = (cur[0], n, sha, _fm(t, "source_file"))
    return seen


def write_sidecar(sources, stored, item, connection, connector, sha, today, supersedes):
    front = ["---", "title: %s" % json.dumps(os.path.splitext(stored)[0]), "derived: true",
             "origin: %s" % json.dumps(item["origin"]), "origin_url: %s" % json.dumps(item["url"]),
             "connection: %s" % connection, "connector: %s" % connector,
             "source_file: %s" % json.dumps("Sources/" + stored), "source_sha256: %s" % sha,
             "fetched: %s" % today]
    if supersedes:
        front.append("supersedes: %s" % json.dumps(supersedes))
    front += ["---", "", "Pulled by gt_connect.py. The original beside this file is untrusted data.", ""]
    try:
        ibin._create_exclusive(os.path.join(sources, stored + " (origin).md"),
                               "\n".join(front).encode("utf-8"))
    except FileExistsError:
        pass


def _text_of(data):
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def store_item(vault, sources, data, name, today, max_bytes=ibin.MAX_BYTES):
    """-> ("stored"|"held"|"refused", stored name or None, detail). Nothing is written when held
    or refused for a text item; a PDF or image goes through gt_ingest_binary.ingest."""
    if len(data) > max_bytes:
        return "refused", None, "size over the cap"
    kind, label = ibin.detect(data[:16])
    if kind == "other":
        return "refused", None, "not a document (type: %s)" % label
    sha = hashlib.sha256(data).hexdigest()
    if kind == "text":
        text = _text_of(data)
        if text is None:
            return "refused", None, "not text and not a pdf or an image"
        verdict, found = ibin.intake_scan(text)
        if verdict != "clean":
            return "held", None, found or [{"kind": "scan", "rule": "incomplete"}]
        try:
            stored, _new = ibin.store(sources, "%s %s" % (today, name), data, sha)
        except ibin.Refused as exc:
            return "refused", None, str(exc)
        return "stored", stored, None
    with tempfile.TemporaryDirectory(prefix="gt-connect-") as tmp:
        path = os.path.join(tmp, name)
        with open(path, "wb") as fh:
            fh.write(data)
        code, rep = ibin.ingest(path, vault)
    if code == 1 or "source" not in rep:
        return "refused", None, rep.get("reason", "refused")
    stored = rep["source"].split("/", 1)[1]
    if code == 4:
        return "held", stored, rep.get("findings") or [{"kind": "scan", "rule": "incomplete"}]
    return "stored", stored, rep.get("status")


# -- the pull ---------------------------------------------------------------------------------

def _refuse(reason):
    return 1, {"status": "refused", "reason": reason}


def pull(vault, slug, connector, client, limit=None, today=None):
    """-> (exit code, report). The report never holds item text."""
    today = today or datetime.date.today().isoformat()
    if not SLUG.match(slug or ""):
        return _refuse("the project slug is not valid")
    if not NAME.match(connector or ""):
        return _refuse("the connector name is not valid")
    reg = os.path.join(vault, "Projects", slug, "connectors.md")
    try:
        with open(reg, encoding="utf-8") as fh:
            rows, problems = parse_registry(fh.read())
    except OSError:
        return _refuse("no connector registry at Projects/%s/connectors.md" % slug)
    row = next((r for r in rows if r.get("connector") == connector), None)
    if row is None:
        return _refuse("no connector named %s in Projects/%s/connectors.md" % (connector, slug))
    why = check_row(row)
    if why:
        return _refuse(why)
    kind = kind_of(row["connection"])
    sel = parse_selector(kind, row.get("selector", ""))
    if sel is None:
        return _refuse("the selector is not valid for a %s connector" % kind)
    cap = row["limit"] if limit is None else max(1, min(limit, row["limit"]))
    connection = row["connection"]
    try:
        items = list_items(kind, sel, client, connection, cap)
    except (LotrError, ValueError, TypeError) as exc:
        return _refuse("the listing failed: %s" % str(exc)[:200])
    if len(items) > cap:
        return _refuse("the selector matches %d items, over the limit of %d; nothing was fetched "
                       "(narrow the selector or raise the limit in the registry)" % (len(items), cap))
    sources = os.path.join(vault, "Sources")
    os.makedirs(sources, exist_ok=True)
    seen = known(sources)
    rep = {"status": "pulled", "connector": connector, "listed": len(items), "stored": 0,
           "unchanged": 0, "held": [], "refused": []}
    for i, item in enumerate(items, 1):
        loc = _loc(item["path"], i)
        name = ibin.safe_name(item["name"])
        if name is None:
            rep["refused"].append({"location": loc, "reason": "unsafe file name"})
            continue
        try:
            data = fetch_bytes(kind, sel, item, client, connection)
        except (LotrError, ValueError) as exc:
            rep["refused"].append({"location": loc, "reason": "fetch failed: %s" % str(exc)[:120]})
            continue
        sha = hashlib.sha256(data).hexdigest()
        prev = seen.get(item["origin"])
        if prev and prev[2] == sha:
            rep["unchanged"] += 1
            continue
        verdict, stored, detail = store_item(vault, sources, data, name, today)
        if verdict == "refused":
            rep["refused"].append({"location": loc, "reason": detail})
            continue
        if verdict == "held":
            rep["held"].append({"location": loc, "findings": detail})
        if stored:
            write_sidecar(sources, stored, item, connection, connector, sha, today,
                          "Sources/" + prev[3].split("/", 1)[-1] if prev and prev[3] else None)
        if verdict == "stored":
            rep["stored"] += 1
    return 0, rep


# -- command line -----------------------------------------------------------------------------

def _vault(explicit):
    v = explicit or os.environ.get("GT_VAULT")
    if not v:
        try:
            with open(os.path.expanduser("~/.claude/vault-config.json"), encoding="utf-8") as fh:
                v = json.load(fh).get("vault_path")
        except (OSError, ValueError):
            v = None
    return os.path.expanduser(v) if v else None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_connect.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("pull", "list"):
        p = sub.add_parser(n)
        if n == "pull":
            p.add_argument("connector")
            p.add_argument("--limit", type=int)
            p.add_argument("--endpoint", default="http://127.0.0.1:8765/mcp")
            p.add_argument("--app", default="gt-connect")
            p.add_argument("--secret-file")
        p.add_argument("--project", required=True)
        p.add_argument("--vault")
        p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    vault = _vault(a.vault)
    if not vault or not os.path.isdir(vault):
        print("gt_connect: no vault (--vault, $GT_VAULT or vault-config.json)", file=sys.stderr)
        return 2
    if a.cmd == "list":
        try:
            with open(os.path.join(vault, "Projects", a.project, "connectors.md"), encoding="utf-8") as fh:
                rows, _p = parse_registry(fh.read())
        except OSError:
            print("gt_connect: no connectors.md for %s" % a.project, file=sys.stderr)
            return 1
        out = [dict(r, ok=check_row(r) is None, why=check_row(r)) for r in rows]
        print(json.dumps(out, indent=2) if a.json else "\n".join(
            "%s  %s  %s" % (r["connector"], r["connection"], "ok" if r["ok"] else "REFUSED: " + r["why"])
            for r in out))
        return 0
    home = os.environ.get("LOTR_HOME") or os.path.expanduser("~/.config/gt-lotr/personal")
    secret = a.secret_file or os.path.join(home, "clients", a.app + ".secret")
    code, rep = pull(vault, a.project, a.connector, AppClient(a.endpoint, a.app, secret), limit=a.limit)
    if a.json:
        print(json.dumps(rep, indent=2))
    else:
        print("%s%s" % (rep["status"], ": " + rep["reason"] if "reason" in rep else ""))
        for k in ("listed", "stored", "unchanged"):
            if k in rep:
                print("  %s: %s" % (k, rep[k]))
        for k in ("held", "refused"):
            for r in rep.get(k, []):
                print("  %s: %s" % (k, json.dumps(r)))
    return code


if __name__ == "__main__":
    sys.exit(main())
