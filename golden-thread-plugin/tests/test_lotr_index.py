"""gt-lotr: curated profiles, the BM25 index behind `find`, and recipes.

Pure in-process tests: these modules read no HOME, no network and no secrets.
"""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

from _harness import REPO, latest_version_dir

_GW_ROOT = REPO / "golden-thread-lotr"
try:
    GW = latest_version_dir(_GW_ROOT)
except RuntimeError:   # before the plugin manifest exists, take the newest version dir
    GW = max((d for d in _GW_ROOT.iterdir() if re.fullmatch(r"\d+\.\d+\.\d+", d.name)),
             key=lambda d: tuple(int(x) for x in d.name.split(".")))
sys.path.insert(0, str(GW / "scripts"))

from lotrlib import profiles, recipes  # noqa: E402
from lotrlib.errors import GatewayError  # noqa: E402
from lotrlib.index import Index, build_docs, tokenize  # noqa: E402

SHIPPED = GW / "templates" / "recipes"


def conn(cid, profile, identity, description, **extra):
    return dict({"id": cid, "profile": profile, "identity": identity,
                 "description": description, "enabled": True}, **extra)


CONNS = [
    conn("github@personal", "github", "shaven @ github.com", "GitHub repos, issues and PRs as shaven"),
    conn("jira@work", "jira-v3", "shaven @ acme.atlassian.net", "Acme Jira Cloud"),
    conn("jiradc@work", "jira-v2", "shaven @ jira.acme.local", "Acme Jira Data Center"),
    conn("m365@work", "graph", "shaven @ acme.com", "Outlook mail, calendar and OneDrive"),
    conn("weather@personal", "generic", "anonymous", "Weather service JSON API"),
]


def shipped():
    return recipes.load_recipes([SHIPPED])


