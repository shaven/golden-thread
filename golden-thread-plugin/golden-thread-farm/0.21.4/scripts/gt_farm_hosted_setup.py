#!/usr/bin/env python3
"""gt_farm_hosted_setup.py -- set up a hosted OpenAI-compatible engine for gt-farm in one command (gt-farm 0.22.0)

    gt_farm_hosted_setup.py --vault VAULT --services FILE --name NAME --model PROVIDER/MODEL
                            [--base-url URL] [--provider LABEL] [--zone ZONE] [--rate USD_PER_MTOK]
                            [--allow-private-host] [--replace-key] [--no-send] [--yes]
    gt_farm_hosted_setup.py ... --dry-run      say what would be done; change nothing, send nothing
    gt_farm_hosted_setup.py ... --check        verify the pieces exist and name the next step; send nothing

Run it in YOUR OWN terminal. It works with any public https host that serves /v1/chat/completions (OpenRouter,
Together, a company gateway); the OpenRouter address is only the default shown by the `openai-compatible`
template. A literal IP address, localhost, a single-label name or a .local / .internal / .lan / .home.arpa name is
refused unless you pass --allow-private-host; plain http is always refused.

Before anything runs it names the host the key will be sent to and asks you to type back a code it shows on
the terminal itself (the same typed challenge LOTR uses: a program that only fakes a terminal cannot read it).
--yes skips only the later cost question, never this. The key's store name is always llm-key-NAME.

What it does, in order, and says done / skipped / failed for each:
  1. secret      the API key is typed at LOTR's own hidden prompt (`lotr secret put`), which this script starts
                 with your terminal handed over to it. This script never reads, receives, passes on or prints
                 the key: it is not an argument, not in the environment, not on a pipe and not in a file here.
  2. connection  `lotr add-http NAME@ZONE --from-template openai-compatible --base-url URL --token-ref
                 store:llm-key-NAME`. A connection that exists is used only if its address and key reference
                 are exactly these; otherwise the run stops and shows both.
  3. row         the farm's service table gets (or updates) one `transport: lotr` row, through the vault write
                 queue, inside the table (the table reader stops at the first blank line after the rows).
                 The row is read-only: results are held for your review until you set `trust: autonomous`.
                 Draining the queue also applies other sessions' queued writes (with their usual checks); a
                 write the broker holds as a conflict is reported as such, with the row to add by hand.
  4. check       with your yes, ONE tiny public packet is sent through the row and the result is shown.

Nothing here is specific to one person or provider: names, hosts and model ids are arguments. A failure at any
step leaves the earlier steps in place and names the command that resumes from there. Running it twice updates,
never duplicates. Exit: 0 every step done or skipped | 1 a step failed | 2 refused (not a terminal, bad arguments,
the typed code did not match).
"""
import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import gt_farm_send as fs  # noqa: E402

TEMPLATE = "openai-compatible"
EXAMPLE_BASE_URL = "https://openrouter.ai/api"          # the template's default, shown as an EXAMPLE preset only
OP = "POST /v1/chat/completions"
# every pattern is used with re.fullmatch: `$` would let a trailing newline through
NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}")
ZONE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
TOKEN_RE = re.compile(r"[A-Za-z0-9._:/+-]{1,120}")       # a provider label or a provider/model id: no spaces, no |
RATE_RE = re.compile(r"\d{1,6}(\.\d{1,6})?")
URL_RE = re.compile(r"https://(?P<host>[A-Za-z0-9.-]+)(:(?P<port>\d{1,5}))?(?P<path>/[A-Za-z0-9._~/-]*)?")
LABEL_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
TLD_RE = re.compile(r"[a-z]{2,63}|xn--[a-z0-9-]{1,59}")  # a top label is letters: no 0x7f.0.0.1, no 2130706433
PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".intranet", ".lan", ".home.arpa", ".localdomain")
REQUIRED_COLUMNS = ("name", "transport", "connection", "op", "model", "provider", "tier")
OWNED_EMPTY = ("local",)              # columns a hosted row always leaves empty: the connection says where it runs
MATCHED = ("transport", "connection", "model", "provider", "tier", "trust")   # what the read-back compares
CHECK_PACKET = """### TASK
Name the capital of France.

### INPUTS
None. This is a public connectivity check; no private data is involved.

### RETURN CONTRACT
Answer ONLY in this shape. Omit preamble.

FINDINGS:
  - claim: <one sentence>
    source: NONE
    confidence: high
GAPS:
  - None
"""


