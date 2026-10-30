#!/usr/bin/env python3
"""install_guard (0.22.0): scan what a shell command is about to install, BEFORE it installs it.

    gt_install_guard.py                        # the PreToolUse hook (payload on stdin)
    gt_install_guard.py scan <spec> [--json]   # scan by hand: a URL, owner/repo, npm:pkg, pypi:pkg
    gt_install_guard.py scan --command "<cmd>" # every install target one command line names

## Why

A `git clone`, `npx`, `pip install` or a download piped to a shell, typed by the model, brings in code nobody
has read: a postinstall hook that fetches a second stage, a SKILL.md that tells the next session
to ignore its rules, a reverse shell. gt_intake_scan already catches those in material handed to
ingest; this puts the same scan in front of the install (tool-vetting plan A3, scorecard item 7).

## What it recognises (Bash and PowerShell)

git clone (options before AND after the URL), git pull URL, git submodule add URL, gh repo clone;
npx / bunx / pnpx / uvx / npm exec|x / pnpm|yarn dlx / pipx run|install / uv tool install|run /
uv run --with; pip / pip3 / python -m pip / uv pip install / uv add, global options before the verb
(git+, URL, archive AND plain registry names); npm|pnpm|yarn|bun i|in|ins|install|add|it|ci and
yarn global add, global flags before the verb (github:, git URL, tarball URL AND registry names);
deno run|install (npm: specifiers are scanned); claude mcp add (the server command is parsed
again); claude plugin marketplace add; a curl / wget / iwr / irm download piped to a shell or
interpreter, handed to one through a process or command substitution ($( ), backticks), saved to a
file that the same command then runs, or held in a variable that is then evaluated (bash, sh -c,
python -c, eval, source, iex, [scriptblock]::Create); powershell -EncodedCommand (decoded and
parsed); cmd /c. An interpreter given -c / -e / -m or a script file reads its stdin as DATA, so a
download piped to `python3 -c '...'` is not a pipe-to-shell. Heredoc bodies and quoted arguments (an
echo of such a line) are text, not commands, unless the heredoc feeds a shell or is written to a
file the command then runs.

The command is NORMALISED before the pre-check: backslash-newline continuations are joined and
quote and escape characters are dropped, so `| "bash"`, `| b\\ash` and `g'i't clone` are seen.
Wrapper words (sudo -u x, env, command, nohup, exec, xargs, time, then/do/else, ...) are skipped
and runners (watch, find -exec, script, ...) are looked inside.

An install-like command this guard cannot analyse -- a source computed at run time, an option
it does not understand, an ecosystem it does not fetch (go, cargo, gem, deno URLs), a parse
failure -- ASKS in both modes: an install nothing scanned is not a pass. Partial coverage (a
requirements file, a custom index, the dependencies a package pulls in, winget/choco/scoop) is a
NOTE.

## How the material is fetched -- nothing from it is ever executed

  * git: a hardened shallow clone into a fresh 0700 temp dir: https only, no symlinks, an empty
    hooksPath, no credential helper, no submodules, no prompts, no system or global config (only
    an allow-list of TLS and proxy keys is carried over from the user's config).
  * npm / PyPI: the registry JSON and the tarball or the wheel pip would pick for this
    interpreter and platform (the sdist when none fits) over urllib, then a safe extract (regular
    files and directories only, no `..`, size and member caps, the time budget checked inside the
    loop). Never `pip download` (an sdist runs setup.py) and never `npm pack`.
  * every download host is resolved first and refused if it is private, loopback or link-local;
    every redirect is checked BEFORE it is followed (https only, public hosts only).
  * a download piped to a shell: nothing is fetched. The pipe is itself the serious finding.
  * Child processes run in their own process group and the whole group is killed on a timeout.
  * The temp dir is always removed (with a read-only retry for Windows).

The scan is gt_intake_scan.py --json [--deadline] in a subprocess; the whole check has BUDGET_S,
and a watchdog asks before the hook's own timeout could fire.

## What it decides (`install_guard` setting: off | warn | block, default warn)

  clean                 nothing printed, exit 0 (a guard never says "allow")
  serious finding       warn: ask   block: deny      (reason: kind, rule, file:line -- never text)
  other findings        additionalContext notes, in both modes (credential findings are notes)
  timeout / incomplete  ask, in both modes
  a gt grant covers it  silent -- only when EVERY install-like part of the command is covered by
                        a grant on a word boundary (gt_grant); the PostToolUse spend hook uses a `once`

Serious: code.pipe-download-to-shell, code.shell-runs-download, code.exec-of-fetched,
code.exec-of-decoded, code.reverse-shell, code.install-hook-fetches, and every prompt-injection.

Stdlib only. The pre-check is one linear-time regex over the normalised command; a command that
does not match returns before any other module is imported.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.realpath(__file__))

# 0.22.0: a repeat counter on this guard's refusals (gt_refusals.py, setting refusal_limit), as in the other
# guards. Fails open: without the module beside this file, or on any error, the deny text is exactly what it was.
sys.dont_write_bytecode = True          # the hooks folder is signed: importing a sibling must not leave a __pycache__ in it
try:
    sys.path.insert(0, HERE)
    import gt_refusals as _gtr
except Exception:                                            # noqa: BLE001
    _gtr = None


def _annotated(reason):
    try:
        return _gtr.annotate("guard_install", reason) if _gtr else reason
    except Exception:                                        # noqa: BLE001
        return reason


def _use_session(payload):
    try:
        if _gtr:
            _gtr.use_session(payload)
    except Exception:                                        # noqa: BLE001
        pass


SETTING = "install_guard"
MODES = ("off", "warn", "block")
DEFAULT_MODE = "warn"
BUDGET_S = 50.0              # the whole check: every fetch and scan (the hook's timeout is 75)
WATCHDOG_EXTRA_S = 12.0      # the watchdog asks at budget + this, still under the hook's timeout
MAX_TARGETS = 8              # more than this in one command: the rest is incomplete -> ask
DOWNLOAD_CAP = 100 * 1024 * 1024
EXTRACT_CAP = 400 * 1024 * 1024
EXTRACT_MEMBERS = 20000
CHUNK = 1024 * 1024

TEST_ENV = "GT_INSTALL_GUARD_TEST"           # test-only: file:// sources and fixture overrides
TEST_REGISTRY_ENV = "GT_INSTALL_GUARD_TEST_REGISTRY"
TEST_SCANNER_ENV = "GT_INSTALL_GUARD_TEST_SCANNER"
BUDGET_ENV = "GT_INSTALL_GUARD_BUDGET"

NPM_REGISTRY = "https://registry.npmjs.org"
PYPI_REGISTRY = "https://pypi.org/pypi"
# What a real client sends: a server that answers the guard differently from the installer
# should not be able to tell them apart by the User-Agent.
USER_AGENTS = {"npm": "npm/10.9.2 node/v22.12.0 workspaces/false", "pypi": "pip/24.3.1",
               "other": "curl/8.7.1"}

SERIOUS_RULES = frozenset((
    "code.pipe-download-to-shell", "code.shell-runs-download", "code.exec-of-fetched",
    "code.exec-of-decoded", "code.reverse-shell", "code.install-hook-fetches"))

# -- the pre-check: everything above the parser hangs on this being cheap ---------------------
# Literal words only (no nested quantifiers), so it is linear in the command's length. It runs on
# the NORMALISED command (continuations joined, quotes and escapes dropped).
PRECHECK = re.compile(
    r"clone|install|pip|npx|bunx|pnpx|uvx|dlx|github:|marketplace|submodule|scriptblock"
    r"|downloadstring|downloadfile|downloaddata|webclient|/dev/stdin"
    r"|\b(?:npm|pnpm|yarn|bun|uv|deno|cargo|gem|go|gh|mcp|winget|choco|scoop|pull|curl|wget"
    r"|iwr|irm|iex|invoke-webrequest|invoke-restmethod|invoke-expression|start-bitstransfer"
    r"|certutil|bitsadmin|eval|powershell|pwsh|cmd)\b",
    re.I)
_FLAT = {ord(c): None for c in "'\"\\`^"}


def normalise(command):
    """The command as the pre-check sees it: line continuations joined, quote and escape
    characters dropped (`| "bash"`, `| b\\ash`, `g'i't`, PowerShell's i`ex, cmd's c^url)."""
    s = command.replace("\\\r\n", "").replace("\\\n", "").replace("`\r\n", "").replace("`\n", "")
    return s.translate(_FLAT)


def precheck(command):
    """True when the command MIGHT install something. False is final."""
    return bool(command) and PRECHECK.search(normalise(command)) is not None


def _test_mode():
    return os.environ.get(TEST_ENV) == "1"


# ============================================================================ the parser
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "ash", "mksh", "busybox"}
INTERPRETERS = SHELLS | {"python", "python2", "python3", "py", "pypy", "pypy3", "perl", "ruby",
                         "node", "php", "pwsh", "powershell", "iex", "invoke-expression", "deno",
                         "bun", "tclsh", "lua", "osascript"}
FETCHERS = {"curl", "wget", "iwr", "irm", "invoke-webrequest", "invoke-restmethod",
            "start-bitstransfer", "certutil", "bitsadmin", "aria2c", "fetch", "http", "xh"}
WRAPPERS = {"sudo", "env", "command", "time", "nohup", "exec", "nice", "caffeinate", "builtin",
            "doas", "stdbuf", "timeout", "gtimeout", "!", "xargs", "then", "do", "else", "elif",
            "if", "while", "until", "coproc", "noglob", "nocorrect"}
WRAPPER_VALUE_OPTS = {"sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T", "-R"},
                      "env": {"-u", "-C", "-S", "--unset", "--chdir"}, "nice": {"-n"},
                      "doas": {"-u", "-C"}, "stdbuf": {"-i", "-o", "-e"},
                      "timeout": {"-s", "-k", "--signal", "--kill-after"},
                      "gtimeout": {"-s", "-k", "--signal", "--kill-after"},
                      "xargs": {"-I", "-L", "-n", "-P", "-d", "-E", "-e", "-s", "-a",
                                "--max-args", "--max-procs", "--delimiter", "--arg-file",
                                "--max-lines", "--replace", "--eof", "--max-chars"}}
# Programs that run a command given to them as arguments: the command is looked for inside.
RUNNERS = {"watch", "script", "find", "parallel", "setsid", "ionice", "chronic", "unbuffer",
           "taskset", "chrt", "flock", "strace", "ltrace", "proxychains", "proxychains4",
           "torsocks", "tsocks", "su", "runuser", "systemd-run", "start-process", "entr",
           "fakeroot", "firejail", "arch", "daemonize", "retry", "sg", "unshare", "chroot",
           "start", "ssh-agent", "dbus-run-session", "with-proxy"}
KNOWN_PROGS = {"git", "gh", "npx", "bunx", "pnpx", "npm", "pnpm", "yarn", "bun", "uvx", "uv",
               "pipx", "pip", "pip3", "python", "python3", "py", "claude", "deno", "go", "cargo",
               "gem", "eval", "source", "cmd", "winget", "choco", "scoop"} | FETCHERS | SHELLS | \
    {"pwsh", "powershell", "iex", "invoke-expression"}
SEPARATORS = {";", "&&", "||", "&", "|", "|&", "(", ")", "<(", ">(", "$(", ";;", "{", "}", "\n"}
PIPES = ("|", "|&")
REDIRECTS = {"<", ">", ">>", "&>", ">&", "<&", "<<", "<<<", "&>>", ">|"}
PUNCT = ["<<<", "&>>", "<(", ">(", "&&", "||", "|&", ";;", ">>", "&>", ">&", "<&", "<<", ">|",
         ";", "&", "|", "(", ")", "<", ">"]
ARCHIVE = re.compile(r"\.(?:tar\.gz|tgz|tar\.bz2|tbz2?|tar\.xz|txz|tar|zip|whl)(?:[#?].*)?$", re.I)
_FETCH_NAMES = r"curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod|aria2c|xh"
FETCH_SUBST = re.compile(r"(?:\$\(|`|<\()\s*(?:sudo\s+)?\\?(?:[\w.~/-]*/)?(?:" + _FETCH_NAMES +
                         r")\b", re.I)
INSTALLISH = re.compile(r"clone|install|npx|bunx|uvx|pipx|pip|dlx|\badd\b|curl|wget|\biwr\b|"
                        r"\birm\b|\biex\b|\bpull\b|submodule|invoke-|downloadstring", re.I)
# PowerShell statement-level rules (each statement is checked with `search`, never a nested scan)
PS_FETCH = re.compile(r"\b(?:iwr|irm|invoke-webrequest|invoke-restmethod|curl|wget|"
                      r"start-bitstransfer)\b|downloadstring|downloaddata|downloadfile|"
                      r"net\.webclient|system\.net\.http", re.I)
PS_EXEC = re.compile(r"\b(?:iex|invoke-expression)\b|\[scriptblock\]::create|\.invokescript\(|"
                     r"invoke-command\b[^;]{0,40}-scriptblock", re.I)
PS_CALLOP = re.compile(r"(?:^|[\s|({;])[&.]\s*[($]")
ASSIGN_FETCH = re.compile(r"(?:^|[\s;&|(])([A-Za-z_]\w*)=[\"']?(?:\$\(|`)\s*(?:sudo\s+)?\\?"
                          r"(?:[\w.~/-]*/)?(?:" + _FETCH_NAMES + r")\b", re.I)
PS_ASSIGN = re.compile(r"\$(\w+)\s*=", re.I)
NPM_INSTALL_VERBS = {"i", "in", "ins", "inst", "insta", "instal", "install", "isnt", "isnta",
                     "isntal", "isntall", "add", "a", "it", "install-test", "isntall-test"}
NPM_MANIFEST_VERBS = {"ci", "clean-install", "cit", "install-ci-test", "install-clean", "ic"}
SHIM_EXT = (".exe", ".cmd", ".bat", ".ps1", ".com")


class Unparseable(Exception):
    pass


def strip_heredocs(cmd, p=None):
    """Heredoc bodies are data -- unless the heredoc feeds a shell (`bash <<EOF`, `cat <<EOF |
    sh`, `sudo bash <<EOF`): then it IS commands and is kept. A heredoc written to a file (`tee f
    <<EOF`, `cat > f <<EOF`) is remembered so that a later `bash f` is analysed. Linear: each
    terminator is found with one str.find from where the last heredoc ended."""
    start_rx = re.compile(r"<<-?[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
    out, pos = [], 0
    while True:
        m = start_rx.search(cmd, pos)
        if not m:
            break
        eol = cmd.find("\n", m.end())
        if eol < 0:
            break
        delim = m.group(2)
        k, end_body, after = eol, None, None
        while True:
            nl = cmd.find("\n" + delim, k)
            if nl < 0:
                break
            j = nl + 1 + len(delim)
            rest_end = cmd.find("\n", j)
            tail = cmd[j:rest_end if rest_end >= 0 else len(cmd)]
            if tail.strip(" \t\r") == "":
                end_body, after = nl, (rest_end if rest_end >= 0 else len(cmd))
                break
            k = nl + 1
        if end_body is None:
            # a tab-indented terminator (<<-) or none at all: try the indented form once
            m2 = re.compile(r"\n[ \t]*" + re.escape(delim) + r"[ \t]*(?:\n|$)").search(cmd, eol)
            if not m2:
                break
            end_body, after = m2.start(), m2.end()
        body = cmd[eol + 1:end_body]
        line_start = cmd.rfind("\n", 0, m.start()) + 1
        line = cmd[line_start:m.start()] + " " + cmd[m.end():eol]
        feeds = _heredoc_feeds_shell(line)
        if p is not None:
            for f in _heredoc_writes(line):
                p.heredoc_files[_basename(f)] = body
        out.append(cmd[pos:m.start()] + cmd[m.end():eol])
        out.append("\n" + body + "\n" if feeds else "\n")
        pos = after
    out.append(cmd[pos:])
    return "".join(out)


def _heredoc_feeds_shell(line):
    for part in re.split(r"&&|\|\||[;&]", line)[-1:]:
        for stage in part.split("|"):
            w = stage.split()
            w, _wr = _strip_prefix(w)
            w = [x for x in w if not re.match(r"^\d*[<>]", x)]
            if w and _base(w[0]) in SHELLS | {"pwsh", "powershell"} and _runs_stdin(w):
                return True
    return False


def _heredoc_writes(line):
    out = []
    for m in re.finditer(r"\btee\s+(?:-a\s+)?([^\s|;&<>]+)", line):
        out.append(m.group(1).strip("'\""))
    for m in re.finditer(r"(?<![<\d&])>>?\s*([^\s|;&<>]+)", line):
        out.append(m.group(1).strip("'\""))
    return [x for x in out if x and x != "/dev/null"]


def _preprocess(text, powershell=False):
    """Drop comments and turn unquoted newlines into separators, tracking quotes, so a newline
    inside a quoted argument (a commit message) stays text. Unquoted backticks become $( ).
    Raises Unparseable on open quotes."""
    esc = "`" if powershell else "\\"
    out, i, n, state, tick = [], 0, len(text), None, False
    while i < n:
        c = text[i]
        if state is None:
            if c == esc and i + 1 < n:
                if text[i + 1] == "\n":          # a line continuation
                    i += 2
                    continue
                if text[i + 1] == "\r" and text[i + 2:i + 3] == "\n":
                    i += 3
                    continue
                out.append(text[i + 1] if powershell else c + text[i + 1])
                i += 2
                continue
            if c == "`" and not powershell:
                out.append(" ) " if tick else " $( ")
                tick = not tick
                i += 1
                continue
            if c == "'":
                state = "'"
            elif c == '"':
                state = '"'
            elif c == "#" and (not out or out[-1][-1:] in " \t\n;&|()<>"):
                while i < n and text[i] != "\n":
                    i += 1
                continue
            elif c == "\n":
                out.append(" ; ")
                i += 1
                continue
            out.append(c)
        elif state == "'":
            out.append(c)
            if c == "'":
                state = None
        else:
            if c == esc and i + 1 < n:
                if text[i + 1] == "\n":
                    i += 2
                    continue
                out.append(c + text[i + 1])
                i += 2
                continue
            out.append(c)
            if c == '"':
                state = None
        i += 1
    if state is not None or tick:
        raise Unparseable("an unbalanced quote")
    return "".join(out)


def _split_punct(t):
    out = []
    while t:
        for p in PUNCT:
            if t.startswith(p):
                out.append(p)
                t = t[len(p):]
                break
        else:
            out.append(t[0])
            t = t[1:]
    return out


def _tokens(text, powershell=False):
    import shlex
    lx = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>")
    lx.whitespace = " \t\r\n"
    lx.whitespace_split = True
    lx.commenters = ""
    if powershell:
        lx.escape = ""
    try:
        raw = list(lx)
    except ValueError:
        raise Unparseable("an unbalanced quote")
    out = []
    for t in raw:
        if t and set(t) <= set(";&|()<>"):
            out.extend(_split_punct(t))
        else:
            out.append(t)
    return out


def _segments(toks):
    """-> [{"toks": [...], "before": sep, "after": sep}]. `$` + `(` is a command substitution."""
    segs, cur, before = [], [], None
    for k, t in enumerate(toks):
        if t == "(" and cur and cur[-1].endswith("$"):
            last = cur.pop()[:-1]
            if last:
                cur.append(last)
            t = "$("
        if t in SEPARATORS:
            segs.append({"toks": cur, "before": before, "after": t})
            cur, before = [], t
        else:
            cur.append(t)
    segs.append({"toks": cur, "before": before, "after": None})
    return segs


def _base(w):
    b = w.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for ext in SHIM_EXT:
        if b.endswith(ext) and len(b) > len(ext):
            return b[:-len(ext)]
    return b


def _basename(w):
    return w.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _strip_redirects(seg):
    out, i = [], 0
    while i < len(seg):
        t = seg[i]
        if t in REDIRECTS:
            i += 2
            continue
        if t.isdigit() and i + 1 < len(seg) and seg[i + 1] in REDIRECTS:
            i += 1
            continue
        out.append(t)
        i += 1
    return out


def _strip_prefix(seg):
    """-> (the command with assignments and wrapper words removed, [wrapper words seen])."""
    i, seen = 0, []
    while i < len(seg):
        w = seg[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):
            i += 1
            continue
        b = _base(w)
        if b in WRAPPERS:
            seen.append(b)
            i += 1
            vals = WRAPPER_VALUE_OPTS.get(b, set())
            while i < len(seg) and seg[i].startswith("-") and seg[i] != "-":
                i += 2 if (seg[i] in vals and "=" not in seg[i]) else 1
            if b in ("timeout", "gtimeout") and i < len(seg) and re.match(r"^[\d.]+[smhd]?$", seg[i]):
                i += 1
            if b == "env":
                while i < len(seg) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[i]):
                    i += 1
            continue
        break
    return seg[i:], seen


def _dynamic(tok):
    return "$" in tok or "`" in tok


STDIN_EXEC = re.compile(r"\b(?:exec|eval|compile|Function|vm\.run\w*|instance_eval|"
                        r"system)\s*\(?[^;]{0,80}?(?:stdin|STDIN|readlines?\(|\$<|<STDIN>|"
                        r"ARGF|fs\.readFileSync\(0)")
STDIN_FILES = ("-", "/dev/stdin", "/dev/fd/0", "/proc/self/fd/0")


def _runs_stdin(seg):
    """Does this interpreter execute what arrives on its stdin? `python3 -c CODE`, `node f.js`,
    `python3 -m json.tool`, `perl -ne ...` read stdin as DATA; `sh /dev/stdin` runs it."""
    b, a = _base(seg[0]), seg[1:]
    if b in ("iex", "invoke-expression"):
        return True
    lo = [x.lower() for x in a]
    if b in ("pwsh", "powershell"):
        for k, x in enumerate(lo):
            if x in ("-c", "-command", "-file", "-f", "-encodedcommand", "-ec", "-e"):
                return lo[k + 1:k + 2] == ["-"]          # `-Command -` reads commands from stdin
        return True
    if b in SHELLS:
        code = re.compile(r"^-[a-z]*c[a-z]*$")
    elif re.match(r"^(?:python|pypy)[0-9.]*$", b) or b == "py":
        code = re.compile(r"^-[a-z]*[cm]$")
    else:                                               # node, perl, ruby, php, deno, bun
        code = re.compile(r"^(?:-[a-z]*[eprE][a-z]*|--eval|--print|-r)$")
    if any(code.match(x) for x in a):
        return False
    if "-s" in a or any(x in STDIN_FILES for x in a):
        return True
    return not [x for x in a if not x.startswith("-")]


SHELL_C = re.compile(r"^-[a-zA-Z]*c[a-zA-Z]*$")


def _c_index(prog, args):
    """Index of the option whose next argument is a command string, or None."""
    for n, a in enumerate(args):
        if prog in SHELLS and SHELL_C.match(a):
            return n
        if prog in ("pwsh", "powershell") and a.lower() in ("-c", "-command"):
            return n
        if not a.startswith("-"):
            return None
    return None


def _code_arg(prog, args):
    """The code string an interpreter is handed on its command line (-c, -e, --eval, -r), or
    None."""
    if re.match(r"^(?:python|pypy)[0-9.]*$", prog) or prog == "py":
        rx = re.compile(r"^-[A-Za-z]*c$")
    elif prog in SHELLS or prog in ("pwsh", "powershell"):
        k = _c_index(prog, args)
        return args[k + 1] if k is not None and k + 1 < len(args) else None
    else:
        rx = re.compile(r"^(?:-[A-Za-z]*[eEpr]|--eval|--print)$")
    for n, a in enumerate(args):
        if rx.match(a):
            return args[n + 1] if n + 1 < len(args) else None
        if prog in ("deno", "bun") and a == "eval":
            return args[n + 1] if n + 1 < len(args) else None
    return None


def _is_interp(prog):
    return prog in INTERPRETERS or bool(re.match(r"^(?:python|pypy)[0-9.]*$", prog))


def redact(a):
    a = re.sub(r"(?i)((?:https?|git|ssh)://)[^/@\s]+@", r"\1<cred>@", a)
    a = re.sub(r"(?i)([?&](?:token|key|sig|signature|access_token|auth|password)=)[^&\s#]+",
               r"\1<redacted>", a)
    return "".join(ch for ch in a if ch.isprintable())[:120]


class Parsed(object):
    def __init__(self):
        self.targets = []       # dicts: type git|npm|pypi|archive, label, ...
        self.findings = []      # command-level findings (a download piped to a shell)
        self.notes = []         # partial coverage: shown, never a stop
        self.unscanned = []     # an install this guard could not analyse: asks in both modes
        self.aliases = {}
        self.heredoc_files = {}
        self.fetch_vars = set()
        self.remotes = {}
        self._seen = set()                  # every list dedups through one set: linear

    def _once(self, key):
        if key in self._seen:
            return False
        self._seen.add(key)
        return True

    def target(self, t):
        if self._once(("t", t["type"], t.get("url") or t.get("name"), t.get("version"),
                       t.get("branch"))):
            self.targets.append(t)

    def finding(self, rule, what):
        if self._once(("f", rule, what)):
            self.findings.append({"kind": "unsafe-code", "rule": rule,
                                  "file": "(this command: %s)" % what, "line": 0})

    def note(self, text):
        if self._once(("n", text)):
            self.notes.append(text)

    def gap(self, text):
        if self._once(("g", text)):
            self.unscanned.append(text)

    def empty(self):
        return not (self.targets or self.findings or self.notes or self.unscanned)


def parse_command(command, tool="Bash"):
    """-> Parsed: what the command would install, the command-level findings, and notes."""
    p = Parsed()
    _classify(command, p, 0, tool == "PowerShell")
    return p


def _ps_rules(body, p):
    """PowerShell: a download handed to Invoke-Expression or [scriptblock]::Create in one
    statement, through a variable, or to a command computed at run time."""
    stmts = re.split(r"[;\n]", body)
    vars_ = set()
    for st in stmts:
        if PS_FETCH.search(st):
            m = PS_ASSIGN.search(st)
            if m and st.find("=", m.start()) < (PS_FETCH.search(st).start()):
                vars_.add(m.group(1).lower())
            if PS_EXEC.search(st):
                if not p.findings:            # a pipe into iex already said it more precisely
                    p.finding("code.exec-of-fetched", "Invoke-Expression of a download")
            elif PS_CALLOP.search(st):
                p.gap("a download handed to a command computed at run time: not analysed")
    for st in stmts:
        low = st.lower()
        for v in vars_:
            if re.search(r"\$" + re.escape(v) + r"\b", low) and not re.search(
                    r"\$" + re.escape(v) + r"\s*=", low):
                if PS_EXEC.search(st) or re.search(r"(?:^|[\s|(])[&.]\s*\(?\$" + re.escape(v), low):
                    p.finding("code.exec-of-fetched", "Invoke-Expression of a downloaded variable")


def _classify(text, p, depth, ps, stdin_dl=False):
    if depth > 3:
        p.gap("nested shells more than three deep: the innermost command was not analysed")
        return
    body = strip_heredocs(text, p)
    try:
        pre = _preprocess(body, ps)
        segs = _segments(_tokens(pre, ps))
    except Unparseable as exc:
        if INSTALLISH.search(body):
            p.gap("could not parse an install-like command (%s): nothing was scanned" % exc)
        return
    if not ps:
        for m in ASSIGN_FETCH.finditer(pre):
            p.fetch_vars.add(m.group(1))
    for sg in segs:
        for tok in sg["toks"]:                  # x="$(cmd)": the assignment's command runs
            if ("$(" in tok or "`" in tok) and re.match(r"^[A-Za-z_]\w*=", tok) and \
                    not FETCH_SUBST.search(tok):
                for b in _subst_bodies(tok):
                    if precheck(b):
                        _classify(b, p, depth + 1, ps)
        s, wr = _strip_prefix(_strip_redirects(sg["toks"]))
        if ps and len(s) >= 2 and s[0].startswith("$") and s[1] in ("=", "+="):
            s, wr = _strip_prefix(s[2:])                 # $x = <command>
        if s and _base(s[0]) == "alias":
            for a in s[1:]:
                name, eq, val = a.partition("=")
                if eq and re.match(r"^[\w.-]+$", name):
                    p.aliases[name] = val.split()
        elif s and s[0] in p.aliases:
            s, wr2 = _strip_prefix(p.aliases[s[0]] + s[1:])
            wr = wr + wr2
        sg["cmd"], sg["wr"] = s, wr
    downloads, piped_to = set(), -1
    for i, sg in enumerate(segs):
        s = sg["cmd"]
        if not s:
            continue
        prog = _base(s[0])
        starts_pipeline = sg["before"] not in PIPES
        # a download fed to something that runs it: a process or command substitution whose
        # command is a fetcher, handed to a shell, eval, source or iex
        if sg["before"] in ("<(", "$(", "(") and prog in FETCHERS and i > 0 and segs[i - 1]["cmd"]:
            outer = _base(segs[i - 1]["cmd"][0])
            if outer in ("eval", "source", ".") or _is_interp(outer):
                p.finding("code.exec-of-fetched" if outer in ("iex", "invoke-expression")
                          else "code.shell-runs-download", "%s runs %s output" % (outer, prog))
        # stdin is itself a download (the code of `curl ... | bash -c '...'`)
        if stdin_dl and starts_pipeline and _is_interp(prog) and _runs_stdin(s):
            p.finding("code.pipe-download-to-shell", "%s runs a download from its stdin" % prog)
        # a download piped into an interpreter that runs its stdin (through tee etc.)
        if sg["after"] in PIPES and i > piped_to and \
                _is_fetch_source(s, prog, stdin_dl and starts_pipeline):
            j = i
            while segs[j]["after"] in PIPES and j + 1 < len(segs):
                j += 1
                nxt = segs[j]["cmd"]
                if not nxt:
                    continue
                nb = _base(nxt[0])
                if _dynamic(nxt[0]):
                    p.gap("a download is piped to a program computed at run time: not analysed")
                    break
                if _is_interp(nb) and (_runs_stdin(nxt) or "xargs" in segs[j]["wr"]):
                    p.finding("code.pipe-download-to-shell", "%s ... | %s" % (prog, nb))
                    break
                code = _code_arg(nb, nxt[1:]) if _is_interp(nb) else None
                if code and nb not in SHELLS and nb not in ("pwsh", "powershell") and \
                        STDIN_EXEC.search(code):
                    p.finding("code.pipe-download-to-shell", "%s ... | %s runs its stdin" % (prog, nb))
                    break
                if code:
                    _classify(code, p, depth + 1, nb in ("pwsh", "powershell") or ps, True)
            piped_to = j                      # a later download in this pipeline sees less of it
        if prog in FETCHERS:
            downloads |= _fetch_outputs(prog, s[1:])
        elif downloads or p.heredoc_files:
            _runs_saved(s, prog, downloads, p, depth, ps)
        _segment(s, prog, p, depth, ps)
    if ps:
        _ps_rules(body, p)


def _is_fetch_source(s, prog, stdin_dl):
    if prog in FETCHERS:
        return True
    if stdin_dl and prog in ("cat", "tee") and not [a for a in s[1:] if a not in STDIN_FILES
                                                    and not a.startswith("-")]:
        return True
    if prog in ("echo", "printf"):
        return False
    return any(re.match(r"^https?://", a, re.I) for a in s[1:])


def _fetch_outputs(prog, args):
    """Basenames of the files a download command writes."""
    out, urls, i, named = set(), [], 0, False
    while i < len(args):
        a, lo = args[i], args[i].lower()
        nxt = args[i + 1] if i + 1 < len(args) else ""
        if re.match(r"^https?://", a, re.I):
            urls.append(a)
        if prog == "curl":
            if a.startswith("--"):
                if a == "--output" and nxt:
                    out.add(_basename(nxt))
                    named, i = True, i + 2
                    continue
                if a.startswith("--output="):
                    out.add(_basename(a.split("=", 1)[1]))
                    named = True
                elif a in ("--remote-name", "--remote-name-all"):
                    named = named or "remote"
            elif a.startswith("-") and len(a) > 1:
                letters = a[1:]
                if "o" in letters:
                    rest = letters.split("o", 1)[1]
                    if "O" in letters.split("o", 1)[0]:
                        named = named or "remote"
                    if rest:
                        out.add(_basename(rest))
                        named = True
                    elif nxt:
                        out.add(_basename(nxt))
                        named, i = True, i + 2
                        continue
                elif "O" in letters:
                    named = named or "remote"
        elif prog == "wget":
            if a in ("-O", "--output-document") and nxt:
                out.add(_basename(nxt))
                named, i = True, i + 2
                continue
            if a.startswith("--output-document="):
                out.add(_basename(a.split("=", 1)[1]))
                named = True
            elif a.startswith("-O") and len(a) > 2:
                out.add(_basename(a[2:]))
                named = True
        elif prog in ("iwr", "invoke-webrequest", "irm", "invoke-restmethod"):
            if lo.startswith("-out") and nxt:
                out.add(_basename(nxt))
                i += 2
                continue
        elif prog == "start-bitstransfer":
            if lo.startswith("-dest") and nxt:
                out.add(_basename(nxt))
                i += 2
                continue
            if not a.startswith("-") and not re.match(r"^https?://", a, re.I):
                out.add(_basename(a))
        elif prog in ("certutil", "bitsadmin", "aria2c"):
            if not a.startswith(("-", "/")) and not re.match(r"^https?://", a, re.I):
                out.add(_basename(a))
        i += 1
    if prog == "curl" and named == "remote" or (prog == "wget" and not named):
        for u in urls:
            name = _basename(u.split("?", 1)[0].split("#", 1)[0])
            if name:
                out.add(name)
    return {x for x in out if x and x not in ("-", "/dev/null")}


def _runs_saved(s, prog, downloads, p, depth, ps):
    """`curl -o f URL && bash f`, `iwr -OutFile x; .\\x`, `tee f <<EOF ... EOF; bash f`."""
    names = []
    if "/" in s[0] or "\\" in s[0]:
        names.append(_basename(s[0]))
    script = _script_arg(prog, s[1:])
    if script:
        names.append(_basename(script))
    for n in names:
        if n in downloads:
            p.finding("code.shell-runs-download", "runs %s, which it just downloaded" % prog)
            return
        if n in p.heredoc_files:
            body = p.heredoc_files.pop(n)
            if precheck(body):
                _classify(body, p, depth + 1, ps)
            return


def _script_arg(prog, args):
    """The file an interpreter (or source, ., Start-Process, cmd /c) is told to run, or None."""
    if prog in ("source", ".", "&", "start-process", "start", "invoke-item", "ii"):
        pos = [a for a in args if not a.startswith("-")]
        return pos[0] if pos else None
    if prog == "cmd":
        for k, a in enumerate(args):
            if a.lower() in ("/c", "/k") and k + 1 < len(args):
                return args[k + 1]
        return None
    if not _is_interp(prog):
        return None
    vals = {"-X", "-W", "-O", "--rcfile", "--init-file", "-o", "+o", "-I", "-r", "--require",
            "--import", "--loader"}
    i = 0
    while i < len(args):
        a = args[i]
        if prog in SHELLS:
            if SHELL_C.match(a):
                return None
        elif a in ("-c", "-m", "-e", "-E", "--eval", "-p", "--print", "-Command", "-command") or \
                (re.match(r"^-[A-Za-z]*[cme]$", a) and prog not in ("pwsh", "powershell")):
            return None
        if a.lower() in ("-file", "-f") and prog in ("pwsh", "powershell"):
            return args[i + 1] if i + 1 < len(args) else None
        if a.startswith("-") or a.startswith("+"):
            i += 2 if (a in vals and prog not in SHELLS) else 1
            continue
        return a
    return None


def _subst_bodies(tok):
    """The command substitutions ($( ) and backticks) inside one quoted word."""
    out, i, n = [], 0, len(tok)
    while i < n:
        if tok.startswith("$(", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if tok[j] == "(":
                    depth += 1
                elif tok[j] == ")":
                    depth -= 1
                j += 1
            out.append(tok[i + 2:j - 1] if depth == 0 else tok[i + 2:])
            i = j
        elif tok[i] == "`":
            j = tok.find("`", i + 1)
            out.append(tok[i + 1:j] if j > 0 else tok[i + 1:])
            i = j + 1 if j > 0 else n
        else:
            i += 1
    return out


def _segment(s, prog, p, depth, ps):
    args = s[1:]
    if _dynamic(s[0]) or prog in ("$", "$("):
        if INSTALLISH.search(" ".join(s)):
            p.gap("a command whose program name is computed at run time looks like an install: "
                  "not analysed, not scanned")
        return
    # command substitutions inside quoted words are commands too
    for a in args:
        if ("$(" in a or "`" in a) and not FETCH_SUBST.search(a):
            for b in _subst_bodies(a):
                if precheck(b):
                    _classify(b, p, depth + 1, ps)
    if p.fetch_vars and (prog in ("eval", "source", ".") or _is_interp(prog)):
        for a in args:
            if any(re.search(r"\$\{?" + re.escape(v) + r"\b", a) for v in p.fetch_vars):
                p.finding("code.shell-runs-download", "%s runs a downloaded variable" % prog)
                return
    if prog in ("pwsh", "powershell"):
        for k, a in enumerate(args):
            lo = a.lower()
            if lo == "-ec" or (len(lo) >= 2 and lo.startswith("-e") and
                               "-encodedcommand".startswith(lo)):
                _encoded(args[k + 1] if k + 1 < len(args) else "", p, depth)
                return
    if prog == "cmd":
        for k, a in enumerate(args):
            lo = a.lower()
            if lo in ("/c", "/k", "/r", "//c", "//k") or re.match(r"^/[ck].", lo):
                rest = ([a[2:]] if len(a) > 2 and not lo.startswith("//") else []) + args[k + 1:]
                code = " ".join(rest).replace("^", "")
                if precheck(code):
                    _classify(code, p, depth + 1, True)
                return
        return
    ci = _c_index(prog, args)
    if prog in ("eval", "source", ".") or ci is not None:
        inner = args[ci + 1:ci + 2] if ci is not None else [" ".join(args)]
        if any(FETCH_SUBST.search(a) for a in inner):
            p.finding("code.shell-runs-download", "%s runs a download" % prog)
            return
        for code in inner:
            if _dynamic(code) and INSTALLISH.search(code) and not _subst_bodies(code):
                p.gap("`%s` runs text computed at run time that looks like an install: not "
                      "analysed, not scanned" % prog)
            elif precheck(code):
                _classify(code, p, depth + 1, prog in ("pwsh", "powershell") or ps)
        return
    if _is_interp(prog):
        code = _code_arg(prog, args)
        if code and FETCH_SUBST.search(code):
            p.finding("code.shell-runs-download", "%s runs a download" % prog)
            return
    if prog in ("iex", "invoke-expression") and any(FETCH_SUBST.search(a) or
                                                    "downloadstring" in a.lower() for a in args):
        p.finding("code.exec-of-fetched", "Invoke-Expression of a download")
        return
    if prog in RUNNERS:
        _runner(prog, args, p, depth, ps)
        return
    if prog == "git":
        _git(args, p)
    elif prog == "gh" and args[:2] == ["repo", "clone"]:
        _gh_clone(args[2:], p)
    elif prog in ("npx", "bunx", "pnpx"):
        _exec_pkg(args, "npm", p, prog, depth, ps)
    elif prog in ("npm", "pnpm", "yarn", "bun"):
        _npm_family(prog, args, p, depth, ps)
    elif prog == "uvx":
        _exec_pkg(args, "pypi", p, "uvx", depth, ps)
    elif prog == "uv":
        _uv(args, p, depth, ps)
    elif prog == "pipx" and args[:1] in (["install"], ["run"]):
        _exec_pkg(args[1:], "pypi", p, "pipx " + args[0], depth, ps)
    elif _pip_args(prog, args) is not None:
        _pip(_pip_args(prog, args), p)
    elif _python_module(prog, args) is not None:
        mod, rest = _python_module(prog, args)
        if mod == "pipx" and rest[:1] in (["install"], ["run"]):
            _exec_pkg(rest[1:], "pypi", p, "pipx " + rest[0], depth, ps)
    elif prog == "deno":
        _deno(args, p)
    elif prog in ("go", "cargo", "gem"):
        _other_ecosystem(prog, args, p)
    elif prog in ("winget", "choco", "scoop") and args[:1] and args[0].lower() in ("install", "add", "upgrade"):
        p.note("`%s %s` installs from its package catalogue: not scanned by this guard"
               % (prog, args[0]))
    elif prog == "claude" and args[:2] == ["mcp", "add"]:
        _mcp_add(args[2:], p, depth, ps)
    elif prog == "claude" and args[:2] == ["mcp", "add-json"]:
        p.note("`claude mcp add-json`: the server definition is JSON, not analysed or scanned")
    elif prog == "claude" and args[:3] == ["plugin", "marketplace", "add"]:
        _marketplace_add(args[3:], p)
    elif prog == "claude" and args[:2] == ["plugin", "install"]:
        p.note("`claude plugin install` takes code from a marketplace already added; this "
               "command itself was not scanned")


def _encoded(val, p, depth):
    import base64
    try:
        text = base64.b64decode(val, validate=True).decode("utf-16-le")
    except Exception:
        p.gap("an encoded PowerShell command it could not decode: not analysed")
        return
    if precheck(text):
        _classify(text, p, depth + 1, True)


def _runner(prog, args, p, depth, ps):
    """watch / find -exec / script / su -c ...: the command they run is in their arguments."""
    for k, a in enumerate(args):
        if a.startswith("-"):
            continue
        b = _base(a)
        if b in KNOWN_PROGS:
            inner = [x for x in args[k:] if x not in (";", "\\;", "+", "{}")]
            _segment(inner, b, p, depth + 1, ps)
            return
        if " " in a and precheck(a):
            _classify(a, p, depth + 1, ps)
            return


# -- git and GitHub ---------------------------------------------------------------------------
CLONE_FLAGS = {"-q", "--quiet", "-v", "--verbose", "--progress", "--no-progress", "-n",
               "--no-checkout", "--bare", "--mirror", "-l", "--local", "--no-hardlinks", "-s",
               "--shared", "--dissociate", "--single-branch", "--no-single-branch", "--no-tags",
               "--tags", "--shallow-submodules", "--no-shallow-submodules", "--remote-submodules",
               "--no-remote-submodules", "--sparse", "--reject-shallow", "--no-reject-shallow",
               "--also-filter-submodules", "--no-recurse-submodules", "-4", "-6", "--ipv4",
               "--ipv6", "--no-also-filter-submodules"}
CLONE_VALUE = {"--depth", "-o", "--origin", "--reference", "--reference-if-able", "--filter",
               "-j", "--jobs", "--shallow-since", "--shallow-exclude", "--server-option",
               "--separate-git-dir", "--ref-format"}
CLONE_ASK = {"-u": "runs a command of its choosing on the server side (--upload-pack)",
             "--upload-pack": "runs a command of its choosing (--upload-pack)",
             "--template": "copies hooks from a template directory (--template)",
             "--bundle-uri": "fetches a bundle from another location (--bundle-uri)",
             "--recurse-submodules": "fetches submodules, which were not scanned",
             "--recursive": "fetches submodules, which were not scanned"}
# Settings that change WHAT is fetched or make git run something: the scan of the source would
# not be the scan of what lands, so they ask. Everything else (http.*, advice.*, ...) passes.
RISKY_GIT_CONFIG = re.compile(r"^(?:protocol\.|url\.|submodule\.|include|core\.(?:hookspath|"
                              r"fsmonitor|sshcommand|gitproxy|alternaterefscommand|worktree)$|"
                              r"uploadpack\.|remote\.|filter\.|credential|fetch\.fsck|"
                              r"transfer\.fsck|receive\.|alias\.|safe\.|lfs\.)", re.I)
GIT_GLOBAL_VALUE = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--config-env",
                    "--exec-path", "--super-prefix"}


def _git_config_ok(kv, p, how):
    key = kv.split("=", 1)[0].strip()
    if not key or RISKY_GIT_CONFIG.match(key):
        p.gap("`%s -c %s`: a git setting this guard does not vet; not scanned" % (how, redact(key)))
        return False
    return True


def _git(args, p):
    j = 0
    while j < len(args) and args[j].startswith("-"):
        if args[j] == "-c" and j + 1 < len(args) and not _git_config_ok(args[j + 1], p, "git"):
            return
        j += 2 if args[j] in GIT_GLOBAL_VALUE else 1
    if j >= len(args):
        return
    verb, rest = args[j], args[j + 1:]
    if verb == "clone":
        _clone_args(rest, p, "git clone")
    elif verb in ("pull", "fetch"):
        _git_pull(verb, rest, p)
    elif verb == "submodule" and rest[:1] == ["add"]:
        _git_submodule_add(rest[1:], p)
    elif verb == "remote" and rest[:1] == ["add"]:
        pos = [a for a in rest[1:] if not a.startswith("-")]
        if len(pos) >= 2:
            p.remotes[pos[0]] = pos[1]
    elif verb in ("checkout", "switch") and any(t["type"] == "git" for t in p.targets):
        pos, k = [], 0
        while k < len(rest):
            a = rest[k]
            if a == "--":
                break
            if a in ("-b", "-B", "-c", "-C", "--orphan"):
                return                          # a new local branch from what was scanned
            if not a.startswith("-"):
                pos.append(a)
            k += 1
        if pos:
            p.gap("`git %s %s` after the clone: only the ref the clone checked out was "
                  "scanned" % (verb, redact(pos[0])))


def _git_pull(verb, rest, p):
    pos, k = [], 0
    vals = {"-s", "--strategy", "-X", "--strategy-option", "--depth", "-j", "--jobs", "-o",
            "--server-option", "--shallow-since", "--shallow-exclude", "--negotiation-tip"}
    while k < len(rest):
        a = rest[k]
        if a in ("--upload-pack",) or a.startswith("--upload-pack="):
            p.gap("`git %s --upload-pack` runs a command of its choosing: not scanned" % verb)
            return
        if a in vals:
            k += 2
            continue
        if not a.startswith("-"):
            pos.append(a)
        k += 1
    if not pos:
        return
    src = p.remotes.get(pos[0], pos[0])
    if not _looks_remote(src):
        return                                  # a remote by name: what it holds is not known here
    if verb == "fetch":
        return                                  # fetch checks nothing out; a checkout asks
    ref = pos[1] if len(pos) > 1 else None
    _git_source(src, ref, p, "git pull")


def _git_submodule_add(rest, p):
    pos, k, branch = [], 0, None
    while k < len(rest):
        a = rest[k]
        if a in ("-b", "--branch"):
            branch = rest[k + 1] if k + 1 < len(rest) else None
            k += 2
            continue
        if a in ("--name", "--reference", "--depth"):
            k += 2
            continue
        if not a.startswith("-"):
            pos.append(a)
        k += 1
    if pos:
        _git_source(pos[0], branch, p, "git submodule add")
    else:
        p.gap("`git submodule add` with no source it could read: not scanned")


def _clone_args(rest, p, how, need_src=True):
    """Every argument of a clone: options before and after the URL. -> (src, branch, ok)."""
    pos, branch, i, ok = [], None, 0, True
    while i < len(rest):
        a = rest[i]
        if a == "--":
            pos.extend(rest[i + 1:])
            break
        if not a.startswith("-") or a == "-":
            pos.append(a)
            i += 1
            continue
        k, eq, v = a.partition("=") if a.startswith("--") else (a, "", "")
        if not eq and len(a) > 2 and not a.startswith("--") and a[:2] in ("-b", "-c", "-o", "-j", "-u"):
            k, v, eq = a[:2], a[2:], "="                       # -bdev, -cfoo=bar
        val = v if eq else (rest[i + 1] if i + 1 < len(rest) else None)
        step = 1 if eq else 2
        if k in ("-b", "--branch"):
            branch = val
            i += step
            continue
        if k == "--revision":
            i += step
            if val and re.match(r"^refs/(?:heads|tags)/[^\s]+$", val):
                branch = val.split("/", 2)[2]
            else:
                p.gap("`%s --revision %s`: a revision this guard does not fetch; not scanned"
                      % (how, redact(val or "?")))
                ok = False
            continue
        if k in ("-c", "--config"):
            i += step
            if not _git_config_ok(val or "", p, how):
                ok = False
            continue
        if k in CLONE_ASK:
            p.gap("`%s %s` %s" % (how, k, CLONE_ASK[k]))
            ok = False
            i += step if k in ("-u", "--upload-pack", "--template", "--bundle-uri") else 1
            continue
        if k in CLONE_VALUE:
            i += step
            continue
        if a in CLONE_FLAGS:
            i += 1
            continue
        p.gap("`%s %s`: an option this guard does not understand; not scanned" % (how, redact(k)))
        ok = False
        i += 1
    if branch is not None and (not branch or branch.startswith("-")):
        p.gap("`%s` with a branch that looks like an option: not scanned" % how)
        return
    if len(pos) > 2:
        p.gap("`%s` with more arguments than a source and a directory: not scanned" % how)
        return
    if not ok:
        return
    if pos or need_src:
        _git_source(pos[0] if pos else None, branch, p, how)
    return branch


SCP = re.compile(r"^(?:[\w.+-]+@)?([A-Za-z0-9][\w.-]*[A-Za-z0-9]|[A-Za-z0-9]{2,}):(?!//)(.+)$")


def _looks_remote(src):
    return "://" in src or bool(SCP.match(src)) or src.startswith("git@")


def _git_source(src, branch, p, how, install=False):
    if not src:
        p.gap("`%s` with no source it could read: not scanned" % how)
        return
    if _dynamic(src) or (branch and _dynamic(branch)):
        p.gap("`%s` of a source computed at run time: not scanned" % how)
        return
    if "::" in src.split("/", 1)[0] or src.lower().startswith(("ext::", "fd::")):
        p.gap("`%s` through a transport that runs a command (%s): not scanned"
              % (how, redact(src.split("::", 1)[0] + "::")))
        return
    url = _to_https(src)
    if url is None:
        if _is_local(src):
            return                                  # already on this machine: nothing fetched
        p.gap("`%s` of %s: not an https source this guard can fetch; not scanned"
              % (how, redact(src)))
        return
    t = {"type": "git", "url": url, "branch": branch, "label": redact(url)}
    if install:
        t["install"] = True
    p.target(t)


def _is_local(src):
    if src.startswith("file://"):
        return not _test_mode()
    if SCP.match(src) and not re.match(r"^[A-Za-z]:[\\/]", src):
        return False
    return src.startswith(("/", "./", "../", "~", ".")) or re.match(r"^[A-Za-z]:[\\/]", src) or \
        ("://" not in src and not re.match(r"^[\w.-]+@[\w.-]+:", src))


def _to_https(src):
    """A clone source as an https URL, or None. scp-style (with or without a user) and ssh://
    become https (public repos); git:// becomes https; http:// stays refused. file:// only in
    test mode."""
    if src.startswith("git+"):
        src = src[4:]
    if src.startswith("file://"):
        return src if _test_mode() else None
    m = re.match(r"^ssh:/" r"/(?:[\w.-]+[@])?([\w.-]+)(?::\d+)?/(.+)$", src)
    if m:
        return "https://%s/%s" % (m.group(1), m.group(2))
    m = SCP.match(src)
    if m and "://" not in src and not m.group(2).startswith("/"):
        return "https://%s/%s" % (m.group(1), m.group(2))
    if src.startswith("git://"):
        return "https://" + src[len("git://"):]
    if src.startswith("https://"):
        return src
    return None


def _gh_clone(args, p):
    head, tail = (args[:args.index("--")], args[args.index("--") + 1:]) if "--" in args \
        else (args, [])
    pos, k = [], 0
    while k < len(head):
        a = head[k]
        if a in ("-u", "--upstream-remote-name"):
            k += 2
            continue
        if not a.startswith("-"):
            pos.append(a)
        k += 1
    if not pos:
        p.gap("`gh repo clone` with no repository: not scanned")
        return
    r = pos[0]
    branch = None
    if tail:
        sub = Parsed()
        branch = _clone_args(tail, sub, "gh repo clone --", need_src=False)
        if sub.unscanned or sub.targets:
            for g in sub.unscanned:
                p.gap(g)
            if sub.targets:
                p.gap("`gh repo clone` with a second source after --: not scanned")
            return
    if _dynamic(r):
        p.gap("`gh repo clone` of a repository computed at run time: not scanned")
    elif re.match(r"^[\w.-]+/[\w.-]+$", r):
        _git_source("https://github.com/%s" % r, branch, p, "gh repo clone")
    elif "://" in r or r.startswith("git@"):
        _git_source(r, branch, p, "gh repo clone")
    else:
        p.gap("`gh repo clone %s` names no owner (it means your own account): not scanned"
              % redact(r))


# -- registries -------------------------------------------------------------------------------
PIP_VALUE_OPTS = {"-r", "--requirement", "-c", "--constraint", "-i", "--index-url",
                  "--extra-index-url", "-f", "--find-links", "-t", "--target", "--prefix",
                  "--root", "--src", "--platform", "--python-version", "--implementation",
                  "--abi", "--only-binary", "--no-binary", "--upgrade-strategy", "--progress-bar",
                  "--log", "--cache-dir", "--trusted-host", "--python", "-p", "--index",
                  "--default-index", "--report", "--config-settings", "-C", "--global-option",
                  "--group", "--extra", "--optional", "--package", "--with-requirements"}
PIP_GLOBAL_VALUE = {"--python", "--log", "--cache-dir", "--proxy", "--retries", "--timeout",
                    "--exists-action", "--trusted-host", "--cert", "--client-cert", "--src",
                    "--use-feature", "--use-deprecated", "--keyring-provider", "--progress-bar",
                    "--log-file", "--local-log", "--resume-retries"}
PIP_INDEX_OPTS = {"-i", "--index-url", "--extra-index-url", "-f", "--find-links", "--index",
                  "--default-index"}


def _pip_after(args):
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if (args[i] in PIP_GLOBAL_VALUE and "=" not in args[i]) else 1
    return args[i + 1:] if args[i:i + 1] == ["install"] else None


def _python_module(prog, args):
    """`python3 [-I -u ...] -m mod rest` / `-mmod` / `-Im mod` -> (mod, rest), else None."""
    if not (re.match(r"^(?:python|pypy)[0-9.]*$", prog) or prog == "py"):
        return None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-m":
            return (args[i + 1], args[i + 2:]) if i + 1 < len(args) else None
        if a in ("-X", "-W", "-Q", "--check-hash-based-pycs"):
            i += 2
            continue
        m = re.match(r"^-([A-Za-z]*)m(.*)$", a)
        if m and not a.startswith("--") and "c" not in m.group(1):
            if m.group(2):
                return m.group(2), args[i + 1:]
            return (args[i + 1], args[i + 2:]) if i + 1 < len(args) else None
        if re.match(r"^-[A-Za-z]*c", a) and not a.startswith("--"):
            return None
        if a.startswith("-"):
            i += 1
            continue
        return None
    return None


def _pip_args(prog, args):
    """The arguments after `install` for a pip-style install, or None if this is not one."""
    if prog in ("pip", "pip3") or re.match(r"^pip3?\.\d+$", prog):
        return _pip_after(args)
    mod = _python_module(prog, args)
    if mod and mod[0] in ("pip", "pip3"):
        return _pip_after(mod[1])
    if prog == "uv":
        k = _uv_skip(args)
        if args[k:k + 2] == ["pip", "install"]:
            return args[k + 2:]
        if args[k:k + 1] == ["add"]:
            return args[k + 1:]
    return None


def _uv_skip(args):
    vals = {"--directory", "--project", "--config-file", "--cache-dir", "--color", "--python",
            "-p", "--allow-insecure-host"}
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if (args[i] in vals and "=" not in args[i]) else 1
    return i


def _uv(args, p, depth, ps):
    k = _uv_skip(args)
    rest = args[k:]
    if rest[:1] == ["tool"] and rest[1:2] in (["install"], ["run"]):
        _exec_pkg(rest[2:], "pypi", p, "uv tool " + rest[1], depth, ps)
    elif rest[:1] == ["run"]:
        i, specs = 1, []
        while i < len(rest):
            a = rest[i]
            key, eq, v = a.partition("=")
            if key in ("--with", "-w", "--with-editable"):
                val = v if eq else (rest[i + 1] if i + 1 < len(rest) else "")
                specs += [x for x in val.split(",") if x.strip()]
                i += 1 if eq else 2
                continue
            if key in ("--with-requirements",):
                p.note("`uv run --with-requirements`: the packages it names were not scanned")
                i += 1 if eq else 2
                continue
            if key in ("--python", "-p", "--directory", "--project", "--index", "--index-url",
                       "--extra-index-url", "--env-file", "--package", "--extra", "--group"):
                i += 1 if eq else 2
                continue
            if a.startswith("-"):
                i += 1
                continue
            break
        for spec in specs:
            _pypi_spec(spec.strip(), p)
    elif _pip_args("uv", args) is not None:
        _pip(_pip_args("uv", args), p)


def _opt_walk(args, value_opts):
    """-> (positionals, [(option, value)]) with `--opt=value` split and `--` honoured."""
    pos, opts, i = [], [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            pos.extend(args[i + 1:])
            break
        if a.startswith("--") and "=" in a:
            k, v = a.split("=", 1)
            opts.append((k, v))
        elif a in value_opts:
            opts.append((a, args[i + 1] if i + 1 < len(args) else ""))
            i += 1
        elif a.startswith("-") and len(a) > 1:
            opts.append((a, None))
        else:
            pos.append(a)
        i += 1
    return pos, opts


def _pip(args, p):
    pos, opts = _opt_walk(args, PIP_VALUE_OPTS | {"-e", "--editable"})
    srcs = [v for k, v in opts if k in ("-e", "--editable") and v]
    no_binary = ",".join(v or "" for k, v in opts if k == "--no-binary").lower()
    for k, v in opts:
        if k in ("-r", "--requirement", "--with-requirements"):
            p.note("a requirements file (%s) is installed: the packages it names were not "
                   "scanned" % redact(v or "?"))
        if k in PIP_INDEX_OPTS:
            p.note("a custom package index is named: the PyPI copy was scanned, which may not "
                   "be what installs")
    cleaned = []
    for a in pos:
        if a == "@":                      # name @ url (PEP 508): the url is the source
            if cleaned:
                cleaned.pop()
            continue
        if " @ " in a:
            a = a.split(" @ ", 1)[1].strip()
        cleaned.append(a)
    for a in srcs + cleaned:
        _pypi_spec(a, p, no_binary)


def _pypi_spec(a, p, no_binary=""):
    if _dynamic(a):
        p.gap("a Python package named at run time: not scanned")
        return
    if a.startswith("git+") or a.startswith(("git://", "ssh://")):
        u = a[4:] if a.startswith("git+") else a
        u = u.split("#", 1)[0]
        scheme, _, rest = u.partition("://")
        host, _, path = rest.partition("/")
        branch = None
        if "@" in path:
            path, branch = path.rsplit("@", 1)
        _git_source("%s://%s/%s" % (scheme, host, path), branch or None, p, "pip install from git",
                    install=True)
        return
    if re.match(r"^https?://", a):
        if ARCHIVE.search(a):
            _archive_url(a, p, "pip install")
        else:
            p.gap("pip install from %s, which is not an archive: not scanned" % redact(a))
        return
    if a.startswith("file:") or _looks_local_path(a):
        p.note("a local path (%s) is installed: the dependencies it names were not scanned"
               % redact(a))
        return
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(?:(==|===)\s*([\w.!+-]+)|"
                 r"[<>=!~;].*)?$", a)
    if not m:
        p.gap("a Python requirement it could not read (%s): not scanned" % redact(a))
        return
    name, ver = m.group(1), m.group(4)
    t = {"type": "pypi", "name": name, "version": ver,
         "label": "pypi:%s%s" % (name, ("==" + ver) if ver else "")}
    if no_binary and (":all:" in no_binary or name.lower() in no_binary.split(",")):
        t["dist"] = "sdist"
    p.target(t)


def _looks_local_path(a):
    return a in (".", "..") or a.startswith(("./", "../", "/", "~", ".\\")) or \
        bool(re.match(r"^[A-Za-z]:[\\/]", a)) or \
        ("/" in a and not a.startswith("@") and ARCHIVE.search(a) is not None) or \
        bool(re.search(r"\.(?:whl|tar\.gz|zip|tgz)$", a) and "://" not in a)


NPM_VALUE_OPTS = {"--registry", "--prefix", "-w", "--workspace", "--tag", "--omit", "--include",
                  "--install-strategy", "--cache", "--userconfig", "--before", "--loglevel",
                  "-C", "--cwd", "--filter", "--dir", "--globalconfig", "--modules-folder",
                  "--network-timeout", "--mutex", "--cache-folder", "--global-folder"}


def _npm_family(prog, args, p, depth, ps):
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if (args[i] in NPM_VALUE_OPTS and "=" not in args[i]) else 1
    lead, rest = args[:i], args[i:]
    if prog == "yarn" and rest[:1] == ["global"]:
        rest = rest[1:]
    if not rest:
        return
    verb, tail = rest[0], lead + rest[1:]
    if verb in ("exec", "x") and prog in ("npm", "bun") or (verb == "dlx" and prog in ("pnpm", "yarn")):
        _exec_pkg(rest[1:], "npm", p, "%s %s" % (prog, verb), depth, ps)
    elif verb in NPM_INSTALL_VERBS:
        _npm_install(tail, p, prog, verb)
    elif verb in NPM_MANIFEST_VERBS and prog in ("npm", "pnpm"):
        p.note("`%s %s` installs the dependencies package.json names (and runs their install "
               "scripts): not scanned" % (prog, verb))


def _npm_install(args, p, prog="npm", verb="install"):
    pos, opts = _opt_walk(args, NPM_VALUE_OPTS)
    if any(k == "--registry" for k, _v in opts):
        p.note("a custom npm registry is named: the npmjs.org copy was scanned, which may not be "
               "what installs")
    if not pos:
        p.note("`%s %s` installs the dependencies package.json names (and runs their install "
               "scripts): not scanned" % (prog, verb))
    for a in pos:
        _npm_spec(a, p, "npm install")


def _npm_spec(a, p, how):
    if _dynamic(a):
        p.gap("an npm package named at run time: not scanned")
        return
    m = re.match(r"^(github|gitlab|bitbucket):([\w.-]+/[\w.-]+?)(?:\.git)?(?:#(.+))?$", a)
    if m:
        host = {"github": "github.com", "gitlab": "gitlab.com", "bitbucket": "bitbucket.org"}
        _git_source("https://%s/%s" % (host[m.group(1)], m.group(2)), _semver_ref(m.group(3)), p,
                    how, install=True)
        return
    if a.startswith(("git+", "git://", "git@", "ssh://")):
        url, _, ref = a.partition("#")
        _git_source(url, _semver_ref(ref or None), p, how, install=True)
        return
    if re.match(r"^https?://", a):
        if ARCHIVE.search(a):
            _archive_url(a, p, how)
        else:
            url, _, ref = a.partition("#")
            _git_source(url, _semver_ref(ref or None), p, how, install=True)
        return
    if a.startswith(("file:", "link:", "workspace:", "portal:")) or _looks_local_path(a):
        p.note("a local path (%s) is installed: the dependencies it names were not scanned"
               % redact(a))
        return
    if a.startswith("npm:"):                       # alias@npm:real@ver, written bare
        a = a[4:]
    if "@npm:" in a:
        a = a.split("@npm:", 1)[1]
    m = re.match(r"^([\w.-]+)/([\w.-]+)(?:#(.+))?$", a)
    if m and not a.startswith("@"):                # owner/repo is GitHub shorthand
        _git_source("https://github.com/%s/%s" % (m.group(1), m.group(2)),
                    _semver_ref(m.group(3)), p, how, install=True)
        return
    m = re.match(r"^((?:@[\w.-]+/)?[\w.-]+)(?:@(.+))?$", a)
    if not m:
        p.gap("an npm package spec it could not read (%s): not scanned" % redact(a))
        return
    name, ver = m.group(1), m.group(2)
    p.target({"type": "npm", "name": name, "version": ver,
              "label": "npm:%s%s" % (name, ("@" + ver) if ver else "")})


def _semver_ref(ref):
    if not ref or ref.startswith("semver:"):
        return None
    return ref


EXEC_VALUE_OPTS = {"-p", "--package", "--from", "--with", "--python", "--spec", "-c", "--call",
                   "--index-url", "--index", "--pip-args", "--suffix", "--with-editable",
                   "--with-requirements", "--extra-index-url", "-w", "--workspace", "--prefix",
                   "--registry", "--shell", "--cache-dir", "--node-options"}


def _exec_pkg(args, registry, p, how, depth=0, ps=False):
    i, pkgs, call = 0, [], None
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        k, eq, v = a.partition("=") if a.startswith("--") else (a, "", "")
        if k in EXEC_VALUE_OPTS:
            val = v if eq else (args[i + 1] if i + 1 < len(args) else "")
            i += 1 if eq else 2
            if k in ("--package", "--from", "--with", "--spec", "--with-editable") or \
                    (k == "-p" and registry == "npm"):
                pkgs.append(val)
            elif k in ("-c", "--call"):
                call = val
            elif k in ("--index-url", "--index", "--registry", "--extra-index-url"):
                p.note("`%s` names a custom index: the public registry copy was scanned" % how)
            continue
        if a.startswith("-"):
            i += 1
            continue
        break
    pkgs = [x for x in pkgs if x]
    if i < len(args) and not pkgs:
        pkgs.append(args[i])
    if call is not None:
        before = len(p.targets) + len(p.findings) + len(p.unscanned)
        if precheck(call):
            _classify(call, p, depth + 1, ps)
        if not pkgs and len(p.targets) + len(p.findings) + len(p.unscanned) == before:
            p.gap("`%s --call`: the command it runs was not analysed" % how)
        if not pkgs:
            return
    if not pkgs:
        p.gap("`%s` with no package it could read: not scanned" % how)
        return
    for spec in pkgs:
        if registry == "npm":
            _npm_spec(spec, p, how)
        else:
            _pypi_spec(_uvx_spec(spec), p)


def _uvx_spec(spec):
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)@([\w.!+-]+)$", spec)      # uvx ruff@0.6.0
    if m and m.group(2) != "latest":
        return "%s==%s" % (m.group(1), m.group(2))
    return spec[:-len("@latest")] if spec.endswith("@latest") else spec


def _deno(args, p):
    if not args or args[0] not in ("run", "install", "i", "add", "x", "compile", "serve"):
        return
    vals = {"--config", "-c", "--import-map", "--name", "-n", "--root", "--lock", "--cert",
            "--location", "--seed", "--v8-flags", "--env-file", "--ext", "--inspect", "--output",
            "-o", "--target", "--include"}
    i, specs = 1, []
    while i < len(args):
        a = args[i]
        if a == "--":
            break
        if a.startswith("-"):
            i += 2 if (a in vals and "=" not in a) else 1
            continue
        specs.append(a)
        if args[0] in ("run", "x", "compile", "serve") or args[0] == "install" and specs:
            break
        i += 1
    for spec in specs:
        if spec.startswith("npm:"):
            _npm_spec(spec[4:], p, "deno " + args[0])
        elif re.match(r"^(?:https?:|jsr:)", spec, re.I):
            p.gap("`deno %s %s`: a remote module this guard does not fetch; not scanned"
                  % (args[0], redact(spec)))
        elif _dynamic(spec):
            p.gap("`deno %s` of a module named at run time: not scanned" % args[0])


def _other_ecosystem(prog, args, p):
    """go / cargo / gem: not fetched, but a remote install of them asks."""
    if prog == "go" and args[:1] in (["install"], ["run"]):
        remote = [a for a in args[1:] if not a.startswith("-") and not a.startswith((".", "/"))
                  and re.match(r"^[\w.-]+\.[a-z]{2,}/", a)]
        if remote:
            p.gap("`go %s %s` builds remote code this guard does not fetch; not scanned"
                  % (args[0], redact(remote[0])))
    elif prog == "cargo" and args[:1] == ["install"]:
        rest = args[1:]
        if "--path" in rest or any(a.startswith("--path=") for a in rest):
            return
        if "--git" in rest:
            k = rest.index("--git")
            url = rest[k + 1] if k + 1 < len(rest) else None
            ref = None
            for opt in ("--branch", "--tag"):
                if opt in rest and rest.index(opt) + 1 < len(rest):
                    ref = rest[rest.index(opt) + 1]
            if "--rev" in rest:
                p.gap("`cargo install --git --rev`: a revision this guard does not fetch")
                return
            _git_source(url, ref, p, "cargo install --git", install=True)
            return
        names = [a for a in rest if not a.startswith("-")]
        if names:
            p.gap("`cargo install %s` builds a crate this guard does not fetch (build scripts "
                  "run); not scanned" % redact(names[0]))
    elif prog == "gem" and args[:1] in (["install"], ["i"]):
        names = [a for a in args[1:] if not a.startswith("-") and not _looks_local_path(a)
                 and not a.endswith(".gem")]
        if names:
            p.gap("`gem install %s` installs a gem this guard does not fetch (native "
                  "extensions run); not scanned" % redact(names[0]))


MCP_VALUE_OPTS = {"-s", "--scope", "-t", "--transport", "-e", "--env", "-H", "--header",
                  "--client-id", "--client-secret", "--callback-port"}


def _mcp_add(args, p, depth, ps):
    import shlex
    if "--" in args:
        head, inner = args[:args.index("--")], args[args.index("--") + 1:]
        pos, opts = _opt_walk(head, MCP_VALUE_OPTS)
        if len(pos) > 1:                   # name cmd -- args: the command is before the --
            inner = pos[1:] + inner
    else:
        pos, opts = _opt_walk(args, MCP_VALUE_OPTS)
        inner = pos[1:]
    transport = next((v for k, v in opts if k in ("-t", "--transport")), None)
    if not inner:
        p.gap("`claude mcp add` with no server command it could read: not scanned")
        return
    if transport in ("http", "sse") or re.match(r"^https?://", inner[0]):
        p.note("`claude mcp add` of a remote server (%s): its tools are not scanned here"
               % redact(inner[0]))
        return
    before = len(p.targets) + len(p.findings) + len(p.notes) + len(p.unscanned)
    _classify(" ".join(shlex.quote(a) for a in inner), p, depth + 1, ps)
    if len(p.targets) + len(p.findings) + len(p.notes) + len(p.unscanned) == before and \
            not any(_looks_local_path(x) for x in inner):
        p.note("`claude mcp add` runs `%s`, which is not a package this guard can fetch: not "
               "scanned" % redact(_base(inner[0])))


def _marketplace_add(args, p):
    pos = [a for a in args if not a.startswith("-")]
    if not pos:
        p.gap("`claude plugin marketplace add` with no source: not scanned")
        return
    src = pos[0]
    if _dynamic(src):
        p.gap("`claude plugin marketplace add` of a source computed at run time: not scanned")
    elif re.match(r"^[\w.-]+/[\w.-]+(?:#.+)?$", src):
        repo, _, ref = src.partition("#")
        _git_source("https://github.com/%s" % repo, ref or None, p, "claude plugin marketplace add")
    elif re.match(r"^https?://.*\.json(?:[?#].*)?$", src):
        p.gap("`claude plugin marketplace add` of a marketplace.json URL: the plugins it lists "
              "are not scanned")
    elif "://" in src or src.startswith("git@"):
        _git_source(src.split("#", 1)[0], None, p, "claude plugin marketplace add")
    # a local path: already on this machine


def _archive_url(url, p, how):
    if url.startswith("https://") or (_test_mode() and url.startswith("file://")):
        p.target({"type": "archive", "url": url.split("#", 1)[0], "label": redact(url)})
    else:
        p.gap("`%s` of %s: not an https download; not scanned" % (how, redact(url)))


# ============================================================================ fetching
class Budget(object):
    def __init__(self, seconds):
        import time
        self._t = time
        self.end = time.monotonic() + seconds

    def left(self):
        return self.end - self._t.monotonic()


class Incomplete(Exception):
    """This target could not be fetched or scanned in full: never a pass."""


def _budget_seconds():
    try:
        v = float(os.environ.get(BUDGET_ENV, ""))
        return v if v > 0 else BUDGET_S
    except ValueError:
        return BUDGET_S


def _allowed_url(url):
    return url.startswith("https://") or (_test_mode() and url.startswith("file://"))


def _public_ip(text):
    import ipaddress
    ip = ipaddress.ip_address(text.split("%", 1)[0])
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def _check_host(url, budget):
    """Refuse a URL whose host is, or resolves to, a private, loopback or link-local address.
    file:// (test mode only) has no host to check."""
    import threading
    import urllib.parse
    if url.startswith("file://"):
        return
    host = urllib.parse.urlsplit(url).hostname
    if not host:
        raise Incomplete("a download URL has no host")
    try:
        if not _public_ip(host):
            raise Incomplete("a download host is a private, loopback or link-local address")
        return
    except ValueError:
        pass
    res = {}

    def resolve():
        import socket
        try:
            res["a"] = socket.getaddrinfo(host, 443, 0, socket.SOCK_STREAM)
        except Exception as exc:
            res["e"] = exc
    t = threading.Thread(target=resolve, daemon=True)
    t.start()
    t.join(max(0.5, min(10.0, budget.left())))
    if t.is_alive():
        raise Incomplete("resolving a download host took too long")
    if "e" in res or not res.get("a"):
        raise Incomplete("a download host could not be resolved")
    for info in res["a"]:
        try:
            ok = _public_ip(info[4][0])
        except ValueError:
            ok = False
        if not ok:
            raise Incomplete("a download host resolves to a private, loopback or link-local "
                             "address")


