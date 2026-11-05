"""Configuration loader for the Golden Thread Hermes plugin."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from hermes_cli.config import cfg_get
from hermes_constants import get_hermes_home


class GoldenThreadConfig:
    """Runtime configuration for Golden Thread integration."""

    DEFAULT_VAULT_NAME = "golden-vault"

    def __init__(
        self,
        vault_path: Path,
        active_project: str = "",
        core_rules_max_lines: int = 200,
        project_context_max_lines: int = 400,
    ) -> None:
        self.vault_path = vault_path
        self.active_project = active_project
        self.core_rules_max_lines = core_rules_max_lines
        self.project_context_max_lines = project_context_max_lines

    @classmethod
    def load(cls) -> "GoldenThreadConfig":
        from hermes_cli.config import load_config
        config = load_config()
        plugin_cfg = cfg_get(config, "plugins", "golden-thread", default={}) or {}
        raw_path = plugin_cfg.get("vault_path", "")
        if raw_path:
            vault_path = Path(raw_path).expanduser()
        else:
            # Default to a subfolder inside the active profile's Obsidian vault.
            hermes_home = get_hermes_home()
            vault_path = hermes_home.parent / "obsidian-vaults" / "eman-mobile" / cls.DEFAULT_VAULT_NAME
            if not vault_path.exists():
                vault_path = Path("/mnt/obsidian-vaults/eman-mobile") / cls.DEFAULT_VAULT_NAME
        return cls(
            vault_path=vault_path,
            active_project=str(plugin_cfg.get("active_project", "")).strip(),
            core_rules_max_lines=int(plugin_cfg.get("core_rules_max_lines", 200)),
            project_context_max_lines=int(plugin_cfg.get("project_context_max_lines", 400)),
        )

    def save(self) -> None:
        """Persist non-secret config to config.yaml."""
        from hermes_cli.config import load_config
        import yaml
        config = load_config()
        if "plugins" not in config:
            config["plugins"] = {}
        config["plugins"]["golden-thread"] = {
            "vault_path": str(self.vault_path),
            "active_project": self.active_project,
            "core_rules_max_lines": self.core_rules_max_lines,
            "project_context_max_lines": self.project_context_max_lines,
        }
        cfg_path = get_hermes_home() / "config.yaml"
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.dump(config, f, default_flow_style=False)
