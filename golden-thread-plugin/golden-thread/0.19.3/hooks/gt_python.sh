# gt_python.sh -- SOURCED by every hook wrapper in this directory, never run on its own.
#
# WHY: on native Windows Claude Code runs hook commands through Git Bash, and there
# `python3` is %LOCALAPPDATA%\Microsoft\WindowsApps\python3.exe -- the Microsoft Store
# stub, which prints "Python was not found" and exits 9009. Every wrapper fails open by
# design, so each hook would have done NOTHING, silently: no Core rules injected, no
# guard run, and a session that reads exactly like a healthy one.
#
# WHAT: on Windows only, `python3` becomes the interpreter install.sh resolved and checked,
# in UTF-8 mode, with \r stripped from its stdout (native Windows Python writes CRLF). On
# macOS and Linux this file does nothing at all -- $OSTYPE is a shell variable, so the
# check costs no process.
#
# WHERE THE INTERPRETER COMES FROM, first hit wins:
#   1. $GT_PYTHON, when the caller already resolved one (install.sh exports it);
#   2. ../python beside this hooks dir -- one line install.sh writes. Found relative to the
#      HOOKS DIR, not $HOME: the post-install gate runs these hooks under a throwaway HOME;
#   3. the first python3 / python on PATH that is not under ...\WindowsApps\ (the Store
#      stub, or a Store Python, which does not count). `type -P` is a PATH search inside
#      bash, so this too costs no process.
# None of them: `python3` is left as it is, which is exactly the behaviour before.
#
# `set -u` here changes nothing: every wrapper that sources this file has already set it.
set -u
case "${OSTYPE:-}" in
  msys*|cygwin*)
    if [ -z "${GT_PYTHON:-}" ] && [ -r "${HERE:-.}/../python" ]; then
      IFS= read -r GT_PYTHON < "${HERE:-.}/../python" || true
      GT_PYTHON="${GT_PYTHON%$'\r'}"
    fi
    if [ -z "${GT_PYTHON:-}" ]; then
      _gt_ifs="$IFS"; IFS=$'\n'
      for _gt_c in $(type -aP python3 python 2>/dev/null); do
        case "$_gt_c" in */[Ww]indows[Aa]pps/*) continue ;; esac
        GT_PYTHON="$_gt_c"; break
      done
      IFS="$_gt_ifs"; unset _gt_c _gt_ifs
    fi
    if [ -n "${GT_PYTHON:-}" ]; then
      # UTF-8 mode: otherwise a piped Windows Python encodes as cp1252 and dies on "⚠".
      export PYTHONUTF8=1
      # An argument holding an apostrophe ("/c/.../Sam's Projects/...") is one MSYS does not
      # convert to a Windows path for a native program (0.19.3); cygpath converts exactly those.
      python3() {
        local _a; local -a _v=()
        for _a in "$@"; do
          case "$_a" in /*\'*) _a=$(cygpath -m "$_a" 2>/dev/null || printf '%s' "$_a") ;; esac
          _v+=("$_a")
        done
        "$GT_PYTHON" ${_v[@]+"${_v[@]}"} | tr -d '\r'; return "${PIPESTATUS[0]}"
      }
    fi
    ;;
esac
