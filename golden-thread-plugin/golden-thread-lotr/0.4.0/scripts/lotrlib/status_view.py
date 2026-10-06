"""The readable view of `lotr status` (0.4.0, owner 2026-10-06): what is connected to LOTR and
whether each connection works, the way Claude Code's /mcp shows MCP servers. Used by
`lotr status --table` and by gt's `/gt:gt-settings lotr`, so both show the same thing.
"""

MARK = {"connected": "✓", "needs sign-in": "⚠", "error": "✗", "disabled": "–",
        "not used yet": "○"}
ORDER = ("error", "needs sign-in", "connected", "not used yet", "disabled")


def _when(ts):
    """'2026-10-06T07:50:12' -> '10-06 07:50' (short, still unambiguous within a year)."""
    if not ts:
        return ""
    return ts[5:10] + " " + ts[11:16]


def summary(status):
    """One line: 'LOTR: 3 connected, 1 needs sign-in, 1 error' (states with no connection left out)."""
    if not status.get("ok"):
        return "LOTR: the gateway did not answer (%s)" % (status.get("error") or {}).get("code", "?")
    counts = {}
    for c in status.get("connections") or []:
        counts[c.get("state", "not used yet")] = counts.get(c.get("state", "not used yet"), 0) + 1
    parts = ["%d %s" % (counts[s], s) for s in ORDER if counts.get(s)]
    return "LOTR: " + (", ".join(parts) if parts else "no connections")


def render(status):
    if not status.get("ok"):
        err = status.get("error") or {}
        return ("LOTR: the gateway did not answer: %s %s\n" % (err.get("code", "?"),
                                                              err.get("message", ""))
                + "".join("  %s\n" % h for h in err.get("hints") or []))
    conns = sorted(status.get("connections") or [],
                   key=lambda c: (ORDER.index(c.get("state")) if c.get("state") in ORDER else 9,
                                  c.get("id", "")))
    width = max([len(c.get("id", "")) for c in conns] + [10])
    out = ["LOTR gateway: zone %s, mode %s, %d connection(s)"
           % (status.get("zone"), status.get("mode"), len(conns))]
    for c in conns:
        st = c.get("state", "not used yet")
        line = "  %s %-*s  %-13s" % (MARK.get(st, "?"), width, c.get("id", ""), st)
        err = c.get("last_error")
        if st in ("error", "needs sign-in") and err:
            line += "  since %s (%s)" % (_when(err.get("at")), err.get("code"))
        elif c.get("last_ok"):
            line += "  last ok %s" % _when(c.get("last_ok"))
        if c.get("checked"):
            line += "  [checked now]"
        out.append(line.rstrip())
        if c.get("fix"):
            out.append("  %s   -> %s" % (" " * width, c["fix"]))
    out.append(summary(status))
    out.append("States come from the gateway's own calls since it started; "
               "`lotr status --check --table` tests every MCP connection now.")
    return "\n".join(out) + "\n"
