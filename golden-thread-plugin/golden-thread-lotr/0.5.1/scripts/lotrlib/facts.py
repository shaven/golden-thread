"""The fact sheet: what a research run records beside a template (0.5.0).

A template is only as good as the facts it was filled from. A research run (the gt-lotr-research
skill) writes TWO files for one service: the template (what LOTR uses) and this fact sheet (why
each field has the value it has). Every fact carries the page it came from and how far it was
checked, so a later reader, or a later run, knows what to re-check first.

    {
      "schema": 1,
      "template": "linear",                 # the template this sheet belongs to
      "service": "Linear",
      "archetype": "mcp-oauth",
      "researched": "2026-10-14",
      "researcher": "claude (research run)",
      "facts": [
        {"id": "f1", "field": "connect.url", "value": "https://mcp.linear.app/mcp",
         "claim": "Linear's hosted MCP server is at mcp.linear.app/mcp",
         "source": "https://linear.app/docs/mcp", "retrieved": "2026-10-14",
         "state": "self-verified", "evidence": "short quote or what was checked"}
      ],
      "choices": [{"decision": "...", "because": "...", "facts": ["f1"]}],
      "open": ["what could not be verified"]
    }

Verification states (the vocabulary is closed):
  unverified             read on one page; nothing cross-checked
  self-verified          the researcher cross-checked it against a second source or a live,
                         credential-free check (a public discovery document, a package registry)
  independently-verified a second agent, given only the claim and the sources, re-derived it
                         (needs verified_by and verified_on)

A fact sheet never carries a secret value: a value that looks like one is refused, and the sheet
holds nothing about any account.
"""
import re
from urllib.parse import urlparse

from .secretcheck import looks_like_secret

STATES = ("unverified", "self-verified", "independently-verified")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_TOP = {"schema", "template", "service", "archetype", "researched", "researcher", "facts", "choices",
        "open"}
_FACT = {"id", "field", "value", "claim", "source", "retrieved", "state", "evidence", "verified_by",
         "verified_on"}

# the template fields a wrong value in makes the connection fail or reach the wrong place: each must
# be backed by a fact, or the template is saying something nobody checked
LOAD_BEARING = ("connect.url", "connect.base_url", "connect.command", "connect.runtime",
                "connect.scopes", "connect.auth_scheme", "connect.auth_header",
                "connect.client_registration", "network.hosts")


def _get(obj, path):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _https(u):
    p = urlparse(u) if isinstance(u, str) else None
    return bool(p and p.scheme == "https" and p.hostname and not p.username and not p.password)


