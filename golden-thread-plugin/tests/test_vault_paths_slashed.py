"""Vault-relative paths gt stores, prints or compares are "/"-separated on every OS (0.20.1).

On native Windows `str(p.relative_to(vault))` and `os.path.relpath(a, b)` give backslash paths
("Projects\\alpha\\research.md"). gt treats a vault-relative path as a logical key: it writes
it into vault files (frontmatter, claims, logs, indexes, lint findings, JSON state), compares
it with paths written with "/" (often on another machine, or by the model), prints it for the
model to copy, and matches it against "/"-separated patterns. The Windows 11 suite run of
2026-10-03 failed on exactly that shape in gt_lint, gt_handoff_status, gt_close, gt_surface,
gt_secrets, gt_intake_scan, gt_supersede, vault_init, inject_core_rules and more.

The fix is `rel.as_posix()` in place of `str(rel)`, and `os.path.relpath(a, b).replace(os.sep,
"/")` in place of a bare relpath. On macOS and Linux both are the identity, so POSIX output is
byte-for-byte unchanged.

A POSIX runner cannot make pathlib produce a WindowsPath (and patching os.name to pretend is
dishonest), so the structural guard is an AST scan of the release under test (gt plus the
lockstep modules) for the three shapes that leak a native separator into a string:

  * str(<x>.relative_to(...)) not followed by .replace(os.sep, "/")
  * <x>.relative_to(...) formatted straight into text: an f-string, "%" formatting,
    str.format(), print() or "+" concatenation
  * os.path.relpath(...) not followed by .replace(os.sep, "/") (or .split(os.sep), which
    compares native parts and never reaches text)

plus a line scan of the Python embedded in hooks/*.sh. A site where a NATIVE relative path is
right (handed straight to the OS, never stored or compared) goes in ALLOWLIST with the reason.
Keys are "<plugin>/<path in the version dir>:<enclosing function>:<call source>", so they
survive version bumps and line moves.

The behavioural tests are honest about their limits: os.path.relpath is a pure string function,
so patching the module's `os.path` to ntpath and `os.sep` to "\\" reproduces what Windows
returns for the helpers built on it. pathlib is NOT simulated -- that is what the AST guard is
for.
"""
import ast
import ntpath
import os
import re
import unittest
from pathlib import Path
from unittest import mock

from _harness import DEMO, FARM, FLOW, GT, REPORT_CARD, SCRIPTS, WATCH, load_module

ALLOWLIST = {
    # key -> reason. Only for relative paths that are handed to the OS and never stored,
    # compared with a "/"-written path, matched against a pattern or shown to the model.
    "golden-thread/scripts/gt_scan_language.py:walk:os.path.relpath(dirpath, root)":
        "native on purpose: rel_dir is only re-joined with os.path.join/normpath to build each "
        "entry's path, and every value that leaves walk() -- matched against the ignore and "
        "classify rules or yielded -- is normalised with .replace(os.sep, '/') first",
}

_SEP_NORMALISERS = ("replace",)


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _is_sep_arg(node):
    """os.sep / os.path.sep / "\\\\" -- the native separator as a replace() or split() arg."""
    if _dotted(node) in ("os.sep", "os.path.sep", "sep"):
        return True
    return isinstance(node, ast.Constant) and node.value == "\\"


def _normalised_by(parent, child):
    """True when `parent` is `<child>.replace(os.sep, "/")` (or `.split(os.sep)` for relpath)."""
    if not (isinstance(parent, ast.Attribute) and parent.value is child):
        return False
    call = getattr(parent, "_parent", None)
    if not (isinstance(call, ast.Call) and call.func is parent and call.args):
        return False
    return parent.attr in ("replace", "split") and _is_sep_arg(call.args[0])


def _into_text(node):
    """Where `node` (a relative_to(...) call) is turned into text without .as_posix()."""
    parent = node._parent
    while isinstance(parent, ast.IfExp) and parent.test is not node:   # f"{a.relative_to(b) if c else d}"
        node, parent = parent, parent._parent
    if isinstance(parent, ast.FormattedValue):
        return "relative_to() in an f-string"
    if isinstance(parent, ast.Tuple) and isinstance(parent._parent, ast.BinOp) \
            and isinstance(parent._parent.op, ast.Mod) and parent._parent.right is parent:
        return "relative_to() in %-formatting"
    if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Mod) and parent.right is node:
        return "relative_to() in %-formatting"
    if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Add):
        return "relative_to() in string concatenation"
    if isinstance(parent, ast.Call) and node in parent.args:
        callee = _dotted(parent.func) or ""
        if callee == "print" or (isinstance(parent.func, ast.Attribute)
                                 and parent.func.attr == "format"):
            return "relative_to() passed to %s" % (callee or ".format")
    return None


