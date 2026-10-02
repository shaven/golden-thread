#!/usr/bin/env python3
"""
gt_ingest.py — Scan an existing project and classify its content as Golden Thread migration candidates.

Usage:
  python3 gt_ingest.py <project-dir> [--json] [--vault V] [--no-checkpoint | --dry-run]
  python3 gt_ingest.py --resume CHECKPOINT [--json]            # what is left, in order
  python3 gt_ingest.py --done CHECKPOINT --index N [--result TEXT] [--json]
  python3 gt_ingest.py --status CHECKPOINT

Checkpoints (0.18.0): the candidates ARE the batch. The scan itself takes a second; the work
that gets interrupted is the migration the skill does for each candidate, one at a time,
sometimes across a context limit. So a scan writes a checkpoint holding the candidate list
(gt_checkpoint.py, `<vault>/Projects/golden-thread/spool/ingest/...progress.json`) and names it
on stderr as `checkpoint: <path> (N items)`; every candidate carries its `index`. After
migrating candidate N the skill records it with `--done CHECKPOINT --index N --result "<where
it went>"`, strictly in order. `--resume CHECKPOINT` prints only the candidates still to do (same
shape as a scan) -- from any later session -- and the prior results on stderr. When the last
candidate is marked done, every result (old and new, merged) is printed and the checkpoint is
deleted. A plain scan never resumes: it starts a fresh checkpoint whatever exists.
`--no-checkpoint` (alias `--dry-run`) writes nothing.

Output (--json): JSON array of candidate objects:
  [{
    "type": "memory" | "claude_md" | "doc" | "git_log" | "tech_stack",
    "path": "<absolute path or source label>",
    "filename": "<basename>",
    "content_preview": "<first 200 chars>",
    "word_count": N,
    "suggested_dest": "decisions" | "research" | "design" | "knowledge" | "global_memory" | "ideas" | "skip",
    "confidence": "high" | "medium" | "low"
  }]
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


KNOWLEDGE_KEYWORDS = ("auth", "platform", "networking", "identity", "orchestration", "infrastructure")


def encode_project_path(project_dir: Path) -> str:
    """Mirror Claude Code's internal path encoding: every non-alphanumeric character
    becomes -, the leading - included (/Users/me/my proj -> -Users-me-my-proj)."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(project_dir.resolve()))


