"""Every vault step a skill runs from the shell has a route under gt sandbox mode (0.20.2).

Usability review 2026-10-04, B4: under gt sandbox mode only gt-open, gt-query and part of gt-work
knew to use the gt-vault MCP; fourteen skills ran vault scripts from the shell and got tracebacks.
This is the static check that keeps it closed, over EVERY SKILL.md of EVERY gt plugin (the newest
version of each):

  1. every gt script a skill names (gt_*.py, vault_*.py, wiki_*.py, ...) has a row in
     gt_vault_ops.SANDBOX_ROUTES -- a new script cannot ship without a decided route;
  2. a skill that names any script whose route is not plain "shell" carries the
     "**Under gt sandbox mode**" block, and that block names the script and its route: the
     gt-vault tool, "**terminal**", or "skipped";
  3. every gt-vault tool a block names exists on the server -- a renamed tool cannot leave a
     skill pointing at nothing.
"""
import re
import sys
import unittest

from _harness import REPO, SCRIPTS, latest_version_dir

sys.path.insert(0, str(SCRIPTS))
import gt_vault_mcp as M         # noqa: E402
import gt_vault_ops as OPS       # noqa: E402

PLUGINS = ("golden-thread", "golden-thread-wiki", "golden-thread-visualize", "golden-thread-demo",
           "golden-thread-farm", "golden-thread-flow", "golden-thread-watch",
           "golden-thread-usage", "golden-thread-lotr", "golden-thread-report-card")
SCRIPT = re.compile(r"(?<![\w.-])((?:gt|vault|wiki|lotr)[a-z_]*\.(?:py|sh))\b")
BLOCK = "**Under gt sandbox mode**"
NOT_TOOLS = {"vault_init", "vault_path", "vault_mcp", "vault_hints", "vault_refresh",
             "vault_reads", "vault_shim"}


def skills():
    out = []
    for name in PLUGINS:
        root = REPO / name
        if not root.is_dir():
            continue
        d = latest_version_dir(root) if name != "golden-thread" else SCRIPTS.parent
        for p in sorted((d / "skills").glob("*/SKILL.md")):
            out.append(p)
    return out


def block_of(text):
    i = text.find(BLOCK)
    if i < 0:
        return None
    m = re.search(r"(?m)^#{1,6} ", text[i:])
    return text[i:i + m.start()] if m else text[i:]


def steps(body):
    """Script mentions that are STEPS: inside a fenced code block, or an inline code span that
    carries arguments (`gt_close.py task <ID> ...`). A bare `gt_events.py` in prose describes
    what a tool does; it is not a step the skill runs."""
    fences = [(m.start(), m.end()) for m in re.finditer(r"(?ms)^\s*```.*?^\s*```", body)]
    spans = [(m.start(), m.end(), m.group(1)) for m in re.finditer(r"`([^`\n]+)`", body)]
    for m in SCRIPT.finditer(body):
        i = m.start()
        if any(a <= i < b for a, b in fences):
            yield m
            continue
        for a, b, inner in spans:
            if a <= i < b and (" " in inner.strip()):
                yield m
                break


def routes_for(script, after):
    """-> (route, subcommand or None) for one mention: 'script sub' when a following word on
    the same line is a known subcommand."""
    subs = {k.split(" ", 1)[1] for k in OPS.SANDBOX_ROUTES if k.startswith(script + " ")}
    for tok in re.findall(r"[A-Za-z][\w-]*", after[:120].split("\n", 1)[0]):
        if tok in subs:
            return OPS.SANDBOX_ROUTES[script + " " + tok], tok
    return OPS.SANDBOX_ROUTES.get(script), None


class SkillRoutes(unittest.TestCase):
    maxDiff = None
    def test_there_are_skills_to_check(self):
        self.assertGreater(len(skills()), 40)

    def test_every_named_script_has_a_route(self):
        missing = {}
        for p in skills():
            for s in set(SCRIPT.findall(p.read_text(encoding="utf-8"))):
                if s not in OPS.SANDBOX_ROUTES and not any(k.startswith(s + " ")
                                                           for k in OPS.SANDBOX_ROUTES):
                    missing.setdefault(s, []).append(p.parent.name)
        self.assertEqual(missing, {}, "decide a sandbox route in gt_vault_ops.SANDBOX_ROUTES")

    def test_every_vault_step_has_its_route_in_the_skill(self):
        problems = []
        for p in skills():
            text = p.read_text(encoding="utf-8")
            blk = block_of(text)
            body = text.replace(blk, "") if blk else text
            for m in steps(body):
                s = m.group(1)
                route, sub = routes_for(s, body[m.end():])
                if route in (None, "shell"):
                    continue
                where = "%s: %s%s -> %s" % (p.parent.name, s, " " + sub if sub else "", route)
                if blk is None:
                    problems.append(where + " (no sandbox block)")
                    continue
                want = {OPS.TERMINAL: "**terminal**", OPS.SKIP: "skipped"}.get(route, route)
                rows = [ln for ln in blk.splitlines() if ln.startswith("|") and s in ln
                        and (sub is None or re.search(r"\b%s\b" % re.escape(sub), ln))]
                if not any(want in ln for ln in rows):
                    problems.append(where + " (no row of the block routes it to %s)" % want)
        self.assertEqual(sorted(set(problems)), [])

    def test_blocks_name_only_tools_the_server_offers(self):
        offered = {t["name"] for t in M.TOOLS}
        bad = []
        for p in skills():
            blk = block_of(p.read_text(encoding="utf-8")) or ""
            for t in set(re.findall(r"\bvault_[a-z_]+\b(?!\.py)", blk)):
                if t not in offered and t not in NOT_TOOLS:
                    bad.append("%s: %s" % (p.parent.name, t))
        self.assertEqual(bad, [])

    def test_route_table_names_real_tools(self):
        offered = {t["name"] for t in M.TOOLS}
        for k, v in OPS.SANDBOX_ROUTES.items():
            for r in (v if isinstance(v, tuple) else (v,)):
                self.assertTrue(r in offered or r in (OPS.TERMINAL, OPS.SKIP, "shell")
                                or r in ("vault_read", "vault_list", "vault_search"), (k, r))

    def test_no_preview_label_left(self):
        for p in skills():
            blk = block_of(p.read_text(encoding="utf-8")) or ""
            self.assertNotIn("(preview)", blk, p.parent.name)


if __name__ == "__main__":
    unittest.main()
