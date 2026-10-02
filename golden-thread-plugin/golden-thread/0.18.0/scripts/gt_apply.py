#!/usr/bin/env python3
"""gt_apply.py -- add-ons PROPOSE fixes; this is the one writer that makes them.

    gt_apply.py list [--all] [--json] [--vault V]
    gt_apply.py show ID [--vault V]
    gt_apply.py apply (ID ... | --all) [--plugin-root R] [--vault V] [--dry-run] [--json]
    gt_apply.py undo ID [--vault V] [--dry-run]

WHY A HOST WRITES AND THE ADD-ON NEVER DOES. A checker (gt_check.py) that finds an unclosed tag
could fix it -- but if every add-on wrote files itself, every add-on would need its own
backups, its own respect for session claims and protected paths, and its own proof the fix
worked, and one careless add-on could overwrite a live session's work or touch core-rules/.
So a checker returns a PROPOSAL -- the file, a unified diff against the exact bytes it
examined, the finding it addresses, a one-line reason -- and gt decides.

THE SETTING `addon_fixes`: off (proposals discarded) | propose (default: kept and shown;
nothing changes until `apply`) | apply (applied after a check run -- FIRST-PARTY checkers
only; anything not shipped in the gt release is capped at propose, and the output says so).

WHAT AN APPLY DOES, in order. Any refusal leaves the file byte-identical and records why.

  1. grant       the checker's module.json `fixes` globs must cover the file, and the file
                 must be one that checker's FINDINGS named
  2. protected   core-rules/, global-memory/, Sources/, .git/, .githooks/, gt's own install
                 and settings (~/.claude) and the plugin source are refused whatever a
                 manifest grants
  3. diff        exactly one existing file, edited in place: a diff that creates, deletes,
                 renames, changes a mode (an executable bit), or makes a symlink is refused;
                 so is a whole-file replacement that is not a diff
  4. base        the file's sha256 must still equal the hash of what the checker examined,
                 and the diff must apply exactly (no fuzz)
  5. content     refused if it adds bidirectional-control or zero-width characters, text
                 matching a credential pattern (the `secrets` slot) or a scrub term (never
                 printed), or changes the size by more than `addon_fix_size_limit`; under a
                 checker's `html` rules also a new <script>, an on-event attribute, or a URL
                 to a host the file did not already name
  6. claims      a vault file claimed by another LIVE session (gt_session.py) is refused,
                 naming that session
  7. re-check    the fixed content is written to a STAGED COPY outside the vault and repo,
                 and EVERY installed checker that applies to the file runs on the original
                 and on the copy: refused if any checker that passed now fails; reported
                 `did-not-fix` if the proposer's own finding is still there
  8. write       under one lock per file the hash is re-read and compared immediately before
                 the rename that installs the new content (compare-and-swap), so two applies
                 racing on one file write at most once. A vault Markdown file goes through the
                 write queue and broker (Core rule 1) instead of being written directly
  9. verify      the proposer runs once more on the written file; if its finding is back the
                 original bytes are restored and the outcome is `did-not-fix`
 10. record      the original bytes are kept (`undo` restores them exactly), the proposal's
                 state is updated, and one `addon.fix` event names the add-on, file and outcome

Applied fixes are left UNCOMMITTED, so they show in `git diff` and the commit gate re-checks
them. Proposals live in <vault>/Projects/golden-thread/ext-proposals/ when a vault is
configured, else ~/.claude/golden-thread/proposals/; original bytes in
~/.claude/golden-thread/proposals/backups/.

Exit: 0 done | 1 refused, did-not-fix, or nothing to do | 2 usage.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import fnmatch
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SCHEMA = 1
STATES = ("pending", "applied", "refused", "did-not-fix", "undone")
EXT_PROPOSALS_REL = Path("Projects") / "golden-thread" / "ext-proposals"
SIZE_LIMITS = {"1k": 1024, "4k": 4096, "16k": 16384, "64k": 65536, "256k": 262144}
# Bidirectional controls (Trojan Source) and zero-width / invisible formatting characters.
INVISIBLE = set(chr(c) for c in (
    list(range(0x202A, 0x202F)) + list(range(0x2066, 0x206A)) +
    [0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0x2061, 0x2062, 0x2063, 0x2064,
     0xFEFF, 0x061C, 0x180E]))
PROTECTED_VAULT_TOP = ("core-rules", "global-memory", "Sources")
PROTECTED_ANYWHERE = (".git", ".githooks", "core-rules")
URL_RE = re.compile(r"""(?i)\b(?:https?|wss?|ftp)://([^/\s"'<>?#:]+)""")
SCRIPT_RE = re.compile(r"(?i)<script\b")
ONEVENT_RE = re.compile(r"""(?i)<[^>]*\son[a-z]+\s*=""")
OK, REFUSED, USAGE = 0, 1, 2


