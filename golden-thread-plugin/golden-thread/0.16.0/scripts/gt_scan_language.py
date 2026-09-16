#!/usr/bin/env python3
"""gt_scan_language.py -- scan code against the LANGUAGE definitions in effect here.

A LEAF command: it does one thing and is runnable and testable on its own. `gt_scan.py` is the
aggregator that sequences this and its siblings; it owns no checking logic of its own.

This is the CONSUMER of the slot registry. Everything it looks for comes from packs, not from
constants in this file: a contributed language pack changes what the scan does, which is the
whole point of the registry existing.

  gt_scan_language.py <path> [--vault V] [--only encoding,naming] [--json] [--all-files]

CHECKS, each named after the slot that defines it
  encoding   charset / line ending / BOM disagrees with the rule (from `encoding`)
  naming     a construct is not named the way the language says  (from `naming`)
and two slots shape the walk rather than producing findings:
  ignore     paths never opened                                  (from `ignore`)
  classify   generated/vendored/static files are skipped, so a scan is not drowned by
             findings in files nobody wrote                      (from `classify`)

NO CREDENTIAL SCANNING LIVES HERE. It did, and an independent validation on 2026-09-16 found
the credential check missed a .env file entirely, had no generic KEY=/SECRET= rule, and -- worst
-- leaked the very values it redacted, because the NAMING check prints raw source text and a
credential-shaped identifier went straight to stdout and --json on the same run. A secret
scanner that misses is worse than none, because people stop looking. Credential scanning is a
separate tool with different output rules, different failure modes and a different risk profile;
the `secrets` slot still exists for it. Conflating the two is what hid all three defects.

REPORTING RULE. This check prints source text (an identifier is the finding), so it must never
be pointed at content where the text itself is the sensitive thing. That is precisely why
secrets does not live here.

Exit: 0 clean | 1 findings | 2 usage | 3 the registry could not answer | 4 nothing to scan with
"""
import argparse
import fnmatch
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import gt_registry                                        # noqa: E402

# Only the slots that shape or drive the scan.
WALK_SLOTS = ("ignore", "classify")
# `lint` is NOT here. The slot's fields are lang/rule/severity -- a rule NAME and a severity,
# with nothing to match against -- so it cannot express what to detect. It was listed as a check
# and silently did nothing: entries resolved, were counted into "definitions in effect", and
# were never read. Giving it a `pattern` and a `message` would make it real; until then, saying
# it runs is the same manufactured assurance this codebase keeps having to dig out (2026-09-16).
CHECK_SLOTS = ("encoding", "naming")

# Files nobody hand-wrote produce findings nobody will act on. `classify` says which they are;
# these kinds are skipped unless --all-files is given.
SKIP_KINDS = ("generated", "vendored", "static")

MAX_BYTES = 2 << 20              # a source file, not a dataset

# NOTHING about a language lives in this file. Which extensions mean which language comes from
# the `filetype` slot; how to find a construct comes from `construct`. That is the difference
# between a registry and a lookup table -- an Elixir pack teaches gt Elixir with no edit here.
# Until 2026-09-16 both were hard-coded, so a contributed language resolved correctly in the
# registry and then silently never fired, and "adding a language is a pack, not a patch" was
# true only for the languages already patched in.

STYLE_OK = {
    "snake": re.compile(r"\A[a-z_][a-z0-9_]*\Z"),
    "camel": re.compile(r"\A[a-z][A-Za-z0-9]*\Z"),
    "pascal": re.compile(r"\A[A-Z][A-Za-z0-9]*\Z"),
    "kebab": re.compile(r"\A[a-z][a-z0-9-]*\Z"),
    "screaming_snake": re.compile(r"\A[A-Z][A-Z0-9_]*\Z"),
}


def _entries(slot, vault):
    """-> (list of entries, problems). Problems are never swallowed: the registry reporting a
    broken pack must reach the person running the scan, not be silently scanned around."""
    eff, _shadowed, _retracted, problems = gt_registry.resolve(slot, None, vault)
    return [r["entry"] for r in eff], problems


def classify(rel, rules):
    for e in rules:
        if fnmatch.fnmatch(rel, e["path"]) or fnmatch.fnmatch(os.path.basename(rel), e["path"]):
            return e["kind"]
    return None


def ignored(rel, rules):
    for e in rules:
        pat = e["path"]
        if pat.endswith("/"):
            if rel.startswith(pat) or ("/" + pat) in ("/" + rel):
                return True
            if any(part == pat.rstrip("/") for part in rel.split("/")):
                return True
        elif fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(os.path.basename(rel), pat):
            return True
    return False


