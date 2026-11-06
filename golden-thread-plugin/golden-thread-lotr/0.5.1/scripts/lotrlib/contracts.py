"""What the model is told about an operation, and how much (gt-lotr 0.5.0, gt 0.21.4).

Request 2026-10-08-lotr-acs-capability-index-and-contracts: ideas from the Agent Capability
Standard. The model plans with two layers and the gateway keeps a third to itself:

  find results     one short entry per operation: id, a one-line OUTCOME, tier, risk class. At most
                   ITEM_TOKENS each, FIND_BUDGET_TOKENS in all; over budget the most relevant
                   connections come first and the rest are counted, not shown.
  describe         the planning contract of ONE operation, asked for when the model has chosen it:
                   what it does, what it takes, how risky it is, whether somebody must confirm it.
                   Never an endpoint, an auth detail or a raw schema.
  invocation       method, path, headers, credential, wire schema: held by the gateway, never sent.

Pure functions and constants; the only file reads are the lint's, over the shipped templates.

Tokens here are an ESTIMATE, the same one gt-usage's ledger uses everywhere: characters of compact
JSON / 4. A figure built on them is labelled `estimated`.
"""
import json
import re
from pathlib import Path

from . import profiles as profiles_mod
from .redact import scrub_text

CHARS_PER_TOKEN = 4
ITEM_TOKENS = 50               # one operation's entry in a find result (the request's ~50)
FIND_BUDGET_TOKENS = 1200      # a whole find result; gateway.json limits.find_budget_tokens
MIN_OUTCOME_CHARS = 24         # an outcome clipped below this is not an outcome: the entry is dropped
SUMMARY_MAX_CHARS = 90         # the longest outcome a SHIPPED op or recipe may carry (the lint)
DESCRIBE_OUTCOME_CHARS = 200
INPUT_DESCRIPTION_CHARS = 120
ELLIPSIS = "…"
SHIPPED = Path(__file__).resolve().parents[2] / "templates"

RISK = {"read": "read", "write": "write", "consent": "destructive"}