def scan_source(text, rel):
    """[(key, lineno, what)] for every site that leaks a native separator into a string."""
    tree = ast.parse(text, filename=rel)
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child._parent = node
    found = []

    def report(node, func, what):
        seg = re.sub(r"\s+", " ", ast.get_source_segment(text, node) or "")[:80]
        found.append(("%s:%s:%s" % (rel, func, seg), node.lineno, what))

    def visit(node, func):
        for child in ast.iter_child_nodes(node):
            name = func
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = child.name if func == "<module>" else "%s.%s" % (func, child.name)
            if isinstance(child, ast.Call):
                classify(child, name)
            visit(child, name)

    def classify(call, func):
        parent = call._parent
        callee = _dotted(call.func) or ""
        if isinstance(call.func, ast.Attribute) and call.func.attr == "relative_to":
            if isinstance(parent, ast.Call) and _dotted(parent.func) == "str" \
                    and parent.args and parent.args[0] is call:
                if not _normalised_by(parent._parent, parent):
                    report(parent, func, "str(relative_to()) keeps the native separator")
                return
            what = _into_text(call)
            if what:
                report(call, func, what)
        elif callee in ("os.path.relpath", "relpath", "path.relpath"):
            if not _normalised_by(parent, call):
                report(call, func, "os.path.relpath() keeps the native separator")

    visit(tree, "<module>")
    return found


_SH_PATTERNS = (
    (re.compile(r"str\([^()]*\.relative_to\("), "str(relative_to()) in embedded Python"),
    (re.compile(r"os\.path\.relpath\("), "os.path.relpath() in embedded Python"),
)


def scan_shell(text, rel):
    """Line scan of the Python a hook script embeds (inject_core_rules.sh has a heredoc)."""
    found = []
    for n, line in enumerate(text.splitlines(), 1):
        if "replace(os.sep" in line or "as_posix()" in line:
            continue
        for rx, what in _SH_PATTERNS:
            if rx.search(line):
                found.append(("%s:<sh>:%s" % (rel, line.strip()[:80]), n, what))
    return found


def shipped_files():
    """(plugin-relative name, path) for every .py and hooks/*.sh in the release under test."""
    out = []
    for vdir in (GT, DEMO, WATCH, REPORT_CARD, FARM, FLOW):
        if vdir is None:
            continue
        for p in sorted(list(vdir.rglob("*.py")) + list(vdir.rglob("*.sh"))):
            if "__pycache__" in p.parts:
                continue
            out.append(("%s/%s" % (vdir.parent.name, p.relative_to(vdir).as_posix()), p))
    return out


def scan_release():
    hits = []
    for rel, path in shipped_files():
        text = path.read_text(encoding="utf-8")
        scan = scan_source if path.suffix == ".py" else scan_shell
        hits += [(key, path, line, what) for key, line, what in scan(text, rel)]
    return hits


