#!/usr/bin/env python3
"""gt_aggregate.py -- the shared honesty rules for every command that runs other commands.

Not a command. `gt_scan.py` and `gt_allin.py` both import this, because they had the same five
defects and fixing one and forgetting the other is exactly the failure this file prevents.

WHAT AN AGGREGATOR GETS WRONG, measured on 2026-09-16:

1. THE DENOMINATOR WAS THE INSTALLED SET, NOT THE DECLARED SET. A member whose script was
   missing simply vanished from the run, so a partial install printed `1 of 1 member(s) ran`
   and exited 0 -- having never scanned the code or linted the vault -- and then offered to
   push. The aggregator built to stop a skipped check passing for a clean one did exactly that.
   Fix: the denominator is what the tool DECLARES it checks. Absent members are reported.

2. DUPLICATES INFLATED THE HEADLINE. `--only doctor,doctor,doctor,doctor` read `4 of 4`.

3. A CRASHING MEMBER COUNTED AS HAVING RUN. An uncaught Python exception exits 1, which was
   read as "ran, found something", and the traceback was then thrown away because detail only
   printed for members that failed. Fix: exit 1 with no stdout and a stderr traceback is a
   crash, not a finding, and stderr is always kept.

4. NO TIMEOUT. A member that hung hung the sweep forever.

5. MEMBER OUTPUT WAS PRINTED RAW, so a member could forge the aggregator's own summary --
   including an ANSI conceal sequence that hid the real summary line while a fake one stood
   above it. Fix: strip escapes, prefix every line with the member's name, and cap the size.
"""
import os
import re
import subprocess
import sys

CLEAN, FINDINGS, USAGE, NO_ANSWER = 0, 1, 2, 3

DEFAULT_TIMEOUT_S = 300
MAX_OUTPUT_BYTES = 256 * 1024

# C0/C1 control characters and ANSI escape sequences. A member's stdout is untrusted text: it
# reaches a terminal, and it sits directly above the aggregator's own conclusions.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitise(text, member, cap=MAX_OUTPUT_BYTES):
    """-> the member's output, safe to print under the aggregator's own lines."""
    if not text:
        return ""
    truncated = False
    if len(text) > cap:
        text, truncated = text[:cap], True
    out = []
    for line in text.split("\n"):
        # Prefixing is what makes forgery visible: a faked summary line still arrives labelled
        # as something a member said.
        out.append("  [%s] %s" % (member, _ANSI.sub("", line)))
    if truncated:
        out.append("  [%s] ... output truncated at %d bytes" % (member, cap))
    return "\n".join(out)


def looks_like_a_crash(proc):
    """Exit 1 is ambiguous: a leaf uses it for `findings`, python uses it for an uncaught
    exception. Distinguish by what came out, not by the number alone."""
    if proc.returncode != FINDINGS:
        return False
    stderr = proc.stderr or ""
    return ("Traceback (most recent call last)" in stderr) and not (proc.stdout or "").strip()


def run_member(name, cmd, timeout=DEFAULT_TIMEOUT_S):
    """-> a result dict. `ran` is True only when the member demonstrably did its work."""
    if cmd is None:
        return {"member": name, "ran": False, "exit": None, "status": "could-not-run",
                "detail": "not installed", "output": ""}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"member": name, "ran": False, "exit": None, "status": "could-not-run",
                "detail": "timed out after %ds" % timeout, "output": ""}
    except OSError as exc:
        return {"member": name, "ran": False, "exit": None, "status": "could-not-run",
                "detail": str(exc), "output": ""}

    if looks_like_a_crash(proc):
        return {"member": name, "ran": False, "exit": proc.returncode,
                "status": "could-not-run",
                "detail": "crashed: %s" % (proc.stderr or "").strip().split("\n")[-1][:200],
                "output": sanitise(proc.stderr, name)}
    ran = proc.returncode in (CLEAN, FINDINGS)
    return {"member": name, "ran": ran, "exit": proc.returncode,
            "status": ("clean" if proc.returncode == CLEAN else "findings") if ran
                      else "could-not-run",
            # stderr is ALWAYS kept, for members that ran too: a warning on the way to a
            # successful exit is still something the reader needs.
            "detail": (proc.stderr or "").strip()[:400],
            "output": sanitise(proc.stdout, name)}


def resolve_wanted(declared, installed, only):
    """-> (wanted, error). The denominator is what the tool DECLARES, never what it found."""
    if only is not None:
        names = [s.strip() for s in only.split(",") if s.strip()]
        if not names:
            return None, "--only needs at least one member name"
        unknown = [n for n in names if n not in declared]
        if unknown:
            return None, "unknown member(s): %s" % ", ".join(unknown)
        return list(dict.fromkeys(names)), None          # de-duplicated, order kept
    return sorted(declared), None


def summarise(results, wanted, out=sys.stdout):
    """Print the honest headline. Returns the exit code."""
    ran = [r for r in results if r["ran"]]
    failed = [r for r in results if not r["ran"]]
    found = [r for r in ran if r["status"] == "findings"]
    print("%d of %d member(s) ran; %d reported findings"
          % (len(ran), len(wanted), len(found)), file=out)
    for r in failed:
        print("  COULD NOT RUN  %-10s exit=%s  %s"
              % (r["member"], r["exit"], r["detail"][:160]), file=out)
    if failed:
        print("A member that could not run is NOT a pass: what it checks is unknown here.",
              file=out)
    return NO_ANSWER if failed else (FINDINGS if found else CLEAN)