def _safe_opener(budget):
    """A urllib opener whose redirects are checked BEFORE they are followed."""
    import urllib.request

    class SafeRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not _allowed_url(newurl):
                raise Incomplete("a download redirected off https")
            _check_host(newurl, budget)
            return urllib.request.HTTPRedirectHandler.redirect_request(
                self, req, fp, code, msg, headers, newurl)
    return urllib.request.build_opener(SafeRedirect), SafeRedirect


def _rmtree(path):
    import shutil
    import stat

    def retry(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            func(p)
        except Exception:
            pass
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


# -- child processes: own process group, the whole group killed on a timeout -------------------
_CHILDREN = set()


def _kill_tree(proc):
    import subprocess
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def _run(cmd, timeout, cwd, env=None):
    """-> (returncode, stdout bytes, stderr bytes). TimeoutExpired after the group is killed."""
    import subprocess
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kw["start_new_session"] = True
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    _CHILDREN.add(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        raise
    finally:
        _CHILDREN.discard(proc)
    return proc.returncode, out, err


GIT_PASS_KEYS = ("http.sslbackend", "http.sslcainfo", "http.proxy", "http.sslverify",
                 "http.sslcapath")


def _git_passthrough(work, budget):
    """-c flags for the allow-listed TLS and proxy keys from the user's real git config: the
    clone itself runs with no system or global config, which would otherwise lose a corporate
    CA or proxy. Hooks, credentials and insteadOf never come across."""
    env = dict(os.environ)
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS",
              "GIT_CONFIG_COUNT"):
        env.pop(k, None)
    try:
        rc, out, _err = _run(["git", "config", "--get-regexp",
                              r"^http\.(sslbackend|sslcainfo|proxy|sslverify|sslcapath)$"],
                             max(0.5, min(5.0, budget.left() - 1)), work, env)
    except Exception:
        return []
    flags = []
    for line in out.decode("utf-8", "replace").splitlines():
        key, _, val = line.partition(" ")
        if key.lower() in GIT_PASS_KEYS and "\n" not in val and "\r" not in val:
            flags += ["-c", "%s=%s" % (key.lower(), val)]
    return flags


def _git_clone(t, work, budget):
    import subprocess
    url = t["url"]
    if not _allowed_url(url):
        raise Incomplete("only https sources are fetched")
    if not _test_mode() or not url.startswith("file://"):
        _check_host(url, budget)
    hooks = os.path.join(work, "nohooks")
    os.mkdir(hooks, 0o700)
    cfg = os.path.join(work, "empty-gitconfig")
    with open(cfg, "w", encoding="utf-8", newline="\n"):
        pass
    dest = os.path.join(work, "src")
    cmd = ["git"] + _git_passthrough(work, budget) + [
        "-c", "core.symlinks=false", "-c", "core.hooksPath=" + hooks,
        "-c", "credential.helper=", "-c", "protocol.allow=never",
        "-c", "protocol.https.allow=always", "-c", "submodule.recurse=false",
        "-c", "core.longpaths=true", "-c", "core.fsmonitor=false", "-c", "init.templateDir="]
    if _test_mode() and url.startswith("file://"):
        cmd += ["-c", "protocol.file.allow=always"]
    cmd += ["clone", "--depth", "1", "--no-tags", "--single-branch", "--no-recurse-submodules",
            "-q"]
    if t.get("branch"):
        cmd += ["--branch", t["branch"]]
    cmd += ["--", url, dest]
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=cfg, GIT_ASKPASS="", SSH_ASKPASS="", GIT_LFS_SKIP_SMUDGE="1",
               GCM_INTERACTIVE="never")
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS",
              "GIT_CONFIG_COUNT"):
        env.pop(k, None)
    left = budget.left()
    if left <= 1:
        raise Incomplete("the time budget ran out before the clone")
    try:
        rc, _out, _err = _run(cmd, left, work, env)
    except subprocess.TimeoutExpired:
        raise Incomplete("the clone did not finish within the time budget")
    except OSError:
        raise Incomplete("git could not be run")
    if rc != 0:
        raise Incomplete("the clone failed (git exit %d)" % rc)
    gitdir = os.path.join(dest, ".git")
    if os.path.isdir(gitdir):
        _rmtree(gitdir)
    return dest


