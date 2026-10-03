"""Every text-mode file write in shipped gt code says newline="\\n" (0.20.0).

On native Windows, Python's text mode turns every "\\n" written into "\\r\\n". 0.19.2 found it
in vault_init.py and gt_upgrade.py: a vault built on Windows differed byte for byte from the
templates it came from, so every tool read as "modified locally". The same translation applies
to every other vault, config and state file gt writes, so the guard is structural: an AST scan
of the release under test (gt plus the lockstep modules) for a text-mode write that does not
pass newline=, and for Path.write_text at all -- its newline= arrived in Python 3.10 and gt
supports 3.8, so a write_text call cannot be made LF-safe and must become open(..., "w",
encoding="utf-8", newline="\\n") or write_bytes(text.encode("utf-8")).

There is no honest way to make POSIX Python translate newlines the way Windows does (the
translation is os.linesep, fixed at interpreter build; -X utf8 changes the encoding, not the
newline), so the AST scan IS the guard. The behavioural tests below only pin that the
representative writers emit LF bytes and that the scan catches the shapes it is for.

A write that must not be LF (preserving a file's own CRLF, say) goes in ALLOWLIST with the
reason. Keys are "<plugin>/<path in the version dir>:<enclosing function>:<call source>", so
they survive version bumps and line moves.
"""
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _harness import DEMO, FARM, FLOW, GT, PYTHON, REPORT_CARD, SCRIPTS, TOOLS, WATCH

ALLOWLIST = {
    # key -> reason. Filled only for writes that deliberately are not "\n"-terminated text.
}

# Opener call shapes: name -> index of the positional mode argument.
_OPEN_FUNCS = {"open": 1, "io.open": 1, "codecs.open": 1, "os.fdopen": 1,
               "gzip.open": 1, "bz2.open": 1, "lzma.open": 1}
_TEMPFILE_FUNCS = {"tempfile.NamedTemporaryFile", "tempfile.TemporaryFile",
                   "tempfile.SpooledTemporaryFile", "NamedTemporaryFile", "TemporaryFile",
                   "SpooledTemporaryFile"}
# `<obj>.open(...)` callees that are not a text file (zip members, tar archives, browsers...).
_NOT_FILE_OPENERS = {"os.open", "webbrowser.open", "tarfile.open", "zipfile.open", "shelve.open",
                     "dbm.open", "wave.open", "sqlite3.connect"}


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _mode_arg(call, pos):
    for kw in call.keywords:
        if kw.arg == "mode":
            return kw.value
    if len(call.args) > pos:
        return call.args[pos]
    return None


def _is_text_write_mode(node, default):
    """True / False for a constant mode, None for one computed at run time."""
    if node is None:
        mode = default
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        mode = node.value
    else:
        return None
    if ":" in mode or "b" in mode:
        return False
    return any(c in mode for c in "wax+")


def scan_source(text, rel):
    """[(key, lineno, what)] for every text-mode write in `text` that lacks newline=."""
    tree = ast.parse(text, filename=rel)
    found = []

    def visit(node, func):
        for child in ast.iter_child_nodes(node):
            name = func
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = child.name if func == "<module>" else "%s.%s" % (func, child.name)
            if isinstance(child, ast.Call):
                what = classify(child)
                if what:
                    seg = ast.get_source_segment(text, child) or ""
                    seg = re.sub(r"\s+", " ", seg)[:70]
                    found.append(("%s:%s:%s" % (rel, name, seg), child.lineno, what))
            visit(child, name)

    def classify(call):
        kws = {k.arg for k in call.keywords}
        if "newline" in kws or None in kws:          # newline= given, or **kwargs passed on
            return None
        callee = _dotted(call.func)
        if isinstance(call.func, ast.Attribute) and call.func.attr == "write_text":
            return "write_text (cannot carry newline= on Python 3.8)"
        if callee in _OPEN_FUNCS:
            text_w = _is_text_write_mode(_mode_arg(call, _OPEN_FUNCS[callee]), "r")
        elif callee in _TEMPFILE_FUNCS:
            text_w = _is_text_write_mode(_mode_arg(call, 0), "w+b")
        elif (isinstance(call.func, ast.Attribute) and call.func.attr == "open"
              and callee not in _NOT_FILE_OPENERS):
            text_w = _is_text_write_mode(_mode_arg(call, 0), "r")   # Path.open(mode)
        else:
            return None
        if text_w is None:
            return "open with a computed mode and no newline="
        return "text-mode write without newline=" if text_w else None

    visit(tree, "<module>")
    return found


