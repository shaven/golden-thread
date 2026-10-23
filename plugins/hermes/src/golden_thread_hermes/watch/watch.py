"""Hermes-friendly wrapper around the upstream gt_watch.py script."""

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

WATCH_SCRIPT = Path(__file__).with_name("gt_watch.py")


def _run(args: List[str], env: Dict[str, str] | None = None) -> Dict[str, Any]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    merged_env.setdefault("GT_WATCH", "report")
    try:
        result = subprocess.run(
            [sys.executable, str(WATCH_SCRIPT), *args],
            capture_output=True,
            text=True,
            timeout=120,
            env=merged_env,
            cwd=str(WATCH_SCRIPT.parent),
        )
        return {
            "status": "ok" if result.returncode == 0 else "error",
            "exit_code": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "exit_code": None, "stdout": "", "stderr": "timed out"}
    except Exception as exc:
        return {"status": "error", "exit_code": None, "stdout": "", "stderr": str(exc)}


def run_watch(args: List[str], env: Dict[str, str] | None = None) -> Dict[str, Any]:
    """Run gt_watch.py with arbitrary arguments."""
    return _run(args, env)


def watch_add(
    url: str,
    label: str = "",
    track: str = "tags,commits,releases",
    branch: str = "",
    watch_path: str = "",
    current_version: str = "",
    p0_when: str = "",
    gh_repo: str = "",
    slug: str = "",
    vault_path: str = "",
    force: bool = False,
) -> Dict[str, Any]:
    args = ["add", url, "--track", track]
    if label:
        args += ["--label", label]
    if branch:
        args += ["--branch", branch]
    if watch_path:
        args += ["--watch-path", watch_path]
    if current_version:
        args += ["--current-version", current_version]
    if p0_when:
        args += ["--p0-when", p0_when]
    if gh_repo:
        args += ["--gh-repo", gh_repo]
    if slug:
        args += ["--slug", slug]
    if force:
        args.append("--force")
    env = {"GT_VAULT": vault_path} if vault_path else None
    return _run(args, env)


def watch_remove(slug: str, vault_path: str = "") -> Dict[str, Any]:
    env = {"GT_VAULT": vault_path} if vault_path else None
    return _run(["remove", slug], env)


def watch_list(vault_path: str = "") -> Dict[str, Any]:
    env = {"GT_VAULT": vault_path} if vault_path else None
    return _run(["list"], env)


def watch_check(vault_path: str = "") -> Dict[str, Any]:
    env = {"GT_VAULT": vault_path} if vault_path else None
    return _run(["check"], env)


def watch_fetch(vault_path: str = "", force: bool = False) -> Dict[str, Any]:
    args = ["fetch", "--force"] if force else ["fetch"]
    env = {"GT_VAULT": vault_path} if vault_path else None
    return _run(args, env)


def watch_show(slug: str = "", all_events: bool = False, vault_path: str = "") -> Dict[str, Any]:
    args = ["show"]
    if all_events:
        args.append("--all")
    if slug:
        args.append(slug)
    env = {"GT_VAULT": vault_path} if vault_path else None
    return _run(args, env)


def watch_ack(slug: str = "", vault_path: str = "") -> Dict[str, Any]:
    args = ["ack"]
    if slug:
        args.append(slug)
    env = {"GT_VAULT": vault_path} if vault_path else None
    return _run(args, env)


def watch_install_cron(every: str = "1h", vault_path: str = "", state_dir: str = "") -> Dict[str, Any]:
    env = {}
    if vault_path:
        env["GT_VAULT"] = vault_path
    if state_dir:
        env["GT_WATCH_STATE"] = state_dir
    return _run(["install-cron", "--every", every], env if env else None)


def watch_uninstall_cron(vault_path: str = "", state_dir: str = "") -> Dict[str, Any]:
    env = {}
    if vault_path:
        env["GT_VAULT"] = vault_path
    if state_dir:
        env["GT_WATCH_STATE"] = state_dir
    return _run(["uninstall-cron"], env if env else None)
