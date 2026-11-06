#!/usr/bin/env python3
"""gt_tty -- "is a human at a terminal?", answered the same way on every platform (0.21.4).

Every "needs a person at a terminal" guard in gt (grant, unlock, sign, uninstall, ...) asked
`sys.stdin.isatty()`. On Windows that is True for the NUL device (`cmd < NUL`), because NUL is
a character device: `py gt_grant.py add "tools/ship.sh --apply" 30m < NUL` was granted with no
human present. has_terminal() closes that: on Windows a stream counts only when its handle is a
real console (GetConsoleMode succeeds); elsewhere it is isatty().
"""
import os


def _console_handle(stream):
    import msvcrt
    return msvcrt.get_osfhandle(stream.fileno())


def has_terminal(stream):
    """True when `stream` is an interactive terminal. None / closed / detached -> False."""
    if stream is None:
        return False
    try:
        if not stream.isatty():
            return False
    except (AttributeError, OSError, ValueError):
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        handle = _console_handle(stream)
        mode = wintypes.DWORD()
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetConsoleMode.restype = wintypes.BOOL
        return bool(k32.GetConsoleMode(wintypes.HANDLE(handle), ctypes.byref(mode)))
    except Exception:                                       # noqa: BLE001 -- unknown means no
        return False
