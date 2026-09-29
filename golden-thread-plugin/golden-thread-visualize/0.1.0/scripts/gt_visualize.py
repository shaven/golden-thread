#!/usr/bin/env python3
"""Golden Thread visualize: show a codebase in 3D, in one offline HTML file.

    gt_visualize.py explain STORY.json [--out FILE|DIR] [--check] [--vault V]
    gt_visualize.py render [PATH] [--out FILE|DIR] [--since DAYS] [--no-churn] [--redact]
                           [--exclude GLOB ...] [--max-files N] [--vault V]

## explain: how the parts work together

A scroll-driven walkthrough. STORY.json names the parts of a system, the links between
them and an ordered list of scenes; each scene shows some parts, focuses one or two, and
animates flows along links (data, rule, return, or block -- stopped at a gate). A narrative
column scrolls beside the 3D stage and drives it. The story is written by the skill from
the code and the vault; nothing about any one codebase is built in. `--check` validates the
story and writes nothing; every problem is listed, and a story with any is never rendered.

## render: the code city

## What it draws

Directories are DISTRICTS -- flat plots, nested so a directory's plot contains its
children's. Files are BUILDINGS standing in their directory's district:

  height     lines of text (binary files are flat grey slabs with no line count)
  footprint  file size in bytes, on a log scale, so a large data file is wide and short
  colour     language, or -- toggled in the page -- how often the file changed in the last
             --since days (cold to hot), from `git log`

The layout is a squarified treemap computed HERE, not in the browser, and embedded in the
page as data: every property worth testing (containment, no sibling overlap, counts) is
testable without a browser. The page only draws it, with three.js.

## What it reads, what it writes

Reads the tree at PATH (default: the current directory). Inside a git work tree the file
list is `git ls-files --cached --others --exclude-standard`, so `.gitignore` is honoured and
`.git/` is never read; outside one it walks the directory, skipping `.git/`. Churn is
`git log --since` per path and is simply off outside a git repository (the output line says
so). `--exclude` globs match the repo-relative path or the basename.

Language comes from gt's own `filetype` definitions (the ones `gt_scan_language.py` uses)
when a gt release can be found beside this module or in the plugin cache; a file those
definitions do not know falls back to a short built-in suffix map, then to `other`.

It writes ONE file: `--out`, or `gt-visualize-<name>-<stamp>.html` in the current directory
-- or in a fresh temp directory when the current directory is inside the vault. Nothing is
ever written inside the vault; an `--out` pointing into it is refused.

## Offline

three.js (MIT, version and hash in scripts/vendor/VENDOR.json) is inlined from there, so the
page makes no network request of any kind: no CDN, no fonts, no fetch.

## --redact

Every path segment becomes a salted hash (`d-3fa2`, `f-91c0`), extensions kept, so the
tree's shape survives and its names do not. The same segment hashes the same way within a
render. The salt is random per render unless `--salt` is given (tests use it to prove
stability); it is never written to the page. After building, the page is checked for every
original segment of four or more characters and the render is refused if one survives.
Anything published or shared must be rendered with --redact.

## Exit codes

0 written · 1 bad arguments, an unreadable path, or a refused --out · 3 no files to draw.

Standard library only, Python 3.8+.
"""
import argparse
import fnmatch
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.dirname(HERE)
# Under scripts/ because install.sh copies a fixed list of module directories and scripts/
# is one of them; a top-level vendor/ would silently not be installed.
VENDOR = os.path.join(HERE, "vendor", "three-bundle.min.js")
# MANIFEST.json covers the bundle, but only when something checks the manifest; this pin makes
# every render check it, and refuse to inline a bundle that is not the one VENDOR.json records.
THREE_SHA256 = "84747b01d6856104f9888ffa55707dfbfd5f5c9e2b9fe6c116b21fa993e4c7a6"
DEFAULT_MAX_FILES = 20000
SAFE_EXT = re.compile(r"(\.[A-Za-z0-9]{1,6})?")
SNIFF = 8192

# Used only for files gt's filetype definitions do not recognise.
FALLBACK_LANG = {
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "css", ".less": "css",
    ".swift": "swift", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala",
    ".lua": "lua", ".pl": "perl", ".pm": "perl", ".r": "r", ".m": "objc",
    ".toml": "toml", ".ini": "ini", ".cfg": "ini", ".xml": "xml", ".svg": "svg",
    ".txt": "text", ".rst": "text", ".csv": "data", ".tsv": "data", ".ps1": "powershell",
    ".bat": "batch", ".cmd": "batch", ".vue": "vue", ".svelte": "svelte", ".dart": "dart",
    ".ex": "elixir", ".exs": "elixir", ".erl": "erlang", ".hs": "haskell", ".clj": "clojure",
    ".tf": "terraform", ".proto": "protobuf", ".graphql": "graphql", ".mk": "make",
}
FALLBACK_NAMES = {"Makefile": "make", "Dockerfile": "docker", "CMakeLists.txt": "cmake"}


class Refused(Exception):
    def __init__(self, code, msg):
        Exception.__init__(self, msg)
        self.code = code


# -- vault -------------------------------------------------------------------------------
def find_vault(explicit):
    """The vault an --out must stay out of: --vault, $GT_VAULT, then vault-config.json."""
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    env = os.environ.get("GT_VAULT")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    cfg = os.path.expanduser("~/.claude/vault-config.json")
    try:
        with open(cfg, encoding="utf-8") as fh:
            v = json.load(fh).get("vault_path")
        return os.path.abspath(os.path.expanduser(v)) if v else None
    except (OSError, ValueError, AttributeError):
        return None


def _inside(path, root):
    if not root:
        return False
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def resolve_out(out, vault, name, now):
    fname = "gt-visualize-%s-%s.html" % (re.sub(r"[^A-Za-z0-9._-]+", "-", name) or "repo",
                                          now.strftime("%Y%m%d-%H%M%S"))
    if out is None:
        base = os.getcwd()
        if _inside(base, vault):
            base = tempfile.mkdtemp(prefix="gt-visualize-")
        path = os.path.join(base, fname)
    elif os.path.isdir(out):
        path = os.path.join(out, fname)
    else:
        path = out
    parent = os.path.dirname(os.path.abspath(path)) or "."
    if _inside(parent, vault) or _inside(path, vault):
        raise Refused(1, "--out %s is inside the vault; gt-visualize never writes into the "
                         "vault. Choose a path outside it (or omit --out)." % path)
    if not os.path.isdir(parent):
        raise Refused(1, "--out %s: the directory does not exist" % path)
    return path


# -- language: gt's filetype definitions first -------------------------------------------
def _newest(root, sub, marker):
    try:
        vs = [d for d in os.listdir(root) if re.fullmatch(r"\d+\.\d+\.\d+", d)
              and os.path.isfile(os.path.join(root, d, sub, marker))]
    except OSError:
        return None
    if not vs:
        return None
    return os.path.join(root, max(vs, key=lambda s: tuple(int(x) for x in s.split("."))), sub)


def gt_scripts_dirs():
    """Where gt_scan_language.py may live, first hit wins: the gt release beside this module
    (plugin cache or source checkout), $GT_CORE_SCRIPTS, then the home plugin cache."""
    parent = os.path.dirname(os.path.dirname(MODULE))
    marker = "gt_scan_language.py"
    out = [_newest(os.path.join(parent, "gt"), "scripts", marker),
           _newest(os.path.join(parent, "golden-thread"), "scripts", marker)]
    env = os.environ.get("GT_CORE_SCRIPTS")
    if env:
        out.append(env)
    out.append(_newest(os.path.expanduser("~/.claude/plugins/cache/golden-thread-plugin/gt"),
                       "scripts", marker))
    return [d for d in out if d and os.path.isfile(os.path.join(d, marker))]


