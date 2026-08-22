#!/usr/bin/env python3
"""
gt_ingest.py — Scan an existing project and classify its content as Golden Thread migration candidates.

Usage:
  python3 gt_ingest.py <project-dir> [--json]

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
import re
import subprocess
import sys
from pathlib import Path


def encode_project_path(project_dir: Path) -> str:
    """Mirror Claude Code's internal path encoding: replace / with -, strip leading -."""
    return str(project_dir.resolve()).replace("/", "-").lstrip("-")


def classify_memory_file(filename: str, content: str) -> str:
    """Classify a memory/*.md file to a suggested_dest."""
    name = filename.lower()
    if "feedback" in name:
        return "decisions"
    if any(kw in name for kw in ("architecture", "design", "structure", "schema")):
        return "design"
    if any(kw in name for kw in ("hyperspace", "platform", "istio", "xsuaa", "kyma", "btp")):
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
    if any(kw in name for kw in ("hyperspace", "platform", "istio", "xsuaa", "kyma", "btp", "knowledge")):
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
    encoded = encode_project_path(project_dir)
    memory_base = Path.home() / ".claude" / "projects" / encoded / "memory"
    if not memory_base.exists():
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


def main():
    parser = argparse.ArgumentParser(description="Golden Thread project scanner")
    parser.add_argument("project_dir", type=Path, help="Root of the project to scan")
    parser.add_argument("--json", action="store_true", help="Output as JSON (default: pretty print)")
    args = parser.parse_args()

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

    if args.json:
        print(json.dumps(candidates, indent=2))
    else:
        # Human-readable summary
        by_dest = {}
        for c in candidates:
            by_dest.setdefault(c["suggested_dest"], []).append(c)
        for dest, items in sorted(by_dest.items()):
            print(f"\n=== {dest.upper()} ({len(items)} items) ===")
            for item in items:
                print(f"  [{item['confidence']}] {item['filename']}")
                print(f"        {item['content_preview'][:100]}...")


if __name__ == "__main__":
    main()
