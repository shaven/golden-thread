"""Golden Thread memory provider for Hermes Agent.

Exposes 52 Golden Thread commands as Hermes tools, backed by a local markdown
vault. The 39 canonical `/gt:gt-*` commands from the Claude Code plugin are all
present; the remaining tools are Hermes-specific helpers, aliases, or stubs for
UI-only / external-subsystem commands that have no Hermes equivalent.
"""

from __future__ import annotations

from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider as BaseMemoryProvider
from tools.registry import tool_result, tool_error

from .commands import CommandContext, dispatch_command, get_command_schemas
from .config import GoldenThreadConfig
from .project import ProjectManager
from .vault import Vault


class GoldenThreadMemoryProvider(BaseMemoryProvider):
    """Hermes memory provider backed by a Golden Thread vault."""

    _NAME = "golden-thread"

    def __init__(self) -> None:
        self.config = GoldenThreadConfig.load()
        self.vault: Vault = Vault(self.config.vault_path)
        self.project = ProjectManager(self.vault, self.config.active_project)

    @property
    def name(self) -> str:
        return self._NAME

    def is_available(self) -> bool:
        return self.config.vault_path.exists()

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self.config = GoldenThreadConfig.load()
        self.vault = Vault(self.config.vault_path)
        self.project = ProjectManager(self.vault, self.config.active_project)

    def system_prompt_block(self) -> str:
        core = self.vault.read_core()
        if not core:
            return ""
        return (
            "Golden Thread Core Rules\n"
            "\n"
            f"{core}\n"
            "\n"
            "When the user says anything about a Golden Thread command (e.g., gt_*), "
            "call the matching tool."
        )

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return get_command_schemas()

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        ctx = CommandContext(
            tool_name=tool_name,
            vault_path=self.vault,
            project_manager=self.project,
            config=self.config,
        )
        import asyncio
        try:
            result = asyncio.run(dispatch_command(ctx, args))
        except Exception as exc:
            return tool_error(str(exc))
        return tool_result(result)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self.project.active_slug:
            return ""
        ctx = self.project.read_context()
        if not ctx:
            return ""
        return (
            f"Active Golden Thread project: {self.project.active_slug}\n"
            "---\n"
            f"{ctx}"
        )

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        pass

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: List[Dict[str, Any]] | None = None,
    ) -> None:
        if self.project.active_slug:
            self.project.log(assistant_content[:240])
