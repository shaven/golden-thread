#!/usr/bin/env python3
"""gt_recipe.py -- turn a short recipe into the finished shell script, from one tested template.

    gt_recipe.py render <recipe> [--out FILE] [--dry-run]   write (or print) the generated script
    gt_recipe.py check  <recipe> --out FILE                 exit 0 only if FILE is what render makes
    gt_recipe.py show   <recipe> [--json]                   the parsed steps, in run order

WHY (owner, 2026-10-01): "Scripts that will read a recipe that you define and just spit out the
finished sh file?" Every hand-written step runner re-implements the same scaffolding -- option
parsing, usage, step order, ok/FAIL reporting, stop at the first failure, counting what ran -- and
every copy needs its own tests. Here the scaffolding is written and tested ONCE; a recipe carries
only what is particular to it (its steps), and a generated script needs only a smoke run.

TWO RECIPE FORMS, ONE MODEL

1. A steps table (`*.tsv`), the release pipeline's form (gt_pipeline.py). One row per step,
   TAB-separated: id, kind (step|gate), guarantee, command, after (id or -), required (yes|no),
   origin (default|user). Lines starting with `#` are comments; `# project: <slug>` and
   `# script: <name>` are read as settings. A command starting `@` is a gt builtin
   (`gt_pipeline.py builtin <name>`), anything else is a shell command run from the repo root.
   Rendered in REPORT mode: each step prints ok / FAIL / SKIP-not-applicable and its time, a
   required FAIL stops the run, and the run ends with how many steps ran and which.

2. A script recipe (`*.recipe`), for a script whose steps are code (dev/copygt.recipe):

       name: copygt.sh
       mode: plain                  # plain: steps run in order under `set -e`; report: as above
       strict: yes                  # set -euo pipefail (no: set -uo pipefail)
       doc:                         # becomes the leading comment block
         copygt.sh -- ...
       usage:                       # printed by -h/--help and on a usage error
         ./copygt.sh --dest <repo>
       option: --dest | value | DEST | | the repository to copy into
       option: --dry-run | flag | DRY | yes | list every change, write nothing
       option: --no-push | ignore | | | accepted for older notes
       prologue:
         SRC="$(cd "$(dirname "$0")" && pwd -P)"
       step: verify | step | gt-src matches what was published | - | yes
         echo "== 1. verify"
         ...
       epilogue:
         echo done

   A block (doc, usage, prologue, epilogue, a step body) is every following line indented by two
   spaces (blank lines included); the indent is removed. `option:` and `step:` fields are
   `|`-separated.

THE SAFETY PROPERTIES THE TEMPLATE OWNS (so no recipe can forget them)

  * Steps run in DEPENDENCY order: file order, with every step moved after the step its `after`
    names. A cycle or an `after` naming no step is refused at render time, never rendered.
  * REPORT mode: a gate that cannot run is a FAIL, never a pass. A step exits 99 to say "not
    applicable here" (printed SKIP-not-applicable); any other non-zero exit is FAIL. A FAIL of a
    required step stops the run; an optional one is reported and the run continues.
  * The generated file names the recipe and its sha256, and `check` re-renders and compares, so
    a hand edit to a generated script, or a recipe changed without re-rendering, is caught.
  * `--list` (report mode) / `--list-steps` (plain mode) print the steps and their guarantees
    without running anything.

Exit: 0 ok | 1 check: the script differs from the recipe | 2 usage or an invalid recipe
"""
import argparse
import hashlib
import json
import os
import re
import sys

SKIP_RC = 99
BUILTINS = ("tests", "allin", "branch", "install", "owner-gate", "push", "sync")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
VAR_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
TSV_COLS = ("id", "kind", "guarantee", "command", "after", "required", "origin")


class RecipeError(ValueError):
    pass


# ------------------------------------------------------------------ parsing ----

def _blank_recipe(name):
    return {"name": name, "mode": "report", "strict": "no", "doc": [], "usage": [],
            "options": [], "prologue": [], "epilogue": [], "steps": [], "settings": {},
            "removed": []}


