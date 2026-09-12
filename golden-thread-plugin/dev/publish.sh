#!/usr/bin/env bash
# publish.sh — the ONE entry point for publishing a release. Runs every requirement in
# order and stops at the first failure.
#
#   dev/publish.sh --dry-run   # say what each step would do, change nothing
#   dev/publish.sh             # publish
#   dev/publish.sh --list      # print the requirements and exit
#
# WHY THIS EXISTS
#
# "Publishing" was four things a person had to remember in the right order: run the
# gate, commit, push, sync gt-src — plus an announcement, and a line in the vault log.
# On 2026-09-12 the first three happened and gt-src was left a release behind, which is
# invisible from this repo: everything here was correct and the shared working copy the
# OTHER machine reads was stale. A checklist in someone's head has no failure mode that
# anyone can see.
#
# THE REQUIREMENTS ARE DATA, not prose. STEPS below is the list; each entry is
# id|description|command. Adding a requirement means adding a line, and --list prints
# exactly what will run, so the contract can be read without reading the script.
#
# Every step is required. There is deliberately no --skip: a step you are allowed to
# skip is not a requirement, and the ones here each exist because something once
# shipped without them.
set -uo pipefail
cd "$(dirname "$0")/.."
DRY=no; LIST=no
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=yes ;;
    --list) LIST=yes ;;
    *) echo "usage: publish.sh [--dry-run | --list]"; exit 2 ;;
  esac
done

# id | what it guarantees | command ({dry} expands to --dry-run, or is dropped)
STEPS=(
  "gate|every release check passes, tests and selftest included|dev/release-check.sh"
  "committed|the tree is committed, so what is published equals a commit|git diff --quiet HEAD --"
  "pushed|the commit exists on the remote others read|git_pushed"
  "gt-src|the shared working copy holds this release, verified by selftest|dev/sync-gt-src.sh {dry}"
  "announced|a Discussion names this version (warn only — needs gh)|check_announced"
  "logged|the vault records the publish|log_to_vault {dry}"
)

GTV=$(ls -d golden-thread/*/ 2>/dev/null | sed 's#.*/\([^/]*\)/#\1#' \
      | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -t. -k1,1n -k2,2n -k3,3n | tail -1)

if [ "$LIST" = yes ]; then
  echo "publish requirements for gt $GTV, in order:"
  for s in "${STEPS[@]}"; do
    printf '  %-11s %s\n' "${s%%|*}" "$(echo "$s" | cut -d'|' -f2)"
  done
  exit 0
fi

git_pushed() {
  local head remote
  head=$(git rev-parse HEAD)
  remote=$(git rev-parse "@{u}" 2>/dev/null) || { echo "    no upstream branch"; return 1; }
  [ "$head" = "$remote" ] || {
    echo "    HEAD $head is not the upstream commit $remote — push first"; return 1; }
}

check_announced() {
  # WARN only. An unannounced release is a communication gap, not a broken artefact,
  # and this is the one step that depends on a network and a logged-in gh.
  command -v gh >/dev/null 2>&1 || { echo "    gh not installed — cannot check"; return 0; }
  if gh api graphql -f query='query{repository(owner:"shaven",name:"golden-thread")
      {discussions(first:20){nodes{title}}}}' --jq '.data.repository.discussions.nodes[].title' \
      2>/dev/null | grep -q "$GTV"; then
    echo "    a Discussion names $GTV"
  else
    echo "    WARNING: no Discussion names $GTV — announce it after this completes"
  fi
}

log_to_vault() {
  local dry="${1:-}"
  local vault
  vault=$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.claude/vault-config.json')))['vault_path'])" 2>/dev/null) || {
    echo "    no vault configured — nothing to log"; return 0; }
  local tool="$vault/Projects/golden-thread/tools/gt_log.py"
  [ -f "$tool" ] || { echo "    $tool is absent — nothing to log"; return 0; }
  # core_explicit_vault_target: name the vault, every time, even here.
  python3 "$tool" add --vault "$vault" $dry \
    "work plugin -> gt-src: published gt $GTV from $(git rev-parse --short HEAD)"
}

FAILED=""
echo "publishing gt $GTV  ($([ "$DRY" = yes ] && echo "DRY RUN" || echo "for real"))"
for s in "${STEPS[@]}"; do
  id=${s%%|*}; rest=${s#*|}; desc=${rest%%|*}; cmd=${rest#*|}
  if [ "$DRY" = yes ]; then
    cmd=${cmd//\{dry\}/--dry-run}
  else
    cmd=${cmd//\{dry\}/}
  fi
  printf '\n== %s — %s\n' "$id" "$desc"
  if [ "$DRY" = yes ] && [ "$id" = "gate" ]; then
    echo "    would run: dev/release-check.sh"
    continue
  fi
  # shellcheck disable=SC2086
  if eval "$cmd" > /tmp/publish-$id.log 2>&1; then
    tail -2 /tmp/publish-$id.log | sed 's/^/    /'
    echo "ok    $id"
  else
    tail -12 /tmp/publish-$id.log | sed 's/^/    /'
    echo "FAIL  $id — stopping here. Nothing after this step has run."
    FAILED=$id
    break
  fi
done

printf '\n'
if [ -n "$FAILED" ]; then
  echo "PUBLISH STOPPED at '$FAILED' (gt $GTV)"
  exit 1
fi
echo "PUBLISHED gt $GTV$([ "$DRY" = yes ] && echo " (dry run — nothing changed)")"
