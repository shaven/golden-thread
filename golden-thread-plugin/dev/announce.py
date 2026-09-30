#!/usr/bin/env python3
"""announce.py — write, or post, the Discussions announcement for a release.

    announce.py --changelog ../CHANGELOG.md --version 0.17.10
    announce.py --changelog ../CHANGELOG.md --version 0.17.10 --mode draft
    announce.py --changelog ../CHANGELOG.md --version 0.17.10 --mode post --dry-run
    announce.py --changelog ../CHANGELOG.md --version 0.17.10 --mode post --out FILE

`dev/publish.sh` runs this as its `announced` step when the gt setting `release_announce`
is `draft` or `post` (`--mode` overrides the setting; without either, it is `off`).

  off    nothing is drafted or posted; publish.sh keeps its old warn-only check
  draft  the announcement is written to a file (default
         ~/.claude/golden-thread/announce-<version>.md) and its path printed
  post   ONE Discussion is created in the repo's "Announcements" category

WHY: announcing was the one publish requirement that depended on someone remembering it,
and it was the one that got skipped — 0.16.2 through 0.17.6 shipped with no announcement
until a batch post, and 0.17.8 and 0.17.9 shipped with none.

What one post covers: every release in the CHANGELOG, newest first from --version down,
until the first one an existing Discussion already names (matched as publish.sh matches:
dots literal, bounded both sides, so 0.12.1 is not named by 0.12.10). A batch of
unannounced releases becomes one post; a release a Discussion names is never announced
again. owner/repo come from `git remote get-url origin`, never a hard-coded account.

Before posting, the title and body pass a scrub: IPv4 addresses, home-folder paths, the
release scrub terms (found exactly as dev/scrub_check.py finds them) and the gt credential
scan (gt_secrets.py). Any hit — or any part of the scrub that cannot run — posts nothing,
writes the draft, and names the KIND of hit, never the matched text. Without a working
`gh` (missing, not logged in, an API error) `post` falls back to `draft` and says so.

--dry-run prints the announcement that would be posted and writes and posts nothing.

Exit is always 0 (2 only for a usage error): the release is already published when this
runs, and announcing must never fail it.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODES = ("off", "draft", "post")
CATEGORY = "Announcements"

HEADING = re.compile(r"^## gt (\d+\.\d+\.\d+)\s+[—-]+\s*(.*?)\s*$")
SUBHEADING = re.compile(r"^### (.+?)\s*$")
IPV4 = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}"
                  r"(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")
HOME = re.compile(r"(?:/Users/|/home/)[A-Za-z0-9._-]+")

LIST_QUERY = (
    "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){id "
    "discussionCategories(first:25){nodes{id name}} "
    "discussions(first:50,orderBy:{field:CREATED_AT,direction:DESC}){nodes{title body}}}}")
CREATE_MUTATION = (
    "mutation($repositoryId:ID!,$categoryId:ID!,$title:String!,$body:String!){"
    "createDiscussion(input:{repositoryId:$repositoryId,categoryId:$categoryId,"
    "title:$title,body:$body}){discussion{url}}}")


def say(msg=""):
    print(msg, flush=True)


# -- setting ------------------------------------------------------------------------------
def setting_mode():
    """`release_announce` from ~/.claude/vault-config.json; anything unrecognised is off."""
    try:
        cfg = json.loads((Path.home() / ".claude" / "vault-config.json").read_text(encoding="utf-8"))
        v = cfg.get("release_announce")
    except (OSError, ValueError, AttributeError):
        v = None
    v = v.strip().lower() if isinstance(v, str) else ""
    return v if v in MODES else "off"


# -- CHANGELOG ----------------------------------------------------------------------------
def vkey(v):
    return tuple(int(x) for x in v.split("."))


def parse_changelog(text):
    """-> [{version, date, lines}] in file order. Only `## gt <x.y.z> — <date>` headings."""
    out, cur = [], None
    for line in text.splitlines():
        m = HEADING.match(line)
        if m:
            cur = {"version": m.group(1), "date": m.group(2), "lines": []}
            out.append(cur)
        elif line.startswith("## "):
            cur = None
        elif cur is not None:
            cur["lines"].append(line)
    return out


def _plain(s):
    return re.sub(r"\*\*|`", "", s).strip()


def first_paragraph(lines):
    para = []
    for line in lines:
        if line.startswith("### ") or line.strip() == "---":
            break
        if not line.strip():
            if para:
                break
            continue
        para.append(line.strip())
    return " ".join(para)


def headlines(rel):
    """The release's `###` sub-headings, minus the standing 'Known'/'Measured' sections."""
    hs = []
    for line in rel["lines"]:
        m = SUBHEADING.match(line)
        if m and not re.match(r"(?i)(known|measured)\b", _plain(m.group(1))):
            hs.append(m.group(1))
    return hs


