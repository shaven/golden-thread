#!/usr/bin/env python3
"""gt_demo_track.py -- the demo's role tracks and their offline walk-throughs (0.22.0, phases 95c and 95d).

    gt_demo_track.py list
    gt_demo_track.py show <role>
    gt_demo_track.py run <role> --vault V [--today YYYY-MM-DD]       step through it, asserting each step
    gt_demo_track.py walkthrough <role> [--write | --out FILE]       the offline HTML for the role
    gt_demo_track.py path <role>                                     where the committed HTML lives

A track is data, templates/demo-tracks/<role>.json: steps, each with four fields -- goal (what you are
doing), say (what to type), see (what you will see) and why (why it matters). `/gt-demo:gt-demo tour <role>`
plays one in the demo vault; `/gt-demo:gt-demo walkthrough <role>` opens the page built from it.

`run` is the non-interactive proof: every step must carry all four fields, and a step with a `run` entry
runs the real script (from gt's own scripts, GT_CORE_SCRIPTS or the newest gt beside this module) against
the vault and must print each `expect` line. A renamed script or a changed output fails the run.

The walk-through is ONE self-contained HTML file per role, built from the track alone: inline CSS and JS, no
network address of any kind, a copy button on what to type, back and next, a progress bar. The build is
deterministic (no date, no ordering by hash), and walkthroughs/<role>.html is committed; a test fails when a
committed page differs from a fresh build, so a renamed command or an edited step cannot leave a stale page.
Stdlib only.
"""
import argparse
import html
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TRACKS = os.path.join(ROOT, "templates", "demo-tracks")
WALK = os.path.join(ROOT, "walkthroughs")
FIELDS = ("goal", "say", "see", "why")
LABELS = (("goal", "What you are doing"), ("say", "What to type"), ("see", "What you will see"),
          ("why", "Why it matters"))
NAME = re.compile(r"^[a-z][a-z0-9-]{0,39}$")


def esc(text):
    return html.escape(str(text), quote=True)


def roles():
    try:
        return sorted(f[:-5] for f in os.listdir(TRACKS) if f.endswith(".json"))
    except OSError:
        return []


