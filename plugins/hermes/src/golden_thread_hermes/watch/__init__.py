"""gt-watch wrapper for Hermes.

The real watch engine is `gt_watch.py`, vendored from upstream Golden Thread v0.21.0.
Hermes exposes `gt_watch` as a tool that writes watch notes to the vault and runs
check/fetch/list/ack via this module. The scheduled fetch is installed separately
through a cron job (see `gt_watch.py install-cron`).
"""

from .watch import (
    run_watch,
    watch_add,
    watch_remove,
    watch_list,
    watch_check,
    watch_fetch,
    watch_show,
    watch_ack,
    watch_install_cron,
    watch_uninstall_cron,
)

__all__ = [
    "run_watch",
    "watch_add",
    "watch_remove",
    "watch_list",
    "watch_check",
    "watch_fetch",
    "watch_show",
    "watch_ack",
    "watch_install_cron",
    "watch_uninstall_cron",
]
