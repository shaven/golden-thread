"""What the gateway says about a busy, slow or failing downstream, and its own optional brakes.

Downstream LLMs (an HTTP model server, a hosted API behind http-rest, an MCP tool that answers a
chat request) are slow, rate limited and expensive in ways a GitHub read is not. This module holds
the pieces the engine and the HTTP connection share, so a new provider or status is data here:

  STATUS_CODES / STATUS_CLASSES   upstream HTTP status -> gateway error code
  parse_retry_after               a Retry-After header -> whole seconds, or None
  UpstreamError                   a GatewayError whose error dict also carries `upstream_status`
                                  and `retry_after_s` (numbers only)
  map_downstream                  best-effort: an MCP tool's rate-limit text -> the same code
  timeouts_problem / limits_problem / llm_problem
                                  the one definition of the connection fields connect.timeouts,
                                  limits and llm (registry.py and conn_templates.py both call them)
  CallLimiter                     optional per-connection calls-per-minute and concurrency caps

The gateway never sleeps or retries on its own. It reports; the caller decides.
"""
import math
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone

from .errors import GatewayError

# -- upstream status -> gateway error code (data, not code) ------------------------------------
STATUS_CODES = {429: "upstream_rate_limited", 408: "upstream_timeout", 504: "upstream_timeout",
                402: "upstream_payment_required"}     # 402: a hosted LLM out of credit (0.5.1)
STATUS_CLASSES = {5: "upstream_error"}              # the first digit of the status
RETRY_AFTER_MAX_S = 3600                            # gateway ceiling on a Retry-After it reports
UPSTREAM_CODES = frozenset(STATUS_CODES.values()) | frozenset(STATUS_CLASSES.values())
LOCAL_LIMIT_CODE = "rate_limited_locally"

_DELTA = re.compile(r"\d{1,9}")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
# IMF-fixdate, the one form servers send today: "Sun, 06 Nov 1994 08:49:37 GMT". Parsed by hand so a
# non-English locale cannot change what a month name means.
_HTTP_DATE = re.compile(r"[A-Za-z]{3}, (\d{2}) ([A-Za-z]{3}) (\d{4}) (\d{2}):(\d{2}):(\d{2}) GMT")


def code_for_status(status):
    """The gateway code for an upstream HTTP status, or None (the caller keeps `http_<status>`)."""
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    return STATUS_CODES.get(status) or STATUS_CLASSES.get(status // 100)


def parse_retry_after(value, now=None):
    """A Retry-After header as whole seconds, 0 to RETRY_AFTER_MAX_S, or None for anything else.
    Both forms of RFC 9110: delta-seconds, and an HTTP-date (taken relative to `now`)."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v or len(v) > 64:
        return None
    if _DELTA.fullmatch(v):
        return min(int(v), RETRY_AFTER_MAX_S)
    m = _HTTP_DATE.fullmatch(v)
    if not m or m.group(2).lower() not in _MONTHS:
        return None
    try:
        when = datetime(int(m.group(3)), _MONTHS.index(m.group(2).lower()) + 1, int(m.group(1)),
                        int(m.group(4)), int(m.group(5)), int(m.group(6)), tzinfo=timezone.utc)
    except ValueError:
        return None
    now = time.time() if now is None else now
    return int(min(max(math.ceil(when.timestamp() - now), 0), RETRY_AFTER_MAX_S))


class UpstreamError(GatewayError):
    """A GatewayError that also reports, as plain numbers, what the provider said: the HTTP
    status it answered and how long it asked us to wait. Both are optional; neither is text."""

    def __init__(self, code, message, hints=None, *, upstream_status=None, retry_after_s=None):
        super().__init__(code, message, hints)
        self.upstream_status = upstream_status
        self.retry_after_s = retry_after_s

    def to_dict(self):
        d = super().to_dict()
        if self.retry_after_s is not None:
            d["retry_after_s"] = self.retry_after_s
        if self.upstream_status is not None:
            d["upstream_status"] = self.upstream_status
        return d


def from_status(status, message, retry_after_s=None, hints=()):
    """An UpstreamError for a mapped status (see code_for_status), else None."""
    code = code_for_status(status)
    if code is None:
        return None
    return UpstreamError(code, message, list(hints), upstream_status=status,
                         retry_after_s=retry_after_s)


# -- MCP: no header exists, so this is a best-effort reading of the tool's own error text ------
# Documented in SPEC.md. A tool that says something else (or nothing) stays `downstream_error`.
RATE_LIMIT_PHRASES = ("rate limit", "rate-limit", "ratelimit", "too many requests", "quota")
_STATUS_429 = re.compile(r"(?<![\w.])429(?![\w.])")
_PREFIX = re.compile(r"^.*? failed: ", re.S)       # "<connection>: <tool> failed: <the tool's words>"


def rate_limit_text(text):
    """True when an MCP tool's error text reads like a rate limit or quota refusal."""
    t = str(text or "").lower()
    return any(p in t for p in RATE_LIMIT_PHRASES) or bool(_STATUS_429.search(t))


def map_downstream(conn, err):
    """`err` as an upstream_rate_limited UpstreamError when it is an MCP tool failure whose own
    text reads like a rate limit (no retry_after_s: MCP has no standard header); else `err`."""
    if (conn or {}).get("kind") != "mcp" or getattr(err, "code", None) != "downstream_error":
        return err
    body = _PREFIX.sub("", str(err.message), count=1)       # the connection and tool names do not count
    if not rate_limit_text(body):
        return err
    return UpstreamError("upstream_rate_limited",
                         "the tool reported a rate limit or quota refusal",
                         ["the provider is busy, not down; wait and retry later",
                          "no wait time is known for an MCP tool; the gateway does not retry"])


# -- the connection fields --------------------------------------------------------------------
HARD_CALL_TIMEOUT_S = 3600           # no timeout, in a connection or a setting, goes above this
DEFAULT_MAX_CALL_TIMEOUT_S = 300     # the gateway ceiling on a per-call timeout unless gateway.json says
MIN_CALL_TIMEOUT_S = 1
LIMIT_MAX = 1_000_000
LLM_TOOLS = ("none", "read", "write")


def _number(v):
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)


