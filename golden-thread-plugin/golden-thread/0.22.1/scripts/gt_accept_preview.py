"""gt_accept_preview.py -- what a presence dialog for `gt_broker.py accept` shows, in ONE place (0.22.0).

Two sides use it and must agree: the client (gt_accept: may this change go to the authority at all,
and how many hidden characters does a terminal review count) and the authority (gt_unlockd_methods
accept_presence: compose the dialog from the two texts it was sent, never from caller prose).

THE BOUNDS ARE ONE TABLE. The macOS helper (gt-presence.swift) refuses any prompt over HELPER_CAP
characters, and a refusal there means no dialog was shown. So the preview limits (changed lines,
columns), the header and the footer are derived together and tests keep the whole composed prompt,
worst case, within the cap. Python counts code points, the helper counts Characters, so the check is
conservative. How the real Touch ID sheet renders HELPER_CAP characters is UNVERIFIED on hardware.

HIDDEN CHARACTERS (one predicate, `hidden_flags`). Printable ASCII, tab and newline pass. Of the rest
only letters, digits, punctuation and symbols of non-blank categories pass, plus a combining mark
that sits on a visible base character (at most MAX_MARKS in a row). Everything else is hidden:
controls, format characters (bidi, zero width, tags), separators, every non-ASCII space, private use
and unassigned code points, the variation selectors (U+FE00-FE0F, U+E0100-E01EF), and the blank
letters (U+034F, U+115F, U+1160, U+17B4-17B5, U+180B-180F, U+3164, U+FFA0, U+2800). A change that
holds one is `not_previewable` (a terminal accept); a terminal `full` review shows it as <U+XXXX>.
A dialog is stricter still: no tab in a changed line, no right-to-left letter (bidi R/AL) anywhere it
shows, no hidden character in the requester or target, and no changed line ending in a flag-shaped
suffix the change could use to label itself.
"""
from __future__ import annotations

import difflib
import re
import unicodedata

# ---- the table: every bound the dialog depends on -------------------------------------------
HELPER_CAP = 500            # gt-presence.swift reasonOf: r.count <= 500 (a test pins the two)
MAX_CHANGED = 4             # changed lines shown whole; more is a terminal accept
MAX_COLS = 44               # a changed line longer than this would be cut: terminal accept
CTX_COLS = 28               # a context line is cut to this (it is not part of the change)
MAX_TEXT = 200000           # characters of either text
MAX_TARGET = 56             # a longer target path is not shown whole: terminal accept
MAX_REQUESTER = 40          # the asking process's description, cut to this
MAX_MARKS = 4               # combining marks one base character may carry
TAG_MAX = 12                # longest flag the authority appends to a row
HEAD_MAX = len("ACCEPT held write? ") + MAX_REQUESTER + 1 + MAX_TARGET + 1 \
    + len("conflict ") + 12 + len(" keep current, 9 changed, sha ") + 16 + 1
FOOT = "Touch only if this is the change."
BODY_BUDGET = HELPER_CAP - HEAD_MAX - len(FOOT)

_BLANK = frozenset([0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x180B, 0x180C, 0x180D, 0x180E,
                    0x180F, 0x3164, 0xFFA0, 0x2800])
_HIDDEN_CATS = frozenset(["Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp", "Zs"])


class NotPreviewable(Exception):
    """The change cannot be shown whole in a dialog; accept it at a terminal."""


def _always_hidden(ch):
    o = ord(ch)
    if o in _BLANK or 0xFE00 <= o <= 0xFE0F or 0xE0100 <= o <= 0xE01EF:
        return True
    if o < 0x80:
        return not (0x20 <= o < 0x7F)
    return unicodedata.category(ch) in _HIDDEN_CATS


def hidden_flags(text, keep="\n\t"):
    """-> [bool per character]: True where the character cannot be seen. `keep` characters (the
    newline and tab a diff is made of) are never flagged."""
    out, base, run = [], False, 0
    for ch in text:
        if ch in keep:
            out.append(False)
            base, run = False, 0
        elif _always_hidden(ch):
            out.append(True)
            base, run = False, 0
        elif unicodedata.category(ch)[0] == "M":
            run += 1
            out.append(not base or run > MAX_MARKS)
        else:
            out.append(False)
            base, run = ch != " ", 0
    return out


def hidden_count(text, keep="\n\t"):
    return sum(hidden_flags(text, keep))


def reveal(text):
    """-> (text with every hidden character shown as <U+XXXX>, how many there were)."""
    flags = hidden_flags(text)
    return "".join("<U+%04X>" % ord(c) if f else c for c, f in zip(text, flags)), sum(flags)


