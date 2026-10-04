"""gt_vault_mcp.py -- gt's vault MCP server (0.20.1, gt sandbox mode; request
2026-10-02-vault-mcp-read-server).

Driven the way Claude Code drives it: JSON-RPC lines over stdio, against a fixture vault in a
throwaway home. The load-bearing assertions are about what is NEVER returned or written:
nothing outside the vault, nothing in a locked folder (even named exactly), no vault file
opened for writing by the server, and -- with gt unlock on -- no gt:vault:read for anything but
the session's registered vault server.
"""
import builtins
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from _harness import IS_WINDOWS, PYTHON, SCRIPTS, TOOLS, Sandbox, load_module, skip_on_windows
from _unlock_fixture import AuthorityCase

SERVER = SCRIPTS / "gt_vault_mcp.py"

sys.path.insert(0, str(SCRIPTS))
import gt_unlock_policy as P     # noqa: E402
import gt_unlockd as D           # noqa: E402


class VaultFixture(Sandbox):
    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        gt = self.vault / "Projects" / "golden-thread"
        (gt / "tools").mkdir(parents=True)
        (gt / "sessions").mkdir()
        for tool in ("gt_task.py", "gt_tasks.py", "gt_session.py"):
            shutil.copy(TOOLS / tool, gt / "tools" / tool)
        (self.vault / "Knowledge").mkdir()
        (self.vault / "index.md").write_text(
            "# Index\n\n- [[Quokka Care]] -- feeding and housing quokkas\n", encoding="utf-8")
        (self.vault / "Knowledge" / "Quokka Care.md").write_text(
            "---\nstatus: current\n---\n# Quokka care\n\nQuokkas eat leaves.\n", encoding="utf-8")
        (self.vault / "Knowledge" / "Old Quokka.md").write_text(
            "# Old\n\nQuokkas eat leaves (old note).\n", encoding="utf-8")
        (self.vault / "Knowledge" / "New Quokka.md").write_text(
            "---\nsupersedes: Old Quokka.md\n---\n# New\n\nQuokkas eat leaves, updated.\n",
            encoding="utf-8")
        sec = self.vault / "Knowledge" / "Secrets"
        sec.mkdir()
        (sec / ".gt-locked").write_text("locked\n")
        (sec / "quokka-passwords.md").write_text("quokka password hunter2\n")
        (sec / "x.md.age").write_bytes(b"age-encryption.org/v1\n...")
        (self.vault / ".obsidian").mkdir()
        (self.vault / ".obsidian" / "quokka.md").write_text("quokka dotfolder\n")
        (self.tmp / "outside.md").write_text("quokka outside the vault\n")
        self.config(vault_path=str(self.vault), sandbox_mode="on")

    def rpc(self, *msgs, env=None):
        lines = "".join(json.dumps(m) + "\n" for m in msgs)
        p = self.run_cmd([PYTHON, SERVER], input=lines, env=env)
        self.assertNotIn("Traceback", p.stderr)
        return {m["id"]: m for m in map(json.loads, p.stdout.splitlines())}

    def call(self, name, args=None, protocol="2025-06-18"):
        out = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": protocol}},
                       {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": name, "arguments": args or {}}})
        r = out[2]["result"]
        self.assertEqual(json.loads(r["content"][0]["text"]), r["structuredContent"])
        return r["structuredContent"]


class ToolList(VaultFixture):
    def tools(self, protocol="2025-06-18"):
        out = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": protocol}},
                       {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        return out[2]["result"]["tools"]

    def test_offered_with_sandbox_mode_and_annotated(self):
        t = {x["name"]: x for x in self.tools()}
        self.assertEqual(set(t), {"vault_list", "vault_read", "vault_search",
                                  "vault_queue_write", "vault_queue_drain"})
        for n in ("vault_list", "vault_read", "vault_search"):
            self.assertIs(t[n]["annotations"]["readOnlyHint"], True)
            self.assertIn("outputSchema", t[n])
        self.assertIs(t["vault_queue_write"]["annotations"]["readOnlyHint"], False)
        self.assertIs(t["vault_queue_write"]["annotations"]["destructiveHint"], False)

    def test_no_output_schema_before_2025_06_18(self):
        for x in self.tools("2025-03-26"):
            self.assertNotIn("outputSchema", x)

    def test_off_lists_nothing_and_refuses_calls(self):
        self.config(vault_path=str(self.vault))                 # sandbox off, vault_mcp auto
        self.assertEqual(self.tools(), [])
        out = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                       {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "vault_read", "arguments": {"path": "index.md"}}})
        self.assertIn("error", out[2])
        self.config(vault_path=str(self.vault), vault_mcp="on")  # explicitly on, no sandbox
        self.assertEqual(len(self.tools()), 5)
        self.config(vault_path=str(self.vault), sandbox_mode="on", vault_mcp="off")
        self.assertEqual(self.tools(), [])