def _get(url, budget, cap=DOWNLOAD_CAP, kind="other"):
    import urllib.request
    if not _allowed_url(url):
        raise Incomplete("only https downloads are fetched")
    left = budget.left()
    if left <= 1:
        raise Incomplete("the time budget ran out before a download")
    _check_host(url, budget)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENTS.get(kind, USER_AGENTS["other"]),
                                               "Accept": "application/json, */*"})
    opener, _h = _safe_opener(budget)
    chunks, total = [], 0
    try:
        with opener.open(fullurl=req, timeout=max(1.0, min(30.0, left))) as r:
            final = r.geturl() if hasattr(r, "geturl") else url
            if not _allowed_url(final):
                raise Incomplete("a download redirected off https")
            while True:
                left = budget.left()
                if left <= 0.5:
                    raise Incomplete("the time budget ran out during a download")
                try:
                    r.fp.raw._sock.settimeout(max(0.5, min(30.0, left)))
                except Exception:
                    pass
                b = r.read(CHUNK)
                if not b:
                    break
                total += len(b)
                if total > cap:
                    raise Incomplete("a download is over the %d MB cap" % (cap // (1024 * 1024)))
                chunks.append(b)
    except Incomplete:
        raise
    except Exception as exc:
        raise Incomplete("a download failed (%s)" % exc.__class__.__name__)
    return b"".join(chunks)


def _registry(kind):
    base = os.environ.get(TEST_REGISTRY_ENV) if _test_mode() else None
    if base:
        return base.rstrip("/") + "/" + kind
    return NPM_REGISTRY if kind == "npm" else PYPI_REGISTRY


EXACT_VER = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][\w.-]+)?$")


