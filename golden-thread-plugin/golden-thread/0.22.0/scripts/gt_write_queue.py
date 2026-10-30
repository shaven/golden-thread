#!/usr/bin/env python3
"""gt_write_queue.py -- ask for a vault write instead of making it; gt_broker.py applies it.

    gt_write_queue.py --vault V --path REL --op append|replace-section|create|set-property|replace-file
                      (--content TEXT | --content-file F) [--section HEADING]
                      [--session ID] [--origin session|farm] [--hint TEXT] [--dry-run] [--json] [--verbose]
    gt_write_queue.py --vault V --batch FILE [--session ID] [--origin session|farm] [--dry-run]
                      [--json]                       (0.21.0: many files, applied as one unit)

WHY THIS EXISTS. Every session shares one working tree, so two writers to one vault file are a
last-writer-wins overwrite with nothing to show for it (PROTOCOL.md, "Concurrent sessions").
Core rule 1 closes the common case -- a session claims a file before editing it -- but an agent
running outside the claiming session (a farmed result coming back, a batch of subagents all
filing findings) has no claim and no way to wait for one. This tool gives such a writer a place
to put its write: one JSON request file per write in

    <vault>/Projects/golden-thread/spool/queue/<UTC timestamp>-<session>-<random>-<target>.json

and nothing else. It never touches the target. `gt_broker.py drain` applies the queue later, in
timestamp order, after checking claims and conflicts; see that tool for what it decides.

UNDER GT SANDBOX MODE (0.20.1) a command in Claude Code's sandbox cannot write the vault, so the
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

BATCH (0.21.0). `--batch FILE` queues a JSON list of requests across any number of files, each
`{path, op, section?, key?, content | content_file | value, hint?}`. The members share one batch
id; `gt_broker.py drain` checks every member (claims, review targets, hashes, duplicates) and then
writes every member -- or writes none, and names the member that stopped it. Every member is
validated before any is queued, so a refused member queues nothing. At most 50 members.

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
import errno
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import time
from pathlib import Path

SCHEMA = 1
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import gt_vault_manifest as vm                                  # noqa: E402  the layout, one reader

QUEUE_REL = vm.QUEUE_REL                # the built-ins; a vault.manifest.json may add to them (vm.load)
OPS = ("append", "replace-section", "create", "set-property", "replace-file")
ORIGINS = ("session", "farm")
# Origins only the broker stamps (0.20.x review M4): a request picked up from the sandbox inbox is
# re-stamped origin "inbox" (or kept "farm", never loosened) with session INBOX_SESSION, whatever
# its body said -- a writer in Claude's sandbox does not get to name the session it speaks for.
STAMPED_ORIGINS = ("inbox",)
INBOX_SESSION = "unknown-inbox"
# A request id becomes a file name in the queue: one plain component, nothing that could climb out
# of the queue folder (0.20.x review: an inbox body could carry "../../Knowledge/x").
REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
MAX_CONTENT = 256 * 1024
KEYS = {"schema", "id", "submitted", "session", "origin", "path", "op", "section", "content", "key", "target_existed",
        "base_sha256", "hint", "batch", "batch_size"}
# A batch (0.21.0): the members share one `batch` id and each carries `batch_size`, the number of
# members. The broker applies them as one unit and never a partial one.
MAX_BATCH = 50

# Files another tool owns. A write here would be lost (generated files are rebuilt from their
# spool) or would break a rule (Sources are immutable; Core rules are re-asserted every turn).
GENERATED_NAMES = set(vm.GENERATED_NAMES)
REFUSED_EXACT = set(vm.REFUSED_EXACT)
REFUSED_PREFIXES = vm.REFUSED_PREFIXES

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
    try:
        return p if p.is_dir() else None
    except OSError:
        # gt sandbox mode with sandbox_vault_reads deny (0.20.1, B4): Claude's sandbox refuses
        # even a stat of the vault. The vault is named (flag, env or config), so trust the name:
        # deposit() then reaches the inbox fallback instead of dying here in a traceback.
        if _sandbox_on():
            return p
        raise


def queue_dir(vault: Path) -> Path:
    return vault / vm.load(vault).queue_rel


def path_refusal(vault: Path, rel: str) -> str | None:
    """-> why this vault-relative path may not be written through the queue, or None."""
    if not isinstance(rel, str) or not rel.strip():
        return "no path given"
    if "\\" in rel or "\0" in rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        return "the path must be relative to the vault, with forward slashes"
    parts = rel.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        return "the path may not contain empty, `.`, `..` or dot-prefixed segments"
    lay = vm.load(vault)
    if not rel.endswith(".md"):
        return "only Markdown (.md) files are written through the queue"
    # Compared component by component through gt_review_target.fold (0.22.0, phase 84 review M1/M2):
    # the vault disk folds case, Unicode form and (on Windows) trailing dots, spaces and 8.3 short
    # names, so `sources/x.md`, `Sources./x.md`, `SOURCE~1/x.md` and an NFD spelling ARE Sources/x.md.
    # Then the same checks on the path the vault really resolves it to: a link `Srclink -> Sources`
    # or `Knowledge/cr -> ../core-rules` is refused like the folder it leads into.
    try:
        real = vm.resolved_parts(vault, parts)
    except (OSError, ValueError):
        real = None
    if real is None:
        return "the path resolves outside the vault"
    for shape in (parts, real):
        why = _floor_refusal(lay, shape, rel if shape is parts else "/".join(shape))
        if why:
            return why
    return None


def _floor_refusal(lay, parts: list, shown: str) -> str | None:
    """-> why the component list `parts` (spelled `shown`) is one of the refused files or folders."""
    if any(len(parts) == len(vm.split_rel(e)) and vm.under(parts, e)
           for e in REFUSED_EXACT | set(lay.refused_exact)):
        return "%s is generated by its own tool; never written by hand or by the broker" % shown
    name = parts[-1]
    if any(vm.same_name(name, g) for g in GENERATED_NAMES):
        return ("decisions.md is generated from spool/decisions/ -- allocate an ADR with "
                "gt_adr.py instead")
    if any(vm.same_name(name, g) for g in lay.generated_names):
        return "%s is generated by its own tool; never written by hand or by the broker" % name
    for pre in dict.fromkeys(REFUSED_PREFIXES + lay.refused_prefixes):
        if len(parts) > len(vm.split_rel(pre)) and vm.under(parts, pre):
            return "%s is owned by another tool or rule and is never written through the queue" % pre
    for f in vm.FLOOR_DIRS:
        if len(parts) > len(vm.split_rel(f)) and vm.under(parts, f):
            return "%s/ is owned by another tool or rule and is never written through the queue" % f
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


def validate(req: dict, vault: Path, at_submit: bool = True) -> str | None:
    """-> why this request is malformed or not allowed, or None. Used at submit AND drain:
    a request file is data on disk, and anything on disk may have been written by hand.
    at_submit=False (the broker's drain-time check) skips the two checks about the vault as it
    stood when the write was asked for -- the project exists, idea.md is still unwritten -- so a
    project archived after a request was queued is escalated by the broker as a moved target,
    not rejected as never-created, and the brain dump queued into a fresh idea.md still lands."""
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
    if not REQUEST_ID.match(req["id"]) or ".." in req["id"]:
        return "request id must be one plain name (letters, digits, '.', '-', '_')"
    if req["op"] not in OPS:
        return "unknown op %r" % req["op"]
    if req["origin"] not in ORIGINS + STAMPED_ORIGINS:
        return ("unknown origin %r -- a third-party extension has no write path (ADR-8)"
                % req["origin"])
    sec = req.get("section")
    if sec is not None and (not isinstance(sec, str) or not sec.strip() or "\n" in sec):
        return "section must be one non-empty line"
    if sec is not None and sec.lstrip().startswith("#"):
        return ("section is the heading text without `## ` (got %r); pass %r"
                % (sec, sec.lstrip().lstrip("#").strip()))
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
    if (req.get("batch") is None) != (req.get("batch_size") is None):
        return "batch and batch_size go together"
    if req.get("batch") is not None:
        if not isinstance(req["batch"], str) or not REQUEST_ID.match(req["batch"]) \
                or ".." in req["batch"]:
            return "batch must be one plain name (letters, digits, '.', '-', '_')"
        bs = req["batch_size"]
        if isinstance(bs, bool) or not isinstance(bs, int) or not 1 <= bs <= MAX_BATCH:
            return "batch_size must be a whole number from 1 to %d" % MAX_BATCH
    if req.get("target_existed") is not None and not isinstance(req.get("target_existed"), bool):
        return "field target_existed must be true or false"
    for k in ("base_sha256", "hint"):
        if req.get(k) is not None and not isinstance(req.get(k), str):
            return "field %s must be a string" % k
    if not req["content"].strip():
        return "empty content"
    if "\0" in req["content"] or len(req["content"].encode("utf-8")) > MAX_CONTENT:
        return "content is binary or larger than %d bytes" % MAX_CONTENT
    return path_refusal(vault, req["path"]) or (
        (idea_refusal(vault, req["path"], req["op"]) or project_refusal(vault, req["path"]))
        if at_submit else None)


IDEA_SCAFFOLD_LINE = re.compile(r"^\s*(#\s.*|<!--.*-->)\s*$")


def idea_refusal(vault: Path, rel: str, op: str) -> str | None:
    """idea.md is the project's original brain dump, immutable once written (CONVENTIONS.md).
    vault_init writes it as a bare `# Title` scaffold and gt-create / gt-promote fill it with ONE
    append; after that, every write but an identical create is refused (0.20.1). Until 0.20.1 the
    queue took any append to it."""
    if rel.split("/")[-1] != "idea.md" or op == "create":
        return None
    try:
        text = (vault / rel).read_bytes().decode("utf-8", "replace")
    except OSError:
        return None                                     # absent: the append creates it
    if all(IDEA_SCAFFOLD_LINE.match(ln) for ln in text.splitlines() if ln.strip()):
        return None                                     # still the scaffold: this fills it
    return ("%s is immutable once written (the project's original brain dump); put new thinking "
            "in research.md or design.md instead" % rel)


def project_refusal(vault: Path, rel: str) -> str | None:
    """A write into Projects/<slug>/ needs the project to exist. Until 0.20.1 an append to a
    mistyped or not-yet-created project made an orphan folder with no README, which no tool
    lists; now a write that would CREATE Projects/<slug>/ is refused with the command that makes
    the project properly. A folder that is already there is written to as before (an older
    project without a README is gt-lint's to report, not a reason to refuse). Files directly
    under Projects/ (INFRASTRUCTURE.md) are not in a project and are not checked."""
    parts = rel.split("/")
    if len(parts) < 3 or parts[0] != "Projects":
        return None
    if (vault / "Projects" / parts[1]).is_dir():
        return None
    return ("project %s does not exist (no Projects/%s/) -- create it first: "
            "python3 \"%s\" create-project --vault \"%s\" --name %s  (or /gt:gt-create project %s)"
            % (parts[1], parts[1], _vault_init(), vault, parts[1], parts[1]))


def _vault_init() -> str:
    """The vault_init.py to name in a refusal: beside this file (the plugin's scripts/), else the
    newest one in the plugin cache (this file also runs from ~/.claude/golden-thread/hooks/,
    where vault_init.py is not installed)."""
    here = Path(__file__).resolve().parent / "vault_init.py"
    if here.is_file():
        return str(here)
    def ver(p):
        try:
            return tuple(int(x) for x in p.parent.parent.name.split("."))
        except ValueError:
            return ()
    cands = list((Path.home() / ".claude" / "plugins" / "cache").glob("*/gt/*/scripts/vault_init.py"))
    return str(max(cands, key=ver)) if cands else "<gt scripts>/vault_init.py"


_LAST_STAMP = None


def _request_id(stamp: str, session: str, rel: str) -> str:
    return "%s-%s-%s-%s" % (stamp, _slug(session, 8), secrets.token_hex(4), _slug(rel, 60))


def _fresh_id(req: dict) -> str:
    """A new random part for a request whose name was taken: same stamp, session and target,
    so the queue order (submitted, then name) is unchanged."""
    m = re.match(r"^(\d{8}T\d{6}\.\d{6}Z)-", req["id"])
    stamp = m.group(1) if m else req["id"][:23]
    return _request_id(stamp, req.get("session") or "x", req.get("path") or "x")


def build(vault: Path, rel: str, op: str, content: str, section: str | None, session: str,
          origin: str, hint: str | None, now: datetime.datetime | None = None,
          key: str | None = None) -> dict:
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
        # Strictly increasing within one process (0.20.1). The broker orders by `submitted`,
        # then file name; Windows' clock ticks coarsely enough that one `mark` queued three
        # writes in the same microsecond, the ids collided, deposit() suffixed -2/-3, and
        # "x-2.json" < "x-3.json" < "x.json" applied them out of order.
        global _LAST_STAMP
        if _LAST_STAMP is not None and now <= _LAST_STAMP:
            now = _LAST_STAMP + datetime.timedelta(microseconds=1)
        _LAST_STAMP = now
    stamp = now.strftime("%Y%m%dT%H%M%S.%fZ")
    return {"schema": SCHEMA,
            # The random part (B2, 0.20.1): two writers in ONE session -- parallel stage agents
            # share its id -- can read the same clock tick on Windows, and then the same id.
            "id": _request_id(stamp, session, rel),
            "submitted": now.isoformat(timespec="microseconds"),
            "session": session, "origin": origin, "path": rel, "op": op,
            "section": section, "content": content, "key": key,
            # Did the target exist when this was queued? If it is gone at drain time it was moved
            # or deleted meanwhile, and the broker escalates rather than recreate it at the old
            # path (owner question, 2026-10-01).
            "target_existed": _is_file(vault / rel),
            "base_sha256": (current_base(vault, rel, section) if op in ("replace-section",
                                                                         "replace-file")
                            else current_base(vault, rel, None, key) if op == "set-property"
                            else None),
            "hint": hint}


def new_batch_id(session: str) -> str:
    """One name for a batch: UTC stamp, the session, a random part."""
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return "b-%s-%s-%s" % (stamp, _slug(session, 8), secrets.token_hex(3))


def stamp_batch(reqs: list, batch_id: str) -> list:
    for r in reqs:
        r["batch"], r["batch_size"] = batch_id, len(reqs)
    return reqs


def deposit_batch(vault: Path, reqs: list) -> list:
    """Deposit every member of a batch, or none: if one deposit fails, those already landed are
    removed (best effort) and the error is raised. -> the deposited paths, in order."""
    done = []
    try:
        for r in reqs:
            done.append(deposit(vault, r))
    except BaseException:
        for d in done:
            try:
                retry_os(os.unlink, d, seconds=1.0)
            except OSError:
                pass
        raise
    return done


def _is_file(p: Path):
    """True / False, or None when the OS will not say (a read-denied vault under gt sandbox
    mode): the broker then skips the moved-or-deleted check, as for a request queued before
    the field existed."""
    try:
        return p.is_file()
    except OSError:
        return None


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
    """gt sandbox mode's queue inbox (~/.gt-inbox/queue): where a sandboxed command leaves its
    requests (its other writable place is the stage agents' ~/.gt-scratch). Mirrors gt_sandbox.inbox_queue_dir, so this file needs nothing else to find it."""
    return Path(os.path.expanduser("~")) / ".gt-inbox" / "queue"


def in_inbox(path: Path) -> bool:
    try:
        root = os.path.realpath(str(inbox_dir()))
        return os.path.commonpath([os.path.realpath(str(path)), root]) == root
    except ValueError:
        return False


def deposit_inbox(vault: Path, req: dict) -> Path:
    """Leave the request in the queue inbox for the broker (0.20.1, gt sandbox mode). The inbox
    is outside the vault and writable by anything in Claude's sandbox, so its files are DATA:
    the broker re-validates each one against the vault it names before queuing it, exactly as
    it re-validates queued requests at drain time."""
    q = inbox_dir()
    q.mkdir(parents=True, exist_ok=True)
    return _land(q, req, lambda: {"gt_inbox": 1, "vault": os.path.realpath(str(vault)),
                                  "request": req})


def deposit(vault: Path, req: dict) -> Path:
    """Write the request atomically. The temp name starts with `.` and does not end `.json`,
    so a concurrent drain never sees a half-written request.

    Under gt sandbox mode (0.20.1) a command in Claude's sandbox cannot write the vault, so the
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
    return _land(q, req, lambda: req)


# Windows (B2, 0.20.1): a file another process has open cannot be replaced or deleted for a
# moment -- PermissionError [WinError 5] -- where POSIX swaps the inode under the reader. Every
# such step is retried with backoff for about RETRY_SECONDS, then reported in one line.
RETRY_SECONDS = 5.0
# A reserved name is an empty file for the instant between reserving it and the request landing
# over it. A reader skips an empty request younger than this; an older one is a crashed writer's.
RESERVE_GRACE = 120


# macOS (0.21.1): a rename or unlink in spool/queue/ has failed EPERM for a moment and then
# succeeded (2026-10-11; orphans from 10-02 and 10-03 show it earlier). The cause is not
# reproduced, so the answer is a short bounded retry -- on macOS, for EPERM only, and never under gt
# sandbox mode, whose refusals are EPERM too and must reach the inbox fallback without delay.
EPERM_RETRY_SECONDS = 1.0


def _transient(exc: PermissionError) -> float:
    """-> how long this PermissionError may be retried for; 0 means it is a real refusal."""
    if os.name == "nt":
        return RETRY_SECONDS                     # a sharing violation, which passes
    if sys.platform == "darwin" and exc.errno == errno.EPERM and not _sandbox_on():
        return EPERM_RETRY_SECONDS
    return 0.0


def retry_os(fn, *args, seconds: float | None = None):
    """fn(*args), retried while a PermissionError is transient: native Windows (a sharing
    violation) for `seconds` (default RETRY_SECONDS), macOS EPERM outside gt sandbox mode for
    EPERM_RETRY_SECONDS at most. Anything else -- the gt sandbox, EACCES, a read-only mount -- is a
    real refusal and is raised at once, so the sandbox inbox fallback is not delayed."""
    delay, start = 0.01, time.monotonic()
    while True:
        try:
            return fn(*args)
        except PermissionError as exc:
            window = _transient(exc)
            if window and seconds is not None:
                window = min(window, seconds)
            if not window or time.monotonic() - start >= window:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.25)


ORPHAN_AGE = 86400


def sweep_orphans(vault: Path, older_than: float = ORPHAN_AGE) -> int:
    """Remove `.req.*.tmp` files in the queue older than `older_than` seconds: a writer whose
    cleanup failed left them (see EPERM_RETRY_SECONDS). A fresh one may still be in use and stays;
    request files are never touched. -> how many were removed."""
    q, cut, n = queue_dir(vault), time.time() - older_than, 0
    try:
        names = list(q.iterdir())
    except OSError:
        return 0
    for p in names:
        if p.name.startswith(".req.") and p.name.endswith(".tmp"):
            try:
                if p.stat().st_mtime < cut:
                    p.unlink()
                    n += 1
            except OSError:
                pass
    return n


def _reserve(q: Path, req: dict) -> Path:
    """Create the request's file name EXCLUSIVELY (O_CREAT|O_EXCL) -- never an existing one. A
    taken name gets a fresh random part and is tried again (B2, 0.20.1). Until 0.20.1 this
    checked exists() and then os.replace()d, so two same-session writers in one clock tick wrote
    the same name and the second silently overwrote the first, both printing `queued`."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    for _ in range(50):
        dest = q / (req["id"] + ".json")
        try:
            os.close(retry_os(os.open, str(dest), flags, 0o644))
            return dest
        except FileExistsError:
            req["id"] = _fresh_id(req)
    raise FileExistsError(errno.EEXIST, "no free request name after 50 tries", str(q))


def _land(q: Path, req: dict, body) -> Path:
    """Reserve a unique name, write the request beside it under a dot-name a drain never reads,
    then move it over the (empty, our own) reservation. Any failure removes both, so the caller
    is told `could not queue` and nothing half-made is left; success means the file is there.
    `body()` is called after the name is final, so it carries the final id."""
    dest = _reserve(q, req)
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=".req.", suffix=".tmp", dir=str(q))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(body(), fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        retry_os(os.replace, tmp, str(dest))
        tmp = None
    except BaseException:
        for leftover in (tmp, str(dest)):
            if leftover:
                try:
                    retry_os(os.unlink, leftover, seconds=1.0)
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
           drain: bool = True, batch: bool = False):
    """Queue every write in `writes`, then (by default) run one drain.

    writes: [{"path", "op", "content", "section"?, "key"?, "hint"?}, ...] -- deposited in order,
    and all-or-nothing: if any one is refused, none is queued.

    batch=True (0.21.0): the writes are also ONE UNIT for the broker -- it checks every member,
    then writes every member, or writes none and says which member stopped it. Without it each
    write is decided on its own, as before.

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
    if batch:
        if len(built) > MAX_BATCH:
            return ([{"id": None, "path": None, "op": None, "decision": "refused",
                      "reason": "a batch holds at most %d writes (got %d)"
                                % (MAX_BATCH, len(built))}], None)
        stamp_batch(built, new_batch_id(sid))
        for i, req in enumerate(built):
            why = validate(req, vault)
            if why:
                return ([{"id": None, "path": req["path"], "op": req["op"], "decision": "refused",
                          "reason": "member %d: %s" % (i + 1, why)}], None)
    inboxed = False
    if batch:
        inboxed = any(in_inbox(d) for d in deposit_batch(vault, built))
    else:
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
    missing = [r["id"] for r in results if r["id"] not in rows]
    if missing:
        rows.update(logged(vault, missing))
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
    rows = {r.get("request"): r for r in data.get("results", [])}
    return rows, None


def logged(vault: Path, ids) -> dict:
    """{request id: its broker log row} for those of `ids` a drain has already decided -- one
    that ran between this caller's deposit and its own drain decides them first (0.20.1, M4)."""
    want, out = set(ids), {}
    if not want:
        return out
    logs = sorted((vault / "Projects" / "golden-thread" / "spool" / "broker").glob("log-*.jsonl"))
    for f in logs[-2:]:
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("request") in want:
                out[row["request"]] = row
    return out


def drain_hint(vault: Path) -> str:
    return "it is applied by the next `gt_broker.py drain --vault %s`" % vault


# -------------------------------------------------------------------------- CLI ----

def _batch_items(a):
    """-> (items, error). The --batch file: a JSON list of request objects."""
    try:
        raw = sys.stdin.read() if a.batch == "-" else Path(a.batch).read_text(encoding="utf-8")
        items = json.loads(raw)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return None, "could not read %s as JSON: %s" % (a.batch, exc)
    if not isinstance(items, list) or not items:
        return None, "%s must hold a non-empty JSON list of requests" % a.batch
    if len(items) > MAX_BATCH:
        return None, "a batch holds at most %d requests (got %d)" % (MAX_BATCH, len(items))
    return items, None


def _token_refusal(target: str, args_sha256: str) -> bool:
    """Single-use token (item #32): paranoid.action_tokens.vault_write. True (after saying why) when
    the setting is on and GT_ACTION_TOKEN does not hold a valid token for exactly this write. The
    same target and fingerprint the gt-vault MCP tool vault_queue_write asks for, so one token
    works on either route and only once. Nothing changes while the setting is off."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import gt_action_token as AT
    try:
        AT.check("vault_write", AT.token_from_env(), target, args_sha256)
    except AT.TokenError as e:
        print("gt_write_queue: refused -- vault_write needs a single-use token", file=sys.stderr)
        print(AT.refusal_text(e, via_env=True).rstrip("\n"), file=sys.stderr)
        print("  the fingerprint to bind: gt_action_token.py fingerprint --op ... --content-file ...",
              file=sys.stderr)
        return True
    return False


def _args_hash_of(obj) -> str:
    import gt_action_token as AT
    return AT.args_hash(obj)


def _batch_cli(a, vault: Path) -> int:
    """gt_write_queue.py --batch FILE: build and validate EVERY member first, then queue them all
    (or, with --dry-run, none). A refused member refuses the batch and is named by its number."""
    items, err = _batch_items(a)
    if err:
        print("gt_write_queue: %s" % err, file=sys.stderr)
        return 2
    sid = session_id(a.session)
    built = []
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            print("gt_write_queue: refused -- member %d is not a JSON object" % i, file=sys.stderr)
            return 1
        unknown = set(it) - {"path", "op", "section", "key", "content", "content_file", "value",
                             "hint"}
        if unknown:
            print("gt_write_queue: refused -- member %d has unknown field(s): %s"
                  % (i, ", ".join(sorted(unknown))), file=sys.stderr)
            return 1
        if sum(k in it for k in ("content", "content_file", "value")) != 1:
            print("gt_write_queue: refused -- member %d needs exactly one of content, "
                  "content_file, value" % i, file=sys.stderr)
            return 1
        if "value" in it and it.get("op") != "set-property":
            print("gt_write_queue: refused -- member %d: value goes with op set-property" % i,
                  file=sys.stderr)
            return 1
        if "content_file" in it:
            try:
                content = Path(it["content_file"]).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError, TypeError) as exc:
                print("gt_write_queue: refused -- member %d: could not read content_file: %s"
                      % (i, exc), file=sys.stderr)
                return 1
        else:
            content = it.get("content", it.get("value"))
        sec, hint = it.get("section"), it.get("hint")
        if not all(isinstance(x, str) for x in (it.get("path"), it.get("op"), content)) \
                or not all(x is None or isinstance(x, str) for x in (sec, hint, it.get("key"))):
            print("gt_write_queue: refused -- member %d: path, op, content, section, key and hint "
                  "must be strings" % i, file=sys.stderr)
            return 1
        req = build(vault, it["path"].strip(), it["op"], content, sec.strip() if sec else None,
                    sid, a.origin, " ".join(hint.split()) if hint else None, key=it.get("key"))
        why = validate(req, vault)
        if not why and it["op"] == "set-property":
            try:
                cur_text = (vault / req["path"]).read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError):
                cur_text = None
            if frontmatter_is_block(cur_text, it.get("key")):
                why = "%s holds a YAML block; use replace-file" % it.get("key")
        if why:
            print("gt_write_queue: refused -- member %d (%s): %s; nothing was queued"
                  % (i, it["path"], why), file=sys.stderr)
            return 1
        built.append(req)
    stamp_batch(built, new_batch_id(sid))
    for r in built:                                            # the batch fields must validate too
        why = validate(r, vault)
        if why:
            print("gt_write_queue: refused -- %s" % why, file=sys.stderr)
            return 1
    if a.dry_run:
        if a.json:
            print(json.dumps(built, indent=1))
        else:
            for i, r in enumerate(built, 1):
                print("would queue %s -> %s (member %d of %d, batch %s)" % (
                    r["op"], (queue_dir(vault) / (r["id"] + ".json")).relative_to(vault).as_posix(),
                    i, len(built), r["batch"]))
        return 0
    if _token_refusal("vault_queue_write:batch", _args_hash_of(items)):
        return 1
    try:
        dests = deposit_batch(vault, built)
    except OSError as exc:
        print("gt_write_queue: could not queue the batch (none of it was queued): %s" % exc,
              file=sys.stderr)
        return 1
    inboxed = any(in_inbox(d) for d in dests)
    if a.json:
        out = {"queued": [str(d) for d in dests], "ids": [r["id"] for r in built],
               "batch": built[0]["batch"]}
        if inboxed:
            out.update({"inbox": True, "note": INBOX_NOTE})
        print(json.dumps(out))
    else:
        print("queued batch %s: %d request(s), applied together by the next drain"
              % (built[0]["batch"], len(built)))
        for r, d in zip(built, dests):
            print("  %s %s" % (r["op"], r["path"]))
        if inboxed:
            print("  " + INBOX_NOTE)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gt_write_queue.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--vault", help="the vault (default: $GT_VAULT, else vault-config.json)")
    ap.add_argument("--path", help="target file, relative to the vault")
    ap.add_argument("--op", choices=OPS)
    ap.add_argument("--batch", metavar="FILE",
                    help="queue a BATCH: FILE is a JSON list of requests ({path, op, section?, key?, "
                         "content | content_file | value, hint?}), applied by the broker wholly "
                         "or not at all (`-` = stdin; at most %d)" % MAX_BATCH)
    src = ap.add_mutually_exclusive_group()
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
    ap.add_argument("--verbose", action="store_true",
                    help="also print the queue file the request was written to")
    a = ap.parse_args(argv)

    vault = find_vault(a.vault)
    if vault is None:
        print("gt_write_queue: no vault found; pass --vault", file=sys.stderr)
        return 2
    if a.batch:
        if a.path or a.op or a.content is not None or a.content_file or a.value is not None \
                or a.section or a.key:
            print("gt_write_queue: --batch takes its requests from the file; give no --path, "
                  "--op, --content, --section or --key with it", file=sys.stderr)
            return 2
        return _batch_cli(a, vault)
    if not a.path or not a.op or (a.content is None and not a.content_file and a.value is None):
        ap.error("--path, --op and one of --content, --content-file, --value are required "
                 "(or --batch FILE)")
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
    single = {"op": a.op, "content": content}
    if a.section:
        single["section"] = a.section
    if a.key:
        single["key"] = a.key
    if _token_refusal("vault_queue_write:%s" % a.path.strip(), _args_hash_of(single)):
        return 1
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
        where = (" § " + req["section"]) if req["section"] else (" :: " + req["key"]) if req.get("key") else ""
        line = "queued %s %s%s" % (a.op, req["path"], where)
        if a.verbose or inboxed:                   # 0.21.4: the queue file is only named on request
            line += " -> %s" % (dest if inboxed else dest.relative_to(vault).as_posix())
        print(line)
        if inboxed:
            print("  " + INBOX_NOTE)
    return 0


if __name__ == "__main__":
    # A refusal from the OS -- gt sandbox mode, or a macOS interpreter refusal -- is one line
    # naming the next step, not a traceback (0.20.1, B4). gt_errors sits beside this file.
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import gt_errors as _gte
    except ImportError:
        _gte = None
    raise SystemExit(_gte.run(main, "gt_write_queue", mcp="vault_queue_write") if _gte else main())