def has_word(name: str, keywords) -> bool:
    """True if any keyword appears as a whole word. _ - . and spaces separate words,
    so 'auth' matches auth-flow.md but not AUTHORS.md, and 'structure' does not
    match infrastructure_notes.md."""
    return any(re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", name) for kw in keywords)


def classify_memory_file(filename: str, content: str) -> str:
    """Classify a memory/*.md file to a suggested_dest."""
    name = filename.lower()
    if "feedback" in name:
        return "decisions"
    if has_word(name, ("architecture", "design", "structure", "schema")):
        return "design"
    if has_word(name, KNOWLEDGE_KEYWORDS):
        return "knowledge"
    if "golden-thread" in name or "golden_thread" in name:
        return "global_memory"
    if name in ("user.md", "user_prefs.md", "personal.md"):
        return "skip"
    # content-based fallback
    content_lower = content.lower()
    if any(kw in content_lower for kw in ("don't", "never", "always", "avoid", "rule:", "constraint")):
        return "decisions"
    if any(kw in content_lower for kw in ("found", "discovered", "bug", "error", "gotcha", "fixed", "workaround")):
        return "research"
    return "research"


def classify_doc_file(path: Path, content: str) -> str:
    """Classify a root-level markdown doc."""
    name = path.name.lower()
    if any(kw in name for kw in ("arch", "design", "schema", "system", "overview")):
        return "design"
    if has_word(name, KNOWLEDGE_KEYWORDS + ("knowledge",)):
        return "knowledge"
    if any(kw in name for kw in ("backlog", "todo", "ideas", "future")):
        return "ideas"
    if name in ("readme.md", "contributing.md", "license.md", "changelog.md"):
        return "skip"
    return "research"


def classify_claude_md_section(heading: str, content: str) -> str:
    """Classify a section of CLAUDE.md by heading."""
    h = heading.lower()
    if any(kw in h for kw in ("key decision", "constraint", "pattern", "convention", "rule", "important")):
        return "decisions"
    if any(kw in h for kw in ("golden thread", "memory")):
        return "skip"
    if any(kw in h for kw in ("deploy", "overview", "see also", "component")):
        return "design"
    return "decisions"


def preview(text: str, chars=200) -> str:
    return text.strip()[:chars].replace("\n", " ")


def word_count(text: str) -> int:
    return len(text.split())


def scan_memory_dir(project_dir: Path, candidates: list):
    projects = Path.home() / ".claude" / "projects"
    # Claude Code's form first; the slash-only form (which keeps _ and .) as a fallback.
    for encoded in (encode_project_path(project_dir),
                    str(project_dir.resolve()).replace("/", "-")):
        memory_base = projects / encoded / "memory"
        if memory_base.exists():
            break
    else:
        return
    for f in sorted(memory_base.glob("*.md")):
        content = f.read_text(encoding="utf-8", errors="replace")
        dest = classify_memory_file(f.name, content)
        candidates.append({
            "type": "memory",
            "path": str(f),
            "filename": f.name,
            "content_preview": preview(content),
            "word_count": word_count(content),
            "suggested_dest": dest,
            "confidence": "high" if dest != "research" else "medium",
        })


def scan_claude_md(project_dir: Path, candidates: list):
    claude_md = project_dir / "CLAUDE.md"
    if not claude_md.exists():
        return
    content = claude_md.read_text(encoding="utf-8", errors="replace")
    # Split by H2/H3 headings
    sections = re.split(r'\n(#{2,3} .+)', content)
    current_heading = "Overview"
    buffer = sections[0] if sections else ""
    for i, part in enumerate(sections[1:]):
        if re.match(r'^#{2,3} ', part):
            current_heading = part.strip("# ").strip()
            buffer = ""
        else:
            buffer = part
            dest = classify_claude_md_section(current_heading, buffer)
            if dest == "skip":
                continue
            candidates.append({
                "type": "claude_md",
                "path": str(claude_md),
                "filename": f"CLAUDE.md § {current_heading}",
                "content_preview": preview(buffer),
                "word_count": word_count(buffer),
                "suggested_dest": dest,
                "confidence": "medium",
            })


def scan_root_docs(project_dir: Path, candidates: list):
    skip_names = {"claude.md", "readme.md", "contributing.md", "license.md", "changelog.md"}
    for f in sorted(project_dir.glob("*.md")):
        if f.name.lower() in skip_names:
            continue
        content = f.read_text(encoding="utf-8", errors="replace")
        dest = classify_doc_file(f, content)
        candidates.append({
            "type": "doc",
            "path": str(f),
            "filename": f.name,
            "content_preview": preview(content),
            "word_count": word_count(content),
            "suggested_dest": dest,
            "confidence": "low",
        })


def scan_tech_stack(project_dir: Path, candidates: list):
    stack_parts = []
    if (project_dir / "package.json").exists():
        try:
            pkg = json.loads((project_dir / "package.json").read_text())
            stack_parts.append(f"Runtime: Node.js {pkg.get('engines', {}).get('node', 'unknown')}")
            deps = list(pkg.get("dependencies", {}).keys())[:10]
            stack_parts.append(f"Key deps: {', '.join(deps)}")
        except Exception:
            stack_parts.append("Runtime: Node.js (package.json found)")
    if (project_dir / "go.mod").exists():
        first_line = (project_dir / "go.mod").read_text().splitlines()[0]
        stack_parts.append(f"Runtime: Go ({first_line})")
    if (project_dir / "Cargo.toml").exists():
        stack_parts.append("Runtime: Rust (Cargo.toml found)")
    if (project_dir / "requirements.txt").exists() or (project_dir / "pyproject.toml").exists():
        stack_parts.append("Runtime: Python")
    if stack_parts:
        content = "\n".join(stack_parts)
        candidates.append({
            "type": "tech_stack",
            "path": str(project_dir),
            "filename": "tech-stack (detected)",
            "content_preview": preview(content),
            "word_count": word_count(content),
            "suggested_dest": "design",
            "confidence": "high",
        })


def scan_git_log(project_dir: Path, candidates: list):
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-20"],
            cwd=project_dir, capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            content = result.stdout.strip()
            candidates.append({
                "type": "git_log",
                "path": str(project_dir / ".git"),
                "filename": "git log (last 20 commits)",
                "content_preview": preview(content),
                "word_count": word_count(content),
                "suggested_dest": "design",
                "confidence": "low",
            })
    except Exception:
        pass


def _checkpoint_mod():
    import gt_checkpoint                                           # noqa: PLC0415
    return gt_checkpoint


def print_candidates(candidates, as_json):
    if as_json:
        print(json.dumps(candidates, indent=2))
        return
    by_dest = {}
    for c in candidates:
        by_dest.setdefault(c["suggested_dest"], []).append(c)
    for dest, items in sorted(by_dest.items()):
        print(f"\n=== {dest.upper()} ({len(items)} items) ===")
        for item in items:
            idx = f"#{item['index']} " if "index" in item else ""
            print(f"  {idx}[{item['confidence']}] {item['filename']}")
            print(f"        {item['content_preview'][:100]}...")


def do_resume(path, as_json):
    gc = _checkpoint_mod()
    try:
        ck = gc.Checkpoint.load(path, "ingest")
    except gc.CheckpointError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print("checkpoint: %s -- %d of %d candidate(s) already migrated"
          % (ck.path, ck.next_index, ck.total), file=sys.stderr)
    for i, r in enumerate(ck.results):
        print("  done #%d: %s" % (i, (r or {}).get("result", "")), file=sys.stderr)
    print_candidates([item for _i, item in ck.remaining()], as_json)
    return 0


def do_done(path, index, result, as_json):
    gc = _checkpoint_mod()
    try:
        ck = gc.Checkpoint.load(path, "ingest")
        item = ck.items[index] if 0 <= index < ck.total else None
        if item is None:
            raise gc.CheckpointError("no candidate #%d (the checkpoint has %d)" % (index, ck.total))
        ck.done(index, {"index": index, "filename": item.get("filename"),
                        "result": result or ""})
    except gc.CheckpointError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    if ck.next_index < ck.total:
        msg = {"done": ck.next_index, "total": ck.total, "checkpoint": str(ck.path)}
        print(json.dumps(msg) if as_json else
              "recorded #%d; %d of %d done" % (index, ck.next_index, ck.total))
        return 0
    results = list(ck.results)
    ck.finish()
    if as_json:
        print(json.dumps({"done": ck.total, "total": ck.total, "complete": True,
                          "results": results}, indent=2))
    else:
        print("ingest complete: %d of %d candidate(s); checkpoint removed" % (ck.total, ck.total))
        for r in results:
            print("  #%d %s -> %s" % (r["index"], r.get("filename"), r.get("result")))
    return 0


def main():
    parser = argparse.ArgumentParser(description="Golden Thread project scanner")
    parser.add_argument("project_dir", type=Path, nargs="?", help="Root of the project to scan")
    parser.add_argument("--json", action="store_true", help="Output as JSON (default: pretty print)")
    parser.add_argument("--vault", help="the vault whose spool holds the checkpoint")
    parser.add_argument("--no-checkpoint", "--dry-run", dest="no_checkpoint",
                        action="store_true", help="write no checkpoint")
    parser.add_argument("--resume", metavar="CHECKPOINT",
                        help="print the candidates an interrupted ingest has not migrated yet")
    parser.add_argument("--done", metavar="CHECKPOINT",
                        help="record candidate --index as migrated")
    parser.add_argument("--index", type=int)
    parser.add_argument("--result", help="where the candidate went (one line)")
    parser.add_argument("--status", metavar="CHECKPOINT", help="one line of progress")
    args = parser.parse_args()

    if args.status:
        gc = _checkpoint_mod()
        try:
            print(gc.Checkpoint.load(args.status, "ingest").summary())
            return 0
        except gc.CheckpointError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if args.resume:
        return do_resume(args.resume, args.json)
    if args.done:
        if args.index is None:
            parser.error("--done needs --index")
        return do_done(args.done, args.index, args.result, args.json)
    if args.project_dir is None:
        parser.error("a project directory is required")

    project_dir = args.project_dir.resolve()
    if not project_dir.exists():
        print(json.dumps({"error": f"Project directory not found: {project_dir}"}))
        sys.exit(1)

    candidates = []
    scan_memory_dir(project_dir, candidates)
    scan_claude_md(project_dir, candidates)
    scan_root_docs(project_dir, candidates)
    scan_tech_stack(project_dir, candidates)
    scan_git_log(project_dir, candidates)

    # Filter out empty previews
    candidates = [c for c in candidates if c["content_preview"].strip()]
    for i, c in enumerate(candidates):
        c["index"] = i

    if candidates and not args.no_checkpoint:
        gc = _checkpoint_mod()
        try:
            ck = gc.Checkpoint.start("ingest", str(project_dir), candidates,
                                     gc.find_vault(args.vault))
            print("checkpoint: %s (%d items)" % (ck.path, ck.total), file=sys.stderr)
        except OSError as exc:
            print("note: no checkpoint (%s); an interrupted ingest will start over"
                  % exc.__class__.__name__, file=sys.stderr)

    print_candidates(candidates, args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