# ------------------------------------------------------------------ plumbing ----

def _load(name, *dirs):
    for d in dirs or (HERE, Path.home() / ".claude" / "golden-thread" / "hooks"):
        p = Path(d) / ("%s.py" % name)
        if p.is_file():
            spec = importlib.util.spec_from_file_location("gt_apply_" + name, str(p))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    return None


def sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def read_bytes(path) -> bytes | None:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def home_dir() -> Path:
    return Path.home() / ".claude" / "golden-thread" / "proposals"


def find_vault(explicit=None):
    wq = _load("gt_write_queue")
    return wq.find_vault(explicit) if wq else None


def store_dir(vault) -> Path:
    return (Path(vault) / EXT_PROPOSALS_REL) if vault else home_dir()


def setting(name, default):
    gs = _load("gt_settings")
    try:
        return gs.get(name) or default if gs else default
    except Exception:
        return default


def _atomic_write(path: Path, data: bytes, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".gt_apply.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def save(rec, vault):
    d = store_dir(vault)
    _atomic_write(d / ("%s.json" % rec["id"]),
                  (json.dumps(rec, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))


def load_all(vault):
    out = []
    for d in dict.fromkeys([store_dir(vault), home_dir()]):
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if not n.endswith(".json") or n.startswith("."):
                continue
            try:
                rec = json.loads((d / n).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict) and rec.get("schema") == SCHEMA and rec.get("id"):
                rec["_store"] = str(d)
                out.append(rec)
    return sorted(out, key=lambda r: r.get("created", ""))


def find(vault, pid):
    hits = [r for r in load_all(vault) if r["id"] == pid or r["id"].startswith(pid)]
    return hits[0] if len(hits) == 1 else None


@contextlib.contextmanager
def file_lock(target):
    """One lock per target file, outside the vault and the repo. The hash check and the
    rename both happen inside it -- checking earlier leaves a race window."""
    d = Path.home() / ".claude" / "golden-thread" / "locks"
    d.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(os.path.realpath(str(target)).encode("utf-8")).hexdigest()[:24]
    fh = open(d / ("apply-%s.lock" % key), "a")
    try:
        try:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX)
        except ImportError:
            pass
        yield
    finally:
        fh.close()


# ------------------------------------------------------------------ the diff ----

class Refusal(Exception):
    def __init__(self, outcome, why):
        super().__init__(why)
        self.outcome, self.why = outcome, why


def _name(s):
    s = s.split("\t")[0].strip()
    if s == "/dev/null":
        return s
    return s[2:] if s.startswith(("a/", "b/")) else s


def parse_diff(text):
    """-> {"old", "new", "hunks": [(old_start, old_len, [(tag, line)])]}; Refusal otherwise.

    Only an in-place edit of ONE existing file is accepted. Every header that could create,
    delete, rename, re-mode or re-type a file is refused by name."""
    if not isinstance(text, str) or not text.strip():
        raise Refusal("refused", "the proposal carries no diff (whole-file replacements are "
                                 "not accepted)")
    lines = text.splitlines(keepends=True)
    if any(re.match(r"^(new|old) mode 120\d{3}\s*$|^new file mode 120", l) for l in lines):
        raise Refusal("refused", "the diff turns the file into a symlink")
    old = new = None
    hunks, i, files = [], 0, 0
    while i < len(lines):
        ln = lines[i]
        bare = ln.rstrip("\r\n")
        if bare.startswith(("old mode", "new mode", "new file mode", "deleted file mode")):
            mode = bare.split()[-1]
            if mode.startswith("120"):
                raise Refusal("refused", "the diff turns the file into a symlink")
            if bare.startswith("new file"):
                raise Refusal("refused", "the diff creates a file")
            if bare.startswith("deleted"):
                raise Refusal("refused", "the diff deletes a file")
            raise Refusal("refused", "the diff changes the file mode (an executable bit)")
        if bare.startswith(("rename from", "rename to", "copy from", "copy to",
                            "similarity index")):
            raise Refusal("refused", "the diff renames or copies a file")
        if bare.startswith(("Binary files", "GIT binary patch")):
            raise Refusal("refused", "the diff is binary")
        if bare.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            files += 1
            if files > 1:
                raise Refusal("refused", "the diff touches more than one file")
            old, new = _name(bare[4:]), _name(lines[i + 1].rstrip("\r\n")[4:])
            if old == "/dev/null":
                raise Refusal("refused", "the diff creates a file")
            if new == "/dev/null":
                raise Refusal("refused", "the diff deletes a file")
            if old != new:
                raise Refusal("refused", "the diff renames %s" % old)
            i += 2
            continue
        m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", bare)
        if m:
            if old is None:
                raise Refusal("refused", "the diff has a hunk before any file header")
            ostart, olen = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            nlen = int(m.group(4) if m.group(4) is not None else 1)
            body, seen_o, seen_n = [], 0, 0
            i += 1
            while i < len(lines) and (seen_o < olen or seen_n < nlen
                                      or lines[i].startswith("\\")):
                h = lines[i]
                if h.startswith("\\"):
                    if body:                         # "\ No newline at end of file"
                        tag, content = body[-1]
                        body[-1] = (tag, content.rstrip("\n").rstrip("\r"))
                    i += 1
                    continue
                tag, content = h[:1], h[1:]
                if tag == "" and h in ("\n", "\r\n"):
                    tag, content = " ", h
                if tag not in (" ", "-", "+"):
                    raise Refusal("refused", "the diff has a malformed hunk line")
                if tag in (" ", "-"):
                    seen_o += 1
                if tag in (" ", "+"):
                    seen_n += 1
                body.append((tag, content))
                i += 1
            if seen_o != olen or seen_n != nlen:
                raise Refusal("refused", "the diff's hunk counts do not match its lines")
            hunks.append((ostart, olen, body))
            continue
        i += 1
    if old is None or not hunks:
        raise Refusal("refused", "the proposal is not a unified diff of one file")
    return {"old": old, "new": new, "hunks": hunks}


def apply_hunks(original: bytes, parsed) -> bytes:
    """Exact application, no fuzz: every context and removed line must match."""
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError:
        raise Refusal("refused", "the file is not UTF-8 text")
    src = text.splitlines(keepends=True)
    out, pos = [], 0
    for ostart, olen, body in parsed["hunks"]:
        start = ostart - 1 if olen else ostart
        if start < pos or start > len(src):
            raise Refusal("refused", "the diff does not apply to the current content")
        out.extend(src[pos:start])
        k = start
        for tag, content in body:
            if tag in (" ", "-"):
                if k >= len(src) or src[k] != content:
                    raise Refusal("refused", "the diff does not apply to the current content")
                k += 1
                if tag == " ":
                    out.append(content)
            else:
                out.append(content)
        pos = k
    out.extend(src[pos:])
    return "".join(out).encode("utf-8")


# ------------------------------------------------------------------ the rules ----

def protected_reason(target, vault, plugin_root):
    t = os.path.realpath(target)

    def under(root):
        r = os.path.realpath(str(root))
        return t == r or t.startswith(r.rstrip(os.sep) + os.sep)

    if under(Path.home() / ".claude"):
        return "gt's own install and settings (~/.claude) are protected"
    if plugin_root and under(plugin_root):
        return "the gt plugin source is protected"
    parts = t.split(os.sep)
    for seg in PROTECTED_ANYWHERE:
        if seg in parts[:-1]:
            return "%s/ is a protected path" % seg
    if vault and under(vault):
        rel = os.path.relpath(t, os.path.realpath(str(vault))).split(os.sep)
        if rel[0] in PROTECTED_VAULT_TOP:
            return "%s/ is a protected path" % rel[0]
    return None


def _count(chars, text):
    return {c: text.count(c) for c in chars if c in text}


def _scrub_terms(vault):
    path = os.environ.get("GT_SCRUB_TERMS")
    if not path and vault:
        path = os.path.join(str(vault), "Projects", "golden-thread", "scrub-terms.txt")
    pats = []
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    try:
                        pats.append(re.compile(line, re.I))
                    except re.error:
                        raise Refusal("refused", "a scrub term does not compile; every fix is "
                                                 "refused until the terms file is fixed")
    return pats


def _secret_counts(text, vault):
    """{rule id: matches} from the `secrets` slot -- ids only, never the matched text."""
    gs = _load("gt_secrets", HERE)
    if gs is None:
        return {}
    rules, _problems, _ret = gs.rules_from_slot(str(vault) if vault else None)
    out = {}
    for r in rules:
        n = len(r["re"].findall(text))
        if n:
            out[r["id"]] = n
    return out


def content_reason(old: bytes, new: bytes, rules, vault, limit):
    """-> why this change must not be written, or None. Never quotes what it matched."""
    if abs(len(new) - len(old)) > limit:
        return ("the fix changes the size by %d bytes; the limit (addon_fix_size_limit) is %d"
                % (abs(len(new) - len(old)), limit))
    o, n = old.decode("utf-8", "replace"), new.decode("utf-8", "replace")
    oc, nc = _count(INVISIBLE, o), _count(INVISIBLE, n)
    for c, k in sorted(nc.items()):
        if k > oc.get(c, 0):
            return ("the fix adds an invisible or bidirectional-control character (U+%04X)"
                    % ord(c))
    for i, pat in enumerate(_scrub_terms(vault), 1):
        if len(pat.findall(n)) > len(pat.findall(o)):
            return "the fix adds text matching scrub term #%d (not shown)" % i
    so, sn = _secret_counts(o, vault), _secret_counts(n, vault)
    for rid, k in sorted(sn.items()):
        if k > so.get(rid, 0):
            return "the fix adds text matching the credential pattern %r (not shown)" % rid
    if "html" in (rules or []):
        if len(SCRIPT_RE.findall(n)) > len(SCRIPT_RE.findall(o)):
            return "html rules: the fix adds a <script> element"
        if len(ONEVENT_RE.findall(n)) > len(ONEVENT_RE.findall(o)):
            return "html rules: the fix adds an inline on-event attribute"
        hosts_o = {h.lower() for h in URL_RE.findall(o)}
        new_hosts = sorted({h.lower() for h in URL_RE.findall(n)} - hosts_o)
        if new_hosts:
            return "html rules: the fix adds a URL to a host the file did not name"
    return None


def claim_reason(target, vault):
    if not vault:
        return None
    t, v = os.path.realpath(target), os.path.realpath(str(vault))
    if not t.startswith(v.rstrip(os.sep) + os.sep):
        return None
    rel = os.path.relpath(t, v).replace(os.sep, "/")
    br = _load("gt_broker", HERE)
    if br is None:
        return "session claims could not be checked (gt_broker.py missing)"
    me = br.my_session() or ""
    holders, err = br.claim_holders(Path(v), rel, me, {me} if me else set())
    if err:
        return "session claims could not be checked: %s" % err
    if holders:
        return "claimed by live session %s" % ", ".join(holders)
    return None


# ------------------------------------------------------------------ events ----

def emit(rec, outcome, vault):
    """One addon.fix event per apply, refusal, rollback or undo; and a home-ledger row
    always, so a run with no vault still leaves a record."""
    row = {"at": now(), "id": rec["id"], "addon": rec.get("addon"), "file": rec.get("file"),
           "outcome": outcome}
    led = home_dir() / "ledger.jsonl"
    try:
        led.parent.mkdir(parents=True, exist_ok=True)
        with open(led, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass
    if not vault:
        return
    ev = _load("gt_events", HERE.parent / "templates" / "tools")
    if ev is None:
        print("gt_apply: addon.fix event NOT recorded (gt_events.py unavailable)",
              file=sys.stderr)
        return
    note = ev.clip("addon %s: %s -> %s" % (rec.get("addon"), os.path.basename(rec.get("file")
                                                                             or "?"), outcome))
    ev.safe_emit(Path(vault), "addon.fix",
                 (EXT_PROPOSALS_REL / ("%s.json" % rec["id"])).as_posix(), note=note)


# ------------------------------------------------------------------ intake ----

def make_record(result, checker, prop, root, staged, plugin_root):
    rel = prop["file"]
    base = result["files"].get(rel)
    finding_files = sorted({f["file"] for f in result.get("findings") or []
                            if f.get("rule") != "gt-check/read-only-violation"})
    target = rel if os.path.isabs(rel) else os.path.join(root, rel)
    pid = hashlib.sha256(json.dumps([checker["key"], os.path.realpath(target), base,
                                     prop["diff"]]).encode("utf-8")).hexdigest()[:12]
    return {"schema": SCHEMA, "id": pid, "created": now(), "addon": checker["module"],
            "checker": checker["key"], "first_party": bool(checker.get("first_party")),
            "root": root, "file": rel, "target": target, "base_sha256": base,
            "diff": prop["diff"], "finding": str(prop.get("finding") or ""),
            "reason": str(prop.get("reason") or "")[:200], "finding_files": finding_files,
            "fixes": list(checker.get("fixes") or []), "rules": list(checker.get("rules") or []),
            "plugin_root": plugin_root, "staged": bool(staged), "state": "pending",
            "outcome": None, "history": []}


def intake(results, by_key, root, staged=False, vault=None, as_json=False, plugin_root=None):
    """Called by gt_check.py after a run. Honours `addon_fixes`. -> [summary dict]."""
    mode = setting("addon_fixes", "propose")
    props = [(r, p) for r in results for p in r.get("proposals") or []]
    out = []
    if mode == "off":
        if not as_json:
            print("%d fix proposal(s) discarded (addon_fixes off)" % len(props))
        return [{"discarded": len(props)}]
    vault = find_vault(vault)
    if plugin_root is None:
        chk = _load("gt_check", HERE)
        plugin_root = chk.plugin_root() if chk else None
    for r, p in props:
        c = by_key.get(r["checker"])
        if c is None:
            continue
        rec = make_record(r, c, p, root, staged, plugin_root)
        existing = find(vault, rec["id"])
        if existing and existing.get("state") != "pending":
            # The same change, proposed again after it was applied, refused or undone: a new
            # proposal with its own id, so the old record's history stands.
            rec["id"] = hashlib.sha256((rec["id"] + now() + str(os.getpid()))
                                       .encode("utf-8")).hexdigest()[:12]
        save(rec, vault)
        line = {"id": rec["id"], "addon": rec["addon"], "file": rec["file"], "state": "pending"}
        if mode == "apply":
            if not rec["first_party"]:
                line["note"] = ("capped to propose: %s is not first-party (not shipped in the gt "
                                "release); apply it yourself with gt_apply.py apply %s"
                                % (rec["checker"], rec["id"]))
            else:
                outcome, why = apply_one(rec, vault)
                line.update(state=outcome, note=why or "")
        out.append(line)
        if not as_json:
            print("fix proposal %s  %s  %s  [%s]%s" % (rec["id"], rec["addon"], rec["file"],
                                                      line["state"],
                                                      ("  -- " + line["note"]) if line.get("note")
                                                      else ""))
            if line["state"] == "pending":
                print("  reason: %s" % rec["reason"])
                for dl in rec["diff"].splitlines()[:60]:
                    print("  | " + dl)
    if props and mode == "propose" and not as_json:
        print("apply with: gt_apply.py apply <id>   (or --all); nothing has been changed")
    return out


# ------------------------------------------------------------------ apply ----

def _recheck(rec, original: bytes, fixed: bytes):
    """Run EVERY installed checker that applies to the file on the original and on a staged
    copy of the fix. -> (ok, why, outcome)."""
    chk = _load("gt_check", HERE)
    if chk is None:
        return False, "gt_check.py is not installed, so the fix cannot be re-checked", "refused"
    rel = rec["file"] if not os.path.isabs(rec["file"]) else os.path.basename(rec["file"])
    checkers = [c for c in chk.installed_checkers(rec.get("plugin_root"))
                if chk.applies(c, rel)]
    proposer = next((c for c in checkers if c["key"] == rec["checker"]), None)
    if proposer is None:
        return False, "the proposing checker %s is no longer installed" % rec["checker"], "refused"
    before = {c["key"]: chk.run_one(c, [rel], lambda _r: original) for c in checkers}
    after = {c["key"]: chk.run_one(c, [rel], lambda _r: fixed) for c in checkers}
    for k in sorted(after):
        if before[k]["verdict"] == "pass" and after[k]["verdict"] != "pass":
            return False, ("the fix makes checker %s %s on the staged copy"
                           % (k, "fail" if after[k]["verdict"] == "fail" else "unable to check")), \
                "refused"
    p = after[rec["checker"]]
    if p["verdict"] == "cannot-check":
        return False, "the proposer could not check the staged copy: %s" % p["reason"], "refused"
    if _still_found(rec, p):
        return False, "the proposer's finding %r is still present after the fix" % rec["finding"], \
            "did-not-fix"
    return True, None, None


def _still_found(rec, result):
    return any(f.get("rule") == rec["finding"] for f in result.get("findings") or []) \
        if rec["finding"] else result.get("verdict") != "pass"


def _is_vault_md(target, vault):
    if not vault or not str(target).endswith(".md"):
        return None
    t, v = os.path.realpath(target), os.path.realpath(str(vault))
    if not t.startswith(v.rstrip(os.sep) + os.sep):
        return None
    return os.path.relpath(t, v).replace(os.sep, "/")


def _write(target, data: bytes, expect: str, vault, hint):
    """Compare-and-swap write under the caller's lock. -> None or why it was not written.

    A vault Markdown file goes through gt_write_queue + gt_broker (Core rule 1): the broker
    checks claims again and writes only if the file still has the hash queued against."""
    cur = read_bytes(target)
    if sha(cur) != expect:
        return "the file changed since it was examined; it was left as it is"
    rel = _is_vault_md(target, vault)
    if rel:
        wq = _load("gt_write_queue", HERE)
        if wq is None:
            return "gt_write_queue.py is not installed; a vault file is never written directly"
        results, note = wq.submit(Path(os.path.realpath(str(vault))),
                                  [{"path": rel, "op": "replace-file",
                                    "content": data.decode("utf-8"), "hint": hint}],
                                  origin="session")
        dec = results[0].get("decision") if results else None
        if dec in ("apply", "deduplicate"):
            return None
        return "the write queue did not apply it (%s: %s)" % (dec, results[0].get("reason")
                                                             if results else note)
    st = os.lstat(target)
    if os.path.islink(target):
        return "the file is a symlink"
    _atomic_write(Path(target), data, mode=st.st_mode & 0o7777)
    return None


def apply_one(rec, vault, dry_run=False):
    """-> (outcome, why). outcome in applied | refused | did-not-fix."""
    try:
        outcome, why = _apply(rec, vault, dry_run)
    except Refusal as r:
        outcome, why = r.outcome, r.why
    if dry_run:
        return outcome, why
    fresh = find(vault, rec["id"])
    if outcome != "applied" and fresh and fresh.get("state") != "pending":
        # Another apply of this same proposal finished first (two racing processes): its
        # record stands. This refusal is still an event, but must not overwrite "applied".
        emit(rec, outcome, vault)
        return outcome, why
    rec["history"].append({"at": now(), "outcome": outcome, "why": why})
    rec["outcome"] = outcome
    rec["state"] = {"applied": "applied", "did-not-fix": "did-not-fix"}.get(outcome, "refused")
    rec.pop("_store", None)
    save(rec, vault)
    emit(rec, outcome, vault)
    return outcome, why


def _apply(rec, vault, dry_run):
    if rec.get("state") != "pending":
        raise Refusal("refused", "proposal is %s, not pending" % rec.get("state"))
    target, rel = rec["target"], rec["file"]
    rel_f = rel.replace(os.sep, "/")
    if not any(fnmatch.fnmatch(rel_f, g) or fnmatch.fnmatch(os.path.basename(rel_f), g)
               for g in rec.get("fixes") or []):
        raise Refusal("refused", "checker %s holds no fix grant (module.json `fixes`) for %s"
                      % (rec["checker"], rel))
    if rel not in (rec.get("finding_files") or []):
        raise Refusal("refused", "%s is not a file the add-on's findings named" % rel)
    why = protected_reason(target, vault, rec.get("plugin_root"))
    if why:
        raise Refusal("refused", why)
    parsed = parse_diff(rec["diff"])
    if parsed["old"] != rel_f and not rel_f.endswith("/" + parsed["old"]):
        raise Refusal("refused", "the diff edits %s, a file outside the add-on's findings"
                      % parsed["old"])
    if os.path.islink(target):
        raise Refusal("refused", "the target is a symlink")
    original = read_bytes(target)
    if original is None:
        raise Refusal("refused", "%s does not exist" % rel)
    if sha(original) != rec.get("base_sha256"):
        raise Refusal("refused", "%s changed since the add-on examined it" % rel)
    fixed = apply_hunks(original, parsed)
    limit = SIZE_LIMITS.get(setting("addon_fix_size_limit", "16k"), SIZE_LIMITS["16k"])
    why = content_reason(original, fixed, rec.get("rules"), vault, limit)
    if why:
        raise Refusal("refused", why)
    why = claim_reason(target, vault)
    if why:
        raise Refusal("refused", why)
    ok, why, outcome = _recheck(rec, original, fixed)
    if not ok:
        raise Refusal(outcome, why)
    if dry_run:
        return "applied", "would apply (dry run; nothing written)"
    backup = home_dir() / "backups" / ("%s.orig" % rec["id"])
    _atomic_write(backup, original)
    with file_lock(target):
        why = _write(target, fixed, rec["base_sha256"], vault,
                     "add-on fix %s from %s" % (rec["id"], rec["addon"]))
    if why:
        raise Refusal("refused", why)
    rec["backup"], rec["applied_sha256"] = str(backup), sha(fixed)
    # Verify on the WRITTEN file, with the proposer only: the staged re-check already ran
    # every checker on these exact bytes.
    chk = _load("gt_check", HERE)
    written = read_bytes(target)
    proposer = next((c for c in chk.installed_checkers(rec.get("plugin_root"))
                     if c["key"] == rec["checker"]), None) if chk else None
    if proposer is not None and written is not None:
        res = chk.run_one(proposer, [rel], lambda _r: written)
        if res["verdict"] == "cannot-check" or _still_found(rec, res):
            with file_lock(target):
                _write(target, original, sha(written), vault, "roll back add-on fix %s" % rec["id"])
            raise Refusal("did-not-fix", "the re-check on the written file did not pass; the "
                                         "original bytes were restored")
    return "applied", None


def undo_one(rec, vault, dry_run=False):
    if rec.get("state") != "applied":
        return REFUSED, "proposal %s is %s, not applied" % (rec["id"], rec.get("state"))
    original = read_bytes(rec.get("backup") or "")
    if original is None:
        return REFUSED, "the original bytes are missing (%s)" % rec.get("backup")
    if dry_run:
        return OK, "would restore %s (dry run)" % rec["file"]
    with file_lock(rec["target"]):
        why = _write(rec["target"], original, rec.get("applied_sha256"), vault,
                     "undo add-on fix %s" % rec["id"])
    if why:
        return REFUSED, why
    rec["state"], rec["outcome"] = "undone", "undone"
    rec["history"].append({"at": now(), "outcome": "undone", "why": None})
    rec.pop("_store", None)
    save(rec, vault)
    emit(rec, "undone", vault)
    return OK, "restored the original bytes of %s" % rec["file"]


# ------------------------------------------------------------------ CLI ----

def cmd_list(a, vault):
    recs = [r for r in load_all(vault) if a.all or r.get("state") == "pending"]
    if a.json:
        print(json.dumps({"proposals": [{k: v for k, v in r.items() if k != "_store"}
                                        for r in recs]}, indent=2))
        return OK
    if not recs:
        print("no %sfix proposals" % ("" if a.all else "pending "))
        return OK
    for r in recs:
        print("%s  %-11s %-14s %s  -- %s" % (r["id"], r.get("state"), r.get("addon"), r["file"],
                                             r.get("reason") or r.get("finding")))
        if r.get("state") == "pending":
            for dl in r["diff"].splitlines()[:40]:
                print("    | " + dl)
    return OK


def cmd_show(a, vault):
    r = find(vault, a.ids[0])
    if not r:
        print("no single proposal matches %r" % a.ids[0], file=sys.stderr)
        return USAGE
    r.pop("_store", None)
    print(json.dumps(r, indent=2))
    return OK


def cmd_apply(a, vault):
    recs = ([r for r in load_all(vault) if r.get("state") == "pending"] if a.all
            else [find(vault, i) for i in a.ids])
    if not recs or any(r is None for r in recs):
        print("nothing to apply" if a.all else "unknown proposal id", file=sys.stderr)
        return REFUSED if a.all else USAGE
    worst, rows = OK, []
    for r in recs:
        if a.plugin_root:
            r["plugin_root"] = a.plugin_root
        outcome, why = apply_one(r, vault, a.dry_run)
        rows.append({"id": r["id"], "file": r["file"], "outcome": outcome, "why": why})
        if outcome != "applied":
            worst = REFUSED
    if a.json:
        print(json.dumps({"results": rows}, indent=2))
    else:
        for row in rows:
            print("%-11s %s  %s%s" % (row["outcome"], row["id"], row["file"],
                                      ("  -- " + row["why"]) if row["why"] else ""))
    return worst


def cmd_undo(a, vault):
    r = find(vault, a.ids[0])
    if not r:
        print("no single proposal matches %r" % a.ids[0], file=sys.stderr)
        return USAGE
    code, msg = undo_one(r, vault, a.dry_run)
    print(msg)
    return code


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_apply.py", description=__doc__.split("\n\n")[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--vault", help="vault (default: $GT_VAULT, then config)")
    common.add_argument("--dry-run", action="store_true", help="decide, write nothing")
    common.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", parents=[common], help="pending fix proposals")
    ls.add_argument("--all", action="store_true", help="every state, not only pending")
    sh = sub.add_parser("show", parents=[common], help="one proposal, as JSON")
    sh.add_argument("ids", nargs=1, metavar="ID")
    apl = sub.add_parser("apply", parents=[common], help="apply proposals")
    apl.add_argument("ids", nargs="*", metavar="ID")
    apl.add_argument("--all", action="store_true", help="every pending proposal")
    apl.add_argument("--plugin-root", help="plugin source (default: as recorded)")
    un = sub.add_parser("undo", parents=[common], help="restore an applied fix's original")
    un.add_argument("ids", nargs=1, metavar="ID")
    a = ap.parse_args(argv)
    vault = find_vault(a.vault)
    if a.vault and vault is None:
        print("no vault at %s" % a.vault, file=sys.stderr)
        return USAGE
    if a.cmd == "apply" and not (a.ids or a.all):
        print("name a proposal id, or --all", file=sys.stderr)
        return USAGE
    return {"list": cmd_list, "show": cmd_show, "apply": cmd_apply,
            "undo": cmd_undo}[a.cmd](a, vault)


if __name__ == "__main__":
    sys.exit(main())
