"""The 2026-10-04 independent re-review of gt 0.20.x: the queue inbox and the vault MCP server.

Each test is one bypass the review demonstrated, made deterministic:

  H1  the broker lstat()ed an inbox file and then re-opened it BY NAME, so a writer in Claude's
      sandbox could swap the file for a symlink (or swap the inbox folder itself) between the
      check and the open, and the reject path copied whatever that pointed at -- the TOTP seed,
      in the review's harness -- into spool/broker/rejected/ inside the vault, where vault_read
      served it. A hard link to a read-denied file needed no race at all. The swap is injected
      exactly between check and open: through gt_broker._INBOX_RACE_HOOK where the fixed code
      offers one, and by wrapping os.lstat / os.path.islink (the checks the old code made) --
      whichever code runs, the swap lands in its window, never by luck.
  L3  every rejected inbox file was copied into the vault, without limit.
  M4  an inbox request, or a vault_queue_write call, named its own session and origin, and so
      wrote through a live claim holder's claim (Core rule 1).
  L2  vault_search followed a symlink out of the vault and returned the target's text.
  --  an inbox request id became a queue file name unchecked ("../../x" climbed out).

The load-bearing assertions are about what is NOT in the vault afterwards.
"""
import io
import json
import os
import shutil
import stat
import sys
import unittest

from _harness import IS_WINDOWS, PYTHON, SCRIPTS, TOOLS, Sandbox, skip_on_windows

BROKER = SCRIPTS / "gt_broker.py"
SERVER = SCRIPTS / "gt_vault_mcp.py"
MARKER = "quokka-private-marker-7f3e"          # stands in for a read-denied file's content


class InboxCase(Sandbox):
    """A vault, an inbox in the sandbox home, and gt_broker imported with HOME pointing there."""

    def setUp(self):
        super().setUp()
        self._saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE",
                                                      "CLAUDE_CODE_SESSION_ID",
                                                      "CLAUDE_SESSION_ID", "GT_SESSION_ID")}
        os.environ["HOME"] = str(self.home)
        if IS_WINDOWS:
            os.environ["USERPROFILE"] = str(self.home)
        for k in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "GT_SESSION_ID"):
            os.environ.pop(k, None)
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import gt_broker
        import gt_write_queue
        self.B, self.wq = gt_broker, gt_write_queue
        self.vault = self.tmp / "vault"
        gt = self.vault / "Projects" / "golden-thread"
        (gt / "tools").mkdir(parents=True)
        (gt / "sessions").mkdir()
        (gt / "README.md").write_text("# golden-thread\n\n## Tasks\n")
        for tool in ("gt_task.py", "gt_tasks.py", "gt_session.py"):
            shutil.copy(TOOLS / tool, gt / "tools" / tool)
        (self.vault / "Knowledge").mkdir()
        (self.vault / "Knowledge" / "Claimed.md").write_text("# Claimed\n\n## Notes\n\n- one\n")
        self.inbox = self.home / ".gt-inbox" / "queue"
        self.inbox.mkdir(parents=True)
        self.private = self.tmp / "private"           # what the sandbox denies reading
        self.private.mkdir()
        self.secret = self.private / "totp.seed"
        self.secret.write_text(MARKER + "\n")
        (self.private / "state.json").write_text(json.dumps({"policy_approved": MARKER}))
        self.config(vault_path=str(self.vault), sandbox_mode="on")
        self._lstat, self._islink = os.lstat, os.path.islink

    def tearDown(self):
        os.lstat, os.path.islink = self._lstat, self._islink
        self.B._INBOX_RACE_HOOK = None
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    # -- fixtures
    def req(self, name, path="Knowledge/New.md", op="create", content="# New\n", **over):
        r = {"schema": 1, "id": name, "submitted": "2026-10-04T01:00:00.000000+00:00",
             "session": "s-inbox", "origin": "session", "path": path, "op": op,
             "section": None, "content": content, "key": None, "target_existed": False,
             "base_sha256": None, "hint": None}
        r.update(over)
        return r

    def put(self, name, req=None, raw=None):
        body = raw if raw is not None else json.dumps(
            {"gt_inbox": 1, "vault": os.path.realpath(str(self.vault)), "request": req or {}})
        (self.inbox / (name + ".json")).write_text(body, encoding="utf-8")
        return self.inbox / (name + ".json")

    def leaked(self):
        """-> vault files holding the marker (followed symlinks excluded: the L2 tests plant
        links on purpose, and a link is not a copy)."""
        hits = []
        for root, _dirs, files in os.walk(str(self.vault)):
            for f in files:
                p = os.path.join(root, f)
                if os.path.islink(p):
                    continue
                with open(p, "rb") as fh:
                    if MARKER.encode() in fh.read():
                        hits.append(os.path.relpath(p, str(self.vault)))
        return hits

    def assertNoLeak(self):
        self.assertEqual(self.leaked(), [], "a read-denied file's bytes reached the vault")
        self.assertEqual(self.secret.read_text(), MARKER + "\n", "the target is untouched")

    def swap_between_check_and_open(self, name, swap):
        """Run `swap` once, in the window between the broker's check of inbox entry `name` and
        its open: at the fixed code's hook, or straight after the old code's os.lstat."""
        done = []

        def once():
            if not done:
                done.append(1)
                swap()

        def hook(stage, n):
            if stage == "file" and n == name:
                once()
        self.B._INBOX_RACE_HOOK = hook
        real = self._lstat

        def lstat(p, *a, **k):
            st = real(p, *a, **k)
            if os.path.basename(os.fspath(p)) == name and stat.S_ISREG(st.st_mode):
                once()
            return st
        os.lstat = lstat
        return done


