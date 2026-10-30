#!/usr/bin/env python3
"""gt_savings.py -- what each gt mechanism actually used vs what it would have used without it.

    gt_savings.py record   [--hook] [--days N] [--projects-dir DIR] [--max-seconds S]
    gt_savings.py ingest-run --since ISO [--session ID ...] [--run ID] [--projects-dir DIR] [--json]
    gt_savings.py note-read --mechanism gt-open|digest --read FILE... --instead FILE...
    gt_savings.py benchmark --task LABEL --with-gt TOKENS --without-gt TOKENS
    gt_savings.py report   [--days N | --since D --until D] [--project ID] [--profile P] [--full] [--json]
    gt_savings.py net      [--days N]            # one line: the net figure, for gt-usage
    gt_savings.py farm     [--days N | --since D --until D] [--json]   # the farm section alone
    gt_savings.py overhead [--days N] [--save] [--json]     # what a subagent costs just to start
    gt_savings.py net-check [--json]             # trailing 7-day net per mechanism; writes the pause state
    gt_savings.py export   [--days N] [--out FILE]
    gt_savings.py projects                       # the opaque project ids the ledger holds

THE LEDGER, ~/.claude/golden-thread/usage/savings.jsonl, beside readings.jsonl. Append-only, one
line per mechanism event, on this machine only. It holds numbers and identifiers and nothing
else: no prompt text, no file content, no path, no project name (a project is an 8-character
hash of its transcript directory). It is never written into a vault or a git repo, and no
figure is ever sent anywhere: sharing is `export`, a file the user chooses to hand on.

WHERE THE NUMBERS COME FROM. Claude Code transcripts (~/.claude/projects/**/*.jsonl, subagent
transcripts included) already carry per-request `usage`: input, output, cache read, cache
creation. `record` reads them with the same reader rules as gt_optimize_session.read_turns --
one request is one turn however many streamed rows repeat it, and a half-written last line is
skipped -- plus the output tokens and the tool-result sizes that reader does not keep. It adds
no model call and prints nothing, so it adds no text to a session.

EVERY FIGURE CARRIES ITS VERIFICATION STATE and the report never promotes one silently:
    measured   straight from transcript usage fields, or a head-to-head `benchmark`
               (the Core-rules row applies a benchmarked factor to measured usage: estimated)
    estimated  derived from measured inputs (a counterfactual price, characters / 4)
    design     a claim with no measurement behind it -- shown as a claim, never as a number
A row's state is the WEAKEST state among its events.

UNITS. Savings are in token-equivalents at the session's model: a request's tokens are weighted
by what each kind costs (input 1, output 5, cache read 0.1, cache write 1.25 for five minutes
and 2.0 for an hour -- the multipliers gt_optimize_session uses; output at 5x input is the
published ratio, an assumption here) and scaled by that model's list price over the session
model's. So an agent run on a cheaper model than the session's is a saving even though it used
the same tokens. USD is the same figure at list price: on a subscription (savings_billing subscription,
the default) an equivalent, not a bill; on API billing (savings_billing api) spend avoided. Prices come
from gt_model_prices.json, one table, so a price change re-prices history without re-recording.

PROFILES. `--profile` re-prices the SAME recorded work: inherit prices every run at the session
model, very-high at opus, average at the model its tier (haiku / sonnet / opus) maps to. Only
the counterfactual columns move; the actual columns are read from the ledger and never recomputed.
Effort has no per-call token field, so its effect is not claimed.

FARMED PACKETS (0.1.10). gt-farm's gt_farm_send.py appends one `farm` line per packet it sent to
an engine (state `measured` when the engine reported `usage`, `estimated` when it did not) and per
packet a person carried (`relay: true`, always `estimated`): service, model, local, prompt and
completion tokens, wall seconds, rate_per_mtok and usd_avoided. The `farm` row of the table takes
the line's own actual/counterfactual (a local engine's tokens are all saved; another service's
are off Claude but not free, so they save nothing in tokens) and its usd_avoided at the row's
reference rate. The row is NET (0.22.0): its actual also holds what the farming cost Claude
(`claude_overhead`, the send; `main_context_tokens`, a local review's output in the session), and its USD
also takes off `claude_spent_usd` (a calibration's reference answers). A "Farmed savings by purpose"
table under the main one shows what the net is made of. The farm section under the table breaks it down per engine and model, shows the
local share, measured against estimated, wall time and throughput, cost avoided ("-" with no
reference rate), sent against relayed, a trend over the window, and the share of the window's
work kept off Claude: farmed tokens weighted like Claude's (prompt x1, completion x5) against the
main-session and agent work `record` measured. That share is `estimated`; it is never turned into
a percent of the 5-hour or weekly allowance, because how tokens count against a plan is
undocumented (the gt-usage skill's rule). A line that is not JSON, or a farm line without whole
non-negative token counts, is skipped and counted, never guessed. Failures and retries are shown
only when a line records them; gt-farm 0.21.1 writes no line for a failed or rejected packet.

FARM LINES BY PURPOSE (gt 0.21.4). A `farm` line may carry `purpose`: `review` (the local second opinion in the
commit step) or `calibration` (an engine's onboarding calibration); a line with none is a packet, as before. A
review line adds `kept_out_tokens` (what Claude did not have to read or write) and `main_context_tokens` (what
reached the session instead, estimated from the printed text); a calibration line claims NO saving (its actual
and counterfactual are 0) and carries `claude_spent_usd`, the estimated Claude cost of its reference answers and
judge. The FARM section gains a by-purpose block when any line has a purpose; the figures keep their state --
an estimated line is never counted as measured -- and a field that is not a whole non-negative count is ignored,
never trusted.

LOTR FIND BUDGET (gt 0.21.4, gt-lotr 0.5.0). gt-lotr's daemon appends one `lotr-find` line per find
that searched for something (state `estimated`, characters / 4): actual_eq is the tokens of the find
result the model received, counterfactual_eq the tokens of every operation of the same connections
loaded as full tool definitions, the way a client that connects to those MCP servers directly would
carry them. The line holds numbers and counts and nothing else (no query, no connection id, no
argument). It is written only when this folder exists and `savings_ledger` is not off; the `lotr`
row (the measured 25-query Jira session) is a different claim and stays as it is.

VAULT ANSWERS (gt 0.21.4). gt's gt_vault_answer.py appends one `vault-answer` line per /gt:gt-query
question it tried to answer from a local engine (setting `vault_answers`, local-first): state
`estimated`, actual_eq the tokens of the short answer and sources the session was shown,
counterfactual_eq the tokens of the cited files it would otherwise have read (each capped at 6000,
characters / 4). An escalation (the engine was unsure, a quote did not check out, no engine) is a line
with counterfactual 0, so what it cost the session shows as a cost, never as a saving. The line holds
numbers, the engine's service name and a reason code: no question, no path, no file text.
`gt_vault_answer.py stats` gives the local hit rate over a window.

NET PER MECHANISM (gt 0.21.4, "keeping the main conversation small"). Keeping text out of the main
conversation is not free: a subagent opens its own context before it reads a byte, a spill file the
model reads back puts the text in anyway, an audit call is a call. So the report ends its table
with a NET section: per mechanism, the tokens kept out of the main context, minus the tokens the
mechanism itself spent on Claude, and what is left. All `estimated`:
    agent-isolation  the main-session path it replaced, against the agent's own usage. kept =
                     (tool output the agent read - the answer it returned) x what carrying it would
                     have cost the main session (written once at 1.25, re-read at 0.1 on each later
                     turn until the next compaction: the turns are COUNTED in the session's transcript,
                     capped at REREAD_CAP, REREAD_DEFAULT when no timestamp places the agent)
                     + the agent's output tokens x 5 (the main session would have written them)
                     + the model calls the agent made beyond its first and last, each re-reading the main
                     session's context at 0.1 (the main session would have made them: "main_ctx" is the
                     size of its last call before the agent started). spent = the agent's whole usage,
                     priced at the session model. Without the last two terms a long agent loop in a big
                     context looks like a loss it is not: an early version without them read -106M
                     token-equivalents for the owner's own week (68 agent runs); with them it reads
                     +141M, 57 of the 68 agents ahead (2026-10-15, estimated).
    spill            kept = the output's tokens minus the preview's (counted once); spent = the
                     tokens of spill files the model read back (main-session tool results for a
                     tool call that names the spill folder). The read-back is also an actual of
                     the row, so the table's `saved` column is net of it too.
    farm             kept = the packet's tokens off Claude when the engine is local; spent = a
                     `spent_eq` the line may carry (a reference or audit call), else 0.
    core-rules       kept = the rework avoided; spent = the injection overhead (the rules_injection
                     setting changes what that is: see injection_eq).
    other rows       spent 0: their own `actual` already holds what they put in the context.
TOTAL: the main table's TOTAL row is the net: what the rows kept out of context minus what gt itself spent (its
overhead). `design` rows are not in it, and a row shown beside others (rules sent once) is not added. The
per-mechanism NET view and the export print no total: those rows have different counterfactuals, and
adding them would count one thing twice.
A mechanism gt routes by itself (PAUSEABLE: agent-isolation, spill, farm) whose trailing 7-day net
is negative over at least PAUSE_MIN_EVENTS events is FLAGGED in the report and written to
~/.claude/golden-thread/usage/net-paused.json, which gt_break_even.paused() reads: the spill hook
stands down while spill is paused, and an auto-router (farm_first auto, where that setting exists)
asks before it routes. The state is rewritten at each session start, so it lifts by itself once the
window turns; with savings_ledger off `record` removes it and gt_break_even ignores it (0.21.4).
Advisory everywhere else: nothing is blocked.

    gt_savings.py overhead [--days N] [--save] [--json] [--projects-dir DIR]
        the opening context of each subagent (its first request: system prompt, tools,
        instructions, before it has read anything), by agent type, from the transcripts; `--save`
        stores it for gt_break_even.py, which turns it into the size at which a subagent pays.
    gt_savings.py net-check [--json]     the trailing 7-day net of each mechanism; writes the pause state

Setting `savings_ledger` (on, the default | off) in module.json. Off stops all recording; the
report says so and still shows what was recorded earlier.

Exit: 0 ok | 2 usage | 4 nothing recorded to report.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, ".claude", "golden-thread", "usage")
LEDGER = os.path.join(BASE, "savings.jsonl")
STATE = os.path.join(BASE, "savings.state.json")
SCHEMA = 1
GT_VERSION = "0.21.0"

def _script_dirs():
    """Where gt's own scripts may be: the hooks dir, this tree's release, the installed cache
    (gt_optimize_session.py is not copied to the hooks dir, only to the plugin cache)."""
    out = [os.path.join(HOME, ".claude", "golden-thread", "hooks"), HERE]
    # this tree's gt releases, newest first: a pinned version directory goes stale the day it is pruned
    tree = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "golden-thread")
    cache = os.path.join(HOME, ".claude", "plugins", "cache", "golden-thread-plugin", "gt")
    for root in (tree, cache):
        try:
            vers = sorted((v for v in os.listdir(root) if v.replace(".", "").isdigit()),
                          key=lambda v: tuple(int(x) for x in v.split(".")), reverse=True)
            out += [os.path.join(root, v, "scripts") for v in vers]
        except (OSError, ValueError):
            pass
    return out


for _p in reversed(_script_dirs()):
    if os.path.isdir(_p) and os.path.isfile(os.path.join(_p, "gt_optimize_session.py")) \
            and _p not in sys.path:
        sys.path.insert(0, _p)
for _p in _script_dirs():
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.append(_p)
try:
    import gt_optimize_session as _gos                       # noqa: E402
    import gt_model_records as _gmr                          # noqa: E402
except Exception:                                            # noqa: BLE001
    _gos = _gmr = None

W_IN, W_OUT, W_READ, W_5M, W_1H = 1.0, 5.0, 0.1, 1.25, 2.0
CHARS_PER_TOKEN = 4                      # an estimate, and labelled one
STATES = ("measured", "estimated", "design")        # strongest first

# mechanism -> (label, what the counterfactual is). The order is the report's.
MECHANISMS = (
    ("agent-models", "Agent models (task tiers)", "same tokens priced at the policy tier's model"),
    ("model-profile", "Model profile per skill", "the skill at the session model"),
    ("gt-open", "gt-open lazy memory load", "every memory file the index lists"),
    ("digest", "Research digest", "the whole research.md"),
    ("agent-isolation", "Agents keeping dumps out of context",
     "the tool output the agent consumed"),
    ("spill", "Long outputs spilled to a file", "the whole output in the conversation"),
    ("lotr", "LOTR specialist agents", "direct MCP payload into main context"),
    ("lotr-find", "LOTR find budget (vs full schemas)",
     "every op of the same connections as a full tool definition"),
    ("farm", "Farmed packets (local / other engines)", "the same tokens at the reference rate"),
    ("vault-answer", "Vault answers from a local engine", "the cited pages read into the session"),
    ("gt-minimize", "gt-minimize / compact timing", "projected re-reads at the pre-cut size"),
    ("rules-once", "Rules sent once (compact)",
     "the full rule text on every prompt"),
    ("paste-guard", "Huge pastes held back", "the whole paste in the conversation"),
    ("auto-compact", "Context nudge + regular auto-compact", "the session re-read at its uncut size"),
    ("core-rules", "Rules (rework avoided)",
     "the session's own work at the benchmarked rules-off rework rate"),
    ("core-rules-wider", "Rules: leaks, releases, personas",
     "secret clean-up, release re-runs and persona rework the benchmark did not cover"),
)
DESIGN_NOTES = {
    "rules-once": "rules_injection is full: every prompt carries the whole text",
    "farm": "no farm packets yet (gt-farm records one line per packet)",
    "lotr-find": "no LOTR finds recorded yet (the gateway records one line per search)",
    "vault-answer": "no vault answers recorded yet (vault_answers local-first records one per question)",
    "spill": "none recorded (spill_threshold_lines is off, or no output was long enough)",
}
PROFILES = ("average", "very-high", "inherit")
# NET (0.21.4)
REREAD_CAP = 40                 # main turns a kept token is assumed to be re-read, at most
REREAD_DEFAULT = 10             # when no timestamp places the agent in its session
PAUSE_DAYS = 7
PAUSE_MIN_EVENTS = 5            # a mechanism with fewer events in the window is never paused
PAUSEABLE = ("agent-isolation", "spill", "farm")        # what gt routes by itself
COMPACT_LINE_TOKENS = 45        # the one-line Core-rules form, characters / 4 (rules_injection compact)
RULES_REFRESH = 20              # gt_rules_once.REFRESH_EVERY
OVERHEAD_AGENT = "Explore"      # the agent type gt_break_even.py reads
# The 2026-10-05 LOTR measurement, the ledger's first measured row (25 Jira queries in one
# session): 382,400 tokens of main context connected directly, 8,545 through LOTR plus a
# specialist agent.
LOTR_SEED = {"key": "seed:lotr-2026-10-05", "mechanism": "lotr", "state": "measured",
             "actual_eq": 8545, "counterfactual_eq": 382400, "note": "25 Jira queries; measured 2026-10-05"}
# The 2026-10-11 Core-rules A/B benchmark: 90 headless runs (opus, sonnet, haiku x three task
# levels x rules on/off x 5), each failed run re-prompted until right, so the figure is the cost
# of a CORRECT result. Per finished task the rules-off work cost 1.262x the rules-on work,
# weighted by the owner's 36-hour request mix; nearly all of it was commits that broke the tests.
# Applied to each main session's own tokens, so the row is `estimated`, and it can only be as
# wide as the benchmark: leaks and persona work are the design row beside it.
# $GT_SAVINGS_CORE_RULES_FACTOR overrides it (a fixture, or a re-run benchmark).
CORE_RULES_REWORK = 0.262


def core_rules_factor():
    try:
        return max(0.0, float(os.environ.get("GT_SAVINGS_CORE_RULES_FACTOR", CORE_RULES_REWORK)))
    except ValueError:
        return CORE_RULES_REWORK


# ---- settings, pricing -----------------------------------------------------------------

def ledger_on():
    """The savings_ledger setting, read from the one config file gt_settings keeps (so this works
    in a hook before the module's settings are registered); anything but "off" is on."""
    try:
        with open(os.path.join(HOME, ".claude", "vault-config.json"), encoding="utf-8") as fh:
            v = json.load(fh).get("savings_ledger")
    except (OSError, ValueError, AttributeError):
        v = None
    return str(v or "on").strip().lower() != "off"


BILLING = ("subscription", "api")


def billing():
    """The savings_billing setting: `api` when tokens are what the account is invoiced for, so a
    USD figure is spend avoided; `subscription` (the default) when they are not, so it is only a
    list-price equivalent. Read from the same config file as savings_ledger."""
    try:
        with open(os.path.join(HOME, ".claude", "vault-config.json"), encoding="utf-8") as fh:
            v = str(json.load(fh).get("savings_billing") or "").strip().lower()
    except (OSError, ValueError, AttributeError):
        v = ""
    return v if v in BILLING else "subscription"


def usd_words():
    """-> how a USD figure is named under the billing setting."""
    return ("USD spend avoided at list price" if billing() == "api"
            else "USD list-price equivalent")


FALLBACK_PRICE = 3.0     # labelled: used for token-equivalent ratios only, never added into USD


def price_for(model):
    """-> (USD per million INPUT tokens at list price, known?) from the one model record table. A model with no
    row is (FALLBACK_PRICE, False): a caller adding up dollars must not use that number (see usd_of)."""
    if _gmr is not None:
        return _gmr.TABLE.price(model)
    return FALLBACK_PRICE, False


def price_value(model):
    """The price as a bare number, the labelled fallback when the model has no row. For the ratio between two
    models' token-equivalents in an event that is already state `estimated`, never for a USD total."""
    return price_for(model)[0]


def usd_of(ev, tokens):
    """-> (USD, label of the model when it has no price row, else None) for `tokens` token-equivalents of an event,
    at the list price of its unit model: the policy model for an agent-models event that names one (0.22.0),
    else the session model. An event whose model has no row adds no USD: it is counted as unpriced."""
    model = ev.get("policy_model") or ev.get("session_model") or ev.get("model")
    price, known = price_for(model)
    return (tokens * price / 1e6, None) if known else (0.0, model or "(none)")


def unpriced_bucket(*row_sets):
    """-> {events, models: {model: events}} over summarise() row dicts: the events whose USD was left out
    because their model has no price row."""
    models = {}
    for rows in row_sets:
        for r in (rows or {}).values():
            for m, c in r.get("unpriced", {}).items():
                models[m] = models.get(m, 0) + c
    return {"events": sum(models.values()), "models": dict(sorted(models.items()))}


def tier_of(model):
    """The model's tier word from the table (fast, balanced, deep, ...), 'other' when it has no row or no tier."""
    return (_gmr.TABLE.tier_of(model) if _gmr is not None else None) or "other"


def read_engines(doc):
    """-> the price document's `engines` rows (hosted farm engines), longest model id first. A row is keyed by
    PROVIDER + MODEL (the provider is its own field, never read out of a name) and carries in_per_mtok,
    out_per_mtok and `verified`. A row with no provider, no model or no verified date is refused: a price
    nobody read from a provider's page is not shown as one."""
    out = []
    rows = doc.get("engines") if isinstance(doc, dict) else None
    for row in rows if isinstance(rows, list) else ():
        if (isinstance(row, dict) and all(isinstance(row.get(k), str) and row[k].strip()
                                          for k in ("provider", "model", "verified"))):
            out.append(dict(row, provider=row["provider"].strip().lower(), model=row["model"].strip().lower()))
    return sorted(out, key=lambda r: -len(r["model"]))


def _engine_rows():
    try:
        with open(os.path.join(_gos.HERE, "gt_model_prices.json"), encoding="utf-8") as fh:
            return read_engines(json.load(fh))
    except Exception:                                        # noqa: BLE001
        return ()


ENGINE_ROWS = _engine_rows() if _gos is not None else ()


def engine_row(provider, model, rows=None):
    """-> the price row for this provider's model (the longest model id contained in `model`), or None. With no
    provider recorded there is no row: the provider is never guessed from the model name."""
    p, m = (provider or "").strip().lower(), (model or "").lower()
    if not p:
        return None
    for r in ENGINE_ROWS if rows is None else rows:
        if r["provider"] == p and r["model"] in m:
            return r
    return None


def read_weight(model):
    """The cache-read weight at this model's price: its row's multiple, else the flat W_READ (0.1)."""
    return _gmr.TABLE.cache_read(model, W_READ) if _gmr is not None else W_READ


def weighted(u, model=None):
    """Price-weighted tokens of one usage dict, at a price of 1 per input token. `model` sets the
    cache-read weight (it differs by model); without one the flat W_READ applies."""
    return (u.get("input", 0) * W_IN + u.get("output", 0) * W_OUT + u.get("cache_read", 0) * read_weight(model)
            + u.get("cache_write_5m", 0) * W_5M + u.get("cache_write_1h", 0) * W_1H)


def raw_tokens(u):
    return (u.get("input", 0) + u.get("output", 0) + u.get("cache_read", 0)
            + u.get("cache_write_5m", 0) + u.get("cache_write_1h", 0))


def short(s, n=8):
    return str(s or "")[:n]


def opaque(s):
    return hashlib.sha256(str(s).encode("utf-8", "replace")).hexdigest()[:8]


# ---- the ledger ------------------------------------------------------------------------

_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")
_BIG_INT = 10 ** 15                      # no ledger count is near a quadrillion; a larger one is not a measurement


def _usable(ev):
    """True for a ledger line the report can place in a window and add up. A `ts` that is not a date (missing,
    text such as "zzz" or "nan", a list, null) would sort into every window, and a number too large for a float
    (a JSON integer of 400 digits) raised OverflowError in the table: such a line is skipped AND counted as
    malformed, like a line that is not JSON."""
    ts = ev.get("ts")
    if not (isinstance(ts, str) and _DATE_PREFIX.match(ts)):
        return False
    for k in _NUMERIC_FIELDS:
        v = ev.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and abs(v) > _BIG_INT:
            return False
    return True


def read_ledger(path=None, stats=None):
    """-> {key: latest event}. Append-only on disk; the newest line per key wins on read.
    `stats` (a dict), when given, gets `malformed`: the non-blank lines skipped because they were
    not a JSON object with a key (0.1.10: skipped AND counted, so a damaged ledger is visible)."""
    out, bad = {}, 0
    try:
        with open(path or LEDGER, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                except (ValueError, RecursionError, OverflowError):
                    bad += 1                                 # not JSON, or nested far deeper than any event
                    continue
                # a key is text: a list or object there is unhashable, and a number or a flag is not one of ours
                if isinstance(ev, dict) and isinstance(ev.get("key"), str) and ev["key"] and _usable(ev):
                    out[ev["key"]] = ev
                else:
                    bad += 1
    except OSError:
        pass
    if stats is not None:
        stats["malformed"] = stats.get("malformed", 0) + bad
    return out


def append(events):
    """Append each event as ONE os.write on an O_APPEND descriptor (0.21.4). A buffered handle
    flushes in blocks that need not end on a line, so a batch could interleave mid-line with another
    session appending at the same moment; a single write() of a whole line to an O_APPEND file lands
    as one piece on POSIX. Binary, so a line ends in LF on Windows too."""
    if not events:
        return 0
    os.makedirs(BASE, exist_ok=True)
    fd = os.open(LEDGER, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
    try:
        for ev in events:
            data = (json.dumps(ev, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            while data:
                data = data[os.write(fd, data):]
    finally:
        os.close(fd)
    return len(events)


def stamp(ev, ts=None):
    ev.setdefault("v", SCHEMA)
    ev.setdefault("gt", GT_VERSION)
    ev["ts"] = ts or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ev


# ---- reading transcripts ----------------------------------------------------------------

def _num(d, k):
    try:
        return max(0, int(d.get(k) or 0))
    except (TypeError, ValueError, AttributeError):
        return 0


def _text_len(content):
    if isinstance(content, str):
        return len(content)
    n = 0
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict):
                if isinstance(b.get("text"), str):
                    n += len(b["text"])
                elif isinstance(b.get("content"), (str, list)):
                    n += _text_len(b["content"])
    return n


def read_file(path, seen, since):
    """-> one record for a transcript file, or None. `seen` dedups API calls across files."""
    rec = {"sid": "", "sidechain": False, "models": {}, "usage": dict.fromkeys(
        ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h"), 0),
        "turns": 0, "prompts": 0, "tool_chars": 0, "last_text_chars": 0, "last_ts": None,
        "first_ts": None, "timeline": [], "ctxline": [], "compactions": [], "spill_read_chars": 0,
        "first_ctx": None, "first_write": 0, "first_read": 0}
    spill_uses = set()
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue                         # a half-written last line
            if not isinstance(row, dict):
                continue
            rec["sid"] = rec["sid"] or str(row.get("sessionId") or row.get("session_id") or "")
            if row.get("isSidechain"):
                rec["sidechain"] = True
            msg = row.get("message") if isinstance(row.get("message"), dict) else {}
            ts = _gos._ts(row.get("timestamp")) if _gos else None
            if since is not None and ts is not None and ts < since:
                continue
            if row.get("type") == "system" and (row.get("subtype") == "compact_boundary"
                                                 or row.get("compactMetadata")):
                if ts is not None:
                    rec["compactions"].append(ts)           # NET: where a kept token stops being re-read
                continue
            if row.get("type") == "user":
                content = msg.get("content")
                if isinstance(content, list) and any(
                        isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            n = _text_len(b.get("content"))
                            rec["tool_chars"] += n
                            if b.get("tool_use_id") in spill_uses:
                                rec["spill_read_chars"] += n     # the model fetched a spill file back
                elif not row.get("isMeta") and not row.get("isSidechain") and _text_len(content):
                    rec["prompts"] += 1
                continue
            if row.get("type") != "assistant":
                continue
            # a streamed request spreads its content blocks over several rows: look at every row
            for b in (msg.get("content") if isinstance(msg.get("content"), list) else []):
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") \
                        and mentions_spill(b.get("input")):
                    spill_uses.add(b["id"])
            usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
            if not usage:
                continue
            dedup = (row.get("requestId"), msg.get("id"))
            if dedup == (None, None):
                dedup = ("uuid", row.get("uuid"))
            if dedup in seen:
                continue
            seen.add(dedup)
            cc = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
            w1h, w5m = _num(cc, "ephemeral_1h_input_tokens"), _num(cc, "ephemeral_5m_input_tokens")
            write = _num(usage, "cache_creation_input_tokens")
            if write and not (w1h or w5m):
                w5m = write
            if rec["first_ctx"] is None:                    # the opening context: all of it overhead
                rec["first_read"], rec["first_write"] = _num(usage, "cache_read_input_tokens"), write
                rec["first_ctx"] = _num(usage, "input_tokens") + write + rec["first_read"]
            u = rec["usage"]
            u["input"] += _num(usage, "input_tokens")
            u["output"] += _num(usage, "output_tokens")
            u["cache_read"] += _num(usage, "cache_read_input_tokens")
            u["cache_write_5m"] += w5m
            u["cache_write_1h"] += w1h
            rec["turns"] += 1
            m = msg.get("model") or ""
            rec["models"][m] = rec["models"].get(m, 0) + 1
            if ts is not None and m:
                rec["timeline"].append((ts, m))
                rec["ctxline"].append((ts, _num(usage, "input_tokens") + _num(usage, "cache_read_input_tokens")
                                       + write))
                if rec["first_ts"] is None or ts < rec["first_ts"]:
                    rec["first_ts"] = ts
            n = _text_len([b for b in (msg.get("content") or [])
                           if isinstance(b, dict) and b.get("type") == "text"])
            if n:
                rec["last_text_chars"] = n
            rec["last_ts"] = ts or rec["last_ts"]
    return rec if rec["turns"] or rec["prompts"] else None


def mentions_spill(tool_input):
    """Does a tool call's input name a gt spill file (the folder gt_spill.py writes to)?"""
    try:
        text = tool_input if isinstance(tool_input, str) else json.dumps(tool_input)
    except (TypeError, ValueError):
        return False
    text = text.replace("\\\\", "/").replace("\\", "/")
    return "/spill/" in text and ("golden-thread" in text or ".gt-scratch" in text)


def turns_after(tl, compactions, t0, cap=None):
    """-> how many main-session turns followed `t0` before the next compaction (at most `cap`):
    the turns a token kept out of the main context would have been re-read on. REREAD_DEFAULT
    when no timestamp places the agent."""
    cap = REREAD_CAP if cap is None else cap
    if not tl or t0 is None:
        return REREAD_DEFAULT
    end = min([c for c in (compactions or []) if c > t0], default=None)
    return min(cap, sum(1 for t, _m in tl if t > t0 and (end is None or t < end)))


def context_at(line, t0):
    """-> the size of the main session's context at its last request at or before `t0`, else at its
    first request after it, else 0."""
    if not line or t0 is None:
        return 0
    before = [c for t, c in line if t <= t0]
    return before[-1] if before else line[0][1]


def session_model_at(tl, ts):
    """-> the main session's model at time `ts` (its last reply at or before then), or None.
    Before the session's first reply, its first reply's model."""
    if not tl or ts is None:
        return None
    at = None
    for t, m in tl:
        if t <= ts:
            at = m
        else:
            break
    return at or tl[0][1]


def modal(models):
    return max(models, key=models.get) if models else ""


def project_of(projects_dir, path):
    rel = os.path.relpath(path, projects_dir)
    return opaque(rel.split(os.sep)[0])


def _recent_first(files, since):
    """-> the files written at or after `since`, newest first. A file last written before the
    window opened holds no row inside it, so it is not opened at all."""
    floor = since.timestamp() if since is not None else None
    stamped = []
    for path in files:
        try:
            m = os.path.getmtime(path)
        except OSError:
            continue
        if floor is None or m >= floor:
            stamped.append((m, path))
    return [p for _m, p in sorted(stamped, reverse=True)]


def events_from_transcripts(projects_dir, since, deadline=None):
    """-> (events, files_read). Pure over the transcripts: no ledger, no clock but `since` and
    `deadline` (a time.monotonic() value): once it passes, no further file is opened, and the
    newest files have already been read. At least one file is always read."""
    if _gos is None:
        return [], 0
    files = _recent_first(_gos.transcript_files(projects_dir), since)
    seen, recs, nread = set(), [], 0
    for path in files:
        if nread and deadline is not None and time.monotonic() > deadline:
            break
        nread += 1
        r = read_file(path, seen, since)
        if r:
            if r["sidechain"]:
                r["agent_type"] = agent_type(path)
            r["project"] = project_of(projects_dir, path)
            r["fid"] = opaque(os.path.relpath(path, projects_dir))
            recs.append(r)
    session_model, timeline, compactions, ctxline = {}, {}, {}, {}
    for r in recs:
        if not r["sidechain"]:
            sm = session_model.setdefault(r["sid"], {})
            for m, n in r["models"].items():
                sm[m] = sm.get(m, 0) + n
            timeline.setdefault(r["sid"], []).extend(r.get("timeline") or [])
            compactions.setdefault(r["sid"], []).extend(r.get("compactions") or [])
            ctxline.setdefault(r["sid"], []).extend(r.get("ctxline") or [])
    for tl in timeline.values():
        tl.sort(key=lambda e: e[0])
    for cl in ctxline.values():
        cl.sort(key=lambda e: e[0])
    events = []
    per_turn = injection_tokens()
    for r in recs:
        sid8 = short(r["sid"])
        # The counterfactual for an agent is the model the session was on WHEN THE AGENT
        # STARTED, not the session's most common model over the window: a session that moved
        # between Opus and Sonnet made every agent spawned in its Opus hours look like an Opus
        # choice made under Sonnet (2026-10-11: -94M token-equivalents of phantom cost from one
        # session). The modal model is only the fallback when no timestamp places the agent.
        smodel = (session_model_at(timeline.get(r["sid"]), r.get("first_ts"))
                  or modal(session_model.get(r["sid"], {})))
        base = {"session": sid8, "project": r["project"]}
        if not r["sidechain"]:
            if per_turn and r["prompts"]:
                events.append(dict(base, key="overhead:%s:%s" % (sid8, r["fid"]),
                                   mechanism="overhead", state="estimated", turns=r["prompts"],
                                   per_turn_tokens=per_turn, model=modal(r["models"]),
                                   actual_eq=injection_eq(r["prompts"], per_turn), counterfactual_eq=0))
                # The rules ran in this session, so its work is priced against the rework
                # the benchmark measured without them. The main loop is at its own model.
                w, factor = weighted(r["usage"], modal(r["models"])), core_rules_factor()
                if w and factor:
                    events.append(dict(base, key="core-rules:%s:%s" % (sid8, r["fid"]),
                                       mechanism="core-rules", state="estimated",
                                       model=modal(r["models"]), factor=factor,
                                       actual_eq=round(w), counterfactual_eq=round(w * (1 + factor))))
            if r["spill_read_chars"]:
                # what the model fetched back from spill files: a cost of the spill mechanism
                back = r["spill_read_chars"] // CHARS_PER_TOKEN
                events.append(dict(base, key="spill-read:%s:%s" % (sid8, r["fid"]), mechanism="spill",
                                   state="estimated", actual_eq=back, counterfactual_eq=0,
                                   kept_eq=0, spent_eq=back))
            continue
        model = modal(r["models"])
        if smodel and model:
            # the counterfactual is the POLICY tier (0.22.0, phase 82): the model the spawn's type would have got
            # from the routing policy, so toggling the session between Opus and Sonnet no longer flips agents
            # between saving and loss. Units are tokens at the policy model's price. A spawn whose type is not
            # recorded (or that policy leaves on the session) keeps the older reading, the session model.
            pm = policy_model(r.get("agent_type"))
            unit = pm or smodel
            ratio = price_value(model) / price_value(unit)
            events.append(dict(base, key="agent-models:%s:%s" % (sid8, r["fid"]),
                               mechanism="agent-models", state="estimated", model=model,
                               session_model=smodel, tier=tier_of(model), usage=r["usage"],
                               actual_tokens=raw_tokens(r["usage"]),
                               actual_eq=round(weighted(r["usage"], model) * ratio),
                               counterfactual_eq=round(weighted(r["usage"], unit)),
                               **({"policy_model": pm} if pm else {})))
        if r["tool_chars"] and r["last_text_chars"]:
            consumed = r["tool_chars"] // CHARS_PER_TOKEN
            returned = r["last_text_chars"] // CHARS_PER_TOKEN
            # NET (0.21.4): what carrying the kept tokens would have cost the main session, against
            # what the agent itself spent. The turns after the agent started are COUNTED.
            k = turns_after(timeline.get(r["sid"]), compactions.get(r["sid"]), r.get("first_ts"))
            reread = W_5M + W_READ * k
            main_ctx = context_at(ctxline.get(r["sid"]), r.get("first_ts"))
            loop = max(0, r["turns"] - 2) * W_READ * main_ctx        # main-session calls the agent saved
            kept = round(max(0, consumed - returned) * reread + r["usage"]["output"] * W_OUT + loop)
            ratio = (price_value(model) / price_value(smodel)) if (smodel and model) else 1.0
            spent = round(weighted(r["usage"], model) * ratio)
            events.append(dict(base, key="agent-isolation:%s:%s" % (sid8, r["fid"]),
                               mechanism="agent-isolation", state="estimated", model=model,
                               actual_eq=returned, counterfactual_eq=consumed,
                               turns_after=k, main_ctx=main_ctx, agent_calls=r["turns"],
                               kept_eq=kept, spent_eq=spent, net_eq=kept - spent))
    return events, nread


# ---- measuring gt's own overhead --------------------------------------------------------

def config_value(name, default=""):
    """A key of ~/.claude/vault-config.json (where gt_settings keeps every setting), lower-cased."""
    try:
        with open(os.path.join(HOME, ".claude", "vault-config.json"), encoding="utf-8") as fh:
            v = json.load(fh).get(name)
    except (OSError, ValueError, AttributeError):
        return default
    return str(v).strip().lower() if isinstance(v, str) and v.strip() else default


def injection_eq(prompts, per_turn):
    """The Core-rules injection one session paid, in tokens. With rules_injection `full` (the default)
    every prompt carries the whole text. With `compact` (gt 0.21.4, gt_rules_once.py) the whole text
    goes out on the first prompt and every RULES_REFRESH-th after it, and a COMPACT_LINE_TOKENS line
    on the rest. Priced at the setting in force NOW, like every other figure here: the ledger does
    not record which mode a past prompt ran in."""
    if config_value("rules_injection", "full") != "compact" or prompts <= 0:
        return prompts * per_turn
    full = -(-prompts // RULES_REFRESH)                      # ceil
    return full * per_turn + (prompts - full) * min(per_turn, COMPACT_LINE_TOKENS)


def injection_tokens():
    """Tokens one prompt pays for the Core-rule injection, or None when it cannot be measured.

    $GT_SAVINGS_INJECTION_TOKENS overrides (a fixture, or a measured figure). Otherwise the
    installed hook is run once with an empty event and its injected text is counted (characters
    / 4 -- an estimate, so the overhead row is labelled one). Cached by the hook's size+mtime."""
    env = os.environ.get("GT_SAVINGS_INJECTION_TOKENS")
    if env:
        try:
            return max(0, int(env))
        except ValueError:
            return None
    hook = os.path.join(HOME, ".claude", "golden-thread", "hooks", "inject_core_rules.sh")
    try:
        st = os.stat(hook)
    except OSError:
        return None
    sig = "%d:%d" % (st.st_size, int(st.st_mtime))
    state = load_state()
    cached = state.get("injection")
    if isinstance(cached, dict) and cached.get("sig") == sig:
        return cached.get("tokens")
    try:
        r = subprocess.run(["bash", hook], input="{}", capture_output=True, text=True, timeout=15)
        data = json.loads(r.stdout)
        ctx = (data.get("hookSpecificOutput") or {}).get("additionalContext") or ""
        tokens = len(ctx) // CHARS_PER_TOKEN
    except Exception:                                        # noqa: BLE001
        return None
    state["injection"] = {"sig": sig, "tokens": tokens}
    save_state(state)
    return tokens


def load_state():
    try:
        with open(STATE, encoding="utf-8") as fh:
            d = json.load(fh)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(d):
    try:
        os.makedirs(BASE, exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
        os.replace(tmp, STATE)
    except OSError:
        pass


# ---- commands: record --------------------------------------------------------------------

def cmd_record(a):
    if not ledger_on():
        clear_paused()                            # off: no NET is kept, so no pause may stand (0.21.4)
        return 0
    projects = a.projects_dir or os.path.join(HOME, ".claude", "projects")
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=a.days)
    started = time.monotonic()
    events, nfiles = events_from_transcripts(projects, since, started + a.max_seconds)
    have = read_ledger()
    new = []
    for ev in events:
        old = have.get(ev["key"])
        if old and all(old.get(k) == ev.get(k) for k in ("actual_eq", "counterfactual_eq", "turns",
                                                           "spent_eq", "kept_eq")):
            continue                              # unchanged since last recorded
        new.append(stamp(ev))
        if time.monotonic() - started > a.max_seconds:
            break
    if LOTR_SEED["key"] not in have:
        new.append(stamp(dict(LOTR_SEED, session="", project="")))
    append(new)
    try:                                          # NET (0.21.4): who is paused this week, from the ledger alone
        merged = dict(have)
        merged.update({ev["key"]: ev for ev in new})
        write_paused(pause_state(list(merged.values())))
    except Exception:                             # noqa: BLE001
        pass
    if not a.hook:
        print("savings: %d event(s) recorded from %d transcript file(s)" % (len(new), nfiles))
    return 0


def read_cost(paths):
    """-> (tokens estimate, files readable). Characters / 4; paths are used and never stored."""
    chars, n = 0, 0
    for p in paths:
        try:
            chars += os.path.getsize(p)
            n += 1
        except OSError:
            pass
    return chars // CHARS_PER_TOKEN, n


def cmd_note_read(a):
    if not ledger_on():
        return 0
    if a.mechanism not in ("gt-open", "digest"):
        print("savings: --mechanism must be gt-open or digest", file=sys.stderr)
        return 2
    read, nr = read_cost(a.read)
    instead, ni = read_cost(a.instead)
    if not nr or not ni:
        print("savings: nothing readable to measure", file=sys.stderr)
        return 2
    ev = {"key": "%s:%d" % (a.mechanism, time.time_ns()), "mechanism": a.mechanism,
          "state": "estimated", "session": "", "project": opaque(a.project or ""),
          "files_read": nr, "files_instead": ni, "actual_eq": read, "counterfactual_eq": instead}
    append([stamp(ev)])
    print("savings: %s read %d tokens against %d for the full set" % (a.mechanism, read, instead))
    return 0


def _since(text):
    """An ISO time (naive = local) -> an aware UTC datetime, or None."""
    try:
        d = datetime.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.astimezone()
    return d.astimezone(datetime.timezone.utc)


def cmd_ingest_run(a):
    """What the specialist agents of ONE ingest run kept out of the session (0.21.0, item #31).

    The same measurement as the agent-isolation mechanism of `record` -- the tool output a
    subagent consumed (characters / 4) against the result it returned -- restricted to this
    session's subagent transcripts that began at or after the run's start. The events are
    recorded under the keys `record` would give them, so the hook's own pass never counts them twice.
    Prints one JSON object with --json, else the figures; always `estimated`."""
    since = _since(a.since)
    if since is None:
        print("savings: --since must be an ISO time", file=sys.stderr)
        return 2
    projects = a.projects_dir or os.path.join(HOME, ".claude", "projects")
    events, _n = events_from_transcripts(projects, since)
    want = {short(s) for s in (a.session or []) if s}
    iso = [e for e in events if e["mechanism"] == "agent-isolation"
           and (not want or e.get("session") in want)]
    consumed = sum(e["counterfactual_eq"] for e in iso)
    returned = sum(e["actual_eq"] for e in iso)
    recorded = 0
    if iso and ledger_on():
        have = read_ledger()
        new = []
        for ev in iso:
            old = have.get(ev["key"])
            if old and old.get("actual_eq") == ev["actual_eq"] \
                    and old.get("counterfactual_eq") == ev["counterfactual_eq"]:
                continue
            new.append(stamp(dict(ev, ingest_run=opaque(a.run or ""))))
        recorded = append(new)
    out = {"consumed": consumed, "returned": returned, "kept": max(0, consumed - returned),
           "agents": len(iso), "state": "estimated", "recorded": recorded,
           "ledger": "on" if ledger_on() else "off"}
    if a.json:
        print(json.dumps(out, sort_keys=True))
    else:
        print("agents %d, consumed %d, returned %d, kept %d tokens (estimated); %d event(s) recorded"
              % (out["agents"], consumed, returned, out["kept"], recorded))
    return 0


def cmd_benchmark(a):
    """A head-to-head: the without-gt side is MEASURED because the same task was run both ways."""
    ev = {"key": "benchmark:%s:%d" % (opaque(a.task), time.time_ns()), "mechanism": "benchmark",
          "state": "measured", "session": "", "project": "", "task_id": opaque(a.task),
          "actual_eq": a.with_gt, "counterfactual_eq": a.without_gt}
    if ledger_on():
        append([stamp(ev)])
    print("savings: benchmark %s recorded: with gt %d, without %d tokens"
          % (ev["task_id"], a.with_gt, a.without_gt))
    return 0


# ---- the report --------------------------------------------------------------------------

def weakest(states):
    """The weakest label among `states`. A state this code does not know (a hand-edited or foreign ledger
    line) counts as weaker than every known one and reads "design", so it can never lift a figure."""
    states = list(states)
    if not states:
        return "design"
    w = max(states, key=lambda s: STATES.index(s) if s in STATES else len(STATES))
    return w if w in STATES else "design"


def reprice(ev, profile):
    """-> the counterfactual of an event under `profile`; only agent-models events move."""
    if profile is None or ev.get("mechanism") != "agent-models":
        return ev.get("counterfactual_eq", 0)
    u, model, smodel = ev.get("usage") or {}, ev.get("model"), ev.get("session_model")
    if profile == "inherit":
        target = smodel
    elif profile == "very-high":                  # the table's top tier
        target = (_gmr.TABLE.tier_model(_gmr.TABLE.tiers()[-1]) if _gmr is not None and _gmr.TABLE.tiers() else None) or "opus"
    else:                                         # average: the tier's own model
        target = tier_model(ev.get("tier"), model)
    return round(weighted(u, target) * price_value(target) / price_value(ev.get("policy_model") or smodel))


def policy_model(atype):
    """-> the model gt's routing policy puts an ad-hoc spawn of this type on (an alias: opus, sonnet, haiku), or
    None when the type is unknown here, not an ad-hoc type, or the policy leaves it on the session's model.
    The same call the spawn hook makes (gt_agent_model.resolve), so the ledger prices what the hook would do."""
    try:
        if not atype or atype == "unknown":
            return None
        import gt_agent_model                                       # noqa: E402
        return gt_agent_model.resolve("" if atype == "general-purpose" else atype)[0]
    except Exception:                                              # noqa: BLE001 - a price is never worth a crash
        return None


def tier_model(tier, default=None):
    """The model a tier word stands for: the table's default for that tier (fast, balanced, deep), or the word
    itself when it is a model alias (the Agent tool's own spelling, which older ledger events carry)."""
    if _gmr is not None and isinstance(tier, str):
        return _gmr.TABLE.tier_model(tier) or (tier if _gmr.TABLE.exact(tier) else default)
    return default


def in_window(ev, since, until):
    ts = str(ev.get("ts", ""))[:10]
    return (not since or ts >= since) and (not until or ts <= until)


# Mechanisms the report shows under another row (0.22.0, phase 53). The ledger keeps its own key; only the
# displayed row changes: the expiry guard's savings count as "Rules (rework avoided)", so the total is the same.
DISPLAY_AS = {"expiry-guard": "core-rules"}


_NUMERIC_FIELDS = ("actual_eq", "counterfactual_eq", "kept_eq", "spent_eq", "_usd")
# text fields the report uses as dict keys or compares: a non-text value is dropped, so the field reads as absent
_TEXT_FIELDS = ("model", "session_model", "policy_model", "state", "service", "provider", "tier")
_MAX_NUMBER = 10 ** 15      # the same bound read_ledger applies: a larger number is hand-edited, not a count


def _clean(ev):
    """-> a copy of one ledger line the report can add up, or None for a line that is not a record. The ledger
    is a plain file people can edit, and a hand-edited line ("counterfactual_eq": "abc", null, a list; a
    mechanism that is a dict) crashed the report with a TypeError. A number field that is not a finite number
    is dropped (so `kept_eq` and `_usd` keep their 'absent' meaning, and the others read as 0); a mechanism
    that is not text becomes "unknown". The file itself is never rewritten."""
    if not isinstance(ev, dict):
        return None
    ev = dict(ev)
    for k in _NUMERIC_FIELDS:
        v = ev.get(k)
        if k in ev and (isinstance(v, bool) or not isinstance(v, (int, float)) or v != v
                        or v in (float("inf"), float("-inf")) or abs(v) > _MAX_NUMBER):
            del ev[k]
    if not isinstance(ev.get("mechanism"), str):
        ev["mechanism"] = "unknown"
    for k in _TEXT_FIELDS:                        # a model / state / service that is a dict or a list would be a dict key
        if k in ev and not isinstance(ev[k], str):
            del ev[k]
    if "usage" in ev:                             # token counts of an agent-models line, read by the repricing
        u = ev["usage"]
        if not isinstance(u, dict):
            del ev["usage"]
        else:
            ev["usage"] = {k: v for k, v in u.items() if isinstance(k, str) and not isinstance(v, bool)
                           and isinstance(v, (int, float)) and v == v and abs(v) <= _MAX_NUMBER}
    return ev


def summarise(events, profile=None):
    rows = {}
    for ev in filter(None, map(_clean, events)):
        m = ev.get("mechanism")
        m = DISPLAY_AS.get(m, m)
        r = rows.setdefault(m, {"actual": 0, "cf": 0, "n": 0, "states": [], "usd": 0.0,
                                "kept": 0, "spent": 0, "weighted": 0, "unpriced": {}})
        cf = reprice(ev, profile)
        r["actual"] += ev.get("actual_eq", 0)
        r["cf"] += cf
        # NET (0.21.4): `kept_eq` when the event carries it (the main-session carrying cost of what
        # was kept out), else the plain counterfactual - actual; `spent_eq` is what the mechanism
        # itself spent on Claude beyond its own `actual`.
        if "kept_eq" in ev:
            r["kept"] += ev["kept_eq"]
            r["weighted"] += 1
        else:
            r["kept"] += cf - ev.get("actual_eq", 0)
        r["spent"] += ev.get("spent_eq", 0)
        if "_usd" in ev:
            # a farm line prices itself: usd_avoided at the service row's reference rate (0.1.10)
            r["usd"] += ev["_usd"] or 0.0
        else:
            # USD: the saving at the session model's list price; a model with no price row adds none and is
            # counted in `unpriced` instead of being priced at a made-up rate
            usd, missing = usd_of(ev, cf - ev.get("actual_eq", 0))
            r["usd"] += usd
            if missing:
                r["unpriced"][missing] = r["unpriced"].get(missing, 0) + 1
        r["n"] += 1
        r["states"].append(ev.get("state", "design"))
    return rows


def summarise_by_model(events, profile=None):
    """-> rows {mechanism, model, scope, actual, cf, saved, usd, n}, one per mechanism x model x (agent | main
    session), over the same events and arithmetic as summarise() so the rows add up to its mechanism rows."""
    rows = {}
    for ev in filter(None, map(_clean, events)):
        m = DISPLAY_AS.get(ev.get("mechanism"), ev.get("mechanism"))
        scope = "agent" if ev.get("mechanism") in ("agent-models", "agent-isolation") or ev.get("tier") else "main"
        r = rows.setdefault((m, ev.get("model") or "-", scope),
                            {"mechanism": m, "model": ev.get("model") or "-", "scope": scope,
                             "actual": 0, "cf": 0, "usd": 0.0, "n": 0, "known": True, "unpriced": 0})
        cf = reprice(ev, profile)
        r["actual"] += ev.get("actual_eq", 0)
        r["cf"] += cf
        if "_usd" in ev:
            r["usd"] += ev["_usd"] or 0.0
        else:
            usd, missing = usd_of(ev, cf - ev.get("actual_eq", 0))
            r["usd"] += usd
            if missing:
                r["known"] = False
                r["unpriced"] += 1
        r["n"] += 1
    out = sorted(rows.values(), key=lambda x: (x["mechanism"], x["scope"], x["model"]))
    for r in out:
        r["saved"] = r["cf"] - r["actual"]
    return out


def by_model_lines(rows):
    lines = ["", "by model (mechanism x model; agents apart from the main session):",
             "%-15s %-17s %-5s %9s %9s %9s %8s" % ("mechanism", "model", "scope", "actual", "cf",
                                                    "saved", "USD")]
    for r in rows:
        lines.append("%-15s %-17s %-5s %9s %9s %9s %8s" % (
            r["mechanism"][:15], r["model"][:17], r["scope"], n(r["actual"]), n(r["cf"]), n(r["saved"]),
            "%.3f" % r["usd"] if r.get("known", True) else "unpriced" if not r["usd"] else "%.3f+" % r["usd"]))
    return lines


def split_farm(evs):
    """-> (farm lines normalised, how many were malformed)."""
    farm, bad = [], 0
    for e in evs:
        if e.get("mechanism") == "farm":
            f = farm_line(e)
            if f is None:
                bad += 1
            else:
                farm.append(f)
    return farm, bad


def row_events(evs, farm):
    return ([e for e in evs if e.get("mechanism") not in ("overhead", "benchmark", "farm")]
            + [f["event"] for f in farm if not f["failed"]])


def build(a):
    stats = {}
    ledger = read_ledger(stats=stats)
    since = a.since or (datetime.datetime.now(datetime.timezone.utc)
                        - datetime.timedelta(days=a.days)).strftime("%Y-%m-%d")
    evs = [e for e in ledger.values() if in_window(e, since, a.until)
           and (not a.project or e.get("project") in (a.project, opaque(a.project)))]
    farm, farm_bad = split_farm(evs)
    rows = summarise(row_events(evs, farm), a.profile)
    over = summarise([e for e in evs if e.get("mechanism") == "overhead"])
    bench = summarise([e for e in evs if e.get("mechanism") == "benchmark"])
    stats["farm_malformed"] = farm_bad
    stats["all_events"] = list(ledger.values())
    return since, evs, rows, over.get("overhead"), bench.get("benchmark"), farm, stats


# ---- NET per mechanism (0.21.4) -----------------------------------------------------------------

def net_table(rows, over):
    """-> [ {mechanism, label, events, kept, spent, net, state, weighted} ] from summarise() rows.
    `over` is the overhead row (the Core-rule injection), the core-rules mechanism's own spend."""
    labels = {k: l for k, l, _ in MECHANISMS}
    out = []
    for key, _label, _cf in MECHANISMS:
        r = rows.get(key)
        if not r:
            continue
        spent = r["spent"] + (over["actual"] if key == "core-rules" and over else 0)
        out.append({"mechanism": key, "label": labels[key], "events": r["n"], "kept": r["kept"],
                    "spent": spent, "net": r["kept"] - spent, "state": weakest(r["states"]),
                    "weighted": r["weighted"] > 0})
    return out


def pause_state(events, today=None):
    """-> {mechanism: {net, events, since}} for the PAUSEABLE mechanisms whose trailing PAUSE_DAYS net
    is negative over at least PAUSE_MIN_EVENTS events. `events` is the ledger's events."""
    today = today or datetime.datetime.now(datetime.timezone.utc)
    since = (today - datetime.timedelta(days=PAUSE_DAYS)).strftime("%Y-%m-%d")
    evs = [e for e in events if in_window(e, since, None)]
    farm, _bad = split_farm(evs)
    rows = summarise(row_events(evs, farm))
    out = {}
    for key in PAUSEABLE:
        r = rows.get(key)
        if r and r["n"] >= PAUSE_MIN_EVENTS and r["kept"] - r["spent"] < 0:
            out[key] = {"net": r["kept"] - r["spent"], "events": r["n"], "since": since}
    return out


def paused_file():
    return os.path.join(BASE, "net-paused.json")


def clear_paused():
    """Remove the pause file: with savings_ledger off nothing would ever rewrite it, and a stale
    pause would keep spill standing down for good. Never raises."""
    try:
        os.unlink(paused_file())
    except OSError:
        pass


def write_paused(state):
    """Store the pause state where gt_break_even.paused() reads it. Never raises."""
    try:
        os.makedirs(BASE, exist_ok=True)
        tmp = paused_file() + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "days": PAUSE_DAYS, "paused": state}, fh, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, paused_file())
    except OSError:
        pass


NET_NOTES = {
    "agent-isolation": "kept counts each token's later re-reads; spent is the agent's own usage",
    "spill": "spent is what the model read back from spill files",
    "core-rules": "spent is the injection overhead",
    "farm": "spent counts only packets whose line records a reference or audit call",
}


def net_lines(table, paused):
    """-> the NET section of the report: one line per mechanism, the state as a column."""
    out = ["", "NET -- kept out of the main context, minus what each mechanism spent on Claude",
           (NROW % ("mechanism", "kept", "spent", "net", "state")).rstrip()]
    notes = []
    for t in table:
        st = "%s (%d event%s)" % (t["state"], t["events"], "" if t["events"] == 1 else "s")
        out.append((NROW % (t["label"][:40], cell(t["kept"], 13), cell(t["spent"], 13),
                            cell(t["net"], 13), st)).rstrip())
        if t["net"] < 0:
            out.append("  NEGATIVE: it cost more tokens than it kept out")
        if t["mechanism"] in NET_NOTES:
            notes.append("* %s: %s" % (t["label"], NET_NOTES[t["mechanism"]]))
    if not table:
        out.append("no mechanism has events in this window")
    for key, row_ in sorted(paused.items()):
        out.append("PAUSED (7-day net %s over %d events): %s -- %s" % (
            n(row_["net"]), row_["events"], key,
            {"spill": "the spill hook stands down until the week turns",
             "farm": "an auto-router reads net-paused.json (gt_break_even.paused)",
             }.get(key, "advisory: nothing is blocked")))
    if "farm" in paused and config_value("farm_first", "") == "auto":
        out.append("farm_first is auto: its auto-routing is paused with the farm row")
    out += [""] + notes + ["* No total: the rows have different counterfactuals and would count one thing twice"]
    return fit(out)


# ---- the farm (0.1.10) -------------------------------------------------------------------

def _count(v):
    """A whole, non-negative token count, or None. A bool is not a count; 12.0 is 12."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if 0 <= v <= _MAX_NUMBER else None      # beyond the bound it is hand-edited, and floats overflow
    if isinstance(v, float) and 0 <= v <= _MAX_NUMBER and v == int(v):
        return int(v)
    return None


def _amount(v):
    """A finite, non-negative number, or None."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):       # a 400-digit integer overflows float()
        return None
    return f if f >= 0 and f == f and f != float("inf") else None


def farm_line(ev):
    """-> one gt-farm ledger line, normalised, or None when it is malformed (no whole,
    non-negative prompt and completion counts). Nothing is guessed: a line without counts is
    skipped and counted by the caller. A missing state is `estimated`, and a relayed line is
    always `estimated` (its tokens are lengths / 4), so nothing is promoted to measured."""
    pt, ct = _count(ev.get("prompt_tokens")), _count(ev.get("completion_tokens"))
    if pt is None or ct is None:
        return None
    local = ev.get("local") is True
    tokens = pt + ct
    relay = ev.get("relay") is True
    state = ev.get("state") if ev.get("state") in ("measured", "estimated") else "estimated"
    if relay:
        state = "estimated"
    rate = _amount(ev.get("rate_per_mtok")) or 0.0
    usd = _amount(ev.get("usd_avoided"))
    if usd is None and rate:
        usd = tokens * rate / 1e6
    if not rate and not usd:
        usd = None                               # no reference rate: tokens shown, cost "-"
    actual, cf = _count(ev.get("actual_eq")), _count(ev.get("counterfactual_eq"))
    if cf is None:
        cf = tokens
    if actual is None:
        actual = 0 if local else tokens
    failed = str(ev.get("status") or "").strip().lower() in ("failed", "rejected", "error")
    purpose = str(ev.get("purpose") or "packet").strip().lower()
    purpose = purpose if purpose in ("packet", "review", "calibration") else "packet"
    f = {"purpose": purpose, "kept_out": _count(ev.get("kept_out_tokens")) or 0,
         "main_ctx": _count(ev.get("main_context_tokens")) or 0,
         "claude_usd": _amount(ev.get("claude_spent_usd")) or 0.0,
         "service": str(ev.get("service") or "?"), "model": str(ev.get("model") or ""),
         "provider": str(ev.get("provider") or "").strip().lower(),
         "transport": "lotr" if ev.get("transport") == "lotr" else "",
         "connection": str(ev.get("connection") or "") if ev.get("transport") == "lotr" else "",
         "op": str(ev.get("op") or "") if ev.get("transport") == "lotr" else "",
         "local": local, "relay": relay, "prompt": pt, "completion": ct, "tokens": tokens,
         "state": state, "wall": _amount(ev.get("wall_s")) or 0.0, "rate": rate, "usd": usd,
         "actual": actual, "cf": cf, "day": str(ev.get("ts", ""))[:10],
         "retries": _count(ev.get("retries")) or 0, "has_retries": "retries" in ev,
         "failed": failed, "has_status": "status" in ev, "rl": ev.get("rate_limited") is True}
    # The mechanism table's unit is the token-EQUIVALENT (price-weighted), and gt-farm writes
    # raw prompt + completion tokens. Weighted here like a Claude request (prompt x1, completion
    # x5, the same weights as farm_eq and "kept off Claude"), so TOTAL adds like with like
    # (gt 0.21.4). The ledger's own ratio of actual to counterfactual is kept; the raw tokens
    # stay in the FARM section, which says "raw tokens".
    scale = (pt * W_IN + ct * W_OUT) / tokens if tokens else 0.0
    # What the farming cost Claude (0.22.0): `claude_overhead` is the send (the call, the packet Claude
    # wrote, the result it read, chars / 4) and `main_context_tokens` is what a local review's output put
    # in the session. Both are Claude reading or writing, so they are part of the row's actual: a packet
    # that kept 1,000 tokens off Claude and cost it 400 saved 600. The USD figure nets the same tokens at
    # the line's own reference rate, the formula `gt_farm_send.py ledger` already uses, and takes off
    # `claude_spent_usd`, what Claude spent on a calibration's reference answers: one row, everything in
    # it, no split. A line written before the overhead figure existed has neither field and adds none.
    overhead = (_count(ev.get("claude_overhead")) or 0) + (_count(ev.get("main_context_tokens")) or 0)
    net_usd = (usd or 0.0) - overhead * rate / 1e6 - f["claude_usd"]
    f["overhead"] = overhead
    f["gross_eq"] = round(cf * scale) - round(actual * scale)        # kept off Claude, before its own cost
    f["cost_eq"] = round(overhead * W_IN)                            # what the farming cost Claude, in tokens
    f["net_usd"] = net_usd
    f["event"] = {"mechanism": "farm", "actual_eq": round(actual * scale + overhead * W_IN),
                  "counterfactual_eq": round(cf * scale), "state": state, "_usd": net_usd,
                  "spent_eq": _count(ev.get("spent_eq")) or 0}
    return f


FARM_PURPOSES = (("packet", "work packets"), ("review", "local review"), ("calibration", "engine calibration"))


def farm_breakdown_lines(farm):
    """-> the farmed savings broken down by purpose, a secondary table under the main one (0.22.0). The
    main table's farm row is the net of all of it; this shows what the net is made of: what each kind of
    farmed work kept off Claude, what it cost Claude, and what was left. Token-equivalents, like the row.
    A calibration keeps nothing off Claude and costs the USD Claude spent on its reference answers, so
    its only figure is the negative USD."""
    ok = [f for f in farm if not f["failed"]]
    if not ok:
        return []
    out = ["", "Farmed savings by purpose (the Farmed packets row is the net of these)",
           "%-20s %7s %12s %12s %12s %9s" % ("purpose", "events", "kept off", "Claude cost", "net saved", "USD net")]
    tot = [0, 0, 0, 0, 0.0]
    for key, label in FARM_PURPOSES:
        g = [f for f in ok if f["purpose"] == key]
        if not g:
            continue
        v = [len(g), sum(f["gross_eq"] for f in g), sum(f["cost_eq"] for f in g), 0,
             sum(f["net_usd"] for f in g)]
        v[3] = v[1] - v[2]
        out.append("%-20s %7s %12s %12s %12s %9.2f" % (label, n(v[0]), n(v[1]), n(v[2]), n(v[3]), v[4]))
        tot = [a + b for a, b in zip(tot, v)]
    if len(out) > 3:
        out.append("%-20s %7s %12s %12s %12s %9.2f" % ("total",n(tot[0]), n(tot[1]), n(tot[2]), n(tot[3]), tot[4]))
    return out


def farm_eq(f):
    """Farmed tokens as Claude token-equivalents: weighted like a Claude request's (input 1,
    output 5), so they compare with the work `record` measured. An estimate, labelled one."""
    return f["prompt"] * W_IN + f["completion"] * W_OUT


def farm_groups(farm):
    """-> [row] per (service, model, local), largest first."""
    groups = {}
    for f in farm:
        g = groups.setdefault((f["service"], f["model"], f["local"], f["transport"], f["connection"], f["op"]), {
            "service": f["service"], "model": f["model"], "provider": f["provider"], "local": f["local"],
            "transport": f["transport"], "connection": f["connection"], "op": f["op"],
            "packets": 0,
            "sent": 0, "relayed": 0, "prompt_tokens": 0, "completion_tokens": 0, "tokens": 0,
            "measured": 0, "estimated": 0, "wall_s": 0.0, "_wall_tokens": 0, "_wall_out": 0,
            "usd_avoided": None, "no_rate": 0, "rates": set(), "retries": 0, "failed": 0,
            "_days": set(), "rate_limited": 0})
        g["rate_limited"] += 1 if f["rl"] else 0
        if f["failed"]:
            g["failed"] += 1
            g["retries"] += f["retries"]
            continue
        g["packets"] += 1
        if f["day"]:
            g["_days"].add(f["day"])
        g["relayed" if f["relay"] else "sent"] += 1
        g["prompt_tokens"] += f["prompt"]
        g["completion_tokens"] += f["completion"]
        g["tokens"] += f["tokens"]
        g[f["state"]] += 1
        g["retries"] += f["retries"]
        if f["wall"] > 0 and not f["relay"]:
            g["wall_s"] += f["wall"]
            g["_wall_tokens"] += f["tokens"]
            g["_wall_out"] += f["completion"]
        if f["usd"] is None:
            g["no_rate"] += 1
        else:
            g["usd_avoided"] = (g["usd_avoided"] or 0.0) + f["usd"]
        if f["rate"]:
            g["rates"].add(f["rate"])
    out = []
    for g in groups.values():
        g["state"] = ("estimated" if g["estimated"] else "measured") if g["packets"] else "none"
        g["tokens_per_s"] = round(g["_wall_tokens"] / g["wall_s"], 1) if g["wall_s"] else None
        g["completion_tokens_per_s"] = round(g["_wall_out"] / g["wall_s"], 1) if g["wall_s"] else None
        g["wall_s"] = round(g["wall_s"], 1)
        rates = sorted(g.pop("rates"))
        g["rate_per_mtok"] = rates[0] if len(rates) == 1 else (None if not rates else "mixed")
        if g["usd_avoided"] is not None:
            g["usd_avoided"] = round(g["usd_avoided"], 6)
        del g["_wall_tokens"], g["_wall_out"]
        g["days"] = len(g.pop("_days"))
        price = None if g["local"] else engine_row(g["provider"], g["model"])
        g["verified"] = price["verified"] if price else None
        out.append(g)
    return sorted(out, key=lambda g: (-g["tokens"], g["service"], g["model"]))


def claude_work_eq(evs):
    """-> Claude's own work in the window, in token-equivalents at the session model, from the
    events `record` wrote: each main session's work (the core-rules events' actual) plus each
    agent run's (agent-models actual). None when the window holds neither."""
    seen, total = False, 0
    for e in evs:
        if e.get("mechanism") in ("core-rules", "agent-models"):
            seen = True
            total += _count(e.get("actual_eq")) or 0
    return total if seen else None


def latest_allowance():
    """-> (weekly %, 5-hour %, age in hours) from gt_usage's latest.json, or None."""
    try:
        with open(os.path.join(BASE, "latest.json"), encoding="utf-8") as fh:
            row = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(row, dict) or (row.get("seven_day") is None and row.get("five_hour") is None):
        return None
    try:
        age = max(0.0, (time.time() - float(row.get("ts") or 0)) / 3600.0)
    except (TypeError, ValueError):
        return None
    return row.get("seven_day"), row.get("five_hour"), age


def _buckets(since, until):
    """-> (unit, [bucket start dates as YYYY-MM-DD]) covering since..until: days up to 14 days,
    ISO weeks (Monday) up to 26 weeks, months beyond."""
    d0 = datetime.date.fromisoformat(since)
    d1 = datetime.date.fromisoformat(until)
    span = (d1 - d0).days + 1
    if span <= 14:
        return "day", [(d0 + datetime.timedelta(days=i)).isoformat() for i in range(max(span, 1))]
    if span <= 26 * 7:
        w = d0 - datetime.timedelta(days=d0.weekday())
        out = []
        while w <= d1:
            out.append(w.isoformat())
            w += datetime.timedelta(days=7)
        return "week", out
    out, m = [], d0.replace(day=1)
    while m <= d1:
        out.append(m.isoformat())
        m = (m.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    return "month", out


def _bucket_of(day, unit):
    d = datetime.date.fromisoformat(day)
    if unit == "day":
        return day
    if unit == "week":
        return (d - datetime.timedelta(days=d.weekday())).isoformat()
    return d.replace(day=1).isoformat()


def farm_trend(farm, since, until):
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    until = until or today
    try:
        unit, keys = _buckets(since, max(until, since))
    except ValueError:
        return {"unit": "day", "points": [], "shown": "none"}
    pts = {k: {"start": k, "packets": 0, "tokens": 0, "local_tokens": 0, "usd_avoided": None,
               "estimated": 0} for k in keys}
    for f in farm:
        if f["failed"]:
            continue
        try:
            k = _bucket_of(f["day"], unit)
        except ValueError:
            continue
        p = pts.get(k)
        if p is None:
            continue
        p["packets"] += 1
        p["tokens"] += f["tokens"]
        p["local_tokens"] += f["tokens"] if f["local"] else 0
        p["estimated"] += f["state"] == "estimated"
        if f["usd"] is not None:
            p["usd_avoided"] = round((p["usd_avoided"] or 0.0) + f["usd"], 6)
    points = [pts[k] for k in keys]
    shown = "all"
    if len(points) > 31:
        points, shown = [p for p in points if p["packets"]], "non-empty"
    return {"unit": unit, "points": points, "shown": shown}


def farm_data(farm, evs, since, until, stats):
    """-> the farm section as data: totals, rows, trend, the share kept off Claude."""
    ok = [f for f in farm if not f["failed"]]
    n = len(ok)
    tokens = sum(f["tokens"] for f in ok)
    local_tokens = sum(f["tokens"] for f in ok if f["local"])
    wall = sum(f["wall"] for f in ok if f["wall"] > 0 and not f["relay"])
    wall_tokens = sum(f["tokens"] for f in ok if f["wall"] > 0 and not f["relay"])
    usd = [f["usd"] for f in ok if f["usd"] is not None]
    est = sum(1 for f in ok if f["state"] == "estimated")
    fq = sum(farm_eq(f) for f in ok)
    cw = claude_work_eq(evs)
    allowance = latest_allowance()
    kept = {"state": "estimated", "farm_eq": round(fq), "claude_eq": cw,
            "share": round(fq / (fq + cw), 4) if (cw is not None and fq + cw > 0) else None,
            "weekly_allowance_pct": allowance[0] if allowance else None,
            "five_hour_allowance_pct": allowance[1] if allowance else None,
            "reading_age_h": round(allowance[2], 1) if allowance else None,
            "allowance_percent": None,
            "allowance_note": "not converted: how tokens count against a plan is undocumented"}
    by_purpose = {}
    for f in ok:
        b = by_purpose.setdefault(f["purpose"], {"runs": 0, "tokens": 0, "kept_out_tokens": 0,
                                                 "main_context_tokens": 0, "claude_spent_usd": 0.0,
                                                 "measured": 0, "estimated": 0})
        b["runs"] += 1
        b["tokens"] += f["tokens"]
        b["kept_out_tokens"] += f["kept_out"]
        b["main_context_tokens"] += f["main_ctx"]
        b["claude_spent_usd"] = round(b["claude_spent_usd"] + f["claude_usd"], 6)
        b["measured" if f["state"] == "measured" else "estimated"] += 1
    for b in by_purpose.values():
        b["state"] = "estimated" if b["estimated"] else "measured"
    return {"by_purpose": by_purpose,
            "packets": n, "sent": sum(1 for f in ok if not f["relay"]),
            "relayed": sum(1 for f in ok if f["relay"]),
            "local_packets": sum(1 for f in ok if f["local"]),
            "local_share": round(sum(1 for f in ok if f["local"]) / n, 4) if n else None,
            "prompt_tokens": sum(f["prompt"] for f in ok),
            "completion_tokens": sum(f["completion"] for f in ok),
            "tokens": tokens, "local_tokens": local_tokens,
            "local_token_share": round(local_tokens / tokens, 4) if tokens else None,
            "measured": n - est, "estimated": est,
            "state": ("estimated" if est else "measured") if n else "none",
            "wall_s": round(wall, 1),
            "tokens_per_s": round(wall_tokens / wall, 1) if wall else None,
            "usd_avoided": round(sum(usd), 6) if usd else None,
            "no_rate_packets": sum(1 for f in ok if f["usd"] is None),
            "retries": sum(f["retries"] for f in farm),
            "failed": sum(1 for f in farm if f["failed"]),
            "failures_recorded": any(f["has_status"] or f["has_retries"] for f in farm),
            "malformed": stats.get("farm_malformed", 0),
            "rows": farm_groups(farm),
            "trend": farm_trend(farm, since, until),
            "kept_off_claude": kept}


FROW_A = "%-40s %-5s %5s %13s %13s  %s"
FROW_B = "%-40s %8s %7s %9s %13s  %s"
FROW_T = "%-14s %7s %12s %6s %10s  %s"


def _name(service, model, width=40, via=""):
    if model and model.startswith("/"):
        model = os.path.basename(model.rstrip("/"))      # a local model path: its file name, never the path
    if via:
        service = via                                    # a LOTR row is labelled by its connection
    s = "%s / %s" % (service, model) if model else service
    return s if len(s) <= width else s[:width - 1] + "~"


def _via(g):
    return "lotr:%s" % g["connection"] if g.get("transport") == "lotr" and g.get("connection") else ""


def _usd(v, places=4):
    return "-" if v is None else ("%." + str(places) + "f") % v


def _pct(part, whole):
    return "-" if not whole else "%.0f%%" % (100.0 * part / whole)


def farm_lines(fd):
    """-> the farm section as text, every line at most 80 columns (owner rule: fixed-width text
    never wraps)."""
    out = ["", "FARM -- packets kept off Claude (gt-farm; raw tokens, prompt + completion)"]
    if not fd["packets"] and not fd["failed"]:
        out.append("no farm packets in this window -- gt-farm's gt_farm_send.py records one")
        out.append("line per packet it sends (`send`) or a person carried (`relay`)")
        if fd["malformed"]:
            out.append("farm lines skipped as malformed (no whole token counts): %d"
                       % fd["malformed"])
        return out
    out.append(FROW_A % ("engine / model", "where", "pkts", "prompt", "completion", "state"))
    for g in fd["rows"]:
        if not g["packets"]:
            continue
        st = g["state"] if not (g["measured"] and g["estimated"]) \
            else "%d meas, %d est" % (g["measured"], g["estimated"])
        out.append(FROW_A % (_name(g["service"], g["model"], via=_via(g)), "local" if g["local"] else "other",
                             g["packets"], n(g["prompt_tokens"]), n(g["completion_tokens"]), st))
    out.append(FROW_A % ("TOTAL", "", fd["packets"], n(fd["prompt_tokens"]),
                         n(fd["completion_tokens"]), fd["state"] if not (
                             fd["measured"] and fd["estimated"])
                         else "%d meas, %d est" % (fd["measured"], fd["estimated"])))
    out += ["", FROW_B % ("engine / model", "wall s", "tok/s", "$/Mtok", "USD avoided",
                          "sent/relay")]
    for g in fd["rows"]:
        if not g["packets"]:
            continue
        rate = g["rate_per_mtok"]
        out.append(FROW_B % (_name(g["service"], g["model"], via=_via(g)),
                             "%.1f" % g["wall_s"] if g["wall_s"] else "-",
                             "%.1f" % g["tokens_per_s"] if g["tokens_per_s"] else "-",
                             "-" if rate is None else (rate if isinstance(rate, str) else "%.2f" % rate),
                             _usd(g["usd_avoided"]), "%d/%d" % (g["sent"], g["relayed"])))
    out.append(FROW_B % ("TOTAL", "%.1f" % fd["wall_s"] if fd["wall_s"] else "-",
                         "%.1f" % fd["tokens_per_s"] if fd["tokens_per_s"] else "-", "",
                         _usd(fd["usd_avoided"]), "%d/%d" % (fd["sent"], fd["relayed"])))
    out += ["",
            "local engines: %d of %d packets (%s), %s of %s tokens (%s)"
            % (fd["local_packets"], fd["packets"], _pct(fd["local_packets"], fd["packets"]),
               n(fd["local_tokens"]), n(fd["tokens"]), _pct(fd["local_tokens"], fd["tokens"])),
            "measured %d, estimated %d (relayed, or the engine sent no usage field)"
            % (fd["measured"], fd["estimated"])]
    if fd["no_rate_packets"]:
        out.append("no reference rate on %d packet(s): tokens shown, cost \"-\""
                   % fd["no_rate_packets"])
    if fd["failures_recorded"]:
        out.append("failed or rejected: %d packet(s); retries recorded: %d"
                   % (fd["failed"], fd["retries"]))
    else:
        out.append("failures and retries: not in the ledger (gt-farm writes no line for a failed or rejected packet)")
    if fd["malformed"]:
        out.append("farm lines skipped as malformed (no whole token counts): %d"
                   % fd["malformed"])
    out += engine_lines(fd["rows"])
    out.append("throughput: tokens / wall s over sent packets (relayed packets have no time)")
    out.append("USD avoided counts local engines; another service is off Claude, not free")
    out += farm_purpose_lines(fd.get("by_purpose") or {})
    out += farm_kept_lines(fd["kept_off_claude"])
    out += farm_trend_lines(fd["trend"])
    return out


def engine_lines(rows):
    """-> the per-engine days / 429 / price-verified table: only for a hosted engine or one that was
    rate limited (a quiet local engine has nothing to add). 80 columns."""
    shown = [g for g in rows if (g["packets"] or g["rate_limited"]) and (not g["local"] or g["rate_limited"])]
    if not shown:
        return []
    out = ["", "%-40s %5s %5s  %s" % ("per engine", "days", "429s", "price verified")]
    for g in shown:
        out.append("%-40s %5d %5d  %s" % (_name(g["service"], g["model"], via=_via(g)), g["days"], g["rate_limited"],
                                          "-" if g["local"] else (g["verified"] or "unverified")))
    return out


PURPOSE_WORDS = {"packet": "work packets", "review": "local review (commit step)",
                 "calibration": "engine calibration (a cost, no saving)"}


def farm_purpose_lines(bp):
    """The by-purpose block (gt 0.21.4); nothing when every line is a plain packet."""
    if not bp or set(bp) == {"packet"}:
        return []
    out = ["", "by purpose (kept out of Claude = what Claude did not read or write)"]
    for key in ("packet", "review", "calibration"):
        b = bp.get(key)
        if not b:
            continue
        head = "  %-38s %4d run%s, %s" % (PURPOSE_WORDS[key], b["runs"], " " if b["runs"] == 1 else "s", b["state"])
        out.append(head.rstrip())
        if key == "review":
            out.append("    kept out of Claude %s tokens; %s reached your session (estimated)"
                       % (n(b["kept_out_tokens"]), n(b["main_context_tokens"])))
        elif key == "calibration":
            out.append("    %s engine tokens; Claude spent about %s USD on references"
                       % (n(b["tokens"]), ("%.4f" % b["claude_spent_usd"])))
        else:
            out.append("    %s tokens" % n(b["tokens"]))
    return out


def farm_kept_lines(k):
    out = ["", "kept off Claude (estimated)",
           "  farmed work      %14s token-eq (prompt x1, completion x5)" % n(k["farm_eq"])]
    if k["claude_eq"] is None:
        out.append("  Claude's own work in this window is not in the ledger (run `record`),")
        out.append("  so no share is shown")
    else:
        out.append("  Claude's own work %14s token-eq (main sessions + agents)" % n(k["claude_eq"]))
        out.append("  share kept off   %14s of all the work in this window"
                   % ("-" if k["share"] is None else "%.1f%%" % (100 * k["share"])))
    if k["weekly_allowance_pct"] is not None or k["five_hour_allowance_pct"] is not None:
        bits = []
        if k["weekly_allowance_pct"] is not None:
            bits.append("weekly %.0f%%" % k["weekly_allowance_pct"])
        if k["five_hour_allowance_pct"] is not None:
            bits.append("5-hour %.0f%%" % k["five_hour_allowance_pct"])
        age = k["reading_age_h"]
        out.append("  allowance read %s used, %s" % (", ".join(bits),
                   "just now" if age < 1 else "%.0f hours ago%s" % (age, " (stale)" if age > 12 else "")))
    out.append("  not turned into an allowance percent: how tokens count against a plan is undocumented")
    return out


def farm_trend_lines(t):
    out = ["", (FROW_T % ("farm, by " + t["unit"], "packets", "tokens", "local", "USD", "")).rstrip()]
    pts = t["points"]
    peak = max([p["tokens"] for p in pts] + [1])
    for p in pts:
        bar = "#" * (round(20 * p["tokens"] / peak) if p["tokens"] else 0)
        out.append((FROW_T % (p["start"], p["packets"], n(p["tokens"]),
                              _pct(p["local_tokens"], p["tokens"]), _usd(p["usd_avoided"]),
                              bar)).rstrip())
    if t["shown"] == "non-empty":
        out.append("(periods with no packets are not shown)")
    return out


ROW = "%-40s %13s %15s %13s %11s  %s"   # one line per mechanism, USD and state as columns (owner, 2026-10-16)
NROW = "%-40s %13s %13s %13s  %s"
RULE = "-" * 108
WIDTH = 132                          # owner rule: fixed-width text never wraps; one row is one line
FARM_WEIGHT_NOTE = "weighted like Claude: prompt x1, completion x5"


def skipped_line(count):
    """The one line every command prints when the ledger had lines it could not use."""
    return "ledger lines skipped as malformed (not a JSON object with a key, a date and plain numbers): %d" % count


def cell(v, width):
    """A number for a table cell: thousands separated, in millions when it would not fit."""
    if not isinstance(v, (int, float)):
        return str(v)
    try:
        t = n(v)
        return t if len(t) <= width else "%sM" % n(round(v / 1e6))
    except (OverflowError, ValueError):
        return "-"


def money(v):
    return v if isinstance(v, str) else ("%.2f" % v)


def row(label, actual, cf, saved, usd="-", state=""):
    return (ROW % (label, cell(actual, 13), cell(cf, 15), cell(saved, 13), money(usd), state)).rstrip()


def fit(lines, width=WIDTH):
    """Wrap any line longer than `width` at spaces; a continuation keeps the line's indent plus
    two. A line with no space to break at is cut nowhere -- there is none in this report."""
    out = []
    for line in lines:
        while len(line) > width:
            lead = len(line) - len(line.lstrip(" "))
            cut = line.rfind(" ", lead + 1, width + 1)
            if cut <= lead:
                break
            out.append(line[:cut].rstrip())
            line = " " * (lead + 2) + line[cut:].lstrip()
        out.append(line)
    return out


def state_line(text):
    """The second line of a mechanism: how it was established, indented under the label."""
    return fit(["    " + text])


def n(v):
    """A token count for people: thousands separated (0.1.9; owner, "easier to read")."""
    return "{:,}".format(v)


def window_line(a, since):
    """The window in words. The default is a ROLLING one: every report shows the --days (7)
    before it, so the same command run tomorrow drops a day and adds one. The ledger keeps
    everything; only the view rolls."""
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    if a.since or a.until:
        return "from %s to %s" % (since, a.until or today)
    return "last %d days, rolling (%s to %s)" % (a.days, since, today)


def rules_once_row(evs):
    """The Core rules sent once (rules_injection compact, gt 0.21.4) as a row: what the overhead events
    paid against the full text on every prompt. None with the setting off (it is then a design row)."""
    if config_value("rules_injection", "full") != "compact":
        return None
    r = {"actual": 0, "cf": 0, "n": 0, "states": [], "usd": 0.0, "kept": 0, "spent": 0, "weighted": 0,
         "unpriced": {}}
    for ev in filter(None, map(_clean, evs)):               # the same cleaning summarise() applies to a hand-edited line
        if ev.get("mechanism") != "overhead":
            continue
        turns, per = ev.get("turns"), ev.get("per_turn_tokens")
        if (isinstance(turns, bool) or isinstance(per, bool) or not isinstance(turns, int) or not isinstance(per, int)
                or abs(turns) > _MAX_NUMBER or abs(per) > _MAX_NUMBER):
            continue
        cf, act = turns * per, ev.get("actual_eq", 0)
        r["actual"] += act
        r["cf"] += cf
        r["kept"] += cf - act
        usd, missing = usd_of(dict(ev, session_model=None), cf - act)
        r["usd"] += usd
        if missing:
            r["unpriced"][missing] = r["unpriced"].get(missing, 0) + 1
        r["n"] += 1
        r["states"].append("estimated")
    return r if r["n"] else None


def render(a):
    since, evs, rows, over, bench, farm, stats = build(a)
    ro = rules_once_row(evs)
    if ro:
        rows = dict(rows, **{"rules-once": ro})
    fd = farm_data(farm, evs, since, a.until, stats)
    api = billing() == "api"
    head = ["gt savings ledger -- %s%s" % (window_line(a, since), (" (re-priced under %s)" % a.profile)
                                            if a.profile else ""),
            "units: token-equivalents at the policy tier; " + (
                "USD is spend avoided at list price (api)" if api
                else "USD is list-price, not a bill (subscription)")]
    if not ledger_on():
        head.append("recording is OFF (savings_ledger) -- showing what was recorded earlier")
    head.append("")
    lines = [row("mechanism", "actual", "counterfactual", "saved", "USD saved", "state")]
    saved_total, saved_usd, summary = 0, 0.0, []
    tot_actual, tot_cf, states = 0, 0, []
    for key, label, cf in MECHANISMS:
        r = rows.get(key)
        if not r:
            lines.append(row(label, "-", "-", "-", "-", "design"))
            summary.append({"mechanism": key, "state": "design",
                            "note": DESIGN_NOTES.get(key, "no events recorded in this window")})
            continue
        saved = r["cf"] - r["actual"]
        st = weakest(r["states"])
        summary.append({"mechanism": key, "actual": r["actual"], "counterfactual": r["cf"],
                        "saved": saved, "usd": round(r["usd"], 4), "state": st, "events": r["n"],
                        "kept": r["kept"], "spent": r["spent"]})
        lines.append(row(label, r["actual"], r["cf"], saved, r["usd"],
                         "%s (%d event%s)" % (st, r["n"], "" if r["n"] == 1 else "s")))
        if key == "rules-once":
            summary[-1]["in_total"] = False     # its actual IS the overhead row below: counted once, there
            continue
        saved_total += saved
        saved_usd += r["usd"]
        tot_actual += r["actual"]
        tot_cf += r["cf"]
        states += r["states"]
    over_tokens = over["actual"] if over else 0
    over_usd = -over["usd"] if over else 0.0
    if over:
        tot_actual += over_tokens
        states += over["states"]
        lines.append(row("gt overhead (Rule injection)", over_tokens, 0, -over_tokens, -over_usd,
                         "%s (%d event%s) -- COST" % (weakest(over["states"]), over["n"],
                                                      "" if over["n"] == 1 else "s")))
    else:
        lines.append(row("gt overhead (Rule injection)", "-", "-", "-", "-", "design"))
    net, net_usd = saved_total - over_tokens, saved_usd - over_usd
    # sum(counterfactual) - (sum(actual) + overhead) = sum(saved) - overhead: the TOTAL's saved IS the net
    lines.append(RULE)
    lines.append(row("TOTAL", tot_actual, tot_cf, net, net_usd,
                     weakest(states) if states else "design"))
    cr = rows.get("core-rules")
    cr_net = (cr["cf"] - cr["actual"] - over_tokens) if cr else None
    cr_usd = (cr["usd"] - over_usd) if cr else None
    # the savings first (owner, 2026-10-16: "why doesn't it show the savings"), then the table
    top = ["SAVED: %.2f USD (%s) -- %s token-equivalents, net of gt's own overhead%s"
           % (net_usd, "spend avoided at list price" if api else "list-price equivalent, not a bill",
              n(net), "" if over else " (overhead not measured)")]
    if cr:
        top.append("Rules, net of their own cost: %s token-equivalents (%.2f %s), %s"
                   % (n(cr_net), cr_usd, usd_words(),
                      weakest(cr["states"] + (over["states"] if over else []))))
    head += top + [""]
    notes = ["design rows have no measurement yet and are not in the total"]
    if ro:
        notes.append("Rules sent once: shown beside the overhead it reduced, not added to the total")
    if rows.get("farm"):
        notes.append("farmed packets are " + FARM_WEIGHT_NOTE)
        notes.append("farmed packets are net of Claude's cost: sends, review output, calibration")
    notes.append("not in the overhead: SessionStart output, report cards, hook context (not measured)")
    unpriced = unpriced_bucket(rows, {"overhead": over} if over else {})
    if unpriced["events"]:
        notes.append("unpriced: %d event%s on a model with no price row (%s): their token-equivalents are in "
                     "the table, their USD is not in any figure" % (
                         unpriced["events"], "" if unpriced["events"] == 1 else "s",
                         ", ".join("%s x%d" % (m, c) for m, c in unpriced["models"].items())))
    lines += [""] + ["* " + t for t in notes]
    if bench:
        lines.append("benchmarks (head-to-head, measured): %d run(s), with gt %s, without gt %s"
                     % (bench["n"], n(bench["actual"]), n(bench["cf"])))
    if stats.get("malformed"):
        lines.append("ledger lines skipped as malformed (not a JSON object with a key): %d"
                     % stats["malformed"])
    lines += farm_breakdown_lines(farm)
    net_rows = net_table(rows, over)
    paused = pause_state(stats.get("all_events") or [])
    if getattr(a, "full", False):
        lines += net_lines(net_rows, paused)
        lines += farm_lines(fd)
    else:
        # the plain report is the savings and their table (owner, 2026-10-16: "for a regular request it
        # doesn't need to be this verbose"); --full adds the NET and FARM sections
        lines += ["", "--full adds the NET section (per-mechanism cost) and the FARM section (per engine)"]
        lines += ["PAUSED: %s (7-day net %s over %d events)" % (k, n(r_["net"]), r_["events"])
                  for k, r_ in sorted(paused.items())]
    by_model = summarise_by_model(row_events(evs, farm), a.profile) if getattr(a, "by_model", False) else None
    if by_model is not None:
        lines += by_model_lines(by_model)
    if not evs:
        lines.append("nothing recorded in this window -- run `gt_savings.py record` or start a session")
    return "\n".join(fit(head + lines)), dict({"since": since, "until": a.until, "window_days": None if (a.since or a.until) else a.days,
                              "net": net, "net_usd": round(net_usd, 4),
                              "total_actual": tot_actual, "total_counterfactual": tot_cf,
                              "billing": billing(), "core_rules_net": cr_net,
                              "core_rules_net_usd": None if cr_usd is None else round(cr_usd, 4),
                              "overhead": over_tokens, "rows": summary,
                              "net_by_mechanism": net_rows, "paused": paused,
                              "events": len(evs), "malformed_lines": stats.get("malformed", 0),
                              "unpriced": unpriced, "farm": fd}, **({"by_model": by_model} if by_model is not None else {}))


def cmd_report(a):
    text, data = render(a)
    print(json.dumps(data, indent=1) if a.json else text)
    return 0 if data["events"] else 4


def cmd_net(a):
    a.since = a.until = a.project = a.profile = None
    _, data = render(a)
    # One line, at most 80 columns (gt 0.21.4): the window always rolls, so "rolling" is implied,
    # and the USD words are the short form of usd_words().
    words = "USD spend avoided" if billing() == "api" else "USD list-price equivalent"
    print("\n".join(fit(["gt net, %d days: %s token-equivalents, %.2f %s%s"
                         % (a.days, n(data["net"]), data["net_usd"], words,
                            "" if data["events"] else " -- nothing recorded yet")])))
    if data.get("malformed_lines"):
        # stderr, not stdout: `net` is one line of at most 80 columns by contract (a status line reads it)
        print(skipped_line(data["malformed_lines"]), file=sys.stderr)
    return 0


def cmd_farm(a):
    """The farm section alone (0.1.10): the same figures the report shows under its table."""
    a.project = a.profile = None
    since, evs, _rows, _o, _b, farm, stats = build(a)
    fd = farm_data(farm, evs, since, a.until, stats)
    if a.json:
        print(json.dumps(dict(fd, since=since, until=a.until,
                              malformed_lines=stats.get("malformed", 0)), indent=1))
    else:
        print("gt farm savings -- %s" % window_line(a, since))
        print("\n".join(farm_lines(fd)[1:]))
        if stats.get("malformed"):
            print(skipped_line(stats["malformed"]))
    return 0 if fd["packets"] else 4


def farm_glance(days=7, tail_bytes=None, path=None):
    """-> {packets, tokens, local_share, farm_eq, usd_avoided, estimated, days} for the farm lines
    of the last `days` days, or None when there are none. For gt_usage --now and the session-start
    brief: no transcript reading, no price table; `tail_bytes` limits the read to the ledger's
    end (the brief runs while the user waits)."""
    since = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    seen = {}
    try:
        with open(path or LEDGER, "rb") as fh:
            if tail_bytes:
                try:
                    fh.seek(-tail_bytes, os.SEEK_END)
                    fh.readline()
                except OSError:
                    fh.seek(0)
            for raw in fh.read().decode("utf-8", "replace").splitlines():
                if '"farm"' not in raw:
                    continue
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(ev, dict) and ev.get("mechanism") == "farm" and ev.get("key") \
                        and str(ev.get("ts", ""))[:10] >= since:
                    seen[ev["key"]] = ev
    except OSError:
        return None
    farm = [f for f in (farm_line(e) for e in seen.values()) if f and not f["failed"]]
    if not farm:
        return None
    tokens = sum(f["tokens"] for f in farm)
    usd = [f["usd"] for f in farm if f["usd"] is not None]
    return {"days": days, "packets": len(farm), "tokens": tokens,
            "local_share": round(sum(f["tokens"] for f in farm if f["local"]) / tokens, 4)
            if tokens else None,
            "farm_eq": round(sum(farm_eq(f) for f in farm)),
            "usd_avoided": round(sum(usd), 6) if usd else None,
            "estimated": sum(1 for f in farm if f["state"] == "estimated")}


def percentile(sorted_values, p):
    return sorted_values[min(len(sorted_values) - 1, int(p * len(sorted_values)))] if sorted_values else 0


def agent_type(path):
    """The agentType Claude Code recorded beside a subagent transcript (agent-<id>.meta.json)."""
    try:
        with open(path[:-len(".jsonl")] + ".meta.json", encoding="utf-8") as fh:
            v = json.load(fh).get("agentType")
        return str(v) if v else "unknown"
    except (OSError, ValueError, AttributeError):
        return "unknown"


def first_request(path):
    """-> (opening context tokens, cache-write tokens, cache-read tokens) of a transcript's first
    model request, or None. Stops at the first usage row: it is the cheapest read there is."""
    seen = set()
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                msg = row.get("message") if isinstance(row, dict) and isinstance(row.get("message"), dict) else {}
                usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
                if row.get("type") != "assistant" or not usage:
                    continue
                rd, wr = _num(usage, "cache_read_input_tokens"), _num(usage, "cache_creation_input_tokens")
                return _num(usage, "input_tokens") + rd + wr, wr, rd
    except OSError:
        return None
    return None


def measure_overhead(projects_dir, days, deadline=None):
    """-> {days, runs, by_type: {type: {runs, median, p25, p75, min, max}}, all: {...},
    write_share} from the subagent transcripts of the last `days` days. The opening context is
    system prompt + tool definitions + instructions: the agent has read nothing yet."""
    if _gos is None:
        return {"days": days, "runs": 0, "by_type": {}, "all": {}, "write_share": None}
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    by, allv, writes = {}, [], 0
    for path in _recent_first(_gos.transcript_files(projects_dir), since):
        if "/subagents/" not in path.replace(os.sep, "/"):
            continue
        if deadline is not None and time.monotonic() > deadline:
            break
        first = first_request(path)
        if not first:
            continue
        ctx, wr, rd = first
        by.setdefault(agent_type(path), []).append(ctx)
        allv.append(ctx)
        writes += wr > rd

    def stat(vals):
        v = sorted(vals)
        return {"runs": len(v), "median": int(statistics.median(v)) if v else 0, "p25": percentile(v, 0.25),
                "p75": percentile(v, 0.75), "min": v[0] if v else 0, "max": v[-1] if v else 0}
    return {"days": days, "runs": len(allv), "by_type": {k: stat(v) for k, v in sorted(by.items())},
            "all": stat(allv), "write_share": round(writes / len(allv), 3) if allv else None}


def cmd_overhead(a):
    projects = a.projects_dir or os.path.join(HOME, ".claude", "projects")
    doc = measure_overhead(projects, a.days, time.monotonic() + a.max_seconds)
    doc["measured_at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if a.save and doc["runs"]:
        try:
            os.makedirs(BASE, exist_ok=True)
            tmp = os.path.join(BASE, "overhead.json.tmp")
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(doc, fh, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, os.path.join(BASE, "overhead.json"))
        except OSError:
            print("savings: could not save overhead.json", file=sys.stderr)
    if a.json:
        print(json.dumps(doc, sort_keys=True, indent=1))
        return 0 if doc["runs"] else 4
    if not doc["runs"]:
        print("no subagent transcripts in the last %d days" % a.days)
        return 4
    print("what a subagent costs just to start: its first request, before it has read anything")
    print("(system prompt + tool definitions + instructions), last %d days, %d runs" % (a.days, doc["runs"]))
    print("%-24s %5s %9s %9s %9s %9s" % ("agent type", "runs", "median", "p25", "p75", "max"))
    for k, v in sorted(doc["by_type"].items(), key=lambda kv: -kv[1]["runs"]):
        print("%-24s %5d %9s %9s %9s %9s" % (k[:24], v["runs"], n(v["median"]), n(v["p25"]), n(v["p75"]), n(v["max"])))
    v = doc["all"]
    print("%-24s %5d %9s %9s %9s %9s" % ("ALL", v["runs"], n(v["median"]), n(v["p25"]), n(v["p75"]), n(v["max"])))
    if doc["write_share"] is not None:
        print("the first request was a cache WRITE (billed 1.25x) in %.0f%% of runs" % (100 * doc["write_share"]))
    if a.save:
        print("saved to %s; gt_break_even.py reads the %s row" % (os.path.join(BASE, "overhead.json"), OVERHEAD_AGENT))
    return 0


def cmd_net_check(a):
    stats = {}
    ledger = read_ledger(stats=stats)
    state = pause_state(list(ledger.values()))
    write_paused(state)
    evs = [e for e in ledger.values() if in_window(e, (datetime.datetime.now(datetime.timezone.utc)
                                                       - datetime.timedelta(days=PAUSE_DAYS)).strftime("%Y-%m-%d"), None)]
    farm, _bad = split_farm(evs)
    rows = summarise(row_events(evs, farm))
    over = summarise([e for e in evs if e.get("mechanism") == "overhead"]).get("overhead")
    table = net_table(rows, over)
    if a.json:
        print(json.dumps({"days": PAUSE_DAYS, "net_by_mechanism": table, "paused": state,
                          "malformed_lines": stats.get("malformed", 0)}, sort_keys=True, indent=1))
    else:
        print("\n".join(net_lines(table, state)).lstrip("\n"))
        if stats.get("malformed"):
            print(skipped_line(stats["malformed"]))
    return 0


def cmd_projects(a):
    counts = {}
    stats = {}
    for ev in read_ledger(stats=stats).values():
        proj = ev.get("project")
        if isinstance(proj, str) and proj:                   # a hand-edited number, flag, list or object is not a project
            counts[proj] = counts.get(proj, 0) + 1
    for p, n in sorted(counts.items()):
        print("%s  %d event(s)" % (p, n))
    if stats.get("malformed"):
        print(skipped_line(stats["malformed"]))
    return 0


def export_net(data):
    """The NET part of the export (0.21.4): numbers and mechanism names only."""
    rows = data.get("net_by_mechanism") or []
    if not rows:
        return []
    out = ["", "## Net per mechanism", "",
           "Kept out of the main context minus what the mechanism itself spent on Claude "
           "(token-equivalents; every row is estimated). No total: the rows have different "
           "counterfactuals.", "",
           "| mechanism | kept | spent | net | state |", "|---|---:|---:|---:|---|"]
    for t in rows:
        out.append("| %s | %s | %s | %s | %s |" % (t["label"], n(t["kept"]), n(t["spent"]),
                                                   n(t["net"]), t["state"]))
    if data.get("paused"):
        out += ["", "Paused (7-day net negative): " + ", ".join(sorted(data["paused"]))]
    return out


def export_farm(fd):
    """The farm part of the export (0.1.10). Scrubbed like the rest: a service name can be an
    internal host and a model id can be a file path, so engines are numbered, never named."""
    if not fd.get("packets"):
        return ["", "## Farmed packets", "", "No farm packets in this window."]
    out = ["", "## Farmed packets", "",
           "%d packet(s), %s raw tokens (prompt + completion), %s on local engines; %s; "
           "USD avoided at the services' reference rates: %s. Kept off Claude: %s of the "
           "window's work (estimated)."
           % (fd["packets"], n(fd["tokens"]), _pct(fd["local_tokens"], fd["tokens"]),
              "all measured" if not fd["estimated"] else "%d estimated" % fd["estimated"],
              _usd(fd["usd_avoided"]),
              "-" if fd["kept_off_claude"]["share"] is None
              else "%.1f%%" % (100 * fd["kept_off_claude"]["share"])), "",
           "| engine | where | packets | prompt | completion | state | wall s | tok/s | USD avoided | sent / relayed |",
           "|---|---|---:|---:|---:|---|---:|---:|---:|---|"]
    for i, g in enumerate([g for g in fd["rows"] if g["packets"]], 1):
        out.append("| engine %d | %s | %d | %s | %s | %s | %s | %s | %s | %d / %d |"
                   % (i, "local" if g["local"] else "other", g["packets"], n(g["prompt_tokens"]),
                      n(g["completion_tokens"]), g["state"],
                      "%.1f" % g["wall_s"] if g["wall_s"] else "-",
                      "%.1f" % g["tokens_per_s"] if g["tokens_per_s"] else "-",
                      _usd(g["usd_avoided"]), g["sent"], g["relayed"]))
    out += ["", "```", *farm_trend_lines(fd["trend"])[1:], "```"]
    return out


def cmd_export(a):
    """A scrubbed summary: per-mechanism totals and states and a bar chart. No session ids, no
    project ids, no task ids, no paths -- only numbers and the mechanism names above."""
    a.project = a.profile = a.since = a.until = None
    _, data = render(a)
    out = ["# gt savings summary", "",
           "Last %d days, rolling (each export shows the %d days before it). Token-equivalents at "
           "the policy tier's model (the session model for mains); %s. actual = what the work used with gt; counterfactual = the same "
           "work without that mechanism; saved = counterfactual - actual (a negative is extra cost). "
           "Every row carries how it was established." % (a.days, a.days, "USD is spend avoided at list price (API billing)"
                                 if billing() == "api" else "USD is a list-price equivalent, not a bill"), "",
           "| mechanism | actual | counterfactual | saved | state |", "|---|---:|---:|---:|---|"]
    labels = {k: l for k, l, _ in MECHANISMS}
    peak = max([abs(r.get("saved", 0)) for r in data["rows"]] + [1])
    chart = []
    for r in data["rows"]:
        if "saved" in r:
            out.append("| %s | %s | %s | %s | %s |" % (labels[r["mechanism"]], n(r["actual"]),
                                                       n(r["counterfactual"]), n(r["saved"]), r["state"]))
            chart.append("%-38s %s %s" % (labels[r["mechanism"]],
                                          "#" * max(0, round(24 * r["saved"] / peak)), n(r["saved"])))
        else:
            out.append("| %s | - | - | - | design (not measured) |" % labels[r["mechanism"]])
    out.append("| gt overhead (Rule injection) | %s | 0 | %s | a cost |"
               % (n(data["overhead"]), n(-data["overhead"])))
    out.append("| **TOTAL** | **%s** | **%s** | **%s** | design rows not counted |"
               % (n(data["total_actual"]), n(data["total_counterfactual"]), n(data["net"])))
    out += ["", "**Net: %s token-equivalents** (%.2f %s)" % (n(data["net"]), data["net_usd"], usd_words())]
    if data.get("core_rules_net") is not None:
        out += ["", "**Rules, net of their own cost: %s token-equivalents** (%.2f %s, estimated)"
                % (n(data["core_rules_net"]), data["core_rules_net_usd"], usd_words())]
    out += ["", "```", *chart, "```"]
    if data.get("malformed_lines"):
        out += ["", skipped_line(data["malformed_lines"])]
    out += export_net(data)
    out += export_farm(data.get("farm") or {})
    text = "\n".join(out) + "\n"
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("savings: wrote %s" % a.out)
    else:
        sys.stdout.write(text)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="gt's measured token savings")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("record", help="record events from the transcripts (silent with --hook)")
    p.add_argument("--hook", action="store_true")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--projects-dir")
    p.add_argument("--max-seconds", type=float, default=5.0)
    p = sub.add_parser("ingest-run", help="tokens one ingest run's specialist agents kept out of the session")
    p.add_argument("--since", required=True)
    p.add_argument("--session", action="append")
    p.add_argument("--run")
    p.add_argument("--projects-dir")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("note-read", help="record tokens read vs the full set (gt-open, digest)")
    p.add_argument("--mechanism", required=True)
    p.add_argument("--read", nargs="+", required=True)
    p.add_argument("--instead", nargs="+", required=True)
    p.add_argument("--project")
    p = sub.add_parser("benchmark", help="record a head-to-head run of one task, with and without gt")
    p.add_argument("--task", required=True)
    p.add_argument("--with-gt", type=int, required=True)
    p.add_argument("--without-gt", type=int, required=True)
    p = sub.add_parser("overhead", help="what a subagent costs just to start, from the transcripts")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--save", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--projects-dir")
    p.add_argument("--max-seconds", type=float, default=60.0)
    p = sub.add_parser("net-check", help="the trailing 7-day net per mechanism; writes the pause state")
    p.add_argument("--json", action="store_true")
    for n in ("report", "net", "export", "projects", "farm"):
        p = sub.add_parser(n)
        p.add_argument("--days", type=int, default=7)
        if n == "farm":
            p.add_argument("--since")
            p.add_argument("--until")
            p.add_argument("--json", action="store_true")
        if n == "report":
            p.add_argument("--since")
            p.add_argument("--until")
            p.add_argument("--project")
            p.add_argument("--profile", choices=PROFILES)
            p.add_argument("--json", action="store_true")
            p.add_argument("--full", action="store_true", help="add the NET and FARM sections")
            p.add_argument("--by-model", action="store_true", help="add mechanism x model rows (they add up to the table)")
        if n == "export":
            p.add_argument("--out")
    a = ap.parse_args(argv)
    return {"record": cmd_record, "ingest-run": cmd_ingest_run, "note-read": cmd_note_read, "benchmark": cmd_benchmark,
            "report": cmd_report, "net": cmd_net, "export": cmd_export,
            "projects": cmd_projects, "farm": cmd_farm, "overhead": cmd_overhead,
            "net-check": cmd_net_check}[a.cmd](a)


if __name__ == "__main__":
    hook = "--hook" in sys.argv[1:]
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:                                        # noqa: BLE001
        if not hook:
            raise
        sys.exit(0)                                          # never block a session start