class RelativePathsAreSlashed(unittest.TestCase):
    def test_no_native_separator_reaches_a_vault_relative_string(self):
        bad, seen = [], set()
        hits = scan_release()
        self.assertGreater(len(shipped_files()), 50, "the scan found too few files to be gt")
        for key, path, line, what in hits:
            seen.add(key)
            if key not in ALLOWLIST:
                bad.append("%s:%d: %s\n    key: %s" % (path, line, what, key))
        self.assertEqual(
            bad, [],
            "%d relative path(s) would carry backslashes on native Windows. Use "
            "rel.as_posix() for a pathlib relative path and os.path.relpath(a, b).replace("
            "os.sep, \"/\") for os.path; if the path is handed straight to the OS and never "
            "stored, compared or shown, add its key to ALLOWLIST in this file with the "
            "reason:\n%s" % (len(bad), "\n".join(bad)))
        stale = sorted(set(ALLOWLIST) - seen)
        self.assertEqual(stale, [], "ALLOWLIST keys that match no site any more -- remove "
                                    "them:\n" + "\n".join(stale))

    def test_the_scan_catches_the_shapes_it_is_for(self):
        src = (
            "import os\n"
            "from os.path import relpath\n"
            "def f(p, v):\n"
            "    a = str(p.relative_to(v))\n"
            "    b = f'{p.relative_to(v)}'\n"
            "    c = '%s' % p.relative_to(v)\n"
            "    d = '%s %s' % (p.relative_to(v), a)\n"
            "    e = os.path.relpath(p, v)\n"
            "    g = relpath(p, v)\n"
            "    print(p.relative_to(v))\n"
            "    h = '{}'.format(p.relative_to(v))\n"
            "    i = f'{p.relative_to(v) if v else p}'\n"
            # not flagged:
            "    k = p.relative_to(v).as_posix()\n"
            "    m = str(p.relative_to(v)).replace(os.sep, '/')\n"
            "    n = os.path.relpath(p, v).replace(os.sep, '/')\n"
            "    o = os.path.relpath(p, v).split(os.sep)\n"
            "    q = p.relative_to(v).parts\n"
            "    r = f'{p.relative_to(v).as_posix()}'\n"
            "    s = v / p.relative_to(v)\n")
        hits = scan_source(src, "x.py")
        self.assertEqual([h[1] for h in hits], [4, 5, 6, 7, 8, 9, 10, 11, 12], hits)
        self.assertTrue(all(h[0].startswith("x.py:f:") for h in hits), hits)

    def test_the_shell_scan_reads_embedded_python(self):
        src = ("x=$(python3 - <<'PY'\n"
               "where = str(core.relative_to(vault)) if core else 'core-rules/'\n"
               "ok = core.relative_to(vault).as_posix()\n"
               "PY\n)\n")
        self.assertEqual([h[1] for h in scan_shell(src, "x.sh")], [2])


class WindowsRelpathSimulated(unittest.TestCase):
    """os.path.relpath is pure string work, so ntpath stands in for it honestly: patch the
    module's os.path to ntpath and os.sep to "\\" and the helper sees exactly what native
    Windows hands it. Only os.path-based helpers can be simulated this way -- pathlib cannot
    build a WindowsPath on POSIX, and patching os.name to pretend is not a simulation."""

    def _under_windows(self, fn, *args):
        with mock.patch.object(os, "path", ntpath), mock.patch.object(os, "sep", "\\"):
            return fn(*args)

    def test_the_simulation_is_real(self):
        """Without the fix's normalisation, the simulated relpath IS backslashed -- so the
        tests below would fail on an unfixed helper rather than pass by construction."""
        got = self._under_windows(lambda *a: os.path.relpath(*a), "C:\\v\\Projects\\a\\x.md", "C:\\v")
        self.assertEqual(got, "Projects\\a\\x.md")

    def test_relpath_helper_is_slashed_under_windows(self):
        mod = load_module(SCRIPTS / "gt_demote.py", "gt_demote_slashed")
        got = self._under_windows(mod._rel, "C:\\v", "C:\\v\\Projects\\a\\memory\\x.md")
        self.assertEqual(got, "Projects/a/memory/x.md")

    def test_withheld_name_keeps_its_slashed_folder_under_windows(self):
        """gt_intake_scan.display() rebuilt the shown path with os.path.join, so on Windows a
        withheld file NAME came back as notes\\[name withheld...] beside "/" paths."""
        mod = load_module(SCRIPTS / "gt_intake_scan.py", "gt_intake_scan_slashed")
        rel = "notes/deep/ignore_all_previous_instructions.md"
        got = self._under_windows(mod.display, rel)
        self.assertTrue(got.startswith("notes/deep/[name withheld: inject."), got)
        self.assertEqual(got, mod.display(rel), "POSIX output must be unchanged")

    @unittest.skipIf(WATCH is None, "this tree does not ship gt-watch")
    def test_watch_slug_of_a_windows_local_clone_is_its_last_segment(self):
        mod = load_module(WATCH / "scripts" / "gt_watch.py", "gt_watch_slashed")
        url = "C:\\Users\\gt\\AppData\\Local\\Temp\\gt-test-x\\remote\\widget-lib.git"
        self.assertEqual(self._under_windows(mod.slugify, url), "widget-lib")
        self.assertEqual(mod.slugify("https://github.com/o/widget-lib.git"), "widget-lib")


if __name__ == "__main__":
    unittest.main()