class OffReason(VaultFixture):
    """0.20.1 (WORDING): "off (the vault_mcp setting)" blamed a setting still at its default;
    with vault_mcp auto the cause is sandbox_mode being off, and the message says so."""

    def instructions(self):
        out = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        return out[1]["result"]["instructions"]

    def test_the_off_message_names_the_setting_that_decides(self):
        self.config(vault_path=str(self.vault))                 # sandbox off, vault_mcp auto
        msg = self.instructions()
        self.assertIn("vault_mcp auto follows sandbox_mode, which is off", msg)
        self.config(vault_path=str(self.vault), sandbox_mode="on", vault_mcp="off")
        self.assertIn("vault_mcp is off", self.instructions())


class Reads(VaultFixture):
    def test_read_returns_level_and_supersession(self):
        r = self.call("vault_read", {"path": "Knowledge/Old Quokka.md"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["level"], "4 knowledge")
        self.assertEqual(r["superseded_by"], "Knowledge/New Quokka.md")
        self.assertTrue(r["untrusted"])

    def test_read_is_capped_and_pages(self):
        (self.vault / "Knowledge" / "Big.md").write_text("é" * 5000, encoding="utf-8")
        r = self.call("vault_read", {"path": "Knowledge/Big.md", "max_bytes": 1001})
        self.assertTrue(r["truncated"])
        self.assertLessEqual(len(r["content"].encode("utf-8")), 1001)
        got = r["content"]
        while r["truncated"]:
            r = self.call("vault_read", {"path": "Knowledge/Big.md", "offset": r["next_offset"],
                                         "max_bytes": 4000})
            got += r["content"]
        self.assertEqual(got, "é" * 5000)
        r = self.call("vault_read", {"path": "Knowledge/Big.md", "max_bytes": 10 ** 9})
        self.assertEqual(r.get("error", {}).get("code"), None)
        self.assertLessEqual(len(r["content"].encode("utf-8")), 256 * 1024)

    def test_locked_and_outside_paths_are_never_returned(self):
        for path, code in (("Knowledge/Secrets/quokka-passwords.md", "locked"),
                           ("Knowledge/Secrets/x.md.age", "locked"),
                           ("../outside.md", "bad_path"), ("/etc/hosts", "bad_path"),
                           (".obsidian/quokka.md", "bad_path"),
                           ("Knowledge/./Quokka Care.md", "bad_path")):
            r = self.call("vault_read", {"path": path})
            self.assertFalse(r["ok"], path)
            self.assertEqual(r["error"]["code"], code, (path, r))
            self.assertNotIn("hunter2", json.dumps(r))
        r = self.call("vault_list", {"path": "Knowledge/Secrets"})
        self.assertEqual(r["error"]["code"], "locked")

    @skip_on_windows("creating a symlink needs a privilege a standard Windows account lacks")
    def test_a_symlink_out_of_the_vault_is_refused(self):
        os.symlink(str(self.tmp / "outside.md"), str(self.vault / "Knowledge" / "link.md"))
        os.symlink(str(self.vault / "Knowledge" / "Secrets"), str(self.vault / "Knowledge" / "S2"))
        r = self.call("vault_read", {"path": "Knowledge/link.md"})
        self.assertEqual(r["error"]["code"], "outside_vault")
        r = self.call("vault_read", {"path": "Knowledge/S2/quokka-passwords.md"})
        self.assertEqual(r["error"]["code"], "locked", "a link INTO a locked folder is locked")

    def test_list_shows_locked_folders_without_their_contents(self):
        r = self.call("vault_list", {"path": "Knowledge"})
        entries = {e["path"]: e for e in r["entries"]}
        self.assertTrue(entries["Knowledge/Secrets"]["locked"])
        self.assertNotIn("Knowledge/Secrets/quokka-passwords.md", entries)
        self.assertEqual(entries["Knowledge/Quokka Care.md"]["level"], "4 knowledge")
        root = {e["path"] for e in self.call("vault_list", {})["entries"]}
        self.assertNotIn(".obsidian", root)

    def test_search_is_index_first_lock_aware_and_supersession_ordered(self):
        r = self.call("vault_search", {"query": "quokka leaves"})
        self.assertTrue(r["ok"], r)
        self.assertIn("Quokka Care", r["index"][0]["text"])
        paths = [x["path"] for x in r["results"]]
        self.assertTrue(all("Secrets" not in p and not p.startswith(".") for p in paths), paths)
        self.assertNotIn("hunter2", json.dumps(r))
        states = [x["state"] for x in r["results"]]
        self.assertEqual(states, sorted(states, key=["current", "expired", "superseded"].index))
        old = next(x for x in r["results"] if x["path"] == "Knowledge/Old Quokka.md")
        self.assertEqual(old["superseded_by"], "Knowledge/New Quokka.md")
        self.assertGreaterEqual(r["locked_skipped"], 1)


class NeverWrites(VaultFixture):
    """V1: the server process opens no vault file for writing on any read path."""

    def test_read_paths_open_nothing_for_writing(self):
        os.environ["HOME"], saved = str(self.home), os.environ.get("HOME")
        if IS_WINDOWS:
            saved_up = os.environ.get("USERPROFILE")
            os.environ["USERPROFILE"] = str(self.home)
        try:
            m = load_module(SERVER, "gt_vault_mcp_under_test")
            writes = []
            real_open = builtins.open
            real_osopen = os.open
            vault = os.path.realpath(str(self.vault))

            def spy(file, mode="r", *a, **k):
                if isinstance(file, (str, os.PathLike)) and any(c in mode for c in "wax+") \
                        and os.path.realpath(os.fspath(file)).startswith(vault):
                    writes.append((os.fspath(file), mode))
                return real_open(file, mode, *a, **k)

            def spy_os(path, flags, *a, **k):
                if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT) and \
                        os.path.realpath(os.fspath(path)).startswith(vault):
                    writes.append((os.fspath(path), flags))
                return real_osopen(path, flags, *a, **k)
            builtins.open, os.open = spy, spy_os
            try:
                srv = m.Server(vault=str(self.vault), out=io.StringIO(), offered=True)
                for name, args in (("vault_list", {}), ("vault_list", {"path": "Knowledge"}),
                                   ("vault_read", {"path": "Knowledge/Quokka Care.md"}),
                                   ("vault_search", {"query": "quokka"})):
                    r = srv.tools_call(name, args)
                    self.assertFalse(r["isError"], r)
            finally:
                builtins.open, os.open = real_open, real_osopen
            self.assertEqual(writes, [])
        finally:
            if saved is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved
            if IS_WINDOWS:
                if saved_up is None:
                    os.environ.pop("USERPROFILE", None)
                else:
                    os.environ["USERPROFILE"] = saved_up


