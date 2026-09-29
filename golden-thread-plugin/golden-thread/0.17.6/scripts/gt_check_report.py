#!/usr/bin/env python3
"""gt_check_report.py -- file a check's result into the vault, so a gate leaves a record.

    gt_check_report.py record --vault V --check secrets --verdict clean|findings|cannot-run
                              [--count N] [--scope "..."] [--ref HEAD] [--detail TEXT]
    gt_check_report.py show   --vault V [--check secrets] [--json]

WHY A RECORD AT ALL. A gate that blocks tells you about the commit in front of you and
nothing about the week. Three questions need history and cannot be answered from an exit
code: is this check actually running, is it getting noisier or quieter, and did anyone
ever look at the thing it flagged. The owner asked for exactly this -- "file reports of the
results for different check ins ... they certainly should be captured in the vault".

WHAT IS AND IS NOT RECORDED. A verdict, a count, a scope and a ref. **Never a finding's
content.** gt_secrets exists because a credential must not reach a log, and a vault file is
a log that gets committed and pushed; a report naming `path:line` for a credential would
republish the location of every secret the scanner found, in a file that outlives the fix.
So the report carries HOW MANY and WHAT KIND, never WHERE. That asymmetry is the reason
this is a separate tool from the scanners rather than a flag on each of them.

NEVER BLOCKS, NEVER COMMITS. Recording is bookkeeping, so every failure path here exits 0
with a note -- the opposite of the pre-commit gate's posture, and for the same reason the
attribution hooks fail open: losing a record is recoverable, refusing work is not. It also
never commits; the next session commits the report with its own work, exactly as
gt_lint_weekly.py does.

CLAIMS ARE HONOURED. If a live session holds a claim on the report file, the write is
SKIPPED and said out loud rather than forced (Core rule 1).

Exit: 0 always for `record`. `show`: 0 report(s) found | 1 none yet | 2 usage.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gt_paths                                          # noqa: E402

KIND = "checks"
VERDICTS = ("clean", "findings", "cannot-run")
# `cannot-run` is a first-class verdict, not an error to swallow. A check that could not run
# is the state this whole release keeps finding mislabelled as a pass, and a history that
# records it as "clean" would launder exactly that.


def now():
    return datetime.datetime.now().astimezone()


def claim_holder(vault: Path, rel: str):
    """-> the session id holding a claim on `rel`, or None.

    Read directly from the session files rather than by shelling out to gt_session.py:
    this tool must work when the vault's tools folder is absent, and a bookkeeping write
    must not fail because a helper is missing.
    """
    sessions = vault / "Projects" / "golden-thread" / "sessions"
    if not sessions.is_dir():
        return None
    for f in sorted(sessions.glob("*.md")):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "status: active" not in text:
            continue
        if rel in text or ("`%s`" % rel) in text:
            return f.stem.split("_")[0]
    return None


def line_for(check: str, verdict: str, count, scope, ref, detail) -> str:
    stamp = now().strftime("%Y-%m-%d %H:%M %Z")
    parts = ["- %s  **%s**  %s" % (stamp, check, verdict)]
    if count is not None:
        parts.append("%d finding(s)" % count)
    if scope:
        parts.append("scope: %s" % scope)
    if ref:
        parts.append("ref: %s" % ref)
    if detail:
        parts.append(detail)
    return " · ".join(parts)


def record(args) -> int:
    vault = Path(args.vault).expanduser()
    if not vault.is_dir():
        # State the verdict anyway. Every path that cannot file the record must still put
        # it in front of whoever is watching, or a vault-less machine turns a `findings`
        # result into silence -- which is the failure this tool exists to prevent, arriving
        # through the tool itself.
        print("gt-check-report: no vault at %s — nothing recorded, result was: %s %s"
              % (vault, args.check, args.verdict), file=sys.stderr)
        return 0
    out_dir = gt_paths.report_dir(vault, gt_paths.read_config(), kind=KIND)
    target = out_dir / ("%s.md" % args.check)
    rel = os.path.relpath(target, vault)

    holder = claim_holder(vault, rel)
    if holder:
        print("gt-check-report: %s is claimed by session %s — NOT written, result was: "
              "%s %s" % (rel, holder, args.check, args.verdict), file=sys.stderr)
        return 0

    entry = line_for(args.check, args.verdict, args.count, args.scope, args.ref, args.detail)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            header = ("# %s — check history\n\n"
                      "Newest last. A verdict, a count and a scope; **never a finding's "
                      "content**, because this file is committed and pushed and a "
                      "credential's location must not be.\n\n"
                      "Written by `gt_check_report.py`. `cannot-run` means the check did "
                      "not happen — it is not a pass.\n\n" % args.check)
            target.write_text(header, encoding="utf-8")
        with target.open("a", encoding="utf-8") as fh:
            fh.write(entry + "\n")
    except OSError as exc:
        print("gt-check-report: could not write %s (%s) — result was: %s %s"
              % (rel, exc.__class__.__name__, args.check, args.verdict), file=sys.stderr)
        return 0

    print("gt-check-report: recorded in %s" % rel)
    return 0


def show(args) -> int:
    vault = Path(args.vault).expanduser()
    out_dir = gt_paths.report_dir(vault, gt_paths.read_config(), kind=KIND)
    files = sorted(out_dir.glob("*.md")) if out_dir.is_dir() else []
    if args.check:
        files = [f for f in files if f.stem == args.check]
    if not files:
        print("gt-check-report: no check reports yet in %s"
              % os.path.relpath(out_dir, vault) if vault.is_dir() else out_dir)
        return 1
    if args.json:
        print(json.dumps({"dir": str(out_dir),
                          "checks": {f.stem: f.read_text(encoding="utf-8").splitlines()[-1]
                                     for f in files}}, indent=2))
        return 0
    for f in files:
        lines = [l for l in f.read_text(encoding="utf-8").splitlines() if l.startswith("- ")]
        print("%-12s %d run(s), last: %s"
              % (f.stem, len(lines), lines[-1] if lines else "(none)"))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="file a check's result into the vault")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record")
    r.add_argument("--vault", required=True)
    r.add_argument("--check", required=True)
    r.add_argument("--verdict", required=True, choices=VERDICTS)
    r.add_argument("--count", type=int)
    r.add_argument("--scope")
    r.add_argument("--ref")
    r.add_argument("--detail")

    s = sub.add_parser("show")
    s.add_argument("--vault", required=True)
    s.add_argument("--check")
    s.add_argument("--json", action="store_true")

    a = ap.parse_args(argv)
    return record(a) if a.cmd == "record" else show(a)


if __name__ == "__main__":
    sys.exit(main())