@skip_on_windows("creating a symlink needs a privilege a standard Windows account lacks")
class H1SymlinkSwap(InboxCase):
    def test_a_file_swapped_for_a_symlink_after_the_check_is_never_read(self):
        f = self.put("x", self.req("x", content=""))          # empty content: the reject path

        def swap():
            os.unlink(str(f))
            os.symlink(str(self.secret), str(f))
        done = self.swap_between_check_and_open("x.json", swap)
        rows = self.B.pickup_inbox(self.vault)
        self.assertTrue(done, "the swap ran in the check-to-open window")
        self.assertNoLeak()
        self.assertFalse(os.path.lexists(str(f)), "the link is removed")
        self.assertEqual(len(rows), 1)
        self.assertIn("never read", rows[0]["reason"])

    def test_the_inbox_folder_swapped_for_a_symlink_after_the_check_is_never_read(self):
        self.put("ok", self.req("ok", content=""))
        q = str(self.inbox)
        done = []

        def swap():
            if not done:
                done.append(1)
                os.rename(q, q + ".real")
                os.symlink(str(self.private), q)

        def hook(stage, n):
            if stage == "dir":
                swap()
        self.B._INBOX_RACE_HOOK = hook
        real = self._islink

        def islink(p):
            r = real(p)
            if os.fspath(p) == q and not r:
                swap()
            return r
        os.path.islink = islink
        self.B.pickup_inbox(self.vault)
        self.assertTrue(done)
        self.assertNoLeak()
        self.assertTrue((self.private / "state.json").exists(), "nothing in the target is touched")

    def test_a_symlinked_gt_inbox_parent_is_not_read(self):
        shutil.rmtree(str(self.home / ".gt-inbox"))
        (self.private / "queue").mkdir()
        (self.private / "queue" / "s.json").write_text(MARKER)
        os.symlink(str(self.private), str(self.home / ".gt-inbox"))
        self.assertEqual(self.B.pickup_inbox(self.vault), [])
        self.assertNoLeak()
        self.assertTrue((self.private / "queue" / "s.json").exists())


