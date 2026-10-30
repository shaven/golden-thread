"""`lotr onboard compare ID` and `lotr onboard tune ID` (gt-lotr 0.5.1, plan phase 42).

The call script (lotr_agent_call.py) keeps the last 10 stored results per connection: the full result and a small
record (route, sizes, the summary). `compare` lays the full result beside its summary so a person can decide whether
the summary was good enough; `tune` changes the thresholds when it was not.

Neither makes a model call or a gateway call. The output is labelled `estimated` (characters / 4), is at most 100
columns wide, and holds no credential-shaped string: a record that does is shown as withheld.
"""
import json
import re
import textwrap
from itertools import zip_longest
from pathlib import Path

from . import onboard_discover, onboard_route, shaping
from .errors import GatewayError
from .util import atomic_write

WIDTH = 100
ROWS = 14
MIN_DIRECT, MAX_DIRECT = 100, 200_000


def side_by_side(left, right, width=WIDTH, rows=ROWS):
    """Two texts in two columns, each line at most `width` characters, at most `rows` rows."""
    col = (width - 3) // 2

    def lines(text):
        out = []
        for line in str(text).split("\n"):
            out += textwrap.wrap(line, col, break_long_words=True, break_on_hyphens=False, replace_whitespace=False,
                                 drop_whitespace=False) or [""]
        return out

    a, b = lines(left), lines(right)
    cut = max(len(a), len(b)) > rows
    a, b = a[:rows], b[:rows]
    table = ["%s | %s" % (x.ljust(col), y.ljust(col)) for x, y in zip_longest(a, b, fillvalue="")]
    if cut:
        table.append("[rows cut at %d; the whole result is in the file named below]" % rows)
    return [t.rstrip() for t in table]


def field_names(obj, found=None, depth=0):
    found = set() if found is None else found
    if depth > 6 or len(found) > 300:
        return found
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                found.add(k)
            field_names(v, found, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:50]:
            field_names(v, found, depth + 1)
    return found


def _when(stem):
    m = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})", stem)
    return "%s-%s-%s %s:%s:%s" % m.groups() if m else stem


def _records(home, cid, last):
    folder = Path(home) / "onboard" / "records" / cid
    if not folder.is_dir():
        return folder, []
    recs = sorted(folder.glob("*.record.json"), reverse=True)
    return folder, recs[:last]


def _safe(text):
    found = shaping.scan_credentials(text)
    if found:
        return "[withheld: credential-shaped text (%s)]" % ", ".join(sorted({f["rule"] for f in found}))
    return text


def compare(home, cid, last=3):
    if not onboard_discover.CONN_OK.match(cid or ""):
        raise GatewayError("bad_request", "the connection id must look like name@zone")
    folder, recs = _records(home, cid, last)
    if not recs:
        return "No stored results for %s yet: a result is stored when it is larger than the direct limit.\n" % cid
    out = []
    for rec_path in recs:
        stem = rec_path.name[:-len(".record.json")]
        try:
            rec = json.loads(rec_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out.append("== unreadable record %s\n" % stem)
            continue
        full_path = folder / (stem + ".full.json")
        try:
            full_obj = json.loads(full_path.read_text(encoding="utf-8"))
            pretty = json.dumps(full_obj, indent=1, ensure_ascii=False)
        except (OSError, ValueError):
            full_obj, pretty = None, "(the full result file is gone)"
        summary = rec.get("summary") or ""
        facts = rec.get("facts") or []
        shown = summary + ("\n" + "\n".join("- %s" % f for f in facts) if facts else "")
        if rec.get("route") != "summarised":
            shown = "(not summarised: %s)" % (rec.get("why") or "no reason recorded")
        n_full, n_shown = int(rec.get("tokens_full") or 0), int(rec.get("tokens_shown") or 0)
        pct = (100.0 * n_shown / n_full) if n_full else 0.0
        head = ["== %s  %s  (%s)" % (rec.get("op"), rec.get("route"), _when(stem)),
                "  full %s tokens -> summary %s tokens (%.1f%% of the full; estimated, characters / 4)" % (
                    format(n_full, ","), format(n_shown, ","), pct)]
        if rec.get("route") == "summarised" and full_obj is not None:
            blob = (shown + " " + " ".join(map(str, facts))).lower()
            missing = sorted(k for k in field_names(full_obj) if k.lower() not in blob)
            head.append("  not in the summary: %s" % (", ".join(missing[:12]) + (" ..." if len(missing) > 12 else "")
                                                       if missing else "(every field name is mentioned)"))
        if rec.get("truncated"):
            head.append("  the summariser said it left out detail that could matter")
        table = side_by_side(_safe(pretty), _safe(shown))
        out.append("\n".join(head + ["  " + "FULL".ljust((WIDTH - 5) // 2) + " | SUMMARY"] + ["  " + t[:WIDTH - 2] for t in table]
                             + ["  full result: %s" % full_path]) + "\n")
    text = "\n".join(out)
    text = "\n".join(l[:WIDTH] for l in text.split("\n"))
    return _safe(text) + ("" if text.endswith("\n") else "\n")


def tune(home, cid, direct_tokens=None, never=(), summarise_again=()):
    if not onboard_discover.CONN_OK.match(cid or ""):
        raise GatewayError("bad_request", "the connection id must look like name@zone")
    mf = Path(home) / "onboard" / ("%s.manifest.json" % cid)
    try:
        manifest = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise GatewayError("no_manifest", "no manifest for %s" % cid, hints=["run: lotr onboard discover %s" % cid])
    ops = {o["name"] for o in manifest.get("ops", [])}
    cur = onboard_route.settings(home, cid)
    new_direct = cur["direct_tokens"]
    if direct_tokens is not None:
        try:
            new_direct = int(str(direct_tokens))
        except ValueError:
            raise GatewayError("bad_request", "--direct-tokens must be a whole number")
        if not MIN_DIRECT <= new_direct <= MAX_DIRECT:
            raise GatewayError("bad_request", "--direct-tokens must be between %d and %d" % (MIN_DIRECT, MAX_DIRECT))
    never_list = list(cur["never_summarise"])
    for op in list(never) + list(summarise_again):
        if op not in ops:
            raise GatewayError("bad_request", "%r is not one of this connection's operations" % op)
    for op in never:
        if op not in never_list:
            never_list.append(op)
    never_list = [o for o in never_list if o not in set(summarise_again)]
    changed = direct_tokens is not None or bool(never) or bool(summarise_again)
    out = {"connection": cid, "direct_tokens": {"old": cur["direct_tokens"], "new": new_direct},
           "never_summarise": never_list, "file": None}
    if changed:
        path = Path(home) / "onboard" / ("%s.tune.json" % cid)
        atomic_write(path, json.dumps({"direct_tokens": new_direct, "never_summarise": never_list}, indent=2) + "\n", 0o600)
        out["file"] = str(path)
    return out
