#!/usr/bin/env python3
"""gt_use_record -- the write side of use recording (0.21.0), kept apart on purpose.

The guards (guard_vault_writes, guard_session_claims, guard_test_before_commit) record that a
decision named a Core rule. They import ONLY this file, so gt_integrity.hook_deps -- which follows
static imports to decide which files a hook is checked against -- gives each guard exactly what it
runs: this file and gt_paths, not gt_use's reporting side (which reads settings through gt_settings
and, through it, gt_unlock_policy). gt_use re-exports these names and keeps its own imports static.
Stdlib only; every function fails open.
"""
import datetime as dt
import json
import os
import sys
from pathlib import Path

USAGE_DIR = "usage"
USE_LOG = "use.jsonl"
KINDS = ("cited", "acted-on", "answered-from", "enforced")


def record_use(vault, page, kind, by=None, session=None, today=None):
    """Append one use event. Fails open (returns False) -- callers include guards."""
    try:
        # GT_USE_LOG=off: a check exercising the guards (gt_doctor's probes, the release gate)
        # makes decisions that are not uses of the rule, so they are not recorded.
        if kind not in KINDS or not page or os.environ.get("GT_USE_LOG") == "off":
            return False
        usage = Path(vault) / USAGE_DIR
        usage.mkdir(exist_ok=True)
        ignore = usage / ".gitignore"
        if not ignore.exists():
            with open(ignore, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("# local access log (gt read/use logs): never committed\n*\n")
        row = {"page": page, "kind": kind, "date": (today or dt.date.today()).isoformat()}
        if by:
            row["by"] = str(by)[:200]
        if session:
            row["session"] = str(session)[:8]
        with open(usage / USE_LOG, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row) + "\n")
        return True
    except Exception:                                            # noqa: BLE001
        return False


def rule_page(vault, rule):
    """The vault-relative path of Core rule `rule` (e.g. core_explicit_vault_target), or None."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from gt_paths import find_core_rules                     # noqa: PLC0415
        cr = find_core_rules(Path(vault), record=False)
        if cr and (Path(cr) / ("%s.md" % rule)).is_file():
            return (Path(cr) / ("%s.md" % rule)).resolve().relative_to(
                Path(vault).resolve()).as_posix()
    except Exception:                                            # noqa: BLE001
        pass
    return None


def record_enforced(rule, by, session=None):
    """For guards: a decision named Core rule `rule`. Never raises, never prints."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from gt_paths import find_vault                          # noqa: PLC0415
        vault = find_vault()
        if not vault:
            return False
        page = rule_page(vault, rule) or "core-rules/%s.md" % rule
        return record_use(vault, page, "enforced", by, session)
    except Exception:                                            # noqa: BLE001
        return False