class ProfileTables(unittest.TestCase):
    EXPECTED = {
        "github": {"list_pulls", "get_pull", "list_issues", "get_issue", "search_issues",
                   "create_issue", "comment_issue", "merge_pull", "get_repo",
                   "list_workflow_runs", "graphql"},
        "jira-v3": {"myself", "search", "get_issue", "get_transitions", "transition_issue",
                    "add_comment", "create_issue"},
        "graph": {"me", "list_messages", "get_message", "send_mail", "list_events",
                  "list_drive_root", "search_drive"},
        "generic": set(),
    }
    EXPECTED["jira-v2"] = EXPECTED["jira-v3"]

    def test_profiles_present_and_well_formed(self):
        self.assertEqual(set(profiles.PROFILES), set(self.EXPECTED))
        for name, prof in profiles.PROFILES.items():
            with self.subTest(profile=name):
                self.assertEqual(prof["name"], name)
                for k in ("title", "auth_hint", "noise_keys", "ops"):
                    self.assertIn(k, prof)
                self.assertIn(prof["pagination"],
                              ("link-header", "jira-token", "jira-startat", "odata", None))
                self.assertEqual({o["name"] for o in prof["ops"]}, self.EXPECTED[name])
                self.assertEqual(len(prof["ops"]), len({o["name"] for o in prof["ops"]}))
                for op in prof["ops"]:
                    self.assertIn(op["method"], profiles.METHODS)
                    self.assertTrue(op["path"].startswith("/"))
                    self.assertTrue(op["summary"] and len(op["summary"]) < 200)
                    self.assertIsInstance(op["params"], dict)
                    self.assertIsInstance(op["query_defaults"], dict)
                    self.assertIsInstance(op["tags"], list)
                    for p, desc in op["params"].items():
                        self.assertTrue(isinstance(desc, str) and desc, f"{name}.{op['name']}.{p}")
        self.assertTrue(8 <= len(profiles.PROFILES["github"]["ops"]) <= 15)

    def test_path_placeholders_are_declared_params(self):
        for name, prof in profiles.PROFILES.items():
            for op in prof["ops"]:
                for ph in profiles.placeholders(op["path"]):
                    self.assertIn(ph, op["params"], f"{name}.{op['name']} path {{{ph}}} undeclared")

    def test_known_real_paths(self):
        def r(p, o):
            d = profiles.resolve_op(profiles.get(p), o)
            return d["method"], d["path"]
        self.assertEqual(r("github", "merge_pull"), ("PUT", "/repos/{owner}/{repo}/pulls/{pull_number}/merge"))
        self.assertEqual(r("github", "graphql"), ("POST", "/graphql"))
        self.assertEqual(r("github", "search_issues"), ("GET", "/search/issues"))
        self.assertEqual(r("jira-v3", "search"), ("GET", "/rest/api/3/search/jql"))
        self.assertEqual(r("jira-v2", "search"), ("GET", "/rest/api/2/search"))
        self.assertEqual(r("jira-v2", "get_issue"), ("GET", "/rest/api/2/issue/{issueIdOrKey}"))
        self.assertEqual(r("graph", "send_mail"), ("POST", "/me/sendMail"))
        self.assertEqual(r("graph", "list_events"), ("GET", "/me/calendarView"))
        ev = profiles.resolve_op(profiles.get("graph"), "list_events")
        self.assertIn("startDateTime", ev["params"])
        self.assertIn("endDateTime", ev["params"])

    def test_query_defaults_keep_results_small(self):
        g = profiles.get("github")
        self.assertEqual(profiles.resolve_op(g, "list_pulls")["query_defaults"]["per_page"], 20)
        js = profiles.resolve_op(profiles.get("jira-v3"), "search")["query_defaults"]
        self.assertEqual(js, {"maxResults": 20, "fields": "summary,status,assignee,priority,updated"})
        gi = profiles.resolve_op(profiles.get("jira-v3"), "get_issue")["query_defaults"]
        self.assertEqual(gi["fields"], "summary,status,assignee,priority,updated")
        lm = profiles.resolve_op(profiles.get("graph"), "list_messages")["query_defaults"]
        self.assertEqual(lm, {"$select": "subject,from,receivedDateTime,isRead", "$top": 20})

    def test_tiers(self):
        self.assertEqual(profiles.resolve_op(profiles.get("github"), "merge_pull")["tier"], "consent")
        self.assertEqual(profiles.resolve_op(profiles.get("graph"), "send_mail")["tier"], "consent")
        for p, prof in profiles.PROFILES.items():
            for op in prof["ops"]:
                if op["method"] in ("GET", "HEAD"):
                    self.assertNotIn("tier", op, f"{p}.{op['name']}: GET reads stay implicit")

    def test_get_unknown_profile(self):
        with self.assertRaises(GatewayError) as cm:
            profiles.get("gitlab")
        self.assertEqual(cm.exception.code, "unknown_profile")

    def test_get_returns_a_copy(self):
        profiles.get("github")["ops"].clear()
        self.assertTrue(profiles.PROFILES["github"]["ops"])


class ResolveOp(unittest.TestCase):
    def test_curated(self):
        d = profiles.resolve_op(profiles.get("github"), "list_pulls")
        for k in ("name", "method", "path", "summary", "params", "query_defaults", "tags", "graphql"):
            self.assertIn(k, d)
        self.assertEqual(d["name"], "list_pulls")
        self.assertFalse(d["graphql"])
        self.assertTrue(profiles.resolve_op(profiles.get("github"), "graphql")["graphql"])

    def test_raw(self):
        d = profiles.resolve_op(profiles.get("github"), "get /user/repos")
        self.assertIsNone(d["name"])
        self.assertEqual((d["method"], d["path"]), ("GET", "/user/repos"))
        self.assertFalse(d["graphql"])
        d = profiles.resolve_op(profiles.get("github"), "GET /repos/{owner}/{repo}/branches")
        self.assertEqual(set(d["params"]), {"owner", "repo"})
        d = profiles.resolve_op(profiles.get("generic"), "GET /v1/forecast?days=3")
        self.assertEqual((d["path"], d["query_defaults"]), ("/v1/forecast", {"days": "3"}))

    def test_raw_graphql_on_github_only(self):
        self.assertTrue(profiles.resolve_op(profiles.get("github"), "POST /graphql")["graphql"])
        self.assertFalse(profiles.resolve_op(profiles.get("generic"), "POST /graphql")["graphql"])

    def test_raw_curated_endpoint_keeps_tier(self):
        d = profiles.resolve_op(profiles.get("github"), "PUT /repos/a/b/pulls/7/merge")
        self.assertEqual(d.get("tier"), "consent")
        self.assertIsNone(d["name"])
        self.assertEqual(profiles.resolve_op(profiles.get("graph"), "POST /me/sendMail").get("tier"),
                         "consent")

    def test_raw_rejects_bad(self):
        g = profiles.get("github")
        for bad in ("FETCH /x", "GET x", "GET //evil.example/x", "GET https://evil/x",
                    "GET /a/../b", "/repos/a/b"):
            with self.subTest(op=bad):
                with self.assertRaises(GatewayError) as cm:
                    profiles.resolve_op(g, bad)
                self.assertEqual(cm.exception.code, "bad_op")

    def test_unknown_curated_hints(self):
        with self.assertRaises(GatewayError) as cm:
            profiles.resolve_op(profiles.get("github"), "list_pull")
        self.assertEqual(cm.exception.code, "unknown_op")
        self.assertIn("list_pulls", cm.exception.hints)
        with self.assertRaises(GatewayError) as cm:
            profiles.resolve_op(profiles.get("generic"), "list_things")
        self.assertEqual(cm.exception.code, "unknown_op")