def _npm_fetch(t, work, budget, notes):
    import urllib.parse
    name, ver = t["name"], t.get("version")
    if ver and not EXACT_VER.match(ver) and not re.match(r"^[A-Za-z][\w.-]*$", ver):
        notes.append("%s: a version range; the latest release was scanned" % t["label"])
        ver = None
    url = "%s/%s/%s" % (_registry("npm"), urllib.parse.quote(name, safe="@"), (ver or "latest").lstrip("v"))
    try:
        meta = json.loads(_get(url, budget, 20 * 1024 * 1024, "npm").decode("utf-8"))
        tarball = meta["dist"]["tarball"]
    except Incomplete:
        raise
    except Exception:
        raise Incomplete("the npm registry entry could not be read")
    if not isinstance(tarball, str):
        raise Incomplete("the npm registry entry names no tarball")
    return _extract(_get(tarball, budget, kind="npm"), tarball, work, budget)


# -- which PyPI file pip would install here -----------------------------------------------------
def _platform_tags():
    import platform
    mach = (platform.machine() or "").lower()
    if sys.platform == "darwin":
        arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64", "amd64": "x86_64"}.get(mach, mach)
        ok = {arch, "universal2", "universal"} | ({"intel", "fat64", "fat3"} if arch == "x86_64" else set())
        return lambda pt: pt.startswith("macosx_") and pt.rsplit("_", 1)[-1] in ok
    if sys.platform.startswith("win"):
        want = {"amd64": "win_amd64", "x86_64": "win_amd64", "arm64": "win_arm64",
                "aarch64": "win_arm64", "x86": "win32", "i386": "win32", "i686": "win32"}.get(mach)
        return lambda pt: pt == want
    if sys.platform.startswith("linux"):
        libc = (platform.libc_ver()[0] or "glibc").lower()
        arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(mach, mach)
        prefixes = ("musllinux_",) if libc == "musl" else ("manylinux", "linux_")
        return lambda pt: pt.startswith(prefixes) and pt.endswith("_" + arch)
    return lambda pt: False