class H1HardLink(InboxCase):
    def test_a_hard_link_to_a_private_file_is_refused_unread(self):
        link = self.inbox / "h.json"
        try:
            os.link(str(self.secret), str(link))
        except (OSError, AttributeError) as exc:
            self.skipTest("no hard links here (%s)" % exc)
        rows = self.B.pickup_inbox(self.vault)
        self.assertNoLeak()
        self.assertFalse(link.exists(), "the extra link is removed")
        self.assertEqual(os.stat(str(self.secret)).st_nlink, 1)
        self.assertEqual([r["decision"] for r in rows], ["reject"])
        self.assertIn("never read", rows[0]["reason"])


@unittest.skipUnless(hasattr(os, "mkfifo"), "no FIFOs on this platform")
class H1Fifo(InboxCase):
    def test_a_file_swapped_for_a_fifo_does_not_block_the_drain(self):
        """Not run against the pre-fix code: there the read blocks forever -- the review's DoS."""
        f = self.put("p", self.req("p", content=""))

        def swap():
            os.unlink(str(f))
            os.mkfifo(str(f))
        self.swap_between_check_and_open("p.json", swap)
        rows = self.B.pickup_inbox(self.vault)
        self.assertEqual(len(rows), 1)
        self.assertIn("never read", rows[0]["reason"])


class L3RejectsAreCapped(InboxCase):
    def test_rejected_inbox_copies_in_the_vault_are_capped(self):
        keep = getattr(self.B, "INBOX_REJECT_KEEP", 50)
        for i in range(keep + 5):
            self.put("g%03d" % i, raw="{not json %d" % i)
        rows = self.B.pickup_inbox(self.vault)
        self.assertEqual(len(rows), keep + 5)
        rej = self.vault / "Projects" / "golden-thread" / "spool" / "broker" / "rejected"
        self.assertEqual(len(list(rej.glob("inbox-*"))), keep)
        self.assertEqual(list(self.inbox.glob("*.json")), [], "every reject left the inbox")
        self.assertEqual(sum("not kept" in r["reason"] for r in rows), 5)


class RequestIdStaysInTheQueue(InboxCase):
    def test_an_id_that_climbs_out_of_the_queue_is_rejected(self):
        self.put("t", self.req("../../../../Knowledge/Escaped"))
        rows = self.B.pickup_inbox(self.vault)
        self.assertEqual([r["decision"] for r in rows], ["reject"])
        self.assertFalse((self.vault / "Knowledge" / "Escaped.json").exists())
        self.assertFalse(any(p.name.endswith(".json")
                             for p in (self.vault / "Knowledge").iterdir()))


class M4InboxCannotNameASession(InboxCase):
    def register(self, sid, files):
        p = self.py(self.vault / "Projects/golden-thread/tools/gt_session.py",
                    "--vault", self.vault, "--id", sid, "register", "--task", "t",
                    "--files", *files, env={"CLAUDE_PID": str(os.getpid())})
        self.assertOk(p)

    def drain(self, env=None):
        p = self.py(BROKER, "drain", "--vault", self.vault, "--json", env=env)
        return json.loads(p.stdout)["results"]

    def test_an_inbox_request_naming_the_claim_holder_is_held(self):
        self.register("holder-live", ["Knowledge/Claimed.md"])
        self.put("c1", self.req("c1", path="Knowledge/Claimed.md", op="append",
                                content="- from the sandbox\n", session="holder-live"))
        rows = self.drain()
        self.assertEqual([r["decision"] for r in rows], ["held"], rows)
        self.assertEqual(rows[0]["session"], "unknown-inbox")
        self.assertEqual(rows[0]["origin"], "inbox")
        self.assertNotIn("from the sandbox", (self.vault / "Knowledge" / "Claimed.md").read_text())

    def test_not_even_the_draining_sessions_own_claim_covers_an_inbox_request(self):
        self.register("holder-live", ["Knowledge/Claimed.md"])
        self.put("c2", self.req("c2", path="Knowledge/Claimed.md", op="append",
                                content="- from the sandbox\n", session="holder-live"))
        rows = self.drain(env={"CLAUDE_CODE_SESSION_ID": "holder-live"})
        self.assertEqual([r["decision"] for r in rows], ["held"], rows)

    def test_unclaimed_inbox_requests_still_apply_and_farm_stays_strict(self):
        self.put("a1", self.req("a1", path="Knowledge/Claimed.md", op="append",
                                content="- applied\n", section="Notes"))
        self.put("f1", self.req("f1", path="Knowledge/Claimed.md", op="append",
                                content="- farmed\n", section="Notes", origin="farm"))
        rows = {r["request"]: r for r in self.drain()}
        self.assertEqual(rows["a1"]["decision"], "apply", rows)
        self.assertEqual(rows["a1"]["origin"], "inbox")
        self.assertEqual(rows["f1"]["decision"], "escalate", rows)
        text = (self.vault / "Knowledge" / "Claimed.md").read_text()
        self.assertIn("- applied", text)
        self.assertNotIn("farmed", text)