def first_sentence(text, limit=90):
    s = _plain(text)
    m = re.match(r"(.+?[.:!?])(?:\s|$)", s)
    s = m.group(1).rstrip(".:") if m else s
    return shorten(s, limit)


def shorten(s, limit):
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0].rstrip(",;:—- ")
    return cut + "…"


# -- which releases -----------------------------------------------------------------------
def names(ver):
    """publish.sh check_announced's matcher, verbatim: dots literal, bounded both sides."""
    return re.compile(r"(?<![\w.])v?" + re.escape(ver) + r"(?!\w|\.\d)")


def named_by(ver, nodes):
    pat = names(ver)
    return any(pat.search((n.get("title") or "") + "\n" + (n.get("body") or "")) for n in nodes)


def unannounced(releases, version, nodes):
    """Releases at or below `version`, newest first, up to the first one a Discussion names."""
    todo = []
    for rel in sorted((r for r in releases if vkey(r["version"]) <= vkey(version)),
                      key=lambda r: vkey(r["version"]), reverse=True):
        if named_by(rel["version"], nodes):
            break
        todo.append(rel)
    return todo


# -- the text -----------------------------------------------------------------------------
def compose(todo, owner, name):
    """-> (title, body) for the releases in `todo` (newest first)."""
    newest, oldest = todo[0]["version"], todo[-1]["version"]
    heads = []
    for rel in todo:
        hs = headlines(rel)
        if hs:
            heads.extend(_plain(h) for h in hs)
        else:
            heads.append(first_sentence(first_paragraph(rel["lines"]), 60))
    summary = shorten("; ".join(h for h in heads[:3] if h), 80) or "release notes"
    if len(todo) == 1:
        title = "gt %s — %s" % (newest, summary)
        intro = "gt %s is out." % newest
    else:
        title = "gt %s → %s — %s" % (oldest, newest, summary)
        intro = "gt %s is out, and with it %d releases that were not announced here: %s." % (
            newest, len(todo) - 1, ", ".join(r["version"] for r in todo[1:]))
    base = "https://github.com/%s/%s" % (owner, name)
    tag = "v%s" % newest
    parts = [intro, "",
             "**Install or upgrade:** [%s](%s/releases/tag/%s) — `bash install.sh` in the "
             "release, then restart Claude Code." % (tag, base, tag), ""]
    for rel in todo:
        parts.append("### gt %s — %s" % (rel["version"], rel["date"]))
        parts.append("")
        para = first_paragraph(rel["lines"])
        if para:
            parts.append(para)
            parts.append("")
        hs = headlines(rel)
        if hs:
            parts.extend("- %s" % h for h in hs)
            parts.append("")
    parts.append("Everything, with the reasons: [CHANGELOG](%s/blob/%s/CHANGELOG.md)" % (base, tag))
    return title, "\n".join(parts) + "\n"


# -- scrub --------------------------------------------------------------------------------
def load_scrub_terms():
    """The release scrub's own term list, located by dev/scrub_check.py. None = unavailable."""
    try:
        sys.path.insert(0, str(HERE))
        import scrub_check
        pats = scrub_check.load_terms(required=False)
    except Exception:
        return None
    finally:
        if sys.path and sys.path[0] == str(HERE):
            sys.path.pop(0)
    return pats or None


def secrets_script():
    env = os.environ.get("GT_SECRETS_BIN")
    if env and Path(env).is_file():
        return Path(env)
    vers = []
    for d in (HERE.parent / "golden-thread").glob("*/scripts/gt_secrets.py"):
        v = d.parent.parent.name
        if re.fullmatch(r"\d+\.\d+\.\d+", v):
            vers.append((vkey(v), d))
    return max(vers)[1] if vers else None


def scrub(text):
    """-> list of KINDS of hit. Never the matched text. Empty means the text may be posted."""
    kinds = []
    if IPV4.search(text):
        kinds.append("IPv4 address")
    if HOME.search(text):
        kinds.append("home-folder path")
    pats = load_scrub_terms()
    if pats is None:
        kinds.append("scrub terms unavailable (a scrub that cannot run is not a pass)")
    elif any(p.search(line) for line in text.splitlines() for p in pats):
        kinds.append("scrub term")
    script = secrets_script()
    if script is None:
        kinds.append("credential scan unavailable (gt_secrets.py not found)")
        return kinds
    tmpd = tempfile.mkdtemp(prefix="gt-announce-scan-")
    try:
        (Path(tmpd) / "announcement.md").write_text(text, encoding="utf-8")
        p = subprocess.run([sys.executable, str(script), tmpd], capture_output=True,
                           text=True, timeout=300)
        rc = p.returncode
    except (OSError, subprocess.SubprocessError):
        rc = None
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
    if rc == 1:
        kinds.append("credential scan")
    elif rc != 0:
        kinds.append("credential scan could not run")
    return kinds


# -- GitHub -------------------------------------------------------------------------------
def github_repo():
    """(owner, name) of origin, as publish.sh's github_repo derives it; None if not GitHub."""
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True,
                             text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"^(?:git@github\.com:|ssh://git@github\.com/|https?://(?:[^@/]+@)?github\.com/)"
                 r"([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    return (m.group(1), m.group(2)) if m else None


