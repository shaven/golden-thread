#!/usr/bin/env python3
"""Locate the vault and its core-rules folder WITHOUT hardcoding any project slug.

Projects get renamed, merged and removed. Anything that pins a project path into a
config file or a script becomes a breaking change the next time that happens — and a
silent one, because a hook pointing at a missing file just stops firing.

Resolution order, most explicit first, each step self-healing:

  vault:       $GT_VAULT  ->  ~/.claude/vault-config.json:vault_path
  core-rules:  vault-config.json:core_rules_path (relative to vault)
               -> search the vault for a dir named 'core-rules' holding the model file
               -> None

The search fallback is what makes a rename survivable: if the recorded path is stale,
the folder is found anyway and the caller can re-record it.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from pathlib import Path

CONFIG = Path.home() / ".claude" / "vault-config.json"
MODEL_FILE = "core_rule_priority_model.md"   # the marker that identifies a real core-rules dir

# Where a NEW vault puts its Core rules, and where the 0.17.0 migration moves them to:
# the vault ROOT. Core rules govern every project in the vault, including projects with
# nothing to do with golden-thread, so filing them under one project asserted an
# ownership that was never real and buried the vault's most important files three levels
# down.
#
# Read these constants; do not spell the path. That is this module's entire purpose (see
# the docstring above), and it was being bypassed by four scripts that hardcoded
# "Projects/golden-thread/core-rules" -- the precise failure the docstring warns about.
CORE_RULES_DEFAULT = "core-rules"
# Where vaults seeded before 0.17.0 keep them. Recognised, so an un-migrated vault keeps
# working; never written to by anything shipped.
CORE_RULES_LEGACY = "Projects/golden-thread/core-rules"


def default_core_rules(vault: Path) -> Path:
    """The canonical location for a vault's Core rules. Use when CREATING them."""
    return Path(vault) / CORE_RULES_DEFAULT


def legacy_core_rules(vault: Path) -> Path:
    """The pre-0.17.0 location. Use only to DETECT a vault that has not migrated."""
    return Path(vault) / CORE_RULES_LEGACY


