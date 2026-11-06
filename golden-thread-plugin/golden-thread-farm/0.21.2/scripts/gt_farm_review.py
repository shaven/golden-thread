#!/usr/bin/env python3
"""gt_farm_review.py -- a local engine's second opinion on a change (gt 0.21.4, gt-farm 0.21.1).

    gt_farm_review.py review (--repo PATH | --diff-file FILE) [--staged] [--if-enabled]
                      [--service NAME | --pool NAME] [--services FILE] [--vault DIR]
                      [--max-chars N] [--max-tokens N] [--timeout S] [--no-ledger] [--json]

WHAT IT DOES. It sends the repository's STAGED diff (`git diff --cached`; unstaged work is never read), cut to
fit the engine, to a calibrated `local: yes` engine with a FINDINGS/GAPS review prompt, and prints what comes
back under "local review". It is ADVISORY: the output says so, the exit code is 0 whatever the engine did,
and gt_allin_commit.py prints it and carries on. It exists because (research 2026-10-15) a local engine's
review of new security code gave 3 real defects in 10 claims -- none of the 3 caught by 1,342 passing
tests -- and 7 of the 10 claims were wrong or already known, so every finding needs Claude's or a person's
check. The output therefore calls the findings UNVERIFIED suggestions, never results.

WHEN IT RUNS (`--if-enabled`, what gt_allin_commit.py passes). The setting `commit_local_review` is off by
default and on when set so; unset, it is on exactly when `farm_first` is auto and a local engine exists. An
explicit `off` stays off under auto. `farm_first` off ends it: no automatic farm work. Without
`--if-enabled` the command reviews when run by hand, subject to everything below.

WHAT KEEPS IT SAFE.
  * A calibrated engine only. The engine must have a calibration on record for its current MODEL with a
    code-review verdict of can-take or review-only (gt_farm_calibration.automatic_ok); otherwise the review is
    skipped, and the line says why and which command measures the engine. review-only adds a line saying
    Claude must check every finding.
  * Local engines only (`local: yes`). The diff does not leave your network.
  * The diff is scanned first. The text that would be sent goes through gt core's intake scan (credentials,
    prompt injection, unsafe-code patterns); ANY hit skips the review, and the line names the KIND of hit, never
    the content. A scan that could not run also skips it.
  * Bounded. At most --max-chars (default 24,000) and never more than the engine's usable context; files are
    taken whole, in order, until the budget is spent, and the files left out are named.
  * Nothing is written: no vault file, no repo file. The diff goes to a temporary file for the scan and the
    file is removed.

LEDGER. One `farm` line labelled purpose `review`: the engine's tokens, `kept_out_tokens` (the diff Claude did
not have to read and the findings it did not have to write) and `main_context_tokens` (what this command
printed into your session, estimated). State measured when the engine reported usage, else estimated.

Exit: 0 always for a review (skipped or done) | 2 usage.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
sys.dont_write_bytecode = True
import gt_farm_calibration as cal                                    # noqa: E402
import gt_farm_probes as probes                                      # noqa: E402
import gt_farm_send as fs                                            # noqa: E402

MAX_DIFF_CHARS = 24000
WIDTH = 96
JOB = ("Review the staged change below as a second reader. Report only concrete defects you can point to in the "
       "diff: wrong logic, a missing check, an unhandled error, an inconsistency between two parts of it. Do not "
       "praise, restate or summarise the change. If you find nothing, return an empty findings list and say in "
       "gaps what you could not judge.")
EVIDENCE = ("In a trial a local engine's review gave 3 real defects in 10 claims, none caught by the tests, and "
            "7 claims needed Claude to dismiss them.")


def wrap(text, indent=""):
    return textwrap.fill(" ".join(str(text).split()), width=WIDTH, initial_indent=indent, subsequent_indent=indent + "  ")


def effective(first=None, setting=None, have_engine=False):
    """Is the review on? commit_local_review on -> yes; off -> no; unset -> yes only under farm_first auto
    with a local engine. farm_first off -> no, whatever the setting says."""
    first = first or cal.farm_first()
    setting = setting if setting is not None else cal.commit_review_setting()
    if first == "off":
        return False
    if setting is not None:
        return setting == "on"
    return first == "auto" and have_engine


def git_diff(repo):
    p = subprocess.run(["git", "-C", repo, "diff", "--cached", "--no-color", "--no-ext-diff", "-U3"],
                       capture_output=True, text=True, errors="replace")
    return p.stdout if p.returncode == 0 else None


def split_files(diff):
    """-> [(path, text)] one entry per file of a unified diff."""
    out, cur, name = [], [], None
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if cur:
                out.append((name, "".join(cur)))
            cur, name = [line], line.split(" b/", 1)[-1].strip()
        else:
            cur.append(line)
    if cur:
        out.append((name or "(diff)", "".join(cur)))
    return out


def bound(files, limit):
    """-> (text, kept names, left-out names): whole files, in order, until `limit` characters are spent."""
    kept, left, used, parts = [], [], 0, []
    for name, text in files:
        if used + len(text) <= limit:
            parts.append(text)
            kept.append(name)
            used += len(text)
        else:
            left.append(name)
    return "".join(parts), kept, left


def scan_text(text, core_dir):
    """-> (ok, why): gt core's intake scan over the text that would be sent. A hit names its KIND."""
    tool = os.path.join(core_dir or "", "gt_intake_scan.py")
    if not core_dir or not os.path.isfile(tool):
        return False, "the intake scan (gt core) was not found, so nothing can be sent safely"
    fd, path = tempfile.mkstemp(suffix=".diff", prefix="gt-review-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        p = subprocess.run([sys.executable, tool, path, "--json"], capture_output=True, text=True,
                           timeout=300, stdin=subprocess.DEVNULL)
        if p.returncode == 0:
            return True, ""
        try:
            kinds = sorted({f["kind"] for u in json.loads(p.stdout)["units"] for f in u["findings"]})
        except (ValueError, KeyError, TypeError):
            kinds = []
        return False, ("the intake scan found %s in the diff" % ", ".join(kinds)) if kinds else \
            "the intake scan could not complete (exit %d)" % p.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "the intake scan could not run (%s)" % type(exc).__name__
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def skipped(a, why, **extra):
    msg = "local review skipped: %s" % why
    if a.json:
        print(json.dumps(dict(extra, ran=False, why=why)))
    else:
        print(msg)
    return 0


