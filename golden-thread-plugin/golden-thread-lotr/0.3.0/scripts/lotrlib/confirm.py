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


def describe(connection, identity, op, args, client):
    """The text the owner sees. Argument values are shown (they say WHICH PR, WHICH mail) but
    credential-shaped ones are replaced and the whole text is capped."""
    shown = args or {}
    if shaping.scan_credentials(shown):
        shown = {k: "<withheld>" for k in shown}
    body = json.dumps(shown, sort_keys=True, default=str)
    if len(body) > 400:
        body = body[:400] + " ..."
    return (f"{client} wants to run a CONSENT operation.\n\n"
            f"Connection: {connection}\nAs: {identity}\nOperation: {op}\nArguments: {body}\n\n"
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