class M4McpCannotNameASession(InboxCase):
    def call(self, args, session):
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "vault_queue_write", "arguments": args}}]
        p = self.run_cmd([PYTHON, SERVER], input="".join(json.dumps(m) + "\n" for m in msgs),
                         env={"CLAUDE_CODE_SESSION_ID": session})
        self.assertNotIn("Traceback", p.stderr)
        out = {m["id"]: m for m in map(json.loads, p.stdout.splitlines())}
        return out[2]["result"]["structuredContent"]

    def register(self, sid, files):
        M4InboxCannotNameASession.register(self, sid, files)

    def test_a_tool_call_naming_the_claim_holder_does_not_write_through_its_claim(self):
        self.register("holder-live", ["Knowledge/Claimed.md"])
        r = self.call({"path": "Knowledge/Claimed.md", "op": "append", "content": "- via mcp\n",
                       "session": "holder-live"}, session="srv-other")
        self.assertEqual(r["decision"], "held", r)
        self.assertNotIn("via mcp", (self.vault / "Knowledge" / "Claimed.md").read_text())

    def test_the_holders_own_server_still_writes(self):
        self.register("holder-live", ["Knowledge/Claimed.md"])
        r = self.call({"path": "Knowledge/Claimed.md", "op": "append", "content": "- mine\n"},
                      session="holder-live")
        self.assertEqual(r["decision"], "apply", r)

    def test_the_session_argument_is_no_longer_advertised(self):
        import gt_vault_mcp as M
        t = {x["name"]: x for x in M.TOOLS}
        self.assertNotIn("session", t["vault_queue_write"]["inputSchema"]["properties"])


@skip_on_windows("creating a symlink needs a privilege a standard Windows account lacks")
class L2SearchStaysInTheVault(InboxCase):
    def test_a_symlink_out_of_the_vault_is_not_a_search_result(self):
        (self.vault / "index.md").write_text("# Index\n")
        (self.private / "page.md").write_text("# Wombat\n\nwombat wombat %s\n" % MARKER)
        os.symlink(str(self.private / "page.md"), str(self.vault / "Knowledge" / "Wombat.md"))
        os.symlink(str(self.private), str(self.vault / "Knowledge" / "Out"))
        (self.vault / "Knowledge" / "Real.md").write_text("# Real\n\nwombat inside\n")
        import gt_vault_mcp as M
        srv = M.Server(vault=str(self.vault), out=io.StringIO(), offered=True)
        r = srv.tools_call("vault_search", {"query": "wombat"})["structuredContent"]
        paths = [x["path"] for x in r["results"]]
        self.assertIn("Knowledge/Real.md", paths)
        self.assertNotIn("Knowledge/Wombat.md", paths)
        self.assertNotIn(MARKER, json.dumps(r))

    def test_a_symlinked_index_md_is_not_read(self):
        (self.private / "idx.md").write_text("- wombat %s\n" % MARKER)
        os.symlink(str(self.private / "idx.md"), str(self.vault / "index.md"))
        import gt_vault_mcp as M
        srv = M.Server(vault=str(self.vault), out=io.StringIO(), offered=True)
        r = srv.tools_call("vault_search", {"query": "wombat"})["structuredContent"]
        self.assertNotIn(MARKER, json.dumps(r))


