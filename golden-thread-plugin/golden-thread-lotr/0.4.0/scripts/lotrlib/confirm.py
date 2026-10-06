"""Owner confirmation for consent-tier operations, raised by the DAEMON (ADR-5).

Why the daemon and not Claude Code: Claude Code's prompt guards only its own MCP tool call.
Anything running as the owner -- including the assistant through Bash -- can reach the socket
or run `lotr consent` directly, and no Claude Code prompt stands in that path. The one place
every consent-tier call must pass is here, so the confirmation lives here.

Modes (gateway.json `local.confirm`):
  auto    a native macOS dialog on darwin; refuse everywhere else       (default)
  dialog  the native dialog; refuse if it cannot be shown
  refuse  every consent-tier op is refused
  none    no confirmation -- an explicit, recorded choice to rely on the caller's own prompt
  biometric (0.3.0) require gt unlock's PLATFORM consent (Touch ID / Windows Hello over the op,
          raised by the unlock authority; engine._consent); refuse whenever it is unavailable.
          Never falls back to the dialog: a screen-control agent can click a dialog, it cannot
          touch a sensor.

The dialog text is passed to osascript as an ARGUMENT, never spliced into the script, so
text from a downstream (a PR title, a mail subject) cannot become AppleScript. It defaults to
Deny and gives up after 60 seconds, which counts as Deny.
"""
import json
import re
import subprocess
import sys

from . import shaping
from .errors import GatewayError

MODES = ("auto", "dialog", "refuse", "none", "biometric")
OSASCRIPT = "/usr/bin/osascript"
TIMEOUT_S = 60

_SCRIPT = [
    "on run argv",
    'display dialog (item 1 of argv) with title "gt-lotr: confirm" '
    'buttons {"Deny", "Allow"} default button "Deny" cancel button "Deny" '
    f"with icon caution giving up after {TIMEOUT_S}",
    'if gave up of result then error "timeout"',
    "end run",
]


_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")


def _clean(v, n=120):
    t = " ".join(_CTRL.sub(" ", str(v)).split())
    return t if len(t) <= n else t[:n - 1] + "\u2026"


def _addresses(recips, n=5):
    got = []
    for r in recips if isinstance(recips, list) else []:
        a = ((r or {}).get("emailAddress") or {}).get("address") if isinstance(r, dict) else None
        if a:
            got.append(_clean(a, 80))
    more = " (+%d more)" % (len(got) - n) if len(got) > n else ""
    return ", ".join(got[:n]) + more


def summarize(op, args):
    """-> [line]: WHAT a known consent op will do (recipients and subject of a mail, the PR a
    merge targets), shown before the arguments are truncated. Mirror of gt core's
    gt_unlockd_methods.consent_summary (the platform route); tests pin one output."""
    args = args if isinstance(args, dict) else {}
    lines = []
    if op == "send_mail":
        m = args.get("message") if isinstance(args.get("message"), dict) else {}
        for label, key in (("To", "toRecipients"), ("Cc", "ccRecipients"),
                           ("Bcc", "bccRecipients")):
            a = _addresses(m.get(key))
            if a:
                lines.append("%s: %s" % (label, a))
        if not any(l.startswith("To:") for l in lines):
            lines.insert(0, "To: (no recipients)")
        lines.append("Subject: %s" % _clean(m.get("subject") or "(none)"))
    elif op == "merge_pull":
        target = "%s/%s#%s" % (_clean(args.get("owner") or "?", 60),
                               _clean(args.get("repo") or "?", 80),
                               _clean(args.get("pull_number") or "?", 12))
        how = _clean(args.get("merge_method") or "merge", 12)
        sha = _clean(args.get("sha"), 12) if args.get("sha") else "any"
        lines.append("Merge: %s (%s, head %s)" % (target, how, sha))
    return lines


def describe(connection, identity, op, args, client):
    """The text the owner sees. A known consent op is summarised first (recipients, subject,
    the PR), so the 400-character cap on the raw arguments never hides WHO or WHAT; argument
    values are shown, credential-shaped ones replaced (and then there is no summary)."""
    shown = args or {}
    secret = bool(shaping.scan_credentials(shown))
    if secret:
        shown = {k: "<withheld>" for k in shown}
    summary = "" if secret else "".join(l + "\n" for l in summarize(op, args))
    body = json.dumps(shown, sort_keys=True, default=str)
    if len(body) > 400:
        body = body[:400] + " ..."
    return (f"{client} wants to run a CONSENT operation.\n\n"
            f"Connection: {connection}\nAs: {identity}\nOperation: {op}\n{summary}"
            f"Arguments: {body}\n\n"
            "Allow only if you asked for this.")


def _dialog(text):
    try:
        r = subprocess.run([OSASCRIPT, *[a for line in _SCRIPT for a in ("-e", line)], text],
                           capture_output=True, text=True, timeout=TIMEOUT_S + 15)
    except (OSError, subprocess.TimeoutExpired):
        return None                      # could not ask: treated as not confirmed
    return r.returncode == 0 and "Allow" in (r.stdout or "")


def confirm(mode, text, *, dialog=None):
    """Return normally when confirmed; raise GatewayError otherwise."""
    if mode not in MODES:
        raise GatewayError("settings_invalid", f"local.confirm must be one of {', '.join(MODES)}")
    if mode == "none":
        return
    if mode == "biometric":
        # Reached only when the engine's platform route did not approve: refuse, never ask a
        # weaker way.
        raise GatewayError("consent_refused", "local.confirm is 'biometric' and gt unlock's "
                           "platform confirmation is not available")
    if mode == "refuse":
        raise GatewayError("consent_refused", "consent-tier operations are refused on this gateway",
                           hints=["set local.confirm to 'dialog' (macOS) to allow them with your confirmation"])
    if mode == "auto" and sys.platform != "darwin":
        raise GatewayError("consent_refused",
                           "no way to ask the owner on this platform, so consent-tier operations are refused",
                           hints=["set local.confirm to 'none' only if every caller prompts on its own"])
    answer = (dialog or _dialog)(text)
    if answer is None:
        raise GatewayError("consent_unconfirmed", "the confirmation dialog could not be shown")
    if not answer:
        raise GatewayError("consent_denied", "the owner denied this operation (or did not answer in time)")