def hooks_dir():
    return os.environ.get("GT_HOOKS_DIR") or fs.HOOKS_DIR


def say(step, status, text):
    sys.stdout.write("[%s] %s: %s\n" % (status, step, text))
    sys.stdout.flush()


def parse_args(argv):
    p = argparse.ArgumentParser(prog="gt_farm_hosted_setup.py", description=(__doc__ or "").split("\n\n")[0])
    p.add_argument("--vault", required=True, help="the vault (required, never inferred)")
    p.add_argument("--services", required=True, help="the farm service table (a Markdown file inside the vault)")
    p.add_argument("--name", required=True, help="the engine's name: its row and its connection id")
    p.add_argument("--model", required=True, help="the model id to request, e.g. <provider/model-id>")
    p.add_argument("--base-url", default=EXAMPLE_BASE_URL,
                   help="the host's address WITHOUT /v1 (default: the template's example, %s)" % EXAMPLE_BASE_URL)
    p.add_argument("--provider", help="the row's provider label (default: the host name of --base-url)")
    p.add_argument("--zone", default="personal", help="the LOTR zone of the connection (default personal)")
    p.add_argument("--rate", help="reference price, dollars per million tokens, for the row (optional)")
    p.add_argument("--secret-name", help=argparse.SUPPRESS)     # only llm-key-<name> is accepted (see validate)
    p.add_argument("--allow-private-host", action="store_true",
                   help="allow a literal IP address, localhost or an internal name in --base-url (still https)")
    p.add_argument("--replace-key", action="store_true", help="ask for the key again even if one is stored")
    p.add_argument("--dry-run", action="store_true", help="say what would be done; change nothing")
    p.add_argument("--check", action="store_true", help="verify the pieces and name the next step")
    p.add_argument("--no-send", action="store_true", help="do not offer the one-packet check")
    p.add_argument("--yes", action="store_true",
                   help="send the check packet without the cost question (the typed code is still asked)")
    return p.parse_args(argv)