@unittest.skipUnless(os.environ.get("GT_SRT"), "live sandbox proof: set GT_SRT to the path of "
                     "@anthropic-ai/sandbox-runtime's srt (the runtime Claude Code builds on)")
class H1LiveRace(InboxCase):
    """The review's race, for real: a writer INSIDE the sandbox runtime, with the filesystem
    lists gt generates, swaps an inbox file for a symlink to the unlock home's TOTP seed (which
    the sandbox denies it reading) and tries a hard link to it, as fast as it can, while the
    broker drains outside. Probabilistic by nature -- the deterministic proof is H1SymlinkSwap;
    this one shows the real sandbox and the real broker together leak nothing."""

    RACER = r'''
import json, os, sys, time
q, secret, vault = sys.argv[1], sys.argv[2], sys.argv[3]
benign = json.dumps({"gt_inbox": 1, "vault": vault, "request": {}})
p, reg, lnk = os.path.join(q, "x.json"), os.path.join(q, ".r"), os.path.join(q, ".l")
try:
    os.link(secret, os.path.join(q, "h.json")); print("hardlink-made")
except OSError as e:
    print("hardlink-refused", e.errno)
end, n = time.time() + 4, 0
while time.time() < end:
    with open(reg, "w") as fh:
        fh.write(benign)
    os.replace(reg, p)
    try:
        os.symlink(secret, lnk)
    except FileExistsError:
        pass
    os.replace(lnk, p)
    n += 1
print("swaps", n)
'''

    def test_a_sandboxed_racer_gets_nothing_into_the_vault(self):
        import subprocess
        import time
        sys.path.insert(0, str(SCRIPTS))
        from _harness import load_module
        gs = load_module(SCRIPTS / "gt_sandbox.py", "gt_sandbox_live_h1")
        seed = self.home / ".claude" / "golden-thread" / "unlock" / "totp.seed"
        seed.parent.mkdir(parents=True)
        seed.write_text(MARKER + "\n")
        p = gs.plan()
        t = self.tmp / "t"
        t.mkdir()
        cfg = {"network": {"allowedDomains": [], "deniedDomains": []},
               "filesystem": {"denyRead": p["lists"]["sandbox.filesystem.denyRead"],
                              "allowWrite": [str(t)] + p["lists"]["sandbox.filesystem.allowWrite"],
                              "denyWrite": p["lists"]["sandbox.filesystem.denyWrite"]}}
        f = self.tmp / "srt.json"
        f.write_text(json.dumps(cfg))
        racer = t / "racer.py"
        racer.write_text(self.RACER)
        r = subprocess.run([os.environ["GT_SRT"], "--settings", str(f), "cat", str(seed)],
                           capture_output=True, text=True, timeout=60, cwd=str(t))
        self.assertNotIn(MARKER, r.stdout, "the sandbox itself must deny the seed")
        proc = subprocess.Popen([os.environ["GT_SRT"], "--settings", str(f), sys.executable,
                                 str(racer), str(self.inbox), str(seed),
                                 os.path.realpath(str(self.vault))],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                cwd=str(t))
        drains, deadline = 0, time.time() + 60
        try:
            while proc.poll() is None and time.time() < deadline:
                self.B.pickup_inbox(self.vault)
                drains += 1
        finally:
            if proc.poll() is None:
                proc.kill()
            out, err = proc.communicate(timeout=30)
        self.B.pickup_inbox(self.vault)
        sys.stderr.write("\nH1-LIVE drains=%d racer=%s\n" % (drains, " ".join(out.split())))
        self.assertIn("swaps", out, err)
        self.assertGreater(drains, 0)
        self.assertEqual(self.leaked(), [], "a sandbox-denied file reached the vault")
        self.assertEqual(seed.read_text(), MARKER + "\n")


if __name__ == "__main__":
    unittest.main()