def wheel_fits(filename, version=None, plat_ok=None):
    """Would pip on THIS interpreter and platform accept this wheel? (A simplified PEP 425
    check: python tag, abi tag, platform tag, each possibly a dotted set.)"""
    maj, mn = (version or sys.version_info[:2])
    plat_ok = plat_ok or _platform_tags()
    stem = filename[:-4] if filename.lower().endswith(".whl") else None
    if not stem:
        return False
    parts = stem.split("-")
    if len(parts) < 5:
        return False
    pys, abis, plats = (x.split(".") for x in parts[-3:])

    def py_ok(t, abi):
        if t in ("py%d" % maj, "py%d%d" % (maj, mn)):
            return True
        m = re.match(r"^(py|cp)(\d)(\d+)$", t)
        if not m or int(m.group(2)) != maj:
            return False
        v = int(m.group(3))
        if m.group(1) == "py":
            return v <= mn
        return v == mn or (abi == "abi3" and v <= mn)

    def abi_ok(a):
        return a in ("none", "abi3", "cp%d%d" % (maj, mn), "cp%d%dm" % (maj, mn))
    return any(py_ok(pt, ab) and abi_ok(ab) and (pl == "any" or plat_ok(pl))
               for pt in pys for ab in abis for pl in plats)


def _wheel_rank(filename):
    parts = filename[:-4].split("-")
    py, abi, plat = parts[-3:]
    return (0 if plat == "any" else 1, 0 if abi == "none" else 1, 1 if py.startswith("cp") else 0)


