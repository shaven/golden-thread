#!/usr/bin/env python3
"""Golden Thread watch: follow any git repo, and raise a P0 when it ships a security fix.

## Shape

A watch is a NOTE in the vault, `Projects/golden-thread/watches/<slug>.md`, whose
frontmatter says what to watch. Everything the machine learns lives OUTSIDE the vault,
in the state dir (`$GT_WATCH_STATE`, else `~/.claude/golden-thread/watch/`), and each
file there has exactly one writer:

  state.json    last seen per repo        written only by `fetch`
  events.jsonl  the queue                 appended only by `fetch`
  acked.json    the read cursor           written only by `ack`
  cache/        shallow bare clones       `fetch`
  fetch.log     what each run did         `fetch`

`fetch` runs from cron, without Claude. It asks each remote what changed (`git
ls-remote`, then a shallow fetch into the cache for commit messages, paths and tag
annotations; `gh api` for releases and advisories only when gh is installed and
authenticated), classifies every change by fixed rules, and appends one event per
change. A host it cannot reach is logged and skipped; it never fails the run.

`--hook` is the SessionStart report. It only reads local files: unacknowledged P0s
first, spelled out; review items one line each; routine changes as a count. It prints
nothing at all when the `watch` setting is `off` (the default). `GT_WATCH=off|report`
in the environment overrides the setting, so a demo never has to change it.

## P0 comes from rules, never from a reading of commit prose

A change is P0 when (1) the host publishes a new security advisory; (2) a new tag or
release carries a CVE/GHSA id or the phrases "security fix|release|update" or
"vulnerability"; (3) a commit on the tracked branch carries a CVE/GHSA id; (4) a
change touches SECURITY.md or a watch_path AND matches rule 2's phrases; (5) it
matches one of the watch's own `p0_when` patterns. Bare words like "auth" or
"security" are deliberately NOT P0: a P0 that cries wolf stops being read.

Since 0.15.0 this ships in the `watch` module (plugin gt-watch, `/gt-watch:gt-watch`),
not in gt core; install.sh copies it into the hooks dir and wires `--hook` from there.

Standard library only, Python 3.8+.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


# Since 0.15.0 this script ships in the `watch` module (plugin gt-watch), so gt's
# gt_settings.py is beside it only in the hooks dir, where install.sh copies both. From
# the module's own scripts dir it is resolved, first hit wins (the rule gt_demo.sh uses):
#   1. beside this file (the hooks-dir copy);
#   2. $GT_CORE_SCRIPTS (explicit override);
#   3. the newest gt beside this plugin: <cache>/gt-watch/<v> -> <cache>/gt/<newest>/scripts,
#      or in a source checkout <repo>/golden-thread-watch/<v> -> <repo>/golden-thread/<newest>/scripts;
#   4. ~/.claude/golden-thread/hooks, then the installed gt in the plugin cache.
# Nothing found: the setting reads as off and the hook prints plain text.
def _newest_scripts(root):
    try:
        vs = [d for d in os.listdir(root) if re.fullmatch(r"\d+\.\d+\.\d+", d)
              and os.path.isfile(os.path.join(root, d, "scripts", "gt_settings.py"))]
    except OSError:
        return None
    if not vs:
        return None
    return os.path.join(root, max(vs, key=lambda s: tuple(int(x) for x in s.split("."))), "scripts")


def _core_scripts():
    if os.path.isfile(os.path.join(HERE, "gt_settings.py")):
        return HERE
    env = os.environ.get("GT_CORE_SCRIPTS")
    if env and os.path.isfile(os.path.join(env, "gt_settings.py")):
        return env
    parent = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
    for cand in (_newest_scripts(os.path.join(parent, "gt")),
                 _newest_scripts(os.path.join(parent, "golden-thread"))):
        if cand:
            return cand
    hooks = os.path.expanduser("~/.claude/golden-thread/hooks")
    if os.path.isfile(os.path.join(hooks, "gt_settings.py")):
        return hooks
    return _newest_scripts(os.path.expanduser("~/.claude/plugins/cache/golden-thread-plugin/gt"))


_CORE = _core_scripts()
if _CORE and _CORE not in sys.path:
    sys.path.insert(1, _CORE)

VAULT_CONFIG = os.path.expanduser("~/.claude/vault-config.json")
WATCH_REL = os.path.join("Projects", "golden-thread", "watches")
CRON_TAG = "# gt-watch"
TIMEOUT = int(os.environ.get("GT_WATCH_TIMEOUT") or 60)   # seconds per git/gh call
DEPTH = 200
MAX_COMMITS = 50
VALID_KINDS = ("commits", "tags", "releases")

CVE_RE = re.compile(r"\bCVE-\d{4}-\d{4,}\b", re.I)
GHSA_RE = re.compile(r"\bGHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}\b", re.I)
PHRASE_RE = re.compile(r"\bsecurity (?:fix|release|update)(?:es|s)?\b|\bvulnerabilit(?:y|ies)\b", re.I)
BREAKING_RE = re.compile(r"BREAKING CHANGE|\bbreaking:", re.I)
SEMVER_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


# -- locations ---------------------------------------------------------------------
def vault_path():
    env = os.environ.get("GT_VAULT")
    if env:
        return env
    try:
        with open(VAULT_CONFIG) as fh:
            return json.load(fh).get("vault_path") or ""
    except Exception:
        return ""


def state_dir():
    return os.environ.get("GT_WATCH_STATE") or os.path.expanduser("~/.claude/golden-thread/watch")


def watches_dir(vault=None):
    return os.path.join(vault or vault_path(), WATCH_REL)


def setting():
    env = (os.environ.get("GT_WATCH") or "").strip().lower()
    if env in ("off", "report"):
        return env
    try:
        import gt_settings
        return gt_settings.get("watch") or "off"
    except Exception:
        return "off"


def _sp(name):
    return os.path.join(state_dir(), name)


def _read_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def read_events():
    out = []
    try:
        with open(_sp("events.jsonl")) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    return out


def _log(msg):
    os.makedirs(state_dir(), exist_ok=True)
    with open(_sp("fetch.log"), "a") as fh:
        fh.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))


# -- frontmatter (small YAML subset: scalars, [a, b] lists, "- item" lists) ---------
def _strip_comment(v):
    out, q = [], None
    for i, ch in enumerate(v):
        if q:
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
        elif ch == "#" and (i == 0 or v[i - 1].isspace()):
            break
        out.append(ch)
    return "".join(out).strip()


def _scalar(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    if v.lower() in ("true", "yes"):
        return True
    if v.lower() in ("false", "no"):
        return False
    return v


def _split_list(inner):
    items, cur, q = [], [], None
    for ch in inner:
        if q:
            cur.append(ch)
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
            cur.append(ch)
        elif ch == ",":
            items.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    items.append("".join(cur))
    return [_scalar(x) for x in items if x.strip()]


def parse_frontmatter(text):
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    data, key = {}, None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^\s+-\s*(.*)$", line)
        if m and key is not None:
            if not isinstance(data.get(key), list):
                data[key] = []
            data[key].append(_scalar(_strip_comment(m.group(1))))
            continue
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), _strip_comment(m.group(2))
        if val.startswith("[") and val.endswith("]"):
            data[key] = _split_list(val[1:-1])
        elif val == "":
            data[key] = None
        else:
            data[key] = _scalar(val)
    return data


def _as_list(v):
    if v is None or v == "":
        return []
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    return [str(v)]


def load_watches(vault=None):
    d = watches_dir(vault)
    out = []
    try:
        names = sorted(os.listdir(d))
    except Exception:
        return out
    for n in names:
        if not n.endswith(".md") or n.startswith(("_", ".")):
            continue
        try:
            with open(os.path.join(d, n), encoding="utf-8") as fh:
                fm = parse_frontmatter(fh.read())
        except Exception:
            continue
        if not fm.get("url"):
            continue
        fm["slug"] = n[:-3]
        fm["track"] = [t for t in _as_list(fm.get("track")) if t in VALID_KINDS] or list(VALID_KINDS)
        fm["watch_paths"] = _as_list(fm.get("watch_paths"))
        fm["p0_when"] = _as_list(fm.get("p0_when"))
        out.append(fm)
    return out


# -- subprocess helpers -------------------------------------------------------------
def _env():
    e = dict(os.environ)
    e["GIT_TERMINAL_PROMPT"] = "0"
    e.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return e


def _run(args, cwd=None):
    """-> (ok, stdout, stderr). Never raises; bounded by TIMEOUT."""
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                           timeout=TIMEOUT, env=_env())
        return p.returncode == 0, p.stdout or "", (p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return False, "", "timed out after %ds" % TIMEOUT
    except Exception as exc:
        return False, "", str(exc)


def ls_remote(url):
    """-> (ok, head_target_branch, heads{name: sha}, tags{name: sha}, why)."""
    ok, out, err = _run(["git", "ls-remote", "--symref", url])
    if not ok:
        return False, None, {}, {}, err or "git ls-remote failed"
    head, heads, tags = None, {}, {}
    for line in out.splitlines():
        if line.startswith("ref:"):
            m = re.match(r"ref:\s*refs/heads/(\S+)\s+HEAD", line)
            if m:
                head = m.group(1)
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        sha, ref = parts
        if ref.startswith("refs/heads/"):
            heads[ref[len("refs/heads/"):]] = sha
        elif ref.startswith("refs/tags/") and not ref.endswith("^{}"):
            tags[ref[len("refs/tags/"):]] = sha
    return True, head, heads, tags, ""


# -- classification ----------------------------------------------------------------
def _major(v):
    m = SEMVER_RE.search(str(v or ""))
    return int(m.group(1)) if m else None


def classify(kind, name, text, paths, watch, prior_major=None):
    """-> (severity, [reasons]). kind: commit | tag | release | advisory."""
    blob = "%s\n%s" % (name or "", text or "")
    p0, review = [], []
    if kind == "advisory":
        p0.append("security advisory %s" % name)
    ids = sorted(set(m.group(0).upper() for m in CVE_RE.finditer(blob)) |
                 set("GHSA-" + m.group(0)[5:].lower() for m in GHSA_RE.finditer(blob)))
    phrase = PHRASE_RE.search(blob)
    wpaths = watch.get("watch_paths") or []
    touched = [p for p in (paths or []) if p == "SECURITY.md" or p.endswith("/SECURITY.md")
               or any(p == w or p.startswith(w.rstrip("/") + "/") or
                      (w.endswith("/") and p.startswith(w)) for w in wpaths)]
    if kind in ("tag", "release"):
        if ids:
            p0.append("%s carries %s" % (kind, ", ".join(ids)))
        if phrase:
            p0.append('%s says "%s"' % (kind, phrase.group(0).lower()))
    if kind == "commit" and ids:
        p0.append("commit carries %s" % ", ".join(ids))
    if touched and phrase and kind == "commit":
        p0.append('touches %s and says "%s"' % (", ".join(touched[:3]), phrase.group(0).lower()))
    for pat in watch.get("p0_when") or []:
        try:
            if re.search(pat, blob, re.I):
                p0.append("matches p0_when /%s/" % pat)
        except re.error:
            pass
    if p0:
        return "p0", p0
    if kind in ("tag", "release"):
        review.append("new %s" % kind)
        new_major = _major(name)
        base = [x for x in (prior_major, _major(watch.get("current_version"))) if x is not None]
        if new_major is not None and base and new_major > max(base):
            review.append("major version bump to %s" % name)
    if kind == "commit":
        watched = [p for p in touched if p in (paths or []) and any(
            p == w or p.startswith(w.rstrip("/") + "/") for w in wpaths)]
        if watched:
            review.append("touches watched path %s" % ", ".join(watched[:3]))
    if BREAKING_RE.search(blob):
        review.append("breaking change")
    if review:
        return "review", review
    return "routine", []


# -- gh (releases and advisories only) ---------------------------------------------
def gh_repo_for(watch):
    r = watch.get("gh_repo")
    if r:
        return str(r)
    m = re.match(r"^(?:https?://|git@|ssh://git@)github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$",
                 str(watch.get("url") or ""))
    return "%s/%s" % (m.group(1), m.group(2)) if m else None


def gh_fetch(repo):
    """-> (releases [{tag, name, body}], advisories [{id, summary}]) or (None, None)."""
    if not repo or shutil.which("gh") is None:
        return None, None
    ok, _, _ = _run(["gh", "auth", "status", "--hostname", "github.com"])
    if not ok:
        return None, None
    rel, adv = None, None
    ok, out, _ = _run(["gh", "api", "repos/%s/releases?per_page=30" % repo])
    if ok:
        try:
            rel = [{"tag": r.get("tag_name") or r.get("name") or "",
                    "name": r.get("name") or "", "body": r.get("body") or ""}
                   for r in json.loads(out)]
        except Exception:
            rel = None
    ok, out, _ = _run(["gh", "api", "repos/%s/security-advisories?per_page=30" % repo])
    if ok:
        try:
            adv = [{"id": a.get("ghsa_id") or "", "summary": a.get("summary") or "",
                    "severity": a.get("severity") or ""} for a in json.loads(out)]
        except Exception:
            adv = None
    return rel, adv


# -- fetch -------------------------------------------------------------------------
def _cache(slug):
    c = os.path.join(state_dir(), "cache", slug + ".git")
    if not os.path.isdir(c):
        os.makedirs(c, exist_ok=True)
        _run(["git", "init", "-q", "--bare", c])
    return c


def _commits(cache, old, new):
    have_old = old and _run(["git", "-C", cache, "cat-file", "-e", old + "^{commit}"])[0]
    rng = ["%s..%s" % (old, new)] if have_old else ["-n", "1", new]
    ok, out, _ = _run(["git", "-C", cache, "log", "--no-color", "--format=%x1e%H%x1f%B%x1f",
                       "--name-only", "-n", str(MAX_COMMITS)] + rng)
    res = []
    if not ok:
        return res
    for chunk in out.split("\x1e")[1:]:
        parts = chunk.split("\x1f")
        if len(parts) < 3:
            continue
        paths = [p.strip() for p in parts[2].splitlines() if p.strip()]
        res.append({"sha": parts[0].strip(), "msg": parts[1].strip(), "paths": paths})
    return res


def _tag_text(cache, url, names):
    specs = ["+refs/tags/%s:refs/tags/%s" % (n, n) for n in names]
    _run(["git", "-C", cache, "fetch", "-q", "--depth", "1", "--no-tags", url] + specs)
    out = {}
    for n in names:
        ok, ann, _ = _run(["git", "-C", cache, "for-each-ref", "--format=%(contents)", "refs/tags/" + n])
        ok2, body, _ = _run(["git", "-C", cache, "log", "-1", "--format=%B", "refs/tags/%s^{commit}" % n])
        out[n] = "\n".join(x.strip() for x in (ann if ok else "", body if ok2 else "") if x.strip())
    return out


def _eid(slug, kind, ref):
    return hashlib.sha1(("%s|%s|%s" % (slug, kind, ref)).encode()).hexdigest()[:12]


def fetch_one(w, prior):
    """-> (new_state_entry, [events], status_line)."""
    slug, url = w["slug"], str(w["url"])
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    ok, head_target, heads, tags, why = ls_remote(url)
    if not ok:
        entry = dict(prior or {})
        entry.update({"url": url, "last_fetch": now, "error": why.splitlines()[-1] if why else "unreachable"})
        return entry, [], "unreachable (%s) — skipped" % entry["error"]
    branch = str(w.get("branch") or head_target or ("main" if "main" in heads else
                                                     (sorted(heads)[0] if heads else "")))
    head = heads.get(branch)
    cache = _cache(slug)
    if head:
        _run(["git", "-C", cache, "fetch", "-q", "--depth", str(DEPTH), "--no-tags", url,
              "+refs/heads/%s:refs/gtw/head" % branch])
    events = []
    baseline = not prior or "head" not in prior

    def ev(kind, ref, title, text="", paths=None, pmaj=None):
        sev, reasons = classify(kind, ref if kind != "commit" else "", text, paths, w, pmaj)
        track_kind = {"commit": "commits", "tag": "tags", "release": "releases"}.get(kind)
        if track_kind and track_kind not in w["track"] and sev != "p0":
            return
        events.append({"id": _eid(slug, kind, ref), "ts": now, "slug": slug,
                       "label": w.get("label") or slug, "url": url, "kind": kind,
                       "ref": ref, "title": title, "severity": sev, "reasons": reasons,
                       "paths": (paths or [])[:20]})

    prior = prior or {}
    if not baseline:
        old = prior.get("head")
        if head and head != old and prior.get("branch", branch) == branch:
            for c in reversed(_commits(cache, old, head)):
                subj = c["msg"].splitlines()[0] if c["msg"] else c["sha"][:7]
                ev("commit", c["sha"], subj, c["msg"], c["paths"])
        old_tags = prior.get("tags") or {}
        new_tags = [t for t in sorted(tags) if old_tags.get(t) != tags[t]]
        if new_tags:
            majors = [m for m in (_major(t) for t in old_tags) if m is not None]
            pmaj = max(majors) if majors else None
            texts = _tag_text(cache, url, new_tags)
            for t in new_tags:
                ev("tag", t, "tag %s" % t, texts.get(t, ""), None, pmaj)
    rel, adv = gh_fetch(gh_repo_for(w))
    entry = {"url": url, "branch": branch, "head": head, "tags": tags, "last_fetch": now,
             "last_ok": now, "error": None,
             "releases": prior.get("releases") or [], "advisories": prior.get("advisories") or []}
    if rel is not None:
        seen = set(prior.get("releases") or [])
        if not baseline and "releases" in prior:
            majors = [m for m in (_major(t) for t in (prior.get("tags") or {})) if m is not None]
            for r in rel:
                if r["tag"] and r["tag"] not in seen:
                    ev("release", r["tag"], "release %s" % (r["name"] or r["tag"]),
                       "%s\n%s" % (r["name"], r["body"]), None, max(majors) if majors else None)
        entry["releases"] = sorted(seen | set(r["tag"] for r in rel if r["tag"]))
    if adv is not None:
        seen = set(prior.get("advisories") or [])
        if not baseline and "advisories" in prior:
            for a in adv:
                if a["id"] and a["id"] not in seen:
                    ev("advisory", a["id"], "advisory %s: %s" % (a["id"], a["summary"]), a["summary"])
        entry["advisories"] = sorted(seen | set(a["id"] for a in adv if a["id"]))
    status = "baseline taken" if baseline else "%d new event(s)" % len(events)
    return entry, events, status


def fetch(only=None):
    vault = vault_path()
    watches = load_watches(vault)
    if only:
        watches = [w for w in watches if w["slug"] == only]
    state = _read_json(_sp("state.json"), {})
    events = read_events()
    seq = max([e.get("seq", 0) for e in events] or [0])
    _log("fetch start: %d watch(es) in %s" % (len(watches), watches_dir(vault)))
    total = 0
    for w in watches:
        try:
            entry, evs, status = fetch_one(w, state.get(w["slug"]))
        except Exception as exc:          # one bad watch never fails the run
            _log("%s: error %s — skipped" % (w["slug"], exc))
            print("%s: error %s — skipped" % (w["slug"], exc))
            continue
        state[w["slug"]] = entry
        if evs:
            os.makedirs(state_dir(), exist_ok=True)
            with open(_sp("events.jsonl"), "a") as fh:
                for e in evs:
                    seq += 1
                    e["seq"] = seq
                    fh.write(json.dumps(e, sort_keys=True) + "\n")
        total += len(evs)
        _log("%s: %s" % (w["slug"], status))
        print("%s: %s" % (w["slug"], status))
    _write_json(_sp("state.json"), state)
    _log("fetch done: %d new event(s)" % total)
    return total


# -- report ------------------------------------------------------------------------
def unacked(events=None, acked=None):
    events = read_events() if events is None else events
    acked = _read_json(_sp("acked.json"), {}) if acked is None else acked
    cur = acked.get("cursors") or {}
    allc = acked.get("all", 0)
    return [e for e in events if e.get("seq", 0) > max(cur.get(e.get("slug"), 0), allc)]


def report():
    watches = load_watches()
    pend = unacked()
    if not pend:
        if watches:
            print("GOLDEN THREAD watch: %d watch(es), nothing new upstream." % len(watches))
        else:
            print("GOLDEN THREAD watch: on, but no watches yet — /gt-watch:gt-watch add <git-url>.")
        return 0
    p0 = [e for e in pend if e["severity"] == "p0"]
    rv = [e for e in pend if e["severity"] == "review"]
    rt = [e for e in pend if e["severity"] == "routine"]
    print("GOLDEN THREAD watch: %d P0, %d to review, %d routine — unacknowledged upstream changes."
          % (len(p0), len(rv), len(rt)))
    for e in p0:
        print("  P0      %s (%s): %s — %s" % (e["slug"], e.get("label") or e["url"], e["title"],
                                            "; ".join(e["reasons"])))
    for e in rv:
        print("  review  %s: %s — %s" % (e["slug"], e["title"], "; ".join(e["reasons"])))
    if rt:
        print("  routine %d change(s) across %d repo(s)" % (len(rt), len(set(e["slug"] for e in rt))))
    print("  explain: /gt-watch:gt-watch show <slug>   mark seen: /gt-watch:gt-watch ack [<slug>]")
    return len(p0)


def cron_missing_note():
    """One line when watches exist but no gt-watch crontab entry does, else None.

    The report reads what the cron fetch wrote, so without the entry it says "nothing new"
    for ever -- 0.15.0 found `install.sh --without watch` then `--with watch` leaving
    exactly that, silently. No crontab binary means no way to tell: say nothing."""
    if shutil.which("crontab") is None or not load_watches():
        return None
    if any(CRON_TAG in l for l in _crontab_lines()):
        return None
    return ("  ⚠ no gt-watch cron entry, so nothing is fetched and this report cannot change — "
            "python3 %s install-cron" % _shq(os.path.abspath(__file__)))


def _hook_report():
    r = report()
    note = cron_missing_note()
    if note:
        print(note)
    return r


def hook():
    if setting() == "off":
        return 0
    try:
        import gt_settings
        r, text = gt_settings.capture(_hook_report)
        gt_settings.emit(text, as_hook=True)
    except Exception:
        _hook_report()
    return 0


# -- interactive subcommands --------------------------------------------------------
def slugify(url):
    base = re.sub(r"\.git/?$", "", str(url).rstrip("/")).rstrip("/")
    base = re.split(r"[/:]", base)[-1] or "repo"
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "repo"


def _template():
    for p in (os.path.join(os.path.dirname(HERE), "templates", "watch.md"),
              os.path.expanduser("~/.claude/plugins/marketplaces/golden-thread-plugin/plugins/gt-watch/templates/watch.md")):
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as fh:
                return fh.read()
    return ("---\nurl:\nlabel:\ntrack: [tags, releases, commits]\nbranch:\nwatch_paths: [SECURITY.md]\n"
            "current_version:\np0_when: []\ngh_repo:\nstarred: false\nadded:\n---\n# Watch\n")


def _yaml_val(v):
    if isinstance(v, list):
        return "[%s]" % ", ".join(json.dumps(x) if re.search(r"[,\[\]#:]", x) else x for x in v)
    return "" if v is None else str(v)


def render_note(values):
    text = _template()
    head, sep, body = text.partition("\n---\n")
    out = []
    for line in head.splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*):(\s*)([^#]*?)(\s+#.*)?$", line)
        if m and m.group(1) in values:
            val = _yaml_val(values[m.group(1)])
            line = "%s: %s%s" % (m.group(1), val, m.group(4) or "") if val else \
                "%s:%s" % (m.group(1), ("   " + (m.group(4) or "").strip()) if m.group(4) else "")
        out.append(line)
    body = re.sub(r"^# .*$", "# " + str(values.get("label") or ""), body, count=1, flags=re.M)
    return "\n".join(out) + sep + body


def _opt(args, name, multi=False):
    vals, rest, i = [], [], 0
    while i < len(args):
        if args[i] == name and i + 1 < len(args):
            vals.append(args[i + 1])
            i += 2
            continue
        if args[i].startswith(name + "="):
            vals.append(args[i].split("=", 1)[1])
            i += 1
            continue
        rest.append(args[i])
        i += 1
    args[:] = rest
    if multi:
        return [x.strip() for v in vals for x in v.split(",") if x.strip()]
    return vals[-1] if vals else None


def cmd_add(args):
    label = _opt(args, "--label")
    track = _opt(args, "--track", multi=True)
    branch = _opt(args, "--branch")
    wpaths = _opt(args, "--watch-path", multi=True)
    cur = _opt(args, "--current-version")
    p0w = _opt(args, "--p0-when")
    p0w = [p0w] if p0w else []
    ghr = _opt(args, "--gh-repo")
    slug = _opt(args, "--slug")
    force = "--force" in args
    args = [a for a in args if a != "--force"]
    if not args:
        print("usage: gt_watch.py add <git-url> [--label L] [--track tags,commits,releases] "
              "[--branch B] [--watch-path P] [--current-version V] [--p0-when REGEX] [--gh-repo o/r]")
        return 2
    url = args[0]
    bad = [t for t in track if t not in VALID_KINDS]
    if bad:
        print("unknown --track kind(s): %s (valid: %s)" % (", ".join(bad), ", ".join(VALID_KINDS)))
        return 2
    vault = vault_path()
    if not vault or not os.path.isdir(vault):
        print("No vault found: set GT_VAULT or run /gt:gt-init.")
        return 1
    ok, _, _, _, why = ls_remote(url)
    if not ok:
        print("Cannot reach %s with `git ls-remote` — not added.\n  %s" % (url, why.splitlines()[-1] if why else ""))
        return 1
    slug = slug or slugify(url)
    path = os.path.join(watches_dir(vault), slug + ".md")
    if os.path.exists(path) and not force:
        print("A watch named %s already exists: %s (use --force to replace)" % (slug, path))
        return 1
    values = {"url": url, "label": label or slug, "added": time.strftime("%Y-%m-%d")}
    if track:
        values["track"] = track
    if branch:
        values["branch"] = branch
    if wpaths:
        values["watch_paths"] = wpaths
    if cur:
        values["current_version"] = cur
    if p0w:
        values["p0_when"] = p0w
    if ghr:
        values["gh_repo"] = ghr
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render_note(values))
    print("Watch added: %s" % path)
    # The baseline is taken by the fetch code (the only writer of state.json), so the
    # first real fetch reports only what changes AFTER the watch was created.
    fetch(only=slug)
    if setting() == "off":
        print("Note: the `watch` setting is off — nothing is fetched or reported until it is "
              "switched on (gt_settings.py set watch report).")
    return 0


def cmd_remove(args):
    if not args:
        print("usage: gt_watch.py remove <slug>")
        return 2
    path = os.path.join(watches_dir(), args[0] + ".md")
    if not os.path.exists(path):
        print("No watch named %s (%s)." % (args[0], path))
        return 1
    os.remove(path)
    print("Removed watch %s." % args[0])
    return 0


def cmd_list(args):
    ws = load_watches()
    if not ws:
        print("No watches in %s." % watches_dir())
        return 0
    state = _read_json(_sp("state.json"), {})
    pend = unacked()
    print("watch: %s   state: %s" % (setting(), state_dir()))
    for w in ws:
        s = state.get(w["slug"], {})
        n = [e for e in pend if e["slug"] == w["slug"]]
        p0 = sum(1 for e in n if e["severity"] == "p0")
        print("  %-20s %s  [%s]  last fetch %s%s  unacked %d (P0 %d)"
              % (w["slug"], w["url"], ",".join(w["track"]), s.get("last_fetch") or "never",
                 (" ERROR " + s["error"]) if s.get("error") else "", len(n), p0))
    return 0


def cmd_show(args):
    show_all = "--all" in args
    args = [a for a in args if a != "--all"]
    slug = args[0] if args else None
    evs = read_events() if show_all else unacked()
    if slug:
        evs = [e for e in evs if e["slug"] == slug]
    if not evs:
        print("No %sevents%s." % ("" if show_all else "unacknowledged ", " for " + slug if slug else ""))
        return 0
    for e in sorted(evs, key=lambda e: ({"p0": 0, "review": 1}.get(e["severity"], 2), e.get("seq", 0))):
        print("[%s] #%s %s %s %s — %s" % (e["severity"].upper(), e.get("seq"), e["slug"], e["kind"],
                                         e["ref"], e["title"]))
        if e["reasons"]:
            print("    why: %s" % "; ".join(e["reasons"]))
        if e.get("paths"):
            print("    paths: %s" % ", ".join(e["paths"]))
    if slug:
        print("cache (git log here for full commits): %s" % os.path.join(state_dir(), "cache", slug + ".git"))
    return 0


def cmd_ack(args):
    events = read_events()
    acked = _read_json(_sp("acked.json"), {})
    cur = acked.setdefault("cursors", {})
    if args:
        mx = max([e.get("seq", 0) for e in events if e["slug"] == args[0]] or [0])
        cur[args[0]] = max(cur.get(args[0], 0), mx)
        what = args[0]
    else:
        acked["all"] = max([e.get("seq", 0) for e in events] or [0])
        what = "all watches"
    acked["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    _write_json(_sp("acked.json"), acked)
    print("Acknowledged %s." % what)
    return 0


def _cron_expr(every):
    m = re.fullmatch(r"(\d+)([mhd])", (every or "1h").strip())
    if not m or int(m.group(1)) < 1:
        raise ValueError("--every must look like 30m, 1h, 6h or 1d")
    n, u = int(m.group(1)), m.group(2)
    if u == "m":
        return "*/%d * * * *" % n if n < 60 else "0 * * * *"
    if u == "h":
        return "0 * * * *" if n == 1 else "0 */%d * * *" % n
    return "0 9 * * *"


def _crontab_lines():
    ok, out, _ = _run(["crontab", "-l"])
    return [l for l in out.splitlines()] if ok else []


def _crontab_write(lines):
    body = "\n".join(lines) + "\n" if lines else ""
    try:
        p = subprocess.run(["crontab", "-"], input=body, capture_output=True, text=True, timeout=TIMEOUT)
        return p.returncode == 0, (p.stderr or "").strip()
    except Exception as exc:
        return False, str(exc)


def cmd_install_cron(args):
    every = _opt(args, "--every") or "1h"
    try:
        expr = _cron_expr(every)
    except ValueError as exc:
        print(exc)
        return 2
    if shutil.which("crontab") is None:
        print("No crontab binary on this machine — cron entry not installed.")
        return 1
    env = " ".join("%s=%s" % (k, _shq(os.environ[k])) for k in ("GT_VAULT", "GT_WATCH_STATE")
                   if os.environ.get(k))
    os.makedirs(state_dir(), exist_ok=True)
    cmd = "%s %s%s %s fetch >> %s 2>&1 %s" % (expr, (env + " ") if env else "", _shq(sys.executable),
                                            _shq(os.path.abspath(__file__)),
                                            _shq(_sp("fetch.log")), CRON_TAG)
    lines = [l for l in _crontab_lines() if CRON_TAG not in l] + [cmd]
    ok, err = _crontab_write(lines)
    if not ok:
        print("crontab refused the update: %s" % err)
        return 1
    print("Installed cron entry (every %s):\n  %s" % (every, cmd))
    return 0


def cmd_uninstall_cron(args):
    if shutil.which("crontab") is None:
        print("No crontab binary on this machine — nothing to remove.")
        return 0
    before = _crontab_lines()
    lines = [l for l in before if CRON_TAG not in l]
    if len(lines) == len(before):
        print("No gt-watch cron entry installed.")
        return 0
    ok, err = _crontab_write(lines)
    print("Removed the gt-watch cron entry." if ok else "crontab refused the update: %s" % err)
    return 0 if ok else 1


def _shq(s):
    s = str(s)
    return s if re.fullmatch(r"[\w@%+=:,./-]+", s) else "'" + s.replace("'", "'\\''") + "'"


USAGE = """usage: gt_watch.py <command>
  add <git-url> [--label L] [--track tags,commits,releases] [--branch B]
      [--watch-path P] [--current-version V] [--p0-when REGEX] [--gh-repo owner/repo]
  remove <slug>          stop watching (deletes the watch note)
  list                   watches, last fetch, unacknowledged counts
  fetch [--force]        the cron job; does nothing while the watch setting is off unless --force
  check                  fetch now, whatever the setting
  show [<slug>] [--all]  queued events, P0 first
  ack [<slug>]           mark events seen
  install-cron [--every 1h] | uninstall-cron
  --hook                 the SessionStart report (read-only)"""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--hook" in argv and not [a for a in argv if a != "--hook"]:
        return hook()
    argv = [a for a in argv if a != "--hook"]
    if not argv:
        print(USAGE)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "fetch":
        if setting() == "off" and "--force" not in rest:
            print("watch is off — nothing fetched (switch on: gt_settings.py set watch report).")
            return 0
        fetch()
        return 0
    if cmd == "check":
        fetch()
        return 0
    handlers = {"add": cmd_add, "remove": cmd_remove, "list": cmd_list, "show": cmd_show,
                "ack": cmd_ack, "install-cron": cmd_install_cron,
                "uninstall-cron": cmd_uninstall_cron}
    if cmd == "report":
        report()
        return 0
    if cmd not in handlers:
        print(USAGE)
        return 2
    return handlers[cmd](rest)


if __name__ == "__main__":
    raise SystemExit(main())
