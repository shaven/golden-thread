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
from pathlib import Path

CONFIG = Path.home() / ".claude" / "vault-config.json"
MODEL_FILE = "core_rule_priority_model.md"   # the marker that identifies a real core-rules dir


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
    for key in ("level", "enforcement", "imperative", "inject"):
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
