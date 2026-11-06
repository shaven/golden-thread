#!/bin/sh
# gt-model-for.sh -- print the id of the cheapest engine that gt-farm's calibration says can take a task type.
#
#   gt-model-for.sh TASK_TYPE [--vault DIR] [--include-review] [--task-key KEY]
#
# With --task-key KEY it also applies the farm-share table (farm-share.tsv) when the farm_share setting is on:
# it prints the engine for THAT task, or the word keep when the task stays with your own model. The same key
# always gets the same answer. It is advice only: it records no spend and moves no ramp. With farm_share off
# the key is ignored and you get the cheapest capable engine, as without it.
#
# TASK_TYPE is a class (log-triage, quote-extract, extract, classify, draft, generalize, review, summarise,
# code-fix, code-feature) or an alias (quote-extraction, field-extraction, classification, drafting,
# generalising-claims, code-review, summaries). It reads the live calibration store through
# `gt_farm_calibrate.py --export-json --top 1` and writes nothing. Meant for a coding assistant's model line
# (opencode, Codex); see FARM-LOCAL-ENGINES.md, "Wiring opencode and Codex to calibration verdicts".
#
# Prints one engine id and a newline. Exit: 0 found | 1 no engine can take the task (prints $GT_MODEL_FALLBACK
# when that is set, and then exits 0) | 2 usage or unknown task type | 3 gt-farm not found.
#
# Where the scripts are: $GT_FARM_SCRIPTS (a directory), else ../scripts next to this file, else the newest
# installed gt-farm under ~/.claude/plugins. $GT_PYTHON names the interpreter (default python3, then python).
# POSIX sh. On Windows run it under Git Bash.

task=""
extra=""
vault=""
key=""
haskey=""
while [ $# -gt 0 ]; do
    case "$1" in
        --vault) [ $# -ge 2 ] || { echo "gt-model-for: --vault needs a directory" >&2; exit 2; }
                 vault="$2"; shift 2 ;;
        --include-review) extra="--include-review"; shift ;;
        --task-key) [ $# -ge 2 ] || { echo "gt-model-for: --task-key needs a key" >&2; exit 2; }
                    key="$2"; haskey=1; shift 2 ;;
        -h|--help) sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "gt-model-for: unknown option $1" >&2; exit 2 ;;
        *) if [ -n "$task" ]; then echo "gt-model-for: one task type only" >&2; exit 2; fi
           task="$1"; shift ;;
    esac
done
[ -n "$task" ] || { echo "usage: gt-model-for.sh TASK_TYPE [--vault DIR] [--include-review] [--task-key KEY]" >&2; exit 2; }

here=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
scripts=""
if [ -n "$GT_FARM_SCRIPTS" ] && [ -f "$GT_FARM_SCRIPTS/gt_farm_calibrate.py" ]; then
    scripts="$GT_FARM_SCRIPTS"
elif [ -n "$here" ] && [ -f "$here/../scripts/gt_farm_calibrate.py" ]; then
    scripts="$here/../scripts"
else
    for d in "$HOME"/.claude/plugins/cache/golden-thread-plugin/gt-farm/*/scripts; do
        [ -f "$d/gt_farm_calibrate.py" ] && scripts="$d"
    done
fi
[ -n "$scripts" ] || { echo "gt-model-for: gt-farm scripts not found (set GT_FARM_SCRIPTS)" >&2; exit 3; }

py="$GT_PYTHON"
if [ -z "$py" ]; then
    if command -v python3 >/dev/null 2>&1; then py=python3; else py=python; fi
fi

if [ -n "$haskey" ]; then
    if [ -n "$vault" ]; then
        out=$("$py" "$scripts/gt_farm_calibrate.py" --export-json --task-type "$task" --task-key "$key" $extra --vault "$vault")
    else
        out=$("$py" "$scripts/gt_farm_calibrate.py" --export-json --task-type "$task" --task-key "$key" $extra)
    fi
    rc=$?
    [ $rc -eq 0 ] || exit $rc
    printf '%s\n' "$(printf '%s' "$out" | "$py" -c 'import json,sys
print(json.load(sys.stdin)["decision"])')"
    exit 0
fi

if [ -n "$vault" ]; then
    out=$("$py" "$scripts/gt_farm_calibrate.py" --export-json --task-type "$task" --top 1 $extra --vault "$vault")
else
    out=$("$py" "$scripts/gt_farm_calibrate.py" --export-json --task-type "$task" --top 1 $extra)
fi
rc=$?
if [ $rc -ne 0 ]; then
    exit $rc
fi

engine=$(printf '%s' "$out" | "$py" -c 'import json,sys
r = json.load(sys.stdin)
print(r[0]["engine"] if r else "")')

if [ -n "$engine" ]; then
    printf '%s\n' "$engine"
    exit 0
fi
if [ -n "$GT_MODEL_FALLBACK" ]; then
    printf '%s\n' "$GT_MODEL_FALLBACK"
    exit 0
fi
echo "gt-model-for: no calibrated engine can take '$task'" >&2
exit 1