def choose_pypi_file(files, prefer_sdist=False):
    """-> (the file dict pip would install on this machine, a note or None)."""
    usable = [f for f in files if isinstance(f, dict) and isinstance(f.get("url"), str)]
    wheels = [f for f in usable if f.get("packagetype") == "bdist_wheel" and
              wheel_fits(str(f.get("filename") or f["url"].rsplit("/", 1)[-1]))]
    sdists = [f for f in usable if f.get("packagetype") == "sdist"]
    if wheels and not prefer_sdist:
        wheels.sort(key=lambda f: _wheel_rank(str(f.get("filename") or f["url"].rsplit("/", 1)[-1])),
                    reverse=True)
        return wheels[0], None
    if sdists:
        why = None
        if not prefer_sdist and any(f.get("packagetype") == "bdist_wheel" for f in usable):
            why = "no wheel fits this interpreter and platform; the sdist was scanned"
        return sdists[0], why
    if prefer_sdist and wheels:
        return wheels[0], "--no-binary was given but PyPI lists no sdist; a wheel was scanned"
    raise Incomplete("PyPI lists no wheel for this interpreter and platform and no sdist")


def _pypi_fetch(t, work, budget, notes):
    name, ver = t["name"], t.get("version")
    url = "%s/%s/%sjson" % (_registry("pypi"), name, (ver + "/") if ver else "")
    try:
        meta = json.loads(_get(url, budget, 20 * 1024 * 1024, "pypi").decode("utf-8"))
        files = [f for f in meta.get("urls") or [] if isinstance(f, dict) and not f.get("yanked")]
    except Incomplete:
        raise
    except Exception:
        raise Incomplete("the PyPI entry could not be read")
    f, why = choose_pypi_file(files, t.get("dist") == "sdist")
    if why:
        notes.append(why)
    return _extract(_get(f["url"], budget, kind="pypi"), f["url"], work, budget)


def _member_ok(name):
    n = name.replace("\\", "/")
    return bool(n) and not n.startswith("/") and ".." not in n.split("/") and \
        not re.match(r"^[A-Za-z]:", n) and "\x00" not in n