def load_gt_language(vault):
    """-> (lang_of(rel) -> str|None, source label). Never raises: gt missing or its registry
    failing only means the built-in map is used, and the label says so."""
    import importlib.util
    for d in gt_scripts_dirs():
        added = d not in sys.path
        if added:
            sys.path.insert(0, d)
        try:
            spec = importlib.util.spec_from_file_location(
                "gt_scan_language_for_visualize", os.path.join(d, "gt_scan_language.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            entries, _problems = mod._entries("filetype", vault)
            filetypes = {e["match"]: e["lang"] for e in entries}
            if filetypes:
                return (lambda rel, _f=filetypes, _m=mod: _m.lang_of(rel, _f)), \
                    os.path.join(d, "gt_scan_language.py")
        except Exception:
            pass
        finally:
            if added and d in sys.path:
                sys.path.remove(d)
    return (lambda rel: None), "built-in suffix map (gt not found)"


def fallback_lang(rel):
    base = os.path.basename(rel)
    if base in FALLBACK_NAMES:
        return FALLBACK_NAMES[base]
    return FALLBACK_LANG.get(os.path.splitext(base)[1].lower(), "other")


# -- the file list -----------------------------------------------------------------------
def git_root(path):
    try:
        r = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return os.path.realpath(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() \
        else None


def list_files(root, in_git):
    if in_git:
        r = subprocess.run(["git", "-C", root, "ls-files", "-z", "--cached", "--others",
                            "--exclude-standard"], capture_output=True, timeout=300)
        if r.returncode != 0:
            raise Refused(1, "git ls-files failed in %s: %s"
                          % (root, r.stderr.decode("utf-8", "replace").strip()))
        rels = [p for p in r.stdout.decode("utf-8", "replace").split("\0") if p]
        # A path deleted from the tree but still in the index has nothing to draw.
        return sorted(p for p in set(rels) if os.path.isfile(os.path.join(root, p)))
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if d != ".git")
        for f in fns:
            full = os.path.join(dp, f)
            if os.path.isfile(full) and not os.path.islink(full):
                out.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return sorted(out)


def excluded(rel, globs):
    base = os.path.basename(rel)
    return any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(base, g) or
               rel.startswith(g.rstrip("/") + "/") for g in globs)


def measure(full):
    """-> (bytes, lines or None when binary, binary)."""
    size = os.path.getsize(full)
    with open(full, "rb") as fh:
        head = fh.read(SNIFF)
        if b"\0" in head:
            return size, None, True
        lines, last = head.count(b"\n"), head[-1:] if head else b""
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            lines += chunk.count(b"\n")
            last = chunk[-1:]
    if size and last != b"\n":
        lines += 1          # a last line without a newline is still a line (as wc -l is not)
    return size, lines, False


def churn(root, days):
    """-> {rel: commits touching it in the last `days` days}."""
    r = subprocess.run(["git", "-C", root, "log", "--since=%d.days" % days, "--no-renames",
                        "--format=", "--name-only", "-z"], capture_output=True, timeout=600)
    if r.returncode != 0:
        return {}
    counts = {}
    for p in r.stdout.decode("utf-8", "replace").replace("\n", "\0").split("\0"):
        if p:
            counts[p] = counts.get(p, 0) + 1
    return counts


# -- layout: squarified treemap ----------------------------------------------------------
def footprint(size):
    return 1.0 + math.log2(1 + size) ** 1.5


def _worst(row, side):
    s = sum(row)
    if s <= 0 or side <= 0:
        return float("inf")
    return max(max(side * side * r / (s * s), (s * s) / (side * side * r)) for r in row)


def squarify(weights, x, y, w, h):
    """-> one rect (x, y, w, h) per weight, in input order, tiling (x, y, w, h) exactly."""
    total = float(sum(weights))
    if not weights or total <= 0 or w <= 0 or h <= 0:
        return [(x, y, 0.0, 0.0) for _ in weights]
    order = sorted(range(len(weights)), key=lambda i: -weights[i])
    scale = w * h / total
    areas = [weights[i] * scale for i in order]
    rects = [None] * len(weights)
    i = 0
    while i < len(areas):
        side = min(w, h)
        row = [areas[i]]
        j = i + 1
        while j < len(areas) and _worst(row + [areas[j]], side) <= _worst(row, side):
            row.append(areas[j])
            j += 1
        s = sum(row)
        if w >= h:              # lay the row as a column on the left
            cw = s / h if h else 0
            cy = y
            for k, a in enumerate(row):
                ch = a / cw if cw else 0
                if i + k == len(areas) - 1 or k == len(row) - 1:
                    ch = y + h - cy
                rects[order[i + k]] = (x, cy, cw, ch)
                cy += ch
            x, w = x + cw, w - cw
        else:                   # lay the row as a strip along the top
            rh = s / w if w else 0
            cx = x
            for k, a in enumerate(row):
                cw2 = a / rh if rh else 0
                if k == len(row) - 1:
                    cw2 = x + w - cx
                rects[order[i + k]] = (cx, y, cw2, rh)
                cx += cw2
            y, h = y + rh, h - rh
        i = j
    return rects


class Node:
    __slots__ = ("name", "path", "children", "files", "weight")

    def __init__(self, name, path):
        self.name, self.path, self.children, self.files, self.weight = name, path, {}, [], 0.0


def build_tree(records):
    root = Node("", "")
    for rec in records:
        parts = rec["path"].split("/")
        node = root
        for k, seg in enumerate(parts[:-1]):
            p = "/".join(parts[:k + 1])
            node = node.children.setdefault(seg, Node(seg, p))
        node.files.append(rec)

    def weigh(n):
        n.weight = sum(footprint(f["bytes"]) for f in n.files) + \
            sum(weigh(c) for c in n.children.values())
        return n.weight
    weigh(root)
    return root


def collapse(records, max_files):
    """Merge the deepest directories into single buildings until the count fits.
    -> (records, depth cut or None)."""
    if len(records) <= max_files:
        return records, None
    depth = max(r["path"].count("/") for r in records)
    while depth > 0:
        depth -= 1
        groups, out = {}, []
        for r in records:
            parts = r["path"].split("/")
            if len(parts) - 1 > depth:
                key = "/".join(parts[:depth + 1])
                groups.setdefault(key, []).append(r)
            else:
                out.append(r)
        for key, rs in groups.items():
            langs = {}
            for r in rs:
                langs[r["lang"]] = langs.get(r["lang"], 0) + (r["lines"] or 0) + 1
            out.append({"path": key, "lang": max(langs, key=langs.get),
                        "bytes": sum(r["bytes"] for r in rs),
                        "lines": sum(r["lines"] or 0 for r in rs),
                        "churn": sum(r["churn"] for r in rs), "binary": False,
                        "collapsed": len(rs)})
        if len(out) <= max_files:
            return sorted(out, key=lambda r: r["path"]), depth
    return records, None


def layout(root):
    """-> (dirs, files): dirs with rects, files with rects inside their dir's inner rect."""
    side = math.sqrt(max(root.weight, 1.0)) * 2.2
    dirs, files = [], []

    def place(node, x, y, w, h, depth, parent):
        idx = len(dirs)
        dirs.append({"path": node.path, "name": node.name, "parent": parent, "depth": depth,
                     "x": x, "y": y, "w": w, "h": h,
                     "files": 0, "lines": 0, "bytes": 0})
        pad = min(w, h) * 0.035 + (0.6 if min(w, h) > 3 else 0)
        pad = min(pad, min(w, h) * 0.2)
        ix, iy, iw, ih = x + pad, y + pad, max(w - 2 * pad, 0), max(h - 2 * pad, 0)
        kids = sorted(node.children.values(), key=lambda n: n.name)
        items = [("d", k, k.weight) for k in kids] + \
                [("f", f, footprint(f["bytes"])) for f in sorted(node.files,
                                                               key=lambda f: f["path"])]
        rects = squarify([it[2] for it in items], ix, iy, iw, ih)
        for (kind, obj, _w), (rx, ry, rw, rh) in zip(items, rects):
            if kind == "d":
                place(obj, rx, ry, rw, rh, depth + 1, idx)
            else:
                m = min(rw, rh) * 0.12          # a gap between buildings on the plot
                f = dict(obj)
                f.update({"dir": idx, "x": rx + m, "y": ry + m,
                          "w": max(rw - 2 * m, 0), "h": max(rh - 2 * m, 0)})
                files.append(f)
        d = dirs[idx]
        return d

    place(root, 0.0, 0.0, side, side, 0, None)
    # Aggregate counts up the tree for the hover panel.
    for f in files:
        i = f["dir"]
        while i is not None:
            dirs[i]["files"] += f.get("collapsed", 1)
            dirs[i]["lines"] += f["lines"] or 0
            dirs[i]["bytes"] += f["bytes"]
            i = dirs[i]["parent"]
    return dirs, files


# -- redaction ---------------------------------------------------------------------------
class Namer:
    def __init__(self, redact, salt=None):
        self.redact = redact
        self.salt = (salt.encode("utf-8") if salt else os.urandom(16)) if redact else b""
        self.seen = {}

    def seg(self, prefix, s):
        if not self.redact or not s:
            return s
        key = (prefix, s)
        if key not in self.seen:
            stem, ext = os.path.splitext(s) if prefix == "f" else (s, "")
            if not SAFE_EXT.fullmatch(ext):
                # ".code-workspace" carries a word; only a short plain extension is kept.
                stem, ext = s, ""
            d = hashlib.sha256(self.salt + prefix.encode() + b"\0" + stem.encode("utf-8"))
            self.seen[key] = "%s-%s%s" % (prefix, d.hexdigest()[:6], ext)
        return self.seen[key]

    def path(self, p, is_file):
        if not self.redact or not p:
            return p
        parts = p.split("/")
        out = [self.seg("d", s) for s in parts[:-1]]
        out.append(self.seg("f" if is_file else "d", parts[-1]))
        return "/".join(out)


# -- page --------------------------------------------------------------------------------
def _num(v):
    return round(v, 3) if isinstance(v, float) else v


def payload(dirs, files, meta, namer):
    d_out = [{"p": namer.path(d["path"], False), "n": namer.seg("d", d["name"]),
              "u": d["parent"], "k": d["depth"],
              "x": _num(d["x"]), "y": _num(d["y"]), "w": _num(d["w"]), "h": _num(d["h"]),
              "fc": d["files"], "lc": d["lines"], "bc": d["bytes"]} for d in dirs]
    f_out = []
    for f in files:
        rec = {"p": namer.path(f["path"], "collapsed" not in f), "d": f["dir"],
               "g": f["lang"], "l": f["lines"], "b": f["bytes"], "c": f["churn"],
               "x": _num(f["x"]), "y": _num(f["y"]), "w": _num(f["w"]), "h": _num(f["h"])}
        if f["binary"]:
            rec["bin"] = 1
        if "collapsed" in f:
            rec["n"] = f["collapsed"]
        f_out.append(rec)
    return {"meta": meta, "dirs": d_out, "files": f_out}


def sensitive(records, root_name):
    out = set()
    for r in records:
        for seg in r["path"].split("/"):
            stem = os.path.splitext(seg)[0]
            for s in (seg, stem):
                if len(s) >= 4:
                    out.add(s)
    if root_name and len(root_name) >= 4:
        out.add(root_name)
    return out


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#0f1115;--panel:rgba(22,25,32,.92);--fg:#e6e8ee;--dim:#98a0b3;--line:#2a2f3a;--acc:#6aa9ff}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);
font:13px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;overflow:hidden}
#scene{position:fixed;inset:0}
.panel{position:fixed;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
#head{top:12px;left:12px;max-width:min(560px,calc(100vw - 24px))}
#head h1{font-size:15px;margin:0 0 4px}#head .sub{color:var(--dim)}
#tools{margin-top:8px;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
button,input{font:inherit;color:var(--fg);background:#1b1f28;border:1px solid var(--line);border-radius:6px;padding:4px 8px}
button[aria-pressed=true]{border-color:var(--acc);color:var(--acc)}
input{min-width:180px}
#legend{bottom:12px;left:12px;max-height:40vh;overflow:auto;min-width:180px}
#legend .row{display:flex;align-items:center;gap:6px;white-space:nowrap}
#legend .sw{width:12px;height:12px;border-radius:2px;flex:none}
#legend .ramp{height:10px;width:160px;border-radius:2px;margin:4px 0}
#tip{pointer-events:none;display:none;max-width:420px;word-break:break-all}
#tip b{color:#fff}#tip .k{color:var(--dim)}
#nogl{display:none;top:40%;left:50%;transform:translate(-50%,-50%)}
</style></head><body>
<div id="scene"></div>
<div id="head" class="panel"><h1>__TITLE__</h1><div class="sub" id="sub"></div>
<div id="tools"><button id="bLang" aria-pressed="true">Language</button>
<button id="bChurn" aria-pressed="false">Churn</button><button id="bReset">Reset view</button>
<input id="q" type="search" placeholder="Highlight paths matching…" aria-label="Highlight paths"></div></div>
<div id="legend" class="panel" aria-live="polite"></div>
<div id="tip" class="panel"></div>
<div id="nogl" class="panel">This page needs WebGL, which this browser has turned off.</div>
<script type="application/json" id="gtv-data">__DATA__</script>
<script>__THREE__</script>
<script>__APP__</script>
</body></html>
"""

APP = r"""
(function(){
"use strict";
var D=JSON.parse(document.getElementById("gtv-data").textContent);
var M=D.meta,DIRS=D.dirs,FILES=D.files;
document.getElementById("sub").textContent=M.summary;
var host=document.getElementById("scene");
var renderer;
try{renderer=new THREE.WebGLRenderer({antialias:true});}catch(e){document.getElementById("nogl").style.display="block";return;}
renderer.setPixelRatio(Math.min(window.devicePixelRatio||1,2));
renderer.setSize(window.innerWidth,window.innerHeight);
renderer.outputColorSpace=THREE.SRGBColorSpace;
host.appendChild(renderer.domElement);
var scene=new THREE.Scene();scene.background=new THREE.Color(0x0f1115);
var S=M.side,camera=new THREE.PerspectiveCamera(45,window.innerWidth/window.innerHeight,S/500,S*20);
scene.add(new THREE.HemisphereLight(0xdfe8ff,0x20242c,1.1));
var sun=new THREE.DirectionalLight(0xffffff,1.6);sun.position.set(-0.6*S,1.4*S,0.9*S);scene.add(sun);
var controls=new THREE.OrbitControls(camera,renderer.domElement);
controls.enableDamping=true;controls.dampingFactor=0.08;controls.maxPolarAngle=Math.PI*0.495;
var box=new THREE.BoxGeometry(1,1,1);box.translate(0.5,0.5,0.5);
var o=new THREE.Object3D(),c=new THREE.Color();
var LIFT=Math.max(S/900,0.05),SLAB=LIFT*0.9;
// districts: stacked flat plots, one per directory
var dm=new THREE.InstancedMesh(box,new THREE.MeshLambertMaterial(),DIRS.length);
DIRS.forEach(function(d,i){o.position.set(d.x,d.k*LIFT,d.y);o.scale.set(Math.max(d.w,1e-4),SLAB,Math.max(d.h,1e-4));
o.updateMatrix();dm.setMatrixAt(i,o.matrix);var g=0.16+Math.min(d.k,8)*0.035;dm.setColorAt(i,c.setRGB(g,g+0.01,g+0.03));});
scene.add(dm);
// buildings
var maxL=1;FILES.forEach(function(f){if(f.l&&f.l>maxL)maxL=f.l;});
var H=S*0.32,base=function(f){return DIRS[f.d].k*LIFT+SLAB;};
var bm=new THREE.InstancedMesh(box,new THREE.MeshLambertMaterial(),FILES.length);
FILES.forEach(function(f,i){var h=f.bin?SLAB*0.6:Math.max(H*Math.sqrt((f.l||0)/maxL),SLAB);
o.position.set(f.x,base(f),f.y);o.scale.set(Math.max(f.w,1e-4),h,Math.max(f.h,1e-4));o.updateMatrix();bm.setMatrixAt(i,o.matrix);});
scene.add(bm);
// colours
var PAL=["#4e79a7","#f28e2b","#e15759","#76b7b2","#59a14f","#edc948","#b07aa1","#ff9da7","#9c755f","#bab0ac","#86bcb6","#d4a6c8","#8cd17d","#f1ce63","#a0cbe8","#ffbe7d"];
var langs={};FILES.forEach(function(f){if(!f.bin)langs[f.g]=(langs[f.g]||0)+(f.n||1);});
var order=Object.keys(langs).sort(function(a,b){return langs[b]-langs[a]||(a<b?-1:1);});
var LC={};order.forEach(function(g,i){LC[g]=g==="other"?"#6b7280":PAL[i%PAL.length];});
var maxC=0;FILES.forEach(function(f){if(f.c>maxC)maxC=f.c;});
function heat(v){if(!v)return "#3a4150";var t=maxC?Math.log(1+v)/Math.log(1+maxC):0;return c.setHSL(0.62-0.62*t,0.75,0.3+0.25*t).getStyle();}
var mode="lang",query="";
function paint(){FILES.forEach(function(f,i){var col=f.bin?"#555a66":(mode==="lang"?LC[f.g]:heat(f.c));
c.set(col);if(query&&f.p.toLowerCase().indexOf(query)<0)c.lerp(new THREE.Color(0x1a1d24),0.85);
bm.setColorAt(i,c);});bm.instanceColor.needsUpdate=true;legend();}
function legend(){var L=document.getElementById("legend"),h="";
if(mode==="lang"){h="<b>Language</b> <span class=k>(files)</span>";order.forEach(function(g){h+='<div class=row><span class=sw style="background:'+LC[g]+'"></span>'+esc(g)+' <span class=k>'+langs[g]+'</span></div>';});}
else if(!M.churn){h="<b>Churn</b><div class=k>"+esc(M.churn_note)+"</div>";}
else{h="<b>Changes, last "+M.since+" days</b><div class=ramp style=\"background:linear-gradient(90deg,"+heat(1)+","+heat(maxC/8)+","+heat(maxC/2)+","+heat(maxC)+")\"></div><div class=k>1 &mdash; "+maxC+" commits</div><div class=row><span class=sw style=\"background:#3a4150\"></span>unchanged</div>";}
h+='<div class=row style="margin-top:6px"><span class=sw style="background:#555a66"></span>binary</div>';
L.innerHTML=h;}
function esc(s){return String(s).replace(/[&<>"]/g,function(ch){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[ch];});}
paint();
// camera
function frame(d){var cx=d.x+d.w/2,cz=d.y+d.h/2,r=Math.max(d.w,d.h);
controls.target.set(cx,d.k*LIFT,cz);camera.position.set(cx-r*0.55,r*0.75+H*0.4,cz+r*1.05);controls.update();}
frame(DIRS[0]);
function setMode(m){mode=m;document.getElementById("bLang").setAttribute("aria-pressed",m==="lang");
document.getElementById("bChurn").setAttribute("aria-pressed",m==="churn");paint();}
document.getElementById("bLang").onclick=function(){setMode("lang");};
document.getElementById("bChurn").onclick=function(){setMode("churn");};
document.getElementById("bReset").onclick=function(){frame(DIRS[0]);};
document.getElementById("q").oninput=function(e){query=e.target.value.trim().toLowerCase();paint();};
// hover + click
var ray=new THREE.Raycaster(),ptr=new THREE.Vector2(),tip=document.getElementById("tip"),down=null;
function pick(ev){ptr.x=ev.clientX/window.innerWidth*2-1;ptr.y=-(ev.clientY/window.innerHeight)*2+1;
ray.setFromCamera(ptr,camera);var hb=ray.intersectObject(bm),hd=ray.intersectObject(dm);
if(hb.length&&(!hd.length||hb[0].distance<=hd[0].distance+1e-6))return{f:FILES[hb[0].instanceId]};
if(hd.length){var best=hd[0];hd.forEach(function(h){if(DIRS[h.instanceId].k>DIRS[best.instanceId].k&&Math.abs(h.distance-best.distance)<LIFT*4)best=h;});return{d:DIRS[best.instanceId]};}
return null;}
function fmt(n){return n==null?"—":n.toLocaleString();}
renderer.domElement.addEventListener("pointermove",function(ev){var p=pick(ev);if(!p){tip.style.display="none";return;}
var h;if(p.f){var f=p.f;h="<b>"+esc(f.p)+"</b><br><span class=k>"+(f.n?"collapsed: "+fmt(f.n)+" files":(f.bin?"binary":esc(f.g)))+"</span><br>"+
fmt(f.l)+" lines &middot; "+fmt(f.b)+" bytes"+(M.churn?" &middot; "+fmt(f.c)+" changes":"");}
else{var d=p.d;h="<b>"+esc(d.p||M.root)+"/</b><br>"+fmt(d.fc)+" files &middot; "+fmt(d.lc)+" lines<br><span class=k>click to focus</span>";}
tip.innerHTML=h;tip.style.display="block";var x=ev.clientX+14,y=ev.clientY+14,r=tip.getBoundingClientRect();
if(x+r.width>window.innerWidth)x=ev.clientX-r.width-14;if(y+r.height>window.innerHeight)y=ev.clientY-r.height-14;
tip.style.left=x+"px";tip.style.top=y+"px";});
renderer.domElement.addEventListener("pointerdown",function(ev){down=[ev.clientX,ev.clientY];});
renderer.domElement.addEventListener("pointerup",function(ev){if(!down||Math.abs(ev.clientX-down[0])+Math.abs(ev.clientY-down[1])>4)return;
var p=pick(ev);if(p&&p.d)frame(p.d);else if(p&&p.f)frame(DIRS[p.f.d]);});
window.addEventListener("resize",function(){camera.aspect=window.innerWidth/window.innerHeight;camera.updateProjectionMatrix();renderer.setSize(window.innerWidth,window.innerHeight);});
(function loop(){requestAnimationFrame(loop);controls.update();renderer.render(scene,camera);})();
})();
"""


def build_page(data, title, three_js):
    blob = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    blob = blob.replace("</", "<\\/").replace("<!--", "<\\!--")
    three_js = three_js.replace("</script", "<\\/script")
    return (PAGE.replace("__TITLE__", title.replace("&", "&amp;").replace("<", "&lt;"))
                .replace("__APP__", APP).replace("__DATA__", blob)
                .replace("__THREE__", three_js))


# -- cli ---------------------------------------------------------------------------------
def cmd_render(a):
    # realpath: git reports /private/var/... for a /var/... path on macOS, and the churn
    # prefix below is computed between the two.
    target = os.path.realpath(os.path.expanduser(a.path))
    if not os.path.isdir(target):
        raise Refused(1, "%s is not a directory" % a.path)
    if a.since < 1:
        raise Refused(1, "--since must be at least 1 day")
    if a.max_files < 1:
        raise Refused(1, "--max-files must be at least 1")
    if not os.path.isfile(VENDOR):
        raise Refused(1, "three.js is missing from this module (%s); reinstall gt-visualize"
                      % VENDOR)
    vault = find_vault(a.vault)
    now = datetime.now().astimezone()
    groot = git_root(target)
    in_git = groot is not None and os.path.realpath(groot) == os.path.realpath(target)
    root = target
    name = os.path.basename(root.rstrip(os.sep)) or "repo"
    if a.out is not None:
        resolve_out(a.out, vault, name, now)          # refuse a bad --out before any work
    if groot is not None and not in_git:
        # A subdirectory of a repo: list through git from the subdirectory, keep paths
        # relative to it.
        in_git = True
    rels = [r for r in list_files(root, in_git) if not excluded(r, a.exclude)]
    if not rels:
        raise Refused(3, "no files to draw under %s; nothing was written" % root)
    use_churn = in_git and not a.no_churn
    counts = {}
    if use_churn:
        prefix = os.path.relpath(root, groot).replace(os.sep, "/")
        prefix = "" if prefix == "." else prefix + "/"
        for p, n in churn(groot, a.since).items():
            if p.startswith(prefix):
                counts[p[len(prefix):]] = n
    lang_of, lang_src = load_gt_language(vault)
    records = []
    for rel in rels:
        try:
            size, lines, binary = measure(os.path.join(root, rel))
        except OSError:
            continue
        lang = "binary" if binary else (lang_of(rel) or fallback_lang(rel))
        records.append({"path": rel, "lang": lang, "bytes": size, "lines": lines,
                        "binary": binary, "churn": counts.get(rel, 0)})
    shown, cut = collapse(records, a.max_files)
    tree = build_tree(shown)
    dirs, files = layout(tree)
    namer = Namer(a.redact, a.salt)
    n_lines = sum(r["lines"] or 0 for r in records)
    shown_name = namer.seg("d", name) if a.redact else name
    summary = "%s files · %s directories · %s lines" % (
        format(len(records), ","), format(len(dirs), ","), format(n_lines, ","))
    if cut is not None:
        summary += " · directories below depth %d collapsed (%s buildings)" % (
            cut, format(len(files), ","))
    churn_note = ("" if use_churn else
                  "off: not a git repository" if not in_git else "off (--no-churn)")
    meta = {"root": shown_name, "summary": summary, "side": round(dirs[0]["w"], 3),
            "churn": use_churn, "since": a.since, "churn_note": churn_note,
            "files": len(records), "dirs": len(dirs), "lines": n_lines,
            "redacted": bool(a.redact), "generated": now.isoformat(timespec="seconds")}
    with open(VENDOR, "rb") as fh:
        raw = fh.read()
    if hashlib.sha256(raw).hexdigest() != THREE_SHA256:
        raise Refused(1, "three.js in this module does not match the vendored hash (%s); "
                         "refusing to inline it -- reinstall gt-visualize" % VENDOR)
    three_js = raw.decode("utf-8")
    data = payload(dirs, files, meta, namer)
    title = "Code city: %s" % shown_name
    if a.redact:
        # Names reach the page only through the path and name strings and the title. The
        # template and three.js are fixed code, numbers are coordinates ("0.16" is in
        # plenty of them), and a language label ("python") is not a name -- so the check
        # reads exactly the strings a name could ride in on, with kept extensions removed.
        carried = [title, meta["root"]] + [d["p"] for d in data["dirs"]] + \
            [d["n"] for d in data["dirs"]] + [f["p"] for f in data["files"]]
        carried = "\n".join(re.sub(r"\.[A-Za-z0-9]{1,6}(?=/|$)", "", s or "")
                            for s in carried)
        leaks = sorted(s for s in sensitive(records, name) if s in carried)
        if leaks:
            raise Refused(1, "--redact: %d original name(s) would survive in the page; "
                             "nothing was written" % len(leaks))
    page = build_page(data, title, three_js)
    out = resolve_out(a.out, vault, shown_name, now)
    tmp = out + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(page)
    os.replace(tmp, out)
    notes = []
    if not use_churn:
        notes.append("churn " + churn_note)
    if cut is not None:
        notes.append("directories below depth %d collapsed to fit --max-files %d"
                     % (cut, a.max_files))
    notes.append("redacted" if a.redact else "real names -- render with --redact before sharing")
    print("wrote %s (%d files, %d directories, %d lines; language from %s; %s)"
          % (out, len(records), len(dirs), n_lines,
             "gt filetype definitions" if lang_src.endswith(".py") else lang_src,
             "; ".join(notes)))
    return 0


# -- explain: a scroll-driven 3D walkthrough of how a system's parts work together --------
#
# The code city answers "where is the code"; an explainer answers "how does it work". Its
# input is a STORY: the parts of a system, the links between them, and an ordered list of
# scenes -- what each scene shows, which parts it puts in focus, and which links carry a
# flow. The skill has Claude write the story from the codebase and the vault; this renders
# it. Nothing about any one codebase is built in here.
EXPLAIN_KINDS = ("box", "store", "actor", "stack", "gate", "file")
FLOW_KINDS = ("data", "rule", "block", "return")
SIZES = ("s", "m", "l")
MAX_PARTS, MAX_LINKS, MAX_SCENES = 80, 160, 24
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
_TOP_KEYS = {"title", "subtitle", "intro", "parts", "links", "scenes", "source"}
_PART_KEYS = {"id", "label", "kind", "group", "size", "at", "note"}
_LINK_KEYS = {"id", "from", "to", "label"}
_SCENE_KEYS = {"title", "body", "show", "focus", "flows", "ghost"}


def _text(v, limit):
    return isinstance(v, str) and v.strip() != "" and len(v) <= limit


def validate_story(s):
    """-> list of problems, empty when the story can be rendered."""
    bad = []
    if not isinstance(s, dict):
        return ["the story is not a JSON object"]
    for k in sorted(set(s) - _TOP_KEYS):
        bad.append("unknown top-level key %r" % k)
    if not _text(s.get("title"), 120):
        bad.append("title: required, 1-120 characters")
    for k, lim in (("subtitle", 240), ("source", 240)):
        if k in s and not _text(s[k], lim):
            bad.append("%s: text of 1-%d characters" % (k, lim))
    intro = s.get("intro", [])
    if isinstance(intro, str):
        intro = [intro]
    if not isinstance(intro, list) or not all(_text(p, 1500) for p in intro):
        bad.append("intro: a paragraph or a list of paragraphs, each 1-1500 characters")
    parts = s.get("parts")
    if not isinstance(parts, list) or not 1 <= len(parts) <= MAX_PARTS:
        return bad + ["parts: a list of 1-%d parts" % MAX_PARTS]
    ids = set()
    for i, p in enumerate(parts):
        where = "parts[%d]" % i
        if not isinstance(p, dict):
            bad.append("%s is not an object" % where)
            continue
        for k in sorted(set(p) - _PART_KEYS):
            bad.append("%s: unknown key %r" % (where, k))
        pid = p.get("id")
        if not isinstance(pid, str) or not _ID.match(pid):
            bad.append("%s.id: letters, digits, _ and -, up to 40" % where)
        elif pid in ids:
            bad.append("%s.id %r is used twice" % (where, pid))
        else:
            ids.add(pid)
        if not _text(p.get("label"), 60):
            bad.append("%s.label: required, 1-60 characters" % where)
        if p.get("kind", "box") not in EXPLAIN_KINDS:
            bad.append("%s.kind: one of %s" % (where, ", ".join(EXPLAIN_KINDS)))
        if p.get("size", "m") not in SIZES:
            bad.append("%s.size: one of s, m, l" % where)
        if "group" in p and not _text(p["group"], 40):
            bad.append("%s.group: text of 1-40 characters" % where)
        if "note" in p and not _text(p["note"], 300):
            bad.append("%s.note: text of 1-300 characters" % where)
        if "at" in p:
            at = p["at"]
            if not (isinstance(at, list) and len(at) == 2 and
                    all(isinstance(v, (int, float)) and not isinstance(v, bool)
                        and -60 <= v <= 60 for v in at)):
                bad.append("%s.at: [x, z], each between -60 and 60" % where)
    links = s.get("links", [])
    lids = set()
    if not isinstance(links, list) or len(links) > MAX_LINKS:
        bad.append("links: a list of at most %d links" % MAX_LINKS)
        links = []
    for i, l in enumerate(links):
        where = "links[%d]" % i
        if not isinstance(l, dict):
            bad.append("%s is not an object" % where)
            continue
        for k in sorted(set(l) - _LINK_KEYS):
            bad.append("%s: unknown key %r" % (where, k))
        for end in ("from", "to"):
            if l.get(end) not in ids:
                bad.append("%s.%s: %r is not a part id" % (where, end, l.get(end)))
        if l.get("from") is not None and l.get("from") == l.get("to"):
            bad.append("%s: a link must join two different parts" % where)
        lid = l.get("id", "%s>%s" % (l.get("from"), l.get("to")))
        if not isinstance(lid, str) or not lid.strip():
            bad.append("%s.id: text" % where)
        elif lid in lids:
            bad.append("%s.id %r is used twice (give one an explicit id)" % (where, lid))
        else:
            lids.add(lid)
        if "label" in l and not _text(l["label"], 60):
            bad.append("%s.label: text of 1-60 characters" % where)
    scenes = s.get("scenes")
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= MAX_SCENES:
        return bad + ["scenes: a list of 1-%d scenes" % MAX_SCENES]
    for i, sc in enumerate(scenes):
        where = "scenes[%d]" % i
        if not isinstance(sc, dict):
            bad.append("%s is not an object" % where)
            continue
        for k in sorted(set(sc) - _SCENE_KEYS):
            bad.append("%s: unknown key %r" % (where, k))
        if not _text(sc.get("title"), 100):
            bad.append("%s.title: required, 1-100 characters" % where)
        body = sc.get("body", [])
        if isinstance(body, str):
            body = [body]
        if not isinstance(body, list) or not body or not all(_text(p, 1500) for p in body):
            bad.append("%s.body: a paragraph or a list of paragraphs, each 1-1500 characters"
                       % where)
        show = sc.get("show", "*")
        if show != "*":
            if not isinstance(show, list) or not show:
                bad.append("%s.show: \"*\" or a non-empty list of part ids" % where)
            else:
                for x in show:
                    if x not in ids:
                        bad.append("%s.show: %r is not a part id" % (where, x))
        for x in sc.get("focus", []) if isinstance(sc.get("focus", []), list) else [None]:
            if x not in ids:
                bad.append("%s.focus: %r is not a part id" % (where, x))
        flows = sc.get("flows", [])
        if not isinstance(flows, list):
            bad.append("%s.flows: a list" % where)
            flows = []
        for j, f in enumerate(flows):
            if not isinstance(f, dict) or f.get("link") not in lids:
                bad.append("%s.flows[%d].link: not a link id (ids default to \"from>to\")"
                           % (where, j))
            elif f.get("kind", "data") not in FLOW_KINDS:
                bad.append("%s.flows[%d].kind: one of %s" % (where, j, ", ".join(FLOW_KINDS)))
        if "ghost" in sc and not isinstance(sc["ghost"], bool):
            bad.append("%s.ghost: true or false" % where)
    return bad


def explain_layout(parts):
    """-> {id: (x, z)}. Groups become columns, left to right in order of first appearance;
    a part's own `at` wins over the automatic place."""
    groups = []
    for p in parts:
        g = p.get("group", "")
        if g not in groups:
            groups.append(g)
    members = {g: [p for p in parts if p.get("group", "") == g] for g in groups}
    pos = {}
    n = len(groups)
    for gi, g in enumerate(groups):
        col = members[g]
        for ri, p in enumerate(col):
            pos[p["id"]] = ((gi - (n - 1) / 2.0) * 4.6, (ri - (len(col) - 1) / 2.0) * 3.0)
    for p in parts:
        if "at" in p:
            pos[p["id"]] = (float(p["at"][0]), float(p["at"][1]))
    return pos


def _inline(text):
    """Escape, then allow `code` and **bold** -- the only markup a story may use."""
    t = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    t = re.sub(r"`([^`\n]{1,200})`", r"<code>\1</code>", t)
    return re.sub(r"\*\*([^*\n]{1,200})\*\*", r"<strong>\1</strong>", t)


def _paras(v):
    return [v] if isinstance(v, str) else list(v or [])


def story_payload(s):
    pos = explain_layout(s["parts"])
    groups = []
    for p in s["parts"]:
        g = p.get("group", "")
        if g not in groups:
            groups.append(g)
    parts = [{"id": p["id"], "label": p["label"], "kind": p.get("kind", "box"),
              "group": p.get("group", ""), "size": p.get("size", "m"),
              "note": p.get("note", ""), "x": round(pos[p["id"]][0], 3),
              "z": round(pos[p["id"]][1], 3)} for p in s["parts"]]
    links = [{"id": l.get("id", "%s>%s" % (l["from"], l["to"])), "from": l["from"],
              "to": l["to"], "label": l.get("label", "")} for l in s.get("links", [])]
    scenes = []
    for sc in s["scenes"]:
        scenes.append({"show": sc.get("show", "*"), "focus": sc.get("focus", []),
                       "flows": [{"link": f["link"], "kind": f.get("kind", "data")}
                                 for f in sc.get("flows", [])],
                       "ghost": bool(sc.get("ghost", False))})
    return {"groups": groups, "parts": parts, "links": links, "scenes": scenes}


EXPLAIN_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f6f5f1;--panel:#ffffff;--fg:#1d2127;--dim:#5d6571;--line:#d9d6ce;--acc:#2f6fdf;
--grid:#d6d2c8;--label-bg:rgba(255,255,255,.92);--link:#9aa3b2;--flow-data:#2f6fdf;
--flow-rule:#c47a00;--flow-block:#d33f3f;--flow-return:#2e9d5b;--code-bg:#eceae4}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f1115;--panel:#161a21;
--fg:#e6e8ee;--dim:#98a0b3;--line:#2a2f3a;--acc:#6aa9ff;--grid:#232833;--label-bg:rgba(22,26,33,.92);
--link:#56607a;--flow-data:#6aa9ff;--flow-rule:#f2b04a;--flow-block:#ff6b6b;--flow-return:#5fd08e;--code-bg:#202532}}
:root[data-theme="dark"]{--bg:#0f1115;--panel:#161a21;--fg:#e6e8ee;--dim:#98a0b3;--line:#2a2f3a;
--acc:#6aa9ff;--grid:#232833;--label-bg:rgba(22,26,33,.92);--link:#56607a;--flow-data:#6aa9ff;
--flow-rule:#f2b04a;--flow-block:#ff6b6b;--flow-return:#5fd08e;--code-bg:#202532}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
code{font:13.5px/1.4 Menlo,Consolas,"Liberation Mono",monospace;background:var(--code-bg);
padding:1px 5px;border-radius:4px;word-break:break-word}
.wrap{display:grid;grid-template-columns:minmax(320px,440px) 1fr;min-height:100vh}
.story{padding:40px 36px 30vh;border-right:1px solid var(--line);background:var(--panel)}
.story header{min-height:62vh;display:flex;flex-direction:column;justify-content:center}
.eyebrow{text-transform:uppercase;letter-spacing:.08em;font-size:12px;color:var(--dim);margin:0 0 10px}
h1{font-size:30px;line-height:1.2;margin:0 0 12px}
.sub{font-size:18px;color:var(--dim);margin:0 0 18px}
.hint{font-size:13px;color:var(--dim)}
.step{min-height:78vh;padding:28px 0;opacity:.38;transition:opacity .35s}
.step.on{opacity:1}
.step .n{font-size:12px;color:var(--acc);font-weight:600;letter-spacing:.06em}
.step h2{font-size:22px;line-height:1.25;margin:6px 0 12px}
.step p{margin:0 0 12px}
.stagecol{position:relative}
#stage{position:sticky;top:0;height:100vh;width:100%;overflow:hidden}
#stage canvas{display:block;cursor:grab}
#rail{position:absolute;right:16px;top:50%;transform:translateY(-50%);display:flex;
flex-direction:column;gap:8px;z-index:2}
#rail button{width:12px;height:12px;border-radius:50%;border:1px solid var(--dim);background:transparent;
padding:0;cursor:pointer}
#rail button[aria-current="true"]{background:var(--acc);border-color:var(--acc)}
#legend{position:absolute;left:16px;bottom:16px;background:var(--label-bg);border:1px solid var(--line);
border-radius:8px;padding:8px 10px;font-size:12px;z-index:2;max-width:60%}
#legend .row{display:flex;align-items:center;gap:6px;white-space:nowrap}
#legend .sw{width:10px;height:10px;border-radius:2px;flex:none}
#legend .fl{display:inline-block;width:18px;height:3px;border-radius:2px;vertical-align:middle;margin-right:4px}
#tip{position:absolute;pointer-events:none;display:none;background:var(--label-bg);border:1px solid var(--line);
border-radius:8px;padding:8px 10px;font-size:13px;max-width:300px;z-index:3}
#nogl{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;padding:24px;
text-align:center;color:var(--dim)}
#nogl[hidden],#legend[hidden]{display:none}
#theme{position:absolute;right:16px;top:16px;z-index:2;font:inherit;font-size:12px;color:var(--fg);
background:var(--label-bg);border:1px solid var(--line);border-radius:6px;padding:4px 8px;cursor:pointer}
.src{font-size:12px;color:var(--dim);margin-top:30px}
@media (max-width:860px){.wrap{display:block}.stagecol{position:sticky;top:0;z-index:1;height:46vh}
#stage{height:46vh;position:relative}.story{border-right:0;padding:24px 16px 40vh}
.story header{min-height:auto;padding:12px 0 30px}.step{min-height:60vh}h1{font-size:25px}
#legend{max-width:calc(100% - 60px)}#rail{right:8px}}
@media (prefers-reduced-motion:reduce){.step{transition:none}}
</style></head><body>
<div class="wrap">
<main class="story">
<header><p class="eyebrow">How it works</p><h1>__TITLE__</h1>__SUBTITLE____INTRO__
<p class="hint">Scroll to step through it. Drag the picture to look around; hover a part for what it does.</p></header>
__STEPS__
__SOURCE__
</main>
<div class="stagecol"><div id="stage" aria-label="3D diagram of the parts named in the text">
<button id="theme" type="button">Theme</button><nav id="rail" aria-label="Scenes"></nav>
<div id="legend"></div><div id="tip"></div><div id="nogl" hidden>This picture needs WebGL, which this browser has turned off. The text tells the same story.</div>
</div></div>
</div>
<script type="application/json" id="gtx-data">__DATA__</script>
<script>__THREE__</script>
<script>__APP__</script>
</body></html>
"""

EXPLAIN_APP = r"""
(function(){
"use strict";
var D=JSON.parse(document.getElementById("gtx-data").textContent);
var steps=[].slice.call(document.querySelectorAll(".step"));
var reduced=!!(window.matchMedia&&matchMedia("(prefers-reduced-motion: reduce)").matches);
var stage=document.getElementById("stage"),tip=document.getElementById("tip");
var FONT='-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif';
var root=document.documentElement;
try{var th=localStorage.getItem("gtx-theme");if(th)root.setAttribute("data-theme",th);}catch(e){}
function tok(n){return getComputedStyle(root).getPropertyValue(n).trim();}
var rail=document.getElementById("rail");
steps.forEach(function(s,i){var b=document.createElement("button");b.type="button";
b.setAttribute("aria-label","Scene "+(i+1)+": "+s.querySelector("h2").textContent);
b.onclick=function(){s.scrollIntoView({behavior:reduced?"auto":"smooth",block:"center"});};rail.appendChild(b);});
var renderer;
try{renderer=new THREE.WebGLRenderer({antialias:true,alpha:true});}catch(e){renderer=null;}
if(!renderer){document.getElementById("nogl").hidden=false;
observe(function(i){steps.forEach(function(s,j){s.classList.toggle("on",j===i);});});return;}
renderer.setPixelRatio(Math.min(window.devicePixelRatio||1,2));
renderer.outputColorSpace=THREE.SRGBColorSpace;
stage.insertBefore(renderer.domElement,stage.firstChild);
var scene=new THREE.Scene();
var camera=new THREE.PerspectiveCamera(38,1,0.1,500);
scene.add(new THREE.HemisphereLight(0xffffff,0x40444f,1.05));
var sun=new THREE.DirectionalLight(0xffffff,1.25);sun.position.set(6,14,9);scene.add(sun);
var controls=new THREE.OrbitControls(camera,renderer.domElement);
controls.enableDamping=true;controls.enablePan=false;controls.maxPolarAngle=Math.PI*0.47;
controls.minDistance=3;controls.maxDistance=160;
var PAL=["#4e79a7","#f28e2b","#59a14f","#b07aa1","#e15759","#76b7b2","#c9a227","#9c755f"];
var GC={};D.groups.forEach(function(g,i){GC[g]=PAL[i%PAL.length];});
function rr(x,a,b,w,h,r){x.beginPath();x.moveTo(a+r,b);x.arcTo(a+w,b,a+w,b+h,r);x.arcTo(a+w,b+h,a,b+h,r);x.arcTo(a,b+h,a,b,r);x.arcTo(a,b,a+w,b,r);x.closePath();}
function labelTex(text,px){var dpr=2,cv=document.createElement("canvas"),x=cv.getContext("2d");
var font="600 "+px+"px "+FONT;x.font=font;var w=Math.ceil(x.measureText(text).width)+26,h=px+16;
cv.width=w*dpr;cv.height=h*dpr;x.scale(dpr,dpr);x.font=font;x.fillStyle=tok("--label-bg");rr(x,0.5,0.5,w-1,h-1,7);x.fill();
x.strokeStyle=tok("--line");x.lineWidth=1;x.stroke();x.fillStyle=tok("--fg");x.textBaseline="middle";x.fillText(text,13,h/2+1);
var t=new THREE.CanvasTexture(cv);t.minFilter=THREE.LinearFilter;t.colorSpace=THREE.SRGBColorSpace;return{t:t,w:w,h:h};}
var sprites=[];
function makeLabel(text,px,k){var m=new THREE.SpriteMaterial({transparent:true,depthTest:false,depthWrite:false,opacity:0});
var s=new THREE.Sprite(m);s.renderOrder=10;s.userData={text:text,px:px,k:k};paintLabel(s);sprites.push(s);return s;}
function paintLabel(s){var c=labelTex(s.userData.text,s.userData.px);if(s.material.map)s.material.map.dispose();
s.material.map=c.t;s.material.needsUpdate=true;s.scale.set(c.w*s.userData.k,c.h*s.userData.k,1);}
var grid=null;
function paintGrid(){if(grid){scene.remove(grid);grid.geometry.dispose();}
var gc=new THREE.Color(tok("--grid"));grid=new THREE.GridHelper(120,60,gc,gc);grid.position.y=-0.02;scene.add(grid);}
paintGrid();
// parts
var PARTS={},plist=[],meshes=[];
D.parts.forEach(function(d){var s={s:0.9,m:1.3,l:1.8}[d.size]||1.3;
var col=new THREE.Color(GC[d.group]||"#8892a6");var g=new THREE.Group(),mats=[],h;
function mat(){var m=new THREE.MeshStandardMaterial({color:col,roughness:0.55,metalness:0.05,emissive:col,emissiveIntensity:0.08,transparent:true,opacity:0});mats.push(m);return m;}
function add(geo,y){var m=new THREE.Mesh(geo,mat());m.position.y=y;m.userData.part=d.id;g.add(m);meshes.push(m);}
if(d.kind==="store"){h=s*0.85;add(new THREE.CylinderGeometry(s*0.55,s*0.55,h,36),h/2);}
else if(d.kind==="actor"){h=s*0.9;add(new THREE.SphereGeometry(s*0.45,28,20),s*0.45);}
else if(d.kind==="stack"){for(var i=0;i<3;i++)add(new THREE.BoxGeometry(s*(1-i*0.18),s*0.22,s*(1-i*0.18)),s*0.11+i*s*0.29);h=s*0.8;}
else if(d.kind==="gate"){h=s*1.15;add(new THREE.BoxGeometry(s*0.2,h,s*1.1),h/2);}
else if(d.kind==="file"){h=s*1.05;add(new THREE.BoxGeometry(s*0.78,h,s*0.14),h/2);}
else{h=s*0.62;add(new THREE.BoxGeometry(s,h,s),h/2);}
var lab=makeLabel(d.label,26,0.0105);lab.position.y=h+0.5;g.add(lab);
g.position.set(d.x,0,d.z);scene.add(g);
var e={def:d,g:g,mats:mats,lab:lab,h:h,op:0,to:0,glow:0.08,glowTo:0.08};PARTS[d.id]=e;plist.push(e);});
// links
var LINKS={},llist=[];
D.links.forEach(function(l){var a=PARTS[l.from],b=PARTS[l.to];
var p0=new THREE.Vector3(a.g.position.x,a.h+0.08,a.g.position.z),p1=new THREE.Vector3(b.g.position.x,b.h+0.08,b.g.position.z);
var mid=p0.clone().lerp(p1,0.5);mid.y+=Math.max(1.1,p0.distanceTo(p1)*0.3);
var curve=new THREE.QuadraticBezierCurve3(p0,mid,p1);
var m=new THREE.MeshBasicMaterial({color:new THREE.Color(tok("--link")),transparent:true,opacity:0,depthWrite:false});
var tube=new THREE.Mesh(new THREE.TubeGeometry(curve,56,0.035,6,false),m);scene.add(tube);
var lab=null;if(l.label){lab=makeLabel(l.label,22,0.0085);lab.position.copy(curve.getPoint(0.5));lab.position.y+=0.32;scene.add(lab);}
var L={def:l,curve:curve,mat:m,tube:tube,lab:lab,op:0,to:0,lop:0,lto:0};LINKS[l.id]=L;llist.push(L);});
// flows
var FLOWC={data:"--flow-data",rule:"--flow-rule",block:"--flow-block","return":"--flow-return"};
var pgeo=new THREE.SphereGeometry(0.12,14,10),particles=[];
function setFlows(list){particles.forEach(function(p){scene.remove(p.m);p.m.material.dispose();});particles=[];
list.forEach(function(f){var L=LINKS[f.link];if(!L)return;var n=reduced?1:4;
for(var i=0;i<n;i++){var m=new THREE.Mesh(pgeo,new THREE.MeshBasicMaterial({color:new THREE.Color(tok(FLOWC[f.kind]||"--flow-data")),transparent:true,opacity:0}));
m.renderOrder=5;scene.add(m);particles.push({m:m,L:L,kind:f.kind,t:reduced?0.5:i/n});}});}
// legend
function legend(){var h="";D.groups.forEach(function(g){if(g)h+='<div class="row"><span class="sw" style="background:'+GC[g]+'"></span>'+esc(g)+"</div>";});
var used={};D.scenes.forEach(function(s){s.flows.forEach(function(f){used[f.kind]=1;});});
var names={data:"data / calls","return":"answer","rule":"rule injected",block:"stopped at a gate"};
Object.keys(names).forEach(function(k){if(used[k])h+='<div class="row"><span class="fl" style="background:'+tok(FLOWC[k])+'"></span>'+names[k]+"</div>";});
document.getElementById("legend").innerHTML=h;document.getElementById("legend").hidden=!h;}
function esc(s){return String(s).replace(/[&<>"]/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];});}
legend();
// scenes
var cur=-1,camFrom,camTo,tgtFrom,tgtTo,camT=1;
function frame(ids){var box=new THREE.Box3(),v=new THREE.Vector3();
ids.forEach(function(id){var e=PARTS[id];if(!e)return;box.expandByPoint(v.set(e.g.position.x-1.1,0,e.g.position.z-1.1));box.expandByPoint(v.set(e.g.position.x+1.1,e.h+1,e.g.position.z+1.1));});
if(box.isEmpty())return;var c=box.getCenter(new THREE.Vector3()),sz=box.getSize(new THREE.Vector3());
var r=Math.max(sz.x,sz.z*0.9,3.2)*0.5,fov=camera.fov*Math.PI/180;
var dist=r/Math.tan(fov/2)/Math.min(1,Math.max(camera.aspect,0.55))+2.5;
tgtFrom=controls.target.clone();tgtTo=new THREE.Vector3(c.x,0.5,c.z);camFrom=camera.position.clone();
camTo=tgtTo.clone().add(new THREE.Vector3(0.28,0.64,1).normalize().multiplyScalar(dist));
if(reduced||cur<0&&camT===1&&camFrom.lengthSq()===0){controls.target.copy(tgtTo);camera.position.copy(camTo);camT=1;}else camT=0;}
function apply(i){if(i===cur||i<0||i>=D.scenes.length)return;var first=cur<0;cur=i;var S=D.scenes[i];
var show=S.show==="*"?null:S.show,focus=S.focus;
plist.forEach(function(e){var id=e.def.id,vis=!show||show.indexOf(id)>=0,f=focus.indexOf(id)>=0;
e.to=vis?(focus.length&&!f?0.5:1):(S.ghost?0.1:0);e.glowTo=f?0.6:0.08;});
var flowing={};S.flows.forEach(function(f){flowing[f.link]=1;});
llist.forEach(function(L){var a=PARTS[L.def.from],b=PARTS[L.def.to],on=a.to>0.3&&b.to>0.3;
L.to=on?(flowing[L.def.id]?0.95:0.3):(S.ghost&&(a.to>0||b.to>0)?0.06:0);L.lto=flowing[L.def.id]?1:0;});
setFlows(S.flows);frame(focus.length?focus:(show||Object.keys(PARTS)));
steps.forEach(function(s,j){s.classList.toggle("on",j===i);});
[].forEach.call(rail.children,function(b,j){b.setAttribute("aria-current",j===i?"true":"false");});
if(first&&!reduced)camT=1,controls.target.copy(tgtTo),camera.position.copy(camTo);}
function observe(cb){if(!("IntersectionObserver" in window)){steps.forEach(function(s){s.classList.add("on");});cb(0);return;}
var io=new IntersectionObserver(function(es){es.forEach(function(en){if(en.isIntersecting)cb(+en.target.getAttribute("data-i"));});},{rootMargin:"-45% 0px -45% 0px"});
steps.forEach(function(s){io.observe(s);});}
observe(apply);
// size
function resize(){var r=stage.getBoundingClientRect();var w=Math.max(r.width,1),h=Math.max(r.height,1);
renderer.setSize(w,h);camera.aspect=w/h;camera.updateProjectionMatrix();}
if(window.ResizeObserver)new ResizeObserver(resize).observe(stage);window.addEventListener("resize",resize);resize();
apply(0);
// theme
function retheme(){paintGrid();sprites.forEach(paintLabel);llist.forEach(function(L){L.mat.color.set(tok("--link"));});
particles.forEach(function(p){p.m.material.color.set(tok(FLOWC[p.kind]||"--flow-data"));});legend();}
document.getElementById("theme").onclick=function(){var dark=tok("--bg").toLowerCase()==="#0f1115";var next=dark?"light":"dark";
root.setAttribute("data-theme",next);try{localStorage.setItem("gtx-theme",next);}catch(e){}retheme();};
if(window.matchMedia){var mq=matchMedia("(prefers-color-scheme: dark)");if(mq.addEventListener)mq.addEventListener("change",retheme);}
// hover
var ray=new THREE.Raycaster(),ptr=new THREE.Vector2();
renderer.domElement.addEventListener("pointermove",function(ev){var r=renderer.domElement.getBoundingClientRect();
ptr.x=(ev.clientX-r.left)/r.width*2-1;ptr.y=-((ev.clientY-r.top)/r.height)*2+1;ray.setFromCamera(ptr,camera);
var hits=ray.intersectObjects(meshes.filter(function(m){return PARTS[m.userData.part].op>0.3;}),false);
if(!hits.length){tip.style.display="none";return;}var d=PARTS[hits[0].object.userData.part].def;
tip.innerHTML="<b>"+esc(d.label)+"</b>"+(d.note?"<br>"+esc(d.note):"");tip.style.display="block";
var x=ev.clientX-r.left+14,y=ev.clientY-r.top+14;if(x+300>r.width)x=Math.max(8,x-320);tip.style.left=x+"px";tip.style.top=y+"px";});
renderer.domElement.addEventListener("pointerleave",function(){tip.style.display="none";});
// loop
var last=performance.now();
function tick(now){var dt=Math.min((now-last)/1000,0.1);last=now;var k=reduced?1:1-Math.pow(0.002,dt);
plist.forEach(function(e){e.op+=(e.to-e.op)*k;e.glow+=(e.glowTo-e.glow)*k;e.g.visible=e.op>0.01;
e.mats.forEach(function(m){m.opacity=e.op;m.emissiveIntensity=e.glow;m.depthWrite=e.op>0.6;});e.lab.material.opacity=Math.min(1,e.op*1.3);});
llist.forEach(function(L){L.op+=(L.to-L.op)*k;L.lop+=(L.lto-L.lop)*k;L.tube.visible=L.op>0.01;L.mat.opacity=L.op;
if(L.lab){L.lab.visible=L.lop>0.01;L.lab.material.opacity=L.lop;}});
if(camT<1){camT=Math.min(1,camT+dt/1.2);var s=camT*camT*(3-2*camT);controls.target.lerpVectors(tgtFrom,tgtTo,s);camera.position.lerpVectors(camFrom,camTo,s);}
particles.forEach(function(p){if(!reduced){p.t+=dt*0.32;if(p.t>1)p.t-=1;}
var t=p.t,op=Math.min(1,p.L.op*1.1);
if(p.kind==="block"){t=p.t*0.55;var near=t>0.47;p.m.scale.setScalar(near?1.35:1);p.m.material.color.set(tok(near?"--flow-block":"--flow-data"));}
p.m.position.copy(p.L.curve.getPoint(p.kind==="return"?1-t:t));p.m.material.opacity=op;});
controls.update();renderer.render(scene,camera);requestAnimationFrame(tick);}
requestAnimationFrame(tick);
window.gtx={camera:camera,controls:controls,scene:function(){return cur;},go:apply};
})();
"""


def build_explain_page(story, three_js):
    title = _inline(story["title"])
    sub = '<p class="sub">%s</p>' % _inline(story["subtitle"]) if story.get("subtitle") else ""
    intro = "".join("<p>%s</p>" % _inline(p) for p in _paras(story.get("intro")))
    n = len(story["scenes"])
    steps = []
    for i, sc in enumerate(story["scenes"]):
        steps.append('<section class="step" data-i="%d"><div class="n">%d / %d</div><h2>%s</h2>%s'
                     '</section>' % (i, i + 1, n, _inline(sc["title"]),
                                     "".join("<p>%s</p>" % _inline(p)
                                             for p in _paras(sc["body"]))))
    src = '<p class="src">%s</p>' % _inline(story["source"]) if story.get("source") else ""
    blob = json.dumps(story_payload(story), separators=(",", ":"), ensure_ascii=False)
    blob = blob.replace("</", "<\\/").replace("<!--", "<\\!--")
    plain = re.sub(r"<[^>]+>", "", title)
    return (EXPLAIN_PAGE.replace("<title>__TITLE__</title>", "<title>%s</title>" % plain)
            .replace("__TITLE__", title).replace("__SUBTITLE__", sub).replace("__INTRO__", intro)
            .replace("__STEPS__", "\n".join(steps)).replace("__SOURCE__", src)
            .replace("__APP__", EXPLAIN_APP).replace("__DATA__", blob)
            .replace("__THREE__", three_js.replace("</script", "<\\/script")))


def read_three():
    if not os.path.isfile(VENDOR):
        raise Refused(1, "three.js is missing from this module (%s); reinstall gt-visualize"
                      % VENDOR)
    with open(VENDOR, "rb") as fh:
        raw = fh.read()
    if hashlib.sha256(raw).hexdigest() != THREE_SHA256:
        raise Refused(1, "three.js in this module does not match the vendored hash (%s); "
                         "refusing to inline it -- reinstall gt-visualize" % VENDOR)
    return raw.decode("utf-8")


def cmd_explain(a):
    try:
        with open(os.path.expanduser(a.story), encoding="utf-8") as fh:
            story = json.load(fh)
    except OSError as e:
        raise Refused(1, "cannot read %s: %s" % (a.story, e.strerror or e))
    except ValueError as e:
        raise Refused(1, "%s is not valid JSON: %s" % (a.story, e))
    problems = validate_story(story)
    if problems:
        shown = problems[:25]
        more = "" if len(problems) <= 25 else "\n  ... and %d more" % (len(problems) - 25)
        raise Refused(1, "%s has %d problem(s); nothing was written:\n  %s%s"
                      % (a.story, len(problems), "\n  ".join(shown), more))
    if a.check:
        print("ok %s (%d parts, %d links, %d scenes)" % (
            a.story, len(story["parts"]), len(story.get("links", [])), len(story["scenes"])))
        return 0
    vault = find_vault(a.vault)
    now = datetime.now().astimezone()
    slug = re.sub(r"[^a-z0-9]+", "-", story["title"].lower()).strip("-")[:40] or "story"
    out = resolve_out(a.out, vault, "explain-" + slug, now)
    page = build_explain_page(story, read_three())
    tmp = out + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(page)
    os.replace(tmp, out)
    print("wrote %s (%d parts, %d links, %d scenes)" % (
        out, len(story["parts"]), len(story.get("links", [])), len(story["scenes"])))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_visualize.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    x = sub.add_parser("explain", help="render a story (parts, links, scenes) as a "
                                       "scroll-driven 3D walkthrough, one HTML file")
    x.add_argument("story", help="the story JSON file")
    x.add_argument("--out", help="file or existing directory, outside the vault")
    x.add_argument("--check", action="store_true", help="validate the story; write nothing")
    x.add_argument("--vault", help="the vault an --out must stay out of "
                                   "(default: $GT_VAULT, then ~/.claude/vault-config.json)")
    r = sub.add_parser("render", help="render a repository as a 3D code city, one HTML file")
    r.add_argument("path", nargs="?", default=".", help="the tree to draw (default: .)")
    r.add_argument("--out", help="file or existing directory, outside the vault")
    r.add_argument("--since", type=int, default=90, help="churn window in days (default 90)")
    r.add_argument("--no-churn", action="store_true", help="do not read git history")
    r.add_argument("--redact", action="store_true", help="hash every path segment")
    r.add_argument("--salt", help=argparse.SUPPRESS)
    r.add_argument("--exclude", action="append", default=[],
                   help="glob of paths to leave out; repeatable")
    r.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES,
                   help="collapse deep directories above this many buildings (default %d)"
                        % DEFAULT_MAX_FILES)
    r.add_argument("--vault", help="the vault an --out must stay out of "
                                   "(default: $GT_VAULT, then ~/.claude/vault-config.json)")
    a = ap.parse_args(argv)
    if a.cmd not in ("render", "explain"):
        ap.print_help(sys.stderr)
        return 1
    try:
        return cmd_render(a) if a.cmd == "render" else cmd_explain(a)
    except Refused as e:
        print("gt_visualize: %s" % e, file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
