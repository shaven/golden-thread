#!/usr/bin/env python3
"""gt_baseline.py -- content-keyed baselines for gt's scanners (gt_secrets, gt_scan_code; 0.20.1).

A baseline is the list of findings the owner looked at and accepted. Until 0.20.1 both scanners
keyed an entry by its PATH (gt_secrets: path, rule, length; gt_scan_code: path, rule, line), so
every version cut -- golden-thread/0.20.1/... copied to golden-thread/0.20.1/... -- re-flagged
every accepted line under the new release directory, and the owner re-reviewed the same lines
each release.

An entry now matches by three things, none of them the matched text itself:

    rule        the rule that fired
    path        the file, with every release-version segment (1.2.3) written as <ver>, so
                golden-thread/0.20.1/tests/x.py and golden-thread/0.20.1/tests/x.py are one path
    hash        a hash of the NORMALISED line (surrounding whitespace stripped, runs of
                whitespace collapsed)

So a byte-identical line moved by a version cut stays accepted, and a new or edited line, a
different rule, or the same text in a different file still fails. Each entry also carries a
COUNT: two identical accepted lines in one file accept two findings, not every copy ever added.

NO VALUES AT REST. The secrets baseline must never hold a credential, so the hash is not a plain
digest: gt_secrets uses `slow_hash` -- PBKDF2-HMAC-SHA256, 200,000 rounds, salted with the rule
and the normalised path -- because a fast hash of a short line is a lookup away from the line.
The lines gt_secrets flags are long, high-entropy values by its own rules, so a guess has to
get the whole line right, at 200k hash rounds per guess. gt_scan_code's findings are code, not
secrets, and use a plain SHA-256 (`fast_hash`).

OLD BASELINES ARE STILL READ. A file whose `accepted` holds the 0.19-era tuples keeps matching
exactly as before; `--write-baseline` writes the new `entries` and no longer writes tuples.
"""
import hashlib
import re
from collections import Counter

FORMAT = 2
_VER = re.compile(r"^\d+\.\d+\.\d+$")
_WS = re.compile(r"\s+")
ROUNDS = 200_000


def norm_path(rel):
    """A repo-relative path with every release-version segment written as <ver>."""
    parts = rel.replace("\\", "/").split("/")
    return "/".join("<ver>" if _VER.match(p) else p for p in parts)


def norm_line(text):
    return _WS.sub(" ", (text or "").strip())


def fast_hash(rule, rel, text):
    h = hashlib.sha256()
    h.update(("%s\0%s\0" % (rule, norm_path(rel))).encode("utf-8"))
    h.update(norm_line(text).encode("utf-8"))
    return "sha256:" + h.hexdigest()


def slow_hash(rule, rel, text):
    salt = ("gt-baseline-v2\0%s\0%s" % (rule, norm_path(rel))).encode("utf-8")
    d = hashlib.pbkdf2_hmac("sha256", norm_line(text).encode("utf-8"), salt, ROUNDS)
    return "pbkdf2-sha256-%d:%s" % (ROUNDS, d.hex())


def entry_key(e):
    return (e.get("rule"), e.get("path"), e.get("hash"))


class Baseline:
    """Old tuples (`accepted`) and new entries (`entries`), matched with counts."""

    def __init__(self, doc):
        doc = doc or {}
        self.legacy = {tuple(x) for x in doc.get("accepted") or [] if isinstance(x, list)}
        self.counts = Counter()
        for e in doc.get("entries") or []:
            if isinstance(e, dict) and e.get("rule") and e.get("path") and e.get("hash"):
                self.counts[entry_key(e)] += int(e.get("count") or 1)

    def accepts(self, legacy_key, rule, rel, line_hash):
        """True when this finding is accepted; consumes one count of a content entry."""
        k = (rule, norm_path(rel), line_hash)
        if self.counts.get(k, 0) > 0:
            self.counts[k] -= 1
            return True
        return tuple(legacy_key) in self.legacy


def entries_for(findings):
    """[{rule, path, hash, count}] for findings carrying rule, path and `_hash`."""
    c = Counter((f["rule"], norm_path(f["path"]), f["_hash"]) for f in findings)
    return [{"rule": r, "path": p, "hash": h, "count": n}
            for (r, p, h), n in sorted(c.items())]
