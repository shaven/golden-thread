"""Project-level operations inside a Golden Thread vault."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .vault import Vault


class ProjectManager:
    """CRUD + logging + workflows for a Golden Thread project."""

    def __init__(self, vault: Vault, active_slug: str = "") -> None:
        self.vault = vault
        self.active_slug = active_slug.strip()

    # -----------------------------------------------------------------------
    # Basic project operations
    # -----------------------------------------------------------------------

    def _require_active(self) -> None:
        if not self.active_slug:
            raise RuntimeError("No active project. Call gt_open first.")

    def open(self, slug: str) -> None:
        slug = slug.strip()
        project_dir = self.vault.project_path(slug)
        if not project_dir.exists():
            raise FileNotFoundError(f"Project '{slug}' does not exist")
        self.active_slug = slug

    async def open_command(self, slug: str, config: Any) -> Dict[str, Any]:
        self.open(slug)
        config.active_project = slug
        config.save()
        ctx = self.read_context()
        return {"status": "ok", "active_project": slug, "context_preview": ctx[:500]}

    def list(self) -> List[str]:
        return self.vault.list_projects()

    async def list_command(self) -> List[str]:
        return self.list()

    def create(self, name: str, idea: str) -> str:
        slug = self.vault.slugify(name)
        project_dir = self.vault.project_path(slug)
        if project_dir.exists():
            base = slug
            n = 2
            while project_dir.exists():
                slug = f"{base}-{n}"
                project_dir = self.vault.project_path(slug)
                n += 1
        self.vault.ensure_dir(project_dir)
        self.vault.ensure_dir(project_dir / "Decisions")
        self.vault.ensure_dir(project_dir / "Log")
        self.vault.ensure_dir(project_dir / "Memory")
        self.vault.ensure_dir(project_dir / "Tasks")
        self.vault.ensure_dir(project_dir / "Handoffs")
        (project_dir / "idea.md").write_text(
            f"# {name}\n\n{idea}\n", encoding="utf-8"
        )
        (project_dir / "CONVENTIONS.md").write_text(
            f"# CONVENTIONS\n\nProject-specific conventions for {name}.\n",
            encoding="utf-8",
        )
        (project_dir / "runbook.md").write_text(
            f"# Runbook: {name}\n\nOperational notes and recurring procedures.\n",
            encoding="utf-8",
        )
        self.active_slug = slug
        return slug

    async def create_command(self, name: str, idea: str, config: Any) -> Dict[str, Any]:
        slug = self.create(name, idea)
        config.active_project = slug
        config.save()
        return {"status": "created", "project": slug, "path": str(self.vault.project_path(slug))}

    def log(self, summary: str) -> None:
        self._require_active()
        log_dir = self.vault.project_path(self.active_slug) / "Log"
        self.vault.ensure_dir(log_dir)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_file = log_dir / f"{today}.md"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        entry = f"- {ts}: {summary}\n"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(entry)

    async def work_command(self, summary: str) -> Dict[str, Any]:
        self.log(summary)
        return {"status": "logged", "project": self.active_slug}

    def decide(
        self,
        title: str,
        context: str,
        decision: str,
        consequences: str,
    ) -> str:
        self._require_active()
        decisions_dir = self.vault.project_path(self.active_slug) / "Decisions"
        self.vault.ensure_dir(decisions_dir)
        existing = sorted(decisions_dir.glob("ADR-*.md"))
        next_id = len(existing) + 1
        adr_id = f"ADR-{next_id:04d}"
        slug = re.sub(r"[^\w]+", "-", title).lower().strip("-")
        file_name = f"{adr_id}-{slug}.md" if slug else f"{adr_id}.md"
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        body = (
            f"# {adr_id}: {title}\n\n"
            f"Date: {today}\n\n"
            f"## Context\n\n{context}\n\n"
            f"## Decision\n\n{decision}\n\n"
            f"## Consequences\n\n{consequences}\n"
        )
        (decisions_dir / file_name).write_text(body, encoding="utf-8")
        return adr_id

    async def decide_command(
        self,
        title: str,
        context: str,
        decision: str,
        consequences: str,
    ) -> Dict[str, Any]:
        adr_id = self.decide(title, context, decision, consequences)
        return {"status": "recorded", "adr": adr_id}

    def add_memory(self, content: str, category: str = "general") -> None:
        self._require_active()
        mem_dir = self.vault.project_path(self.active_slug) / "Memory"
        self.vault.ensure_dir(mem_dir)
        file_path = mem_dir / f"{category}.md"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(f"- {ts}: {content}\n")

    def read_context(self, max_lines: int = 400) -> str:
        if not self.active_slug:
            return ""
        project_dir = self.vault.project_path(self.active_slug)
        candidates = [
            project_dir / "idea.md",
            project_dir / "CONVENTIONS.md",
            project_dir / "runbook.md",
        ]
        lines: List[str] = []
        for cand in candidates:
            if cand.exists():
                text = cand.read_text(encoding="utf-8").splitlines()
                lines.extend(text)
                lines.append("")
                if len(lines) >= max_lines:
                    break
        # Append latest decision
        decisions_dir = project_dir / "Decisions"
        if decisions_dir.exists():
            adrs = sorted(decisions_dir.glob("ADR-*.md"))
            if adrs:
                lines.append("## Latest decision")
                lines.extend(adrs[-1].read_text(encoding="utf-8").splitlines()[:80])
                lines.append("")
        # Append latest log line
        log_dir = project_dir / "Log"
        if log_dir.exists():
            logs = sorted(log_dir.glob("*.md"))
            if logs:
                log_lines = logs[-1].read_text(encoding="utf-8").splitlines()
                if log_lines:
                    lines.append("## Latest log")
                    lines.extend(log_lines[-5:])
                    lines.append("")
        # Append open tasks and handoffs
        lines.extend(self._tasks_and_handoffs_lines(project_dir))
        return "\n".join(lines[:max_lines]).strip()

    def _tasks_and_handoffs_lines(self, project_dir: Path) -> List[str]:
        lines: List[str] = []
        tasks_dir = project_dir / "Tasks"
        if tasks_dir.exists():
            tasks = sorted(tasks_dir.glob("*.md"))
            open_tasks = [t for t in tasks if "[x]" not in t.read_text(encoding="utf-8")[:200]]
            if open_tasks:
                lines.append("## Open tasks")
                for t in open_tasks:
                    lines.append(f"- {t.stem}")
                lines.append("")
        handoffs_dir = project_dir / "Handoffs"
        if handoffs_dir.exists():
            handoffs = sorted(handoffs_dir.glob("*.md"))
            if handoffs:
                lines.append("## Open handoffs")
                for h in handoffs[-5:]:
                    lines.append(f"- {h.stem}")
                lines.append("")
        return lines

    # -----------------------------------------------------------------------
    # Command handlers
    # -----------------------------------------------------------------------

    async def close_command(self, config: Any) -> Dict[str, Any]:
        slug = self.active_slug
        if slug:
            # Write a tiny session closeout note.
            self.log(f"Session closed for {slug}")
        config.active_project = ""
        config.save()
        self.active_slug = ""
        return {"status": "closed", "was_active": slug}

    async def route_command(self, session_so_far: str) -> Dict[str, Any]:
        self._require_active()
        return {
            "status": "ok",
            "active_project": self.active_slug,
            "suggestion": (
                f"Session so far: {session_so_far[:200]!r}. "
                f"Output belongs in project '{self.active_slug}'. "
                "Use gt_work to record findings and gt_decide for new decisions."
            ),
        }

    async def query_command(self, q: str, lineage: str = "") -> Dict[str, Any]:
        return await self.vault.query(q, lineage=lineage, active_project=self.active_slug)

    async def handle_command(
        self,
        kind: str,
        item_id: str = "",
        action: str = "",
        reason: str = "",
    ) -> Dict[str, Any]:
        self._require_active()
        project_dir = self.vault.project_path(self.active_slug)
        folder = project_dir / ("Tasks" if kind == "tasks" else "Handoffs")
        self.vault.ensure_dir(folder)
        if not item_id:
            items = sorted(folder.glob("*.md"))
            return {
                "status": "ok",
                "kind": kind,
                "count": len(items),
                "items": [i.stem for i in items],
            }
        target = folder / f"{item_id}.md"
        if not target.exists():
            return {"status": "error", "detail": f"{kind} '{item_id}' not found"}
        text = target.read_text(encoding="utf-8")
        if action == "close":
            new_text = text.replace("- [ ]", "- [x]", 1) + f"\n\nClosed: {datetime.now(timezone.utc).isoformat()}\n"
            target.write_text(new_text, encoding="utf-8")
            return {"status": "closed", "item": item_id}
        if action == "drop":
            if not reason:
                return {"status": "error", "detail": "reason required to drop item"}
            target.write_text(text + f"\n\nDropped: {datetime.now(timezone.utc).isoformat()} — {reason}\n", encoding="utf-8")
            return {"status": "dropped", "item": item_id, "reason": reason}
        return {"status": "ok", "item": item_id, "action": action or "view", "preview": text[:500]}

    async def complete_command(
        self,
        kind: str,
        item_id: str = "",
        move: bool = False,
    ) -> Dict[str, Any]:
        self._require_active()
        if kind == "project":
            # Mark project as complete in a handoff note.
            self.log(f"Project marked complete (move={move})")
            return {"status": "complete", "project": self.active_slug, "move": move}
        folder = self.vault.project_path(self.active_slug) / ("Tasks" if kind == "task" else "Handoffs")
        target = folder / f"{item_id}.md"
        if not target.exists():
            return {"status": "error", "detail": f"{kind} '{item_id}' not found"}
        text = target.read_text(encoding="utf-8")
        new_text = text.replace("- [ ]", "- [x]", 1) + f"\n\nCompleted: {datetime.now(timezone.utc).isoformat()}\n"
        target.write_text(new_text, encoding="utf-8")
        return {"status": "complete", "kind": kind, "item": item_id}

    async def review_command(self) -> Dict[str, Any]:
        inbox = self.vault.inbox_path()
        if not inbox.exists():
            return {"status": "ok", "inbox_items": 0, "note": "No INBOX.md found"}
        lines = inbox.read_text(encoding="utf-8").splitlines()
        items = [l for l in lines if l.strip() and not l.strip().startswith("#")]
        # Move reviewed items into a daily note.
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily = self.vault.root / "Daily" / f"{today}.md"
        self.vault.ensure_dir(daily.parent)
        if items:
            with open(daily, "a", encoding="utf-8") as f:
                f.write(f"\n## Review {today}\n")
                for it in items:
                    f.write(f"- {it}\n")
            inbox.write_text("# INBOX\n\n", encoding="utf-8")
        return {"status": "reviewed", "inbox_items": len(items), "daily": str(daily)}

    async def plan_command(self, requirement: str) -> Dict[str, Any]:
        self._require_active()
        project_dir = self.vault.project_path(self.active_slug)
        plans_dir = project_dir / ".gt" / "plans"
        self.vault.ensure_dir(plans_dir)
        plan_id = f"plan-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        plan_file = plans_dir / f"{plan_id}.md"
        body = (
            f"# Plan: {plan_id}\n\n"
            f"Requirement: {requirement}\n\n"
            "## Phases\n\n"
            "1. Understand current state and risks.\n"
            "2. Design the smallest change that satisfies the requirement.\n"
            "3. Write or update tests first.\n"
            "4. Implement the change.\n"
            "5. Run checks (scan, lint, tests).\n"
            "6. Record decisions and close handoffs.\n\n"
            "status: draft\n"
        )
        plan_file.write_text(body, encoding="utf-8")
        return {"status": "draft", "plan_id": plan_id, "path": str(plan_file)}

    async def implement_command(self, plan_id: str, phase: str = "") -> Dict[str, Any]:
        self._require_active()
        plan_file = self.vault.project_path(self.active_slug) / ".gt" / "plans" / f"{plan_id}.md"
        if not plan_file.exists():
            return {"status": "error", "detail": f"Plan {plan_id} not found"}
        text = plan_file.read_text(encoding="utf-8")
        if "status: approved" not in text and "status: draft" in text:
            return {"status": "error", "detail": "Plan is still draft. Approve it before implementing."}
        # In Hermes, implementation is delegated to the agent itself; this tool
        # returns the plan phase so the model can act on it.
        return {
            "status": "implementing",
            "plan_id": plan_id,
            "phase": phase or "next",
            "note": (
                "Hermes does not run code autonomously. Use the returned phase to drive "
                "the next edits, tests, and gt_work/gt_decide calls."
            ),
        }

    async def brief_command(self, project: str = "") -> Dict[str, Any]:
        slug = project or self.active_slug
        if not slug:
            return {"status": "error", "detail": "No project specified or active"}
        ctx = self.read_context()
        return {"status": "ok", "project": slug, "brief": ctx[:2000]}

    async def minimize_command(self) -> Dict[str, Any]:
        self._require_active()
        project_dir = self.vault.project_path(self.active_slug)
        # Estimate vault bytes for this project.
        total = sum(
            f.stat().st_size for f in project_dir.rglob("*") if f.is_file()
        )
        return {
            "status": "ok",
            "project": self.active_slug,
            "project_bytes": total,
            "suggestion": (
                "Consider moving stale Memory notes to Knowledge or archiving old Log files."
            ),
        }