def parse_tsv(text, name="release.sh"):
    r = _blank_recipe(name)
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        if line.startswith("#"):
            m = re.match(r"^#\s*(project|script):\s*(\S+)\s*$", line)
            if m:
                r["settings"][m.group(1)] = m.group(2)
                if m.group(1) == "script":
                    r["name"] = m.group(2)
            m = re.match(r"^#\s*removed:\s*(.+)$", line)
            if m:
                r["removed"].append(m.group(1).strip())
            continue
        cols = line.split("\t")
        if len(cols) == 6:
            cols.append("user")
        if len(cols) != 7:
            raise RecipeError("line %d: expected 7 TAB-separated columns (%s), found %d"
                              % (n, ", ".join(TSV_COLS), len(cols)))
        step = dict(zip(TSV_COLS, (c.strip() for c in cols)))
        step["body"] = [command_line(step["command"])]
        r["steps"].append(step)
    r["mode"] = "report"
    nm = r["name"]
    r["doc"] = ["%s -- this project's release pipeline: the steps in release-pipeline.tsv, in "
                "order." % nm, "",
                "A required step that fails stops the run; a step exiting %d is not applicable "
                "here (SKIP)." % SKIP_RC,
                "Change the steps with gt_pipeline.py add / set / remove, which re-render this "
                "file."]
    r["usage"] = ["./%s [--list] [--dry-run] [--until ID] [--from ID] [--go]" % nm]
    validate(r)
    return r


def command_line(cmd):
    """The shell line a TSV command becomes. A builtin runs through gt_pipeline.py."""
    cmd = cmd.strip()
    if cmd.startswith("@"):
        name = cmd[1:].strip()
        if name not in BUILTINS:
            raise RecipeError("unknown builtin @%s (known: %s)"
                              % (name, ", ".join("@" + b for b in BUILTINS)))
        return 'gt_builtin %s' % name
    return cmd


def _split(spec, n):
    parts = [p.strip() for p in spec.split("|")]
    while len(parts) < n:
        parts.append("")
    if len(parts) > n:                      # the last field may itself contain '|'
        parts = parts[:n - 1] + ["|".join(parts[n - 1:])]
    return parts


def parse_recipe(text, name=None):
    r = _blank_recipe(name or "script.sh")
    lines = text.splitlines()
    i = 0
    block = None                     # the list the indented lines go into

    def take_block(start):
        out = []
        j = start
        while j < len(lines):
            ln = lines[j]
            if ln.startswith("  ") or not ln.strip():
                out.append(ln[2:] if ln.startswith("  ") else "")
                j += 1
                continue
            break
        while out and not out[-1].strip():
            out.pop()
        return out, j

    while i < len(lines):
        ln = lines[i]
        if not ln.strip() or ln.startswith("#"):
            i += 1
            continue
        m = re.match(r"^([a-z]+):\s?(.*)$", ln)
        if not m:
            raise RecipeError("line %d: expected `key: value`, found %r" % (i + 1, ln[:60]))
        key, val = m.group(1), m.group(2).rstrip()
        i += 1
        if key in ("name", "mode", "strict"):
            r[key] = val.strip()
        elif key in ("doc", "usage", "prologue", "epilogue"):
            block, i = take_block(i)
            r[key] = block
        elif key == "option":
            flag, kind, var, setval, helptext = _split(val, 5)
            r["options"].append({"flag": flag, "kind": kind, "var": var, "set": setval,
                                 "help": helptext})
        elif key == "step":
            sid, kind, guarantee, after, required = _split(val, 5)
            body, i = take_block(i)
            r["steps"].append({"id": sid, "kind": kind or "step", "guarantee": guarantee,
                               "command": "", "after": after or "-",
                               "required": required or "yes", "origin": "user",
                               "body": body})
        else:
            raise RecipeError("line %d: unknown key %r" % (i, key))
    validate(r)
    return r