def timeouts_problem(block):
    """None, or why connect.timeouts is wrong for an http connection."""
    if not isinstance(block, dict) or set(block) - {"call_s"}:
        return "timeouts may only hold call_s on an http connection"
    if "call_s" in block:
        v = block["call_s"]
        if not _number(v) or not MIN_CALL_TIMEOUT_S <= v <= HARD_CALL_TIMEOUT_S:
            return "timeouts.call_s must be a number of seconds from %d to %d" % (
                MIN_CALL_TIMEOUT_S, HARD_CALL_TIMEOUT_S)
    return None


def limits_problem(block):
    """None, or why a connection's `limits` is wrong."""
    if not isinstance(block, dict) or set(block) - {"max_calls_per_min", "max_concurrent"}:
        return "limits may only hold max_calls_per_min and max_concurrent"
    for k, v in block.items():
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= LIMIT_MAX:
            return "limits.%s must be a whole number from 1 to %d" % (k, LIMIT_MAX)
    return None


def llm_problem(block):
    """None, or why a connection's `llm` is wrong."""
    if not isinstance(block, dict) or set(block) != {"tools"} or block["tools"] not in LLM_TOOLS:
        return "llm must be {\"tools\": %s}" % " | ".join(LLM_TOOLS)
    return None


def call_ceiling(limits):
    """The gateway ceiling on a per-call timeout, from gateway.json `limits` or the default."""
    v = (limits or {}).get("max_call_timeout_s", DEFAULT_MAX_CALL_TIMEOUT_S)
    if not _number(v):
        v = DEFAULT_MAX_CALL_TIMEOUT_S
    return min(max(v, MIN_CALL_TIMEOUT_S), HARD_CALL_TIMEOUT_S)


PROVIDER_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,63}$")


def provider_problem(value):
    """None when `value` is a usable provider label (a short free string such as "acme-cloud" or
    "llama.cpp"; it is shown, never interpreted), else why not."""
    if not isinstance(value, str) or not PROVIDER_RX.match(value):
        return "provider must be a short label: letters, digits and . _ : / + - , 1 to 64 characters"
    return None


def clamp_timeout(value, ceiling=DEFAULT_MAX_CALL_TIMEOUT_S):
    """`value` seconds held to [1, ceiling]; a number that is not one is a ValueError."""
    if not _number(value):
        raise ValueError("timeout_s must be a number of seconds")
    return max(MIN_CALL_TIMEOUT_S, min(value, ceiling))


# -- optional per-connection brakes (off unless the connection has `limits`) ------------------

class _Slot:
    def __init__(self, release):
        self._release = release

    def release(self):
        rel, self._release = self._release, None
        if rel:
            rel()


_NO_SLOT = _Slot(None)


class CallLimiter:
    """Calls per minute (a sliding 60 s window) and calls in flight, per connection id. Only a
    connection that declares `limits` is ever counted; a refused call uses no slot."""

    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._stamps = {}          # connection id -> deque of call times
        self._busy = {}            # connection id -> calls in flight

    def acquire(self, cid, limits):
        """A slot to release when the call ends, or GatewayError rate_limited_locally."""
        if not limits:
            return _NO_SLOT
        per_min, cap = limits.get("max_calls_per_min"), limits.get("max_concurrent")
        with self._lock:
            now = self._clock()
            stamps = self._stamps.setdefault(cid, deque())
            while stamps and stamps[0] <= now - 60:
                stamps.popleft()
            if cap and self._busy.get(cid, 0) >= cap:
                raise self._refusal("%s already has %d call(s) in flight (limits.max_concurrent)"
                                    % (cid, cap), 1)
            if per_min and len(stamps) >= per_min:
                wait = max(1, math.ceil(stamps[0] + 60 - now))
                raise self._refusal("%s has used its %d calls per minute (limits.max_calls_per_min)"
                                    % (cid, per_min), wait)
            if per_min:
                stamps.append(now)
            if cap:
                self._busy[cid] = self._busy.get(cid, 0) + 1
        if not cap:
            return _NO_SLOT
        return _Slot(lambda: self._done(cid))

    def _done(self, cid):
        with self._lock:
            self._busy[cid] = max(0, self._busy.get(cid, 0) - 1)

    @staticmethod
    def _refusal(message, wait):
        return UpstreamError(LOCAL_LIMIT_CODE, message,
                             ["this is the gateway's own limit on the connection, not the provider's",
                              "retry after retry_after_s; the gateway does not retry"],
                             retry_after_s=wait)
