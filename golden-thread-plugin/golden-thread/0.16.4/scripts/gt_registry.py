#!/usr/bin/env python3
"""gt_registry.py -- what definition is actually in effect here, and where did it come from?

Golden Thread keeps pluggable definitions in SLOTS: how a language names things, which paths
are noise, what a credential looks like, what a term means. Each slot is filled by packs --
JSON data files, never programs -- and this resolves them into one answer per key, naming the
source.

  gt_registry.py [--vault V] show <slot> [--lang X] [--json]
  gt_registry.py [--vault V] sources        # every pack found, in precedence order
  gt_registry.py slots                      # the slot table

PRECEDENCE:  community  <  core  <  local

  community  merged contributions, shipped in packs/community/
  core       maintainer-authored defaults, shipped in packs/core/
  local      the user's own packs under <vault>/Projects/golden-thread/packs/

Support tier is the DIRECTORY a pack was merged into, never a field a submission declares, so
a contributor cannot forge it.

MERGE MODES, and why union works the way it does
  union  entries ACCUMULATE and only byte-identical entries collapse (secrets, ignore,
         runbook). Dedup is on the WHOLE entry, not on the key fields, so a later pack can
         never replace an earlier definition -- it can only add. This is the additive-only
         rule where it matters most: `secrets` defines what a credential looks like, and a
         contributed pack must not be able to retire a core pattern by reusing its id.
         (Security review 2026-09-16 found the opposite: keying union on `id` alone let a
         community pack silently replace core's AWS pattern with one that matches nothing,
         and beat the user's local override too. Nothing was reported and the exit was 0.)
  map    ONE value per key (naming, encoding, classify, lint, vocabulary,
         validation_rules). Highest precedence wins; every loser is reported as SHADOWED
         with the winner named, so a definition never disappears without a word.

FAILURE IS LOUD. A pack that cannot be read, is the wrong shape, or carries an entry missing
its key fields is reported as a problem and changes the exit code. A definition that vanished
silently is worse than one that failed noisily. An EMPTY answer is never exit 0: a caller must
not be able to read "no credential patterns found" as "no credential patterns exist".

WHAT PINS A PACK DOWN. Tier is the directory, so the directory has to be the real one: each is
checked to resolve where it is spelled, because a symlinked packs/core would otherwise serve
anything at core tier. For the two release directories the MANIFEST hash is the trust anchor
and every check fails closed -- a missing manifest, an unparseable one, or a pack with no
manifest row all refuse to load rather than serve unverified content. Local packs are the
user's own, are not in the manifest, and are gated instead by entry_problem at load, which
checks field NAMES as well as values.

Exit: 0 clean | 1 resolved, but problems were reported | 2 usage | 3 nothing could be resolved.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# key: identifies an entry in a `map` slot. Unused for `union`, which compares whole entries.
SLOTS = {
    "secrets":          {"mode": "union", "key": ("id",)},
    "ignore":           {"mode": "union", "key": ("path",)},
    "runbook":          {"mode": "union", "key": ("id", "step")},
    "classify":         {"mode": "map",   "key": ("path",)},
    "encoding":         {"mode": "map",   "key": ("lang",)},
    "naming":           {"mode": "map",   "key": ("lang", "construct")},
    "lint":             {"mode": "map",   "key": ("lang", "rule")},
    "vocabulary":       {"mode": "map",   "key": ("term",)},
    "validation_rules": {"mode": "map",   "key": ("id",)},
    # What makes a LANGUAGE PACK self-contained. Without these two, a contributed language
    # resolved fine and then never fired, because the file-extension table and the
    # construct-finding patterns were hard-coded in gt_scan.py -- "adding a language is a pack,
    # not a patch" was only true for languages already patched in (found 2026-09-16).
    # Both are `map`, so a local entry OVERRIDES rather than adds: that is what lets a user say
    # "this extensionless file is shell" or "here .inc means php" and be believed.
    "filetype":         {"mode": "map",   "key": ("match",)},
    "construct":        {"mode": "map",   "key": ("lang", "construct")},
}
# Which shipped tool READS each slot. A slot with no consumer resolves perfectly and is then
# read by nobody: a user can write a pack, watch `show` list it as in effect, and get silence
# forever. That is the same inert-definition failure that let the pack feature ship dead, so the
# slot table states it outright rather than letting someone discover it (2026-09-16).
# test_gt_registry asserts every name here really does reference its slot.
CONSUMERS = {
    "ignore": "gt_scan_language.py",
    "classify": "gt_scan_language.py",
    "filetype": "gt_scan_language.py",
    "construct": "gt_scan_language.py",
    "naming": "gt_scan_language.py",
    "encoding": "gt_scan_language.py",
    # The Tier D slots -- the ones that reach a session rather than an offline scanner.
    # These read None until 0.16.2, and were WRONG from the moment 0.16.1 shipped: gt_context.py
    # has read all three since then, and its MODEL_REACHABLE is exactly this list.
    #
    # The test guarding this map only checked the POSITIVE direction -- that a slot claiming a
    # consumer really is read by it -- and skipped every slot claiming None. So a slot that
    # UNDERSTATED its coverage sailed past. Understating is the safer direction to be wrong in,
    # but it is still a map that did not describe the code, which is the only thing this map is
    # for. The test now checks both directions.
    "vocabulary": "gt_context.py",
    "validation_rules": "gt_context.py",
    "runbook": "gt_context.py",
    # No consumer yet. Listed so the gap is visible, not so it looks supported:
    "secrets": None,            # awaits gt_scan_secrets
    "lint": None,               # the slot cannot express WHAT to detect; needs a pattern field
}

TIERS = ("community", "core", "local")      # low to high precedence
MAX_PACK_BYTES = 1 << 20                    # 1 MiB: a data pack, not a dataset
MAX_JSON_DEPTH = 8


def find_vault(explicit=None):
    if explicit:
        return explicit
    env = os.environ.get("GT_VAULT")
    if env:
        return env
    try:
        with open(os.path.expanduser("~/.claude/vault-config.json"), encoding="utf-8") as fh:
            return json.load(fh).get("vault_path")
    except (OSError, ValueError):
        return None


def _contained(path, root):
    """True when path really resolves inside root -- symlinks included.

    This is only sound when `root` ITSELF is known not to be a symlink, because it realpaths
    both sides: realpath(root) follows root's own links, so a symlinked root always "contains"
    whatever it points at. Adversarial review 2026-09-16 broke the earlier, stronger claim
    made here by replacing packs/core with a symlink -- see _resolves_as_spelled, which is
    what actually pins a pack directory down."""
    rp, rr = os.path.realpath(path), os.path.realpath(root)
    return rp == rr or rp.startswith(rr.rstrip(os.sep) + os.sep)


def _resolves_as_spelled(root, *parts):
    """-> the directory root/*parts, but only if it still resolves THERE after every symlink
    on it is followed; None otherwise.

    `_contained(d, d)` can never fail, so containment alone cannot tell a real packs/core from
    a symlink to /tmp/evil. Comparing against the path rebuilt from realpath(root) can: the
    symlink resolves elsewhere and the equality fails. Without this, a symlinked pack
    directory serves arbitrary content at `core` tier AND lands outside the release, which
    skipped the MANIFEST check as a side effect (security review 2026-09-16)."""
    d = os.path.join(root, *parts)
    return d if os.path.realpath(d) == os.path.join(os.path.realpath(root), *parts) else None


def pack_dirs(vault=None):
    """-> [(tier, directory, fault)] lowest precedence first, fault in (None, "missing",
    "escaped"). Both faults are reported rather than skipped, so a directory that was renamed
    or swapped is never silent."""
    release = os.path.dirname(HERE)
    want = [("community", release, ("packs", "community")),
            ("core", release, ("packs", "core"))]
    v = find_vault(vault)
    if v:
        want.append(("local", v, ("Projects", "golden-thread", "packs")))
    out = []
    for tier, root, parts in want:
        literal = os.path.join(root, *parts)
        if not os.path.isdir(literal):
            # The two release dirs always ship, so an absent one is a fault worth reporting:
            # renaming packs/core was how an attacker emptied the `secrets` slot in silence.
            # An absent LOCAL dir is the normal case for a user with no packs of their own.
            if tier != "local":
                out.append((tier, literal, "missing"))
            continue
        # Spelling is enforced for the RELEASE dirs only. There, a symlinked packs/core serves
        # arbitrary content at core tier and lands outside the release, skipping the manifest.
        # `local` is the user's OWN top-tier content, is never manifest-verified, and is guarded
        # by guard_protected_paths -- so the same rule there only breaks honest setups: a vault
        # whose Projects/ is a symlink had every local pack silently dropped (review 2026-09-16).
        escaped = tier != "local" and _resolves_as_spelled(root, *parts) is None
        out.append((tier, literal, "escaped" if escaped else None))
    return out


def _depth(obj, level=0):
    if level > MAX_JSON_DEPTH:
        return level
    if isinstance(obj, dict):
        return max([level] + [_depth(v, level + 1) for v in obj.values()])
    if isinstance(obj, list):
        return max([level] + [_depth(v, level + 1) for v in obj])
    return level


def _manifest_hashes(release):
    """-> {relative path: sha256 hex} from MANIFEST.json, or None if it cannot be read.

    build_manifest() writes each row as {"bytes": N, "sha256": "..."}, NOT as a bare hex
    string. Comparing the digest to the row itself therefore never matched, and every shipped
    pack was refused as "edited after install" on every installation -- the pack feature was
    inert in the release (found 2026-09-16 when an untouched fixture failed its own baseline;
    the unit tests missed it because their fixture manifests used flat strings). Normalise the
    row shape here, once, so there is one place that knows what a manifest row looks like."""
    try:
        with open(os.path.join(release, "MANIFEST.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    files = data.get("files", data)
    if not isinstance(files, dict):
        return None
    out = {}
    for rel, row in files.items():
        if isinstance(row, str):
            out[rel] = row
        elif isinstance(row, dict) and isinstance(row.get("sha256"), str):
            out[rel] = row["sha256"]
    return out


def load_packs(slot=None, vault=None):
    """Every pack, lowest precedence first, plus a list of problems.

    Every failure mode lands in `problems`; none is swallowed. One malformed pack must not be
    able to take the whole registry down -- a caller that gets an empty answer for `secrets`
    may well treat it as "no patterns to look for"."""
    packs, problems = [], []
    release = os.path.dirname(HERE)
    manifest = _manifest_hashes(release)
    for tier, d, fault in pack_dirs(vault):
        if fault == "missing":
            problems.append((d, "this pack directory ships with every release but is not "
                                "here; the definitions it holds are NOT in effect"))
            continue
        if fault == "escaped":
            # _clean the target: it is a path an attacker named, and it is interpolated into a
            # message that is printed when something is ALREADY wrong -- an erase-line here
            # lands on the security warning itself (review 2026-09-16).
            problems.append((d, "this directory resolves elsewhere (%s); refusing to load it"
                                % _clean(os.path.realpath(d), 200)))
            continue
        try:
            names = sorted(os.listdir(d))
        except OSError as exc:
            problems.append((d, "cannot list: %s" % exc))
            continue
        for name in names:
            if not name.endswith(".pack.json"):
                continue
            # The filename reaches the terminal in every PROBLEM line. A name carrying an ANSI
            # erase-line can forge a row and blank the real message (security review
            # 2026-09-16); such a name is never legitimate, so refuse it outright.
            if any(unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs", "Cn") for ch in name):
                problems.append((os.path.join(d, _clean(name, 80)),
                                 "filename contains control characters; refusing to load"))
                continue
            path = os.path.join(d, name)
            if not _contained(path, d):
                problems.append((path, "resolves outside %s; refusing to load" % d))
                continue
            try:
                size = os.path.getsize(path)
                if size > MAX_PACK_BYTES:
                    problems.append((path, "%d bytes exceeds the %d-byte cap"
                                     % (size, MAX_PACK_BYTES)))
                    continue
                with open(path, encoding="utf-8") as fh:
                    raw = fh.read()
                data = json.loads(raw)
            except RecursionError:
                problems.append((path, "JSON nested too deeply"))
                continue
            except Exception as exc:                 # noqa: BLE001 - one pack must not kill all
                problems.append((path, "%s: %s" % (type(exc).__name__, exc)))
                continue
            if not isinstance(data, dict):
                problems.append((path, "not a JSON object"))
                continue
            if _depth(data) > MAX_JSON_DEPTH:
                problems.append((path, "JSON nested too deeply"))
                continue
            if data.get("slot") not in SLOTS:
                problems.append((path, "missing or unknown slot %r" % (data.get("slot"),)))
                continue
            entries = data.get("entries")
            if not isinstance(entries, list):
                problems.append((path, "entries must be a list, got %s"
                                 % type(entries).__name__))
                continue
            # Release-dir packs are maintainer-reviewed and hashed at build time, so the
            # manifest is the trust anchor for them and every one of these checks FAILS CLOSED.
            # All three of the open seams here were exploited in the 2026-09-16 review: a
            # deleted manifest, a malformed manifest, and a pack ADDED to packs/core with no
            # manifest row all served attacker content at core tier with exit 0. Local packs
            # are the user's own and are never in the manifest; entry_problem is their gate.
            if tier in ("community", "core"):
                if manifest is None:
                    problems.append((path, "MANIFEST.json is missing or unreadable, so no "
                                           "shipped pack can be verified; refusing to load"))
                    continue
                rel = os.path.relpath(path, release)
                want = manifest.get(rel)
                if not want:
                    problems.append((path, "is not listed in MANIFEST.json; a pack added to "
                                           "the release after install; refusing to load"))
                    continue
                got = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                if got != want:
                    problems.append((path, "does not match MANIFEST.json; edited after "
                                           "install or corrupted"))
                    continue
            if slot and data["slot"] != slot:
                continue
            # `retract` is how a user turns OFF a definition in a union slot, where merging is
            # additive and nothing can be replaced. It is honoured for the LOCAL tier only: the
            # rule that a contributed pack must never retire a core credential pattern is the
            # one this design exists to protect, and the user is a different principal from a
            # contributor. A submitted pack cannot even declare it -- `retract` is not in
            # MANIFEST_KEYS, so the submission validator refuses it as an unknown key.
            retract = data.get("retract")
            if retract is not None and tier != "local":
                problems.append((path, "only a pack in your own vault may retract a definition; "
                                       "this one is %s tier and its retract list is ignored"
                                 % tier))
                retract = None
            if retract is not None and not isinstance(retract, list):
                problems.append((path, "retract must be a list of {field: value} objects"))
                retract = None
            packs.append({"tier": tier, "path": path, "name": _clean(data.get("name") or name),
                          "slot": data["slot"], "entries": entries, "retract": retract or []})
    return packs, problems


# Local packs live in the vault and never pass through the submission validator or the
# release gate -- nothing else checks them. An unchecked local pack in a model-reachable slot
# (runbook, vocabulary, validation_rules) is a durable prompt-injection foothold, so the
# entry-level safety rules are applied here, at load, to every pack regardless of tier.
_BAD_CATEGORIES = ("Cf", "Co", "Cs", "Cn", "Zl", "Zp")
_IMPERATIVE = re.compile(
    r"\b(you|your|please|note to|assistant|ignore (all|any|previous)|instead of|make sure|"
    r"be sure to|remember to|never forget|do not|don't|must (also|now)|when you see)\b", re.I)
_MAX_VALUE = 300


_MAX_FIELD_NAME = 64


def _string_problem(label, s, cap):
    """-> a reason string when this string must not be served, else None."""
    if len(s) > cap:
        return "%s is %d chars (cap %d)" % (label, len(s), cap)
    for ch in s:
        cat = unicodedata.category(ch)
        if cat in _BAD_CATEGORIES or cat == "Cc":
            return "%s contains U+%04X (category %s)" % (label, ord(ch), cat)
    if _IMPERATIVE.search(s):
        return ("%s reads as an instruction rather than a definition; a pack must not carry "
                "text that steers a reader" % label)
    return None


def entry_problem(entry):
    """-> a reason string when this entry must not be served, else None.

    Field NAMES are checked exactly like field values. Checking only values left the obvious
    hiding place open: the 2026-09-16 review put 147 characters of instruction text, an RLO
    override and a BEL into a JSON KEY and it was served verbatim, problems empty, exit 0.
    Anything a reader sees is content, and a key is something a reader sees."""
    for i, (k, v) in enumerate(entry.items()):
        if not isinstance(k, str):                       # json gives str keys; be explicit
            return "field name #%d is %s, not a string" % (i + 1, type(k).__name__)
        # The offending name is identified by POSITION, not quoted back. Quoting it echoed the
        # injection into the PROBLEM line -- which is read by a terminal and, more to the
        # point, by a model. A refusal must not become the delivery mechanism.
        bad = _string_problem("field name #%d" % (i + 1), k, _MAX_FIELD_NAME)
        if bad:
            return bad
        if not isinstance(v, str):
            return "field %r is %s, not a string" % (k, type(v).__name__)
        # Safe to name k here: it passed the checks immediately above.
        bad = _string_problem("field %r" % _clean(k, 40), v, _MAX_VALUE)
        if bad:
            return bad
    return None


def _clean(value, limit=48):
    """Render an untrusted string safely: no control characters, no ANSI, bounded length.
    Pack `name` and entry values are author-controlled and are printed to a terminal."""
    if not isinstance(value, str):
        return repr(value)
    out = "".join(ch if (ch.isprintable() and unicodedata.category(ch) not in ("Cf", "Co", "Cn"))
                  else "\\u%04x" % ord(ch) for ch in value)
    return out if len(out) <= limit else out[:limit - 1] + "…"


def _key(entry, fields):
    return tuple(entry.get(f, "") for f in fields)


def _retract_matches(entry, _spec_fields, pattern):
    """A retract names fields of what it removes: {"id": "twilio-account-sid"} for one entry,
    or {"lang": "go"} to switch a whole language off in one line -- which is how a user chooses
    which language packs are live, without a separate install mechanism.

    ANY field of the entry may be named, not just the key fields, but at least one must be
    present in the entry: `{}` and `{"nonsense": 1}` match nothing, so a retract can never
    silently sweep a slot."""
    if not isinstance(pattern, dict) or not pattern:
        return False
    named = [k for k in pattern if k in entry]
    if not named:
        return False
    return all(entry.get(k) == pattern[k] for k in named)


def resolve(slot, lang=None, vault=None):
    """-> (effective, shadowed, retracted, problems)."""
    spec = SLOTS[slot]
    packs, problems = load_packs(slot, vault)
    effective, shadowed = {}, []
    for pack in packs:                                   # lowest precedence first
        for i, entry in enumerate(pack["entries"]):
            where = "%s[%d]" % (pack["path"], i)
            if not isinstance(entry, dict):
                problems.append((where, "entry is %s, not an object" % type(entry).__name__))
                continue
            bad = entry_problem(entry)
            if bad:
                problems.append((where, bad))
                continue
            missing = [f for f in spec["key"] if not entry.get(f)]
            if missing:
                # Without this an entry missing `lang` keys as ("", ...), never collides with a
                # real entry, is never shadowed, and answers every --lang query.
                problems.append((where, "missing key field(s): %s" % ", ".join(missing)))
                continue
            if lang and "lang" in spec["key"] and entry.get("lang") != lang:
                continue
            rec = {"entry": entry, "source": pack["name"], "tier": pack["tier"],
                   "path": pack["path"]}
            if spec["mode"] == "union":
                # Whole entry as the key: additive only, a later pack can never replace one.
                # Overwrite rather than keep-first so the CREDIT goes to the highest tier that
                # ships an identical entry -- keeping the first meant a community pack copying
                # core's AWS pattern byte for byte left `community` as the only attribution on
                # the row, so "which patterns are maintainer-backed" answered wrongly
                # (security review 2026-09-16). The entry itself is unchanged either way.
                k = json.dumps(entry, sort_keys=True)
                effective[k] = rec
            else:
                k = _key(entry, spec["key"])
                prev = effective.get(k)
                if prev is not None and prev["entry"] != entry:
                    shadowed.append(dict(prev, lost_to="%s (%s)"
                                         % (rec["source"], rec["tier"])))
                effective[k] = rec

    # Retracts apply last, so a user can switch off a definition no matter which tier shipped
    # it. Every removal is REPORTED -- a definition that disappeared without a word is the
    # failure this whole module is built to avoid, and that does not stop being true because
    # the user asked for it.
    retracted = []
    for pack in packs:
        for pattern in pack["retract"]:
            hit = False
            for k, rec in list(effective.items()):
                if _retract_matches(rec["entry"], spec["key"], pattern):
                    retracted.append(dict(rec, retracted_by=pack["name"]))
                    del effective[k]
                    hit = True
            if not hit:
                # A retract that matches nothing is usually a definition that was renamed
                # upstream: the user believes something is off and it is quietly back on.
                problems.append((pack["path"], "retract %s matches no definition in this slot; "
                                               "it may be left over from a renamed entry"
                                 % json.dumps(pattern, sort_keys=True)))
    return list(effective.values()), shadowed, retracted, problems


def _fmt(entry):
    return " ".join("%s=%s" % (_clean(k, 24), _clean(v, 60)) for k, v in sorted(entry.items()))


def cmd_show(args):
    if args.slot not in SLOTS:
        print("unknown slot %r; try `gt_registry.py slots`" % args.slot, file=sys.stderr)
        return 2
    eff, shadowed, retracted, problems = resolve(args.slot, args.lang, args.vault)
    if args.json:
        print(json.dumps({"slot": args.slot, "effective": eff, "shadowed": shadowed,
                          "retracted": retracted,
                          "problems": [{"path": p, "error": e} for p, e in problems]}, indent=2))
    else:
        print("slot %s  (%s)  %d in effect" % (args.slot, SLOTS[args.slot]["mode"], len(eff)))
        for rec in eff:
            print("  %-9s %-22s %s" % (rec["tier"], rec["source"], _fmt(rec["entry"])))
        for rec in shadowed:
            print("  SHADOWED  %-22s %s  <- lost to %s"
                  % (rec["source"], _fmt(rec["entry"]), _clean(rec["lost_to"])))
        for rec in retracted:
            print("  RETRACTED %-22s %s  <- switched off by %s (your vault)"
                  % (rec["source"], _fmt(rec["entry"]), _clean(rec["retracted_by"])))
        for path, err in problems:
            # The path is attacker-controlled too -- it carries the pack's FILENAME.
            # BOTH halves are cleaned: the path carries the filename and the err
            # can interpolate an attacker-named symlink target.
            print("  PROBLEM %s: %s" % (_clean(path, 200), _clean(err, 300)))
        if not eff and not problems:
            print("  (nothing defined)")
        if eff and not CONSUMERS.get(args.slot):
            # Say it HERE too, not only in `slots`: this is the moment someone is looking at
            # their own pack resolving and concluding, reasonably, that it is doing something.
            print("\n  NOTE: no shipped tool reads the '%s' slot yet, so these definitions "
                  "resolve\n        correctly and then change nothing." % args.slot)
    if not eff:
        # Nothing in effect is never "clean". A caller reading exit 0 as "complete" would take
        # an empty `secrets` slot for "no credential patterns to look for", which is exactly
        # the answer an attacker who removed a pack directory wants it to have.
        return 3
    return 1 if problems else 0


def cmd_sources(args):
    packs, problems = load_packs(None, args.vault)
    if packs:
        print("%-9s %-24s %-18s %s" % ("TIER", "PACK", "SLOT", "SERVABLE/ENTRIES"))
        for p in packs:
            # ENTRIES alone made `sources` useless as a health check: it counted entries that
            # `show` then refused, so a pack reading 2 served 1 (security review 2026-09-16).
            # Count what would actually be served, and report the rest here too.
            good = 0
            for i, entry in enumerate(p["entries"]):
                if not isinstance(entry, dict):
                    problems.append(("%s[%d]" % (p["path"], i),
                                     "entry is %s, not an object" % type(entry).__name__))
                    continue
                bad = entry_problem(entry)
                if bad:
                    problems.append(("%s[%d]" % (p["path"], i), bad))
                else:
                    good += 1
            print("%-9s %-24s %-18s %d/%d"
                  % (p["tier"], p["name"], p["slot"], good, len(p["entries"])))
    elif not problems:
        print("no packs found")
    for path, err in problems:
        print("PROBLEM %s: %s" % (_clean(path, 200), _clean(err, 300)))
    if not packs:
        return 3
    return 1 if problems else 0


def cmd_slots(_args):
    print("%-18s %-6s %-22s %s" % ("SLOT", "MODE", "KEY", "READ BY"))
    unread = []
    for name in sorted(SLOTS):
        s = SLOTS[name]
        who = CONSUMERS.get(name)
        if not who:
            unread.append(name)
        print("%-18s %-6s %-22s %s"
              % (name, s["mode"], ", ".join(s["key"]), who or "-- nothing yet --"))
    if unread:
        print("\nNOTHING READS THESE YET: %s" % ", ".join(unread))
        print("A pack in one of them resolves correctly, shows as `in effect`, and is then")
        print("read by no tool at all. The definitions are kept because the schema is settled")
        print("and the tools are coming -- but writing one today changes nothing.")
    print("\nprecedence: community < core < local   (the user's own packs always win)")
    print("union slots are ADDITIVE: a later pack can add a definition, never replace one.")
    print("map slots keep one value per key; every loser is reported as SHADOWED.")
    print("\nTo switch a definition off, add a `retract` list to a pack in YOUR vault:")
    print('  {"slot": "secrets", ..., "retract": [{"id": "twilio-account-sid"}]}')
    print("Only packs under <vault>/Projects/golden-thread/packs/ may retract, so a")
    print("contributed pack can never retire a core definition. Every retraction is")
    print("reported as RETRACTED, the same as a shadowed one.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--vault", help="the vault to read local packs from")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show", help="what is in effect for a slot")
    s.add_argument("slot")
    s.add_argument("--lang")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_show)
    sub.add_parser("sources", help="every pack found, in precedence order").set_defaults(
        func=cmd_sources)
    sub.add_parser("slots", help="the slot table").set_defaults(func=cmd_slots)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
