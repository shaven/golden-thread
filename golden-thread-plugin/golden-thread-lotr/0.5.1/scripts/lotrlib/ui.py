"""Friendly terminal output for the commands people run by hand (0.5.1).

A terminal gets a short page: a clear headline, the facts that matter, what is still to do, and the
next command to try, in colour. A pipe, a test, `--json`, NO_COLOR and TERM=dumb get what they got
before. Nothing here ever receives a secret; every line fits an 80-column window (78 with margin).

    use_color(stream)      colour only on a real terminal (FORCE_COLOR forces it on)
    stdout_is_tty()        whether to show a page instead of JSON (patched in tests)
    Panel(title, ok).row / .todo / .next / .render(color)
    error_panel(GatewayError | message, hints)
    view_for(command, result) -> Panel | None
"""
import os
import sys
import textwrap

WIDTH = 78

_CODES = {"reset": "0", "bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33",
          "cyan": "36", "boldgreen": "1;32", "boldred": "1;31"}


def use_color(stream=None):
    stream = stream if stream is not None else sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def stdout_is_tty():
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def paint(text, style, on):
    return "\x1b[%sm%s\x1b[0m" % (_CODES[style], text) if on and text else text


def short_path(path):
    """A path with the home folder shown as ~ (shorter, and kinder to read)."""
    p = str(path)
    home = os.path.expanduser("~")
    return "~" + p[len(home):] if home and p.startswith(home) else p


def _wrap(text, first, rest, width=WIDTH):
    """Wrap to `width`, breaking an over-long word rather than overflowing the window."""
    return textwrap.wrap(text, width=width, initial_indent=first, subsequent_indent=rest,
                         break_long_words=True, break_on_hyphens=False) or [first.rstrip()]


class Panel:
    def __init__(self, title, ok=True):
        self.title, self.ok = title, ok
        self.rows, self.todos, self.nexts, self.notes = [], [], [], []

    def row(self, label, value):
        self.rows.append((label, str(value)))
        return self

    def note(self, text):
        self.notes.append(text)
        return self

    def todo(self, text):
        self.todos.append(text)
        return self

    def next(self, text):
        self.nexts.append(text)
        return self

    def render(self, color=None, width=WIDTH):
        on = use_color(sys.stdout) if color is None else color
        out = []
        mark, style = ("✓", "boldgreen") if self.ok else ("✗", "boldred")
        head = _wrap(self.title, "  %s " % mark, "    ", width)
        out.append("")
        out.append(paint(head[0][:3], style, on) + paint(head[0][3:], "bold", on))
        out.extend(paint(h, "bold", on) for h in head[1:])
        if self.rows:
            out.append("")
            lw = min(max(len(l) for l, _ in self.rows), 14)
            for label, value in self.rows:
                lines = _wrap(value, "    " + label.ljust(lw) + "  ", "    " + " " * lw + "  ", width)
                first = lines[0]
                cut = 4 + lw
                out.append(paint(first[:cut], "dim", on) + first[cut:])
                out.extend(lines[1:])
        for n in self.notes:
            out.append("")
            out.extend(_wrap(n, "    ", "    ", width))
        if self.todos:
            out.append("")
            out.append(paint("  Still to do" if self.ok else "  What to try", "yellow", on))
            for t in self.todos:
                lines = _wrap(t, "    • ", "      ", width)
                out.extend(lines)
        if self.nexts:
            out.append("")
            for n in self.nexts:
                lines = _wrap(n, "  → ", "    ", width)
                out.append(paint(lines[0], "cyan", on))
                out.extend(paint(l, "cyan", on) for l in lines[1:])
        out.append("")
        return "\n".join(out) + "\n"


def error_panel(err, hints=None):
    """A GatewayError (or a plain message) as a page: what happened, then what to try."""
    msg = getattr(err, "message", None) or str(err)
    hints = hints if hints is not None else list(getattr(err, "hints", None) or [])
    p = Panel(msg, ok=False)
    code = getattr(err, "code", None)
    if code:
        p.row("Code", code)
    for h in hints:
        p.todo(h)
    return p


# -- one page per command -----------------------------------------------------------------------

def _app_connect(r):
    if r.get("verify"):
        v = r["verify"]
        return Panel("%s can reach your vault: the hub listed %d tools" % (r["app"], v["tools"]),
                     True).next("Next: point the app at the endpoint and try it.")
    again = r.get("already_connected")
    p = Panel("%s is %sconnected to your vault" % (r["app"], "already " if again else ""), True)
    p.row("Endpoint", r["endpoint"])
    p.row("Secret file", short_path(r["secret_file"]) + "  (only you can read it)")
    p.row("Reaches", ", ".join(r.get("allow") or []) + ", up to " + str(r.get("max_tier")))
    p.row("Headers", "Authorization: Bearer <what is in the secret file>")
    p.row("", "X-GT-Client: " + r["app"])
    p.note("The secret is in that file and nowhere else: this page never shows it, and it is never "
           "put in a web address.")
    for t in r.get("remaining") or []:
        p.todo(t)
    p.next("Check it works:  " + r.get("verify_command", "lotr app-connect %s --verify" % r["app"]))
    p.next("Undo it later:   lotr app-disconnect " + r["app"])
    return p


def _app_disconnect(r):
    p = Panel("%s is disconnected" % r["app"], True)
    p.row("Access", "revoked, and its secret file is deleted")
    for k in r.get("kept") or []:
        p.row("Kept", "%s (%s)" % (k["connection"], k["why"]))
    for c in r.get("removed") or []:
        p.row("Removed", c + " (nothing else uses it)")
    p.next("Connect it again any time:  lotr app-connect %s --vault <your vault>" % r["app"])
    return p


def _grant(r):
    if "window" in r:
        w = r["window"]
        p = Panel("Consent window open for %s" % w["connection"], True)
        import time as _t
        p.row("Ends", _t.strftime("%H:%M on %b %d", _t.localtime(w["expires_at"])))
        p.row("Covers", (w["op_filter"] + " only") if w.get("op_filter")
              else "every consent-tier action on it")
        p.note("Until it ends, those actions run WITHOUT asking you each time. "
               "Open one only when you are about to do the work.")
        p.next("End it early:  lotr grant revoke " + w["id"])
        return p
    if "revoked" in r:
        return Panel("Window %s closed" % r["revoked"], True).next("Back to asking every time.")
    wins = r.get("windows") or []
    if not wins:
        return Panel("No consent windows are open", True).next(
            "Open one:  lotr grant open CONNECTION --for 30m")
    p = Panel("%d consent window%s open" % (len(wins), "" if len(wins) == 1 else "s"), True)
    import time as _t
    for w in wins:
        p.row(w["id"], "%s until %s%s" % (w["connection"],
              _t.strftime("%H:%M", _t.localtime(w["expires_at"])),
              (", " + w["op_filter"] + " only") if w.get("op_filter") else ""))
    p.next("Close one:  lotr grant revoke ID")
    return p


_VIEWS = {"cmd_app_connect": _app_connect, "cmd_app_disconnect": _app_disconnect, "cmd_grant": _grant}


def view_for(command, result):
    fn = _VIEWS.get(command)
    if fn is None or not isinstance(result, dict) or result.get("ok") is False:
        return None
    return fn(result)
