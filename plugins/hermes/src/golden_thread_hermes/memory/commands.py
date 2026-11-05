"""Command definitions and dispatch table for Golden Thread Hermes parity.

This module maps Claude Code `/gt:*` commands to Hermes tool-call schemas and
provides a single dispatch table the memory provider can use to route tool calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Dict, List, Optional

ToolDefinition = Dict[str, Any]
Dispatcher = Callable[["CommandContext", Dict[str, Any]], Coroutine[Any, Any, Dict[str, Any]]]


@dataclass
class CommandContext:
    """Runtime context passed to every command handler."""

    tool_name: str
    vault_path: Any  # Vault instance, set lazily in provider
    project_manager: Any  # ProjectManager instance
    config: Any  # GoldenThreadConfig instance


@dataclass
class Command:
    """One Golden Thread command exposed as a Hermes tool."""

    name: str
    description: str
    parameters: Dict[str, Any]
    required: List[str]
    handler: Dispatcher

    @property
    def schema(self) -> ToolDefinition:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": self.parameters,
                "required": self.required,
            },
        }


# ---------------------------------------------------------------------------
# Placeholder handlers — replaced with real implementations in commands_real.py
# to keep this file a pure registry.
# ---------------------------------------------------------------------------
async def _not_implemented(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "not_implemented",
        "detail": f"{ctx.tool_name} is mapped but not yet implemented in Hermes.",
    }


# Core project / session commands
async def _gt_open(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.project_manager.open_command(args.get("project_slug", ""), ctx.config)


async def _gt_list(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return {"projects": await ctx.project_manager.list_command()}


async def _gt_create(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.project_manager.create_command(
        args.get("name", ""), args.get("idea", ""), ctx.config
    )


async def _gt_work(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.project_manager.work_command(args.get("summary", ""))


async def _gt_decide(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.project_manager.decide_command(
        args.get("title", ""),
        args.get("context", ""),
        args.get("decision", ""),
        args.get("consequences", ""),
    )


async def _gt_close(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.project_manager.close_command(ctx.config)


async def _gt_route(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.project_manager.route_command(args.get("session_so_far", ""))


# Vault maintenance commands
async def _gt_query(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.project_manager.query_command(args.get("q", ""), args.get("lineage", ""))


async def _gt_lint(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.lint_command(fix=args.get("apply", False))


async def _gt_sync(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.sync_command(action=args.get("action", "status"))


async def _gt_forget(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.forget_command(
        claim=args.get("claim", ""), reason=args.get("reason", "")
    )


async def _gt_promote(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.promote_command(
        source=args.get("source", ""),
        destination=args.get("to", ""),
        project=args.get("project", ""),
    )


async def _gt_demote(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.demote_command(
        source=args.get("source", ""),
        destination=args.get("to", ""),
        project=args.get("project", ""),
    )


async def _gt_refresh(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.refresh_command(apply=args.get("apply", False))


async def _gt_upgrade(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.upgrade_command(dry_run=args.get("dry_run", True))


async def _gt_doctor(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.doctor_command()


async def _gt_settings(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.settings_command(
        view=args.get("view", False),
        set_key=args.get("set", ""),
        set_value=args.get("value", ""),
    )


# Task / handoff / completion commands
async def _gt_handle(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.project_manager.handle_command(
        kind=args.get("kind", "tasks"),
        item_id=args.get("id", ""),
        action=args.get("action", ""),
        reason=args.get("reason", ""),
    )


async def _gt_complete(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.project_manager.complete_command(
        kind=args.get("kind", "task"),
        item_id=args.get("id", ""),
        move=args.get("move", False),
    )


async def _gt_review(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.project_manager.review_command()


# Plan / implement / optimize / validation
async def _gt_plan(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.project_manager.plan_command(requirement=args.get("requirement", ""))


async def _gt_implement(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.project_manager.implement_command(
        plan_id=args.get("plan_id", ""), phase=args.get("phase", "")
    )


async def _gt_validate(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.validate_command(claim=args.get("claim", ""))


async def _gt_validation(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.validation_command(
        claim=args.get("claim", ""), file_path=args.get("file", ""), status=args.get("status", "")
    )


async def _gt_optimize(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.optimize_command(
        member=args.get("member", "vault"), apply=args.get("apply", False)
    )


async def _gt_scan(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.scan_command(path=args.get("path", ""))


async def _gt_allin(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.allin_command()


async def _gt_allin_commit(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.allin_commit_command(message=args.get("message", ""))


# Brief / context / runbook / digest / intake
async def _gt_brief(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.project_manager.brief_command(project=args.get("project", ""))


async def _gt_context(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.context_command()


async def _gt_runbook_lint(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.runbook_lint_command()


async def _gt_digest(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.digest_command(file_path=args.get("file", ""))


async def _gt_minimize(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.project_manager.minimize_command()


# Setup / init / calibrate / grant / help
async def _gt_setup(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.setup_command(
        modules=args.get("modules", []), security=args.get("security", "")
    )


async def _gt_init(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.init_command(
        project=args.get("project", ""), repo=args.get("repo", "")
    )


async def _gt_calibrate(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.calibrate_command(estimate=args.get("estimate", False))


async def _gt_grant(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.grant_command(
        command=args.get("command", ""),
        duration=args.get("duration", ""),
        revoke=args.get("revoke", False),
    )


async def _gt_help(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "commands": [c.name for c in COMMANDS],
        "detail": "All mapped Golden Thread commands are listed. Use the command name as a tool call.",
    }


# Ingest / wiki / watch / farm / flow / visualize / workflow / lotr / usage
async def _gt_ingest(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.ingest_command(source=args.get("source", ""))


async def _gt_wiki_init(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.wiki_init_command()


async def _gt_wiki_ingest(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.wiki_ingest_command(source=args.get("source", ""))


async def _gt_wiki_lint(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.wiki_lint_command()


async def _gt_wiki_refresh(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.wiki_refresh_command()


async def _gt_watch(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.watch_command(
        repo=args.get("repo", ""), action=args.get("action", "add")
    )


async def _gt_farm(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.farm_command(task=args.get("task", ""))


async def _gt_flow(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.flow_command(redact=args.get("redact", False))


async def _gt_visualize(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.visualize_command(
        target=args.get("target", ""), redact=args.get("redact", False)
    )


async def _gt_workflow(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.workflow_command(
        name=args.get("name", ""), run=args.get("run", False), save=args.get("save", False)
    )


async def _gt_lotr(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.lotr_command(
        service=args.get("service", ""), operation=args.get("operation", ""), consent=args.get("consent", False)
    )


async def _gt_lotr_research(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.lotr_research_command(service=args.get("service", ""))


async def _gt_usage(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.usage_command()


async def _gt_demo(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return await ctx.vault_path.demo_command(action=args.get("action", "tour"))


COMMANDS: List[Command] = [
    # Core project/session
    Command("gt_open", "Activate a Golden Thread project by slug.", {"project_slug": {"type": "string"}}, ["project_slug"], _gt_open),
    Command("gt_list", "List all Golden Thread projects in the vault.", {}, [], _gt_list),
    Command("gt_create", "Create a new Golden Thread project.", {"name": {"type": "string"}, "idea": {"type": "string"}}, ["name", "idea"], _gt_create),
    Command("gt_work", "Append a timestamped progress entry to the active project's log.", {"summary": {"type": "string"}}, ["summary"], _gt_work),
    Command("gt_decide", "Record an Architecture Decision Record for the active project.", {"title": {"type": "string"}, "context": {"type": "string"}, "decision": {"type": "string"}, "consequences": {"type": "string"}}, ["title", "context", "decision", "consequences"], _gt_decide),
    Command("gt_close", "Close the active Golden Thread session/project claim.", {}, [], _gt_close),
    Command("gt_route", "Mid-session routing check: where does this session's output belong?", {"session_so_far": {"type": "string"}}, [], _gt_route),

    # Vault maintenance
    Command("gt_query", "Look something up in the vault by keyword or lineage.", {"q": {"type": "string"}, "lineage": {"type": "string"}}, ["q"], _gt_query),
    Command("gt_lint", "Audit the vault for structural problems.", {"apply": {"type": "boolean"}}, [], _gt_lint),
    Command("gt_sync", "Vault git sync: status, pull, or push.", {"action": {"type": "string", "enum": ["status", "pull", "push"]}}, ["action"], _gt_sync),
    Command("gt_forget", "Retract a wrong fact across the vault.", {"claim": {"type": "string"}, "reason": {"type": "string"}}, ["claim"], _gt_forget),
    Command("gt_promote", "Graduate content up the memory hierarchy.", {"source": {"type": "string"}, "to": {"type": "string"}, "project": {"type": "string"}}, ["source", "to"], _gt_promote),
    Command("gt_demote", "Move content down the memory hierarchy.", {"source": {"type": "string"}, "to": {"type": "string"}, "project": {"type": "string"}}, ["source", "to"], _gt_demote),
    Command("gt_refresh", "Check Sources/ for upstream changes.", {"apply": {"type": "boolean"}}, [], _gt_refresh),
    Command("gt_upgrade", "Bring an existing vault up to the installed release.", {"dry_run": {"type": "boolean"}}, [], _gt_upgrade),
    Command("gt_doctor", "One report for the whole install and vault.", {}, [], _gt_doctor),
    Command("gt_settings", "View or change Golden Thread settings.", {"view": {"type": "boolean"}, "set": {"type": "string"}, "value": {"type": "string"}}, [], _gt_settings),

    # Task / handoff / completion
    Command("gt_handle", "Work through a set of tasks or handoffs one at a time.", {"kind": {"type": "string", "enum": ["tasks", "handoffs"]}, "id": {"type": "string"}, "action": {"type": "string"}, "reason": {"type": "string"}}, ["kind"], _gt_handle),
    Command("gt_complete", "Complete a project, task, or handoff.", {"kind": {"type": "string", "enum": ["project", "task", "handoff"]}, "id": {"type": "string"}, "move": {"type": "boolean"}}, ["kind"], _gt_complete),
    Command("gt_review", "Empty the inbox and route captured items into projects.", {}, [], _gt_review),

    # Plan / implement / optimize / validation
    Command("gt_plan", "Write a phased, test-first plan for an upcoming change.", {"requirement": {"type": "string"}}, ["requirement"], _gt_plan),
    Command("gt_implement", "Run an approved plan one phase at a time.", {"plan_id": {"type": "string"}, "phase": {"type": "string"}}, ["plan_id"], _gt_implement),
    Command("gt_validate", "Verify a claim by re-deriving it with fresh context.", {"claim": {"type": "string"}}, ["claim"], _gt_validate),
    Command("gt_validation", "Record what a validation established or could not determine.", {"claim": {"type": "string"}, "file": {"type": "string"}, "status": {"type": "string"}}, ["claim"], _gt_validation),
    Command("gt_optimize", "Find context costs and duplicated facts in the vault.", {"member": {"type": "string", "enum": ["vault", "session", "execution"]}, "apply": {"type": "boolean"}}, [], _gt_optimize),
    Command("gt_scan", "Scan code against language definitions in effect.", {"path": {"type": "string"}}, [], _gt_scan),
    Command("gt_allin", "Run every check in one command (reporting only).", {}, [], _gt_allin),
    Command("gt_allin_commit", "Commit once checks pass and a test receipt covers staged files.", {"message": {"type": "string"}}, ["message"], _gt_allin_commit),

    # Brief / context / runbook / digest / intake
    Command("gt_brief", "Draft a self-contained CLAUDE.md section for a project.", {"project": {"type": "string"}}, [], _gt_brief),
    Command("gt_context", "Render the vault's model-reachable definitions.", {}, [], _gt_context),
    Command("gt_runbook_lint", "Scan project runbooks for duplicated content.", {}, [], _gt_runbook_lint),
    Command("gt_digest", "Have a large file or paste read and summarized cheaply.", {"file": {"type": "string"}}, ["file"], _gt_digest),
    Command("gt_minimize", "Prune a heavy session before cutting it.", {}, [], _gt_minimize),

    # Setup / init / calibrate / grant / help
    Command("gt_setup", "Finish a Golden Thread install by choosing modules and security level.", {"modules": {"type": "array", "items": {"type": "string"}}, "security": {"type": "string"}}, [], _gt_setup),
    Command("gt_init", "Create a new vault from scratch or wire an existing vault to a project.", {"project": {"type": "string"}, "repo": {"type": "string"}}, [], _gt_init),
    Command("gt_calibrate", "Run the model calibration pass now.", {"estimate": {"type": "boolean"}}, [], _gt_calibrate),
    Command("gt_grant", "Temporarily allow one command without lowering lockdown level.", {"command": {"type": "string"}, "duration": {"type": "string"}, "revoke": {"type": "boolean"}}, [], _gt_grant),
    Command("gt_help", "List all available Golden Thread Hermes tools.", {}, [], _gt_help),

    # Ingest / wiki / watch / farm / flow / visualize / workflow / lotr / usage / demo
    Command("gt_ingest", "Bulk-import an existing project's memory files and notes.", {"source": {"type": "string"}}, ["source"], _gt_ingest),
    Command("gt_wiki_init", "Initialize the Golden Thread wiki subsystem.", {}, [], _gt_wiki_init),
    Command("gt_wiki_ingest", "Ingest a source into the Golden Thread wiki.", {"source": {"type": "string"}}, ["source"], _gt_wiki_ingest),
    Command("gt_wiki_lint", "Lint the Golden Thread wiki.", {}, [], _gt_wiki_lint),
    Command("gt_wiki_refresh", "Refresh wiki pages from upstream sources.", {}, [], _gt_wiki_refresh),
    Command("gt_watch", "Watch a git repo for security fixes or notable changes.", {"repo": {"type": "string"}, "action": {"type": "string", "enum": ["add", "remove", "list"]}}, ["repo"], _gt_watch),
    Command("gt_farm", "Route a self-contained mechanical task to an external AI service.", {"task": {"type": "string"}}, ["task"], _gt_farm),
    Command("gt_flow", "Render the vault's event stream as an offline HTML page.", {"redact": {"type": "boolean"}}, [], _gt_flow),
    Command("gt_visualize", "Render a codebase as a 3D walkthrough or code city.", {"target": {"type": "string"}, "redact": {"type": "boolean"}}, [], _gt_visualize),
    Command("gt_workflow", "Save or run a chain of Golden Thread steps.", {"name": {"type": "string"}, "run": {"type": "boolean"}, "save": {"type": "boolean"}}, ["name"], _gt_workflow),
    Command("gt_lotr", "Call a connected service through the LOTR MCP gateway.", {"service": {"type": "string"}, "operation": {"type": "string"}, "consent": {"type": "boolean"}}, ["service", "operation"], _gt_lotr),
    Command("gt_lotr_research", "Research a service and produce a LOTR template + fact sheet.", {"service": {"type": "string"}}, ["service"], _gt_lotr_research),
    Command("gt_usage", "Check Claude plan usage allowance (Hermes adapter returns local notes).", {}, [], _gt_usage),
    Command("gt_demo", "Run the Golden Thread guided tour on a fictional project.", {"action": {"type": "string", "enum": ["start", "tour", "end", "clean"]}}, [], _gt_demo),
]


COMMAND_MAP: Dict[str, Command] = {c.name: c for c in COMMANDS}


def get_command_schemas() -> List[ToolDefinition]:
    return [c.schema for c in COMMANDS]


async def dispatch_command(ctx: CommandContext, args: Dict[str, Any]) -> Dict[str, Any]:
    command = COMMAND_MAP.get(ctx.tool_name)
    if not command:
        return {"status": "error", "detail": f"Unknown Golden Thread tool: {ctx.tool_name}"}
    return await command.handler(ctx, args)
