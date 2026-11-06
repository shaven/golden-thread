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


def _downstream(c):
    """One line for a connection's llm / limits / call timeout, or ''."""
    bits = []
    if c.get("llm_tools"):
        bits.append("llm tools: %s" % c["llm_tools"])
    lim = c.get("limits") or {}
    if lim.get("max_calls_per_min"):
        bits.append("%d calls/min" % lim["max_calls_per_min"])
    if lim.get("max_concurrent"):
        bits.append("%d at once" % lim["max_concurrent"])
    if c.get("call_timeout_s"):
        bits.append("call timeout %gs" % c["call_timeout_s"])
    return ", ".join(bits)


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
        if c.get("locality") == "local":
            line += "  [local]"
        if c.get("provider"):
            line += "  provider %s" % c["provider"]
        out.append(line.rstrip())
        vouched = (c.get("oauth") or {}).get("insecure_metadata_hosts")
        if vouched:                              # 0.5.0: --allow-insecure-metadata is never silent
            out.append("  %s   ! plain-http metadata allowed for %s" % (" " * width, ", ".join(vouched)))
        ob = c.get("oauth") or {}
        if ob.get("refresh_storage") == "store":       # the weaker resting place is never silent
            out.append("  %s   ! refresh token %s" % (" " * width, ob.get("refresh_token")))
        if ob.get("scope_missing"):
            out.append("  %s   ! sign-in granted a narrower scope than requested (missing: %s)"
                       % (" " * width, ", ".join(ob["scope_missing"])))
        for w in c.get("consent_windows") or []:
            out.append("  %s   ! consent window %s open until %s%s" % (
                " " * width, w["id"], _when(w["expires"]),
                " (" + w["op"] + " only)" if w.get("op") else ""))
        if c.get("static_headers"):              # 0.5.1: names only, never values
            out.append("  %s   headers: %s" % (" " * width, ", ".join(c["static_headers"])))
        tc = c.get("trust_ca")
        if tc:                                   # 0.5.1: a private CA is never silent
            bits = ["private CA %s sha256 %s" % (tc.get("name"), tc.get("sha256"))]
            if tc.get("ca_days_left") is not None:
                bits.append("CA %dd" % tc["ca_days_left"])
            if tc.get("leaf_days_left") is not None:
                bits.append("leaf %dd" % tc["leaf_days_left"])
            out.append("  %s   %s" % (" " * width, ", ".join(bits)))
            for w in tc.get("warnings") or []:
                out.append("  %s   WARN %s" % (" " * width, w))
        for s in c.get("sub_endpoints") or []:   # 0.22.0: which backend works (UTC), from real calls only
            pad = " " * width
            if s.get("failing"):
                out.append("  %s   ✗ %s (%s, %s UTC — %d consecutive)" % (
                    pad, s["id"], s.get("last_failure_code") or "failed",
                    (s.get("last_failure") or "")[11:16], s.get("consecutive_failures", 0)))
            else:
                out.append("  %s   ✓ %s (last: %s UTC)" % (pad, s["id"], (s.get("last_success") or "")[11:16]))
        shown = _downstream(c)                   # 0.5.1: a downstream LLM's declared limits
        if shown:
            out.append("  %s   %s" % (" " * width, shown))
        if c.get("fix"):
            out.append("  %s   -> %s" % (" " * width, c["fix"]))
    for n in status.get("notes") or []:          # 0.22.0: a switch that widens access is never silent
        out.append("  ! %s" % n)
    if status.get("approvals_migrated"):         # phase 112: trusted at upgrade, said once, plainly
        out.append("  approvals_migrated: %d stdio connection(s) already registered were trusted "
                   "and recorded as approved at upgrade" % status["approvals_migrated"])
    out.append(summary(status))
    out.append("States come from the gateway's own calls since it started; "
               "`lotr status --check --table` tests every MCP connection now.")
    return "\n".join(out) + "\n"
