#!/usr/bin/env python3
"""Golden Thread visualize: draw a repository as a 3D code city, in one offline HTML file.

    gt_visualize.py render [PATH] [--out FILE|DIR] [--since DAYS] [--no-churn] [--redact]
                           [--exclude GLOB ...] [--max-files N] [--vault V]

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
THREE_SHA256 = "58bd7e943f5a98307d30ccd99e8d5152df866e9bb7ddd4a5a388bea376b29672"
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


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_visualize.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
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
    if a.cmd != "render":
        ap.print_help(sys.stderr)
        return 1
    try:
        return cmd_render(a)
    except Refused as e:
        print("gt_visualize: %s" % e, file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