def lang_of(rel, filetypes):
    """-> the language for this path, from the `filetype` slot, or None.

    A MORE SPECIFIC match beats a bare suffix, which is what lets a user say "the extensionless
    file bin/deploy is shell" or "here .inc means php". `filetype` is a map slot, so a local
    entry already outranks core's for the same key; this ordering makes sure a path rule is not
    lost to a suffix rule that happens to match the same file."""
    base = os.path.basename(rel)
    best, best_score = None, -1
    for match, lang in filetypes.items():
        if "/" in match:
            hit, score = fnmatch.fnmatch(rel, match), 2       # a path rule: most specific
        elif match.startswith("*."):
            hit, score = fnmatch.fnmatch(base, match), 0      # a bare suffix: least specific
        else:
            hit, score = fnmatch.fnmatch(base, match), 1      # a whole filename, e.g. Makefile
        if hit and score > best_score:
            best, best_score = lang, score
    return best


def scan_file(path, rel, raw, lang, checks):
    """-> list of findings for one file. `raw` is bytes; decoding is part of what is checked."""
    found = []

    enc = checks.get("encoding", {}).get(lang)
    if enc:
        if enc.get("bom") == "never" and raw.startswith(b"\xef\xbb\xbf"):
            found.append({"check": "encoding", "path": rel, "line": 1, "rule": "bom",
                          "lang": lang,
                          "message": "has a UTF-8 BOM but %s says bom=never" % lang})
        if enc.get("eol") == "lf" and b"\r\n" in raw:
            found.append({"check": "encoding", "path": rel, "line": 1, "rule": "eol",
                          "lang": lang,
                          "message": "contains CRLF but %s says eol=lf" % lang})
        if enc.get("charset") == "utf-8":
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                found.append({"check": "encoding", "path": rel, "line": 1, "rule": "charset",
                              "lang": lang,
                              "message": "is not valid UTF-8 (%s)" % exc.reason})

    naming = checks.get("naming", {}).get(lang)
    if naming:
        text = raw.decode("utf-8", "replace")
        for n, line in enumerate(text.split("\n"), 1):
            for construct, rx in checks.get("construct", {}).get(lang, []):
                want = naming.get(construct)
                if not want:
                    continue
                m = rx.match(line)
                if not m:
                    continue
                name = m.group(1)
                ok = STYLE_OK.get(want)
                if ok and not ok.match(name):
                    found.append({"check": "naming", "path": rel, "line": n,
                                  "rule": "%s/%s" % (lang, construct), "lang": lang,
                                  "message": "%s %r is not %s" % (construct, name, want)})
    return found