def shipped_python():
    """(plugin-relative name, path) for every .py in the release under test."""
    out = []
    for vdir in (GT, DEMO, WATCH, REPORT_CARD, FARM, FLOW):
        if vdir is None:
            continue
        for p in sorted(vdir.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            out.append(("%s/%s" % (vdir.parent.name, p.relative_to(vdir).as_posix()), p))
    return out


class TextWritesAreLF(unittest.TestCase):
    def test_every_text_mode_write_passes_newline(self):
        bad, seen = [], set()
        files = shipped_python()
        self.assertGreater(len(files), 50, "the scan found too few files to be scanning gt")
        for rel, path in files:
            for key, line, what in scan_source(path.read_text(encoding="utf-8"), rel):
                seen.add(key)
                if key not in ALLOWLIST:
                    bad.append("%s:%d: %s\n    key: %s" % (path, line, what, key))
        self.assertEqual(
            bad, [],
            "%d text-mode write(s) would write CRLF on native Windows. Write with "
            "open(path, \"w\", encoding=\"utf-8\", newline=\"\\n\") (or write_bytes(text."
            "encode(\"utf-8\")) in place of write_text); if the write must keep other line "
            "endings, add its key to ALLOWLIST in this file with the reason:\n%s"
            % (len(bad), "\n".join(bad)))
        stale = sorted(set(ALLOWLIST) - seen)
        self.assertEqual(stale, [], "ALLOWLIST keys that match no write any more -- remove "
                                    "them:\n" + "\n".join(stale))

    def test_the_scan_catches_the_shapes_it_is_for(self):
        src = (
            "import os, tempfile\n"
            "from pathlib import Path\n"
            "def f(p, fd, m):\n"
            "    Path(p).write_text('x', encoding='utf-8')\n"
            "    open(p, 'w', encoding='utf-8')\n"
            "    open(p,\n         mode='a',\n         encoding='utf-8')\n"
            "    Path(p).open('x')\n"
            "    os.fdopen(fd, 'w')\n"
            "    tempfile.NamedTemporaryFile('w', dir=p, delete=False)\n"
            "    open(p, m)\n"
            # not flagged:
            "    open(p, 'w', encoding='utf-8', newline='\\n')\n"
            "    open(p, 'wb')\n"
            "    open(p)\n"
            "    Path(p).open()\n"
            "    tempfile.NamedTemporaryFile(dir=p)\n"
            "    Path(p).write_bytes(b'x')\n"
            "    open(p, 'w', newline='')\n")
        hits = scan_source(src, "x.py")
        self.assertEqual([h[1] for h in hits], [4, 5, 6, 9, 10, 11, 12], hits)
        self.assertTrue(all(h[0].startswith("x.py:f:") for h in hits), hits)


class LowLevelOpensAreBinary(unittest.TestCase):
    """os.open() on Windows opens a CRT descriptor in TEXT mode unless O_BINARY is given, and
    every write through it (os.write, or os.fdopen(fd, "w", newline=...)) then turns "\n" into
    "\r\n" below Python's own newline handling. Six such writers were found on 2026-10-03."""

    def test_every_os_open_for_writing_passes_o_binary(self):
        bad = []
        for rel, path in shipped_python():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and _dotted(node.func) == "os.open"
                        and len(node.args) >= 2):
                    continue
                flags = ast.unparse(node.args[1]) if hasattr(ast, "unparse") else ""
                if flags and re.search(r"O_WRONLY|O_RDWR", flags) and "O_BINARY" not in flags:
                    bad.append("%s:%d: os.open(..., %s)" % (path, node.lineno, flags))
        self.assertEqual(bad, [], "add | getattr(os, \"O_BINARY\", 0) to the flags:\n"
                                  + "\n".join(bad))


class RepresentativeWritersEmitLF(unittest.TestCase):
    """Bytes on disk carry no CR. On POSIX this held before the fix too; it pins that the
    LF-safe rewrites did not change what macOS and Linux write."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gt-lf-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _safe_write(self):
        sys.path.insert(0, str(TOOLS))
        try:
            import safe_write                                    # noqa: PLC0415
        finally:
            sys.path.remove(str(TOOLS))
        return safe_write

    def test_safe_write_writes_lf_in_every_text_mode(self):
        sw = self._safe_write()
        target = self.tmp / "note.md"
        path, strategy = sw.write(str(target), "one\ntwo\n")
        self.assertEqual((path, strategy), (str(target.resolve()), "atomic"))
        path, strategy = sw.write(str(target), "three\n", mode="a")
        self.assertEqual(strategy, "direct")
        self.assertEqual(target.read_bytes(), b"one\ntwo\nthree\n")
        # bytes stay bytes: a CRLF the caller wrote on purpose survives untouched
        sw.write(str(target), b"crlf\r\nkept\r\n")
        self.assertEqual(target.read_bytes(), b"crlf\r\nkept\r\n")

    def test_gt_settings_set_writes_lf(self):
        home = self.tmp / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "vault-config.json").write_bytes(
            json.dumps({"vault_path": str(self.tmp / "vault")}).encode("utf-8"))
        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
        r = subprocess.run([PYTHON, str(SCRIPTS / "gt_settings.py"), "set", "vault_hints", "on"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        raw = (home / ".claude" / "vault-config.json").read_bytes()
        self.assertNotIn(b"\r", raw)
        self.assertTrue(raw.endswith(b"}\n"), raw[-20:])
        self.assertIn(b"\n", raw.rstrip(b"\n"), "indented JSON spans lines; the check needs some")
        self.assertEqual(json.loads(raw.decode("utf-8")).get("vault_hints"), "on")


if __name__ == "__main__":
    unittest.main()