def _copy_capped(src, target, state, budget):
    """Copy one archive member in chunks: the total counts toward the extract cap and the time
    budget is checked between chunks."""
    with open(target, "wb") as out:
        while True:
            if budget is not None and budget.left() <= 0.5:
                raise Incomplete("the time budget ran out while extracting")
            b = src.read(CHUNK)
            if not b:
                break
            state["written"] += len(b)
            if state["written"] > EXTRACT_CAP:
                raise Incomplete("the archive is over the extract cap")
            out.write(b)


def _extract(data, url, work, budget=None):
    """Extract an archive's regular files and directories only: links, devices, absolute paths
    and `..` are refused; member count and total size are capped (every member's size counts,
    whatever its type), the time budget is checked inside the loop, and directories end up 0700
    and files 0600 so nothing extracted is hidden from the scan."""
    import io
    dest = os.path.join(work, "src")
    os.mkdir(dest, 0o700)
    path = url.split("#", 1)[0].split("?", 1)[0].lower()
    state = {"written": 0}

    def target_of(name):
        return os.path.join(dest, *[x for x in name.replace("\\", "/").split("/") if x not in ("", ".")])

    def check_time():
        if budget is not None and budget.left() <= 0.5:
            raise Incomplete("the time budget ran out while extracting")
    if path.endswith((".whl", ".zip")):
        import zipfile
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except Exception:
            raise Incomplete("the archive could not be opened")
        infos, total = z.infolist(), 0
        if len(infos) > EXTRACT_MEMBERS:
            raise Incomplete("the archive has more than %d members" % EXTRACT_MEMBERS)
        for i in infos:
            check_time()
            if not _member_ok(i.filename):
                raise Incomplete("the archive holds an unsafe member path")
            total += max(0, i.file_size)
            if total > EXTRACT_CAP:
                raise Incomplete("the archive is over the extract cap")
            if ((i.external_attr >> 16) & 0o170000) == 0o120000:
                continue                                         # a symlink: not extracted
            target = target_of(i.filename)
            if i.is_dir():
                os.makedirs(target, 0o700, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), 0o700, exist_ok=True)
            with z.open(i, mode="r") as src:
                _copy_capped(src, target, state, budget)
        _normalise_modes(dest)
        return dest
    import tarfile
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except Exception:
        raise Incomplete("the archive could not be opened")
    with tar:
        count, total = 0, 0
        for m in tar:
            check_time()
            count += 1
            if count > EXTRACT_MEMBERS:
                raise Incomplete("the archive has more than %d members" % EXTRACT_MEMBERS)
            if not _member_ok(m.name):
                raise Incomplete("the archive holds an unsafe member path")
            total += max(0, m.size)
            if total > EXTRACT_CAP:
                raise Incomplete("the archive is over the extract cap")
            if not (m.isreg() or m.isdir()):
                continue                                         # links, devices: not extracted
            target = target_of(m.name)
            if m.isdir():
                os.makedirs(target, 0o700, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), 0o700, exist_ok=True)
            src = tar.extractfile(m)
            if src is None:
                continue
            with src:
                _copy_capped(src, target, state, budget)
    _normalise_modes(dest)
    return dest


def _normalise_modes(dest):
    os.chmod(dest, 0o700)
    for dirpath, dirnames, filenames in os.walk(dest):
        for d in dirnames:
            try:
                os.chmod(os.path.join(dirpath, d), 0o700)
            except OSError:
                pass
        for f in filenames:
            try:
                os.chmod(os.path.join(dirpath, f), 0o600)
            except OSError:
                pass


# ============================================================================ scanning
def _scanner():
    if _test_mode() and os.environ.get(TEST_SCANNER_ENV):
        return os.environ[TEST_SCANNER_ENV]
    return os.path.join(HERE, "gt_intake_scan.py")


def _supports_deadline(scanner):
    try:
        with open(scanner, encoding="utf-8", errors="replace") as fh:
            return '"--deadline"' in fh.read()
    except OSError:
        return False


def _scan(root, work, budget):
    """-> the scanner's report dict. Raises Incomplete on timeout, error or bad output."""
    import subprocess
    scanner = _scanner()
    left = budget.left()
    if left <= 1:
        raise Incomplete("the time budget ran out before the scan")
    cwd = os.path.join(work, "scan-cwd")
    os.mkdir(cwd, 0o700)
    cmd = [sys.executable, "-I", "-X", "utf8", scanner, root, "--json"]
    if _supports_deadline(scanner):
        cmd += ["--deadline", "%.1f" % max(1.0, left - 2)]
    try:
        rc, out, _err = _run(cmd, left, cwd)
    except subprocess.TimeoutExpired:
        raise Incomplete("the scan did not finish within the time budget")
    except OSError:
        raise Incomplete("the scanner could not be run")
    if rc not in (0, 1, 3):
        raise Incomplete("the scanner failed (exit %d)" % rc)
    try:
        rep = json.loads(out.decode("utf-8", "replace"))
        rep["units"]
    except Exception:
        raise Incomplete("the scanner's report could not be read")
    rep["_exit"] = rc
    return rep


# -- what an install runs: files named by package.json, setup.py, pyproject, hooks.json ---------
_REF_TOKEN = re.compile(r"[\w.@~+-]*(?:/[\w.@~+-]+)+|[\w.@+-]+\.(?:js|cjs|mjs|ts|sh|bash|py|"
                        r"node|ps1|cmd|bat|rb|pl)\b")


def _ref_tokens(s):
    out = []
    s = (s or "").replace("${CLAUDE_PLUGIN_ROOT}/", "").replace("$CLAUDE_PLUGIN_ROOT/", "")
    for tok in _REF_TOKEN.findall(s):
        while tok.startswith("./"):
            tok = tok[2:]
        if tok and not tok.startswith(("http", "/")):
            out.append(tok)
    return out


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            for s in _strings(v):
                yield s
    elif isinstance(obj, list):
        for v in obj:
            for s in _strings(v):
                yield s


def install_refs(root, max_depth=3):
    """-> the set of root-relative paths an install or a plugin would run."""
    import posixpath
    refs = set()
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root).replace(os.sep, "/")
        rel = "" if rel == "." else rel
        if rel.count("/") + (1 if rel else 0) >= max_depth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            if fn not in ("package.json", "setup.py", "pyproject.toml", "hooks.json", "setup.cfg"):
                continue
            try:
                with open(os.path.join(dirpath, fn), "rb") as fh:
                    text = fh.read(2 * 1024 * 1024).decode("utf-8", "replace")
            except OSError:
                continue
            found = []
            if fn in ("package.json", "hooks.json"):
                try:
                    doc = json.loads(text)
                except Exception:
                    doc = None
                if isinstance(doc, dict):
                    parts = [doc.get(k) for k in ("scripts", "main", "bin", "module", "exports",
                                                  "browser", "hooks", "gypfile")]
                    for s in _strings(parts):
                        found += _ref_tokens(s)
                        if re.match(r"^[\w.@+-]+$", s) and "." in s:
                            found.append(s)
            elif fn == "setup.py":
                for s in re.findall(r"['\"]([^'\"\n]{1,200})['\"]", text):
                    found += _ref_tokens(s)
            else:
                for mod in re.findall(r"=\s*['\"]([A-Za-z_][\w.]*)(?::[\w.]+)?['\"]", text):
                    path = mod.replace(".", "/")
                    found += [path + ".py", path + "/__init__.py", "src/" + path + ".py",
                              "src/" + path + "/__init__.py"]
                for s in re.findall(r"['\"]([^'\"\n]{1,200})['\"]", text):
                    found += _ref_tokens(s)
            base = rel
            if fn == "hooks.json" and posixpath.basename(rel) == "hooks":
                base = posixpath.dirname(rel)
            for r in found:
                refs.add(posixpath.normpath(posixpath.join(base, r)) if base else posixpath.normpath(r))
                if fn == "hooks.json":
                    refs.add(posixpath.normpath(posixpath.join(rel, r)) if rel else posixpath.normpath(r))
    return refs


# -- what is shown: names outside a safe charset are shortened -----------------------------------
_SAFE_PART = re.compile(r"^[A-Za-z0-9._+@=,~-]{1,80}$")
_WITHHELD = re.compile(r"^\[name withheld: [\w. -]{1,60}\]$")
_GAP_SUFFIX = re.compile(r": (?:over the \d+ MB size cap, not scanned|\w{1,12} content not scanned "
                         r"\(document format\)|unreadable \(\w+\)|[^:]{0,80}not scanned[^:]{0,60}|"
                         r"[^:]{0,80}\((?:binary|nul|utf-16|utf-7)[^:]{0,60})$", re.I)


def safe_name(path):
    """A file path as shown: each part outside a safe charset becomes a short hash, so a file
    NAME cannot speak to the reader (an instruction, a fake verdict, a terminal escape)."""
    import hashlib
    out = []
    for part in str(path).replace("\\", "/").split("/"):
        if _SAFE_PART.match(part) or _WITHHELD.match(part) or part in ("", ".", ".."):
            out.append(part)
            continue
        ext = os.path.splitext(part)[1]
        ext = ext if re.match(r"^\.[A-Za-z0-9]{1,8}$", ext) else ""
        out.append("<name %s%s>" % (hashlib.sha256(part.encode("utf-8", "replace")).hexdigest()[:10],
                                    ext))
    return "/".join(out)


def _gap_text(why):
    """A scanner's 'path: reason' with the path made safe."""
    why = _clean(why, 400)
    m = _GAP_SUFFIX.search(why)
    if m and m.start() > 0:
        return (safe_name(why[:m.start()]) + why[m.start():])[:240]
    return why[:240]


def _gap_path(why):
    m = _GAP_SUFFIX.search(why)
    return why[:m.start()] if m and m.start() > 0 else None


_CODE_EXT = {".py", ".pyw", ".js", ".cjs", ".mjs", ".ts", ".tsx", ".jsx", ".sh", ".bash", ".zsh",
             ".ps1", ".psm1", ".bat", ".cmd", ".rb", ".pl", ".php", ".lua", ".node", ".pth",
             ".gyp", ".gypi", ".json", ".toml", ".cfg", ".md", ".go", ".rs", ".c", ".h", ".cc",
             ".cpp", ".java", ".kt", ".cs", ".swift", ".vbs", ".exe", ".dll", ".so", ".dylib"}


def _is_document_gap(why):
    return bool(re.search(r"content not scanned \(document format\)$", why))


SKIP_DIR_SAFE = {".git", ".hg", ".svn"}


def fetch_and_scan(t, budget):
    """-> result dict for one target: findings, gaps (scan-level: ask), notes."""
    import tempfile
    import tarfile
    import zipfile
    res = {"label": t["label"], "type": t["type"], "findings": [], "gaps": [], "notes": []}
    registry = t["type"] in ("npm", "pypi", "archive") or bool(t.get("install"))
    work = tempfile.mkdtemp(prefix="gt-install-guard-")
    try:
        try:
            try:
                if t["type"] == "git":
                    root = _git_clone(t, work, budget)
                elif t["type"] == "npm":
                    root = _npm_fetch(t, work, budget, res["notes"])
                elif t["type"] == "pypi":
                    root = _pypi_fetch(t, work, budget, res["notes"])
                else:
                    root = _extract(_get(t["url"], budget), t["url"], work, budget)
            except Incomplete:
                raise
            except (OSError, tarfile.TarError, zipfile.BadZipFile, EOFError) as exc:
                raise Incomplete("could not extract: %s" % exc.__class__.__name__)
            except Exception as exc:
                raise Incomplete("the fetch failed (%s)" % exc.__class__.__name__)
            try:
                refs = install_refs(root)
            except Exception:
                refs = set()
            rep = _scan(root, work, budget)
        except Incomplete as exc:
            res["gaps"].append(str(exc))
            return res
        if registry:
            res["notes"].append("dependencies are not scanned: only this package's own files "
                                "were")
        cred_problem = (rep.get("checks") or {}).get("credential_problem")
        for u in rep.get("units") or []:
            for f in u.get("findings") or []:
                raw = str(f.get("file") or "")
                g = {"kind": str(f.get("kind")), "rule": str(f.get("rule")),
                     "file": safe_name(_clean(raw, 300))[:200], "line": int(f.get("line") or 0)}
                if raw.replace("\\", "/") in refs:
                    g["install_ref"] = True
                res["findings"].append(g)
            for why in u.get("incomplete") or []:
                raw = str(why)
                shown = _gap_text(raw)
                if cred_problem and _clean(raw, 200) == _clean(cred_problem, 200):
                    if "deadline" in raw:
                        res["gaps"].append(shown)
                    else:
                        res["notes"].append("credential scan did not run: " + shown)
                elif _is_document_gap(raw) and (_gap_path(raw) or "") not in refs:
                    res["notes"].append("not scanned: " + shown)
                else:
                    res["gaps"].append(shown)
        for d in rep.get("skipped_dirs") or []:
            d = safe_name(_clean(d, 80))
            if d in SKIP_DIR_SAFE:
                continue
            if registry:
                res["gaps"].append("a bundled %s/ directory was not scanned (an install can run "
                                   "code from it)" % d)
            else:
                res["notes"].append("%s/ was not scanned" % d)
        if rep.get("deadline_hit"):
            res["gaps"].append("the scan's deadline was reached")
        if rep.get("_exit") == 3 and not res["gaps"] and not [n for n in res["notes"]
                                                             if n.startswith(("not scanned",
                                                                              "credential"))]:
            res["gaps"].append("the scan reported itself incomplete")
        res["gaps"] = _dedup(res["gaps"])
        res["notes"] = _dedup(res["notes"])
        return res
    finally:
        try:
            _rmtree(work)
        except Exception:
            pass


def _dedup(xs):
    out = []
    for x in xs:
        if x not in out:
            out.append(x)
    return out


def _clean(s, limit=160):
    s = "" if s is None else str(s)
    return "".join(ch if ch.isprintable() else "?" for ch in s)[:limit]


# A finding in a path that is not what an install runs is shown as a note, not a stop (0.22.0; found
# on python/cpython: a minified d3.js, a urllib2 test and a NEWS entry made a legitimate repo DO NOT
# INSTALL). Narrow on purpose: only these rules, only these paths. code.reverse-shell,
# code.install-hook-fetches and code.exec-of-decoded stay serious everywhere, and so does a finding
# at the top of the repo, in a file an install or a plugin runs (package.json scripts/main/bin,
# setup.py, a pyproject entry point, hooks.json), or anywhere an agent loads instructions from
# (skills/, commands/, agents/, hooks/, any .claude* directory, SKILL.md, CLAUDE.md, AGENTS.md).
_DEMOTE_CODE = frozenset(("code.pipe-download-to-shell", "code.shell-runs-download",
                          "code.exec-of-fetched"))
