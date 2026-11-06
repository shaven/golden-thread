"""Where gt's own scripts are installed (0.5.1).

A connection template that launches a gt script (`vault-mcp` runs gt_vault_mcp.py) cannot hold a
fixed path: the script lives in the versioned plugin cache, <cache>/gt/<version>/scripts/, and the
version changes with every gt upgrade. `scripts_dir()` returns the newest installed folder that holds
the server. Versions compare numerically, so 0.10.0 outranks 0.9.4.
"""
import os
import re
from pathlib import Path

from .errors import GatewayError

SERVER = "gt_vault_mcp.py"
_VER = re.compile(r"^\d+(\.\d+)*$")


def plugin_cache():
    env = os.environ.get("GT_PLUGIN_CACHE")
    return Path(env) if env else Path.home() / ".claude" / "plugins" / "cache" / "golden-thread-plugin"


def scripts_dir(cache=None):
    """-> the scripts folder of the newest installed gt that holds the vault MCP server."""
    root = Path(cache) if cache else plugin_cache()
    found = []
    base = root / "gt"
    if base.is_dir():
        for d in base.iterdir():
            if _VER.match(d.name) and (d / "scripts" / SERVER).is_file():
                found.append((tuple(int(x) for x in d.name.split(".")), d / "scripts"))
    if not found:
        raise GatewayError("gt_not_installed",
                           f"no installed gt with {SERVER} was found under {root}",
                           ["install gt: run install.sh from the gt plugin repository, or "
                            "pass --param scripts_dir=/path/to/gt/scripts"])
    return max(found)[1]