def gh_graphql(fields):
    """-> parsed JSON, or None when gh is missing, not logged in, or the call failed."""
    if not shutil.which("gh"):
        return None
    args = ["gh", "api", "graphql"]
    for k, v in fields:
        args += ["-f", "%s=%s" % (k, v)]
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    try:
        d = json.loads(p.stdout)
    except ValueError:
        return None
    return None if not isinstance(d, dict) or d.get("errors") else d


def repo_state(owner, name):
    """-> (repo_id, category_id or None, discussion nodes) or None if gh cannot answer."""
    d = gh_graphql([("owner", owner), ("name", name), ("query", LIST_QUERY)])
    try:
        repo = d["data"]["repository"]
        nodes = repo["discussions"]["nodes"] or []
    except (TypeError, KeyError):
        return None
    cats = ((repo.get("discussionCategories") or {}).get("nodes")) or []
    cat = next((c.get("id") for c in cats if (c.get("name") or "").lower() == CATEGORY.lower()),
               None)
    return repo.get("id"), cat, nodes


# -- main ---------------------------------------------------------------------------------
def write_draft(path, title, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# %s\n\n%s" % (title, body), encoding="utf-8")
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="write or post the Discussions announcement for a release")
    ap.add_argument("--changelog", required=True, help="the repository's CHANGELOG.md")
    ap.add_argument("--version", required=True, help="the release being published")
    ap.add_argument("--mode", choices=MODES,
                    help="override the release_announce setting (default: the setting)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the announcement that would be posted; write and post nothing")
    ap.add_argument("--out", metavar="FILE",
                    help="draft path (default ~/.claude/golden-thread/announce-<version>.md)")
    a = ap.parse_args(argv)

    mode = a.mode or setting_mode()
    if mode == "off":
        say("release_announce is off — nothing drafted or posted")
        return 0
    try:
        releases = parse_changelog(Path(a.changelog).read_text(encoding="utf-8"))
    except OSError as exc:
        say("cannot read the CHANGELOG (%s) — nothing drafted or posted" % exc.__class__.__name__)
        return 0
    if not any(r["version"] == a.version for r in releases):
        say("the CHANGELOG has no '## gt %s — <date>' section — nothing drafted or posted"
            % a.version)
        return 0
    slug = github_repo()
    if slug is None:
        say("origin is not a GitHub remote — nothing drafted or posted")
        return 0
    owner, name = slug

    state = repo_state(owner, name)
    fallback = None
    if state is None:
        # Cannot see what is announced, so cover only this release; a person checks the rest.
        nodes, repo_id, cat_id = [], None, None
        todo = [r for r in releases if r["version"] == a.version]
        if mode == "post":
            fallback = "gh is missing, not logged in, or could not list Discussions"
    else:
        repo_id, cat_id, nodes = state
        todo = unannounced(releases, a.version, nodes)
        if not todo:
            say("a Discussion already names %s (%s/%s) — nothing to announce"
                % (a.version, owner, name))
            return 0
        if mode == "post" and (not repo_id or not cat_id):
            fallback = "%s/%s has no '%s' Discussions category" % (owner, name, CATEGORY)

    title, body = compose(todo, owner, name)
    covers = ", ".join(r["version"] for r in todo)
    hits = scrub(title + "\n" + body)
    if mode == "post" and hits and fallback is None:
        fallback = "the scrub found: %s" % "; ".join(hits)
    elif hits:
        say("scrub found: %s" % "; ".join(hits))
    out = Path(a.out).expanduser() if a.out else (
        Path.home() / ".claude" / "golden-thread" / ("announce-%s.md" % a.version))

    if a.dry_run:
        say("--- announcement (%s/%s, covers %s) ---" % (owner, name, covers))
        say("title: %s" % title)
        say("")
        say(body.rstrip("\n"))
        say("--- end ---")
        if mode == "post" and fallback is None:
            say("dry run: would post this to %s/%s %s — nothing posted"
                % (owner, name, CATEGORY))
        else:
            say("dry run: would write the draft to %s%s — nothing written or posted"
                % (out, "" if fallback is None else " (not posting: %s)" % fallback))
        return 0

    if mode == "post" and fallback is None:
        d = gh_graphql([("repositoryId", repo_id), ("categoryId", cat_id), ("title", title),
                        ("body", body), ("query", CREATE_MUTATION)])
        try:
            url = d["data"]["createDiscussion"]["discussion"]["url"]
        except (TypeError, KeyError):
            fallback = "gh could not create the Discussion"
        else:
            say("posted the announcement for %s: %s" % (covers, url))
            return 0

    write_draft(out, title, body)
    if fallback:
        say("NOT POSTED — %s. Falling back to draft." % fallback)
    say("announcement draft for %s written: %s" % (covers, out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
