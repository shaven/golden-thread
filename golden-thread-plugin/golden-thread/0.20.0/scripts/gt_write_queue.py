#!/usr/bin/env python3
"""gt_write_queue.py -- ask for a vault write instead of making it; gt_broker.py applies it.

    gt_write_queue.py --vault V --path REL --op append|replace-section|create|set-property|replace-file
                      (--content TEXT | --content-file F) [--section HEADING]
                      [--session ID] [--origin session|farm] [--hint TEXT] [--dry-run] [--json]

WHY THIS EXISTS. Every session shares one working tree, so two writers to one vault file are a
last-writer-wins overwrite with nothing to show for it (PROTOCOL.md, "Concurrent sessions").
Core rule 1 closes the common case -- a session claims a file before editing it -- but an agent
running outside the claiming session (a farmed result coming back, a batch of subagents all
filing findings) has no claim and no way to wait for one. This tool gives such a writer a place
to put its write: one JSON request file per write in

    <vault>/Projects/golden-thread/spool/queue/<UTC timestamp>-<session>-<target>.json

and nothing else. It never touches the target. `gt_broker.py drain` applies the queue later, in
timestamp order, after checking claims and conflicts; see that tool for what it decides.

UNDER GT SANDBOX MODE (0.20.0) a command in Claude Code's sandbox cannot write the vault, so the
queue folder refuses it; with `sandbox_mode on` the request goes instead to the queue inbox,
`~/.gt-inbox/queue/<id>.json`, wrapped with the vault it is for. The next drain outside the
sandbox (gt's vault MCP server, or `gt_broker.py drain` in a terminal) moves it into the queue
after the same validation. Outside the sandbox nothing changes.

A session that holds its claim and writes directly is unaffected. The queue is additive.

OPERATIONS (a section is a level-2 heading, `## <heading>`, matched case-insensitively):

    append            add CONTENT at the end of SECTION (or of the file, with no --section);
                      a missing section is created at the end of the file
    replace-section   replace SECTION's body with CONTENT. The section's current hash is
                      recorded now, so the broker can tell if anyone changed it in between
    create            a new file; refused at drain time if the file exists with other content
    replace-file      replace the WHOLE file with CONTENT (0.17.11 -- for a tool that owns
                      generated blocks and rewrites them on every run, e.g. gt_daily). The
                      file's current hash is recorded now; the broker writes only if the
                      file is unchanged since, and escalates otherwise, so an edit made in
                      between is never overwritten. An absent file behaves as create
    set-property      set ONE frontmatter key (--key K --value V, no --section, no content
                      file) in an existing file. The key's current value is recorded now, so
                      the broker escalates rather than overwrite a value changed in between
                      (0.17.11 -- README `stage:`, a handoff's `status:`)

WHAT IT REFUSES, here and again at drain time: a path outside the vault, a path with a `..` or
a dot-segment, anything but a `.md` file, and the files other tools own -- `log.md`,
`TASKS.md`, every `decisions.md` (all generated), `Sources/` (immutable), the Core rules, and
gt's own `sessions/`, `spool/` and `tools/`. `--origin extension` does not exist: under ADR-8
and design-write-broker.md v1 a third-party extension has no write path at all.

Exit: 0 queued (or would be, with --dry-run) | 1 refused | 2 usage.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

SCHEMA = 1
QUEUE_REL = Path("Projects") / "golden-thread" / "spool" / "queue"
OPS = ("append", "replace-section", "create", "set-property", "replace-file")
ORIGINS = ("session", "farm")
MAX_CONTENT = 256 * 1024
KEYS = {"schema", "id", "submitted", "session", "origin", "path", "op", "section", "content", "key", "target_existed",
        "base_sha256", "hint"}

# Files another tool owns. A write here would be lost (generated files are rebuilt from their
# spool) or would break a rule (Sources are immutable; Core rules are re-asserted every turn).
GENERATED_NAMES = {"decisions.md"}
REFUSED_EXACT = {"log.md", "TASKS.md"}
REFUSED_PREFIXES = ("Sources/", "core-rules/", "Projects/golden-thread/core-rules/",
                    "Projects/golden-thread/sessions/", "Projects/golden-thread/spool/",
                    "Projects/golden-thread/tools/")

INBOX_NOTE = ("queued in the sandbox inbox (gt sandbox mode: this shell cannot write the vault); "
              "the broker applies it at the next drain outside the sandbox -- the vault MCP's "
              "vault_queue_drain or vault_queue_write tool, or `gt_broker.py drain` in a terminal")

HEADING = re.compile(r"^##\s+(.+?)\s*#*\s*$")
ANY_TOP = re.compile(r"^#{1,2}\s")
FENCE = re.compile(r"^\s*(```|~~~)")


# ---------------------------------------------------------------- vault + paths ----

def find_vault(explicit: str | None) -> Path | None:
    """--vault, then $GT_VAULT, then vault_path in ~/.claude/vault-config.json."""
    cand = explicit or os.environ.get("GT_VAULT")
    if not cand:
        try:
            cand = json.loads((Path.home() / ".claude" / "vault-config.json")
                              .read_text(encoding="utf-8")).get("vault_path")
        except Exception:
            cand = None
    if not cand:
        return None
    p = Path(cand).expanduser()
    return p if p.is_dir() else None


def queue_dir(vault: Path) -> Path:
    return vault / QUEUE_REL


def path_refusal(vault: Path, rel: str) -> str | None:
    """-> why this vault-relative path may not be written through the queue, or None."""
    if not isinstance(rel, str) or not rel.strip():
        return "no path given"
    if "\\" in rel or "\0" in rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        return "the path must be relative to the vault, with forward slashes"
    parts = rel.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        return "the path may not contain empty, `.`, `..` or dot-prefixed segments"
    if not rel.endswith(".md"):
        return "only Markdown (.md) files are written through the queue"
    if rel in REFUSED_EXACT:
        return "%s is generated by its own tool; never written by hand or by the broker" % rel
    if parts[-1] in GENERATED_NAMES:
        return ("decisions.md is generated from spool/decisions/ -- allocate an ADR with "
                "gt_adr.py instead")
    for pre in REFUSED_PREFIXES:
        if rel.startswith(pre):
            return "%s is owned by another tool or rule and is never written through the queue" % pre
    try:
        root = os.path.realpath(vault)
        real = os.path.realpath(os.path.join(root, *parts))
        if os.path.commonpath([root, real]) != root:
            return "the path resolves outside the vault"
    except ValueError:
        return "the path resolves outside the vault"
    return None


# ---------------------------------------------------------------------- sections ----

def split_lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").split("\n") if text else []


def find_section(lines: list[str], section: str):
    """-> (heading index, end index exclusive) of the first `## section`, or None. Headings
    inside fenced code blocks are not headings."""
    want = section.strip().lower()
    fenced, start = False, None
    for i, line in enumerate(lines):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        if start is not None and ANY_TOP.match(line):
            return start, i
        m = HEADING.match(line)
        if start is None and m and m.group(1).strip().lower() == want:
            start = i
    return (start, len(lines)) if start is not None else None


def section_body(lines: list[str], section: str) -> str | None:
    rng = find_section(lines, section)
    if rng is None:
        return None
    return "\n".join(lines[rng[0] + 1:rng[1]]).strip("\n")


def sha(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


PROPERTY_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def frontmatter_get(text: str | None, key: str):
    """-> (has frontmatter, current value of `key` or None). Top-level `key: value` lines only."""
    if not text or not text.startswith("---"):
        return False, None
    end = text.find("\n---", 3)
    if end == -1:
        return False, None
    for line in text[3:end].split("\n"):
        m = re.match(r"^%s\s*:\s?(.*)$" % re.escape(key), line)
        if m:
            return True, m.group(1).rstrip()
    return True, None


def frontmatter_is_block(text: str | None, key: str) -> bool:
    """True when `key` holds a YAML block (`key:` with indented `- item` / mapping lines under
    it). set-property replaces ONE line, so on a block it would leave the old items behind and
    break the frontmatter -- such a key is rewritten with replace-file instead."""
    if not text or not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    if end == -1:
        return False
    lines = text[3:end].split("\n")
    for i, line in enumerate(lines):
        m = re.match(r"^%s\s*:\s*(.*)$" % re.escape(key), line)
        if m:
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            return m.group(1).strip() == "" and bool(re.match(r"^\s+\S", nxt))
    return False


def frontmatter_set(text: str, key: str, value: str) -> str:
    """`text` with `key: value` set in its frontmatter (replaced in place, or added last)."""
    end = text.find("\n---", 3)
    head, rest = text[3:end], text[end:]
    lines = head.split("\n")
    for i, line in enumerate(lines):
        if re.match(r"^%s\s*:" % re.escape(key), line):
            lines[i] = "%s: %s" % (key, value)
            break
    else:
        if lines and lines[-1] == "":
            lines.insert(len(lines) - 1, "%s: %s" % (key, value))
        else:
            lines.append("%s: %s" % (key, value))
    return "---" + "\n".join(lines) + rest


def current_base(vault: Path, rel: str, section: str | None, key: str | None = None) -> str | None:
    """Hash of what replace-section would replace, as it stands now. None = absent."""
    try:
        # Raw bytes, decoded without newline translation: the broker hashes exactly this, and
        # read_text() would turn CRLF into LF, so every base on a CRLF file would mismatch and
        # every replace would escalate (found by the gt_daily routing, 2026-10-01).
        text = (vault / rel).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if key:
        return sha(frontmatter_get(text, key)[1])
    return sha(section_body(split_lines(text), section)) if section else sha(text)


# ---------------------------------------------------------------------- requests ----

def session_id(explicit: str | None) -> str:
    if explicit:
        return explicit.strip()
    for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "GT_SESSION_ID"):
        if os.environ.get(var):
            return os.environ[var].strip()
    return "anonymous"


def _slug(text: str, n: int) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return (s[:n].strip("-") or "x")


def validate(req: dict, vault: Path) -> str | None:
    """-> why this request is malformed or not allowed, or None. Used at submit AND drain:
    a request file is data on disk, and anything on disk may have been written by hand."""
    if not isinstance(req, dict):
        return "not a JSON object"
    extra = set(req) - KEYS
    if extra:
        return "unknown field(s): %s" % ", ".join(sorted(extra))
    if req.get("schema") != SCHEMA:
        return "unsupported schema %r" % req.get("schema")
    for k in ("id", "submitted", "session", "origin", "path", "op", "content"):
        if not isinstance(req.get(k), str):
            return "field %s missing or not a string" % k
    if req["op"] not in OPS:
        return "unknown op %r" % req["op"]
    if req["origin"] not in ORIGINS:
        return ("unknown origin %r -- a third-party extension has no write path (ADR-8)"
                % req["origin"])
    sec = req.get("section")
    if sec is not None and (not isinstance(sec, str) or not sec.strip() or "\n" in sec):
        return "section must be one non-empty line"
    if req["op"] == "replace-section" and sec is None:
        return "replace-section needs a section"
    if req["op"] in ("create", "replace-file") and sec is not None:
        return "%s takes no section" % req["op"]
    key = req.get("key")
    if req["op"] == "set-property":
        if sec is not None:
            return "set-property takes no section"
        if not isinstance(key, str) or not PROPERTY_KEY.match(key):
            return "set-property needs --key: one frontmatter key (letters, digits, - and _)"
        if "\n" in req["content"]:
            return "a property value is one line"
    elif key is not None:
        return "only set-property takes a key"
    if req.get("target_existed") is not None and not isinstance(req.get("target_existed"), bool):
        return "field target_existed must be true or false"
    for k in ("base_sha256", "hint"):
        if req.get(k) is not None and not isinstance(req.get(k), str):
            return "field %s must be a string" % k
    if not req["content"].strip():
        return "empty content"
    if "\0" in req["content"] or len(req["content"].encode("utf-8")) > MAX_CONTENT:
        return "content is binary or larger than %d bytes" % MAX_CONTENT
    return path_refusal(vault, req["path"])


_LAST_STAMP = None


def build(vault: Path, rel: str, op: str, content: str, section: str | None, session: str,
          origin: str, hint: str | None, now: datetime.datetime | None = None,
          key: str | None = None) -> dict:
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
        # Strictly increasing within one process (0.20.0). The broker orders by `submitted`,
        # then file name; Windows' clock ticks coarsely enough that one `mark` queued three
        # writes in the same microsecond, the ids collided, deposit() suffixed -2/-3, and
        # "x-2.json" < "x-3.json" < "x.json" applied them out of order.
        global _LAST_STAMP
        if _LAST_STAMP is not None and now <= _LAST_STAMP:
            now = _LAST_STAMP + datetime.timedelta(microseconds=1)
        _LAST_STAMP = now
    stamp = now.strftime("%Y%m%dT%H%M%S.%fZ")
    return {"schema": SCHEMA,
            "id": "%s-%s-%s" % (stamp, _slug(session, 8), _slug(rel, 60)),
            "submitted": now.isoformat(timespec="microseconds"),
            "session": session, "origin": origin, "path": rel, "op": op,
            "section": section, "content": content, "key": key,
            # Did the target exist when this was queued? If it is gone at drain time it was moved
            # or deleted meanwhile, and the broker escalates rather than recreate it at the old
            # path (owner question, 2026-10-01).
            "target_existed": (vault / rel).is_file(),
            "base_sha256": (current_base(vault, rel, section) if op in ("replace-section",
                                                                         "replace-file")
                            else current_base(vault, rel, None, key) if op == "set-property"
                            else None),
            "hint": hint}


def _sandbox_on() -> bool:
    try:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import gt_sandbox                                      # noqa: PLC0415
        return gt_sandbox.is_on()
    except Exception:                                          # noqa: BLE001
        return False


def inbox_dir() -> Path:
    """gt sandbox mode's queue inbox (~/.gt-inbox/queue): the one place a sandboxed command may
    write. Mirrors gt_sandbox.inbox_queue_dir, so this file needs nothing else to find it."""
    return Path(os.path.expanduser("~")) / ".gt-inbox" / "queue"


def in_inbox(path: Path) -> bool:
    try:
        root = os.path.realpath(str(inbox_dir()))
        return os.path.commonpath([os.path.realpath(str(path)), root]) == root
    except ValueError:
        return False


def deposit_inbox(vault: Path, req: dict) -> Path:
    """Leave the request in the queue inbox for the broker (0.20.0, gt sandbox mode). The inbox
    is outside the vault and writable by anything in Claude's sandbox, so its files are DATA:
    the broker re-validates each one against the vault it names before queuing it, exactly as
    it re-validates queued requests at drain time."""
    q = inbox_dir()
    q.mkdir(parents=True, exist_ok=True)
    body = {"gt_inbox": 1, "vault": os.path.realpath(str(vault)), "request": req}
    base, n = req["id"], 1
    while (q / (req["id"] + ".json")).exists():
        n += 1
        req["id"] = "%s-%d" % (base, n)
    fd, tmp = tempfile.mkstemp(prefix=".req.", suffix=".tmp", dir=str(q))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(body, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        dest = q / (req["id"] + ".json")
        os.replace(tmp, dest)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest


def deposit(vault: Path, req: dict) -> Path:
    """Write the request atomically. The temp name starts with `.` and does not end `.json`,
    so a concurrent drain never sees a half-written request.

    Under gt sandbox mode (0.20.0) a command in Claude's sandbox cannot write the vault, so the
    queue folder refuses it (EPERM from Seatbelt, EROFS from bubblewrap). Then -- and only then,
    and only while sandbox_mode is on -- the request goes to the queue inbox instead
    (deposit_inbox). Outside the sandbox nothing changes: the queue is written as before."""
    try:
        return _deposit_queue(vault, req)
    except OSError:
        if not _sandbox_on():
            raise
        return deposit_inbox(vault, req)


def _deposit_queue(vault: Path, req: dict) -> Path:
    q = queue_dir(vault)
    q.mkdir(parents=True, exist_ok=True)
    base, n = req["id"], 1
    while (q / (req["id"] + ".json")).exists():
        n += 1
        req["id"] = "%s-%d" % (base, n)
    fd, tmp = tempfile.mkstemp(prefix=".req.", suffix=".tmp", dir=str(q))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(req, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        dest = q / (req["id"] + ".json")
        os.replace(tmp, dest)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest


def pending(vault: Path) -> list[Path]:
    q = queue_dir(vault)
    try:
        return sorted(p for p in q.iterdir() if p.suffix == ".json" and not p.name.startswith("."))
    except OSError:
        return []


# ------------------------------------------------------ queue, then drain at once ----
#
# 0.17.11: the scripts that used to write vault Markdown directly (gt_handoff, gt_handoff_status,
# gt_lint_weekly) go through here instead. The PreToolUse hook cannot see a write made inside a
# script, so a script that writes directly bypasses the broker -- its claim check, its dedupe, its
# escalation. Draining straight after queuing keeps the caller synchronous whenever nothing holds
# the target; when something does, the request stays queued and the caller says so.

def submit(vault: Path, writes: list, session: str | None = None, origin: str = "session",
           drain: bool = True):
    """Queue every write in `writes`, then (by default) run one drain.

    writes: [{"path", "op", "content", "section"?, "key"?, "hint"?}, ...] -- deposited in order,
    and all-or-nothing: if any one is refused, none is queued.

    -> (results, note). results: one dict per write, in order, with "id", "path", "op" and
    "decision": the broker's (apply | deduplicate | held | escalate | reject), "refused" (not
    queued; "reason" says why), or "queued" (queued, but no drain decided it -- `note` says why).
    Extra keys from the broker's log row ("reason", "conflict", "task") are carried over.
    """
    sid = session_id(session)
    built = []
    for w in writes:
        req = build(vault, w["path"], w["op"], w["content"], w.get("section"), sid, origin,
                    w.get("hint"), key=w.get("key"))
        why = validate(req, vault)
        if why:
            return ([{"id": None, "path": w["path"], "op": w["op"], "decision": "refused",
                      "reason": why}], None)
        built.append(req)
    inboxed = False
    for req in built:
        inboxed = in_inbox(deposit(vault, req)) or inboxed
    results = [{"id": r["id"], "path": r["path"], "op": r["op"], "decision": "queued",
                "reason": "queued; not yet applied"} for r in built]
    if inboxed:
        # Under gt sandbox mode the drain cannot run from here either: it writes the vault.
        return results, INBOX_NOTE
    if not drain:
        return results, "not drained"
    rows, note = drain_now(vault)
    for res in results:
        row = rows.get(res["id"])
        if row:
            res.update({k: v for k, v in row.items() if k not in ("request", "ts")})
    return results, note


def drain_now(vault: Path):
    """Run `gt_broker.py drain --json` from beside this file. -> ({request id: log row}, note);
    note is None when the drain ran, else one line on why it did not."""
    broker = Path(__file__).resolve().parent / "gt_broker.py"
    if not broker.is_file():
        return {}, "gt_broker.py is not beside gt_write_queue.py"
    import subprocess                                         # noqa: PLC0415
    try:
        p = subprocess.run([sys.executable, str(broker), "drain", "--vault", str(vault),
                            "--json"], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return {}, "the drain could not run (%s)" % exc.__class__.__name__
    try:
        data = json.loads(p.stdout)
    except ValueError:
        err = (p.stderr.strip().splitlines() or ["exit %d" % p.returncode])[-1]
        return {}, "the drain did not run: %s" % err
    return {r.get("request"): r for r in data.get("results", [])}, None


def drain_hint(vault: Path) -> str:
    return "it is applied by the next `gt_broker.py drain --vault %s`" % vault


# -------------------------------------------------------------------------- CLI ----

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gt_write_queue.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--vault", help="the vault (default: $GT_VAULT, else vault-config.json)")
    ap.add_argument("--path", required=True, help="target file, relative to the vault")
    ap.add_argument("--op", required=True, choices=OPS)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--content", help="the text to write")
    src.add_argument("--content-file", help="read the text from this file (`-` = stdin)")
    src.add_argument("--value", help="set-property: the new value (one line)")
    ap.add_argument("--section", help="level-2 heading the write targets (without `## `)")
    ap.add_argument("--key", help="set-property: the frontmatter key")
    ap.add_argument("--session", help="originating session id (default: $CLAUDE_CODE_SESSION_ID)")
    ap.add_argument("--origin", default="session", choices=ORIGINS,
                    help="session (a Claude Code session or its agent) or farm (a farmed "
                         "result: only `create` is ever applied automatically)")
    ap.add_argument("--hint", help="one line for the owner if this request ends in a conflict")
    ap.add_argument("--dry-run", action="store_true", help="validate and show; queue nothing")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    vault = find_vault(a.vault)
    if vault is None:
        print("gt_write_queue: no vault found; pass --vault", file=sys.stderr)
        return 2
    if a.op == "set-property" or a.value is not None:
        if a.op != "set-property" or a.value is None or a.content is not None or a.content_file:
            print("gt_write_queue: --value goes with --op set-property (and --key), alone",
                  file=sys.stderr)
            return 2
        content = a.value
    elif a.content is not None:
        content = a.content
    else:
        try:
            content = (sys.stdin.read() if a.content_file == "-"
                       else Path(a.content_file).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            print("gt_write_queue: could not read %s: %s" % (a.content_file, exc), file=sys.stderr)
            return 2
    hint = " ".join(a.hint.split()) if a.hint else None
    req = build(vault, a.path.strip(), a.op, content, a.section.strip() if a.section else None,
                session_id(a.session), a.origin, hint, key=a.key)
    why = validate(req, vault)
    if not why and a.op == "set-property":
        try:
            cur_text = (vault / req["path"]).read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            cur_text = None
        if frontmatter_is_block(cur_text, a.key):
            why = ("%s holds a YAML block (one item per line); set-property would leave the old "
                   "items behind -- rebuild the file and use --op replace-file" % a.key)
    if why:
        print("gt_write_queue: refused -- %s" % why, file=sys.stderr)
        return 1
    if a.dry_run:
        dest = queue_dir(vault) / (req["id"] + ".json")
        print(json.dumps(req, indent=1) if a.json else "would queue %s -> %s" % (
            a.op, dest.relative_to(vault).as_posix()))
        return 0
    try:
        dest = deposit(vault, req)
    except OSError as exc:
        print("gt_write_queue: could not queue: %s" % exc, file=sys.stderr)
        return 1
    inboxed = in_inbox(dest)
    if a.json:
        out = {"queued": str(dest), "id": req["id"]}
        if inboxed:
            out.update({"inbox": True, "note": INBOX_NOTE})
        print(json.dumps(out))
    else:
        print("queued %s %s%s -> %s" % (a.op, req["path"],
                                        (" § " + req["section"]) if req["section"]
                                        else (" :: " + req["key"]) if req.get("key") else "",
                                        dest if inboxed else dest.relative_to(vault).as_posix()))
        if inboxed:
            print("  " + INBOX_NOTE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
