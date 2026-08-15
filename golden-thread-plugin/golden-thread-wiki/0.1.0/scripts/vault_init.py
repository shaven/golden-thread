#!/usr/bin/env python3
"""vault_init.py - deterministic vault initialization for the gt-wiki and
project-flow plugins.

Generates the folder structure, seeds the convention files from templates,
and writes the shared vault config. Idempotent: anything that already
exists is skipped and reported, never overwritten.

Usage:
  vault_init.py wiki --vault PATH [--domain "your domain"] [--config PATH]
  vault_init.py flow --vault PATH [--config PATH]
  vault_init.py all  --vault PATH [--domain "your domain"] [--config PATH]

  --domain   fills <YOUR DOMAIN HERE> in the wiki CLAUDE.md (wiki/all mode).
             If omitted, the placeholder stays and the report says so.
  --config   vault config location (default: ~/.claude/vault-config.json)
  --json     machine-readable report

Exit codes: 0 = success (created or already present), 2 = usage error,
3 = conflict (config exists with a DIFFERENT vault_path - never silently
rewritten; the report shows both paths so a human can decide).
"""
import sys, os, json, argparse

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

INDEX_STUB = """# Index

Every Knowledge page gets one line here: a wiki link plus a one-line
summary. Lookups start at this file.
"""

LOG_STUB = """# Log

Append-only. One entry per operation, newest at the bottom.
Format: `## [YYYY-MM-DD] operation | Title` followed by a bullet summary.
Operations (closed vocabulary): ingest, query, lint, refresh, graduate, retire, relocate.
"""

PROJECTS_README_STUB = """# Projects

Master index. One line per project: a wiki link to its README and a
one-line description, grouped however suits you.
"""

def report_line(results, action, path, note=""):
    results.append({"action": action, "path": path, "note": note})

def ensure_dir(path, results):
    if os.path.isdir(path):
        report_line(results, "skipped", path, "directory exists")
    else:
        os.makedirs(path)
        report_line(results, "created", path, "directory")

def ensure_file(path, content, results, note=""):
    if os.path.exists(path):
        report_line(results, "skipped", path, "file exists - never overwritten")
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        report_line(results, "created", path, note)

def seed_template(template_name, dest, results, substitutions=None):
    candidates = [os.path.join(PLUGIN_DIR, "templates", template_name)]
    parent = os.path.dirname(PLUGIN_DIR)
    for sib in ("golden-thread-wiki", "project-flow"):
        candidates.append(os.path.join(parent, sib, "templates", template_name))
    src = next((c for c in candidates if os.path.exists(c)), None)
    if src is None:
        report_line(results, "error", candidates[0], "template missing from plugin")
        return
    with open(src, encoding="utf-8") as f:
        content = f.read()
    for old, new in (substitutions or {}).items():
        content = content.replace(old, new)
    note = f"seeded from templates/{template_name}"
    if substitutions and any(old in content for old in substitutions):
        note += " (placeholder NOT filled - pass --domain)"
    ensure_file(dest, content, results, note)

def write_config(config_path, vault_path, results):
    config_path = os.path.expanduser(config_path)
    vault_path = os.path.abspath(vault_path)
    if os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            report_line(results, "conflict", config_path,
                        "exists but is not valid JSON - fix by hand")
            return False
        current = existing.get("vault_path")
        if current == vault_path:
            report_line(results, "skipped", config_path, "config already points here")
            return True
        report_line(results, "conflict", config_path,
                    f"points at {current!r}, not {vault_path!r} - not changed; "
                    "a human decides which vault wins")
        return False
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump({"vault_path": vault_path}, f, indent=2)
        f.write("\n")
    report_line(results, "created", config_path, f"vault_path -> {vault_path}")
    return True

def init_wiki(vault, domain, results):
    ensure_dir(os.path.join(vault, "Sources"), results)
    ensure_dir(os.path.join(vault, "Knowledge"), results)
    ensure_file(os.path.join(vault, "index.md"), INDEX_STUB, results, "index stub")
    ensure_file(os.path.join(vault, "log.md"), LOG_STUB, results, "log stub")
    subs = {"<YOUR DOMAIN HERE>": domain} if domain else None
    seed_template("wiki-CLAUDE.md", os.path.join(vault, "CLAUDE.md"), results, subs)
    seed_template("wiki-knowledge-template.md",
                  os.path.join(vault, "Knowledge", "_template.md"), results)

def init_flow(vault, results):
    projects = os.path.join(vault, "Projects")
    ensure_dir(projects, results)
    seed_template("CONVENTIONS.md", os.path.join(projects, "CONVENTIONS.md"), results)
    seed_template("PROTOCOL.md", os.path.join(projects, "PROTOCOL.md"), results)
    seed_template("HANDOFF.md", os.path.join(projects, "HANDOFF.md"), results)
    ensure_file(os.path.join(projects, "README.md"), PROJECTS_README_STUB,
                results, "master index stub")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["wiki", "flow", "all"])
    ap.add_argument("--vault", required=True)
    ap.add_argument("--domain", default=None)
    ap.add_argument("--config", default="~/.claude/vault-config.json")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    vault = os.path.abspath(os.path.expanduser(args.vault))
    results = []
    ensure_dir(vault, results)

    if args.mode in ("wiki", "all"):
        init_wiki(vault, args.domain, results)
    if args.mode in ("flow", "all"):
        init_flow(vault, results)
    write_config(args.config, vault, results)

    conflicts = [r for r in results if r["action"] in ("conflict", "error")]
    if args.json:
        print(json.dumps({"vault": vault, "mode": args.mode,
                          "results": results, "ok": not conflicts}, indent=2))
    else:
        print(f"# Vault init ({args.mode}) - {vault}\n")
        for r in results:
            print(f"- {r['action'].upper():8} {r['path']}"
                  + (f"  ({r['note']})" if r["note"] else ""))
        if conflicts:
            print("\nConflicts above need a human decision. Nothing was overwritten.")
    sys.exit(3 if conflicts else 0)

if __name__ == "__main__":
    main()