_VENDORED = re.compile(r"(?:^|/)(?:_?vendor|_?vendored|third[_-]party|node_modules)/|\.min\.(?:js|css)$",
                       re.I)
_TESTS = re.compile(r"(?:^|/)(?:tests?|__tests__|testing|fixtures?|examples?)/|"
                    r"(?:^|/)test_[^/]*$|[._]test\.\w+$|[._]spec\.\w+$", re.I)
_CHANGELOG_NAME = re.compile(r"^(?:changelog|news|history)(?:$|[._-])", re.I)
_CHANGELOG_DIR = re.compile(r"(?:^|/)(?:news|changelog|changes)\.d/", re.I)
_AGENT_DIRS = re.compile(r"(?:^|/)(?:skills|commands|agents|hooks|\.claude[^/]*)/", re.I)
_AGENT_FILES = {"skill.md", "claude.md", "agents.md"}


def _demoted(f):
    if f.get("install_ref"):
        return False
    path = str(f.get("file") or "").replace("\\", "/")
    if _AGENT_DIRS.search(path) or path.rsplit("/", 1)[-1].lower() in _AGENT_FILES:
        return False
    rule = f.get("rule")
    if rule in _DEMOTE_CODE:
        return bool(_VENDORED.search(path) or _TESTS.search(path))
    if f.get("kind") == "prompt-injection":
        return bool(_CHANGELOG_NAME.search(path.rsplit("/", 1)[-1]) or _CHANGELOG_DIR.search(path))
    return False


def is_serious(f):
    if _demoted(f):
        return False
    return f.get("kind") == "prompt-injection" or f.get("rule") in SERIOUS_RULES


# ============================================================================ deciding
def evaluate(parsed, budget=None):
    """Fetch and scan every target. -> summary dict (also the CLI's --json)."""
    budget = budget or Budget(_budget_seconds())
    results = []
    if parsed.findings or parsed.unscanned:
        results.append({"label": "the command itself", "type": "command",
                        "findings": list(parsed.findings), "gaps": list(parsed.unscanned),
                        "notes": []})
    for k, t in enumerate(parsed.targets):
        if k >= MAX_TARGETS:
            results.append({"label": t["label"], "type": t["type"], "findings": [], "notes": [],
                            "gaps": ["more than %d install targets in one command; not scanned"
                                     % MAX_TARGETS]})
            continue
        if budget.left() <= 1:
            results.append({"label": t["label"], "type": t["type"], "findings": [], "notes": [],
                            "gaps": ["the time budget ran out before this was scanned"]})
            continue
        results.append(fetch_and_scan(t, budget))
    serious = [(r["label"], f) for r in results for f in r["findings"] if is_serious(f)]
    other = [(r["label"], f) for r in results for f in r["findings"] if not is_serious(f)]
    gaps = [(r["label"], g) for r in results for g in r["gaps"]]
    notes = list(parsed.notes) + ["%s: %s" % (r["label"], n) for r in results for n in r["notes"]]
    if serious:
        verdict = "DO NOT INSTALL"
    elif gaps:
        verdict = "INCOMPLETE"
    elif other or notes:
        verdict = "CAUTION"
    else:
        verdict = "SAFE"
    return {"verdict": verdict, "results": results, "serious": serious, "other": other,
            "gaps": gaps, "notes": notes,
            "counts": {"targets": len(parsed.targets) + (1 if parsed.findings or parsed.unscanned
                                                         else 0),
                       "serious": len(serious), "other": len(other), "incomplete": len(gaps),
                       "notes": len(notes)}}


def _fline(label, f):
    return "  %-16s %-30s %s:%d  [%s]" % (f["kind"], f["rule"], f["file"], f["line"], label)


def _grant_hint(command):
    """The exact command, never a first-word wildcard: a grant for `git *` would let every git
    command through the guard."""
    import shlex
    one = (command or "").strip()
    if one and "\n" not in one and "\r" not in one and len(one) <= 300 and redact(one) == one:
        return ("To let this exact command through once, the user runs in a terminal:\n"
                "  python3 ~/.claude/golden-thread/hooks/gt_grant.py add %s once\n"
                "(or /gt:gt-grant). Turn the guard down with: gt_settings.py set install_guard "
                "warn|off" % shlex.quote(one))
    return ("This command cannot be shown as a one-line grant (it spans lines, is long, or "
            "carries a credential). To let it through, the user grants the exact command with "
            "/gt:gt-grant, or turns the guard down with: gt_settings.py set install_guard warn|off")


def _notes_text(summary, limit=15):
    lines = []
    if summary["other"]:
        lines.append("Other findings (not serious; kind, rule, file:line -- no matched text):")
        lines += [_fline(l, f) for l, f in summary["other"][:limit]]
        if len(summary["other"]) > limit:
            lines.append("  ... %d more" % (len(summary["other"]) - limit))
    if summary["notes"]:
        lines.append("Notes:")
        lines += ["  - " + n for n in summary["notes"][:limit]]
        if len(summary["notes"]) > limit:
            lines.append("  ... %d more" % (len(summary["notes"]) - limit))
    return "\n".join(lines)


def decide(mode, summary, command):
    """-> hook output dict, or None for silence. Never an allow."""
    if mode == "off":
        return None
    notes = _notes_text(summary)
    labels = ", ".join(r["label"] for r in summary["results"][:6])
    head = ("gt install_guard scanned what this command would install (%s)" % labels) if labels \
        else "gt install_guard read this command"
    if summary["serious"]:
        lines = [head + ": %d SERIOUS finding(s)." % len(summary["serious"]),
                 "Serious (kind, rule, file:line -- matched text is never shown):"]
        lines += [_fline(l, f) for l, f in summary["serious"][:20]]
        if len(summary["serious"]) > 20:
            lines.append("  ... %d more" % (len(summary["serious"]) - 20))
        if summary["gaps"]:
            lines.append("Also not fully scanned: " + "; ".join(
                "%s: %s" % g for g in summary["gaps"][:5]))
        lines.append(_grant_hint(command))
        decision = "deny" if mode == "block" else "ask"
        reason = "\n".join(lines)
        out = {"hookEventName": "PreToolUse", "permissionDecision": decision,
               "permissionDecisionReason": _annotated(reason) if decision == "deny" else reason}
        if notes:
            out["additionalContext"] = notes
        return {"hookSpecificOutput": out}
    if summary["gaps"]:
        lines = [head + ", and could NOT finish -- an incomplete scan is not a pass:"]
        lines += ["  - %s: %s" % g for g in summary["gaps"][:10]]
        lines.append(_grant_hint(command))
        out = {"hookEventName": "PreToolUse", "permissionDecision": "ask",
               "permissionDecisionReason": "\n".join(lines)}
        if notes:
            out["additionalContext"] = notes
        return {"hookSpecificOutput": out}
    if notes:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "additionalContext": head + ".\n" + notes}}
    return None


def mode_setting():
    try:
        sys.path.insert(0, HERE)
        import gt_settings
        v = gt_settings.get(SETTING)
        if v is None:                   # not registered yet: the raw value, else the default
            v = gt_settings._load().get(SETTING)
    except Exception:
        v = None
    v = (v or DEFAULT_MODE).strip().lower() if isinstance(v, str) else DEFAULT_MODE
    return v if v in MODES else DEFAULT_MODE


_PART_SPLIT = re.compile(r"&&|\|\||\|&|[;&|\n`]|\$\(|<\(|>\(|\)")


def command_parts(command):
    """The pieces a grant must cover: split on && ; | || & newline $( ` <( and ), quotes
    ignored (splitting inside a quoted string only makes a grant cover less)."""
    one = command.replace("\\\r\n", "").replace("\\\n", "")
    return [" ".join(x.split()) for x in _PART_SPLIT.split(one) if x.strip()]


def _rule_covers(rule, tool, part, split_rule):
    import fnmatch
    base, pat = split_rule(rule)
    if base != tool:
        return False
    if pat is None:
        return True
    pat = pat.strip()
    if pat.endswith(":*"):
        pre = " ".join(pat[:-2].split())
        return part == pre or part.startswith(pre + " ")       # a word boundary, never a prefix
    if "*" in pat:
        return fnmatch.fnmatchcase(part, " ".join(pat.split()))
    return part == " ".join(pat.split())


def granted(payload):
    """A grant counts only when EVERY install-like part of the command is covered by an active
    grant on a word boundary: `npm i lodash` does not cover `npm i lodash-evil`, and a grant for
    one command does not cover a second one chained after it."""
    try:
        sys.path.insert(0, HERE)
        import gt_grant
        if getattr(gt_grant, "_no_grants", None) and gt_grant._no_grants():
            return False
        if not gt_grant.is_granted(payload):
            return False
        tool = payload.get("tool_name")
        command = payload["tool_input"]["command"]
        ld = gt_grant.ld
        now = gt_grant._now()
        rules = [g["rule"] for g in ld.grants().values() if ld.grant_active(g, now)]
        parts = [x for x in command_parts(command) if precheck(x)]
        if not parts:
            return False
        return all(any(_rule_covers(r, tool, part, ld._split_rule) for r in rules)
                   for part in parts)
    except Exception:
        return False


def hook_decision(payload):
    """The whole hook, minus stdin/stdout. -> output dict or None."""
    if not isinstance(payload, dict) or payload.get("tool_name") not in ("Bash", "PowerShell"):
        return None
    ti = payload.get("tool_input")
    command = ti.get("command") if isinstance(ti, dict) else None
    if not isinstance(command, str) or not precheck(command):
        return None
    mode = mode_setting()
    if mode == "off":
        return None
    parsed = parse_command(command, payload.get("tool_name"))
    if parsed.empty():
        return None
    if granted(payload):
        return None
    try:
        summary = evaluate(parsed)
    except Exception as exc:
        summary = {"results": [], "serious": [], "other": [], "notes": list(parsed.notes),
                   "gaps": [("install_guard", "the check itself failed (%s)"
                             % exc.__class__.__name__)]}
    return decide(mode, summary, command)


def _start_watchdog(seconds, lock, written):
    """Ask, and kill every child group, if the check runs past `seconds`: the hook's own timeout
    must never be what ends it."""
    import threading

    def fire():
        with lock:
            if written:
                return
            written.append(1)
            for proc in list(_CHILDREN):
                _kill_tree(proc)
            out = {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "ask",
                "permissionDecisionReason": "gt install_guard could not finish its check in "
                                            "time -- an incomplete scan is not a pass."}}
            try:
                sys.stdout.write(json.dumps(out) + "\n")
                sys.stdout.flush()
            finally:
                os._exit(0)
    t = threading.Timer(seconds, fire)
    t.daemon = True
    t.start()
    return t


def hook():
    try:
        payload = json.loads(sys.stdin.read() or "null")
    except Exception:
        return 0
    _use_session(payload)
    import threading
    lock, written = threading.Lock(), []
    timer = None
    try:
        ti = payload.get("tool_input") if isinstance(payload, dict) else None
        if isinstance(ti, dict) and isinstance(ti.get("command"), str) and precheck(ti["command"]):
            timer = _start_watchdog(_budget_seconds() + WATCHDOG_EXTRA_S, lock, written)
        out = hook_decision(payload)
    except Exception:
        out = None
    with lock:
        if timer is not None:
            timer.cancel()
        if written:
            return 0
        written.append(1)
        if out:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
    return 0


# ============================================================================ CLI
EXIT = {"SAFE": 0, "CAUTION": 1, "INCOMPLETE": 3, "DO NOT INSTALL": 4}


def _spec_to_parsed(spec):
    p = Parsed()
    if spec.startswith("npm:"):
        _npm_spec(spec[4:], p, "npm")
    elif spec.startswith("pypi:"):
        _pypi_spec(spec[5:], p)
    elif re.match(r"^[\w.-]+/[\w.-]+$", spec):
        _git_source("https://github.com/%s" % spec, None, p, "scan")
    elif ARCHIVE.search(spec) and re.match(r"^(?:https|file)://", spec) and not spec.endswith(".git"):
        _archive_url(spec, p, "scan")
    else:
        _git_source(spec, None, p, "scan")
    return p


def cli(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="gt_install_guard.py",
                                 description="Scan what an install would bring in, without "
                                             "running any of it. Never prints matched text.")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("scan", help="scan one source or every target of a command")
    s.add_argument("spec", help="https URL, owner/repo, npm:pkg[@ver], pypi:pkg[==ver], or with "
                                "--command a whole command line")
    s.add_argument("--command", action="store_true", help="SPEC is a command line to analyse")
    s.add_argument("--json", action="store_true")
    s.add_argument("--budget", type=float, default=None, help="seconds (default %g)" % BUDGET_S)
    a = ap.parse_args(argv)
    if a.cmd != "scan":
        ap.print_help()
        return 2
    parsed = parse_command(a.spec) if a.command else _spec_to_parsed(a.spec)
    summary = evaluate(parsed, Budget(a.budget or _budget_seconds()))
    if a.json:
        print(json.dumps({"verdict": summary["verdict"], "counts": summary["counts"],
                          "results": summary["results"], "notes": summary["notes"]}, indent=2))
        return EXIT[summary["verdict"]]
    if parsed.empty():
        print("nothing to scan: no install target recognised")
    for r in summary["results"]:
        state = "INCOMPLETE" if r["gaps"] else ("FINDINGS" if r["findings"] else "clean")
        print("%s  (%s)  %s" % (r["label"], r["type"], state))
        for f in r["findings"][:50]:
            print("  %s%-16s %-30s %s:%d" % ("!! " if is_serious(f) else "   ", f["kind"],
                                             f["rule"], f["file"], f["line"]))
        if len(r["findings"]) > 50:
            print("  ... %d more (use --json)" % (len(r["findings"]) - 50))
        for g in r["gaps"]:
            print("  not scanned: %s" % g)
    for n in summary["notes"]:
        print("note: %s" % n)
    c = summary["counts"]
    print("VERDICT: %s -- serious %d, other %d, incomplete %d, notes %d, targets %d"
          % (summary["verdict"], c["serious"], c["other"], c["incomplete"], c["notes"],
             c["targets"]))
    return EXIT[summary["verdict"]]


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))
    sys.exit(hook())