# ---- targets a presence approval never covers ------------------------------------------------
# Folders a presence approval never writes into, matched by name at ANY depth and in any case (the
# vault disk folds case): the Core rules, immutable Sources, and gt's own spool, sessions and tools.
TERMINAL_DIRS = frozenset(["core-rules", "sources", "spool", "sessions", "tools"])


def terminal_only_name(rel):
    """True for a target the presence route never covers, decided by NAME alone (the authority has
    no vault): global-memory/ and a design.md in any spelling gt's review-target rule knows, and a
    TERMINAL_DIRS folder anywhere in the path. The client also asks gt_review_target with the vault
    (links)."""
    try:
        import gt_review_target as R
        parts = [p for p in str(rel).replace("\\", "/").split("/") if p not in ("", ".")]
        if R._spelling(parts) is not None:
            return True
        return any(R.fold(p) in TERMINAL_DIRS for p in parts[:-1])
    except Exception:                                           # noqa: BLE001 - cannot tell = yes
        return True


# ---- right-to-left letters ----------------------------------------------------------------------
# A letter of bidi class R or AL (Hebrew, Arabic, ...) is visible, so hidden_flags passes it, but in
# a dialog it can reorder the words around it. Nothing the dialog shows may hold one.
def rtl_count(text):
    return sum(1 for ch in text if unicodedata.bidirectional(ch) in ("R", "AL"))


# A changed line may not end in text shaped like a flag the authority appends: the change could
# otherwise dress a change of meaning as cosmetic.
_FORGED_TAG = re.compile(r"\[\s*(?:ws\s+only|same\s+text|moved)\s*\]\s*$", re.IGNORECASE)