def cmd_review(a):
    vault = fs.vault_path(a.vault)
    engines = cal.table_engines(a.services, vault, service=a.service, pool=a.pool, local_only=False)
    if a.if_enabled and not effective(have_engine=any(e["local"] for e in engines)):
        return 0
    if not engines:
        return skipped(a, "no local engine in the service table (python3 gt_farm_engine.py install NAME ...)")
    local = [e for e in engines if e["local"]]
    if not local:
        return skipped(a, "the engine is not `local: yes`; the diff goes to local engines only")
    if cal.farm_first() == "off":
        return skipped(a, "farm_first is off")
    if a.diff_file:
        try:
            with open(a.diff_file, encoding="utf-8", errors="replace") as fh:
                diff = fh.read()
        except OSError as exc:
            print("cannot read %s: %s" % (a.diff_file, exc), file=sys.stderr)
            return 2
    else:
        diff = git_diff(os.path.abspath(a.repo))
        if diff is None:
            return skipped(a, "could not read the staged diff of %s" % a.repo)
    if not diff.strip():
        return skipped(a, "nothing is staged")
    chosen, info, last = None, None, None
    for e in local:
        i = cal.probe_engine(e)
        ok = cal.automatic_ok(i, "review", vault)
        last = ok
        if ok["ok"]:
            chosen, info, verdict = e, i, ok
            break
    if chosen is None:
        return skipped(a, last["why"] if last else "no engine answered")
    limit = min(a.max_chars, max(2000, cal.usable_prompt_chars(info) - len(JOB) - 1500))
    text, kept, left = bound(split_files(diff), limit)
    if not text:
        return skipped(a, "the first file's diff alone is larger than the engine can take (%d characters)" % limit)
    ok, why = scan_text(text, cal.core_scripts())
    if not ok:
        return skipped(a, why)
    probe = probes._probe("review-live", "review", JOB, {"diff": text}, probes.REVIEW_FINDING_FIELDS)
    r = cal.ask_engine(chosen, probe["prompt"], probe["schema"], a.max_tokens, a.timeout)
    if not r.get("ok"):
        return skipped(a, "the engine %s (%s)" % ("did not answer" if r["reason"] == "engine" else
                                                  "returned an answer that did not match the schema",
                                                  (r.get("error") or "; ".join(r.get("problems") or []))[:160]))
    res = r["result"]
    lines = ["local review (advisory) -- %s, %s %s, calibrated %s, mode %s" % (
        chosen["name"], info.get("model_id"), info.get("quant") or "",
        (cal.status(info, vault)["record"] or {}).get("date"), verdict["mode"]),
        wrap("These are UNVERIFIED suggestions from a local model, not results. %s Check each one before acting "
             "on it. This never blocks the commit." % EVIDENCE)]
    if verdict["mode"] == "check":
        lines.append(wrap("This engine is review-only for code review: Claude must check every finding before "
                          "anything is done about it."))
    findings = [f for f in res.get("findings") or [] if isinstance(f, dict)]
    if not findings:
        lines.append("  no defects reported.")
    for n, f in enumerate(findings, 1):
        lines.append(wrap("%d. [%s] %s (%s)" % (n, f.get("confidence", "?"), f.get("claim", ""),
                                                f.get("source") or "NONE"), "  "))
    for g in res.get("gaps") or []:
        lines.append(wrap("gap: %s" % g, "  "))
    if res.get("needs_review"):
        lines.append(wrap("the engine asked for a closer look: %s" % (res.get("review_reason") or "no reason given"), "  "))
    lines.append("  reviewed %d of %d file(s)%s." % (len(kept), len(kept) + len(left),
                 (" -- not reviewed (too large for the engine): " + ", ".join(left[:8])
                  + (" ..." if len(left) > 8 else "")) if left else ""))
    out = "\n".join(lines)
    if not a.no_ledger:
        cal.ledger(chosen, "review", "review", r["prompt_tokens"], r["completion_tokens"], r["wall"],
                   r["estimated"], model=r.get("model") or "",
                   kept_out_tokens=r["prompt_tokens"] + r["completion_tokens"],
                   main_context_tokens=len(out) // 4)
    if a.json:
        print(json.dumps({"ran": True, "engine": chosen["name"], "mode": verdict["mode"], "findings": findings,
                          "gaps": res.get("gaps") or [], "files_reviewed": kept, "files_left_out": left,
                          "text": out}))
    else:
        print(out)
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="gt_farm_review.py", description="A local engine's second opinion on a change")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("review", help="review the staged diff (advisory)")
    src = r.add_mutually_exclusive_group(required=True)
    src.add_argument("--repo", help="a git repository: its staged diff")
    src.add_argument("--diff-file", help="a unified diff in a file")
    r.add_argument("--staged", action="store_true", help="(the default for --repo: the staged diff)")
    r.add_argument("--if-enabled", action="store_true", help="do nothing unless commit_local_review is on "
                   "(or unset under farm_first auto with a local engine)")
    r.add_argument("--service")
    r.add_argument("--pool")
    r.add_argument("--services")
    r.add_argument("--vault")
    r.add_argument("--max-chars", type=int, default=MAX_DIFF_CHARS)
    r.add_argument("--max-tokens", type=int, default=3000)
    r.add_argument("--timeout", type=float, default=900.0)
    r.add_argument("--no-ledger", action="store_true")
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=cmd_review)
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    if not a.cmd:
        build_parser().print_help()
        return 2
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
