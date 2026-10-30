#!/usr/bin/env python3
"""gt_spill.py -- keep a command's long output out of the conversation (setting spill_threshold_lines).

    gt_spill.py hook                                 PostToolUse on Bash: replace a long output
    gt_spill.py run [--lines N] [--summarize] [--timeout S] (--shell STRING | -- CMD [ARG ...])
                                                     run a command; print the ends, keep the rest
    gt_spill.py list [--json]                        the spill files kept now
    gt_spill.py prune                                remove old spill files

WHY. A 600-line test run or a recursive listing costs its tokens once when written and again, at the
cache-read price, on every later turn until the conversation is compacted. Nearly all of it is noise
(the model needs the first lines, the last lines and the lines that say what went wrong). The full
text goes to an owner-only file; the model gets those, the line count and the path, and can Read or
grep the file if it turns out to need more.

THE HOOK. Claude Code lets a PostToolUse hook replace a tool's output before the model sees it:
`hookSpecificOutput.updatedToolOutput`, documented for ALL tools and shipped in Claude Code 2.1.121
(changelog: "PostToolUse hooks can now replace tool output for all tools via
hookSpecificOutput.updatedToolOutput (previously MCP-only)"); the value must match the tool's output
shape, and for a built-in tool a value that does not is ignored and the original output is used. For
Bash the shape is {stdout, stderr, interrupted, isImage}, so the hook copies the whole original
response and replaces only `stdout` and `stderr`. Verified 2026-10-15 against the hooks reference and
the 2.1.300 binary's schema; NOT yet run against a live model, which is why `spill_threshold_lines`
ships `off`. `dev/ctx_ab/live_check.py spill` is the one-command live proof (about one cent).

TWO LIMITS, stated where they apply:
  * PostToolUse runs only for a tool call that SUCCEEDED. A command that exits non-zero fires
    PostToolUseFailure, which can add context but cannot replace output, so a failing test run's
    output is not touched by the hook. `gt_spill.py run -- <command>` is the route for those; it
    keeps the command's own exit status.
  * A hook cannot know that Claude is about to read the output for a reason. A command carrying the
    comment `# gt:no-spill` is left alone; so is one that names this script or a spill folder, the
    sandbox fallback ~/.gt-scratch/spill included (Claude reading a spill file back must not spill
    it again); and so is one that runs one of gt's OWN scripts -- a gt_*.py in an installed gt
    plugin's scripts folder (this release's own folder, or any module under the plugin cache's
    golden-thread-plugin), in ~/.claude/golden-thread/hooks or in the vault's
    Projects/golden-thread/tools. Skills (gt-close, gt-handle, lint, task listings) read that output
    whole and act on every line; a cut one would make them act on part of it (0.21.4).

WHAT THE MODEL SEES instead of a long output:

    [gt spill: 1,284 lines (73 KB), so only the ends are shown. Full output: <path>
     Read or grep that file for more. Add `# gt:no-spill` to a command to keep its output whole.]
    --- first 25 lines ---
    --- lines that look like errors (up to 20; line numbers are the file's) ---
    --- last 40 lines ---

SECRETS (0.21.4). The output is scanned with gt_secrets BEFORE a spill file is written (the hook) or
once the command exits (the wrapper): each credential-shaped line is replaced, in the file and in the
preview, by `[gt: line N withheld -- looked like a credential (rule)]` and the preview header names
how many. If the scan cannot run the hook writes no file and leaves the output whole; the wrapper
deletes its file and prints the output whole. The wrapper's file holds the raw bytes, 0600, only while
the command runs. The file is created 0600 in a 0700 folder before a byte is written (gt_private).
`--summarize` sends the
file to a LOCAL engine only (gt-farm, a `local: yes` row on this machine, see gt_intake.py) and only after gt_secrets finds nothing in
it; a finding stops the send and the run says so, naming a count and never a value. Without a local
engine it is skipped and says so. The command line itself is never stored.

THE LEDGER. When the usage module is installed and `savings_ledger` is not off, each spill appends
one `spill` line to ~/.claude/golden-thread/usage/savings.jsonl: lines, the tokens the model saw and
the tokens it would have seen (characters / 4, `estimated`), the spill id and an opaque session and
project id; no command, path or text. gt_savings.py also counts how many tokens the model read BACK
from spill files, so the report's NET for this mechanism is honest when the model fetches it all.

Files are pruned after 7 days or past 256 MB, oldest first. $GT_SPILL_DIR overrides the folder (tests).
Where ~/.claude/golden-thread is not writable (gt sandbox mode) the wrapper falls back to
~/.gt-scratch/spill. Exit (run): the command's own status; 125 when the wrapper could not start it.
The hook always exits 0 and prints nothing on any error. Stdlib only; Python 3.8+.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gt_private  # noqa: E402

GT_VERSION = "0.22.0"
CHARS_PER_TOKEN = 4
MIN_LINES, MAX_LINES = 20, 100000
TOKENS_PER_LINE_BUDGET = 50            # the spill size in estimated tokens is lines * 50 (one 200-character line)
MAX_PAYLOAD = 64 * 1024 * 1024         # a hook payload larger than this is not touched
KEEP_DAYS, KEEP_BYTES = 7, 256 * 1024 * 1024
LINE_CAP = 300                         # a shown line up to this is whole; a longer one is shortened
                                       # at a word boundary (never mid-word), with a marker
ERROR_LINES = 20
OPT_OUT = "gt:no-spill"
ERROR_RE = re.compile(
    r"(?i)\b(error|errors|failed|failure|failures|fatal|traceback|exception|panic|denied|"
    r"cannot|can't|could not|no such file|not found|assertion|segmentation fault)\b")
EXIT_CANNOT_START = 125


def home():
    return os.path.expanduser("~")


def primary_dir():
    return os.environ.get("GT_SPILL_DIR") or os.path.join(home(), ".claude", "golden-thread", "spill")


def fallback_dir():
    return os.path.join(home(), ".gt-scratch", "spill")


def spill_dir():
    """The folder spill files go to: the primary one, else the sandbox-writable fallback."""
    d = primary_dir()
    gt_private.ensure_private_dir(d)
    if os.path.isdir(d) and os.access(d, os.W_OK):
        return d
    if os.environ.get("GT_SPILL_DIR"):
        return d
    f = fallback_dir()
    gt_private.ensure_private_dir(f)
    return f if os.path.isdir(f) else d


def read_dirs():
    """Where a spill file may live (the folders `list` and `prune` look in)."""
    out = [primary_dir()]
    if not os.environ.get("GT_SPILL_DIR"):
        out.append(fallback_dir())
    return out


# gt's own scripts, by where they are installed (see TWO LIMITS above). Matched on the command with
# backslashes turned to slashes, so a Windows path counts too.
GT_SCRIPT_RE = re.compile(
    r"(?:\.claude/golden-thread/hooks"
    r"|\.claude/plugins/cache/golden-thread-plugin/[^/\s\"']+/[^/\s\"']+/scripts"
    r"|/Projects/golden-thread/tools)/gt_\w+\.py\b")


def _norm(path):
    return path.replace("\\", "/").rstrip("/")


def exempt(cmd):
    """True when a Bash command's output must reach the model whole: the opt-out comment, this
    script, a spill folder being read back, or one of gt's own scripts."""
    c = _norm(cmd)
    if OPT_OUT in cmd or "gt_spill.py" in c:
        return True
    for d in ("golden-thread/spill", ".gt-scratch/spill", _norm(primary_dir()), _norm(fallback_dir())):
        if d and d in c:
            return True
    if GT_SCRIPT_RE.search(c):
        return True
    return re.search(re.escape(_norm(HERE)) + r"/gt_\w+\.py\b", c) is not None