def read_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_config(data: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def find_vault() -> Path | None:
    env = os.environ.get("GT_VAULT")
    if env and Path(env).is_dir():
        return Path(env)
    p = read_config().get("vault_path")
    if p and Path(p).is_dir():
        return Path(p)
    return None


def find_core_rules(vault: Path | None = None, record: bool = False) -> Path | None:
    """Locate core-rules/. If found somewhere other than the recorded path and
    record=True, update vault-config.json so the next lookup is direct."""
    vault = vault or find_vault()
    if vault is None:
        return None

    cfg = read_config()
    rel = cfg.get("core_rules_path")
    if rel:
        cand = vault / rel
        if (cand / MODEL_FILE).is_file():
            return cand

    # Self-heal: the recorded path is stale or absent. Find it by its marker file.
    for cand in sorted(vault.rglob("core-rules")):
        if cand.is_dir() and (cand / MODEL_FILE).is_file():
            if record:
                cfg["core_rules_path"] = str(cand.relative_to(vault))
                cfg.setdefault("vault_path", str(vault))
                write_config(cfg)
            return cand
    return None


def core_rule_files(core_dir: Path | None = None) -> list[Path]:
    core_dir = core_dir or find_core_rules()
    if core_dir is None:
        return []
    return sorted(p for p in core_dir.glob("core_*.md") if p.is_file())


def parse_rule(path: Path) -> dict:
    """Extract level / enforcement / imperative from a rule file.

    Also carries `gated_by` / `budget_from` when present -- see the loop below.

    The imperative is taken from an explicit `imperative:` frontmatter key if present,
    otherwise the first **bolded** statement in the body — which is the convention the
    existing rules already follow. Reading it from the file is the point: the .md is
    the single source of truth, so editing a rule changes what the hook injects.
    """
    import re
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    out = {"name": path.stem, "path": path}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    fm, body = (m.group(1), m.group(2)) if m else ("", text)
    # `gated_by` names a gt_settings setting that can switch the rule off, and
    # `budget_from` one whose value is appended to the injected line. Both are read
    # here so the rule FILE stays the source of truth: the hook learns which setting
    # governs a rule from the rule, never from a name hardcoded in the script.
    for key in ("level", "enforcement", "imperative", "inject", "gated_by", "budget_from"):
        km = re.search(rf"^\s*{key}\s*:\s*(.+)$", fm, re.M)
        if km:
            out[key] = km.group(1).strip().strip("\"'")
    if "imperative" not in out:
        bm = re.search(r"\*\*(.+?)\*\*", body, re.S)
        if bm:
            out["imperative"] = " ".join(bm.group(1).split())
    return out


if __name__ == "__main__":
    v = find_vault()
    c = find_core_rules(v, record=True)
    print(json.dumps({
        "vault": str(v) if v else None,
        "core_rules": str(c) if c else None,
        "rules": [parse_rule(p).get("name") for p in core_rule_files(c)],
    }, indent=2))


# ---------------------------------------------------------------------------- report dirs
# Where a check's report goes, with the same three-step resolution gt_lint_weekly.py has
# used since 0.13.0: an explicit config key wins; else the pre-0.13.0 folder if it already
# exists, because an upgrade must never silently move a report to a folder the owner has
# never seen (owner requirement, 2026-09-14); else the modern default.
REPORT_DIR_DEFAULT = Path(".gt")
REPORT_DIR_LEGACY = Path("Projects") / "golden-thread"


def report_dir(vault: Path, config: dict | None = None, kind: str = "lint") -> Path:
    """-> the directory `kind`'s reports belong in, absolute.

    `kind` is a subfolder name ("lint", "checks"), so every check reports into the same
    tree rather than each inventing a location.

    gt_lint_weekly.py DUPLICATES this resolution and cannot import it: it is installed to
    ~/.claude/golden-thread/hooks/ and runs standalone from there, with no path to the
    release's scripts/. The duplication is therefore load-bearing rather than careless --
    and because today's other bug was a duplicated fixture where only one copy got fixed,
    test_gt_paths pins the two implementations as equivalent across all three branches.
    """
    vault = Path(vault)
    config = config or {}
    configured = config.get("lint_report_dir") if kind == "lint" else None
    if isinstance(configured, str) and configured.strip():
        # A relative value resolves inside the vault; an absolute one wins the join.
        return vault / os.path.expanduser(configured.strip())
    legacy = vault / REPORT_DIR_LEGACY / kind
    if legacy.is_dir():
        return legacy
    return vault / REPORT_DIR_DEFAULT / kind


# ---------------------------------------------------------------------------- machine id
# WHICH MACHINE wrote a session file, a worker declaration or a claim -- asked by the claim
# guard (guard_session_claims.py) and gt_workers.py, and answered identically by the vault
# tool gt_session.py, which carries its own copy because a vault tool cannot import from
# the plugin (the same load-bearing duplication as report_dir above).
#
# Until 0.17.2 the answer was `recorded host == socket.gethostname()`. A hostname is not an
# identity: on 2026-09-28 the owner moved a laptop to a wired network, DHCP handed it a
# different name (laptop.office.lan -> printer-room.lan), and every claim written under
# the old name stopped being recognised as this machine's. An unrecognised claim is not an
# error -- its pid simply stops being judged and it lapses on the heartbeat clock -- so Core
# rule 1 was disarmed by a DHCP lease with nothing announced. Two tests caught it only
# because the name changed between their import and their subprocess.
#
# So identity is a uuid4 written ONCE to ~/.claude/golden-thread/machine-id, and the
# hostname is kept only as a human-readable label. Per HOME, deliberately: it lives beside
# workers.jsonl, which is machine-local by the same design. (Consequence worth knowing: two
# OS logins on one Mac get two ids, so each treats the other's sessions as another
# machine's -- liveness by heartbeat, the cross-machine rule -- where before they shared a
# hostname and judged each other's pids.)
MACHINE_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def machine_id_path() -> Path:
    """~/.claude/golden-thread/machine-id, with HOME read NOW, not at import -- a test
    (or a tool) that repoints HOME must get the id under the new one."""
    return Path(os.path.expanduser("~")) / ".claude" / "golden-thread" / "machine-id"


def machine_id(create: bool = True) -> tuple[str | None, str | None]:
    """-> (this machine's id, None), or (None, why it is unavailable).

    Created on first use, atomically and create-if-absent: the uuid is written to a
    private temp file and hard-linked into place, and a link cannot replace an existing
    name -- so two processes racing on a brand-new machine both end up reading the ONE
    winner, never two different ids. Never raises: an unreadable or unwritable HOME gives
    (None, reason), and callers then fall back to the hostname label AND say so, because
    a fallback nobody hears about is the failure this replaces.
    """
    p = machine_id_path()
    for attempt in (1, 2):
        try:
            raw = p.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            raw = None
        except OSError as e:
            return None, f"cannot read {p}: {e.strerror or e}"
        if raw is not None:
            if MACHINE_ID_RE.match(raw):
                return raw, None
            # Never overwritten: it may be damaged, but it may also be an id something
            # else on this machine already relies on. Replacing it would silently make
            # every existing claim "another machine's".
            return None, f"{p} does not hold a machine id (found {raw[:40]!r})"
        if not create or attempt == 2:
            return None, f"{p} does not exist"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".machine-id.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(str(uuid.uuid4()) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.chmod(tmp, 0o644)
                try:
                    os.link(tmp, str(p))
                except FileExistsError:
                    pass                        # another process won the race: read theirs
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        except OSError as e:
            return None, f"cannot create {p}: {e.strerror or e}"
    return None, f"{p} could not be created"  # pragma: no cover -- loop always returns


def same_machine(recorded_machine: str, recorded_host: str, mine: str | None,
                 hostname: str) -> tuple[bool, str | None]:
    """Was a record written on THIS machine? -> (verdict, notice or None).

    The rule, most certain evidence first:
      * both ids known          -> the ids decide, whatever the labels say. Equal ids
                                   with different labels is a RENAME and is reported;
                                   different ids is another machine even under one name.
      * record has an id, we do not -> our id is unreadable: fall back to the label (the
                                   caller has already announced why).
      * record has NO id        -> written by gt <= 0.17.1. Migration, not a flag day: the
                                   label still counts when it matches, exactly as before.
                                   When it does not, nothing can prove whose it is, so it
                                   is NOT claimed as ours -- its pid is not judged and the
                                   heartbeat decides, as it always did -- but that is said,
                                   never left silent. Such a file migrates itself the next
                                   time its own session writes it.
    """
    recorded_machine = (recorded_machine or "").strip()
    recorded_host = (recorded_host or "").strip()
    if recorded_machine and mine:
        if recorded_machine != mine:
            return False, None
        if recorded_host and recorded_host != hostname:
            return True, (f"registered on host '{recorded_host}', and this machine now calls "
                          f"itself '{hostname}' -- same machine id, so its claims still hold")
        return True, None
    if recorded_host == hostname:
        return True, None
    if recorded_machine:
        return False, None
    return False, (f"was written by an older gt with no machine id, on host "
                   f"'{recorded_host or '?'}', which is not this machine's current name "
                   f"'{hostname}' -- its pid cannot be judged here, so only its heartbeat "
                   f"keeps its claims live")