def walk(root, ignore_rules, classify_rules, all_files):
    """Yield (abspath, relpath, kind) for every file the rules say to look at."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in sorted(dirnames) if d != ".git"]
        rel_dir = os.path.relpath(dirpath, root)
        keep = []
        for d in dirnames:
            rel = os.path.normpath(os.path.join(rel_dir, d)) if rel_dir != "." else d
            if not ignored(rel.replace(os.sep, "/") + "/", ignore_rules):
                keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            rel = (os.path.normpath(os.path.join(rel_dir, name)) if rel_dir != "." else name)
            rel = rel.replace(os.sep, "/")
            if ignored(rel, ignore_rules):
                continue
            kind = classify(rel, classify_rules)
            if kind in SKIP_KINDS and not all_files:
                continue
            yield os.path.join(dirpath, name), rel, kind


RETRACT_FIELD = {"naming": "lang", "encoding": "lang"}


def _print_suppression_hint(findings, vault):
    """Tell the person how to switch a noisy rule off, at the moment it annoys them.

    Deliberately PRINTS and does not write: a tool that silently edits the pack directory can
    disable its own checks, and that directory is guarded to `ask` so a person sees such a
    change happen."""
    noisy = {}
    for f in findings:
        # The finding's own `lang`, never the rule name: `rule` for an encoding finding is
        # "bom"/"eol"/"charset", and printing `{"lang": "bom"}` told people to write a retract
        # that matches nothing at all (2026-09-16).
        lang = f.get("lang")
        if lang:
            key = (f["check"], lang)
            noisy[key] = noisy.get(key, 0) + 1
    if not noisy:
        return
    where = (os.path.join(vault, "Projects", "golden-thread", "packs")
             if vault else "<vault>/Projects/golden-thread/packs")
    print("\nA rule firing on things that are not findings? Switch it off in YOUR vault:")
    print("  %s/<slot>.local.pack.json" % where)
    for (check, lang), n in sorted(noisy.items(), key=lambda kv: -kv[1])[:3]:
        print('    "retract": [{"lang": "%s"}]        %s %d hit(s)' % (lang, check, n))
    print("  Only packs in your vault may retract, so this never affects anyone else, and")
    print("  `gt_registry.py show <slot>` reports the rule as RETRACTED, not hidden.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path", help="the tree to scan")
    ap.add_argument("--vault", help="the vault whose local packs apply")
    ap.add_argument("--only", help="comma-separated subset of: %s" % ",".join(CHECK_SLOTS))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--all-files", action="store_true",
                    help="also scan generated, vendored and static files")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.path)
    if not os.path.isdir(root):
        print("not a directory: %s" % root, file=sys.stderr)
        return 2

    wanted = CHECK_SLOTS
    if args.only:
        wanted = tuple(s.strip() for s in args.only.split(",") if s.strip())
        unknown = [s for s in wanted if s not in CHECK_SLOTS]
        if unknown:
            print("unknown check(s): %s" % ", ".join(unknown), file=sys.stderr)
            return 2

    problems = []
    ignore_rules, p = _entries("ignore", args.vault); problems += p
    classify_rules, p = _entries("classify", args.vault); problems += p

    checks, loaded = {}, {}
    # The language definitions themselves, which every per-language check depends on.
    filetype_entries, p = _entries("filetype", args.vault); problems += p
    filetypes = {e["match"]: e["lang"] for e in filetype_entries}
    construct_entries, p = _entries("construct", args.vault); problems += p
    constructs = {}
    for e in construct_entries:
        try:
            constructs.setdefault(e["lang"], []).append((e["construct"], re.compile(e["pattern"])))
        except re.error as exc:
            problems.append(("construct %s/%s" % (e.get("lang"), e.get("construct")),
                             "will not compile: %s" % exc))
    checks["construct"] = constructs

    if "encoding" in wanted:
        entries, p = _entries("encoding", args.vault); problems += p
        checks["encoding"] = {e["lang"]: e for e in entries}
        loaded["encoding"] = len(entries)
    if "naming" in wanted:
        entries, p = _entries("naming", args.vault); problems += p
        by_lang = {}
        for e in entries:
            by_lang.setdefault(e["lang"], {})[e["construct"]] = e["style"]
        checks["naming"] = by_lang
        loaded["naming"] = len(entries)
    # An empty set of definitions is not a clean scan. Refusing here is the difference between
    # "nothing matched" and "nothing was looked for", which otherwise print identically.
    if not any(loaded.values()):
        for where, err in problems:
            print("PROBLEM %s: %s" % (where, err), file=sys.stderr)
        print("REFUSING TO SCAN: not one definition resolved for %s.\n"
              "  A scan with no patterns finds nothing and looks exactly like a clean tree.\n"
              "  Check `gt_registry.py sources` -- packs may be missing or failing to verify."
              % ", ".join(wanted), file=sys.stderr)
        return 4

    findings, unreadable = [], []
    for abspath, rel, _kind in walk(root, ignore_rules, classify_rules, args.all_files):
        try:
            if os.path.getsize(abspath) > MAX_BYTES:
                continue
            with open(abspath, "rb") as fh:
                raw = fh.read()
        except OSError as exc:
            unreadable.append((rel, str(exc)))
            continue
        if b"\x00" in raw[:4096]:                          # binary: nothing to read
            continue
        findings += scan_file(abspath, rel, raw, lang_of(rel, filetypes), checks)

    if args.json:
        print(json.dumps({"version": 1, "root": root, "loaded": loaded,
                          "findings": findings,
                          "problems": [{"where": w, "error": e} for w, e in problems],
                          "unreadable": [{"path": p, "error": e} for p, e in unreadable]},
                         indent=2))
    else:
        print("scanned %s" % root)
        print("definitions in effect: %s"
              % ", ".join("%s=%d" % (k, v) for k, v in sorted(loaded.items())))
        for f in findings:
            extra = ("  [%s]" % f["shape"]) if f.get("shape") else ""
            print("  %-8s %s:%d  %s%s" % (f["check"], f["path"], f["line"], f["message"], extra))
        for where, err in problems:
            print("  PROBLEM %s: %s" % (where, err))
        for p, err in unreadable:
            print("  UNREADABLE %s: %s" % (p, err))
        if not findings:
            print("  no findings")
        else:
            _print_suppression_hint(findings, args.vault)
    if problems and not findings:
        return 3
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