def _config():
    try:
        with open(os.path.join(home(), ".claude", "vault-config.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def threshold(cfg=None):
    """-> the spill threshold in lines, or None when spilling is off (the default). Read from the one
    config file directly: this runs on every Bash call, so it must not import gt_settings."""
    v = str((cfg if cfg is not None else _config()).get("spill_threshold_lines") or "off").strip().lower()
    if v.isascii() and v.isdigit() and MIN_LINES <= int(v) <= MAX_LINES:
        return int(v)
    return None


def new_id():
    return "%s-%s" % (datetime.datetime.now().strftime("%Y%m%d-%H%M%S"), secrets.token_hex(3))


def tokens(chars):
    return int(chars) // CHARS_PER_TOKEN


# Terminal control sequences in a PREVIEW (the model's view, and possibly a terminal): an escape sequence (colour,
# cursor move, an OSC title set, a DCS/APC string) is dropped whole; any other control byte but tab becomes U+FFFD.
# The spill file keeps every byte; only what is shown is made safe.
_ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x9b[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
                      r"|\x1b[PX^_][^\x1b]*(?:\x1b\\)?|\x1b[@-Z\\-_]")
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _safe(line):
    """A line for the preview: no escape sequences, no raw control bytes (tab stays; a single trailing CR from
    a CRLF line is dropped)."""
    if line.endswith("\r"):
        line = line[:-1]
    if "\x1b" in line or "\x9b" in line:
        line = _ESCAPES.sub("", line)
    return _CONTROLS.sub("\ufffd", line)


def _cut(line, number):
    """A preview line: whole, or -- past LINE_CAP -- its start up to a word boundary, never mid-word,
    with a marker naming the line in the spill file. `number` is the 1-based line number."""
    line = _safe(line)
    if len(line) <= LINE_CAP:
        return line
    head = line[:LINE_CAP + 1]
    space = max(head.rfind(" "), head.rfind("\t"))
    head = head[:space].rstrip() if space > 0 else line[:LINE_CAP]   # one giant token: cut it
    return "%s ... [line %d: %s more characters in the file]" % (
        head, number, format(len(line) - len(head), ","))


def sizes(n_bytes):
    if n_bytes < 1000:
        return "%d bytes" % n_bytes
    if n_bytes < 1000 * 1000:
        return "%d KB" % round(n_bytes / 1000.0)
    return "%.1f MB" % (n_bytes / 1000.0 / 1000.0)


def head_tail(limit):
    """-> (head, tail) line counts that always leave room under `limit`."""
    return max(3, min(25, limit // 5)), max(5, min(40, limit // 4))


def preview_mode(cfg=None):
    """-> "smart" (the default: shorten by the kind of output) or "ends" (first and last lines only).
    Setting `spill_preview`; anything else means smart."""
    v = str((cfg if cfg is not None else _config()).get("spill_preview") or "smart").strip().lower()
    return "ends" if v == "ends" else "smart"


def _kind_items(lines, mode, budget):
    """-> (kind, items, note) from gt_spill_kinds, or (None, None, None). A module that is missing or
    broken means the ends preview, never an error (the hook fails open)."""
    if mode != "smart":
        return None, None, None
    try:
        import gt_spill_kinds
        got = gt_spill_kinds.shorten(lines, budget)
    except Exception:                                         # noqa: BLE001
        got = None
    return got if got else (None, None, None)


def preview(lines, path, nbytes, limit, withheld=0, mode=None):
    """-> the text the model sees instead of `lines` (a list of the output's lines, already masked).
    `withheld` is how many credential-shaped lines redact() replaced; the header says so. `mode` is
    preview_mode() unless a caller says. In smart mode the output's kind is looked up (gt_spill_kinds)
    and the kind's preview is used only if it is SMALLER than the ends preview (0.22.0: on the owner's
    own transcripts a grouped grep preview came out 15% larger than first-and-last lines), JSON excepted."""
    head_n, tail_n = head_tail(limit)
    ends = _render(lines, path, nbytes, withheld, head_n, tail_n, None, None, None)
    kind, items, note = _kind_items(lines, preview_mode() if mode is None else mode,
                                    max(30, head_n + tail_n + ERROR_LINES))
    if not items:
        return ends
    smart = _render(lines, path, nbytes, withheld, head_n, tail_n, kind, items, note)
    # JSON is exempt: the ends preview of a document on one line is that line's first 300 characters,
    # small and useless; the JSON rendering has its own line budget and is checked against the output.
    return smart if kind == "json" or len(smart) < len(ends) else ends


def _render(lines, path, nbytes, withheld, head_n, tail_n, kind, items, note):
    """One preview. `items` are (first line, last line, text) numbered as in the file: one line each for
    the ends preview, or -- for a kind gt_spill_kinds recognised -- a folded run of lines."""
    total = len(lines)
    if items is None:
        items = [(i + 1, i + 1, x) for i, x in enumerate(lines)]
    n = len(items)
    whole = kind == "json"          # a JSON rendering is one document: shown whole, not split into ends
    if whole:
        head_n, tail_n = n, 0
    elif head_n + tail_n >= n:
        head_n, tail_n = n // 2, n - n // 2
    out = ["[gt spill: %s lines (%s), so only the ends are shown. Full output: %s" % (format(total, ","), sizes(nbytes), path),
           " Read or grep that file for more. Add `# %s` to a command to keep its output whole.]" % OPT_OUT]
    if withheld:
        out.append("[gt: %d line(s) withheld from the preview and the file: they looked like credentials]"
                   % withheld)
    if kind:
        out.append("[gt: Shown as %s: %s. The file has every line.]" % (kind.replace("-", " "), note))
    out.append("--- sampled JSON (%d lines) ---" % n if whole else "--- first %d lines ---" % head_n)
    out += [_cut(t, a) for a, _b, t in items[:head_n]]
    if whole:
        return "\n".join(out)
    if n > head_n + tail_n:
        lo = items[head_n - 1][1] + 1 if head_n else 1
        hi = items[n - tail_n][0] - 1 if tail_n else total
        out.append("--- %s lines omitted (lines %d to %d of the file) ---" % (format(hi - lo + 1, ","), lo, hi))
    errs = [it for it in items[head_n:n - tail_n] if ERROR_RE.search(it[2])][:ERROR_LINES]
    if errs:
        out.append("--- lines that look like errors (up to %d; the numbers are the file's) ---" % ERROR_LINES)
        out += ["%d: %s" % (a, _cut(t, a)) for a, _b, t in errs]
    out.append("--- last %d lines ---" % tail_n)
    out += [_cut(t, a) for a, _b, t in items[n - tail_n:]]
    return "\n".join(out)


def _drop(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _withheld_note(n):
    return "[gt: %d line(s) withheld from the output: they looked like credentials]" % n


def mask(text):
    """-> (masked text, lines withheld), or None when the scan could not run (gt_intake.redact)."""
    try:
        import gt_intake
        return gt_intake.redact(text)
    except Exception:                                         # noqa: BLE001
        return None


SUMMARY_LINES_DEFAULT = 400
SUMMARY_MIN_LINES = 100
SUMMARY_TIMEOUT = 30.0


def summary_lines(cfg=None):
    """-> the line count from which a spilled output is summarised by a model, or None when spill_summary is
    off (the default). 0.22.0. Read straight from the config file, like threshold()."""
    c = cfg if cfg is not None else _config()
    if str(c.get("spill_summary") or "off").strip().lower() != "on":
        return None
    v = str(c.get("spill_summary_lines") or SUMMARY_LINES_DEFAULT).strip()
    if v.isascii() and v.isdigit() and int(v) >= SUMMARY_MIN_LINES:
        return int(v)
    return SUMMARY_LINES_DEFAULT


def summarised(body, path, n_lines):
    """-> (text, why): the model summary of an already-masked body with the spill file's path, or (None, why)
    when the no-tools reader cannot produce one. Never raises."""
    try:
        import gt_sandbox_reader
        res = gt_sandbox_reader.summarise(body, timeout=SUMMARY_TIMEOUT)
    except Exception as exc:                                  # noqa: BLE001
        return None, "the summariser could not start (%s)" % type(exc).__name__
    if res.get("status") != "read" or not res.get("summary"):
        return None, str(res.get("why") or "no summary came back")
    facts = [f for f in (res.get("facts") or []) if isinstance(f, str)][:12]
    text = ("[gt: %d lines (%s) summarised by a model with no tools; the full text, credentials masked, is in %s "
            "-- read a line range of that file for detail]\n%s" % (n_lines, sizes(len(body.encode("utf-8", "replace"))),
                                                                     path, res["summary"]))
    if facts:
        text += "\n" + "\n".join("- " + f for f in facts)
    return text, ""


def write_spill(text):
    """Write `text` to a new spill file; the caller passes text already masked. -> (id, path) or
    (None, None)."""
    sid = new_id()
    path = os.path.join(spill_dir(), sid + ".txt")
    if not gt_private.write_private(path, text):
        return None, None
    gt_private.prune(os.path.dirname(path), max_age_days=KEEP_DAYS, max_bytes=KEEP_BYTES)
    return sid, path


# ---- the ledger ------------------------------------------------------------------------

def _opaque(s):
    return hashlib.sha256(str(s).encode("utf-8", "replace")).hexdigest()[:8]


def record(spill_id, n_lines, full_chars, shown_chars, session="", transcript=""):
    """Append one `spill` line to the savings ledger when the usage module is there. Never raises."""
    try:
        usage = os.path.join(home(), ".claude", "golden-thread", "usage")
        if not os.path.isdir(usage):
            return False
        if str(_config().get("savings_ledger") or "on").strip().lower() == "off":
            return False
        ev = {"v": 1, "gt": GT_VERSION, "mechanism": "spill", "state": "estimated",
              "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "key": "spill:%s" % spill_id, "spill_id": spill_id, "lines": n_lines,
              "session": str(session or "")[:8],
              "project": _opaque(os.path.basename(os.path.dirname(transcript))) if transcript else "",
              "actual_eq": tokens(shown_chars), "counterfactual_eq": tokens(full_chars)}
        with open(os.path.join(usage, "savings.jsonl"), "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(ev, sort_keys=True, separators=(",", ":")) + "\n")
        return True
    except Exception:                                        # noqa: BLE001
        return False


# ---- the hook ----------------------------------------------------------------------------

def estimate_tokens(text):
    """The text's size in tokens, estimated from its characters (rounded up)."""
    return -(-len(text) // CHARS_PER_TOKEN)


def over_limit(text, limit):
    """Spill when the estimated tokens pass limit * TOKENS_PER_LINE_BUDGET, so one very long line and
    many short lines cross at the same size; more lines than `limit` always spills (the floor)."""
    n = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
    return n > limit or estimate_tokens(text) > limit * TOKENS_PER_LINE_BUDGET


def decide(payload, limit):
    """-> the hook's output dict, or None to leave the output alone."""
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return None
    resp = payload.get("tool_response")
    if not isinstance(resp, dict):
        return None
    if resp.get("isImage") or resp.get("interrupted") or resp.get("backgroundTaskId"):
        return None
    out, err = resp.get("stdout"), resp.get("stderr")
    out = out if isinstance(out, str) else ""
    err = err if isinstance(err, str) else ""
    if not out and not err:
        return None
    cmd = ""
    ti = payload.get("tool_input")
    if isinstance(ti, dict) and isinstance(ti.get("command"), str):
        cmd = ti["command"]
    if exempt(cmd):
        return None
    body = out + (("\n--- stderr ---\n" + err) if err else "")
    if not over_limit(body, limit):
        return None
    lines = body.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    red = mask(body)                                          # scan BEFORE any write (0.21.4, Core rule 9)
    if red is None:
        return None                                           # cannot scan: keep no file, leave it whole
    body, withheld = red
    if withheld:
        lines = body.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
    sid, path = write_spill(body if body.endswith("\n") else body + "\n")
    if not sid:
        return None                                           # could not keep it: leave it whole
    shown = preview(lines, path, len(body.encode("utf-8", "replace")), limit, withheld)
    want = summary_lines()
    if want is not None and len(lines) >= want:               # spill_summary on (0.22.0): a model summary, if it is smaller
        text, why = summarised(body, path, len(lines))
        if text is not None and len(text) < len(shown):
            shown = text
        elif text is None:
            shown += "\n[gt: summary not used: %s]" % why
        else:                                                  # ran, but the summary was not smaller
            shown += "\n[gt: summary not used: the summary was not smaller than the ordinary preview]"
    if len(shown) >= len(body):                               # rule 1 (0.22.0): never larger than the output
        _drop(path)
        if not withheld:
            return None
        shown = _withheld_note(withheld) + "\n" + body       # the masked text whole, never the raw one
        sid = None
    if sid:
        record(sid, len(lines), len(body), len(shown), payload.get("session_id"),
               payload.get("transcript_path") or "")
    new = dict(resp)
    new["stdout"] = shown
    new["stderr"] = ""
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": new}}


def cmd_hook():
    limit = threshold()
    if limit is None:
        return 0                                              # off: do not even read the payload
    try:
        import gt_break_even
        if gt_break_even.is_paused("spill"):                  # (never while savings_ledger is off)
            return 0                                          # its 7-day net is negative: stand down
    except Exception:                                         # noqa: BLE001
        pass
    try:
        raw = sys.stdin.read(MAX_PAYLOAD + 1)
        if len(raw) > MAX_PAYLOAD:
            return 0
        res = decide(json.loads(raw), limit)
        if res:
            sys.stdout.write(json.dumps(res))
    except Exception:                                         # noqa: BLE001
        return 0
    return 0


# ---- the wrapper -------------------------------------------------------------------------

def _summarize(path):
    """-> a short local-engine summary of the file, or a one-line reason there is none."""
    try:
        import gt_intake
        res = gt_intake.local_digest(path, "Summarise this command output: what ran, whether it "
                                          "succeeded, each distinct error or failing test and the "
                                          "final status. Be brief.", allow_remote=False)
    except Exception as exc:                                  # noqa: BLE001
        return "(local summary unavailable: %s)" % type(exc).__name__
    return res["text"] if res.get("ok") else "(no local summary: %s)" % res.get("why", "unavailable")


def run_command(argv=None, shell=None, limit=200, summarize=False, timeout=None, out=None):
    """Run a command, spilling its merged output past `limit` lines. -> its exit status."""
    out = out or sys.stdout
    sid = new_id()
    path = os.path.join(spill_dir(), sid + ".txt")
    gt_private.ensure_private_dir(os.path.dirname(path))
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    except OSError as exc:
        out.write("gt_spill: cannot create a spill file (%s); command not run\n" % type(exc).__name__)
        return EXIT_CANNOT_START
    proc = None
    try:
        try:
            proc = subprocess.Popen(shell if shell is not None else argv, shell=shell is not None,
                                    stdin=subprocess.DEVNULL, stdout=fd, stderr=subprocess.STDOUT)
        except OSError as exc:
            out.write("gt_spill: cannot start the command (%s)\n" % type(exc).__name__)
            return EXIT_CANNOT_START
        finally:
            os.close(fd)
        try:
            signal.signal(signal.SIGTERM, lambda *_a: (proc.terminate(), sys.exit(143)))
        except (ValueError, OSError, AttributeError):
            pass
        try:
            status = proc.wait(timeout=timeout)
        except SystemExit:
            try:
                os.unlink(path)                                # stopped by SIGTERM: do not leave raw bytes
            except OSError:
                pass
            raise
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            status = 124
            out.write("gt_spill: the command was stopped after %s s\n" % timeout)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        out.write("gt_spill: the output could not be read back\n")
        return status
    # Windows: a child writes CRLF, and writing that to a text-mode stdout adds another CR, which
    # shows as a blank line after every line. Keep LF here; the stream adds the platform's ending.
    text = data.decode("utf-8", "replace").replace("\r\n", "\n")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not over_limit(text, limit):
        try:
            os.unlink(path)
        except OSError:
            pass
        out.write(text)
        return status
    red = mask(text)                                          # the raw bytes lived in the file only until now
    if red is None:
        try:
            os.unlink(path)
        except OSError:
            pass
        out.write(text)
        out.write("gt_spill: the secret scan could not run, so no spill file was kept and the output is "
                  "shown whole\n")
        return status
    text, withheld = red
    if withheld:
        if not gt_private.write_private(path, text):           # atomic: os.replace over the raw file
            try:
                os.unlink(path)
            except OSError:
                pass
            out.write(data.decode("utf-8", "replace").replace("\r\n", "\n"))
            out.write("gt_spill: the masked copy could not be written, so no spill file was kept and the "
                      "output is shown whole\n")
            return status
        data = text.encode("utf-8", "replace")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
    shown = preview(lines, path, len(data), limit, withheld) + "\n--- exit status %d ---\n" % status
    if len(shown) >= len(text):                               # rule 1 (0.22.0): never larger than the output
        _drop(path)
        out.write((_withheld_note(withheld) + "\n" if withheld else "") + text)
        return status
    if withheld:
        shown += ("--- the raw output sat in an owner-only (0600) file while the command ran; the file now "
                  "holds the masked text ---\n")
    out.write(shown)
    if summarize:
        summary = "--- local summary ---\n%s\n" % _summarize(path)
        out.write(summary)
        shown += summary
    record(sid, len(lines), len(text), len(shown), os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    gt_private.prune(os.path.dirname(path), max_age_days=KEEP_DAYS, max_bytes=KEEP_BYTES)
    return status


# ---- list, prune, cli ------------------------------------------------------------------

def listing():
    rows = []
    for d in read_dirs():
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if not n.endswith(".txt"):
                continue
            p = os.path.join(d, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            rows.append({"id": n[:-4], "bytes": st.st_size, "mtime": int(st.st_mtime), "path": p})
    return sorted(rows, key=lambda r: -r["mtime"])


def cmd_prune():
    n = 0
    for d in read_dirs():
        n += gt_private.prune(d, max_age_days=KEEP_DAYS, max_bytes=KEEP_BYTES)
    print("gt_spill: removed %d old spill file(s)" % n)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["hook"]:
        return cmd_hook()
    ap = argparse.ArgumentParser(prog="gt_spill.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("run")
    p.add_argument("--lines", type=int, default=0, help="spill past this many lines (default: the setting, else 200)")
    p.add_argument("--summarize", action="store_true")
    p.add_argument("--timeout", type=float)
    p.add_argument("--shell")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p = sub.add_parser("list")
    p.add_argument("--json", action="store_true")
    sub.add_parser("prune")
    try:
        a = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if a.cmd == "run":
        cmd = [c for c in a.command if c != "--"] if a.command[:1] == ["--"] else list(a.command)
        if not a.shell and not cmd:
            print("usage: gt_spill.py run [--lines N] (--shell STRING | -- COMMAND [ARG ...])", file=sys.stderr)
            return 2
        limit = a.lines if MIN_LINES <= a.lines <= MAX_LINES else (threshold() or 200)
        return run_command(argv=cmd, shell=a.shell, limit=limit, summarize=a.summarize, timeout=a.timeout)
    if a.cmd == "list":
        rows = listing()
        if a.json:
            print(json.dumps(rows, sort_keys=True))
        else:
            for r in rows:
                print("%s  %s  %s" % (r["id"], sizes(r["bytes"]).rjust(9),
                                      time.strftime("%Y-%m-%d %H:%M", time.localtime(r["mtime"]))))
            if not rows:
                print("no spill files")
        return 0
    if a.cmd == "prune":
        return cmd_prune()
    ap.print_help()
    return 2


if __name__ == "__main__":
    # A hook checks its own files first when gt's code is sealed (paranoid.signed_manifest); the CLI
    # forms stay usable. One stat() when the group is off.
    try:
        import gt_integrity_gate as _gig
    except ImportError:
        _gig = None
    if _gig is not None:
        _gig.check(__file__, "reporter", when=sys.argv[1:2] == ["hook"])
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:                                         # noqa: BLE001
        if sys.argv[1:2] == ["hook"]:
            sys.exit(0)
        raise