def check(sheet, template=None):
    """-> {'ok', 'problems': [..], 'warnings': [..], 'counts': {state: n}, 'weakest': state|None}.
    `template` (the parsed template) enables the cross-checks: every load-bearing field is backed
    by a fact, and a fact that states a value agrees with the template."""
    problems, warnings = [], []
    counts = {s: 0 for s in STATES}
    if not isinstance(sheet, dict):
        return {"ok": False, "problems": ["a fact sheet must be a JSON object"], "warnings": [],
                "counts": counts, "weakest": None}
    for k in sorted(set(sheet) - _TOP):
        problems.append(f"unknown key {k!r}")
    if sheet.get("schema") != 1:
        problems.append("'schema' must be 1")
    for k in ("template", "service", "researcher"):
        if not isinstance(sheet.get(k), str) or not sheet[k].strip():
            problems.append(f"'{k}' must be a non-empty string")
    if not isinstance(sheet.get("researched"), str) or not _DATE.match(sheet.get("researched") or ""):
        problems.append("'researched' must be a date like 2026-10-14")
    facts = sheet.get("facts")
    if not isinstance(facts, list) or not facts:
        problems.append("'facts' must be a non-empty list")
        facts = []
    seen, covered = set(), {}
    for i, f in enumerate(facts):
        tag = f"facts[{i}]"
        if not isinstance(f, dict):
            problems.append(f"{tag} must be an object")
            continue
        for k in sorted(set(f) - _FACT):
            problems.append(f"{tag}: unknown key {k!r}")
        fid = f.get("id")
        if not isinstance(fid, str) or not _ID.match(fid):
            problems.append(f"{tag}: 'id' must be a short lowercase id like f1")
        elif fid in seen:
            problems.append(f"{tag}: id {fid!r} is used twice")
        else:
            seen.add(fid)
            tag = f"fact {fid}"
        if not isinstance(f.get("claim"), str) or not f["claim"].strip():
            problems.append(f"{tag}: 'claim' (what is being asserted) is missing")
        if not _https(f.get("source")):
            problems.append(f"{tag}: 'source' must be the https URL of the page it came from")
        if not isinstance(f.get("retrieved"), str) or not _DATE.match(f.get("retrieved") or ""):
            problems.append(f"{tag}: 'retrieved' must be the date you read it, like 2026-10-14")
        st = f.get("state")
        if st not in STATES:
            problems.append(f"{tag}: 'state' must be one of {', '.join(STATES)}")
        else:
            counts[st] += 1
            if st == "independently-verified" and not (f.get("verified_by") and
                                                       _DATE.match(str(f.get("verified_on") or ""))):
                problems.append(f"{tag}: independently-verified needs verified_by and verified_on")
        ev = f.get("evidence")
        if ev is not None and (not isinstance(ev, str) or len(ev) > 400):
            problems.append(f"{tag}: 'evidence' must be a short string (400 characters at most): "
                            "a quote or what was checked, not a page")
        for key in ("claim", "evidence", "value"):
            v = f.get(key)
            if isinstance(v, str) and looks_like_secret("", v):
                problems.append(f"{tag}: '{key}' looks like a secret; a fact sheet never holds one")
            if isinstance(v, str) and re.search(r"(?i)\b(sk-|ghp_|xox[bp]-)[A-Za-z0-9]{10,}", v):
                problems.append(f"{tag}: '{key}' looks like a key or token")
        fld = f.get("field")
        if fld is not None:
            if not isinstance(fld, str) or not re.match(r"^[a-z_]+(\.[a-z_]+)*$", fld):
                problems.append(f"{tag}: 'field' must be a template path like connect.url")
            else:
                covered.setdefault(fld, []).append((tag, f))
                if template is not None and "value" in f:
                    ok, actual = _get(template, fld)
                    if not ok:
                        problems.append(f"{tag}: the template has no field {fld}")
                    elif actual != f["value"]:
                        problems.append(f"{tag}: states {fld} = {f['value']!r} but the template "
                                        f"says {actual!r}")
    for c in sheet.get("choices") or []:
        if not isinstance(c, dict) or not isinstance(c.get("decision"), str):
            problems.append("each 'choices' entry needs a 'decision' string")
            continue
        for ref in c.get("facts") or []:
            if ref not in seen:
                problems.append(f"choice {c['decision'][:40]!r} names unknown fact {ref!r}")
    if not isinstance(sheet.get("open", []), list) or not all(isinstance(x, str) for x in sheet.get("open", [])):
        problems.append("'open' must be a list of strings (what could not be verified)")
    if template is not None:
        if isinstance(sheet.get("template"), str) and sheet["template"] != template.get("name"):
            problems.append(f"the sheet is for template {sheet['template']!r}, not {template.get('name')!r}")
        if sheet.get("archetype") and sheet["archetype"] != template.get("archetype"):
            problems.append("the sheet's archetype is not the template's archetype")
        for path in LOAD_BEARING:
            present, _val = _get(template, path)
            if present and path not in covered:
                problems.append(f"template field {path} is not backed by any fact (add a fact "
                                f"with \"field\": \"{path}\")")
        for path, fs in covered.items():
            if path in LOAD_BEARING and all(f.get("state") == "unverified" for _t, f in fs):
                warnings.append(f"{path} rests only on an unverified fact")
    weakest = next((s for s in STATES if counts[s]), None)
    if counts["unverified"]:
        warnings.append(f"{counts['unverified']} fact(s) are unverified")
    return {"ok": not problems, "problems": problems, "warnings": warnings, "counts": counts,
            "weakest": weakest}