def tokens(obj):
    """Estimated tokens of `obj`: a string as it is, anything else as compact JSON; chars / 4."""
    text = obj if isinstance(obj, str) else json.dumps(obj, separators=(",", ":"), ensure_ascii=False,
                                                       default=str)
    return -(-len(text) // CHARS_PER_TOKEN)


def risk_of(tier):
    """read | write | destructive. An unknown tier is a write: towards the more guarded class."""
    return RISK.get(tier, "write")


def one_line(text, limit=None):
    """`text` as a single line, whitespace folded; clipped at a word boundary with an ellipsis."""
    s = " ".join(str(text or "").split())
    if limit and len(s) > limit:
        s = s[:limit - 1]
        sp = s.rfind(" ")
        s = (s[:sp] if sp >= MIN_OUTCOME_CHARS else s).rstrip(" ,;:-") + ELLIPSIS
    return s


# ---------------------------------------------------------------------------------- find budget

def fit_item(item, cap=ITEM_TOKENS):
    """`item` (a find entry) with its summary clipped, at a word boundary, until the whole entry is
    within `cap` tokens. The entry's other fields are never touched; a summary that would have to
    go below MIN_OUTCOME_CHARS means the entry cannot be shown: None, and the caller counts it."""
    text = one_line(scrub_text(item.get("summary") or ""))
    cur = dict(item, summary=text)
    while tokens(cur) > cap:
        over = (tokens(cur) - cap) * CHARS_PER_TOKEN
        text = text[:max(len(text) - max(over, 1) - 1, 0)].rstrip()
        sp = text.rfind(" ")
        if sp >= MIN_OUTCOME_CHARS:
            text = text[:sp]
        text = text.rstrip(" ,;:-")
        if len(text) < MIN_OUTCOME_CHARS:
            return None
        cur = dict(item, summary=text + ELLIPSIS)
    return cur


def fit_budget(items, budget):
    """-> (items kept, how many were left out). Within budget nothing changes, not even the order.
    Over it, the connections are ordered by their best entry (the list arrives most relevant
    first), each connection's entries stay together in their own order, and the list is cut where
    the budget runs out."""
    if sum(tokens(i) for i in items) <= budget:
        return list(items), 0
    first = {}
    for n, it in enumerate(items):
        first.setdefault(it.get("connection"), n)
    ordered = sorted(items, key=lambda it: first[it.get("connection")])     # stable
    kept, used = [], 0
    for n, it in enumerate(ordered):
        t = tokens(it)
        if used + t > budget:
            return kept, len(ordered) - n
        kept.append(it)
        used += t
    return kept, 0


# ---------------------------------------------------------------------------------- describe

def effects_text(tier):
    if tier == "read":
        return "Reads data. It changes nothing."
    if tier == "write":
        return "Changes data on the connection. A later change can usually undo it."
    return "Irreversible or outward: sends, merges or deletes. It cannot be quietly undone."


def confirmation_text(tier, token_needed):
    if tier == "consent":
        text = ("The owner approves every run (a dialog or a touch); you cannot approve it for "
                "them.")
    else:
        text = "None beyond the gateway's own policy."
    if token_needed:
        text += " A single-use token the user mints for exactly this call must come with it."
    return text


def inputs_list(params, required):
    """[{name, required, description}] from an op's `params` (name -> text) or a recipe's
    (name -> {description, default}); required ones first. The wire shape is not part of it."""
    out = []
    for name, spec in (params or {}).items():
        text = spec.get("description") if isinstance(spec, dict) else spec
        out.append({"name": str(name), "required": name in required,
                    "description": one_line(scrub_text(text or ""), INPUT_DESCRIPTION_CHARS)})
    out.sort(key=lambda i: (not i["required"], i["name"]))
    return out


def _tool_def(cid, name, description, params):
    """What injecting this operation as an ordinary MCP tool would put in the context."""
    return {"name": "%s__%s" % (cid, name), "description": description,
            "inputSchema": {"type": "object",
                            "properties": {p: {"type": "string", "description": d}
                                           for p, d in (params or {}).items()}}}


def injection_tokens(conn, prof, recipe_list):
    """Estimated tokens of loading ONE connection's operations as full tool definitions, the way
    a client that connects to the MCP server directly would. An MCP connection's real cached tool
    definitions are the baseline for it; a built-in profile is rendered the same way."""
    if conn.get("kind") == "mcp" and conn.get("tools"):
        return tokens(conn["tools"])
    total = 0
    for op in prof.get("ops", []):
        total += tokens(_tool_def(conn["id"], op["name"], op["summary"], op.get("params")))
    for r in recipe_list:
        total += tokens(_tool_def(conn["id"], r["name"], r["summary"],
                                  {p: (s or {}).get("description", "")
                                   for p, s in (r.get("params") or {}).items()}))
    return total


# ---------------------------------------------------------------------------------- the lint

# What an API-shaped summary looks like. Deliberately the unambiguous markers only: a method with a
# path, a URL or host, a path of two segments, a template placeholder, a query string, a search
# qualifier (is:pr), code syntax (a = b, f()), or the vocabulary of the wire (endpoint, REST, JSON,
# GraphQL, payload). Wording that is merely technical ("pull request") is not flagged.
_LINT = (
    (re.compile(r"\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s*[/{<$]"), "an HTTP method and path"),
    (re.compile(r"https?://|\bwww\.", re.I), "a URL"),
    # lower case only: GitLab.com is a product's name, docs.example.com is an address
    (re.compile(r"\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|dev|ai|app|cloud|co|example|"
                r"local|internal|test)\b"), "a host name"),
    (re.compile(r"(?<![\w.)])/[A-Za-z0-9_.{}$-]+(?:/[A-Za-z0-9_.{}$:()'-]+)+"), "a URL path"),
    (re.compile(r"\{\{?[A-Za-z_][\w.]*\}?\}"), "a template placeholder"),
    (re.compile(r"[?&][A-Za-z_][\w.]*="), "a query string"),
    (re.compile(r"\b[a-z][a-z-]+:[@\w][\w@/.-]*"), "search or query syntax (key:value)"),
    (re.compile(r"\w\(\)|\w\s*[=<>]{1,2}\s*\w"), "code syntax"),
    (re.compile(r"\bREST(?:ful)?\b|\bHTTPS?\b"), "wire vocabulary (REST, HTTP)"),
    (re.compile(r"\b(?:endpoint|JSON|XML|GraphQL|OData|SOAP|payload|request body|status code|"
                r"query string)\b", re.I), "wire vocabulary (endpoint, JSON, payload ...)"),
)


def lint_summary(text, max_chars=SUMMARY_MAX_CHARS):
    """-> problems in plain words; [] when `text` reads as what the call achieves. `max_chars` 0
    skips the length rule (a template's summary is a sentence for a person, not an entry)."""
    s = text if isinstance(text, str) else ""
    if not s.strip():
        return ["is empty: say what the call achieves"]
    out = []
    if "\n" in s or "\r" in s or "\t" in s:
        out.append("is more than one line")
    if max_chars and len(s) > max_chars:
        out.append("is %d characters; keep it to %d so an entry fits its token budget"
                   % (len(s), max_chars))
    for rx, what in _LINT:
        if rx.search(s):
            out.append("contains %s: say what the call achieves, not how it is made" % what)
    return out


def _json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def lint_shipped(templates=None, report=False):
    """The outcome-wording check over what ships: every curated op summary, every recipe's
    summary, every connection template's summary and connect.description (a description lands in
    the connection catalog the model reads), and the bundled catalog's templates. A scaffold that
    still says TODO is skipped: `lotr template check` already refuses it.

    -> [(where, problem)], or with report=True how many of each kind were looked at."""
    root = Path(templates) if templates else SHIPPED
    problems, counts = [], {}

    def look(kind, name, texts, max_chars):
        counts[kind] = counts.get(kind, 0) + 1
        for field, text in texts:
            if isinstance(text, str) and "TODO" in text:
                return
        for field, text in texts:
            if text is None:
                continue
            for p in lint_summary(text, max_chars):
                problems.append(("%s %s" % (kind, name), "%s %s" % (field, p)))

    for prof in profiles_mod.PROFILES.values():
        for op in prof["ops"]:
            look("op", "%s.%s" % (prof["name"], op["name"]), [("summary", op["summary"])],
                 SUMMARY_MAX_CHARS)
    for kind, sub, shape in (("recipe", "recipes", "recipe"),
                             ("connection-template", "connections", "template"),
                             ("catalog-template", "catalog/templates", "template")):
        d = root / sub
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.json")):
            data = _json(f)
            if data is None:
                problems.append(("%s %s" % (kind, f.name), "is not a JSON object"))
                continue
            label = "%s (%s)" % (f.name, data.get("name"))
            if shape == "recipe":
                look(kind, label, [("summary", data.get("summary"))], SUMMARY_MAX_CHARS)
            else:
                connect = data.get("connect") if isinstance(data.get("connect"), dict) else {}
                look(kind, label, [("summary", data.get("summary")),
                                   ("connect.description", connect.get("description"))], 0)
    return counts if report else problems