def _has_control(s):
    """True for any control, format (bidi, zero-width) or line/paragraph separator character."""
    return any(unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") for ch in s)


def host_of(url):
    m = URL_RE.fullmatch(url or "")
    return m.group("host").lower() if m else ""


def host_problem(host, allow_private):
    """-> None, or why `host` is refused. Private and internal hosts pass only with --allow-private-host."""
    if not host or host.endswith(".") or not all(LABEL_RE.fullmatch(x) for x in host.split(".")):
        return "--base-url has a malformed host name"
    if allow_private:
        return None
    flag = " (pass --allow-private-host if you mean it)"
    try:
        ipaddress.ip_address(host)
        return "--base-url is a literal IP address%s" % flag
    except ValueError:
        pass
    if "." not in host:
        return "--base-url host %s is a single-label or local name%s" % (host, flag)
    if any(host.endswith(s) for s in PRIVATE_SUFFIXES):
        return "--base-url host %s is a local or internal name%s" % (host, flag)
    if not TLD_RE.fullmatch(host.rsplit(".", 1)[1]):
        return "--base-url host %s does not end in a name (it reads as an address)%s" % (host, flag)
    return None


def validate(a):
    """-> a list of plain-words problems with the arguments (nothing in them is secret)."""
    bad = []
    for k, v in sorted(vars(a).items()):
        if isinstance(v, str) and _has_control(v):
            bad.append("--%s holds a control character (a newline, escape or invisible mark)" % k.replace("_", "-"))
    if bad:
        return bad
    if not NAME_RE.fullmatch(a.name or ""):
        bad.append("--name is one lower-case word: letters, digits, . _ - (at most 40)")
    if not ZONE_RE.fullmatch(a.zone or ""):
        bad.append("--zone is one word: letters, digits, _ -")
    if not a.model or not TOKEN_RE.fullmatch(a.model):
        bad.append("--model is a model id with no spaces, e.g. <provider/model-id>")
    m = URL_RE.fullmatch(a.base_url or "")
    if not m:
        bad.append("--base-url must be an https address without /v1 (or a query)")
    else:
        why = host_problem(m.group("host").lower(), getattr(a, "allow_private_host", False))
        if why:
            bad.append(why)
    if a.provider is not None and not TOKEN_RE.fullmatch(a.provider):
        bad.append("--provider is a short label with no spaces")
    if a.rate is not None and not RATE_RE.fullmatch(a.rate):
        bad.append("--rate is a number of dollars per million tokens")
    if a.secret_name is not None and a.secret_name != "llm-key-%s" % a.name:
        bad.append("--secret-name: the key's store name is always llm-key-%s (this script never uses another "
                   "stored credential)" % a.name)
    if not bad and not (os.path.isdir(a.vault)):
        bad.append("--vault %s is not a folder" % a.vault)
    return bad


def provider_label(a):
    if a.provider:
        return a.provider
    host = host_of(a.base_url)
    parts = host.split(".")
    return (parts[-2] if len(parts) >= 2 else host).lower()


def connection_id(a):
    return "%s@%s" % (a.name, a.zone)


def secret_name(a):
    return "llm-key-%s" % a.name


# ---------------------------------------------------------------------------------------- terminal

def has_terminal():
    """True when stdin and stdout are a person's terminal. Fails closed: if gt's terminal helper cannot be
    found, a plain isatty() on both streams decides (never 'yes' by default)."""
    try:
        d = fs.core_scripts_dir()
        if d and d not in sys.path:
            sys.path.append(d)
        import gt_tty
        return bool(gt_tty.has_terminal(sys.stdin) and gt_tty.has_terminal(sys.stdout))
    except Exception:                                    # noqa: BLE001 -- unknown means ask the OS
        try:
            return bool(sys.stdin.isatty() and sys.stdout.isatty())
        except (AttributeError, OSError, ValueError):
            return False


def typed_challenge(reason, tty=None, code=None):
    """-> None when the person at the terminal typed back the code shown there, else the refusal text.
    COPIED from gt-lotr 0.5.1 lotr.py `_typed_challenge` (itself copied from gt_grant.py): the farm must not
    import LOTR's command line. isatty() alone is satisfied by `script -q /dev/null ...`; the code is shown on,
    and read from, the terminal itself (/dev/tty), so a bare wrapper that cannot read its own output fails.
    LIMIT: a program that drives a pseudo-terminal and answers can still pass. `tty` (tests) is an object with
    read(n) and write(bytes)."""
    import secrets
    code = code or "%04d" % secrets.randbelow(10000)
    own = None
    if tty is None:
        try:
            if os.name == "nt":
                raise OSError("no /dev/tty")
            own = tty = open("/dev/tty", "r+b", buffering=0)
        except OSError:
            return "refused: this needs a terminal it can show a code on; run it in your own terminal."
    try:
        tty.write(("\n%s\nType %s to confirm (anything else cancels): " % (reason, code)).encode())
        buf = b""
        while b"\n" not in buf:
            chunk = tty.read(64)
            if not chunk:
                break
            buf += chunk
    finally:
        if own is not None:
            own.close()
    return None if buf.decode("utf-8", "replace").strip() == code else \
        "refused: the code did not match; nothing changed."


# ------------------------------------------------------------------------------------------- lotr

def lotr_command():
    """-> [python, lotr.py] or None. $GT_LOTR_SCRIPTS or the installed gt-lotr, as the farm finds it."""
    d = fs._lotr_scripts()
    path = os.path.join(d, "lotr.py") if d else ""
    return [sys.executable, path] if path and os.path.isfile(path) else None


def run_lotr(lotr, args):
    """Run a lotr command with its output captured -> (returncode, parsed JSON or None, text). No key is ever
    passed here: this is for commands that take none."""
    p = subprocess.run(lotr + ["--json"] + list(args), capture_output=True, text=True)
    text = (p.stdout or "") + (p.stderr or "")
    try:
        data = json.loads(p.stdout)
    except (ValueError, TypeError):
        data = None
    return p.returncode, data, text


def error_of(data, text):
    err = (data or {}).get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return err.get("code") or "error", err.get("message") or "", list(err.get("hints") or [])
    return "error", (text or "").strip()[-300:], []


def run_lotr_with_terminal(lotr, args):
    """Hand the terminal to lotr (stdin, stdout and stderr are inherited): the person types the key to LOTR's
    own hidden prompt. Nothing is captured, so this script cannot see it."""
    return subprocess.run(lotr + list(args)).returncode


def secret_present(lotr, ref):
    rc, data, _ = run_lotr(lotr, ["secret", "check", ref])
    return rc == 0 and isinstance(data, dict) and bool(data.get("present"))


def lotr_registry_path():
    """LOTR's registry for the home `lotr add-http` (run without --home/--zone) writes to: $LOTR_HOME, else
    ~/.config/gt-lotr/personal (lotrlib.util.lotr_home)."""
    env = os.environ.get("LOTR_HOME")
    home = os.path.expanduser(env) if env else os.path.join(os.path.expanduser("~"), ".config", "gt-lotr",
                                                            "personal")
    return os.path.join(home, "registry.json")


def existing_connection(lotr, cid):
    """-> ({"base_url", "token_ref"}, None) for an existing connection, or (None, why it cannot be read).
    base_url comes from `lotr connections` (LOTR's own view); no lotr command shows a token_ref, so that one
    comes from LOTR's registry file (a reference such as store:NAME, never a value). Both must be readable."""
    rc, data, text = run_lotr(lotr, ["connections"])
    conns = data.get("connections") if isinstance(data, dict) else None
    if rc != 0 or not isinstance(conns, list):
        return None, "lotr connections did not answer (%s)" % error_of(data, text)[1][:200]
    row = next((c for c in conns if isinstance(c, dict) and c.get("id") == cid), None)
    if row is None:
        return None, "lotr connections does not list %s" % cid
    try:
        with open(lotr_registry_path(), encoding="utf-8") as fh:
            reg = json.load(fh)
        entry = next(c for c in reg.get("connections") or [] if isinstance(c, dict) and c.get("id") == cid)
        ref = (entry.get("auth") or {}).get("token_ref")
    except (OSError, ValueError, StopIteration, AttributeError) as e:
        return None, "LOTR's registry %s could not be read for %s (%s)" % (lotr_registry_path(), cid,
                                                                           type(e).__name__)
    return {"base_url": row.get("base_url"), "token_ref": ref}, None


# --------------------------------------------------------------------------------------- table row

def _cells(line):
    """The cells of one table line. An escaped pipe (`\\|`) is text inside a cell, not a separator, and is kept
    as written so a row the script does not own round-trips unchanged."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", s)]


def _key(c):
    return re.sub(r"[\s-]+", "_", c.strip("*` ").lower())


def locate_table(text):
    """-> (header_idx, keys, [(line_idx, cells)], last_idx) for the first Markdown table with a `name` column,
    or None. The same rule as the farm's reader: the table ends at the first line that does not start with |."""
    lines = text.split("\n")
    header = None
    rows = []
    for i, line in enumerate(lines):
        s = line.strip()
        if not s.startswith("|"):
            if header is not None and rows:
                break
            header = None
            continue
        cells = _cells(s)
        if header is None:
            keys = [_key(c) for c in cells]
            if "name" in keys:
                header = (i, keys)
            continue
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        rows.append((i, cells))
    if header is None:
        return None
    last = rows[-1][0] if rows else header[0] + 1
    return header[0], header[1], rows, last


def row_values(a):
    vals = {"name": a.name, "transport": "lotr", "connection": connection_id(a), "op": OP, "model": a.model,
            "provider": provider_label(a), "tier": "remote", "trust": "read-only"}
    if a.rate is not None:
        vals["reference_rate_per_mtok"] = str(a.rate)
    return vals


def edit_table(text, a):
    """-> (new_text, action, problem). action is 'added', 'updated' or 'unchanged'; problem is a plain-words
    reason the table cannot take the row (and new_text is then None)."""
    loc = locate_table(text)
    if loc is None:
        return None, None, "the service table has no Markdown table with a `name` column"
    hdr_idx, keys, rows, last = loc
    missing = [k for k in REQUIRED_COLUMNS if k not in keys]
    if missing:
        return None, None, ("the service table has no %s column: add %s to its header row (and a blank cell to "
                            "each row) and run this again" % ("/".join("`%s`" % m for m in missing),
                                                              "it" if len(missing) == 1 else "them"))
    vals = row_values(a)
    cells = [vals.get(k, "") for k in keys]
    new_line = "| " + " | ".join(cells) + " |"
    lines = text.split("\n")
    name_at = keys.index("name")
    for i, cs in rows:
        if len(cs) > name_at and cs[name_at].strip("` ") == a.name:
            # keep columns this script does not own (pool, notes ...) as the person set them; `local` is always
            # cleared, because a hosted row that says `local: yes` would be routed as a home engine
            old = dict(zip(keys, cs))
            merged = [vals[k] if k in vals else "" if k in OWNED_EMPTY else old.get(k, "") for k in keys]
            if [old.get(k, "") for k in keys] == merged and len(cs) == len(keys):
                return text, "unchanged", None
            lines[i] = "| " + " | ".join(merged) + " |"
            return "\n".join(lines), "updated", None
    lines.insert(last + 1, new_line)
    return "\n".join(lines), "added", None


class Changed(Exception):
    """The service table changed on disk after this script read it."""


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def queue_write(vault, rel, text, services=None, base_text=None):
    """Replace the services file through the vault write queue and drain it -> (ok, message). With `services`
    and `base_text`, the file is read again immediately before queuing and Changed is raised if it differs from
    what was edited (the queue records the file's hash at enqueue time, so a change before then would otherwise
    be overwritten without a conflict)."""
    q = os.path.join(hooks_dir(), "gt_write_queue.py")
    b = os.path.join(hooks_dir(), "gt_broker.py")
    if not (os.path.isfile(q) and os.path.isfile(b)):
        return False, "gt's write queue was not found in %s" % hooks_dir()
    tmp = tempfile.mkdtemp(prefix="gt-hosted-")
    try:
        f = os.path.join(tmp, "services.md")
        with open(f, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        if services is not None and _read(services) != base_text:
            raise Changed()
        p = subprocess.run([sys.executable, q, "--vault", vault, "--path", rel, "--op", "replace-file",
                            "--content-file", f, "--hint", "hosted engine row"], capture_output=True, text=True)
        if p.returncode != 0:
            return False, "the write queue refused: " + (p.stdout + p.stderr).strip()[-300:]
        d = subprocess.run([sys.executable, b, "drain", "--vault", vault], capture_output=True, text=True)
        if d.returncode != 0:
            return False, "the queue did not drain: " + (d.stdout + d.stderr).strip()[-300:]
        return True, (d.stdout.strip().splitlines() or ["queued"])[-1]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def row_state(services, name):
    """-> ('absent'|'bad'|'ok', why). 'ok' means the row parses and builds an engine."""
    try:
        rows = fs._parse_table(services)
    except SystemExit as e:
        return "bad", str(e)
    row = next((r for r in rows if r.get("name") == name), None)
    if row is None:
        return "absent", ""
    try:
        eng = fs.engine_from_row(row)
    except SystemExit as e:
        return "bad", str(e)
    if eng.get("transport") != "lotr":
        return "bad", "the row's transport is %r, not lotr" % eng.get("transport")
    return "ok", ""


def row_matches(services, a):
    """True when the table on disk has the `a.name` row with exactly the values this script writes (the cells
    in MATCHED, and the rate when one was given), so an older row left by a held write is not taken as ours."""
    try:
        rows = fs._parse_table(services)
    except SystemExit:
        return False
    row = next((r for r in rows if r.get("name") == a.name), None)
    if row is None:
        return False
    want = row_values(a)
    rate = "reference_rate_per_mtok"
    keys = MATCHED + ((rate,) if rate in want and rate in row else ())
    return all(row.get(k, "") == want[k] for k in keys)


# ------------------------------------------------------------------------------------------- steps

def step_secret(a, lotr, ref, name):
    if not a.replace_key and secret_present(lotr, ref):
        say("secret", "skipped", "%s already holds a value (pass --replace-key to type a new one)" % ref)
        return True
    args = ["secret", "put", name] + (["--force"] if a.replace_key else [])
    say("secret", "next", "LOTR will now ask for the key. It is not shown, and this script never sees it.")
    rc = run_lotr_with_terminal(lotr, args)
    if rc != 0:
        say("secret", "failed", "nothing was stored. To try again: lotr secret put %s%s"
            % (name, " --force" if a.replace_key else ""))
        return False
    say("secret", "done", "the key is stored as %s (typed at LOTR's own prompt)" % ref)
    return True


def step_connection(a, lotr, ref):
    cid = connection_id(a)
    args = ["add-http", cid, "--from-template", TEMPLATE, "--base-url", a.base_url, "--token-ref", ref]
    rc, data, text = run_lotr(lotr, args)
    if rc == 0 and isinstance(data, dict) and data.get("ok", True):
        say("connection", "done", "%s added from the %s template" % (cid, TEMPLATE))
        return True
    code, msg, hints = error_of(data, text)
    if code == "duplicate_connection":
        # anything that can run lotr can make a connection, so an existing one is used only when it sends the
        # key this script stores to the host the person asked for, and a re-run with a new address is not lost
        have, why = existing_connection(lotr, cid)
        wanted = "base_url %s, token_ref %s" % (a.base_url, ref)
        if have is None:
            say("connection", "failed", "%s already exists but could not be compared with what you asked for "
                "(%s). Asked for: %s. Check it with: lotr connections" % (cid, why, wanted))
            return False
        found = "base_url %s, token_ref %s" % (have.get("base_url"), have.get("token_ref"))
        if have.get("base_url") != a.base_url or have.get("token_ref") != ref:
            say("connection", "failed", "%s already exists and does NOT match. It has: %s. You asked for: %s. "
                "Nothing was sent and no key was used. If the existing one is not yours or is out of date, remove "
                "its entry from LOTR's registry (%s) and run this again" % (cid, found, wanted,
                                                                         lotr_registry_path()))
            return False
        say("connection", "skipped", "%s already exists with the same %s, so it is not added again"
            % (cid, found))
        return True
    say("connection", "failed", "%s: %s. To resume: lotr add-http %s --from-template %s --base-url %s "
        "--token-ref %s%s" % (code, msg, cid, TEMPLATE, a.base_url, ref,
                              ("  (" + "; ".join(hints[:2]) + ")") if hints else ""))
    return False


def step_row(a):
    services = os.path.abspath(os.path.expanduser(a.services))
    vault = os.path.abspath(os.path.expanduser(a.vault))
    rel = os.path.relpath(services, vault).replace(os.sep, "/")
    if rel.startswith("..") or os.path.isabs(rel):
        say("row", "failed", "the service table must be inside the vault %s (it is %s)" % (vault, services))
        return False
    try:
        text = _read(services)
    except OSError as e:
        say("row", "failed", "cannot read the service table: %s" % e)
        return False
    new, action, problem = edit_table(text, a)
    if problem:
        say("row", "failed", problem)
        return False
    by_hand = "| " + " | ".join(row_values(a).get(k, "") for k in locate_table(text)[1]) + " |"
    if action == "unchanged":
        say("row", "skipped", "the %s row is already as it should be" % a.name)
    else:
        say("row", "note", "writing through the vault write queue; draining it also applies any other sessions' "
            "queued writes, with their usual claim and conflict checks")
        try:
            ok, msg = queue_write(vault, rel.replace(os.sep, "/"), new, services=services, base_text=text)
        except Changed:
            say("row", "failed", "the service table changed while this ran, so nothing was queued (your edit "
                "stays). Run this again, or add this row by hand: " + by_hand)
            return False
        except OSError as e:
            ok, msg = False, "the service table could not be read again (%s)" % e
        if not ok:
            say("row", "failed", msg + ". Your service table was not changed. Add this row by hand: " + by_hand)
            return False
    state, why = row_state(services, a.name)
    if state == "ok" and not row_matches(services, a):
        say("row", "failed", "the write was held as a conflict or otherwise not applied (see the #conflict task "
            "and spool/broker/conflicts in the vault): the table on disk does not have this row as asked. "
            "Add it by hand: " + by_hand)
        return False
    if state != "ok":
        say("row", "failed", "the row does not read back as a usable engine: %s" % (why or "it is missing"))
        return False
    if action != "unchanged":
        say("row", "done", "the %s row was %s in %s (read-only: results are held for your review until you set "
            "`trust: autonomous`)" % (a.name, action, rel))
    return True


def confirm(question):
    try:
        return input(question + " [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, OSError):
        return False


def step_check(a):
    if a.no_send:
        say("check", "skipped", "no check packet (--no-send)")
        return True
    if not a.yes and not confirm("Send ONE tiny public check packet through %s now? It costs a fraction of a cent"
                                 % a.name):
        say("check", "skipped", "not sent. Later: gt_farm_send.py send --service %s PACKET" % a.name)
        return True
    # a folder of our own (mkdtemp: mode 700, a fresh name), so no planted file or symlink is ever written through;
    # on success it is kept with only the result in it, and the message names that file
    tmp = tempfile.mkdtemp(prefix="gt-hosted-check-")
    keep = False
    try:
        packet = os.path.join(tmp, "check.md")
        with open(packet, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(CHECK_PACKET)
        out = os.path.join(tmp, "check-result.md")
        p = subprocess.run([sys.executable, os.path.join(HERE, "gt_farm_send.py"), "send", "--service", a.name,
                            "--services", os.path.abspath(os.path.expanduser(a.services)), "--vault",
                            os.path.abspath(os.path.expanduser(a.vault)), "--max-tokens", "800", "--out", out,
                            packet], capture_output=True, text=True)
        text = (p.stdout + p.stderr).strip()
        tail = "\n".join(text.splitlines()[-6:])
        if p.returncode == 0:
            keep = os.path.isfile(out)
            if keep:
                os.chmod(out, 0o600)
                os.remove(packet)
            say("check", "done", "the engine answered and the contract held. Result file (held for review, "
                "outside the vault): %s\n%s" % (out if keep else "(none was written)", tail))
            return True
        hint = ""
        if re.search(r"gateway|app-connect|enrol|socket", text, re.I):
            hint = " If the gateway is not enrolled for the farm, run once: lotr app-connect"
        say("check", "failed", "the send did not succeed (exit %d).%s\n%s" % (p.returncode, hint, tail))
        return False
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------------------------- modes

def plan_lines(a):
    ref = "store:" + secret_name(a)
    vals = row_values(a)
    return [
        "secret      lotr secret put %s   (typed by you at LOTR's hidden prompt)" % secret_name(a),
        "connection  lotr add-http %s --from-template %s --base-url %s --token-ref %s"
        % (connection_id(a), TEMPLATE, a.base_url, ref),
        "row         in %s: %s" % (a.services, " | ".join("%s=%s" % (k, v) for k, v in vals.items())),
        "check       one public packet through the row (asks first)",
        "the key will be sent as a bearer token to %s (https); a real run asks you to type a code first"
        % host_of(a.base_url),
    ]


def do_check(a, lotr):
    ref = "store:" + secret_name(a)
    services = os.path.abspath(os.path.expanduser(a.services))
    nxt = None
    if lotr is None:
        say("lotr", "failed", "gt-lotr was not found; install the gt-lotr module (or set GT_LOTR_SCRIPTS)")
        return 1
    say("lotr", "done", "gt-lotr found")
    if secret_present(lotr, ref):
        say("secret", "done", "%s holds a value (not read)" % ref)
    else:
        say("secret", "failed", "%s holds no value" % ref)
        nxt = nxt or "lotr secret put %s" % secret_name(a)
    rc, data, text = run_lotr(lotr, ["status"])
    conns = data.get("connections") if isinstance(data, dict) else None
    ids = [c.get("id") for c in conns if isinstance(c, dict)] if isinstance(conns, list) else []
    if rc == 0 and connection_id(a) in ids:                     # the exact id: ai@x never matches openai@x
        say("connection", "done", "%s is registered" % connection_id(a))
    else:
        say("connection", "failed", "%s is not registered" % connection_id(a))
        nxt = nxt or "this script without --check"
    if not os.path.isfile(services):
        say("row", "failed", "the service table %s does not exist" % services)
        nxt = nxt or "create the service table"
    else:
        state, why = row_state(services, a.name)
        if state == "ok":
            say("row", "done", "the %s row reads back as a usable lotr engine" % a.name)
        else:
            say("row", "failed", "the %s row is %s%s" % (a.name, "missing" if state == "absent" else "unusable",
                                                          (": " + why) if why else ""))
            nxt = nxt or "this script without --check"
    if nxt:
        say("next", "note", "run: %s" % nxt)
        return 1
    say("next", "note", "all in place. If a send fails with a gateway error, enrol the farm once: lotr app-connect")
    return 0


def main(argv=None):
    a = parse_args(sys.argv[1:] if argv is None else argv)
    problems = validate(a)
    if problems:
        for p in problems:
            sys.stderr.write("refused: %s\n" % p)
        return 2
    if a.dry_run:
        say("plan", "note", "a dry run: nothing is changed, nothing is sent, no key is asked for")
        for line in plan_lines(a):
            sys.stdout.write("  " + line + "\n")
        return 0
    lotr = lotr_command()
    if a.check:
        return do_check(a, lotr)
    if not has_terminal():
        sys.stderr.write("refused: this asks for your API key at a hidden prompt, so it must run in YOUR OWN "
                         "terminal, not through Claude or a script. Open a terminal and run it there "
                         "(--check and --dry-run need no terminal).\n")
        return 2
    if lotr is None:
        say("lotr", "failed", "gt-lotr was not found; install the gt-lotr module (or set GT_LOTR_SCRIPTS)")
        return 1
    name, ref = secret_name(a), "store:" + secret_name(a)
    host = host_of(a.base_url)
    # the person gate, before the first lotr call: isatty() alone is passed by `script -q /dev/null`, so the
    # person types back a code shown on the terminal itself. --yes never skips this.
    say("confirm", "next", "the key in %s will be sent as a bearer token to %s (https), through LOTR connection "
        "%s (%s). A key already stored there is used as it is." % (ref, host, connection_id(a), a.base_url))
    bad = typed_challenge("gt-farm: set up %s: the key in %s goes as a bearer token to %s (https)"
                          % (a.name, ref, host))
    if bad:
        sys.stderr.write("%s\n" % bad)
        return 2
    for step in (lambda: step_secret(a, lotr, ref, name), lambda: step_connection(a, lotr, ref),
                 lambda: step_row(a), lambda: step_check(a)):
        if not step():
            return 1
    say("done", "note", "set up. Results from this engine are held for your review (trust: read-only); to file them "
        "automatically set `trust: autonomous` on the row after calibrating it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