class QueueWrites(VaultFixture):
    def test_a_create_is_queued_and_applied_by_the_broker(self):
        r = self.call("vault_queue_write", {"path": "Knowledge/Wombat.md", "op": "create",
                                            "content": "# Wombat\n"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["decision"], "apply")
        self.assertEqual((self.vault / "Knowledge" / "Wombat.md").read_text(), "# Wombat\n")
        log = list((self.vault / "Projects" / "golden-thread" / "spool" / "broker").glob("log-*"))
        self.assertTrue(log, "the broker logged the decision")

    def test_refused_targets_are_refused_with_the_queue_validation(self):
        for path in ("core-rules/core_x.md", "log.md", "Sources/s.md", "../x.md",
                     "Knowledge/Secrets/new.md"):
            r = self.call("vault_queue_write", {"path": path, "op": "create", "content": "x\n"})
            self.assertFalse(r["ok"], (path, r))
        self.assertFalse((self.vault / "core-rules").exists())

    def test_a_design_write_escalates_to_the_owner(self):
        a = self.vault / "Projects" / "alpha"
        a.mkdir(parents=True)
        (a / "README.md").write_text("# alpha\n\n## Tasks\n")
        (a / "design.md").write_text("# d\n\n## A\n\nold\n")
        r = self.call("vault_queue_write", {"path": "Projects/alpha/design.md",
                                            "op": "replace-section", "section": "A",
                                            "content": "new\n"})
        self.assertEqual(r["decision"], "escalate", r)
        self.assertIn("old", (a / "design.md").read_text())

    def test_drain_picks_up_the_inbox(self):
        inbox = self.home / ".gt-inbox" / "queue"
        inbox.mkdir(parents=True)
        req = {"schema": 1, "id": "i1", "submitted": "2026-10-03T20:00:00+00:00",
               "session": "s", "origin": "session", "path": "Knowledge/FromShell.md",
               "op": "create", "section": None, "content": "# From the shell\n", "key": None,
               "target_existed": False, "base_sha256": None, "hint": None}
        (inbox / "i1.json").write_text(json.dumps({"gt_inbox": 1, "vault":
                                                   os.path.realpath(str(self.vault)),
                                                   "request": req}))
        r = self.call("vault_queue_drain")
        self.assertTrue(r["ok"], r)
        self.assertEqual([x["decision"] for x in r["results"]], ["apply"])
        self.assertTrue((self.vault / "Knowledge" / "FromShell.md").exists())


class VaultSeat(AuthorityCase):
    """gt unlock on: gt:vault:read is served only to the session's registered vault server."""

    def setUp(self):
        super().setUp()
        self.standard()
        self.auth.vault_shim_ok = lambda peer: True      # the real file check: test below
        self.vshim = self.child()
        self.assertIn("result", self.call(self.vshim, "register_shim", {"role": "vault"}))

    def check(self, proc, scope):
        return self.call(proc, "check", {"scope": scope})["result"]

    def test_only_the_vault_seat_reads(self):
        v = self.check(self.vshim, "gt:vault:read")
        self.assertTrue(v["allowed"], v)                  # open: read_without_unlock
        bash = self.child()
        self.assertEqual(self.check(bash, "gt:vault:read")["code"], "mcp_only")
        lotr = self.child()
        self.assertIn("result", self.call(lotr, "register_shim"))
        self.assertEqual(self.check(lotr, "gt:vault:read")["code"], "mcp_only")

    def test_the_vault_seat_gets_no_lotr_or_secrets(self):
        self.assertEqual(self.check(self.vshim, "lotr:github:read")["code"], "mcp_only")
        self.assertEqual(self.check(self.vshim, "gt:secrets")["code"], "mcp_only")

    def test_without_read_without_unlock_the_seat_needs_an_unlock(self):
        self.set_policy(read_without_unlock=False)
        v = self.check(self.vshim, "gt:vault:read")
        self.assertEqual(v["code"], "locked")
        self.assertIn("result", self.call(self.vshim, "unlock", {"tty": True},
                                          answers=[self.code()]))
        self.assertTrue(self.check(self.vshim, "gt:vault:read")["allowed"])

    def test_one_seat_per_session_and_a_replacement_inherits_no_grant(self):
        self.set_policy(read_without_unlock=False)
        self.call(self.vshim, "unlock", {"tty": True}, answers=[self.code()])
        other = self.child()
        self.assertEqual(self.call(other, "register_shim", {"role": "vault"})["error"]["code"],
                         "seat_taken")
        self.vshim.kill()
        self.vshim.wait(5)
        self.assertIn("result", self.call(other, "register_shim", {"role": "vault"}))
        self.auth.tick()
        self.assertEqual(self.check(other, "gt:vault:read")["code"], "locked")

    def test_the_real_seat_check_refuses_a_look_alike(self):
        self.auth.vault_shim_ok = D.is_vault_shim
        imp = self.child()
        r = self.call(imp, "register_shim", {"role": "vault"})
        self.assertEqual(r["error"]["code"], "not_a_shim")

    def test_policy_level(self):
        eff = P.Effective(P.merge({"enabled": True}, None), [], True)
        self.assertEqual(eff.level("gt:vault:read"), "open")
        eff = P.Effective(P.merge({"enabled": True, "read_without_unlock": False}, None), [], True)
        self.assertEqual(eff.level("gt:vault:read"), "unlocked")


if __name__ == "__main__":
    unittest.main()