# ---- the changed lines, with a place and a little context --------------------------------------
def _lines(t):
    """Split on newline ONLY (a CR or U+2028 stays in its line, where it is seen as hidden); each
    line keeps its newline, so a missing final newline is a difference."""
    if not t:
        return []
    parts = t.split("\n")
    out = [p + "\n" for p in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _ws_free(s):
    return re.sub(r"\s+", "", s)


def build(cur, new):
    """-> {"changed": N, "rows": [row text], "texts": [every shown text]} or raises NotPreviewable.
    A row is `<line no><+|-| ><text>[ <flag>]`: a removed line carries its number in the current
    text, an added or context line its number in the new text. Flags: [same text] (only the line
    ending differs), [ws only], [moved] (the same text is removed and added elsewhere)."""
    cur, new = cur or "", new or ""
    if len(cur) > MAX_TEXT or len(new) > MAX_TEXT:
        raise NotPreviewable("the file is too large to approve from a dialog")
    a, b = _lines(cur), _lines(new)
    hunks, h = [], None
    for ln in difflib.unified_diff(a, b, n=1, lineterm=""):
        if ln.startswith(("--- ", "+++ ")) and h is None:
            continue
        m = _HUNK.match(ln)
        if m:
            h = {"o": int(m.group(1)), "n": int(m.group(2)), "rows": []}
            hunks.append(h)
            continue
        if h is None:
            continue
        mk, body = ln[:1], ln[1:]
        if mk == "-":
            h["rows"].append(("-", h["o"], body))
            h["o"] += 1
        elif mk == "+":
            h["rows"].append(("+", h["n"], body))
            h["n"] += 1
        else:
            h["rows"].append((" ", h["n"], body))
            h["o"] += 1
            h["n"] += 1
    changed = sum(1 for hk in hunks for r in hk["rows"] if r[0] != " ")
    if changed > MAX_CHANGED:
        raise NotPreviewable("%d changed lines: too many to approve from a dialog" % changed)
    for hk in hunks:
        for mk, _n, body in hk["rows"]:
            t = body.rstrip("\n")
            if mk != " " and len(t) > MAX_COLS:
                raise NotPreviewable("a changed line is too long to show whole")
            if mk == " ":
                continue
            if hidden_count(t, keep=""):
                # a tab is refused too: it counts as one column but renders as up to eight
                raise NotPreviewable("the change holds hidden characters or a tab")
            if rtl_count(t):
                raise NotPreviewable("the change holds right-to-left letters")
            if _FORGED_TAG.search(t):
                raise NotPreviewable("a changed line ends in text shaped like a dialog flag")
    removed = {body.rstrip("\n") for hk in hunks for mk, _n, body in hk["rows"]
               if mk == "-" and body.strip()}
    added = {body.rstrip("\n") for hk in hunks for mk, _n, body in hk["rows"]
             if mk == "+" and body.strip()}
    tags = {}
    for hi, hk in enumerate(hunks):
        rows, i = hk["rows"], 0
        while i < len(rows):
            if rows[i][0] != "-":
                i += 1
                continue
            j = i
            while j < len(rows) and rows[j][0] == "-":
                j += 1
            k = j
            while k < len(rows) and rows[k][0] == "+":
                k += 1
            for d in range(min(j - i, k - j)):
                old, nw = rows[i + d][2].rstrip("\n"), rows[j + d][2].rstrip("\n")
                tag = "same text" if old == nw else \
                    "ws only" if _ws_free(old) == _ws_free(nw) else None
                if tag:
                    tags[(hi, i + d)] = tags[(hi, j + d)] = tag
            i = k
    out, ctx, texts = [], [], []
    for hi, hk in enumerate(hunks):
        rows, first, last = hk["rows"], None, None
        for ri, (mk, n, body) in enumerate(rows):
            t = body.rstrip("\n")
            if mk == " ":
                continue
            if first is None:
                first = ri
            last = ri
            tag = tags.get((hi, ri))
            if tag is None and t.strip() and t in (added if mk == "-" else removed):
                tag = "moved"
            out.append(("%d%s%s" % (n, mk, t)) + (" [%s]" % tag if tag else ""))
            texts.append(t)
        pick = [rows[x] for x in ([first - 1] if first else []) + [last + 1]
                if 0 <= x < len(rows) and rows[x][0] == " " and rows[x][2].strip()]
        pick = [r for r in pick if not hidden_count(r[2].rstrip("\n"), keep="")
                and not rtl_count(r[2])]
        if pick:
            mk, n, body = pick[0]
            t = body.rstrip("\n")
            t = t if len(t) <= CTX_COLS else t[:CTX_COLS - 1] + "…"
            ctx.append((hi, "%d %s" % (n, t), t))
    # mandatory rows first; a context row joins its hunk only while the budget allows
    rows_out = list(out)
    pos = {}
    used = sum(len(r) + 1 for r in rows_out)
    for hi, text, t in ctx:
        if used + len(text) + 1 > BODY_BUDGET:
            continue
        used += len(text) + 1
        pos[hi] = (text, t)
    final, texts_all, ci = [], [], 0
    for hi, hk in enumerate(hunks):
        if hi in pos:
            final.append(pos[hi][0])
            texts_all.append(pos[hi][1])
        n = sum(1 for r in hk["rows"] if r[0] != " ")
        final += out[ci:ci + n]
        texts_all += texts[ci:ci + n]
        ci += n
    return {"changed": changed, "rows": final, "texts": texts_all}


def compose(requester, target, cid, version, new_sha, view):
    """-> the whole dialog text. Raises NotPreviewable when it cannot be shown whole or would pass
    the helper's cap."""
    if len(target) > MAX_TARGET:
        raise NotPreviewable("the target path is too long to show whole")
    req = " ".join(str(requester).split())
    # The requester (the asking process, with the script basename) is named by the kernel, but the
    # basename is any file name: it goes through the same hidden-character predicate as the change.
    if hidden_count(req, keep="") or hidden_count(target, keep=""):
        raise NotPreviewable("the requester or the target holds hidden characters")
    req = req if len(req) <= MAX_REQUESTER else req[:MAX_REQUESTER - 1] + "…"
    ver = "v%d" % version if version else "keep current"
    head = "ACCEPT held write? %s\n%s\nconflict %s %s, %d changed, sha %s\n" % (
        req, target, cid[-12:], ver, view["changed"], new_sha[:16])
    body = "".join(r + "\n" for r in view["rows"]) or "(no change to the file)\n"
    text = head + body + FOOT
    if rtl_count(text):
        raise NotPreviewable("the dialog would show right-to-left letters")
    if len(text) > HELPER_CAP:
        raise NotPreviewable("the dialog text is over what the platform helper takes")
    return text


def worst_case():
    """-> the longest prompt `compose` can be made to produce at the table's bounds (tests pin it
    to HELPER_CAP). Every changed line at MAX_COLS, a flag on each, 6-digit line numbers."""
    rows = ["%d-%s [same text]" % (999999, "x" * MAX_COLS) for _ in range(MAX_CHANGED)]
    view = {"changed": MAX_CHANGED, "rows": rows}
    return len(compose("r" * 200, "t" * MAX_TARGET, "2026-10-25-0123456789ab", 999, "0" * 64, view))