def load(role):
    if not NAME.match(role or "") or role not in roles():
        raise KeyError(role)
    with open(os.path.join(TRACKS, role + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def core_scripts():
    """gt's scripts directory: GT_CORE_SCRIPTS, else the newest gt beside this module (a checkout or the
    plugin cache), else the installed copy."""
    env = os.environ.get("GT_CORE_SCRIPTS")
    if env and os.path.isfile(os.path.join(env, "gt_next.py")):
        return env
    parent = os.path.dirname(os.path.dirname(ROOT))
    for base in (os.path.join(parent, "golden-thread"), os.path.join(parent, "gt"),
                 os.path.join(os.path.expanduser("~"), ".claude", "plugins", "cache",
                              "golden-thread-plugin", "gt")):
        try:
            vs = [d for d in os.listdir(base) if re.fullmatch(r"\d+\.\d+\.\d+", d)
                  and os.path.isfile(os.path.join(base, d, "scripts", "gt_next.py"))]
        except OSError:
            continue
        if vs:
            return os.path.join(base, max(vs, key=lambda v: tuple(int(x) for x in v.split("."))), "scripts")
    return None


def run_track(track, vault, today, core):
    """-> (exit code, report lines). Every step asserts its four fields; a scripted step runs for real."""
    lines, bad = [], 0
    for i, s in enumerate(track.get("steps") or [], 1):
        missing = [f for f in FIELDS if not (isinstance(s.get(f), str) and s[f].strip())]
        if missing:
            lines.append("FAIL step %d: missing %s" % (i, ", ".join(missing)))
            bad += 1
            continue
        r = s.get("run")
        if r:
            script = os.path.join(core or "", r["script"])
            if not core or not os.path.isfile(script):
                lines.append("FAIL step %d: %s was not found" % (i, r["script"]))
                bad += 1
                continue
            args = [a.replace("{vault}", vault).replace("{today}", today) for a in r.get("args", [])]
            p = subprocess.run([sys.executable, script] + args, capture_output=True, text=True, timeout=60)
            gone = [e for e in r.get("expect", []) if e not in p.stdout]
            if p.returncode != 0 or gone:
                lines.append("FAIL step %d: %s did not print %s" % (i, r["script"], "; ".join(gone) or "(exit %d)" % p.returncode))
                bad += 1
                continue
            lines.append("ok  step %d: %s (ran %s)" % (i, s["goal"], r["script"]))
        else:
            lines.append("ok  step %d: %s" % (i, s["goal"]))
    if not track.get("steps"):
        lines.append("FAIL: the track has no steps")
        bad += 1
    return (1 if bad else 0), lines


CSS = """
:root{--bg:#fbfaf7;--fg:#1d1c1a;--mut:#615e57;--card:#fff;--line:#d9d5cc;--acc:#1f5f8b;--accfg:#fff;--code:#f1eee7}
@media (prefers-color-scheme:dark){:root{--bg:#171614;--fg:#ece9e2;--mut:#aaa59a;--card:#211f1c;--line:#3a3732;--acc:#79b4e0;--accfg:#10202c;--code:#2a2723}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,sans-serif}
main{max-width:46rem;margin:0 auto;padding:1.5rem 16px 4rem}h1{font-size:1.5rem;margin:.2rem 0}
.sub{color:var(--mut);margin:0 0 1rem}
.bar{height:.5rem;background:var(--line);border-radius:.25rem;overflow:hidden;margin:1rem 0 .4rem}
.bar>i{display:block;height:100%;width:0;background:var(--acc);transition:width .2s}
.count{color:var(--mut);font-size:.9rem;margin-bottom:1rem}
.step{display:none;background:var(--card);border:1px solid var(--line);border-radius:.6rem;padding:1rem 1.2rem}
.step.on{display:block}.step h2{font-size:1.15rem;margin:.1rem 0 .8rem}
dt{font-weight:600;margin-top:.8rem;color:var(--mut);font-size:.85rem;text-transform:uppercase;letter-spacing:.04em}
dd{margin:.2rem 0 0}.say{display:flex;gap:.6rem;align-items:flex-start}
code{background:var(--code);padding:.35rem .55rem;border-radius:.35rem;font:.95rem ui-monospace,monospace;flex:1;overflow-wrap:anywhere}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:.4rem;padding:.45rem .9rem;cursor:pointer}
button.p{background:var(--acc);color:var(--accfg);border-color:var(--acc)}button:disabled{opacity:.45;cursor:default}
nav{display:flex;gap:.6rem;margin-top:1rem}nav .sp{flex:1}
"""
JS = """
(function(){
var steps=[].slice.call(document.querySelectorAll('.step')),i=0,bar=document.getElementById('fill'),
    pb=document.getElementById('bar'),cnt=document.getElementById('count'),b=document.getElementById('back'),
    n=document.getElementById('next');
function show(k){i=Math.max(0,Math.min(steps.length-1,k));
 steps.forEach(function(s,j){s.classList.toggle('on',j===i);});
 var pct=Math.round((i+1)*100/steps.length);bar.style.width=pct+'%';pb.setAttribute('aria-valuenow',pct);
 cnt.textContent='Step '+(i+1)+' of '+steps.length;b.disabled=i===0;n.disabled=i===steps.length-1;}
b.addEventListener('click',function(){show(i-1);});n.addEventListener('click',function(){show(i+1);});
document.addEventListener('keydown',function(e){if(e.key==='ArrowRight')show(i+1);if(e.key==='ArrowLeft')show(i-1);});
[].forEach.call(document.querySelectorAll('[data-copy]'),function(btn){
 btn.addEventListener('click',function(){var t=btn.getAttribute('data-copy');
  var done=function(){btn.textContent='Copied';setTimeout(function(){btn.textContent='Copy';},1500);};
  if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(t).then(done,function(){});}
  else{var a=document.createElement('textarea');a.value=t;document.body.appendChild(a);a.select();
   try{document.execCommand('copy');done();}catch(x){}document.body.removeChild(a);}});});
show(0);})();
"""


def walkthrough_html(track):
    """-> the walk-through page for one track, as text. Deterministic; everything escaped."""
    steps = []
    for s in track["steps"]:
        rows = []
        for key, label in LABELS:
            if key == "say":
                rows.append('<dt>%s</dt><dd class="say"><code>%s</code><button type="button" data-copy="%s">Copy</button></dd>'
                            % (label, esc(s[key]), esc(s[key])))
            else:
                rows.append("<dt>%s</dt><dd>%s</dd>" % (label, esc(s[key])))
        steps.append('<section class="step" aria-label="Step %d"><h2>%d. %s</h2><dl>%s</dl></section>'
                     % (s["n"], s["n"], esc(s["goal"]), "".join(rows)))
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>%s</title><style>%s</style></head><body><main>\n"
            "<h1>%s</h1><p class=\"sub\">%s Practice project: %s (a pretend project; nothing here touches your real notes).</p>\n"
            "<div class=\"bar\" id=\"bar\" role=\"progressbar\" aria-valuemin=\"0\" aria-valuemax=\"100\" aria-valuenow=\"0\">"
            "<i id=\"fill\"></i></div><div class=\"count\" id=\"count\"></div>\n%s\n"
            "<nav><button type=\"button\" id=\"back\">Back</button><span class=\"sp\"></span>"
            "<button type=\"button\" class=\"p\" id=\"next\">Next</button></nav>\n"
            "</main><script>%s</script></body></html>\n"
            % (esc(track["title"]), CSS.strip(), esc(track["title"]), esc(track["audience"]),
               esc(track.get("project", "demo")), "\n".join(steps), JS.strip()))


def show_text(track):
    out = ["%s -- %s" % (track["track"], track["title"]), track["audience"], ""]
    for s in track["steps"]:
        out += ["%d. %s" % (s["n"], s["goal"]), "   type:  %s" % s["say"], "   see:   %s" % s["see"],
                "   why:   %s" % s["why"], ""]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="the demo's role tracks")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for n in ("show", "run", "walkthrough", "path"):
        p = sub.add_parser(n)
        p.add_argument("role")
        if n == "run":
            p.add_argument("--vault", required=True)
            p.add_argument("--today", default=None)
        if n == "walkthrough":
            p.add_argument("--write", action="store_true")
            p.add_argument("--out")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        for r in roles():
            t = load(r)
            print("%-12s %d steps  %s" % (r, len(t["steps"]), t["title"]))
        return 0
    try:
        track = load(a.role)
    except KeyError:
        print("unknown track %r; one of: %s" % (a.role, ", ".join(roles())))
        return 2
    if a.cmd == "show":
        print(show_text(track))
    elif a.cmd == "path":
        print(os.path.join(WALK, a.role + ".html"))
    elif a.cmd == "run":
        import datetime
        rc, lines = run_track(track, a.vault, a.today or datetime.date.today().isoformat(), core_scripts())
        print("\n".join(lines))
        return rc
    else:
        page = walkthrough_html(track)
        dest = a.out or (os.path.join(WALK, a.role + ".html") if a.write else None)
        if dest:
            os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
            with open(dest, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(page)
            print("wrote %s" % dest)
        else:
            sys.stdout.write(page)
    return 0


if __name__ == "__main__":
    sys.exit(main())