def load(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if path.endswith(".tsv"):
        return parse_tsv(text), text
    return parse_recipe(text, os.path.basename(path)), text


# --------------------------------------------------------------- validation ----

def validate(r):
    if r["mode"] not in ("report", "plain"):
        raise RecipeError("mode must be report or plain, not %r" % r["mode"])
    if r["strict"] not in ("yes", "no"):
        raise RecipeError("strict must be yes or no")
    seen = set()
    for s in r["steps"]:
        if not ID_RE.match(s["id"] or ""):
            raise RecipeError("step id %r: use lowercase letters, digits and -" % s["id"])
        if s["id"] in seen:
            raise RecipeError("step id %r appears twice" % s["id"])
        seen.add(s["id"])
        if s["kind"] not in ("step", "gate"):
            raise RecipeError("step %s: kind must be step or gate, not %r" % (s["id"], s["kind"]))
        if s["required"] not in ("yes", "no"):
            raise RecipeError("step %s: required must be yes or no" % s["id"])
        if not any(b.strip() for b in s["body"]):
            raise RecipeError("step %s has no command" % s["id"])
    for s in r["steps"]:
        if s["after"] not in ("-", "") and s["after"] not in seen:
            raise RecipeError("step %s runs after %r, which is not a step" % (s["id"], s["after"]))
    for o in r["options"]:
        if not o["flag"].startswith("--"):
            raise RecipeError("option %r must start with --" % o["flag"])
        if o["kind"] not in ("value", "flag", "ignore"):
            raise RecipeError("option %s: kind must be value, flag or ignore" % o["flag"])
        if o["kind"] != "ignore" and not VAR_RE.match(o["var"]):
            raise RecipeError("option %s: variable %r is not a shell variable name"
                              % (o["flag"], o["var"]))
    order(r["steps"])                  # raises on a cycle
    return r


def order(steps):
    """File order, with each step moved after the step its `after` names. Refuses a cycle."""
    by_id = {s["id"]: s for s in steps}
    out, state = [], {}

    def visit(s, chain):
        st = state.get(s["id"])
        if st == "done":
            return
        if st == "active":
            raise RecipeError("cycle: %s" % " -> ".join(chain + [s["id"]]))
        state[s["id"]] = "active"
        dep = s.get("after")
        if dep and dep != "-" and dep in by_id:
            visit(by_id[dep], chain + [s["id"]])
        state[s["id"]] = "done"
        out.append(s)

    # Visit in file order; a step whose dependency comes later in the file is emitted after it.
    for s in steps:
        visit(s, [])
    # `visit` emits a dependency before its dependant but keeps unrelated steps in file order;
    # a step must also follow EVERYTHING its dependency follows, which the DFS already gives.
    return out


# ---------------------------------------------------------------- rendering ----

def _sh_quote(s):
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _comment(lines):
    return ["# " + l if l else "#" for l in lines]


def render(r, recipe_text, recipe_name):
    sha = hashlib.sha256(recipe_text.encode("utf-8")).hexdigest()
    steps = order(r["steps"])
    out = ["#!/usr/bin/env bash"]
    out += _comment(r["doc"] or ["%s -- generated step runner." % r["name"]])
    out += ["#",
            "# GENERATED by gt_recipe.py from %s (recipe sha256 %s)." % (recipe_name, sha[:16]),
            "# Do not edit this file: edit the recipe, then run",
            "#   gt_recipe.py render %s --out %s" % (recipe_name, r["name"]),
            "# (`gt_recipe.py check` fails while this file and its recipe disagree.)"]
    # REPORT mode never runs under -e: the runner itself decides what a step's exit means.
    out.append("set -euo pipefail" if r["strict"] == "yes" and r["mode"] == "plain"
               else "set -uo pipefail")
    out.append("GT_RECIPE_SHA=%s" % sha[:16])

    # option defaults
    for o in r["options"]:
        if o["kind"] == "value":
            out.append('%s=""' % o["var"])
        elif o["kind"] == "flag":
            out.append("%s=no" % o["var"])
    if r["mode"] == "report":
        out += ["GT_DRY=no; GT_LIST=no; GT_UNTIL=\"\"; GT_FROM=\"\"; GT_GO=no"]

    usage = r["usage"] or ["%s [options]" % r["name"]]
    out.append("usage() {")
    out.append("  cat <<'GT_USAGE'")
    out += usage
    if r["mode"] == "report":
        out += ["",
                "  --list        print the steps in run order and exit",
                "  --dry-run     say what each step would run; run nothing",
                "  --until ID    stop BEFORE step ID (e.g. --until owner-gate)",
                "  --from ID     start at step ID",
                "  --go          the owner's explicit go-ahead, for the owner-gate step"]
    out.append("GT_USAGE")
    out.append("}")

    # the step table, for --list / --list-steps and for the report
    out.append("GT_STEPS=(%s)" % " ".join(s["id"] for s in steps))
    for s in steps:
        v = s["id"].replace("-", "_")
        out.append("GT_G_%s=%s" % (v, _sh_quote(s["guarantee"] or "")))
        out.append("GT_K_%s=%s" % (v, s["kind"]))
        out.append("GT_R_%s=%s" % (v, s["required"]))
        if r["mode"] == "report":               # the command, for --dry-run's "would run"
            out.append("GT_C_%s=%s" % (v, _sh_quote(" ; ".join(b for b in s["body"]
                                                             if b.strip())[:300])))
    out += ["list_steps() {",
            "  local i=0 v g k q",
            "  for id in \"${GT_STEPS[@]}\"; do",
            "    i=$((i+1)); v=${id//-/_}; g=GT_G_$v; k=GT_K_$v; q=GT_R_$v",
            "    printf '%2d. %-14s %-5s required=%-3s %s\\n' \"$i\" \"$id\" \"${!k}\" \"${!q}\" \"${!g}\"",
            "  done",
            "}"]

    # option parsing
    out.append('while [ $# -gt 0 ]; do')
    out.append('  case "$1" in')
    for o in r["options"]:
        f = o["flag"]
        if o["kind"] == "value":
            out.append('    %s) [ $# -ge 2 ] || { echo "REFUSED: %s needs a value"; exit 2; }; '
                       '%s="$2"; shift 2 ;;' % (f, f, o["var"]))
            out.append('    %s=*) %s="${1#%s=}"; shift ;;' % (f, o["var"], f))
        elif o["kind"] == "flag":
            out.append('    %s) %s=%s; shift ;;' % (f, o["var"], _sh_quote(o["set"] or "yes")))
        else:
            out.append('    %s) shift ;;' % f)
    if r["mode"] == "report":
        out += ['    --list) GT_LIST=yes; shift ;;',
                '    --dry-run) GT_DRY=yes; shift ;;',
                '    --until) [ $# -ge 2 ] || { echo "REFUSED: --until needs a step id"; exit 2; }; '
                'GT_UNTIL="$2"; shift 2 ;;',
                '    --from) [ $# -ge 2 ] || { echo "REFUSED: --from needs a step id"; exit 2; }; '
                'GT_FROM="$2"; shift 2 ;;',
                '    --go) GT_GO=yes; shift ;;']
    else:
        out.append('    --list-steps) list_steps; exit 0 ;;')
    out += ['    -h|--help) usage; exit 0 ;;',
            '    *) echo "unknown option: $1"; usage; exit 2 ;;',
            '  esac',
            'done']

    if r["mode"] == "report":
        out += _report_prologue()
    out += r["prologue"]

    for s in steps:
        out.append("step_%s() {" % s["id"].replace("-", "_"))
        # NOT indented: a heredoc terminator in a body must stay at column 0 to end its heredoc.
        out += s["body"]
        out.append("}")

    if r["mode"] == "plain":
        for s in steps:
            out.append("step_%s" % s["id"].replace("-", "_"))
        out += r["epilogue"]
    else:
        out += _report_runner()
        out += r["epilogue"]
    return "\n".join(out) + "\n"


def _report_prologue():
    # REPO is the directory holding the generated script: the project's code root. gt_builtin
    # finds gt_pipeline.py through $GT_PIPELINE_PY, then the newest installed gt release, so the
    # generated file carries no machine-specific path.
    return [
        'REPO="$(cd "$(dirname "$0")" && pwd -P)"',
        'cd "$REPO"',
        'gt_find() {',
        '  local name="$1" var="$2" c',
        '  if [ -n "${!var:-}" ] && [ -f "${!var}" ]; then echo "${!var}"; return 0; fi',
        '  c=$(ls -d "$HOME"/.claude/plugins/cache/*/gt/*/scripts/"$name" 2>/dev/null '
        '| sort -V | tail -1)',
        '  [ -n "$c" ] && echo "$c"',
        '}',
        'gt_builtin() {',
        '  local p',
        '  p=$(gt_find gt_pipeline.py GT_PIPELINE_PY) || true',
        '  if [ -z "$p" ]; then echo "  cannot run @$1: gt_pipeline.py not found (install gt, '
        'or set GT_PIPELINE_PY)"; return 1; fi',
        '  GT_OWNER_GO="$GT_GO" python3 "$p" builtin "$1" --repo "$REPO"',
        '}',
        'gt_metric() {',
        '  [ "${GT_EXECUTION_METRICS:-on}" = off ] && return 0',
        '  local m',
        '  m=$(gt_find gt_metrics.py GT_METRICS_PY) || return 0',
        '  [ -n "$m" ] || return 0',
        '  python3 "$m" record --repo "$REPO" --process "$1" --duration "$2" --exit "$3" '
        '--trait pipeline >/dev/null 2>&1 || true',
        '}',
        'gt_now() { python3 -c "import time; print(time.time())"; }',
        'gt_secs() { python3 -c "import sys; print(\'%.1f\' % (float(sys.argv[2]) - float(sys.argv[1])))"'
        ' "$1" "$2"; }',
    ]


def _report_runner():
    return [
        'if [ "$GT_LIST" = yes ]; then list_steps; exit 0; fi',
        'gt_known() { local x; for x in "${GT_STEPS[@]}"; do [ "$x" = "$1" ] && return 0; done; '
        'return 1; }',
        'for _f in "$GT_UNTIL" "$GT_FROM"; do',
        '  if [ -n "$_f" ] && ! gt_known "$_f"; then echo "REFUSED: no step named $_f"; '
        'list_steps; exit 2; fi',
        'done',
        'GT_RAN=(); GT_SKIPPED=(); GT_WARNED=(); GT_NOT=(); GT_STARTED=no; GT_STOPPED=no',
        '[ -z "$GT_FROM" ] && GT_STARTED=yes',
        'GT_T0=$(gt_now)',
        'for id in "${GT_STEPS[@]}"; do',
        '  v=${id//-/_}',
        '  if [ "$GT_STOPPED" = yes ] || [ "$id" = "$GT_UNTIL" ]; then GT_STOPPED=yes; '
        'GT_NOT+=("$id"); continue; fi',
        '  if [ "$GT_STARTED" = no ]; then',
        '    if [ "$id" = "$GT_FROM" ]; then GT_STARTED=yes; else GT_NOT+=("$id"); continue; fi',
        '  fi',
        '  _g=GT_G_$v; _r=GT_R_$v; _c=GT_C_$v; g="${!_g}"; req="${!_r}"; cmd="${!_c}"',
        '  if [ "$GT_DRY" = yes ]; then printf "would run  %-14s %s\\n" "$id" "$cmd"; '
        'continue; fi',
        '  echo "== $id: $g"',
        '  t=$(gt_now)',
        '  set +e',
        '  ( "step_$v" )',
        '  rc=$?',
        '  s=$(gt_secs "$t" "$(gt_now)")',
        '  gt_metric "release:$id" "$s" "$rc"',
        '  if [ "$rc" -eq 0 ]; then printf "ok     %-14s %6ss\\n" "$id" "$s"; GT_RAN+=("$id")',
        '  elif [ "$rc" -eq ' + str(SKIP_RC) + ' ]; then printf "SKIP-not-applicable %-14s\\n" "$id"; '
        'GT_SKIPPED+=("$id")',
        '  elif [ "$req" = no ]; then printf "FAIL   %-14s %6ss  (exit %s; optional, continuing)\\n" '
        '"$id" "$s" "$rc"; GT_RAN+=("$id"); GT_WARNED+=("$id")',
        '  else',
        '    printf "FAIL   %-14s %6ss  (exit %s)\\n" "$id" "$s" "$rc"',
        '    GT_RAN+=("$id"); GT_STOPPED=yes; GT_FAILED="$id"',
        '  fi',
        'done',
        '[ "$GT_DRY" = yes ] && { echo "(dry run: nothing was run)"; exit 0; }',
        'GT_TOTAL=$(gt_secs "$GT_T0" "$(gt_now)")',
        'echo ""',
        'echo "ran ${#GT_RAN[@]} of ${#GT_STEPS[@]} step(s) in ${GT_TOTAL}s: ${GT_RAN[*]:-none}"',
        '[ ${#GT_SKIPPED[@]} -gt 0 ] && echo "not applicable: ${GT_SKIPPED[*]}"',
        '[ ${#GT_WARNED[@]} -gt 0 ] && echo "optional steps that failed: ${GT_WARNED[*]}"',
        '[ ${#GT_NOT[@]} -gt 0 ] && echo "not run: ${GT_NOT[*]}"',
        'if [ -n "${GT_FAILED:-}" ]; then gt_metric release "$GT_TOTAL" 1; '
        'echo "STOPPED at the first failure: $GT_FAILED"; exit 1; fi',
        'gt_metric release "$GT_TOTAL" 0',
        'exit 0',
    ]


# --------------------------------------------------------------------- CLI ----

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("render", help="write the generated script")
    rp.add_argument("recipe")
    rp.add_argument("--out", help="the script to write (default: print it)")
    rp.add_argument("--dry-run", action="store_true", help="print, write nothing")
    ck = sub.add_parser("check", help="exit 0 only if --out is what render would write")
    ck.add_argument("recipe")
    ck.add_argument("--out", required=True)
    sh = sub.add_parser("show", help="the parsed steps, in run order")
    sh.add_argument("recipe")
    sh.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    try:
        r, text = load(a.recipe)
    except (OSError, RecipeError) as exc:
        print("gt_recipe: %s: %s" % (a.recipe, exc), file=sys.stderr)
        return 2
    name = os.path.basename(a.recipe)
    if a.cmd == "show":
        steps = order(r["steps"])
        if a.json:
            print(json.dumps({"name": r["name"], "mode": r["mode"],
                              "steps": [{k: s[k] for k in TSV_COLS if k in s} for s in steps]},
                             indent=2))
        else:
            for n, s in enumerate(steps, 1):
                print("%2d. %-14s %-5s required=%-3s %s" % (n, s["id"], s["kind"], s["required"],
                                                           s["guarantee"]))
        return 0
    script = render(r, text, name)
    if a.cmd == "check":
        try:
            with open(a.out, encoding="utf-8") as fh:
                have = fh.read()
        except OSError:
            print("%s does not exist -- run: gt_recipe.py render %s --out %s"
                  % (a.out, a.recipe, a.out))
            return 1
        if have == script:
            print("%s is current with %s" % (a.out, name))
            return 0
        print("%s differs from what %s renders -- it was edited by hand, or the recipe changed "
              "without a render. Run: gt_recipe.py render %s --out %s"
              % (a.out, name, a.recipe, a.out))
        return 1
    if a.dry_run or not a.out:
        sys.stdout.write(script)
        return 0
    write_script(a.out, script)
    print("wrote %s from %s (%d step(s))" % (a.out, name, len(r["steps"])))
    return 0


def write_script(path, script):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(script)
    os.chmod(tmp, 0o755)
    os.replace(tmp, path)


if __name__ == "__main__":
    sys.exit(main())