class Search(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.recipes = shipped()
        cls.docs = build_docs(CONNS, cls.recipes)
        cls.idx = Index(cls.docs)

    def test_tokenize(self):
        self.assertEqual(tokenize("list_pulls /repos/{owner}.x"), ["list", "pull", "repo", "owner", "x"])

    def test_build_docs_shape(self):
        kinds = {}
        for d in self.docs:
            for k in ("key", "connection", "op", "kind", "summary", "text"):
                self.assertIn(k, d)
            kinds.setdefault(d["kind"], 0)
            kinds[d["kind"]] += 1
        self.assertEqual(kinds["connection"], len(CONNS))
        self.assertEqual(kinds["op"], 11 + 7 + 7 + 7)
        # my_open_prs + pr_status on github; my_open_issues on both jiras; today on graph
        self.assertEqual(kinds["recipe"], 5)
        self.assertEqual(len({d["key"] for d in self.docs}), len(self.docs))
        d = next(x for x in self.docs if x["key"] == "github@personal:list_pulls")
        for word in ("github@personal", "shaven", "list", "pulls", "owner", "pull"):
            self.assertIn(word, d["text"].lower())
        merge = next(x for x in self.docs if x["key"] == "github@personal:merge_pull")
        self.assertEqual(merge["tier"], "consent")

    def test_disabled_connection_has_no_docs(self):
        docs = build_docs([dict(CONNS[0], enabled=False)], self.recipes)
        self.assertEqual(docs, [])

    def top(self, q, **kw):
        return self.idx.search(q, **kw)

    def test_my_open_pull_requests(self):
        hits = self.top("my open pull requests")
        self.assertEqual(hits[0]["connection"], "github@personal")
        self.assertIn(hits[0]["op"], ("github.my_open_prs", "list_pulls"))

    def test_jira_issues_assigned_to_me(self):
        hits = self.top("jira issues assigned to me")
        self.assertEqual((hits[0]["op"], hits[0]["kind"]), ("jira.my_open_issues", "recipe"))

    def test_other_queries(self):
        self.assertEqual(self.top("send an email")[0]["op"], "send_mail")
        self.assertEqual(self.top("today's meetings calendar")[0]["op"], "m365.today")
        self.assertEqual(self.top("merge pull request")[0]["op"], "merge_pull")
        self.assertEqual(self.top("workflow runs ci")[0]["op"], "list_workflow_runs")

    def test_connection_filter(self):
        hits = self.top("issues", connection="jiradc@work", limit=50)
        self.assertTrue(hits)
        self.assertEqual({h["connection"] for h in hits}, {"jiradc@work"})
        hits = self.top("issues", connection="jira*", limit=50)
        self.assertEqual({h["connection"] for h in hits}, {"jira@work", "jiradc@work"})

    def test_limit_and_scores_descending(self):
        hits = self.top("issue", limit=3)
        self.assertEqual(len(hits), 3)
        scores = [h["score"] for h in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_no_match(self):
        self.assertEqual(self.top("zzqqx"), [])

    def test_empty_query_is_the_catalog(self):
        for q in ("", "   ", "--"):
            hits = self.top(q, limit=200)
            self.assertEqual([h["kind"] for h in hits], ["connection"] * len(CONNS))
            self.assertEqual([h["key"] for h in hits], sorted(c["id"] for c in CONNS))
        self.assertEqual([h["key"] for h in self.top("", connection="m365@work")], ["m365@work"])

    def test_stale_demotion(self):
        fresh = Index(build_docs(CONNS, self.recipes)).search("merge pull request")[0]
        stale_conns = [dict(c, stale_ops=["merge_pull"]) if c["profile"] == "github" else c
                       for c in CONNS]
        idx = Index(build_docs(stale_conns, self.recipes))
        hits = idx.search("merge pull request", limit=50)
        merged = next(h for h in hits if h["op"] == "merge_pull")
        self.assertTrue(merged["stale"])
        self.assertAlmostEqual(merged["score"], fresh["score"] * 0.3, places=4)
        self.assertNotEqual(hits[0]["op"], "merge_pull")

    def test_recipe_boost(self):
        doc = {"connection": "c@z", "op": None, "summary": "s", "text": "alpha beta"}
        idx = Index([dict(doc, key="a", kind="op"), dict(doc, key="b", kind="recipe")])
        a, b = sorted(idx.search("alpha"), key=lambda h: h["key"])
        self.assertAlmostEqual(b["score"], a["score"] * 1.5, places=5)

    def test_ties_ordered_by_key(self):
        doc = {"connection": "c@z", "op": None, "kind": "op", "summary": "s", "text": "alpha"}
        idx = Index([dict(doc, key=k) for k in ("c", "a", "b")])
        self.assertEqual([h["key"] for h in idx.search("alpha")], ["a", "b", "c"])


class Recipes(unittest.TestCase):
    def test_shipped_recipes_load_and_apply(self):
        rs = {r["name"]: r for r in shipped()}
        self.assertEqual(set(rs), {"github.my_open_prs", "github.pr_status",
                                   "jira.my_open_issues", "m365.today"})
        by_profile = {c["profile"]: c for c in CONNS}
        expect = {"github.my_open_prs": {"github"}, "github.pr_status": {"github"},
                  "jira.my_open_issues": {"jira-v3", "jira-v2"}, "m365.today": {"graph"}}
        for name, profs in expect.items():
            for p, c in by_profile.items():
                self.assertEqual(recipes.applicable(rs[name], c), p in profs, f"{name} on {p}")
            self.assertEqual(rs[name]["tier"], "read")

    def test_shipped_expansions(self):
        rs = {r["name"]: r for r in shipped()}
        gh, jira, jdc, m365 = CONNS[0], CONNS[1], CONNS[2], CONNS[3]
        steps = recipes.expand(rs["github.my_open_prs"], gh, {})
        self.assertEqual(steps[0]["op"], "search_issues")
        self.assertEqual(steps[0]["args"]["q"], "is:pr is:open author:@me")
        steps = recipes.expand(rs["github.my_open_prs"], gh, {"repo": "a/b"})
        self.assertEqual(steps[0]["args"]["q"], "is:pr is:open author:@me repo:a/b")
        steps = recipes.expand(rs["github.pr_status"], gh, {"owner": "o", "repo": "r", "pull_number": 7})
        self.assertEqual(steps, [{"op": "get_pull", "args": {"owner": "o", "repo": "r", "pull_number": 7}}])
        for c in (jira, jdc):
            jql = recipes.expand(rs["jira.my_open_issues"], c, {})[0]["args"]["jql"]
            self.assertEqual(jql, "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC")
        jql = recipes.expand(rs["jira.my_open_issues"], jira, {"project": "OPS"})[0]["args"]["jql"]
        self.assertIn('AND project = "OPS" ORDER BY', jql)
        steps = recipes.expand(rs["m365.today"], m365, {"start": "S", "end": "E"})
        self.assertEqual(steps, [{"op": "list_events", "args": {"startDateTime": "S", "endDateTime": "E"}}])
        with self.assertRaises(GatewayError) as cm:
            recipes.expand(rs["m365.today"], m365, {"start": "S"})
        self.assertEqual(cm.exception.code, "missing_param")
        self.assertIn("end", cm.exception.message)
        with self.assertRaises(GatewayError) as cm:
            recipes.expand(rs["m365.today"], gh, {"start": "S", "end": "E"})
        self.assertEqual(cm.exception.code, "recipe_not_applicable")

    def _recipe(self, **over):
        r = {"name": "t.x", "summary": "test", "tier": "read",
             "params": {"a": {"description": "required"},
                        "b": {"description": "optional", "default": None},
                        "c": {"description": "defaulted", "default": "dflt"}},
             "profiles": {"generic": {"steps": [{"op": "GET /x", "args": {
                 "s": "A={{a}}{{#b}} B={{b}}{{/b}} C={{c}}",
                 "whole": "{{b}}", "n": "{{a}}", "lst": ["{{c}}"], "fixed": 3}}]}}}
        r.update(over)
        return recipes.validate(r)

    def test_templating(self):
        r, c = self._recipe(), {"profile": "generic"}
        args = recipes.expand(r, c, {"a": 1})[0]["args"]
        self.assertEqual(args, {"s": "A=1 C=dflt", "n": 1, "lst": ["dflt"], "fixed": 3})
        args = recipes.expand(r, c, {"a": "x", "b": "y", "c": "z"})[0]["args"]
        self.assertEqual(args["s"], "A=x B=y C=z")
        self.assertEqual(args["whole"], "y")
        for empty in ("", None, [], {}):
            args = recipes.expand(r, c, {"a": "x", "b": empty})[0]["args"]
            self.assertEqual(args["s"], "A=x C=dflt", repr(empty))
        with self.assertRaises(GatewayError) as cm:
            recipes.expand(r, c, {"b": "y"})
        self.assertEqual(cm.exception.code, "missing_param")
        with self.assertRaises(GatewayError):
            recipes.expand(r, c, {"a": None})

    def test_validation(self):
        bad = [dict(name="Bad Name"), dict(summary=""), dict(tier="admin"), dict(profiles={}),
               dict(profiles={"gitlab": {"steps": [{"op": "GET /x"}]}}),
               dict(profiles={"github": {"steps": [{"op": "no_such_op"}]}}),
               dict(profiles={"generic": {"steps": [{"op": "GET /x", "args": {"q": "{{zz}}"}}]}}),
               dict(profiles={"generic": {"steps": []}})]
        for over in bad:
            with self.subTest(over=over):
                with self.assertRaises(GatewayError) as cm:
                    self._recipe(**over)
                self.assertEqual(cm.exception.code, "recipe_invalid")

    def test_load_names_bad_file_and_later_dirs_override(self):
        with tempfile.TemporaryDirectory() as t:
            d1, d2, d3 = Path(t, "a"), Path(t, "b"), Path(t, "c")
            for d in (d1, d2, d3):
                d.mkdir()
            base = {"name": "x.one", "summary": "first", "tier": "read",
                    "profiles": {"generic": {"steps": [{"op": "GET /x"}]}}}
            (d1 / "one.json").write_text(json.dumps(base))
            (d2 / "one-override.json").write_text(json.dumps(dict(base, summary="second")))
            rs = recipes.load_recipes([d1, d2, Path(t, "missing")])
            self.assertEqual([(r["name"], r["summary"]) for r in rs], [("x.one", "second")])
            (d3 / "broken.json").write_text(json.dumps({"name": "x.two", "tier": "read"}))
            with self.assertRaises(GatewayError) as cm:
                recipes.load_recipes([d3])
            self.assertEqual(cm.exception.code, "recipe_invalid")
            self.assertIn("broken.json", cm.exception.message)
            (d3 / "broken.json").write_text("{not json")
            with self.assertRaises(GatewayError) as cm:
                recipes.load_recipes([d3])
            self.assertIn("broken.json", cm.exception.message)


if __name__ == "__main__":
    unittest.main()
