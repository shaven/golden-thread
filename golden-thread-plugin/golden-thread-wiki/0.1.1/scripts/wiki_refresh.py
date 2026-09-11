#!/usr/bin/env python3
"""wiki_refresh.py - deterministic change detection for LLM Wiki sources.

Report-only: never modifies the vault. Resolves which sources are in scope,
asks git (local route) what changed upstream since the source was ingested,
and prints a report the LLM turns into supersessions + page updates.

Local route (source has `local:`):
  - repo root via `git rev-parse --show-toplevel`
  - `git fetch --prune origin` once per repo (skip with --no-fetch)
  - head = origin/<default branch>
  - base = `upstream_sha:` frontmatter if present, else the last commit
    touching the path on or before the `ingested:` date. This works for
    `type: summary` sources too, whose body is not verbatim upstream text.
  - changed = `git diff base..head -- <path>` is non-empty
  The working tree is never touched.

Web route (source has `url:` only): listed for the LLM to fetch and
compare; nothing is fetched here.

Sources already superseded (named in another source's `supersedes:`) are
skipped - they are history, not candidates.

Usage:
  wiki_refresh.py VAULT (--page "Page Title" | --repo NAME | --topic WORD | --all)
                  [--no-fetch] [--json] [--max-diff-lines 400]
Exit: 0 nothing changed, 1 changes found, 2 usage/error.
"""
import sys, os, re, json, subprocess, argparse, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wiki_lint import frontmatter, norm, load  # same parser everywhere

def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()

def default_branch(repo):
    try:
        return git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").split("/", 1)[1]
    except RuntimeError:
        for b in ("main", "master"):
            if git(repo, "rev-parse", "--verify", "-q", f"origin/{b}", check=False):
                return b
    raise RuntimeError("no origin/main or origin/master")

def resolve(vault, pages, sources, a):
    superseded = set()
    for s in sources.values():
        for old in s["fm"].get("supersedes", []) or []:
            superseded.add(norm(old))
    live = {k: v for k, v in sources.items() if k not in superseded}
    if a.all:
        sel = live
    elif a.page:
        pg = pages.get(norm(a.page))
        if not pg:
            sys.exit(f"no Knowledge page matching {a.page!r}")
        want = {norm(x) for x in pg["fm"].get("sources", []) or []}
        sel = {k: v for k, v in live.items() if k in want}
    elif a.repo:
        sel = {k: v for k, v in live.items() if a.repo.lower() in str(v["fm"].get("local", "")).lower()}
    elif a.topic:
        t = a.topic.lower()
        sel = {k: v for k, v in live.items() if t in v["file"].lower() or t in str(v["fm"].get("local", "")).lower()}
    else:
        sys.exit("scope required: --page, --repo, --topic, or --all")
    return [v for _, v in sorted(sel.items())]

def check_local(src, fetched, a):
    fm = src["fm"]; local = str(fm["local"]).strip()
    probe = local if os.path.isdir(local) else os.path.dirname(local)
    if not os.path.exists(probe):
        return {"route": "local", "state": "missing-on-disk", "local": local,
                "remote": fm.get("remote", "")}
    try:
        repo = git(probe, "rev-parse", "--show-toplevel")
    except RuntimeError:
        return {"route": "local", "state": "not-a-repo", "local": local}
    if repo not in fetched and not a.no_fetch:
        git(repo, "fetch", "--prune", "origin")
        fetched.add(repo)
    branch = default_branch(repo)
    head = git(repo, "rev-parse", f"origin/{branch}")
    rel = os.path.relpath(local.rstrip("/"), repo)
    base = fm.get("upstream_sha")
    if not base:
        ingested = str(fm.get("ingested", "")).strip() or "1970-01-01"
        base = git(repo, "log", "-1", "--format=%H", f"--before={ingested} 23:59:59",
                   f"origin/{branch}", "--", rel, check=False)
    if not base:
        return {"route": "local", "state": "no-baseline", "local": local, "head": head[:12],
                "note": "path has no commits on or before ingested date; first ingest of a new file"}
    stat = git(repo, "diff", "--stat", f"{base}..{head}", "--", rel)
    if not stat:
        return {"route": "local", "state": "unchanged", "local": local, "base": base[:12], "head": head[:12]}
    diff = git(repo, "diff", f"{base}..{head}", "--", rel).splitlines()
    return {"route": "local", "state": "changed", "local": local, "repo": repo, "branch": branch,
            "base": base[:12], "head": head[:12], "stat": stat,
            "diff": "\n".join(diff[:a.max_diff_lines]),
            "diff_truncated": len(diff) > a.max_diff_lines}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vault")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--page"); g.add_argument("--repo"); g.add_argument("--topic")
    g.add_argument("--all", action="store_true")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--max-diff-lines", type=int, default=400)
    a = ap.parse_args()
    pages, sources, _ = load(a.vault)
    fetched, results = set(), []
    for src in resolve(a.vault, pages, sources, a):
        fm = src["fm"]
        if fm.get("local"):
            try:
                r = check_local(src, fetched, a)
            except RuntimeError as e:
                r = {"route": "local", "state": "error", "local": fm["local"], "error": str(e)}
        elif fm.get("url"):
            r = {"route": "web", "state": "fetch-needed", "url": fm["url"]}
        else:
            r = {"route": "none", "state": "no-locator"}
        r["source"] = src["file"]
        r["cited_by"] = sorted(p["file"] for p in pages.values()
                               if norm(src["file"]) in {norm(x) for x in p["fm"].get("sources", []) or []})
        results.append(r)
    changed = [r for r in results if r["state"] == "changed"]
    if a.json:
        print(json.dumps({"checked": len(results), "changed": len(changed), "results": results}, indent=2))
    else:
        print(f"# wiki-refresh {datetime.date.today()} - {len(results)} sources, {len(changed)} changed\n")
        for r in results:
            line = f"- [{r['state']}] {r['source']}"
            if r["route"] == "local" and r.get("base"):
                line += f"  ({r['base']}..{r['head']})"
            if r["route"] == "web":
                line += f"  {r['url']}"
            if r.get("error") or r.get("note"):
                line += f"  {r.get('error') or r.get('note')}"
            print(line)
            if r["state"] == "changed":
                print("  cited by: " + (", ".join(r["cited_by"]) or "(no page cites this source)"))
                for s in r["stat"].splitlines():
                    print("  " + s)
        print("\nNext: for each [changed] source, read the diff (--json), present the change, and on approval "
              "supersede it (new Sources/ file with supersedes: + upstream_sha:) and update the citing pages.")
    sys.exit(1 if changed else 0)

if __name__ == "__main__":
    main()
