#!/usr/bin/env bash
# Prove that this repo, on its own, can stand up a working Golden Thread vault.
#
# Runs the exact sequence a new user follows -- install.sh, then vault_init.py fresh
# (what /gt:gt-init does), then create-project -- inside a throwaway HOME so nothing
# on this machine is touched, and asserts that every file the documentation tells
# that user to run or read actually exists, that the hooks answer, and that the
# fresh vault lints clean.
#
# Written 2026-09-05, when a vault scaffolded from the plugin alone turned out to
# have no rollup tool, no inbox, no vault tools and no git repo, because those had
# only ever been seeded into the author's own vault by hand or by install.sh.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export HOME="$TMP/home"; mkdir -p "$HOME/.claude"
fail=0
ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1"; fail=1; }

if bash "$HERE/install.sh" >"$TMP/install.log" 2>&1; then ok "install.sh"; else bad "install.sh"; sed 's/^/      /' "$TMP/install.log"; exit 1; fi
VER=$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.claude/plugins/installed_plugins.json')))['plugins']['gt@golden-thread-plugin'][0]['version'])")
SCRIPTS="$HOME/.claude/plugins/cache/golden-thread-plugin/gt/$VER/scripts"
HOOKS="$HOME/.claude/golden-thread/hooks"
VAULT="$TMP/vault"
[ -f "$SCRIPTS/vault_init.py" ] && ok "plugin $VER in cache" || bad "no scripts in cache for $VER"

if python3 "$SCRIPTS/vault_init.py" fresh --vault "$VAULT" --domain "Selftest" >"$TMP/fresh.log" 2>&1; then ok "vault_init.py fresh"; else bad "vault_init.py fresh"; tail -20 "$TMP/fresh.log"; fi
grep -q '"action": "error"' "$TMP/fresh.log" && { bad "fresh reported an error action"; grep -B2 -A2 '"error"' "$TMP/fresh.log" | sed 's/^/      /'; }
if python3 "$SCRIPTS/vault_init.py" create-project --vault "$VAULT" --name demo-project --title "Demo Project" --domain test --topology local >"$TMP/proj.log" 2>&1; then ok "vault_init.py create-project"; else bad "create-project"; tail -8 "$TMP/proj.log"; fi

for f in CLAUDE.md INBOX.md TASKS.md log.md index.md Projects/README.md Projects/CONVENTIONS.md Projects/PROTOCOL.md \
         Projects/golden-thread/README.md Projects/golden-thread/tools/gt_tasks.py Projects/golden-thread/tools/gt_closeout.py \
         Projects/golden-thread/tools/gt_session.py Projects/golden-thread/tools/safe_write.py Projects/golden-thread/tools/gt_edits.py \
         Projects/golden-thread/core-rules/core_rule_priority_model.md Projects/demo-project/README.md; do
  [ -e "$VAULT/$f" ] && ok "vault has $f" || bad "vault lacks $f"
done
[ "$(python3 -c "import json,os;print(os.path.realpath(json.load(open(os.path.expanduser('~/.claude/vault-config.json'))).get('vault_path','')))")" = "$(python3 -c "import os,sys;print(os.path.realpath(sys.argv[1]))" "$VAULT")" ] \
  && ok "vault-config.json points at the vault" || bad "vault-config.json does not point at the vault"
if command -v git >/dev/null; then
  git -C "$VAULT" rev-parse --git-dir >/dev/null 2>&1 && ok "vault is a git repo" || bad "vault is not a git repo"
  [ "$(git -C "$VAULT" config core.hooksPath 2>/dev/null)" = ".githooks" ] && ok "core.hooksPath = .githooks" || bad "attribution hooks not wired"
fi

python3 "$VAULT/Projects/golden-thread/tools/gt_tasks.py" --vault "$VAULT" >/dev/null 2>&1 && ok "gt_tasks.py regenerates TASKS.md" || bad "gt_tasks.py failed"
grep -q '^## Inbox' "$VAULT/TASKS.md" && grep -q '^## Review' "$VAULT/TASKS.md" && ok "TASKS.md carries Inbox and Review sections" || bad "TASKS.md lacks Inbox/Review"
python3 "$VAULT/Projects/golden-thread/tools/gt_closeout.py" --vault "$VAULT" candidates >/dev/null 2>&1 && ok "gt_closeout.py runs" || bad "gt_closeout.py failed"
python3 "$VAULT/Projects/golden-thread/tools/gt_session.py" register --task selftest >/dev/null 2>&1 && python3 "$VAULT/Projects/golden-thread/tools/gt_session.py" release >/dev/null 2>&1 && ok "gt_session.py register/release" || bad "gt_session.py failed"
python3 - "$VAULT/Projects/golden-thread/tools/safe_write.py" "$TMP" <<'PY' && ok "safe_write.py appends in place" || bad "safe_write.py append"
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("sw", sys.argv[1]); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
p = os.path.join(sys.argv[2], "a.txt"); open(p, "wb").write(b"x\r\n"); m.write(p, "y\n", "a")
sys.exit(0 if open(p, "rb").read() == b"x\r\ny\n" else 1)
PY

echo '{}' | "$HOOKS/inject_core_rules.sh" 2>/dev/null | grep -q 'CORE RULES' && ok "UserPromptSubmit hook injects the Core rules" || bad "inject_core_rules.sh gave no rules"
python3 - <<'PY' | while IFS= read -r cmd; do
import json, os
d = json.load(open(os.path.expanduser("~/.claude/settings.json")))
for e in d.get("hooks", {}).get("SessionStart", []):
    for h in e.get("hooks", []): print(h["command"])
PY
  name=$(printf '%s' "$cmd" | grep -oE 'gt_[a-z_]+\.py'); out=$(echo '{}' | bash -c "$cmd" 2>/dev/null)
  printf '%s' "$out" | grep -q '"systemMessage"' && ok "SessionStart $name emits systemMessage" || bad "SessionStart $name: $out"
done
python3 "$HOOKS/gt_report_card.py" </dev/null >/dev/null 2>&1 && ok "report card runs" || bad "report card failed"

# [source-todo] is a reminder that a NEW project's source.md still has blanks to fill;
# a fresh scaffold is expected to carry it. Every other finding class is a defect.
LINT=$(python3 "$SCRIPTS/gt_lint.py" "$VAULT" 2>&1 | grep '^\[' | grep -vc '^\[source-todo\]' || true)
if [ "$LINT" = "0" ]; then ok "gt_lint: no findings on the fresh vault beyond source-todo"; else bad "gt_lint: $LINT finding(s)"; python3 "$SCRIPTS/gt_lint.py" "$VAULT" 2>&1 | grep -v 'source-todo' | grep -A1 '^\[' | sed 's/^/      /'; fi

if [ "$fail" = 0 ]; then echo "SELFTEST PASSED  (plugin $VER, throwaway home removed)"; else echo "SELFTEST FAILED"; fi
exit $fail
