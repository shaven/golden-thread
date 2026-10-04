# Changelog

Releases of the `gt` and `gt-wiki` plugins. `install.sh` always installs the newest
version directory present, so upgrading is `bash install.sh` and a restart of Claude Code.

Announcements for recent releases are posted under
[Discussions → Announcements](../../discussions/categories/announcements).

Entries from 0.9.13 onward are written from the change itself. Earlier entries are the
release's own summary line, kept short rather than reconstructed after the fact.

---

## gt 0.20.0 — unreleased

> **There is no published 0.19.3.** The Windows completion was built as 0.19.3 and never
> released; the owner moved it into 0.20.0 together with the unlock layer (2026-10-03), so the
> 0.19.3 directories were re-cut as 0.20.0, as 0.19.0 was re-cut as 0.19.1. The rollback target
> is 0.19.2, the last release published.

### gt unlock: agents need your presence for what matters (off by default)

(Owner, 2026-10-03: "I want to be able to sleep at night that I am not putting out something
that can be hacked easily"; minimum bar L2.) A new gt-core authority, `gt_unlockd.py`, decides
which processes may use LOTR connections, credentials, publishing and gt's own guards, after
you prove presence. **Unlock ships off**; with it off every hook's path is unchanged. The full
account of what it stops and what it does not is the new `SECURITY.md` (with HTML and PDF):
*"Unlock proves a person was present and limits what agents can do on their own. It is not
anti-malware. If something already runs as you, it can wait for you to unlock."*

- **The authority** (`gt_unlockd.py`, `gt_unlockd_methods.py`, client `gt_unlock_client.py`,
  CLI `gt_unlock.py {enroll,unenroll,recovery,unlock,lock,status,check,run,register,revoke,
  policy,seal,secret,git-credential,aws-credential,audit,daemon,verify}`). One per user, started
  on demand, over `gt_ipc.py`: a 600 unix socket (macOS/Linux) or a named pipe whose DACL holds
  only the user's SID, `PIPE_REJECT_REMOTE_CLIENTS` and `FILE_FLAG_FIRST_PIPE_INSTANCE`
  (Windows). Every caller is identified by the kernel (macOS audit token with pid version,
  Linux `SO_PEERCRED` + start time, Windows pipe client pid + creation time + SID), never by
  what it says. Grants live in memory, bound to a session, and are revoked on TTL (8 h), idle
  (15 min), MCP shim exit, session end, screen lock, sleep, clock rollback, `lock` and restart.
  Every decision is audited with its grant id.
- **Factors.** TOTP (RFC 6238, stdlib; replay refused; lockouts of 1/5/15 min persisted; codes
  typed into a dialog the authority raises, or the asking terminal — never a tool argument;
  terminal QR code from the vendored MIT `qrcodegen.py`, licence kept beside it); **Touch ID**
  (`gt-presence.swift`, built by `install.sh` with the Xcode tools and ad-hoc signed;
  Secure Enclave P-256 keys gated by biometry; gt verifies the ES256 signature itself in pure
  Python, `gt_unlock_crypto.py`); **Windows Hello** (`gt_unlock_hello.ps1` via PowerShell 5.1
  KeyCredentialManager, TPM-backed RSA; strict RS256 verification, full-encoding compare);
  **Microsoft Entra ID** (`gt_unlock_sso.py`: PKCE + loopback, `prompt=login`, nonce bound to
  the request, pure-Python RS256 with pinned algorithms, issuer/tenant/audience/time/auth_time
  checks); ten one-time **recovery codes** (scrypt, or PBKDF2 where the Python lacks scrypt).
  Defaults when on: TOTP + Touch ID (macOS), TOTP + Hello (Windows), TOTP (Linux).
- **Policy** (`gt_unlock_policy.py`): yours in `~/.claude/golden-thread/unlock/policy.json`,
  changed only through the authority with fresh factors; an administrator floor
  (`/Library/Application Support/gt/`, `%ProgramData%\gt\` or `HKLM\SOFTWARE\Policies\gt`,
  `/etc/gt/`) that only tightens. Fail closed when on: an unverifiable admin file, more factors
  required than usable (never lowered), an unreadable policy, or one edited behind gt's back
  (an `unlock_on` marker keeps a file edited to "disabled" from switching anything off).
- **Sealed credentials** (`sealed:` refs, `gt_unlock_seal.py`): encrypted under the Secure
  Enclave (CryptoKit ECIES/AES-GCM in the helper) or a Hello-derived DPAPI entropy; opened only
  under a grant; `seal migrate` moves `file:`/`store:`/`keychain:` refs in. **Bring-your-own
  stores** (`gt_unlock_brokers.py`): `sops:`, `op:`, `bw:`, `vault:`, `wincred:`, each labelled
  with its real level. A git credential helper and AWS `credential_process` release tokens
  only under a `gt:publish` grant.
- **Gating across gt:** edits to `~/.claude/settings.json`, `vault-config.json` and
  `~/.claude/golden-thread/` need a fresh confirmation (`guard_protected_paths`, checked before
  the `protected_paths` switch); `gt_settings.py set` of a guard (`protected_paths`,
  `test_gate`, `foreign_checkout_guard`, `component_updates`, `commit_checks`, `addon_fixes`,
  `unlock`) likewise; hooks fail CLOSED for these when the authority is down — the one
  deliberate exception to gt's fail-open hooks, and only when unlock is on. New setting
  `unlock` (off). Unattended jobs get only allow-listed narrow read scopes, never a prompt.
- **Folder locks** (`gt_lock.py add|open|restore|status`): per-file age encryption for chosen
  vault folders, a `.gt-locked` note, readers (lint, tasks, recall, vault hints) treat a locked
  folder as absent; `core-rules/` and `global-memory/` can never be locked.
- **Self-check:** `gt_unlock.py verify` and doctor rows `unlock` and `security` print the level
  this machine runs at (off / L1 / L2; L3 is not shipped) and each check PASS, FAIL or
  NOT-CHECKED — never "secure". `install.sh` ends by saying unlock is off and how to turn it on.
- **gt-lotr 0.3.0** consumes it: `engine.call` checks `lotr:<connection>:<tier>` for local
  callers, the grant id is in every audit line, the `mcp_only` door refuses LOTR to the
  assistant's shell, biometric consent is an option (`consent_window_s`), the new secret
  schemes resolve through the authority, hub enrol/revoke need a step-up, and LOTR runs on
  native Windows over the named pipe. gt-usage 0.1.6, gt-visualize 0.4.4 and gt-wiki 0.2.7 only
  move `requires_gt` to `>=0.20.0,<0.21.0`.
- **Tests:** `test_gt_ipc`, `test_unlock_core`, `test_unlock_redteam` (every bypass route in
  the design, each attempted and refused), `test_unlock_cli`, `test_unlock_touchid` (a real
  Secure Enclave signature checked by the Python verifier), `test_unlock_hello`,
  `test_unlock_sso` (a local fake identity provider), `test_unlock_brokers`, `test_gt_lock`,
  `test_lotr_unlock`. The 31 Windows tests that faked a program with a `#!` script now run on
  Windows (`tests/_fakes.py`).
- **Not yet proven live** (owner steps): a Touch ID prompt from the helper, a Developer ID
  signed + notarized helper, Windows Hello on a machine with a PIN, an Entra app registration.

### gt unlock after the independent security review: fixed, and described honestly

An independent, fresh-context review of the unlock layer (a release gate, 2026-10-03) drove a
real authority over a real socket and reproduced seven bypasses by a process running as you —
the assistant's own shell — plus medium and low findings. The guide's rows "assistant via Bash:
Stopped (L1, L2)" overstated the code. Owner decision (2026-10-03 18:37): fix every logic bug,
make sealed secrets need a fingerprint per open or per short window with no silent cache, and
rewrite the security guide honestly; L3 is not in this release. Each exploit harness is now a
regression test that failed on the reviewed commit (`fe571f2`) and passes now:
`tests/test_unlock_review_regressions.py`, `tests/test_lotr_review_regressions.py`.

- **Admin floor (F2, a pure logic bug).** `merge()` tightened only identical keys, so a user
  `lotr:github:write: open` beat an admin `lotr:*:write: step_up`. Each scope's level is now the
  stricter of the user's and the admin's, each found by its own most specific pattern; the
  admin's patterns travel in the merged policy (`admin_floor_scopes`, never read from a user
  file).
- **Secrets bound to the requester (F4).** A shell child read a sealed value on the shim's
  session grant, and the opened-value cache was global. Under `mcp_only` the broker
  (`gt:secrets`) now serves only the shim (or gt-lotr's installed `lotrd.py` for it). Every open
  of a `sealed:` ref needs a fresh Touch ID / Hello; `secrets_window_s` (new, default 0, at most
  900; `gt_unlock.py policy secrets-window S`) allows one touch per window **per process and
  grant**, never longer, never shared.
- **Consumers and the shim by real path (F5, F7).** `is_lotr_daemon` was a file-name check (a
  Bash-written `/tmp/x/lotrd.py` passed, and a shipped test asserted it). The credential
  consumer and the shim seat are now the INSTALLED gt-lotr `lotrd.py` / `lotr_mcp.py`, by the
  real path of the file the process runs, from `installed_plugins.json`. A shim replacing an
  earlier one starts with no grant (killing the shim and registering an impostor before the
  next tick inherited it); the real shim re-registers after an authority restart.
- **Fake authority (F1).** `stop` needed nothing and clients never checked the server, so Bash
  could stop the daemon and serve "allowed" itself. Clients now identify the server from the
  kernel (macOS LOCAL_PEERTOKEN, Linux SO_PEERCRED, Windows GetNamedPipeServerProcessId; the
  Windows command line now comes from NtQueryInformationProcess, not a PowerShell call) and
  refuse anything but the installed `gt_unlockd.py` (`server_unverified`, never allowed). `stop`
  needs a fresh factor while unlock is on; `lock` still needs nothing.
- **Policy approval (F3).** The approval was a sha256 in `state.json`; rewriting two files
  opened everything, and deleting them turned unlock off. Where a platform factor is enrolled
  the approval is now its Touch ID / Hello signature over the policy hash, made during the
  step-up and verified on every load; unsigned, invalid, or signed by a key the enrolment says
  needs no finger ⇒ the most restrictive policy. TOTP-only machines keep a hash, labelled L1.
  `unlock-on` markers (inside the unlock home and beside it) keep deleted policy files reading
  as "on, locked"; the running authority also remembers it was on.
- **Orphans (F6).** A double-forked process became a "terminal" and got LOTR reads without
  unlock. A terminal-kind caller with no controlling terminal no longer gets
  `read_without_unlock`.
- **Consent (M1).** Only gt-lotr's installed `lotrd.py` may ask for consent, and the authority
  composes the prompt text and op hash from the operation; a shell child could name the shim,
  write its own text and open a consent window. Every prompt is composed by the authority; a
  caller's reason appears only as a quoted, unverified claim.
- **Hub (M2, M3).** An HTTP client is never the local caller (`local` is refused as a client id
  and on the HTTP path); the hub's live registry reload requires a 600 file owned by the user; a
  hub client's brokered ref is asked as the unattended job `lotr-hub:<client>`, so only an
  allow-list entry opens it (it resolved under lotrd's own identity).
- **Touch ID helper swap.** `install.sh` kept an existing `gt-presence` unhashed. The built
  binary's sha256 (and a release binary's Developer ID requirement) is recorded in
  `gt-presence.install.json` and checked before every use; mismatch ⇒ Touch ID unavailable.
  `gt_unlock_touchid.py record|verify` are new.
- **Low.** With unlock off a brokered ref is refused (`unlock_off`) instead of starting the
  authority; an audit line that cannot be written refuses write / consent / secret releases (in
  the authority, and in LOTR before a write runs); `localize_mcp` writes through `mkstemp` +
  atomic replace; `pipeline-stage.js` accepts only the exact `<run_dir>/prompts/<stage>/<unit>.md`
  (`workflow-args` now prints `run_dir`); `agents/` and `workflows/` are in MANIFEST.json and the
  drift check; every stage prompt names its tool limit, which on the fallback route (the Agent
  tool, used when the installed definition is missing or stale) is advisory — the agent there
  has every tool.
- **The guide.** `SECURITY.md` §1, §2 and §4 rewritten: at L1/L2 the gate is friction against
  the assistant and accidents, not a boundary; the hard guarantees at L2 are sealed credentials
  needing a touch per open (or window) and biometric consent; the agent boundary needs L3
  (future). §4 now lists each way a program running as you can still get around the gate.
  ONBOARDING and MANUAL say the same.


### gt sandbox mode: the assistant's own tools fenced off the vault (off by default)

(Owner, 2026-10-03 18:47: sandbox mode goes into 0.20.0 after the review fixes; the review had
shown that against the assistant's own shell the unlock gate is friction.) `sandbox_mode on`
makes gt configure **Claude Code's own sandbox and permission rules** so the assistant's shell
and file tools cannot touch the vault or gt's state, and the vault is reached only through gt.
Every key was checked against code.claude.com/docs (sandboxing, settings-reference,
permissions) on 2026-10-03.

- **`gt_sandbox.py`** (new, hooks dir) merges into `~/.claude/settings.json`:
  `sandbox.enabled true`, `allowUnsandboxedCommands false`, `failIfUnavailable true`;
  `filesystem.denyWrite` for the vault, `~/.claude/golden-thread` (hooks, state, unlock home and
  markers), LOTR's home and store, `~/.claude/plugins`, `settings.json` and `vault-config.json`;
  `filesystem.denyRead` for the unlock home, LOTR, locked vault folders and (with
  `sandbox_vault_reads deny`, the default) the whole vault; `filesystem.allowWrite` for the queue
  inbox `~/.gt-inbox`; and `permissions.deny` `Read(//…)` / `Edit(//…)` rules for the same paths,
  because the file tools run outside the sandbox. It records exactly what it added, so `remove`
  takes out only that; a backup precedes every write; `check` reports drift and any managed or
  project setting overriding it. `Write(...)`/`NotebookEdit(...)` path rules are not written:
  Claude Code consults only `Read`/`Edit` path rules.
- **Native Windows has no Claude Code sandbox**: there gt writes the permission rules only and
  every report says "friction". Linux and WSL2 need `bubblewrap` and `socat`; without them
  turning the mode on is refused, because `failIfUnavailable` would stop Claude Code starting.
- **The vault MCP server `gt-vault`** (`gt_vault_mcp.py`, in gt's plugin manifest; request
  2026-10-02-vault-mcp-read-server): `vault_search` (index.md first, gt-query scoring,
  supersession order), `vault_read` (64 KiB default, 256 KiB cap, pageable), `vault_list`,
  `vault_queue_write` (gt_write_queue's ops and validation; the broker's decision comes back)
  and `vault_queue_drain`. Read-only; never a file outside the vault or in a locked folder;
  `readOnlyHint` annotations and `outputSchema` (MCP 2025-06-18+). Its tools are offered per the
  new `vault_mcp` setting (`auto` = while sandbox mode is on).
- **Unlock binds it like LOTR**: scope `gt:vault:read`, door `mcp_only`. The server registers
  at startup as the session's *vault* seat (`register_shim` role `vault`: parent `claude`, the
  installed file by real path, first wins, a replacement inherits no grant). The assistant's
  shell and LOTR's shim are refused the scope; the vault seat is refused LOTR and secrets.
- **Writes from the shell go to the inbox.** Inside the sandbox `gt_write_queue.py` cannot write
  the vault's queue folder; with sandbox mode on it leaves the request in `~/.gt-inbox/queue/`.
  Every drain outside the sandbox moves this vault's requests into the queue after the queue's
  validation and rejects the rest to `spool/broker/rejected/inbox-*`; symlinks are never
  followed, a symlinked inbox folder is not read, an oversized file is never read.
- **Folder locks narrowed to the vault** (owner): `gt_lock.py` already refused anything outside
  the vault; under sandbox mode every locked vault folder is read-denied to the shell and the
  file tools whatever `sandbox_vault_reads` says, and locking or unlocking a folder re-applies
  the rules at once.
- **Reported everywhere:** doctor row `sandbox`; `gt_unlock.py verify` rows `sandbox-mode`,
  `sandbox-settings`, `sandbox-deps` (Linux), `claude-sandbox` (`claude sandbox status`) and
  `sandbox-live` (run from Claude's shell, it tries a real vault write and expects the OS to
  refuse); the session-start surface says `SANDBOX MODE: on` and tells the model to use the MCP
  tools; gt-open, gt-query and gt-work say what to do under the mode and change nothing when it
  is off.
- **Proof:** on macOS, the sandbox runtime Claude Code builds on (`@anthropic-ai/sandbox-runtime`
  0.0.78) with gt's generated lists and the working directory inside the vault refused a vault
  write and read (`Operation not permitted`), a hard link to a vault file and a write through a
  symlink, allowed the inbox, and `gt_write_queue.py` inside it queued to the inbox, applied by a
  drain outside. Opt-in test: `GT_SRT=<srt> tests/run.sh test_gt_sandbox`.
- **Not covered:** a live Claude Code session under the mode (the settings translation and the
  file-tool rules were checked by configuration and `claude sandbox status`, not by a live
  session); bubblewrap on Linux/WSL2 was not run live; on native Windows the rules are friction.
  Test receipts and worker declarations under `~/.claude/golden-thread` cannot be written from
  the sandboxed shell, and gt's vault scripts cannot run there — run them in a terminal. The
  vault MCP process starts with every session (it lists no tools while off).

### After the independent re-review (2026-10-04): the inbox, the sandbox settings, orphans, replay

A second, fresh-context review of 0.20.0 (HEAD `efa0620`) found ways round sandbox mode and
unlock: two high, four medium, five low. Each is fixed, or reported where it cannot be prevented
at L1/L2, and each demonstrated bypass is now a regression test that fails against `efa0620` and
passes now (`tests/test_inbox_review2.py`, `tests/test_sandbox_review2.py`,
`tests/test_unlock_review2_regressions.py`). SECURITY.md is corrected to match.

**What you will notice**

- **A hand-started lotrd needs `python3 -I`.** With unlock on, gt trusts its own Python
  processes only under an isolated interpreter (L1, below). gt starts its own with `-I`; a
  `lotrd.py` you start yourself without it is refused until you restart it with
  `python3 -I lotrd.py …`.
- **Terminals that start a non-login shell need an unlock for reads.** `read_without_unlock`
  (LOTR reads, `gt:vault:read`) now needs a login shell above the requesting process (H2,
  below). Terminal, iTerm2, sshd and tmux start one; GNOME Terminal's default does not.
- **`gt_sandbox.py check` reports more**, in your user settings and the project's: every key that
  loosens the sandbox (M1). A user `excludedCommands` that used to pass is now a problem.
- **Sandbox mode also sets `permissions.disableBypassPermissionsMode "disable"`** (restored when
  you turn the mode off) and denies the file tools a set of config files (M2).
- **`vault_queue_write` no longer takes a `session` argument**, and inbox requests wait while any
  live session holds a claim on their path (M4).

**The findings**

- **H1 — the queue inbox could copy a file the sandbox denies into the vault.** The broker
  checked an inbox file with `lstat` and then re-opened it by name, so a writer in the sandbox
  could swap in a symlink (or swap the inbox folder) between the two, and the reject path copied
  the target — the TOTP seed, in the review — into `spool/broker/rejected/`, where `vault_read`
  served it. The broker now opens the inbox folder once with `O_DIRECTORY|O_NOFOLLOW`, opens each
  file relative to it with `O_NOFOLLOW|O_NONBLOCK`, checks the open file (regular, one link,
  size cap), reads it once, bounded, uses only those bytes, and unlinks through the folder
  handle. A hard link and a FIFO are refused unread. Windows (no sandbox there) refuses links and
  reparse points and requires the open handle to be the listed file, inside the inbox. Proven
  deterministically (the swap is injected between check and open) and live under the sandbox
  runtime (`H1LiveRace`), where the pre-fix broker leaked the seed. A request id is now one
  plain name: an inbox body's `../../x` id no longer becomes a path outside the queue.
- **H2 — an orphan could give itself a terminal and read without unlocking.** `setsid` +
  `openpty` + `TIOCSCTTY` gave a double-forked process a controlling terminal, which was all
  `read_without_unlock` asked for. A "terminal" caller now also needs a login shell above it
  (argv[0] starting with `-`, or `-l` / `--login`); on Windows an intact parent chain reaching a
  shell or terminal host (`gt_ipc.is_login_shell`, `interactive_chain`). The re-review's harness
  runs live as the test. Still possible, documented: a process of yours starting its own login
  shell.
- **M1 — a repository's settings could loosen the sandbox unreported.** `check` looked at two
  keys in the working directory only. It now reports, as problems naming the file, every
  loosening key — `sandbox.enabled false`, `allowUnsandboxedCommands`, `excludedCommands`,
  `ignoreViolations`, Unix-socket and Mach-lookup entries, the `enableWeaker…` switches,
  `allowAppleEvents`, `allowLocalBinding`, `filesystem.disabled`, `additionalDirectories`, an
  allow entry re-opening a denied region, a project `disableBypassPermissionsMode` other than
  `disable` — in your user settings and in the project's `.claude/settings.json` (working
  directory) and `.claude/settings.local.json` (working directory and git root). Only managed
  settings or `claude --settings` make the sandbox admin-required, so that Claude Code ignores
  a project's loosening keys; new `gt_sandbox.py managed [--out FILE]` prints that configuration
  (gt never writes managed settings). With it in place, `check` reports project keys as notes.
- **M2 — the file tools could plant code outside the fence.** Edit denies now cover
  `~/.claude.json`, `~/.claude/{settings.local.json,CLAUDE.md,agents,skills,commands,hooks,
  output-styles}`, any `.mcp.json` and project `.claude/{agents,skills,commands,hooks,workflows}`,
  the shell rc files, `~/Library/LaunchAgents` (macOS) and `~/.config/{systemd/user,autostart}`
  (Linux, WSL2) — not all of `~/.claude`, where auto-memory and plans are written. Bypass mode is
  disabled while the mode is on.
- **M3 — an older signed policy approval could be replayed.** The approval now signs
  `policy:<sha256>:serial=N:ts=T`; the authority keeps the highest accepted serial in memory and
  in `approval-floor.json`, and refuses a lower one (or the same serial for other policy
  bytes). Restoring an older policy with its `state.json` fails closed, also after a restart and
  after the floor file is deleted while it runs. Approvals made before this count as serial 0
  until the first new one.
- **M4 — inbox and MCP requests chose their own session and origin.** The broker stamps inbox
  requests origin `inbox` (a body's `farm` is kept: stricter) and session `unknown-inbox`, so no
  live session's claim lets one through (Core rule 1); `vault_queue_write` stamps the server's
  own session id and ignores a `session` argument.
- **L1 — `PYTHONPATH` / `sitecustomize` ran code inside the real installed file.** Trusted peers
  (`lotrd.py`, `lotr_mcp.py`, `gt_vault_mcp.py`, and the `gt_unlockd.py` a client connects to)
  must run with `-I` (or `-E` and `-s`); an unreadable command line is refused. Refusals name
  injection variables, never values (macOS, Linux). gt's launchers pass `-I`: both plugin.json
  MCP entries, `start_daemon`, the doctor's lotrd smoke test; Windows servers keep UTF-8 with
  `-X utf8`. Still possible, documented: modifying the interpreter or its site-packages.
- **L2 — `vault_search` followed symlinks out of the vault.** Every result, and `index.md`, is
  resolved and checked like a `vault_read` path.
- **L3 — inbox rejects could spam the vault.** At most 50 rejected copies are kept; past that,
  logged and deleted.
- **L4 — the vault seat and LOTR's shim shared one grant.** Grants are per seat: approving a
  vault read no longer unlocks `lotr:*`, or the other way round. Lock, session end, sleep and
  screen lock still revoke every seat; a shim ending revokes only its own. `status` gains
  `vault_grant`.
- **L5 — the Touch ID helper was hashed, then executed by path.** Each use reads it once without
  following symlinks, hashes exactly those bytes, writes them to a private 0700 folder, checks
  the signing requirement on the copy, runs the copy and deletes it; a swap after the check runs
  nothing. Still possible, documented: rewriting the helper and its install record together.
- **Not verified live:** the Touch ID tests need an unlocked Mac (Secure Enclave error -25308 on
  a locked one, on the old build too); the private-copy path is covered with a stub helper.

### Claude Code integration: stage agents with their own effort, a pipeline workflow, typed LOTR results

(Owner, 2026-10-03: the 0.20.0 / 0.21.0 split, items 0–4 and 6; plugin-shipped hooks wait for
0.21.0.) Each feature was checked against code.claude.com/docs first, and each falls back to the
0.19 route on a Claude Code that lacks it. `gt_agent_spec.py features` says which apply here.

- **Each pipeline stage is a plugin agent** (`agents/<stage>.md`, run as `gt:extract`,
  `gt:classify`, `gt:reconcile`, `gt:draft`, `gt:verify`, `gt:generalize`, `gt:place`).
  - *Why:* the Agent tool's `model` parameter takes no effort, so every specialist ran at the
    session's effort. A definition carries both (Claude Code 2.1.78+): haiku with no effort,
    sonnet medium, opus high.
  - *Generated, not hand-written:* from a new `agent` block in each stage spec (tools,
    `max_turns`, `omit_claude_md`), by `gt_agent_spec.py agents`; a test fails when a shipped
    file differs from what its spec renders.
  - *Narrower than before:* no stage agent can write, edit or spawn agents. Extract, which reads
    untrusted material, gets only Read, Grep and Glob. verify and reconcile also start without
    the CLAUDE.md files (`omitClaudeMd`, 2.1.271+; an older Claude Code loads them, as the Agent
    tool always did). The validator refuses a spec that loosens any of this, and a kind cannot
    change it.
  - *When it is used:* `gt_model_policy.py apply` writes model and effort into the installed
    definitions, and changing `agent_models` rewrites them at once. `gt_agent_spec.py model`,
    `resolve` and `render --json` name the agent type only when the installed file is exactly
    what the job should run. Otherwise — a hand edit, a vault override of the stage, a job-type
    model override, a Claude Code older than 2.1.78, or a session that does not offer the type —
    the skill spawns as before, with `model`.
  - *Changed on purpose:* `gt_model_policy.py set --agent <stage> --effort E` is now accepted
    (until 0.20.0 every agent effort was refused, because nothing could carry one). A job-type
    effort is still refused: one definition serves every kind.
- **Ingest and promote stages can run as a workflow, `gt:pipeline-stage`** (Claude Code
  workflows, 2.1.154+). `gt_ingest_pipeline.py workflow-args` writes each unit's prompt into the
  run's spool and prints the workflow's args: the stage schema as JSON Schema, and per unit the
  prompt file, its sha256, and the agent type or model and effort.
  - Extract renders its own prompts, through the intake scan of each unit, so the workflow
    cannot skip it; a unit that fails the re-scan is refused and never handed out.
  - The workflow gives each agent only the prompt file's path, and Claude Code validates the
    output against the schema.
  - `gt_ingest_pipeline.py packets` checks every result again and writes the packets.
  - Re-running `workflow-args` hands out only the units without a packet (resume).
  - The workflow cannot ask anything, so every stop is still handled by the skill afterwards.
  - Without the Workflow tool (an older Claude Code, `disableWorkflows`,
    `CLAUDE_CODE_DISABLE_WORKFLOWS`), the per-agent pipeline runs as before.
- **`gt_ingest_pipeline.py --dry-run draft` no longer writes** (found 2026-10-03). Every
  subcommand redefined `--dry-run` with a default of False, and argparse let that default
  overwrite a flag given before the subcommand. A "preview" queued and drained a 55-line
  research entry that cannot be removed (research.md is append-only). The subcommands' copies now
  have no default, so `--dry-run`, `--vault` and `--json` count wherever they are placed. A test
  covers both orders.
- **No skill forks (`context: fork`), and `skill_lint.py` now refuses a forked skill that asks
  the owner anything.** gt-ingest, gt-lint, gt-validate, gt-optimize and gt-review were weighed for
  it. A forked skill sees none of the conversation and runs in the background, so:
  - four of them would lose their conversation with you (ingest's stops and slug question,
    lint's approval loop, optimize's judgement list, review's routing);
  - gt-validate takes its claim from the conversation, and already hands the checking to a fresh
    agent.
- **gt-lotr 0.3.0: typed results.** Each of the four tools declares an `outputSchema` for the
  `structuredContent` envelope it already returned, to clients that negotiated MCP 2025-06-18 or
  later (the protocol that added it). A 2025-03-26 client sees the list unchanged. Only what the
  shim always produces is constrained; a downstream's `data` stays untyped and untrusted. An
  envelope without `ok` gets one, so every result conforms.
- **gt-lotr's hub answers a refused request instead of resetting it.** A reply sent before the
  request body was read -- 401 or 403 before the client is authenticated, 404 for an unknown
  route, 400 on a bad `Content-Length`, 413 over the cap -- closed the socket with the body
  still unread. Python's `http.client` sends headers and body in two `send()` calls. If the body
  was in the server's buffer at the close, closing with unread data sends RST and macOS and
  Linux discard the reply the client has not read yet. If the client was descheduled between
  the two calls -- a loaded host -- the body reached an already-closed socket, which answers
  RST, and Windows raises WinError 10053 on the next read. Either way a wrong secret could look
  like an unreachable hub (`daemon_unreachable` rather than `unauthorized`), and the second
  ordering is the intermittent `test_lotr_frontends.HttpServerTests.test_auth_codes` failure
  under the parallel suite on Windows. Now a request whose body was not read ends with a
  half-close (FIN) and a bounded drain (at most 1 MB + 64 KB or 2 s, discarded, never parsed);
  refusal still happens on the headers alone. A new test sends a 200 KB body to each refusal in
  both orderings and reads only after the server has closed: before the fix it failed on every
  run (ECONNRESET on macOS, WinError 10053 on Windows); after it, it passes on all three.
- **gt-lotr's MCP server starts on native Windows.** Its `plugin.json` launched `python3`, which
  there is the Microsoft Store stub, so the server never started (an open item of this release;
  INSTALL.md documented a manual `claude mcp add`).
  - The fix: `install.sh` rewrites the installed manifests (cache and marketplace) to the
    interpreter it resolved, as an absolute path with `PYTHONUTF8=1`
    (`gt_components.py localize-mcp`).
  - Not a launcher script, on purpose: the server must stay a direct child of `claude`, which gt
    unlock's shim registration checks from the kernel.
  - The post-install gate's `smoke-lotr` row now starts the configured command itself, not
    `sys.executable`.
  - macOS and Linux keep the manifest as shipped.
- **Not covered:** that Claude Code for Windows starts a plugin MCP server directly rather than
  through a shell is inferred from the docs' silence and from the configured command answering
  when run as written (the VM proof). It was not observed under a logged-in Claude Code for
  Windows, an owner step like the hooks. Plugin-shipped workflows (`workflows/` in a plugin) are
  documented, but the docs name no minimum version: gt relies on the Workflow tool being offered.

- **A stage agent that can write gets a private scratch folder, outside the vault.** Pipeline
  stage agents (ingest extract / classify / reconcile / draft, promote verify / generalize /
  place, gt-work's extract-session) shared the run's folder under the vault's
  `spool/pipeline/<run>/`: per run, not per agent, inside the vault -- the wrong place for
  extracted raw material -- and a place sandbox mode denies every write to. New `gt_scratch.py`:
  one folder per run and unit,
  `~/.gt-scratch/<run>/<stage>-<unit>/`, every level `0700` (Windows: `%LOCALAPPDATA%\gt-scratch`,
  inheritance removed, owner-only ACL via `icacls`); a symlinked or foreign-owned level is
  refused, never followed or chmod-ed. `gt_agent_spec.py render --scratch-run R --scratch-unit U`
  and `gt_ingest_pipeline.py workflow-args` (each item carries `scratch_dir`, and a
  skill-rendered prompt without a folder gets one) name it in the prompt as the only place for
  intermediate files; only the agent's result returns, through the packet. **Only an agent whose
  tools can write gets one** (owner, 2026-10-04: "only give them a scratch folder if they
  actually have write capability"): `gt_agent_spec.WRITE_TOOLS` (Bash, PowerShell, Write, Edit,
  NotebookEdit) is the one list, checked against each stage spec's `agent.tools`, so today only
  verify (a shell) qualifies. A read-only stage gets no folder, no scratch text in its prompt or
  its agent definition, and no `scratch_dir`; `render --scratch-run` on one says so on stderr and
  carries on, and a stage spec whose `prompt_delta` names a scratch folder fails validation,
  because the folder comes from the tools, not from spec text. A run's scratch is removed when the run finishes (`draft`, or `promote-plan` with
  nothing waiting) and by the new `gt_ingest_pipeline.py cleanup <run>`; `status` shows it,
  `gt_scratch.py check` exits 1 on a finished or vanished run's leftovers, and `/gt:gt-doctor`
  has a `scratch` row. Sandbox mode adds `~/.gt-scratch` to `sandbox.filesystem.allowWrite`
  (the vault stays write-denied) and takes it out again on `remove`. The stage agents' tools are
  unchanged; the folder bounds where verify may write and adds no write tool. Verify's base
  prompt reads "Do not write, move or delete any file outside the private scratch folder your
  prompt may name"; every read-only agent's still reads "Do not write, move or delete any file";
  the seven agent definitions are regenerated. An ingest run, whose agents are all read-only,
  never has scratch; `status`, `cleanup` and the doctor row treat none as normal.

### Windows, finished; a failed install rolls back; the scheduled jobs keep a working interpreter

(Owner, 2026-10-03: "Finish the work for windows".) 0.19.2 made gt install on Windows; this
makes it usable there. Version directories re-cut from 0.19.3 (gt, demo, farm, flow,
report-card, watch; lockstep modules `requires_gt >=0.20.0,<0.21.0`).

- **`python3` works in Claude's shell on Windows.** Skills tell Claude to run `python3 <tool>.py`;
  in Git Bash that was the Microsoft Store stub. `install.sh` writes a `python3` shim (the Python
  it resolved, UTF-8 mode, `\r` stripped from piped output, exit code kept) to
  `~/.claude/golden-thread/bin/` and to `~/bin/` (first on PATH in every Git Bash login shell),
  never over a `~/bin/python3` that is not gt's. At session start gt's component check appends
  gt's bin dir and `PYTHONUTF8=1` to `$CLAUDE_ENV_FILE`, which Claude Code sources before every
  Bash command (the documented SessionStart mechanism). Nothing changes on macOS or Linux.
- **Every gt write is LF.** 88 text-mode writes in 42 files (scripts, hooks, vault tools, the
  watch/report-card/flow modules) wrote CRLF on Windows; each now writes `newline="\n"` and UTF-8
  (`write_text` became `write_bytes`: `newline=` needs Python 3.10). `tests/test_text_writes_lf.py`
  scans every shipped script so a new one cannot regress (allowlist: empty).
- **Scheduled jobs on Task Scheduler.** `gt_schedule.py install|check|remove|list|reconcile` use
  `schtasks` on Windows, per user and without admin, with the same job table and the same proof:
  run the task once, read Task Scheduler's Last Result. The task runs
  `~/.claude/golden-thread/jobs/gt-<job>.cmd`, which logs to the same `.out`/`.err` files as the
  macOS jobs. Like a launchd agent, a task runs only while the user is logged on at the desktop;
  from an SSH session `install` registers it and says NOT PROVEN. `gt_doctor`'s `schedule` and
  `daily-job` rows read either platform.
- **The test suite runs on Windows**, and running it there found the defects below. The harness
  runs scripts with the interpreter running it (never the Store's), sandboxes `USERPROFILE` with
  `HOME`, puts a `python3` shim first on PATH as an install does, locks the install cache with
  `msvcrt` where there is no `fcntl`, and rewrites every spelling of a sandbox path in a cached
  install; `tests/run.sh` resolves Python like the hooks do. A test that cannot run on Windows
  skips with a specific reason (launchd, cron, osascript, permission bits, a `#!` fake of an
  external program, a file name Windows forbids, gt-lotr, rolling back to a pre-Windows gt).
- **Windows defects the suite found, fixed:**
  - The session-claim guard and `gt_session.py` checked a pid with `os.kill(pid, 0)`, which on
    Windows TERMINATES the process; they now ask the kernel (OpenProcess/GetExitCodeProcess).
  - `gt_session.py` claims and heartbeats failed with "Access is denied" (Windows cannot rename
    over an open file) and had no lock; they now close before the swap and serialise on an
    `msvcrt` lock. Claims, lint keys, queue paths, handoffs and every other vault-relative path
    are written with `/` (about 80 places printed `Projects\x`, so the claim guard never matched
    and the write queue refused gt_daily's note).
  - Vault git hooks ran the Store stub, so every commit to a gt vault was refused.
  - Hook payloads were read as `{}` (`select()` on a pipe fails on Windows): gt_state,
    gt_surface and the report card now peek the pipe.
  - `os.open` without `O_BINARY` wrote CRLF below Python's newline handling (6 writers);
    `gt_demote` died on `O_NOFOLLOW`; pack verification hashed decoded text instead of bytes;
    a checker timeout used `os.killpg`; the doctor's smoke rows left read-only git objects in
    TEMP; reminder credentials were refused for "mode 0o666"; `gt_lint` reported every vault git
    hook "not executable"; the Git Bash `/c/...` spelling escaped the claim and foreign-checkout
    guards; `gt_test_receipt` sent `.sh` suites to cmd.exe; queue timestamps collided.
  - Said in words instead of silently "clean": `gt_workers` (no POSIX process table),
    `gt_watch install-cron` (no cron), `gt_tasks` priority windows (no time-zone database without
    the `tzdata` package), and gt-lotr, which is POSIX-only by design and is now off on Windows.
  - `install.sh`'s "Python:" line goes to stderr, so `--list-plugins` / `--list-modules` stay
    machine-readable; its embedded Python runs from a file (Windows' 32,767-character command line).
- **A failed install is rolled back.** Before its first write `install.sh` copies aside
  `settings.json`, `installed_plugins.json`, `known_marketplaces.json`, the golden-thread-plugin
  marketplace and cache, `~/.claude/golden-thread` (not `backups/`), `vault-config.json`,
  `~/.claude/CLAUDE.md` and the Windows shim, and on any non-zero exit puts them back and says
  `ROLLED BACK` — except exit 4 (installed; the vault is a question for the user) and exit 9 under
  `--force-manifest-mismatch`. Not covered: the vault (it has its own pre-write backup) and loaded
  launchd jobs. Before this, exits 6, 7 and 9 left a half-installed machine.
- **The scheduled jobs' interpreter is chosen, not overwritten** (seen on the publishing Mac,
  2026-10-03). 0.19.1 and 0.19.2 recorded the python running the install (Homebrew's 3.9 there),
  which macOS privacy refuses the vault under launchd: every install broke the jobs, `reconcile`
  died on a traceback when a plist rewrite hit EPERM, and the daily job ended up gone. Now
  `gt_schedule.py choose-interpreter` keeps the recorded interpreter; with a job installed it
  proves the choice with a one-file write probe in the vault run as a launchd job, first success
  wins; with no record macOS prefers `/usr/bin/python3` when it is a working 3.8+ (a Homebrew
  path changes with every Python upgrade; without the Command Line Tools it is never run).
  Plist rewrites are atomic, and one that fails leaves the job exactly as it was and says so in
  words.
- `gt_supersede.py` prints vault paths with `/` on every platform (on Windows it printed `\`,
  which the model then wrote into `supersedes:`).
- **Not covered:** Claude Code's PowerShell tool does not read `$CLAUDE_ENV_FILE` (there,
  python.org's `python` works); hooks under a logged-in Claude Code for Windows are still an
  owner step.

- **macOS: one interpreter for hooks, tools and jobs, chosen by a write probe.** macOS can
  refuse one Python writes it allows another (a file carrying `com.apple.provenance`, a
  privacy-protected folder): on 2026-10-03 Homebrew's `python3.9`, the default `python3` there,
  got `[Errno 1] Operation not permitted` replacing the vault's `log.md` and rewriting worktree
  files while `/usr/bin/python3` succeeded -- and gt's hooks and tools ran bare `python3`.
  New `gt_write_probe.py`: run BY each candidate, it creates, writes, `os.replace`s and removes a
  file in the vault and in `~/.claude/golden-thread`, and opens `log.md` for append without
  writing. `gt_schedule.py choose-interpreter` (which `install.sh` runs) now passes over a
  candidate that fails it on macOS -- in a normal process, not only under launchd -- still
  preferring `/usr/bin/python3` with no record; `install.sh` writes the choice to
  `~/.claude/golden-thread/python`, which `hooks/gt_python.sh` now reads on macOS too, and the
  `.py` hook commands in `settings.json` name it instead of `python3`. A recorded interpreter
  that disappears is a `badpath` in the wiring check, never a silent fail-open. EPERM/EACCES in
  the write helpers is one line naming the interpreter and the fix, not a traceback:
  `gt_spool.write_if_changed` raises `WriteRefused` (a `PermissionError`), `gt_log.py`,
  `gt_adr.py` and `gt_events.py` exit `5` with that line, `safe_write.py` says it before falling
  back, and the broker HOLDS the request with it. `/gt:gt-doctor` has a `write-probe` row:
  PASS/FAIL per interpreter and which one gt uses (all platforms). Linux and Windows are
  otherwise unchanged. Not covered: a skill's own `python3 ...` command run from Claude's shell
  still uses the PATH's `python3`. The refusal could not be reproduced on demand on the
  publishing Mac (the 2026-10-03 condition had cleared), so the tests simulate the EPERM.

### Test receipts name the gates that ran

- **`-j` no longer turns a full run into a subset.** `tests/run.sh` treated any argument as a
  test selector, so `dev/remote-test.sh -j 8` (which passed `-j 8` through) ran the suite as a
  "subset" that skips the secrets gate and the code gate, and remote-test.sh then recorded a
  full receipt from the exit code. Every remote receipt on 2026-10-03 had skipped the secrets
  scan. `tests/run.sh` now separates options (`-j`, `--hosts`, `--no-load-aware`, `--affected`)
  from selectors: a run with no selector is FULL and runs both gates (together, so a secrets
  finding no longer hides the code verdict), and an unknown option is refused instead of being
  taken for a module name. remote-test.sh sends the worker count as `GT_TEST_JOBS`, and its
  `--affected` subsets run with `--gates`.
- **Every run ends with a verdict line**, `gt-gates: scope=full|scoped|subset tests=… count=N
  secrets=… code=…`. remote-test.sh records a receipt only when that line, read back from the
  runner, says full (or `--affected`) and names tests, secrets and code all as `pass`.
- **A receipt carries the verdicts, and readers require them.** `gt_test_receipt.py record
  --gate NAME=VERDICT`. A repo whose test runner declares gates (`# gt-receipt-gates: tests
  secrets code`, in `tests/run.sh` — this repo's does) gets no passing receipt without a `pass`
  for each: recording one is refused (exit 2), and `check`, the commit guard, `gt-allin-commit`
  and scoped coverage all skip one that lacks them — the guard and `check` say which gates are
  missing. A repo that declares none is unaffected. `dev/release-check.sh` records the suite's
  own verdicts on its receipt. Receipts written before this change carry no gates and no longer
  count in this repo: the next full run writes one that does.
- remote-test.sh no longer fails to ship when a tracked file has been deleted from the working
  tree (`git ls-files -co` still listed it and tar aborted), works under Git Bash (the remote
  path came from mixed `C:/` and `/tmp` forms) and on Linux (`mktemp -t NAME` is macOS-only), and
  runs the suite with HOME and TMPDIR in one per-run directory on the runner's disk,
  `/var/tmp/gt-test-<uid>/<run id>` (`$GT_REMOTE_TMP` overrides `/var/tmp`), which it removes: test temp dirs that leaked into a 3.9 GB tmpfs
  `/tmp` had made claudebox2's full runs fail with ENOSPC.
- **A test that leaks a temp dir now fails.** `tests/prun.py` gives every unit its own empty
  `TMPDIR` and fails any unit that leaves something in it, naming each entry and its size, then
  removes it. The leaks were invisible because the tests passed: `test_gt_demote` left a ~118 MB
  `gt-dem-*` per test, `test_gt_registry` a `gt-reg-*`, and nine other classes the same (all now
  clean up with `addCleanup`). The check found three more on its first runs: the harness's
  `Sandbox` removed its directory in `tearDown`, which never runs when a subclass's `setUp`
  calls `skipTest()` (every skipped sandbox test leaked; it is a cleanup now); on Windows
  `shutil.rmtree(..., ignore_errors=True)` silently left every sandbox holding a git repo
  (read-only objects) in `dev/check_wiring_coverage.py` and the shared post-install fixture;
  and `dev/remote-test.sh` never removed its own log (it now keeps it only for a failed run,
  whose message names it). On macOS it found one in the product: Apple's `swiftc` shim leaves an
  empty `TemporaryDirectory.XXXXXX` in `$TMPDIR` on every call, so building the Touch ID helper
  left two behind per install; `gt_unlock_touchid.py build` now gives both calls a private
  `TMPDIR` and removes it. `GT_TEST_LEAK_CHECK=0` turns the check off for diagnosis; plain
  `GT_TEST_SERIAL=1` runs are not checked, and tests that put sockets in `/tmp` on purpose
  (the LOTR tests) are outside `TMPDIR` and covered only by their own `tearDown`.
- **A sandbox never touches the real scheduler.** `gt_schedule.py` took any HOME *inside* the
  real home for the real user, so a test or throwaway install under it -- `test_tmpdir=noindex`
  puts every test HOME in `~/Library/Caches/gt-tests.noindex` -- would `launchctl bootout` /
  `bootstrap` the developer's real jobs on `reconcile` (which `install.sh` runs) and probe under
  the real launchd domain. The real user is now exactly `<pwd home>/Library/LaunchAgents`,
  realpath-compared; and with a sandbox marker set (`GT_TEST_SANDBOX`, which the test harness
  sets for every test and every process a test starts, and `selftest.sh` for its throwaway
  install) every scheduler write -- launchctl
  bootstrap/bootout/kickstart/load/unload..., schtasks /Create /Run /Delete /Change /End -- is
  refused whatever code path reaches it. Reads (`launchctl print`, `schtasks /Query`) still run.
- **Scanner baselines are content-keyed, so a version cut no longer re-flags accepted lines.**
  `tests/secrets-baseline.json` and `.gt/code-baseline.json` keyed each entry by path
  (`gt_secrets`: path, rule, length; `gt_scan_code`: path, rule, line), so every cut --
  `golden-thread/0.20.0/...` copied to `golden-thread/0.20.1/...` -- re-flagged every line the
  owner had accepted, under the new release directory. New `gt_baseline.py`, used by both: an
  entry is the rule, the path with every release-version segment written `<ver>`, a hash of the
  normalised line, and a count. A byte-identical line moved by a cut stays accepted; an edited
  line, the same text in another file, a different rule, or one more copy of an accepted line
  still fails. No values at rest: the secrets baseline holds a PBKDF2-HMAC-SHA256 of the line
  (200,000 rounds, salted with rule and path), never the text, the value or a fast digest; the
  code baseline uses SHA-256. Old path-keyed baselines are still honoured as before (they just
  do not survive a cut); `--write-baseline` writes the new format, and `gt_scan_code` still
  carries `reasons` through a rewrite. Simulated 0.20.0 -> 0.20.1 rename of the code baseline's
  file: 18 accepted findings re-flagged with the old baseline, 0 with the new one, and 1 again
  once an accepted line is edited. The repo's own two baselines are NOT rewritten here: they
  are the owner's records; migrating them is one `--write-baseline` each.

### MCP servers outside LOTR, named (downstream-MCP design, Phase A)

(Owner decision 2026-10-04 07:04 CDT: Phase A ships with this release; lockdown, `import-mcp`
and the rest move to 0.21.) gt unlock, LOTR's tiers and gt sandbox mode cover only what passes
through LOTR and gt-vault. Every MCP server Claude Code connects to directly is outside both,
and nothing said so.

- **`gt_mcp_inventory.py`** (new, stdlib, read-only; `--json`) lists every MCP server Claude
  Code may load from the current folder: user and per-folder local servers in `~/.claude.json`
  (or `$CLAUDE_CONFIG_DIR`), `.mcp.json` in the folder and its parents with its approval state,
  every enabled plugin's servers (`plugin.json` inline, a file path or a list, else `.mcp.json`),
  connected claude.ai connectors, the built-in Claude in Chrome, and `managed-mcp.json`;
  `allowManagedMcpServersOnly` / `allowedMcpServers` / `deniedMcpServers` and per-folder
  disables are applied to each server's state. Each is GATED (named `gt-vault` / `gt-lotr`
  **and** running gt's own script) or UNGATED, with its source, transport, a coarse endpoint
  (command base name, URL scheme and host) and the *kind* of place its credentials live
  (headers in config, env in config, headers helper, args, URL, OAuth store, server-side,
  browser profile, none) plus whether a literal value sits in a file. It never prints a header,
  env, argument or URL value; a test feeds it secret-shaped values in every field and asserts
  none reaches the text or JSON output. Windows folder keys (`C:/Users/...`, any case or slash)
  are matched; `gt_mcp_inventory.py managed` prints the enterprise managed-settings recipe —
  guidance only, gt never writes managed settings.
- **`/gt:gt-doctor` row `mcp`** lists the ungated servers as a note (never "ok" while any
  exist; UNKNOWN when a config file cannot be read).
- **`gt_unlock.py verify` row `mcp`** is NOT-CHECKED — never PASS — while any ungated server
  exists, names them, and the level line says the L1/L2 claim covers LOTR and gt-vault only.
- **SECURITY.md section 9, "MCP servers outside LOTR"**: direct servers are outside unlock and
  sandbox mode (not "friction" — outside), how to route an HTTP server through LOTR
  (`lotr add-mcp`, then `claude mcp remove`), and the managed-settings recipe with the paths
  per OS.
- **Sandbox mode read-denies Claude Code's login file**, `~/.claude/.credentials.json` (its
  OAuth tokens on Linux, WSL and Windows), in `sandbox.filesystem.denyRead` and as a
  `Read(//…/.credentials.json)` rule. Checked live on macOS with Claude Code 2.1.289: headless
  `claude -p` still answers with the entry in place, and a sandboxed `cat` of a read-denied
  fixture is refused (`Operation not permitted`) where the same file, not denied, is read. The
  real file does not exist on macOS (Keychain), and Linux was not run live. A machine already
  running sandbox mode reports one missing entry in `gt_sandbox.py check` until
  `gt_sandbox.py apply` runs again.

### gt unlock, usable on every machine (usability run 2026-10-04: B3, M5, M6, M12)

- **`gt_unlock.py run` works.** `run --scope S -- CMD` crashed every time (`TypeError:
  unhashable type: 'list'`): its command positional shared the subcommand's argparse dest.
  Tested end to end -- refused while locked, runs once unlocked, `--secret-file` handed over
  and deleted.
- **TOTP-only machines can turn unlock on** (a Mac without Touch ID or the Command Line Tools,
  a desktop Mac, a PC without Windows Hello). `gt_unlock.py policy enable --factors totp` sets
  K (how many factors must agree) to the factors you name and records the choice in the policy
  (`factors.chosen`), the audit log and `status`; it still needs your current factors. At a
  terminal `policy enable` offers it; anywhere else the refusal names that command. K is still
  never lowered silently. Windows Hello (and Touch ID) is optional:
  `enroll totp --without-platform` skips it -- only for a process the kernel shows is a
  person's terminal (no `claude` ancestor, a tty, a login shell), never Claude Code's shell.
- **One code per command.** A request that unlocks and also needs a step-up
  (`gt:settings:security`) takes the factors it just collected as the step-up, when they are
  what a step-up asks for (the platform factor where one is usable, else any but a recovery
  code); a grant made earlier still steps up. `gt_settings.py set unlock on|off` no longer asks
  the settings gate before `policy enable|disable` asks for K fresh factors. "Code already
  used" now says "wait for the next code (about N s)"; replay is still refused. On Linux / WSL2
  `set sandbox_mode on` checks bubblewrap and socat before asking for any code, drops the
  nonexistent `--force` hint, and names apt / dnf / pacman / zypper by distribution.
- **The authority restarts after an install.** The same `gt_unlockd.py` pid survived
  reinstall, rollback and roll-forward, running old code. It now fingerprints its code files at
  start; `install.sh` runs `gt_unlock.py daemon restart-if-stale`, which stops it only when the
  code changed (no factor: it only stops and never grants; every grant is dropped and the
  output says so) and starts it again when unlock is on. A 0.20.0 authority, which has no such
  method, is ended by signal after the client verified it is the installed `gt_unlockd.py`.
  `status`, `daemon status` and `verify` (new `authority-code` row) report "the unlock service
  is running old code (pid N, since ...)" with the command.
- **A refused consent cools down.** A platform consent (Touch ID / Hello) that was cancelled,
  refused or failed set no cooldown, so a confused or injected session could raise prompts back
  to back. It now sets the same cooldown an unlock refusal does: the refusal says
  `consent_denied ... wait N s`, and the next consent inside the cooldown is refused with code
  `cooldown` without a prompt. Nothing is auto-approved.
- **Wording.** `unlock` says the grant is bound to this shell / Claude Code session and what
  ends it; `status` shows the last revocation ("revoked at 14:02: screen locked"), K explained,
  and one `Next:` line that `enroll` and `recovery` print too and that never names a factor
  the OS cannot have; `enroll totp` at end of input prints one line, not an `EOFError`;
  `--help` describes every command; prompts name "Claude Code session (pid N)" for a shim or
  hook of Claude Code (and "<script> (pid N), a command in Claude Code session (pid M)" for a
  deeper process) instead of "Python (pid N)".

## gt 0.19.2 — 2026-10-03

**gt installs on native Windows** (owner, 2026-10-03). Proven on a Windows 11 VM (Git for
Windows 2.56.0, Python 3.12.10 from python.org): `install.sh` from Git Bash and the new
`install.cmd` from cmd.exe both install with and without a vault, and the post-install gate
passes there. Before this, the install stopped at the first line that ran Python. Version
directories copied from 0.19.1 (gt, demo, farm, flow, report-card, watch; lockstep modules
`requires_gt >=0.19.2,<0.20.0`); gt-lotr 0.2.0, gt-usage 0.1.5, gt-visualize 0.4.3 and gt-wiki
0.2.6 already admit 0.19.2 and are unchanged. Nothing changes on macOS or Linux.

- **A real Python, found once; the Microsoft Store's does not count.** On Windows `python3` is
  the Store's "Python was not found" stub (exit 9009). `install.sh` now resolves `python3`, then
  `python`, then `py -3`, rejects anything under `...\WindowsApps\` — the stub and a real Store
  Python alike — and anything below 3.8, and with nothing left refuses **before anything is
  installed**, naming python.org and *Add python.exe to PATH*. Every `python3` in the installer
  then means that interpreter, in UTF-8 mode, with `\r` stripped from what it prints.
- **`install.cmd`** beside `install.sh`: a launcher for cmd.exe, PowerShell and Explorer. It
  finds Git Bash (skipping WSL's `System32\bash.exe`), refuses with the Git for Windows link
  when there is none, and runs `install.sh` with every argument, passing its exit code back.
  CRLF on purpose; `.gitattributes` pins `*.cmd` to CRLF.
- **Hooks run on Windows.** Every hook wrapper sources the new `hooks/gt_python.sh` (a no-op off
  Windows): the interpreter install.sh recorded beside the hooks dir, else the first non-Store
  Python on PATH. `.py` hooks in `settings.json` name the interpreter by absolute path with
  `-X utf8`, and every hook path is written with `/` — Git Bash reads a `\` as an escape.
  Before, every hook failed open silently: no Core rules injected, no guard run.
- **MANIFEST keys are `/` everywhere.** Windows `relpath` gives `hooks\x`, so every shipped file
  read as both missing and extra and the installer refused; `gt_registry` refused every shipped
  pack the same way.
- **CRLF:** files a fresh vault is built from are written with `\n` (`vault_init`, and the
  stamp and merge bases `gt_upgrade` writes). On Windows they came out CRLF, so a new vault
  reported every tool and Core rule as "modified locally" and held both doc merges.
- **UTF-8 output:** piped Windows Python is cp1252, and the first `⚠` or `→` was a
  UnicodeEncodeError (vault refresh, `gt_daily`, the upgrade check).
- `gt_doctor` sets `USERPROFILE` with `HOME` for its throwaway runs: Windows Python reads
  `USERPROFILE`, so the smoke tests were reading the real user's `~/.claude`.
- `gt_schedule.py install|check|remove` refuse in words on Windows (launchd only; it died on
  `os.getuid`). `record-interpreter`, `reconcile` and `list` work.
- **Not yet covered:** hooks as Claude Code for Windows runs them (run directly with the same
  payloads instead); skills that tell the model to run `python3 …` (the model sees the stub's
  message and retries with `python`); other text-mode writers outside the install path still
  write CRLF on Windows; Task Scheduler jobs. Tests: `tests/test_windows_install.py`.

## gt 0.19.1 — 2026-10-02

> **There is no published 0.19.0.** `install.sh` changed (phases 5 and 6c) after the 0.19.0
> directories were cut, and `check_installer_version` exists so one version name never covers
> two installers; as with 0.18.1, the owner chose to re-cut (2026-10-02). The rollback target
> stays 0.18.1, the last release published.

Batch 1 of the 2026-10-02 accepted requests: model and effort per plugin, a recall benchmark,
prompt-relevant vault hints (off by default), supersession and expiry at read time, and five
fixes to rough edges found installing 0.18.1 — plus, added during the build by the owner, SSO
for LOTR, a commit gate that fits a large repo, scanner false positives and the install-time
timestamp alert. Version directories copied from 0.18.1; gt-lotr 0.2.0 (new feature), gt-usage
0.1.5, gt-visualize 0.4.3 and gt-wiki 0.2.6 with `requires_gt >=0.19.1,<0.20.0`.

- **Model and effort per skill.** Every shipped skill declares `model_intent` (fast, balanced,
  deep; agent `model_tier` standard/careful map to balanced/deep), enforced by
  `skill_lint --require-intent` at release. The intent pack carries an effort, and the new
  `gt_model_policy.py` writes `model:`/`effort:` into the INSTALLED copy of every gt skill,
  never the release source. Profiles: `average` (haiku with no effort — Haiku has no effort
  levels — sonnet·medium, opus·high; a new install's default), `very-high` (opus·xhigh) and
  `inherit`. `install.sh --model-profile`; an interactive upgrade asks once, a scripted one keeps
  `inherit`. Per-skill and per-plugin overrides (`set`/`clear`, re-applied at once); an effort a
  model does not accept is refused, naming the allowed values. Doctor `model-policy` row;
  `gt_model.py skill` reports the installed frontmatter.
- **Specialist agents run on the model their task needs.** An agent's `model_tier` is no longer
  advisory: `gt_agent_spec.py model <job>` (and `resolve`, `render --json`) names the alias the
  skill passes as the Agent tool's `model` — classify and draft haiku, extract and place sonnet,
  reconcile, verify and generalize opus — whatever profile the skills use, so a very-high machine
  still reads documents on sonnet. New setting `agent_models` (`task`, the default, or
  `session` to pass no model). `gt_model_policy.py set|clear --agent <stage or job type>` overrides one (a
  model alias only: the Agent tool has no effort parameter); `show` lists every stage.
- **Prompt hints, off by default.** `vault_hints` adds a UserPromptSubmit hook naming at most
  three `index.md` titles relevant to the prompt — never a page body, nothing past 0.8 s.
- **Supersession and expiry at read time.** `gt_supersede.py listing|rank|dangling`; gt-open lists
  the newest of each chain, gt-query ranks current before expired before superseded, gt-work
  records `supersedes:` when a contradiction is resolved; gt-lint `supersedes-missing`.
- **LOTR handles SSO (gt-lotr 0.2.0).** `lotr add-mcp` fronts an SSO/OAuth MCP endpoint by
  reusing its client's token by reference (`file:<path>#<field>`), refreshing with the client's own
  helper on 401 and retrying once; tools discovered and tiered by annotation or name.
- **Queue guard reads shell tokens.** Quoted `>`, `$VAR` targets and a preceding `cd` no longer
  draw false refusals; a `cd` into the vault is still caught.
- **Nothing writes bytecode into gt-src.** Hook commands run `python3 -B`; the doctor and the
  session checks switch bytecode off for themselves and their children.
- **post-install resolves the installed release from any path**, and **every completed gate run
  writes the receipt** (`writer`: hook or manual; a crash, the install stage and `--dry-run` write
  nothing).
- **One recorded interpreter for every launchd job.** `install.sh` records it and reconciles
  installed jobs (reload only under the real home); an EPERM under CloudStorage names the
  interpreter and folder that need Full Disk Access.
- **The commit gate fits a large repo.** `--timeout` / `allin_timeout`; the gate skips the `tests`
  member (the receipt is the evidence); code and naming scans skip release folders older than
  the newest two.
- **Scanner false positives.** `except _Name` is not bare; naming rules take `exempt` (unittest
  names, `do_<METHOD>`); a private `_Pascal` class is PascalCase.
- **No timestamp alert at install.** The Stop validator holds a reply to the timestamp rule only
  when that turn was given the time — the turn that runs `install.sh` never was.
- **Recall benchmark.** `gt_bench.py recall --fixture DIR [--retriever keyword|FILE.py ...]`
  reports recall@1/3/10, files read and approximate tokens per question; a retriever that cannot
  load or raises is could-not-run, never zero. Baseline, keyword retriever (`gt_keyword_recall`,
  the gt-query path) on the synthetic 36-question fixture `tests/fixtures/recall-bench`:
  **recall@1 0.972, recall@3 1.000, recall@10 1.000**, 123 files read, ~9,919 tokens
  (2026-10-02, self-verified). Optimistic by construction — the fixture's questions and pages
  were written together — so it compares retrievers; it does not predict a real vault.

## gt 0.18.1 — 2026-10-02

> **There is no published 0.18.0.** The release was cut as 0.18.0, then `install.sh` and
> `selftest.sh` were fixed after the cut (the SIGPIPE bug below). `check_installer_version` exists
> so one version name never covers two different installers, so the owner chose to re-cut as
> 0.18.1 (2026-10-02). The 0.18.0 directories were renamed, not copied, so the rollback target
> stays 0.17.11 -- the last release that was actually published.

A release built from the accepted feature-request queue rather than from one incident. Its themes:
**one verb per action** (create, open, list, handle, close), a **coding loop** with a plan gate,
**decisions you can trace** (ADR expiry, supersession lineage, a repo CLAUDE.md drafted from the
vault), **subtraction** (gt-optimize measures what sessions and the vault cost, and can archive;
gt-minimize prunes a heavy session), **write-back that checks itself** (contradictions, promotion
candidates, a research digest), **returning after time away** (a catch-up brief, what changed this
session), **install health and guards** (the doctor names the repo a command will hit, checks
hook entries against Claude Code's events, and a guard refuses commits in another machine's
checkout), **checks that modules contribute** (a validation host, and fixes only gt writes),
**model intent, not model name**, **across machines and outside sessions** (`gt-sync`, reminders
by notification, SMS/Discord or email), **batches that resume** after an interruption, a
**release pipeline for every project**, a **faster build loop with measured execution**, and
**ingest and promote as staged pipelines**. Skills: 28 → 36. gt_lint checks: 19 → 24. Settings:
twenty-two new, every one listed under its theme. Hook registrations: two new (one `PreToolUse` guard, one `PostToolUse` read log).

Module versions: gt-demo, gt-farm, gt-flow, gt-report-card and gt-watch 0.18.1 (they move with
gt); gt-wiki 0.2.5; gt-visualize 0.4.2; gt-usage 0.1.4; gt-lotr 0.1.1 — each bumped so its
`requires_gt` admits 0.18. gt-flow 0.18.1 also widens redacted hashes and knows the `addon.fix`
event; gt-wiki 0.2.5's `review-due` ages a page from when it was last read (both below).

Owner decisions this release rests on (2026-10-01): the **verb-first vocabulary is accepted**;
**gt-learn is folded into gt-work** rather than shipped as a third capture skill; **gt-minimize
prunes and writes no in-flight note** (carrying work forward is the handoff's job); **gt-close
archives a project in place**, and relocates it to `Archive/` only with `--move`.

### One verb per action: create, open, list, handle, close

**What.** gt's skills used two naming schemes at once: verbs (`gt-open`, `gt-create`, `gt-work`)
and artifact-typed compounds (`gt-task`, `gt-handoff`, `gt-task-list`, `gt-handoff-list`,
`gt-task-handle`, `gt-handoff-handle`). The verb is now the skill and the artifact its argument:

| verb | project | task | handoff |
|---|---|---|---|
| `gt-create` | `project <slug>` (a bare slug still works) | `task <text>` | `handoff` |
| `gt-open` | `<slug>` (unchanged) | `task <id>` — one task's full detail | `handoff [id]` — one handoff and only the task lines citing it |
| `gt-list` | — | `tasks [filter]` | `handoffs` |
| `gt-handle` | — | `task [filter]` | `handoff` |
| `gt-close` | `project <slug>` | `task <id>` | `handoff [id]` |

Bare `/gt:gt-list` summarises both lists; bare `/gt:gt-handle` asks which to work through when both
have something waiting. Handling a task gains a fifth decision, **move** to another project
(`gt_task.py move ID --to SLUG --reason R`), beside done, drop, defer and keep. `gt_surface.py`'s
session-start lines now name the new verbs.

**`gt-close` is new.** `gt_close.py project <slug>` lists every open task (the project's and its
sub-projects', deferred ones included — a deferral comes back), every open or deferred handoff and
the research entries worth offering for graduation, and **exits 1 while anything is undecided**:
the close stops there. Each task is closed, dropped, moved, or kept shelved at `p:: 7`
(`gt_task.py shelve`, new); each handoff has its items settled and is closed. The skill then offers
graduation one finding at a time through `/gt:gt-promote`, and `--archive` runs
`vault_init.py archive-project`: `stage: archived`, an *Archived* banner, the `Projects/README.md`
row marked — **archived in place**, nothing moves, every link resolves. `--move`, only on request,
then relocates the folder to `Archive/<slug>/` and re-points `Projects/<slug>/` path references
through the write queue, refusing while a live session claims a file in the project.
`gt_close.py task <id>` is `gt_task.py done` plus the rollup; `gt_close.py handoff <file>` refuses
while a task citing the handoff is open, then marks it `handled`. `gt-handle`'s per-item close
calls the same two commands.

**Deprecated, still working through 0.18.x:** `gt-task`, `gt-handoff`, `gt-task-list`,
`gt-handoff-list`, `gt-task-handle`, `gt-handoff-handle`. Each prints
`Note: <old> is deprecated. Use /gt:gt-<verb> <artifact> instead.` and then follows the new skill's
section, where its procedure moved unchanged. Their trigger phrases moved to the new verbs (two
skills may not share a trigger), so plain-language requests already reach the new names. They are
removed in the release after 0.18.x.

**Why.** Someone who wants to "handle" something had to remember which compound applied, and the
list grew by one skill per artifact per action. One skill per verb keeps the count flat as
artifacts are added. `gt-close` exists because projects, tasks and handoffs each had a "done" state
and no guided path to it: done work lingered in the rollup and findings were stranded in finished
projects. The request asked for "move to `Archive/` and mark complete"; the owner chose archive in
place, because a moved folder breaks every path link into it, with `--move` for when that is
wanted anyway.

### A coding loop: `gt-plan` and `gt-implement`; learning folded into `gt-work`

**What.** Two skills, no new scripts.

- **`/gt:gt-plan <task>`** restates the requirement and waits for "that's right"; reads the
  project's `design.md`, `spec.md`, the relevant ADRs and the code it will touch, citing each;
  lists risks and unknowns marked verified or unverified; and writes a numbered, phased plan (goal,
  test first, change, done-when) to `<repo>/.claude/gt-plan-current.md` at `status: draft`. It
  ends with one question, "Approve this plan?", and writes no code. Only an explicit yes sets
  `status: approved`; editing an approved plan returns it to draft.
- **`/gt:gt-implement`** refuses without an approved plan, naming the file. Then, per phase:
  the failing test first, then the code, then the refactor, then docs if a documented interface
  changed; `gt_allin.py --only tests,scan`, naming the tests that ran; **any failure, or any
  member that could not run, stops the run**. It asks before each next phase and commits only
  through `gt_allin_commit.py` (dry run first, then "Commit this?"). It never pushes.
- **`/gt:gt-work` gains a *Learn* step** (owner decision: no separate gt-learn): patterns applied
  more than once, decisions not yet written down, reusable techniques — anything not already
  captured is offered one at a time and routed to an ADR, a runbook step, a memory note or
  `/gt:gt-promote`, always through the queue.

**Why.** gt covered knowledge capture and the project lifecycle but not the coding loop itself,
which people ran through a separate review plugin. These reuse gt's own checks and commit gate
rather than carrying a second copy. A third capture skill beside gt-work would have split where
findings go.

### Decisions you can trace

**ADRs can say when they stop being true.** `gt_adr.py allocate --expires-when "<condition>"` and
`--expires YYYY-MM-DD` write `- **Expires when**:` / `- **Expires**:` field lines. New gt_lint
check **`adr-expires`** files, in the review queue and with the condition text, every ADR
declaring a condition (at any age: only a person can tell whether "until the SDK upgrade" has
happened) and every ADR whose date has passed. An ADR another ADR supersedes is not listed — the
replacement answers the question. gt-work now asks, when it writes an ADR, whether the decision
has a known expiry condition. *Why:* "SQLite until 10k requests a day" is exactly what an ADR
records, and without the field it reads as permanent after the condition has passed.

**"How did we decide X?" has an answer.** `allocate --supersedes N` (repeatable; refuses a number
the project has no ADR for) writes `- **Supersedes**: ADR-N`. Read-only
`gt_adr.py lineage <project> "<topic>"` finds every ADR mentioning the topic, follows its
supersession chain both ways, and prints it oldest first — number, date, title, "Superseded by
ADR-M because …", the live one marked `— current` with its rejected alternatives.
`/gt:gt-query --lineage <topic>` (or "what is the history of decisions about X?") runs it. gt-work
asks whether a new ADR replaces an earlier one. *Why:* `decisions.md` is append-only, so a topic
accumulates chains (ADR-1 → ADR-4 → ADR-9) that nothing could navigate. A chain is only as good as
its `Supersedes` fields: ADRs that superseded others in prose only appear as one-entry chains.

**A sub-project's ADRs land in the sub-project.** `gt_adr.py` joined `Projects/` with the slug it
was given, so `allocate`/`merge` for a sub-project made a new top-level `Projects/<slug>/
decisions.md` (a folder with no README), reported "updated", and left the sub-project's own
`decisions.md` an empty stub — and a second merge said "unchanged", so the mistake was silent and
self-consistent. Every subcommand now resolves the name through one shared resolver
(`gt_spool.resolve_project`): a bare sub-project slug resolves to `Projects/<parent>/<slug>/`; an
unknown name, or one two sub-projects share, is refused (exit 2) naming them. **Existing vaults
need no migration step:** the first `allocate`/`merge` for an affected sub-project moves the stray
spool slots into the right spool (exit 3 if one ADR number would be held twice); the stray
top-level `decisions.md` is named on stderr and left for you to delete.

**Every tool that takes a project now resolves it the same way.** The other slug-taking tools
still joined `Projects/` with the slug, so they missed sub-projects, and the writing ones could
grow a stray top-level folder (`gt_demote --to project-memory` made `Projects/<slug>/memory/`,
`gt_handoff` made `Projects/<slug>/handoff/`). Switched to the resolver: `gt_task.py` (add, move,
task IDs), `gt_handoff.py`, `gt_handoff_status.py list`, `gt_demote.py`, `gt_close.py`,
`gt_catchup.py`, `gt_digest.py`, `gt_optimize.py --project`, `gt_memory_check.py`,
`gt_promote_detect.py`, `gt_surface.py handoffs` (soft: it falls back to the name as typed, since
it runs inside gt-open), and `vault_init.py rename-project`, `merge-project` and
`archive-project` — which until now took the *first* directory anywhere with that name, a
`memory/` folder included. Every one now also accepts `parent/child`. **Behaviour change:**
`gt_memory_check.py` with an unknown slug exits 2 with the resolver's message; it used to exit 0
silently. A stray top-level `Projects/<slug>/` left by the old behaviour needs no migration: move
its contents into `Projects/<parent>/<slug>/` and delete it.

**A sub-project's CLAUDE.md points at its real folder.** The template rendered `Projects/<slug>/`,
which for a sub-project does not exist; it now uses the path create-project made. `--parent`
resolves through the same resolver, so `--parent <a sub-project's slug>` nests under it. Existing
CLAUDE.md files are never rewritten: fix an old sub-project's "Deeper context" line by hand.

**`/gt:gt-brief` drafts a repo's CLAUDE.md from the vault.** PROTOCOL's "graduating a fact out to
a repo" defined the outward path and no tool did the mechanical part. `gt_brief.py --vault V <slug>
[--repo PATH]` prints a proposed section — the project in one paragraph, **Constraints** (every ADR
neither superseded nor carrying an expiry), **Where it runs** (from `source.md`), **What not to do**
(current ADRs' rejected alternatives), and the optional "Deeper context" trailer — with ADR
numbers, wikilinks and vault-folder lines stripped, because the reader has no vault. It writes
nothing; the skill reviews the draft with you (would a teammate need this? is it stable?) and, on
a yes, places it at `Projects/<path>/CLAUDE.md` through the queue. `runbook.md` is deliberately not
read: its facts still graduate by hand. The name stays `gt-brief` — "brief the repo" is already a
verb.

### Review-queue lint checks, and memory by what it is about

Three new gt_lint checks file **questions for the owner** into `review-queue.md`, not defects
(with `adr-expires` above, 19 → 23 checks):

- **`bundled-concept`** — a Knowledge page with four or more `## ` headings of which at most two
  share a keyword with its title or tags: "this page covers multiple topics; consider splitting".
  It never proposes the split. `category: decision` and `_`-prefixed pages are exempt. Exactly two
  sharing **fires** — the request's own example has two on-topic headings and must fire
  (`BUNDLED_COHERENT_AT = 3`).
- **`decision-candidate`** — "we chose", "by design", "deliberately" and the like in a project's
  `design.md` or `research.md`, filed with the line, the phrase and a proposed
  `gt_adr.py allocate` command. Nothing writes an ADR. New setting **`decision_signals`**
  (`default`; `off`, or `;`-separated `+phrase`/`-phrase` edits to the built-in list). New
  per-line suppression: `suppress: <path>:#<hash>` (survives the line moving) or `<path>:L<n>`.
- **`memory-entity-orphan`** — a name that appears three or more times in a memory note's body and
  is not in its `entities:` list.

**Memory can be looked up by what it is about.** A memory note may declare `entities: [...]` in
frontmatter (the template now shows `entities: []`). Read-only `gt_entities.py lookup <name>
--project <slug>` returns only the notes that declare it, each with its index description and full
text; `list` shows every declared entity. `/gt:gt-query --entity <name>` (or "what do we know about
X?") runs it, and gt-work asks which entities a new note covers. gt never writes entity tags
itself. *Why:* memory is filed by when it was written, so "what do we know about the auth service?"
meant opening every file.

The PROTOCOL template gains *ADR fields and memory entities*; `/gt:gt-upgrade` merges it.

### Subtraction: what the vault and a session cost, and how to cut it

**gt-optimize is an aggregator with two members.** It asked only what the vault **stores** that
every session pays for; nothing asked what a **session carries**, which was the larger number:
over 180 days of one machine's transcripts, 98.6% of input came from cache, yet about 65% of
cache-write spend was avoidable, mostly sessions idled past the cache TTL and rebuilt in full.
`gt_optimize.py` now runs `vault` (the old report) and **`session`** (new
`gt_optimize_session.py`) through the same aggregator as gt-scan and gt-allin and always says
`N of 2 member(s) ran`. The session member splits every cache write into `cold`, `growth`,
`expiry` (the gap outlived the TTL that prefix was written at) and `invalid` (the cached prefix
changed); only the last two are avoidable. It prices them as list-price **equivalents**, not a
bill. New settings **`optimize_session_days`** (30) and **`optimize_avoidable_pct`** (50).
**Behaviour change:** a bare `gt_optimize.py` exits 3 on a machine with no transcripts, so
`gt_allin` runs it as `--only vault`.

**gt-optimize can subtract** (the memory-optimisation request, rescoped by the owner into
gt-optimize rather than a new command):

- **`--cost`** — what `/gt:gt-open` actually loads per project, in lines and approximate tokens.
- **`single-project-global`** — a `global-memory/` note whose body names exactly one project
  (whole-word): that project's memory charged to every other.
- **`--archive --project S --before YYYY-MM-DD [--apply]`** — moves dated `research.md` entries
  older than the cutoff to `research-archive-<YYYY>.md`, leaving one dated index line per entry.
  Archive written first, read back, and only then is `research.md` rewritten — a failure leaves the
  entry in both places, never in neither.
- **`--supersede --file F --entry H --by H [--apply]`** — marks an entry superseded in place;
  the entry stays.

Reporting never writes; both actions preview unless `--apply`, go through the write queue, and
delete nothing.

**Knowledge pages are logged when read.** Git shows when a page was written, never whether anyone
read it. A new hook, `log_knowledge_read.sh` (`PostToolUse`, matcher `Read`, installed by
`install.sh`), appends one line per Read of a `Knowledge/` page to `<vault>/usage/knowledge.jsonl`
(page, 8-character session id, date), which a `usage/.gitignore` keeps out of git. The vault member
gains **`knowledge-unused`**: pages not read in `--unused-days` (90), report-only. With no log yet
it reports nothing, or every page would read "never read" on day one. Setting
**`knowledge_access_log`** (`on`). It is a hook, not skill prose, because prose logs only the reads
a model remembers to log.

**`/gt:gt-minimize` prunes a session, then tells you to cut while the cache is warm.** A resumed
session past its cache TTL rebuilds its whole prefix (the median resume rewrote 432.7K tokens;
fresh sessions start near 44.5K), and `/compact` silently drops what the vault exists to keep.
`gt_minimize.py` (read-only) measures this session's billed context, an estimated breakdown
labelled as one, and the minutes of cache warmth left; the skill triages what is worth keeping
(to `Knowledge/` via gt-promote, or `INBOX.md`), lets the rest go, and says "/compact or /clear
now". **It writes no in-flight note** (owner decision): anything mid-flight goes to
`/gt:gt-create handoff` first.

### Write-back that checks itself

- **Contradicting memory notes are flagged.** `gt_memory_check.py`, run by `/gt:gt-work` after
  write-back, compares only the notes written or changed this session with their close
  neighbours, by keyword rules (the same `key:` with different values; the same subject with
  opposite polarity). Each pair is shown with both sentences and "same fact? which is current?".
  Nothing is modified. Setting **`memory_contradiction_check`** (`on`).
- **Promotion candidates are listed.** `gt_promote_detect.py`, also run by gt-work: a memory note
  changed in 3 of the last 5 commits to its project's `memory/`; `research.md` sections in two
  projects whose words overlap by **`promotion_overlap`** (80%) of the smaller; and gt-lint's
  `global-scope-leak`, with the demote command. Nothing is promoted without a yes. Setting
  **`promotion_candidates`** (`on`).
- **`research-digest.md`** — `gt_digest.py write`, on every gt-work, rewrites a one-line-per-entry
  summary of the newest 20 `research.md` sections plus up to 5 marked `[pinned]` in their heading,
  through the queue, and indexes it in the project's `MEMORY.md` once. It records the hash of the
  `research.md` it was built from; `/gt:gt-open` reads it in place of the headings only when
  `gt_digest.py check` says it is current. `research.md` itself is never touched.
- **The skeptic pass no longer needs agent specialization.** In 0.17.11 the gt-work skeptic ran
  only with `skeptic_pass` **and** `agent_specialization` on, so turning the skeptic on also handed
  ingest and validation to specialist agents — a separate decision with its own cost.
  `skeptic_pass` alone now governs it; a 0.17.x vault override that still lists both is honoured
  without restoring the coupling.

- **Link suggestions after a Knowledge write.** CONVENTIONS calls cross-domain links the most
  valuable and the most often missed, and lint could only find broken ones. `gt_link_suggest.py
  suggest`, run by gt-work after it writes a Knowledge page, reads that page and **only the
  frontmatter** of every other page and proposes up to five unlinked pages — cross-domain first,
  then shared tags (3 points) and title words (1) — each with a reason and a link type. `apply`
  writes only the ones you pick, both directions, into `## Related` through the queue.
- **`review-due` measures when a page was last read** (gt-wiki 0.2.5). It aged a page from
  `updated:`, so a page written once and never consulted looked as current as one reread last
  week. `/gt:gt-query` (after it answers) and `/gt:gt-open` (for Knowledge pages it loads) now run
  `gt_review_stamp.py`, which queues `last_reviewed: <today>` as a `set-property` request, at most
  once a page a day, never touching `updated:`. The wiki lint ages a page from the newer of the
  two; a page with no stamp reports exactly as before, so existing declines still match. Setting
  **`review_stamp`** (`on`).

*Why:* each of these was a step gt-work asked a person to remember at the end of a session, which
is the step that gets skipped.

### Returning after time away

- **A catch-up brief.** `/gt:gt-open` runs `gt_catchup.py` before it reads anything: when you have
  not opened the project on this machine for **`brief_absence_days`** (7), or asked with
  `--brief`, it leads with one paragraph of at most 150 words, labelled as generated — the commits
  to the project since you last opened it, the newest research entry, the oldest open `p:: 1` and
  every open `waiting:: user` task. Built from git and structured fields, never a summary of
  prose; the full reading sequence still follows. `--no-brief` skips it. *Why:* after two weeks
  away the reading order answers "what is the state", not "what happened".
- **What changed this session, in the handoff.** `gt_session.py register` records the vault's
  commit as `start_commit:`, and `gt_handoff.py` adds `## What Changed This Session`: new memory
  notes with descriptions, research headings appended, new ADRs, `design.md` files touched, and
  Knowledge pages created or updated, from `git diff <start>..HEAD`, labelled `self-verified`.
  `--since-commit` overrides; `--dry-run` is new. The handoff procedure itself now lives in
  `/gt:gt-create handoff`.
- **The pre-compaction state write is visible to you.** `gt_state.py` printed to stdout, which
  for a hook reaches only the model — the failure the SessionStart checks had until 0.9.6. As a
  hook it now writes a `systemMessage`: one line when the threshold write happens, one when it
  fails and why, and at `PreCompact` always one line. A below-threshold turn shows nothing.
- **A new session no longer reports another session's context figure.** The usage ledger is
  shared by every session on the machine, and `gt_state` took its newest reading whoever wrote it
  — which is why, on 2026-09-29, a fresh session's first prompt said `context 88% — already
  written`. It now filters by the session id; a session with no reading of its own is "cannot
  tell". `--session ID` for runs by hand.

### Install health and guards

- **`/gt:gt-doctor` says which repo a repo-scoped command will hit.** gt makes the vault the
  working directory, and the vault is a git repo, so `/security-review`, `/code-review`, a test
  runner — anything that says "the current branch" — resolves to the vault and finds a plausible
  answer there. On 2026-09-25 `/security-review` collected a 624 KB vault diff for a 174-line code
  change, without an error. New row **`repo-target`**: the working directory, the repo it
  resolves to, and whether that is the vault. It is a new **note** mark (`i`): never a finding,
  never raises the exit code, because cwd == vault is the normal configuration. `/gt:gt-open`
  says the same once in its summary.
- **`/gt:gt-doctor` checks hook entries against Claude Code's events and tools.** `wiring` could
  not see an entry that is present, correctly pointed, and never fires because its event or
  matcher names something Claude Code does not have. New row **`hooks-schema`** reads every
  `settings.json` hook entry (gt's and anyone's) and warns on `hooks-unknown-event`,
  `hooks-unknown-tool` (a plain tool name on a tool event; patterns are not judged) and
  `hooks-missing-file` (a command pointing into gt's hooks dir at a file that is not there). The
  allowlists ship as `hooks/known_events.json` and `hooks/known_tools.json`.
- **A commit or push inside another machine's checkout is refused.** On 2026-09-11 a session asked
  to "push everything" committed a release into a checkout a second machine owns, then — when the
  push failed for want of a credential never meant to be there — began rewriting the remote URL.
  The rule had been written down three times; prose cannot fire at the moment of the mistake. New
  `PreToolUse` guard **`guard_foreign_checkout.sh`** (installed by `install.sh`) denies
  `git commit`/`git push` inside a checkout you have **declared** foreign in
  `~/.claude/vault-config.json` (`foreign_checkouts`, or `guard_foreign_checkout.py add <path>
  --label L --route R`), naming the owner and the supported route. Never inferred from a path,
  remote or hostname; with nothing declared it denies nothing; it fails open on anything it cannot
  analyse. One visible exception: `GT_FOREIGN_CHECKOUT=allow git push …`. Setting
  **`foreign_checkout_guard`** (`on`).

### Checks that modules contribute, and fixes only gt writes

**What.** A validation host, `gt_check.py`. A module contributes **checkers** through a new
`checkers` key in `module.json` (id, script, globs / mime / events, `requires_tools`, timeout,
`fixes`, `rules`, summary — every key closed). `gt_check.py list [--for PATH]` shows each installed
checker, its module, what it applies to, whether its tools are present, and "not first-party" when
its script does not match the release MANIFEST. `gt_check.py run [PATH … | --staged]` runs every
checker that applies, in parallel, and prints one report; `--event commit-msg --message-file F`
runs commit-message checkers. Every checker returns the same small JSON: `pass`, `fail` or
`cannot-check`. **`cannot-check` never counts as a pass** — a missing tool, a timeout (the process
group is killed) or malformed output is `cannot-check`, with the reason, and the run exits 1; exit
3 means nothing applied, which is not the same as clean. Checkers run on a snapshot outside the
repo and vault, and any change to the snapshot is a `fail`. A run records each file's hash and
verdict (`~/.claude/golden-thread/check-runs.jsonl`); with the new setting **`commit_checks`**
(`off` by default) the existing commit guard refuses a commit whose staged bytes no passing run
covers, naming the file and the checker. `/gt:gt-validate` asks `gt_check.py list --for` first and
lets a checker settle a mechanical claim. `/gt:gt-doctor` gains a `checkers` row.

**Fixes go through one writer.** A checker may return **proposals** — a unified diff against the
exact bytes it examined, with the finding and a reason — and `gt_apply.py` is the only thing that
writes them. Setting **`addon_fixes`**: `off` discards them, `propose` (default) keeps them for
`gt_apply.py apply <id>`, `apply` applies after a run. An apply is refused, the file left
byte-identical and the reason recorded, when: the module's `fixes` grant does not cover the file or
its findings did not name it; the path is protected (`core-rules/`, `global-memory/`, `Sources/`,
`.git/`, `.githooks/`, `~/.claude`, the plugin source) whatever the grant; the diff no longer
applies; another live session claims the vault file. Hardened in the same path
(`2026-09-15-addon-fix-gatekeeper-hardening`):

- **Compare-and-swap at write time**, under one lock per file, so two racing applies write at most
  once — for repo files too, which session claims never covered.
- **An independent re-check** on a staged copy: every installed checker that applies runs on the
  original and the fix; refused if any that passed now fails, and the proposer's own finding must be
  gone (else `did-not-fix`).
- **Content rules, whatever the grant:** one in-place edit of one existing file (no create, delete,
  rename, mode change, symlink, binary or whole-file replace); no added bidi-control or zero-width
  characters; no added text matching the `secrets` slot or a scrub term (never printed); a size
  change within **`addon_fix_size_limit`** (`16k`); under `"rules": ["html"]` no new `<script>`,
  inline event attribute, or URL to a host the file did not already name.
- **Trust tiers:** only a first-party checker (script hash equals the MANIFEST row) may auto-apply
  under `addon_fixes apply`; anything else is capped at propose, and the output says so.

The original bytes are kept (`gt_apply.py undo <id>` restores them exactly), the fix is left
uncommitted for the commit gate to re-check, vault Markdown goes through the write queue, and each
apply, refusal, rollback and undo is one `addon.fix` event (a new kind; gt-flow 0.18.1 draws it).

**Why.** Mechanical checks — valid HTML, labels, a formatter, a commit-message convention — had
nowhere to live: each would have been its own hook with its own matching and output, and nothing
told a user which checks were installed or why a commit was refused. And letting each add-on write
its own fixes would give every add-on its own backups and claims handling, and one careless add-on
could overwrite a live session's work or touch `core-rules/`. The hardening exists because the
first design trusted session claims (vault only), let the add-on grade its own fix, checked only
*where* and never *what*, and put no ceiling on automatic application.

### Model intent, not model name

**What.** A skill or agent may declare `model_intent: fast | balanced | deep`. `gt_model.py`
resolves it through a new registry slot, `model` (Tier D), whose shipped rows live in
`packs/core/model.intents.pack.json` — fast → haiku, balanced → sonnet, deep → opus, Claude Code's
subagent model aliases, each row stamped `verified: 2026-10-01`. A local pack in the vault overrides
any row (the core row reported SHADOWED). Every resolution names the model; an unmapped intent is
reported UNMAPPED and runs at `balanced`; an unknown value is refused by the skill lint, by
`gt_model.py check` and by `dev/submissions.py` (packs, and the new `submissions.py skill
<SKILL.md>`). A skill that declares nothing runs on the session's own model, as before.
`gt_code_review.py plan` states each dimension's resolved model; `/gt:gt-validate` declares `deep`.
The session member's price table moved out of code into `scripts/gt_model_prices.json`, so no
script names a model; that file and the pack are the only places a test allows a model id.

**Why.** A ten-agent sweep inherited the interactive session's model. And a literal model id in
shipped content ages on someone else's schedule and fails at use time, invisibly — the same
"constant from memory reads as evidence" defect as the old SPDX list, which is why every row carries
the date it was verified.

### Across machines and outside sessions

- **`/gt:gt-sync`.** The vault is a git repository so that what one machine learns reaches the
  others, but nothing in gt moved it, and a session could open a project from files another machine
  had already superseded. `gt_sync.py status` (fetch, bounded; ahead, behind, uncommitted, fetch
  age), `pull` (fetch, then `git pull --ff-only`; on divergence it **stops** before git does
  anything — gt never merges or rebases the vault), `push` (only when the push check reports the
  vault ahead of a real upstream, and refused when origin has commits this machine lacks).
  Fetches never prompt and every git call has a timeout. Setting **`sync_check`** (`off`; `cached`
  compares with the refs on disk, `fetch` runs one 4-second fetch) adds a "vault is BEHIND" line to
  the existing push-check report at session start — no new hook registration.
- **Reminders.** The MUST DO block reaches you only when a session starts; a credential rotation
  once sat 15 days overdue that way. `gt_reminder.py` adds three push channels, **each off by
  default**, each with `setup <channel>` text and a `check <channel>` that sends a real test and
  says DELIVERED only on positive evidence: a macOS notification (**`reminder_macos`**), SMS or
  Discord through your notification relay (**`reminder_relay`** `sms`/`discord`), and email over
  SMTP (**`reminder_email`**). **`reminder_days`** (7) sets the window; overdue items are always
  included. Credentials are read at send time from a mode-600 file your secrets store wrote, and a
  looser file is refused unread. A new scheduled job, `reminder` (daily 08:30), installs only
  when its preflight passes. **Designed around macOS TCC:** a launchd job is refused access to
  `~/Library/CloudStorage` — how the weekly lint died for three Mondays — so the job never reads the
  vault. It reads a mode-600 mirror of `deadlines.md`, refreshed by `gt_reminder.py mirror` and at
  session start by `gt_surface` (when a push channel is on or a mirror exists); countdowns are
  computed at send time, so an old mirror only misses rows added since. Full Disk Access for python
  and moving the vault were both rejected. `import-tsv` folds an old `label<TAB>date` list into
  `deadlines.md` through the queue, so there stays one list.

### Batches that resume

**What.** `/gt:gt-scan` and `/gt:gt-ingest` restarted from item 1 after a context limit, a cancel or
a crash, and a session that ran out of context could not hand its place to the next. New
`gt_checkpoint.py`: a checkpoint is JSON written by atomic rename to the vault spool
(`Projects/golden-thread/spool/<tool>/`, else `~/.claude/golden-thread/spool/`), and `find` ignores
the session id, so **any later session can resume**. `gt_scan.py` checkpoints after each member
and the `language` member per file; `--resume <checkpoint>` skips what is done and merges the
earlier results into one report. `gt_ingest.py` checkpoints its candidate list: `--done CK --index
N --result …` records each migrated candidate in order, `--resume CK` prints what remains, and the
last one prints every result merged and deletes the checkpoint. The skills ask "A previous scan was
interrupted at item N of M. Resume it?" A run without `--resume` starts fresh. Checkpoints over 7
days old are pruned at every session registration.

**Why.** The triage made cross-session resume the requirement — a checkpoint only one session can
read misses the case that motivated the request.

### A release pipeline for every project

**What.** New `gt_pipeline.py` gives any code repository a **release pipeline**:
`release-pipeline.tsv` in the code root (one TAB-separated row per step — id, `step` or `gate`, a
one-line guarantee, the command, what it runs after, required or optional, `default` or `user`) and
a `release.sh` **generated** from it by `gt_recipe.py`; `check` fails while the two disagree. The
default steps: `@tests` (suite plus receipt), `@allin`, `@branch` (never the default branch),
`@install` (install plus post-install validation — it **fails** in a repo with an `install.sh` until
its real command is declared, because a gate that cannot run is not a pass), `@owner-gate` (stops
until `--go`), `@push`, `@sync` (not applicable until a downstream copy is declared). A step exiting
99 is SKIP-not-applicable, never a pass. `release.sh` stops at the first failed required step and
always says how many steps ran and which. `add` inserts a user gate at a stated position; `check`
refuses a cycle, an `after` naming no step and a command that does not exist; removing a default
gate needs `--reason`, recorded in the steps file. `init` adopts a pipeline in an existing repo
without overwriting anything (a foreign `release.sh` stays; the generated script becomes
`release-pipeline.sh`).

Projects declare it in README frontmatter, `release_pipeline: yes | no | planned`;
`vault_init.py create-project --release-pipeline yes --project-dir <code root>` scaffolds it and
records it in `source.md`, and `/gt:gt-create` asks whether the project ships code. New gt_lint
check **`release-pipeline`** (19 → 24 with the four above): a project with code marked `no`, a `yes`
with no `release.sh`, or a bad value. The gt-upgrade migration `release-pipeline-flag` records
`planned` on every README that lacks the key, through the write queue. `/gt:gt-allin` gains a
`pipeline` member — only in a repo that adopted one — running `release.sh --until owner-gate`, and
`/gt:gt-allin-commit` refuses on a failed pre-commit step even with `--allow-findings`.

**Why.** Every miss in the 0.17.11 release was a step that lived only in someone's head: a module
left off the release branch, post-install validation added only when asked, `copygt.sh` forgotten,
six scripts committed without the executable bit. gt's own `dev/publish.sh` showed the model that
works — steps as data, run in order, stop at the first failure, no skip — and no other project had
it.

### A faster build loop, and measured execution

**What.**

- **Recipes, not hand-written scripts.** `gt_recipe.py` renders a finished `.sh` from a short
  recipe through one tested template — dependency order, ok/FAIL/SKIP per step, stop at the first
  required failure, the count of what ran, `--until`/`--from`/`--list`/`--dry-run`. A steps table
  (`*.tsv`) renders a report-mode runner (`release.sh`); a script recipe renders a plain script.
  `check` catches a hand edit. `dev/copygt.sh` (~1,050 hand-written lines) is now generated from
  `dev/copygt.recipe`, with identical behaviour.
- **Scoped test receipts.** `tests/run.sh --affected` runs only the test modules that name the
  files changed on the branch (an unmapped code change, or a harness or installer change, selects
  the full suite) and records a **scoped** receipt naming those files. The commit guard accepts it
  on a branch that is not the default branch (setting **`scoped_receipts`**, `on`); the default
  branch and every release gate still need a full-suite receipt. `dev/remote-test.sh --affected`
  does the same on the runner.
- **A cached test install** — the harness builds an installed machine once per run and copies it
  into each test's sandbox; a test proves the copy equals a fresh install.
- **Load-aware workers.** `gt_load.py` sizes a parallel run from the machine's load, memory
  pressure and other parallel runs, not only its core count; `prun.py` shrinks its pool when units
  run more than twice their recorded time and starts the heaviest units first.
- **Remote runners.** `dev/remote-test.sh` takes its host from `--host`, `$GT_REMOTE_TEST_HOST`, or
  the first of the **`runners`** setting (empty by default). `prun.py --hosts a,b[,local]` splits
  units across several runners by measured capacity; an unreachable runner is SKIPPED and its units
  go to the others — a unit no host reported is a failure, never a pass.
- **`gt_bench.py`** measures the parallel profile instead of guessing it: a bounded calibration
  (180 s by default) at several widths, taking the knee — the smallest width reaching 90% of the
  best throughput — and writing `parallel_profile` with `source: measured`. install.sh had written
  `io_max = 2 × cores`, never measured. `--hosts` calibrates each runner.
- **`gt_metrics.py`** records one row per execution — tests, every release-pipeline step,
  `remote-test`, `/gt:gt-allin` — per project, per machine, per month
  (`<vault>/Projects/<slug>/metrics/`), with arguments hashed and never stored and credential-shaped
  strings stored only as hashes. Each process has a rolling baseline (median and p90 of its last 20
  passing runs); a newest run above max(p90, 1.25 × median) is a **regression**, reported with what
  changed since. `peers` compares like processes across projects; `mark` and `verify` record an
  applied optimisation and whether it paid. Setting **`execution_metrics`** (`on`).
- **gt-optimize's opt-in `execution` member** (`gt_optimize_exec.py`, run by
  `gt_optimize.py --only execution` or the `--execution` alias; a bare run stays vault + session)
  ranks serial bottlenecks, redundant full-suite runs, slow steps and units, regressions, much
  cheaper peers, unmeasured defaults and hand-written step scripts a recipe could generate — by
  measured cost, with the expected saving. `--apply ID`, after a yes, makes one finding's change and
  marks it for `verify`.
- **`/gt:gt-doctor`** gains an `execution` row: the profile measured or default and its age, a shell
  translated by Rosetta, test temp files in a synced folder. Setting **`test_tmpdir`** (`off`;
  `noindex`) moves the test runner's temp files out of watched folders.

**Why.** The 0.17.11 release ran the ~2,400-test suite about eight times in one day, including for
single-tool changes; a local run at load ~50 dropped the owner's keystrokes; and every "it would be
faster if" was a guess. Owner, 2026-10-01: *"It is about all processes inside of how a dev works …
capture metrics on different executions across the board."* Measured: the full suite on an 8-core
runner, 2,582 tests in 217.6 s; the cached install took one test module from 45.2 s to 30.8 s
serially. A first load-aware formula started one worker at load 6.6 on 8 cores and took the suite
1,195 s; the shipped formula scales by 1/overload² only above one runnable process per core.

### Ingest and promote as staged pipelines

**What.** 0.17.10 gave every agent job one spec per kind of material, and each did a whole ingest
in one job, so nothing could be handed off or run side by side. Ingest is now **intake-scan →
survey → extract → classify → reconcile → draft**, and promote is **scan → verify → generalize →
place → owner approves**. The kind (code, docs, tool, session, wiki) is a parameter: one spec per
stage in `templates/agent-specs/stages/`, one small delta per kind in `kinds/`, which can add prompt
lines and fields but never change the executor, the intake-scan requirement or the context
strategy. The new `gt_ingest_pipeline.py` runs every step that is not an agent: `survey` (intake scan
and a split into units, one per top-level folder; `--deeper`, `--unit-depth`, saved with
`--record`), `packet` (exactly one result packet per unit, so parallel extracts never collide),
`fan-in`, `reconcile` (dedupe, and compare with facts already in gt — a contradiction **stops** the
run and that finding is never written), `draft` (through the write queue, no approval prompt),
`promote-scan`, `promote-plan` (ends at `awaiting-owner`; **no command applies a plan**), `status`.
Ingest stops for the owner only on a contradiction, a security issue or unsafe code, and a stop
names the kind and the place, never the content. **`/gt:gt-work` is now the session kind** of the
same pipeline: scan, segment, extract, reconcile, draft. `agent_specialization` still gates the
agent stages; the skeptic is now the `reconcile-session` stage and is still decided by
`skeptic_pass` alone. The five 0.17.10 spec names stay as aliases for one release.

**Why.** The owner asked for small jobs that can be handed off and run side by side; a stage that
reads only the previous stage's packet is stateless and can go to a fresh agent.

### Repository tooling (not installed)

- **`dev/remote-test.sh`** runs the full suite on a Linux VM over ssh and records the receipt on the
  Mac only when the tree that passed is still the tree on disk. Measured 2026-10-01: **2,487 tests
  in 214 s remotely, against 892 s on the Mac**, where the run drove the load average to 50–96 and
  dropped keystrokes. Three tests were made portable for it (the ast-grep tier no longer mistakes
  Linux's `sg` for ast-grep; the LOTR consent test injects its dialog; the runner uses
  `umask 022`).
- **`dev/copygt.sh`** takes gt-src to a validated, committed install on the receiving machine in
  one command: verify against `SHA256SUMS`, mirror exactly (deleting what gt-src no longer
  carries), `install.sh`, validate (`validate-install.py`, which reuses `gt_doctor.py
  post-install`), and commit only when the report is clean. It never pushes. gt-src no longer
  carries `*.code-workspace`, `BUILD-NOTE.md`, either `CLAUDE.md`, `dev/` or `SUBMISSIONS.md`.
  *Why:* a hand copy only adds, so a file removed here lived on there.
- **`dev/feature_requests.py --src`** descends into `golden-thread-plugin/` when it names a gt-src
  in the repository layout.

### "Flakes" that were bugs

Two failures were filed as load flakes during this release and were not:

- **`grep -q` behind a pipe, under `pipefail`.** `install.sh`, `package.sh`, `selftest.sh` and
  `dev/release-check.sh` matched output with `printf "$out" | grep -q PATTERN`. `grep -q` exits on
  its first match; the writer can then die of SIGPIPE, `pipefail` makes the pipeline fail, and a
  line that IS present reads as absent. On a real install that meant *"Vault upgrades: the check
  could not run (unrecognised output)"* -- the vault's pending upgrade silently skipped -- and
  `package.sh` occasionally claimed there was no gt version directory. Every such match is now a
  here-string (`grep -q PATTERN <<<"$out"`), and `tests/test_no_sigpipe_grep_q.py` refuses the
  pipe shape in any pipefail script of the newest releases.
- **Backups stamped to the second.** `gt_upgrade`'s vault tarball (`vault-YYYYmmdd-HHMMSS`) and
  its `.base.` snapshots were named to the second, so two upgrades within one second replaced the
  earlier backup. Names now count up (`-2`, `-3`, ...) instead (`test_upgrade_backup_names.py`).

### Known, and not fixed

- **The deprecated aliases are equivalent by construction, not by measurement**: each follows the
  new skill's section, and a test asserts every command the 0.17.11 skill ran appears verbatim
  there; no model was run on both. For that parity test gt-open's project steps keep the old
  names; every other shipped text names the new verbs.
- **A handoff close is recorded as event kind `retire`** with a note beginning `handoff.close`,
  not a new kind: event schema v1 refuses unknown kinds and gt-flow maps each kind to a family.
- **After `gt-close --move`** the project's decisions spool stays at `spool/decisions/<slug>`; a
  future project reusing the slug continues its numbering. gt-lint and `gt_tasks` do not look
  under `Archive/`, by intent. The folder move itself is a rename guarded by a claim check; only
  the reference rewrites go through the queue.
- **No script enforces the plan gate.** `gt-implement`'s refusal is skill text checking
  `status: approved`; `.claude/gt-plan-current.md` is not added to `.gitignore`.
- **Other tools still join `Projects/<slug>` directly** (`gt_task.py`, `gt_handoff.py`,
  `gt_demote.py`), so a bare sub-project slug fails there; `gt_spool.resolve_project` is the
  helper to switch them to. gt-lint does not flag the stray README-less folder the ADR bug left,
  nor a pre-0.18 sub-project CLAUDE.md with the wrong path.
- **What changed this session counts committed changes only**, vault-wide: another session's
  commits in the same window appear too, and sessions registered before 0.18.1 have no
  `start_commit` (pass `--since-commit`).
- **The catch-up brief's "absence" is per machine.** A `brief_absence_days` value outside its list
  falls back to 7.
- **`gt_state`'s first turn in a session is usually "cannot tell"**: the usage ledger records about
  once a minute, so there is no reading of this session's own yet. That is the correct answer.
- **The hook allowlists are a snapshot.** They were first written from memory and then checked
  against Claude Code's hooks and tools documentation before release (33 events, 48 tools; six
  names that are not tools removed). An event Claude Code adds later shows as
  `hooks-unknown-event` until the list is updated; review both files each release.
- **The foreign-checkout guard sees literal `git commit`/`git push` only** — a push run through a
  script or alias is not seen, and an unquoted `$(...)` fails open by design.
- **Contradiction and promotion detection are keyword rules**: a paraphrased contradiction is
  missed; `global-scope-leak` matches a slug as a substring (`alpha` hits `alphabet`).
- **The Knowledge read log records `trigger: "read"` only** (the hook cannot see which skill caused
  the Read); reads by shell or by a subagent outside the hook are not logged; the log is local.
- **`--archive` cannot rewrite a remaining `research.md` over 256 KiB** in one queue request; pick
  an earlier cutoff (the refusal says why; nothing is lost).
- **`gt_minimize`'s breakdown is chars/4**, an estimate, labelled so; only the billed context is
  measured. It cannot run `/compact` itself.
- **Pinning a research finding** means adding `[pinned]` to its existing `##` heading — a narrow
  exception to "append-only" that PROTOCOL does not yet word.

- **Checker `commit-msg` events are not run at commit time**: the commit guard reads file receipts
  only, and nothing calls `--event commit-msg` yet (a git `commit-msg` hook would). `selftest.sh`
  did not gain a cold-install checker step (editing it would need an installer bump); the same
  assertion lives in `tests/test_gt_check.py`. No checkers ship — each is its own module request.
- **A vault whose `tools/gt_events.py` predates 0.18.1 refuses to merge an `addon.fix` event**
  until `/gt:gt-upgrade` refreshes it. Proposals (`ext-proposals/*.json`) are written directly, not
  through the queue: they are JSON, not Markdown.
- **`agent-specs`' `model_tier` and `model_intent` are separate vocabularies**; not unified.
- **Review stamps record a read, not a verification**: a session that reads a wrong page resets
  its clock. The standalone gt-wiki skills do not stamp. Each stamp is a vault write (a git line),
  at most once a page a day.
- **Link suggestions are lexical** (tags and title words; no synonyms), and gt-promote does not run
  the pass yet.
- **`gt-sync push` runs no credential scan of its own** — it relies on the vault's pre-commit gate.
  One remote only; gt does not stash for a `pull` that touches edited files.
- **Reminders are not proven through launchd in this build.** The tests stop at `launchctl`; the
  proof is `gt_schedule.py install reminder` on the Mac, which sends a real reminder if anything is
  due. A macOS notification appears as Script Editor, and "DELIVERED" means `osascript` exited 0. No
  dedupe across days: an overdue item nags daily until its row is deleted.
- **A resumed language scan refuses** when the files it had done are no longer the first of the
  walk (after a rename); the `code` member resumes per member, not per file.
- **gt-flow's redacted-hash widening depends on order** within one render: the same name may show a
  different width in two renders. Hashes are now at least 6 hex characters (a 20-name cross-render
  collision ~1 in 42,000, from ~1 in 1,024 at 4).
- **`gt_task.py list` and the rollup see two project levels only**; a third-level sub-project's
  tasks are not listed.
- **The five 0.17.10 agent spec files remain** in `templates/agent-specs/` (`ingest-code`,
  `ingest-docs`, `ingest-tool`, `validate`, `skeptic`). The loader ignores them; deleting them is
  the owner's decision.
- **`gt_secrets.py` on the Mac flags `scripts/gt_metrics.py:451`** (a `tokens=` keyword argument) as
  an assigned credential — a false positive that blocks a local full-run receipt until the owner
  baselines it. Not reshaped to dodge the scanner.
- **Execution metrics are partial.** Token cost per workflow is recordable (`--tokens`) but nothing
  feeds it; no before/after timing of the whole 0.17.11 workflow was re-run; `gt_bench` calibrates on
  a synthetic workload only, not your own test command; there is no weekly execution worker
  (`--only execution` is on demand); nothing prunes old metrics months.
- **gt's own repository has not adopted a release pipeline** (`dev/publish.sh` remains its release
  sequence). A `yes` project's `release.sh` is found through the absolute path in `source.md`, so a
  vault synced to another machine may report it missing there.
- **The cached test install is used only by `test_gt_doctor_postinstall`**; tests whose subject is
  the install cannot use it. Rosetta is detected, not fixed. `prun.py --hosts` records no receipt.
- **Script-side contradiction detection in the ingest pipeline is narrow** (same statement, a
  different figure or flipped polarity); anything subtler needs the `reconcile-<kind>` agent. A
  saved unit split is keyed by folder name unless `--repo-key` is given.

### Measured, and deliberately not built

- **`bundled-concept` on the owner's vault: 38 pages fire** (read-only run, 2026-10-01). Many are
  narrative pages whose headings are structural; expect to suppress a share on first sight.
- **`decision-candidate` on the owner's vault: 114 findings** — 92 "deliberately", 21 "by design",
  1 "this is intentional". `gt_settings.py set decision_signals "-deliberately"` cuts it to 22. The
  request's list was kept as specified.
- **`memory-entity-orphan` without an adoption gate produced 129 findings**, one per memory note,
  on a vault where no note declares `entities:` yet. So ALLCAPS and identifier-shaped names count
  only in a project where some note already declares the field; until then the check reports 0.
- **The session member's duplicate rows:** one API request is written as several transcript rows;
  keyed on timestamp the prototype inflated turns 2.2x and reported invalidation at 50.7% instead of
  3.4%. It dedups on request id + message id.
- **Not built:** gt-minimize's in-flight tier, its PreCompact capture hook and a size-watch prompt
  (owner decision; gt-usage's status line already says when cutting is cheap); a gt-learn skill
  (folded into gt-work); gt-optimize's allowance-window thresholds (gt-usage ships them); a new
  cross-project duplicate detector (gt-optimize's `duplicate-fact` and the promotion detector cover
  it); the request's per-task "closed on time" note in gt-close; a `handoff.close` event kind.

---

## gt 0.17.11 — 2026-10-01

One owner ruling drives most of this release: *"the agents write to the queues rather than directly
to the files"* (2026-10-01). Core rule 1 becomes queue-first, the queue gains the two operations
the skills and scripts needed to use it for everything, and they now do. Also: the daily note puts
each fact under its heading, and a new optional module, **gt-lotr** (LOTR). Module versions:
gt-wiki 0.2.4; gt-farm, gt-watch, gt-demo, gt-flow and gt-report-card 0.17.11; gt-visualize 0.4.1;
gt-usage 0.1.3 unchanged; gt-lotr 0.1.0 new.

### Core rule 1 is queue-first

**What.** The rule id is unchanged (`core_concurrent_session_claim`); its meaning is not. It now
reads: *write vault content only through the write queue (`gt_write_queue.py`), then apply it with
`gt_broker.py drain`; never edit a vault file directly, and never write one another live session
has claimed.* The `guard_session_claims` hook enforces it:

- **Denied:** a direct `Write`/`Edit` to any vault `.md`, claimed or not, outside `Sources/`,
  `core-rules/`, `.obsidian/`, `.git/`, `.gt/` and gt's own `spool/`, `sessions/` and `tools/` —
  with the exact queue command in the reason. Also denied: the visible shell writes into the vault
  — `>`, `>>`, `tee`, `sed -i`, and `cp`/`mv`.
- **Not denied, because not visible:** a script that opens a file itself, the owner's own edits in
  Obsidian, and files outside the vault. The rule says so; that part of it is a reminder, not
  validated.
- **Held, not failed:** a write to a file another live session has claimed stays queued for the
  next drain; session start shows "WRITE QUEUE: N waiting". Claims still exist — they are what the
  broker honours.
- **Every `design.md` and `global-memory/` write goes to the owner** for review — the owner's
  choice ("queue everything").

**Why.** "Claim, then write" depended on every writer remembering to claim, and on the write
depending on the claim. On 2026-10-01 it failed twice in one hour: a Bash append went through a
live claim (the guard never read Bash), and a refused `claim` printed CONFLICT but the next command
was not chained to it and wrote anyway. A queued request never touches its target, so the safe path
is the only one. Run `/gt:gt-upgrade` after installing: an old vault copy of the rule still says
"claim, then write" until it does.

### Two new queue operations, and an audit

**What.** `gt_write_queue.py` gains `set-property` (one top-level frontmatter key, `--key`/`--value`)
and `replace-file` (the whole file, guarded by the hash of what was read), beside `append`,
`replace-section` and `create`. The broker now escalates instead of writing when a target was moved
or deleted after the request was queued — it never recreates a file at its old path — as it already
did for a target that changed. `gt_broker.py audit [--since H]` lists vault `.md` files changed in
the window that the broker did not write; it is a report, not an alarm, and the owner's own edits
appear in it. `gt_write_queue.py`, `gt_broker.py` and `gt_demote.py` are now installed into the
hooks dir.

**Why.** With only `append`, `replace-section` and `create`, a writer that changes one
frontmatter key (a handoff's status) or rewrites a file whole (the daily note) had no queued path,
so the rule could not cover every writer. The audit is the check on what the guard cannot see; the hooks-dir install is what lets
the nightly jobs and the vault's `gt_task.py` reach the queue at all (without it the 22:00
daily-note job exits 3, "write queue is not installed").

### Skills, modules and scripts write through the queue

**What.** gt-work and fifteen other core skills, and the module skills (gt-wiki, gt-farm,
gt-watch, gt-demo), queue their writes and drain once. The scripts `gt_daily`, `gt_task`,
`gt_lint_weekly`, `gt_handoff`, `gt_handoff_status` and `wiki_log` route their vault writes the
same way; in a gt vault `wiki_log.py` uses `gt_log.py` and the queue (gt-wiki 0.2.4). The gt-demo
fixes ride along: demo act 4 and the demo's ADR numbering. `log.md`, `decisions.md` and `TASKS.md`
keep their own tools. gt-flow and gt-report-card move with gt unchanged; gt-visualize 0.4.1 is
wording only.

**Why.** A rule the shipped tools break is a rule nobody can follow. A script that edited a claimed
file because it was not the Write tool would disarm Core rule 1 by the side door.

### The daily note puts each fact where it belongs

`gt_daily.py` wrote the whole day into one
block at the bottom of `Daily Notes/<date>.md`, so `## Did`, `## Decided` and `## Open at end of
day` stayed empty however much happened ("it did not put the information into the proper
locations. It just appended to the bottom" — owner, 2026-10-01). Now new projects, tasks closed and
commits go under `## Did`; ADRs added that day under `## Decided`; tasks added and open ones due
today under `## Open at end of day`. Each sits in its own marked block after the owner's lines and
is replaced whole on a re-run; the owner can keep writing above it. Totals, event counts, the
active span and notes stay in the footer block. `## Noticed` is still never written to — it is what
`gt-review` sweeps. A missing heading is added; a note from an earlier release is rearranged on its
next run.

ADRs are new to the note: read from the git diff of every `Projects/**/decisions.md`, like tasks,
not from `adr` events — on 2026-09-30 the diff found 7 ADRs where the event log had 4. A reworded
ADR (same number, new title) is not counted. `--dry-run` now prints the whole note as it would be
written, so the placement is visible.

`gt_daily` now writes the note through the queue as one `replace-file` per run. The three
`test_gt_daily` tests that pin the replace-never-append contract pass unchanged, byte-identical to
their text in commit `22fb651` — the release gate for this change.

### New optional module: gt-lotr 0.1.0 (LOTR, also called gt MCP)

**What.** One gateway to rule them all: a fixed four-tool MCP surface (`find`, `call_read`,
`call_write`, `call_consent`) in front of any number of downstream connections — GitHub, Jira
(Cloud and Data Center), Microsoft Graph and other REST APIs — with a registry of who talks to whom,
as whom, and from which machine. Skill `/gt-lotr:gt-lotr`; CLI `lotr` (also `mcp`), daemon
`lotrd.py`, stdio shim `lotr_mcp.py`. Stdlib-only Python 3.9+. Placement per zone: `local`, or `hub`
with enrolled `client` machines (hybrid is parsed and refused). Local security: private files, a
kernel peer-uid check on the socket, TLS required beyond loopback, local callers under an allow list
and a tier ceiling, and consent-tier operations confirmed in a dialog the daemon raises itself.
Credentials are references (keychain, store, file), never values; results are marked untrusted and
credential-shaped text is withheld. **Off by default** — `./install.sh --with lotr`.

**Why.** One MCP server per system puts hundreds of operations and a token per server into every
session, and leaves the assistant deciding what is risky. A small fixed surface keeps the context
small and moves the risk decision to the gateway, where the owner sets it. Design and decisions:
vault `Projects/golden-thread/mcp-gateway/`, ADR-1..6.

### Repository: workspace files untracked, a fuller `.gitignore`

**What.** The editor workspace file is no longer tracked and `*.code-workspace` is ignored. The
root `.gitignore` gains editor and IDE files, OS litter, Python caches and virtualenvs, logs, temp
and patch leftovers, Dropbox conflict copies, and secret-shaped files (`.env`, `*.pem`, `*.key`,
`*.p12`, `*.pfx`). **Why.** Owner, 2026-10-01: a workspace file is per-machine, never part of the
project; and a secret-shaped file should be impossible to commit by accident (Core rule 9).
Checked against `git ls-files`: no tracked file matches a new rule.

### Known, and not fixed

- **No delete or move queue op.** `gt_demote.py`, and demoting a Core rule out of `core-rules/`,
  still delete or move files directly.
- **The broker's own `#conflict` task is written directly**, while it holds the drain lock — the
  one remaining direct vault write by a script, after the same claim check.
- **`until: none`** in a handoff's frontmatter means "no deferral": the queue cannot delete a
  frontmatter key.
- **Two machines, one Dropbox-synced queue are not atomically locked.** The drain lock is a file in
  the vault; two machines draining in the same few seconds could in principle both apply one
  request. Drain from one machine. Not yet measured.
- **A script that opens a vault file itself is invisible to the guard.** Only the visible shell
  writes are denied; the audit is the after-the-fact check.

---

## gt-visualize 0.4.0 — 2026-09-30

**A guided walkthrough of any explainer** — feature request `2026-09-30-gt-visualize-guided-walkthrough`
(re-filed in the queue's format from `2026-09-30-gt-visualize-altitude-verbs`). Four skills:
`/gt-visualize:tour` (scene by scene with a prediction pause, progress saved per project and marked
private), `/gt-visualize:whatis` (one part at its own level of detail), `/gt-visualize:trace` (a scene
run forward hop by hop on a real test, or a labelled STATIC walkthrough — never invented values; a
scenario test only on your yes) and `/gt-visualize:explain-back` (opt-in questions on the story's
seams). New script commands `story scene|part|questions` and `tour-state get|set` (claim-before-write,
`--dry-run`). Built in gt's own terms; it depends on no other plugin. gt stays at 0.17.10.

Thanks to **Sergey Kryvets**, who recommended adding a visualization tool to Golden Thread in the
first place — the idea that became gt-visualize.

---

## gt 0.17.10 — 2026-09-30

Four owner requests: queued vault writes for concurrent agents, specialist agents by job type,
self-posting release announcements, and safety rules for every ingest. The two agent features and
announcing are **off by default**.

### Queued vault writes — a write broker for concurrent agents

Feature request `2026-09-30-vault-write-broker-queue`. `gt_write_queue.py` queues a write (append,
replace-section, create) instead of making it; `gt_broker.py drain` applies the queue oldest first
and exits (no daemon, no hook); session start shows how many are waiting. Appends from any number of
sessions merge and near-duplicates are dropped; two sessions replacing one section differently — or
a section that changed after the replace was requested — write nothing: every version is saved in
`spool/broker/conflicts/` and a `#conflict` P1 task points at it. A claimed file stays queued (Core
rule 1); generated and protected files are refused; `design.md` and `global-memory/` always go to
the owner. Reconciled with ADR-8: this orders the owner's own sessions' writes and is not the
extension broker; a result from gt-farm (external text) may only create a new file.

### Specialist agents by job type (`agent_specialization`, `skeptic_pass`)

Feature request `2026-09-30-agent-specialization-by-job-type`. `gt_agent_spec.py` lists, validates,
resolves and renders job-type specs (`ingest-code`, `ingest-docs`, `ingest-tool`, `validate`,
`skeptic`) shipped in `templates/agent-specs/` (not `packs/`, which the release gate treats as
security-control input); a valid spec in `<vault>/Projects/golden-thread/packs/agent-specs/`
overrides one. With `agent_specialization` on, gt-ingest and gt-validate hand work to a specialist
agent (validate loads no vault context); with `skeptic_pass` also on, gt-work runs a skeptic over the
drafted findings. These job types are due to become stage × kind specs — request
`2026-09-30-ingest-promote-stage-pipeline`, which also carries gt-work's breakdown.

### Release announcements that post themselves (`release_announce`)

Feature request `2026-09-30-publish-auto-announce-release`. `publish.sh`'s `announced` step only
warned, and announcing was the step that got skipped. `off` (default) keeps the warning; `draft`
writes the post to `~/.claude/golden-thread/announce-<version>.md`; `post` creates it in
Announcements with `gh`. One post covers every release no Discussion names yet, built from this
CHANGELOG, scrubbed first (IPv4, home paths, scrub terms, credentials — a hit or a scrub that cannot
run posts nothing). Never announces a version twice; never fails a release.

### Ingest scans first, stops only when it must, and runs on Claude only

Feature request `2026-09-30-ingest-promote-stage-pipeline` (the owner's rulings, first part).
Before anything reads material for ingest, `/gt:gt-ingest` runs
`python3 <gt>/scripts/gt_intake_scan.py <project-dir>`: credentials (gt_secrets), unsafe source code
(a download piped into a shell, exec of fetched or decoded data, obfuscated blobs, destructive
filesystem commands, install hooks that fetch and run) and prompt injection (text telling an AI to
drop its instructions, fake role markers, hidden Unicode, instruction-bearing HTML comments).
Results are per unit — each top-level folder, plus `.` for root files; `--unit-depth 2` splits a
monorepo, `--unit U` scans one, `--json` gives the full report. A finding is kind, file, line and
rule id — the matched text is never printed. Exit 0 clean, 1 findings, 2 usage, 3 incomplete (a
scan that could not run is not a pass; unreadable formats such as PDF make a unit incomplete).
Ingest no longer asks for approval: it stops for the owner only on a contradiction with a fact
already in the vault, a security finding, or unsafe code, and writes nothing for a stopped unit. No
ingest stage may run through gt-farm or any non-Claude service — `gt_agent_spec.py validate`
refuses such a spec, and `gt_agent_spec.py render` refuses to produce an ingest prompt unless that
unit scans clean.

---

## gt 0.17.9 — 2026-09-30

Two owner requests: the daily note captures more of the day, and a gt-visualize page can be
published.

### The daily note (`gt_daily.py`)

Feature request `2026-09-30-gt-daily-enhancements-tasks-domain-new-project`.

- **Tasks added** today and **Due today (open)** — tasks marked `[due:: <today>]` still open when
  the job runs, read from the READMEs as they stand rather than from the diff.
- **Grouped by domain, then project** — every task list and the commits sit under `### <domain>`
  from each project's `domain:`, alphabetical, `uncategorized` last. A vault commit is filed under
  the project whose files it changed most.
- **New projects** — one line on a day a project README was created.
- **Push before writing** — a failed push, or a vault with no remote, becomes a `> NOTE` in the
  block and the write still happens. `--dry-run` never pushes; `--check` reports an unreachable
  remote.
- **Fixed on the way:** tasks were read from every file under `Projects/`, so a handoff's
  checklist ("Do the tests pass right now?") was reported as tasks closed. Only project
  `README.md` files are read now. An open task edited or moved during the day is not counted as
  added.

### Named sections, and email/Teams content off by default

Feature request `2026-09-30-gt-daily-comms-section`, redesigned with the owner the same morning:
instead of a fixed `none`/`m365`/`joule` setting, the owner **names** sections and other tools fill
them. gt_daily makes an empty, marked area for each in the daily note — which always lives in
`Daily Notes/` — and writes `Daily Notes/.handoff/<date>.md` saying where the note is and which
sections are waiting; it never overwrites a section, and with nothing open for filling there is no
handoff (a stale one is removed). gt_daily itself never reaches for mail, Teams or a network, so the
`m365` problem — a scheduled job cannot use a session's M365 tools — disappears: a session or Joule
is just another tool filling a section.

Sections and the comms policy live in the **shared vault** (`.gt/daily-sections.json`), because one
user works from several machines on one vault. **Email and Teams content is off by default**; a
`--comms` section is placed only while the vault's policy is `on` (`--comms-content on`) and the
machine does not force it off with the new gt setting `daily_comms_content` (`follow`/`off`). A
machine can tighten the policy, never loosen it: a shared vault is readable from every machine,
including a work one whose employer does not want such content anywhere Claude can read.

### gt-visualize 0.3.0 — publish

Feature request `2026-09-30-gt-visualize-default-publish-target`. `gt_visualize.py publish` puts a
rendered page where others can see it, after a **scrub gate** (credential scan, IPv4 addresses,
home-folder paths, the owner's scrub terms — findings are named, never their values) and a printed
plan the owner agrees to; nothing is published without `--yes`. Targets, chosen by the owner:
`local` (the default), `claude` (Claude publishes a private claude.ai Artifact; updates keep the
URL), `github-pages` (always public) and `gist` (secret or public; GitHub shows its HTML as
source, which the tool says every time). A visibility a target cannot enforce is refused. Two
settings, `visualize_publish` and `visualize_publish_visibility`; target details via
`targets set`; every publish recorded and listed by `publishes`. No credential is ever stored —
each target uses a login the owner already has.

The scrub gate strips the embedded three.js by its shape rather than by this module's copy: a
page rendered by an older gt-visualize carries an older bundle, and scanning minified three.js
reported 7 false "assigned credential" findings.

gt-visualize 0.2.0 and gt 0.17.8 stay as released; the gt-versioned modules move to 0.17.9
unchanged.

---

## gt 0.17.8 — 2026-09-29

**gt-visualize 0.2.0: explainers follow written rules instead of taste.** A story that put one or
two parts in focus in every scene — the first included — rendered "all zoomed in from the start",
because the camera framed only the focus. The shape is now fixed, in two layers.

**The page enforces framing itself (F1–F6)**, so no story can break it: it opens on an
establishing shot of the whole system; it frames everything a scene *shows*, with focus shown by
light rather than by zooming in; it never comes closer than 55% of the establishing shot; it keeps
one viewing angle; parts outside a scene stay as faint ghosts; and the last scene pulls back out.

**`--check` enforces the story (S1–S10)** and names the rule each problem breaks: scene 1 is the
overview (everything shown, no focus, no flows) and the last scene a recap of the whole; 4–10
scenes; 8–30 parts in 3–6 groups; labels of at most 24 characters; each middle scene shows at
least 3 parts and focuses 1–2 of them; at most 5 flows a scene, each with both ends shown; 1–2
paragraphs a scene; and every scene has a one-line **caption** of what the picture shows, rendered
under the scene as "On the stage: …". The skill teaches the same rules before the story is
written. A 0.1.0 story needs a caption per scene and a closing recap to pass.

Also: **three test classes about an ABSENT ast-grep failed on any machine that had it installed** — they built their PATH from the real one, and passed on the release machine only because ast-grep had never been installed there. A shared helper now removes ast-grep from the PATH those tests see (never `sg`, which on Linux is an unrelated system tool).

gt and the gt-versioned modules move to 0.17.8 with gt-visualize; their content is unchanged.

---

## gt 0.17.7 — 2026-09-29

Two things: the installer reports what it is doing, and a new module, **gt-visualize 0.1.0**. The installer work was first released this morning as 0.17.6 and withdrawn after 22 minutes; it ships here under a new number so no machine that installed the withdrawn 0.17.6 holds a different release of the same name.

### The installer is never silent

An install of 0.17.5 over a vault last installed at 0.9.4 sat
with no output while it worked — backing the vault up, then applying a long chain of upgrades — and
was stopped twice as hung. The owner's rule: *"not overly verbose, but no feedback is not
acceptable for an install."*

- **Every slow step announces itself before it starts** and reports how long it took: the checksum
  pass ("verifying 532 published files…"), the pre-write vault backup, the vault tool refresh, the
  upgrade check, the upgrade itself.
- **The vault upgrade counts.** It knows how many steps it will apply, so it says so up front and
  prints `[k/N]` as each starts, with its time when it ends; the backup it takes first names the
  file count and size.
- **A heartbeat for quiet steps:** any step silent for 15s prints `...still <what> (Ns)`.
- **Streamed, not captured:** the upgrade's output reaches the screen as it happens; before, the
  installer collected it and printed it only when everything was done.
- **The checksum runs after the installer's first line,** not before it: in 0.17.5 it hashed every
  file before saying anything, which on a cloud-synced gt-src first downloads every file.

A test asserts the announcements appear, in order, before the work they describe, and that each
step reports its time.

### gt-visualize 0.1.0 — a codebase in 3D

**A new module: a codebase in 3D.** `/gt-visualize:gt-visualize` has two modes. **explain**
renders a scroll-driven walkthrough of how a system's parts work together: a narrative column
beside a 3D stage whose scenes highlight parts and animate flows (data, rules, answers, and
requests stopped at a gate), from a story file Claude writes out of the code and the vault;
`--check` lists every problem and a story with any is never rendered. **render** draws any
source tree as one offline HTML file with three.js inlined — directories as
districts, files as buildings, height by lines, footprint by size, colour by language (gt's
`filetype` definitions) or by git churn. Orbit, hover, click to focus, search.
`--redact` hashes every name before sharing, and an `--out` inside the vault is refused.
The three.js bundle (r186.1, MIT) is built by esbuild with only the classes the page uses
and pinned by hash; a bundle that does not match is refused rather than inlined. Filed as
feature request `2026-09-29-gt-visualize-3d-codebase-view`.

---


## gt 0.17.5 — 2026-09-29

**Two fixes requested by the receiving machine after its first install from gt-src.**

### The component check survives a moved plugin source

The SessionStart component check is registered with the path of the source tree `install.sh`
ran from. When that tree moved — gt-src took the repository's layout in 0.17.3, so everything
went one level down — the check reported `badpath` and `no-manifest`: it checked nothing, and
said so only obliquely. It now falls back to the **installed** copy of the same release in the
plugin cache, which carries the same `MANIFEST.json`, compares against that, and says plainly
that the source is gone and that re-running `install.sh` from its new location re-points the
hook. With no installed copy either, it still reports `no-manifest` — nothing is guessed.

### An unregistered vault write is warned about when it happens

After `gt_session.py release`, or in a session that never registered, vault writes were
unattributed and unprotected, and the first anyone heard of it was the report card at session
end. The claims guard now emits a warning on such a write — naming the file and the
`gt_session.py register` command — without blocking it (the write still lands and is still
recorded as unattributed). A session whose id cannot be determined is not called unregistered.
`gt_session.py release` now prints the same reminder.

## gt 0.17.4 — 2026-09-29

**An install from gt-src — or from any git clone — refused to load gt's definitions.** Found on
the receiving machine the morning after 0.17.3 shipped.

`packs/community/` shipped **empty**. git does not track an empty directory, so the directory
existed on the publishing Mac and in nothing made through git: the publisher copies only tracked
files, so gt-src lacked it, and so would any repository that committed gt-src. `gt_registry`
treats a missing *release* pack directory as a fault, deliberately — renaming a release pack
directory is how a definitions slot can be emptied in silence — so every install from gt-src
failed while every install on the Mac, which reads its own working tree, passed. Neither the
selftest nor the publisher's verification exercised the registry from the published tree.

- `packs/community/README.md` now ships, so the directory survives git (the registry reads only
  `*.pack.json`, so this changes nothing it loads).
- The release gate fails on **any empty directory** in a release, with the reason, so the next
  one cannot ship. Shown failing with the README removed.

Nothing else changed; the five gt-versioned modules move to 0.17.4 with gt, content unchanged.

**And the check that would have caught it is now a requirement for checking in** (owner). The
publisher's only working-tree check was "git status is clean" — and git status cannot see an
empty directory. `dev/tree_is_commit.py` walks the real directories and files and fails on any
directory with no committed file or any untracked file that was tested. It is a `dev/publish.sh`
requirement (`tested-is-committed`, right after `committed`) and runs inside
`dev/sync-gt-src.sh` before anything is published. On its first run it found the same empty
directory in every release since 0.16.0 — including 0.17.3, the rollback copy, which is reported
as a warning because a released tree cannot be changed. After publishing, the verification now
also asks the published release's own registry to find its pack directories, so "gt-src verified"
means the definitions load, not only that the files arrived.

## gt 0.17.3 — 2026-09-28

**A publishing release on top of 0.17.2, the same day.** Nothing a session does changes; what
changes is what the other machine receives and how it can prove it received all of it.

### gt-src takes the repository's layout

The owner asked for gt-src to hold the files *"just as they should go into github"*. It did not:
it was the `golden-thread-plugin/` folder alone, so the repo root — `README.md`, `CHANGELOG.md`,
`LICENSE`, `CLAUDE.md`, `SUBMISSIONS.md`, `docs/`, `.github/` — never travelled, and the other
side's copy of the CHANGELOG was whatever someone remembered to carry. `dev/sync-gt-src.sh` now
publishes the whole tracked repository in its own layout, minus release directories older than
the previous one (rollback still works). `SOURCE.json` records `"layout": "repository"`.

### gt-src carries checksums

`dev/sync-gt-src.sh` now writes `SHA256SUMS` (every published file, hashed from the commit) and a
single `tree_sha256` in `SOURCE.json`, re-verifies what landed against them, and prints the tree
digest. The receiving machine runs `shasum -a 256 -c SHA256SUMS` before installing — BUILD-NOTE
§3 makes it the first step — so it can tell it holds every file of the newest release, unchanged.

### `install.sh` verifies the checksums itself — and never blocks a plain download

Owner: *"I want the checksum to be automatic ... that way we know all the files are there and in
the right places"* — and, the same hour, *"someone who downloads it from github [must not] have an
issue."* Before copying anything, `install.sh` finds `SHA256SUMS` at the repository root and
checks every listed path. A file missing, changed, or not where the list puts it is **named**; by
default the install continues, marked unverified, because the receiving repository's later
commits, a Windows CRLF checkout or a missing hash tool must not turn a stranger's honest install
into a failure. `--require-checksum` (or `GT_REQUIRE_CHECKSUM=1`) makes it a refusal — exit 8,
nothing copied — and the build note tells the receiving machine to use it. A tree with no
`SHA256SUMS` installs as it stands.

A first cut refused by default; it was changed before release on exactly that objection.

### `.gitattributes`: LF everywhere

Every text file now checks out with LF on every platform. On Windows, git's default CRLF
conversion broke the bash scripts outright and would have made every published file fail its
checksum.

### `/gt:gt-doctor` checks gt-src against `SHA256SUMS`

The doctor's gt-src check guessed "unmanaged" from top-level names it knew, and knew only the old
layout — it would have flagged the whole repo root the moment gt-src took the new one. Where
`SHA256SUMS` exists it now reports exactly: files changed since publish, files missing, and files
the publisher did not write. The name-based guess remains only for a gt-src published before
0.17.3. The first cut of the checksum writer listed its own half-written temp file; the new test
caught it.

## gt 0.17.2 — 2026-09-28

**gt could write everything a next session needs and put none of it in front of that session.
Now it does — and it gives tasks and handoffs the same three verbs: create, see, handle.** A new
SessionStart hook, `gt_surface.py`, shows what is overdue, what a previous session handed over and
nobody has dealt with, how many urgent tasks wait on you, and what state was written before a
compaction. Five new skills create, list and work through tasks and handoffs. Plus: session claims keyed
on a machine id instead of a hostname a DHCP lease can change, and a loose-ends audit that found
one scheduled job that could never install, one promised and never built, and a third that had
failed for three weeks while its checker called the crash "findings".

gt-demo, gt-watch, gt-report-card, gt-farm and gt-flow move to 0.17.2 with gt, as they do every
release (content unchanged; `requires_gt` `>=0.17.2,<0.18.1`). gt-wiki stays at 0.2.3 and gt-usage
at 0.1.3, on their own trains.

### Tasks and handoffs

The owner called the task list a black hole. Tasks were hand-typed into `## Tasks` in a format
only a careful writer gets right, and read by nothing but the `TASKS.md` rollup when someone
asked "what's next". Handoffs were worse: nothing recorded whether anyone had dealt with one.
0.17.2 gives both the same shape — **create, see, handle** — and the same rules: nothing is
deleted, a deferral needs a date, and being told costs no project context.

**Tasks — `/gt:gt-task`, `/gt:gt-task-list`, `/gt:gt-task-handle`, and the vault tool
`gt_task.py`** (`add`, `list`, `done`, `drop`, `defer`, `count`; seeded to
`Projects/golden-thread/tools/`). In the owner's words: create a task "like a dev would add that
to code", tied to "a vault entry or a wiki entry or a source entry"; handle them with a parameter;
list "just shows them".

- **One store, one parser.** Tasks stay in each README's `## Tasks`; `gt_task.py` parses with
  `gt_tasks.py`'s own rules, so a task written by the tool ranks exactly like a hand-written one.
  A second parser is how the 2026-09-03 multi-line field bug would come back. Existing
  hand-written tasks are unchanged.
- **New optional fields.** `[ref:: …]` ties a task to a `[[page]]` or vault path, and must
  resolve — the tool refuses a ref to nothing. `[defer:: YYYY-MM-DD]` hides a task until that
  date; `gt_tasks.py` now keeps a deferred task out of the ranking **and out of escalation**,
  because the stale-P1 rule kept escalating tasks the owner had explicitly put off.
- **IDs are `slug:LINE:HASH`**, and every write re-reads the line and refuses if the hash changed,
  so an ID from a stale list can never close the wrong task.
- **`drop` needs a reason; `defer` needs a future date and a reason.** Done and dropped tasks are
  checked off with what settled them, so a dropped task reads as closed to `gt_events` and
  `gt_daily`, and the line records `dropped: <reason>`. "Later, some time" is a drop, said out loud.
- **Core rule 1 by the side door, closed.** `gt_task.py` asks `gt_session` whether another live
  session has claimed the README and refuses if so; a tool that edited a claimed file because it
  is not the Write tool would disarm the rule.
- `--inbox` writes to `INBOX.md`, which `/gt:gt-review` routes as normal.

This implements the feature request `2026-09-28-gt-task-add-list-handle`.

**Handoffs — a status, `/gt:gt-handoff-list`, `/gt:gt-handoff-handle`.**

The first cut of this release showed a new handoff **once** and then went quiet, on the
`gt_closeout` lesson that a question asked every session trains people to scroll past it. The
owner ruled the same day that the opposite failure is worse: a handoff scrolled past on a busy
morning is as good as never written. So:

- **An unhandled handoff is shown every session until it is handled or deferred** — one line
  (path, project, age, open items), never its body, so being told costs no project context. The
  separate "N open handoff tasks" line is gone; each handoff carries its own count.
- **`gt_handoff_status.py`** gives each handoff a state. `open` keeps surfacing. `deferred` must
  carry a future date and is open again on that date — "later, some time" is a decision to drop
  it, recorded as `handled` with a reason. `handled` is marked by a person **or** follows when
  every task citing the handoff's filename is checked off, so closing the last task needs no
  second step. `history` is a pre-0.17.2 handoff with no status, over a week old and with no open
  citing task, so the upgrade does not resurface every old handoff in the vault. `mark` appends
  to the handoff's status log; nothing loads a handoff's body. `gt_handoff.py` now writes
  `status: open` in frontmatter (with `type`, `project`, `created`).
- **Setting `handoff_surface`** — `any` (default, every session start), `project` (only when
  `/gt:gt-open` opens that project, via `gt_surface.py handoffs --project`), `manual` (only in
  `/gt:gt-handoff-handle`). `any` is the default because under `project` a handoff waits until
  someone happens to open its project.
- **Two skills.** `/gt:gt-handoff-list` lists what is waiting and reads nothing.
  `/gt:gt-handoff-handle` loads only the handoff you pick and the task lines citing it, walks
  each item — done, keep as a task, drop with a reason — then marks it handled or deferred to a
  date.

`/gt:gt-open` now names the project's waiting handoffs without reading them. `/gt:gt-work` says
the task it raises **must name the handoff's filename** — that is the join that lets the last
closed task close the handoff.

With the three task skills that makes **28 skills in gt**.

### Surfacing at session start

By 0.17.1 four things were written for "the next session" and read at the start of it by
nothing: `gt_handoff.py`'s handoffs, `gt_state.py`'s state files, `gt-work`'s handoff tasks and
the owner's own dated must-do items. Measured on 2026-09-28: the SessionStart hooks were
gt_components, gt_workers, gt_version_check, gt_push_check, inject_core_rules and the report
card, and none of them read a handoff, a state file, `TASKS.md` or any `p::`. Two credential
rotations sat **15 days overdue** at the vault's top priority, worked around almost daily,
because priority is a sort order and not an alarm — 60 `p:: 0` tasks existed across ten
projects, and a 61st alerts nobody.

| Shows | How often |
|---|---|
| **MUST DO** — rows of `<vault>/deadlines.md` overdue (🔴) or due within 14 days (🟡); later rows counted, not listed | every session, recomputed live from the row's date |
| every handoff not yet dealt with — `Projects/<slug>/handoff/*.md`, and hand-written `handoff*.md` beside a project README — one line each: path, project, age, open items, never the body | every session **until handled or deferred to a date** (where: setting `handoff_surface`) |
| one line counting `p:: 1` tasks waiting on you — overdue, open over a week (setting `task_surface`) | every session while any wait |
| state files `gt_state.py` wrote | once each; after a compaction (`source: compact`) the newest one's content is handed to the model in full |

`deadlines.md` is one vault-root table, `| item | category | due | see |`, `due` as
`YYYY-MM-DD`. Adding a category is a row, not code. The detail stays in the file `see` points
at, because a rotation's danger is its *sequence* and a one-line alarm cannot carry that. A row
that does not parse is skipped **and counted**; a label shaped like a credential is withheld
rather than printed, because this output reaches both the terminal and the model's context.
`gt_surface.py must-do` prints the block on its own.

It never creates, edits or closes a task and **never writes to the vault**. The one thing it
writes is a machine-local "already shown" ledger, `~/.claude/golden-thread/surface/seen.json`,
so a state file is announced once.
A clean run says `nothing waiting` in one line, because silence reads the same as a hook that
never ran. Every failure exits 0. Setting `surface` (`on` by default, `off` to stop it).

**Why core and not the report card.** The feature request that started this
(`2026-09-28-cross-project-deadline-alert`) proposed a deadline check in the report-card
module. It went into core instead: an alarm must not depend on an optional module being
installed, and the report card shows what the *previous* session computed at its end, where
a countdown has to be computed live at every start.

### Session identity (Core rule 1)

On 2026-09-28 a laptop moved to a wired network and DHCP gave it a different name. Every claim
recorded under the old name stopped being recognised as this machine's — not an error: its pid
simply stopped being judged, and each claim would lapse on the heartbeat clock while its
session was still running. **Core rule 1, disarmed by a DHCP lease, in silence.**

Identity is now a uuid4 in `~/.claude/golden-thread/machine-id`, created once (atomically, so
two first runs cannot mint two ids), never overwritten, never synced between machines. The
hostname stays as a readable label. `gt_session.py`, `guard_session_claims.py` and
`gt_workers.py` all decide by `gt_paths.same_machine` (the vault tool carries a pinned copy,
because a vault tool cannot import from the plugin):

- both ids known — the ids decide; the same id under a new name is a **rename, reported once**;
- a file from gt ≤ 0.17.1 with no id — still counts as this machine's while its label matches
  (migration, not a flag day); when it does not, its heartbeat decides, **and that is said**.
  It adopts the id the next time its own session writes it;
- no id readable here — falls back to the label, and says so.

Worth knowing: the id is per HOME, so **two OS logins on one Mac now get two ids** and judge
each other's sessions by heartbeat, the cross-machine rule, where before they shared a hostname
and judged each other's pids. `gt_workers` declarations now record the machine too.

**Fixed alongside it: a pid no longer vouches for someone else's session.** One Claude Code
process hosts successive sessions, so an older session's file recording *our own* pid looked
live for as long as we ran, and its claims never lapsed. When the recorded pid is the asker's
and the session is not, the pid proves nothing and the heartbeat decides.

### Wiring fixes from the loose-ends audit

A loose-ends audit asked of every script: does anything actually run this?

- **The `daily` job could never install.** `gt_schedule` refuses a job whose script is not in
  the hooks dir (a script under CloudStorage fails under launchd), and `gt_daily.py` was not
  in `HOOK_DIR_SCRIPTS`. It, `gt_schedule.py`, `gt_sweep.py` and the sweep's members and their
  imports (`gt_secrets`, `gt_scan_code`, `gt_check_report`, `gt_registry`, `gt_staged`) now
  install there. The release gate's `check_wiring_coverage` now fails any `gt_schedule` job
  whose script does not reach the hooks dir — the gap both shipped through.
- **A `sweep` job**, Mondays 07:30 — `gt_sweep.py`'s docstring had promised a launchd job since
  it shipped, and there was none. Install runs the new `gt_sweep.py --check` first. **Known
  limitation: that pre-flight currently refuses**, because `gt_registry` cannot find `packs/`
  from the hooks dir, so every member would report "could not run". Refusing is what the check
  is for; the job is not usable until the packs resolve from there.
- **`lint-weekly`'s "normal" exits were wrong.** It listed `{0, 1}` on the belief that the job
  exits 1 on findings. It never did — the only way it exited 1 was an uncaught exception — so a
  crash read as "the lint found something". Now `{0}`. `gt_lint_weekly.py` reports **COULD NOT
  RUN** and exits 3 when a lint died, instead of writing "Findings: **0**" built from two
  tracebacks, and writes its report as a new file renamed into place. `gt_daily.py` turns an
  uncaught exception into exit 3 for the same reason: Python's crash code is its "nothing to
  report".
- **`gt_doctor` gains a `schedule` check**: every *installed* job, loaded, script present, last
  exit judged by `gt_schedule`'s own `BENIGN_EXITS` rather than a copy of it. A job never
  installed is a choice, not a finding.
- **`gt_doctor`'s `vault` check delegates to `gt_upgrade`** instead of hand-coding the 0.11.0
  migrations. It now names the release the vault is stamped at beside the installed release,
  and sees every migration since, not two.
- **A project born in the new decisions format is no longer "unmigrated".** A project whose
  ADRs went through `gt_adr.py` from the first one has no `0000-baseline.md`, and `migrate`
  refuses a generated file — so the pending step could never clear.
- **`retired.json` removes a stray `hooks/gt_ingest.py`**, byte-identical to the copy shipped
  0.6.0–0.9.13 and installed by no release — most likely a hand copy. install.sh backs it up
  first.

### gt-allin

`secrets` (`gt_secrets.py` over `--repo`), `runbooks` (`gt_lint.py --runbooks`) and `wiki`
(gt-wiki's `wiki_lint.py`, **only while that module is installed**). Each existed and ran
nowhere "everything" was run; `gt_secrets` in particular had no aggregator home at all, since it
is deliberately not a `gt-scan` member. The roster grows from four members to six in core, plus
`wiki` with gt-wiki installed — the denominator is what this install declares, so switching the
wiki module off does not produce a "could not run" on every run.

Then the last two checks that existed (owner: *"add all the other checks into allin"*):
`tests` runs the repo's own suite through the commit guard's discovery table — one table, so
the command all-in runs is the one the guard wants a receipt for — and records that receipt, so
a clean all-in is also the evidence `/gt:gt-allin-commit` checks; `validations` reports files
changed since their recorded validation. A repo with no test command is *could not run*, not
clean. `gt_code_review.py` stays out on purpose: it plans a review and finds nothing itself, so
it could only ever report a pass for work nobody did.

### The release gate files its verdict in the vault

`dev/release-check.sh` now ends by filing a dated row in `<vault>/.gt/checks/release.md`
through `gt_check_report.py`: verdict, how many of how many checks passed, the commit, and the
names of any failed steps — never their output. Before this a release's gate result lived in a
terminal and a machine-local receipt; the vault got one `log.md` line at publish, and only if
publish ran. A `--quick` run is filed too, labelled quick. Filing is bookkeeping and never
changes the gate's exit code.

### Known, and not fixed

- **Calendar-fired launchd runs are refused the vault.** Every calendar-fired `lint-weekly` run
  (09-14, 09-21, 09-28) was refused by macOS privacy controls (TCC) reading existing files in
  the CloudStorage vault; only session-kicked runs ever succeeded. The `daily` 22:00 job will
  very likely fail the same way. `gt_doctor`'s `schedule` check now *reports* it, and the job
  says COULD NOT RUN rather than "findings" — but it is not fixed.

- **The `sweep` job cannot be installed yet**: its pre-flight refuses because `gt_registry`
  cannot find `packs/` from the hooks dir (see *Wiring fixes*).

### Measured, and deliberately not built

**No detector for a session that ended uncaptured.** A feature request of 2026-09-28 asked
whether `gt-work` could tell on its own that a session lost work and write the handoff
unasked. Measured across 40 sessions (8 lossy, 32 healthy), no combination of signals
separated them: the best AND-pairs caught 1 of 8, and 3 lossy sessions fired no signal at
all. So 0.17.2 keeps the declared trigger — `gt-work` asks — and wires no detector, per the
request's own rule.

---

## gt 0.17.1 — 2026-09-28

**The checks gt asks you to run, gt now runs: a credential scan, source validation, a
commit gate, a weekly sweep, a record of every verdict in the vault, and a review framework
that deliberately ships no opinions.** Plus the piece that closes this release's own gap —
session state written *before* the context runs out rather than as it does.

There is no 0.17.0. The version was cut and then held back so the state write could go in the
same release; 0.17.1 is what 0.17.0 was, with that one addition and its documentation.

### Three cadences, and the difference between them is the point

| Cadence | Looks at | Does |
|---|---|---|
| `tests/run.sh` | the file set the scanners define | **fails the run** |
| the commit gate | the **staged diff** only | **denies the commit** |
| `gt_sweep.py` | the **whole tree**, weekly | **reports**, never blocks |

The narrow two are what keep the gates usable: a commit gate that scanned the whole tree would
punish you for someone else's old code. But that means nothing ever re-examines what is already
there — a rule added today never sees a file nobody touches, and a credential committed before
the gate existed stays committed. The sweep is the cadence that goes back over it, and it
reports rather than blocks because there is no commit in front of it to refuse.

### `gt_secrets.py` — and why it is a separate process

> Nothing in this process ever prints matched source text, and no other check runs beside it.

A finding is a path, a line, a rule id and a length. Never an excerpt, never a prefix, never a
hash — a prefix is crackable and a hash still confirms a guess.

Every other gt scanner prints the text it found, because there the text *is* the finding. Here
the text is the thing being protected. Those are **opposite output rules**, and two tools with
opposite output rules in one process is exactly how the 0.16.0 attempt leaked: its `secrets`
check printed only a length while its `naming` check, same tool and same run, printed raw source
text, so a credential-shaped identifier reached stdout and `--json` in full. So this is not a
`gt_scan` member, and registering it as one would be a regression rather than a tidy-up.

### `gt_scan_code.py` — source validation, where rules are data

Code checked against `lint` packs from the slot registry, in a documented subset of ast-grep's
rule schema — same names, same nesting, same meanings as upstream, so a user can read ast-grep's
published reference and have it apply. Contributing a rule is contributing a JSON file.

Each rule declares the evaluator tier it needs — `text` (regex), `stdlib` (Python's own `ast`),
`astgrep` (the ast-grep CLI), `treesitter` — and **a rule whose tier is absent is reported
SKIPPED, never silently passed.** That is the same rule as "a member that could not run is not a
pass", one level down: a rule that was never evaluated and a rule that found nothing produce
identical output otherwise, and the difference between them is the whole value of the scan.
SARIF 2.1.0 carries the skips in `toolExecutionNotifications`.

**The ast-grep Python binding was removed and replaced by the CLI.** The binding shipped macOS
wheels for cp39 only, 0.25.0 through 0.30.0, and 0.30.0 lacked enough of the rule language to be
worth keeping. The CLI is MIT, current at 0.45.3, takes gt's matcher **verbatim** through
`--inline-rules`, and covers many more languages than gt ships rules for. It is optional and not bundled: `install.sh` offers
`brew install ast-grep` or `npm i -g @ast-grep/cli` and never installs it behind your back.
Below 0.45.3 it is **refused rather than used**, because a partial version silently
under-matches — which is the failure the tier contract exists to prevent.

One measured decision worth stating: ast-grep's `stopBy: neighbor` default is worth **201 false
positives** on gt's own tree against `stopBy: end`. The default stands.

### `gt_check_report.py` — a gate that leaves a record

A gate tells you about the commit in front of you and nothing about the week. Three questions
need history and cannot be answered from an exit code: is this check actually running, is it
getting noisier or quieter, and did anyone ever look at the thing it flagged.

`record` files a **verdict, a count, a scope and a ref** into the vault. **Never a finding's
content** — `gt_secrets.py` exists because a credential must not reach a log, and a vault file
is a file like any other. `show` exits `1` when there are no reports yet, which is a different
thing from a clean history.

### `gt_code_review.py` — the framework, and no opinions

gt does **not** perform the review, and ships **zero** review dimensions. It cannot perform one:
"does this abstraction earn its keep" has no mechanical oracle, and a tool claiming to test that
is testing something else. What gt owns is everything around the judgement.

The dimensions are yours — `review.*.pack.json` in your own vault, each an `id`, a `title`, a
`rubric` and optionally a `files` glob. The rubric reaches the plan **verbatim**; a rubric gt
paraphrased would be gt's opinion wearing your name. A core review pack would make gt's idea of
a good review everyone's default, so there is not one, and a test asserts there never quietly
becomes one.

**Zero dimensions configured exits `3`, never `0`.** Since gt ships none, the empty case is the
*default* case — and reporting success there would tell every user who never configured a
dimension that their code had been reviewed.

`validate` rejects any finding that cannot be **checked**: a file that does not exist, a line
past the end of one, a dimension nobody configured, a severity outside the vocabulary, or
`confirmed: false` — a verification pass that says no must be honoured, or the pass is
decoration. Rejections are counted in the summary, never silently dropped, and `--ledger`
suppresses a finding already declined once.

### `gt_state.py` — write the state before the context runs out

Three pieces existed and nothing joined them: `gt_usage` reads the context percentage and only
*displays* it; `gt_report_card` runs at `SessionEnd` and `PreCompact` and writes a health summary
with nothing about what the session was doing; `gt_handoff` gathers the right content and is
manual, so nothing ever calls it. On 2026-09-28 the owner warned that compaction was near and
the assistant wrote a handoff by hand. This is that, with nobody needing to think of it.

**The signal is `ctx_pct`, and the distinction is the whole design.** `readings.jsonl` carries
context fill and rate-limit allowance side by side, and they are unrelated. A real reading from
the session that prompted this was `{"five_hour": 5, "seven_day": 10, "ctx_pct": 91}` — 5% of
the five-hour allowance, 91% of the context. Triggering on the allowance meter would have fired
at entirely the wrong moment **and looked correct doing it**, because both are percentages with
plausible values. A test asserts the tool never reads `rate_limits` at all.

It fires once per crossing, at `--margin` (default 5) below the flag point for your
`usage_alert` mode, wired to `UserPromptSubmit`. `PreCompact` remains the **backstop, not the
primary** — it fires when compaction has already started, so the write competes with the thing
it exists to survive — and its file says which one wrote it. A missing usage ledger reads as
**"cannot tell"**, never as room to spare. Every failure path exits `0`.

### `/gt:gt-work` offers a handoff for what did not reach a file

`gt_handoff.py` gathers exactly what a next session needs, and had **one** caller: a person
typing `/gt:gt-handoff`. So it was offered when someone happened to think of it rather than when
it was needed.

`gt-work` is the one skill that knows the answer, because classifying findings tells it what it
is *not* writing as well as what it is: a decision that needs the user, something mid-flight, a
question raised and unanswered. It now names those items and **asks** whether to write a
handoff. On yes it runs `gt_handoff.py` and writes the narrative itself — the script refuses to,
on the grounds that a document which reads finished when it is not hands the next session false
confidence, which is worse than handing it none.

**On yes it also raises the work**: one task per unresolved item in the project's `## Tasks`,
each naming its own item and citing the handoff, at `[p:: 1] [waiting:: user] [since:: <today>]`.
A handoff nobody is told to read is write-only, so the task is the mechanism and the file is the
context. `p:: 1` rather than a top priority because **there is no `p:: 0`** — `0` is a
project-level tier defined as one nothing sits in permanently — and because `since::` makes the
existing stale-P1 rule escalate the project once the task is a week old. An unread handoff
climbs by itself; a read one costs nothing. One task per item, never a single "review the
handoff" line, which competes with real work while hiding everything after the first.

**The restraint is the feature: the question is asked only when the list is non-empty.** An
offer made every session is one that is always declined, which `gt_closeout` had already
demonstrated by asking the same question every session until the answer stopped being read.
Nothing uncaptured, no question. A `no` is final, and an existing handoff is refused rather than
overwritten — it is someone's record of a session.

A person decides each time, so this cannot produce a pile of files nobody opens. Writing the
handoff automatically, and raising a task per unresolved item, is a separate question and is
filed for 0.17.2.

### `gt_daily.py` and `gt_schedule.py` — the day, captured without being narrated

`gt_daily.py` writes tasks closed, commits per repo, event counts, wiki item **counts** and an
active span per project into one fenced block in `Daily Notes/<date>.md`, replaced whole each
run. **Terse by design**: it does not explain, summarise or interpret, because a generated block
that editorialised would encode a reading of the day that is not the owner's. It never touches a
line outside its block, and specifically never `## Noticed` — the unfiled capture surface
`/gt:gt-review` sweeps.

`gt_schedule.py` installs, verifies and removes the launchd jobs (`daily` at 22:00,
`lint-weekly` Mondays at 07:00). The weekly lint agent had been installed **by hand** in
2026-09-08 with no `--check` and no rollback. `check` validates **through launchd** and reads
back launchd's own last exit code, because on macOS a job can run perfectly in a shell and be
denied the filesystem under launchd — and it knows which exits are a *normal* outcome, because
an earlier version called the live weekly job broken for exiting `1` when the lint finds
something, which is most weeks.

### Fixed

- **`install.sh` aborted under `set -e` on any machine without brew or npm.** A bare
  `[ -n "$X" ] && echo` and a `fn; rc=$?` pattern each abort the script when the left side is
  false, so the installer died before its final line. Nearly dismissed as a mid-run editing
  artefact; the regression test now asserts exit 0 *and* that "Restart Claude Code" is printed
  under `PATH=/usr/bin:/bin`. **A convincing explanation for a failure is not evidence.**
- **`gt_daily.py` mangled the text it quoted**, found on its first real run: `gt_daily.py`
  rendered as `gtdaily.py` because underscores were treated as Markdown emphasis, and a commit
  subject containing bold was reduced to `'s'` — `BOLD.search` where `BOLD.match` was meant.
- **Two crying-wolf checks**: `gt_daily --check` called a repository subdirectory "not a git
  repo", and `gt_schedule check` called the weekly lint job broken for its normal exit code.
- **Core rules moved to the vault root** (`core-rules/`, from `Projects/golden-thread/`), and
  four things that had quietly bound themselves to the old path were corrected with it.
- **`selftest.sh` could not report FAILED**, and three of its checks were wrong.
- **Tests that passed for the wrong reason**: a dirty-vault test compared before against after
  inside a fixture that had already committed, so it still passed with the guard stripped; a
  temp-cleanup test globbed a shared `/tmp` and failed only under 32 workers.
- **Four hand-maintained fixture lists** that had fallen behind what they stood in for, now
  derived from the thing itself. *A fixture that lists what it needs falls behind the thing it
  stands in for* — this release found that defect four separate times.

### Documentation

The release gate now covers `scripts/*.py`. Any script with an `argparse` command line must be
named in at least one of the four shipped documents, with an explicit exemption list for
helpers that exist only to be imported. When that gate was written it found **nine commands
with command lines documented in zero files** — one of them, `gt_demote.py`, shipped long before
this release. A release that adds a command nobody can discover has added nothing.

All nine are now documented, and `/gt:gt-scan` no longer claims it has one member or that the
`secrets` slot is still waiting for its tool.

---

## gt-usage 0.1.2 — 2026-09-23

**A new module, on by default: a plan-allowance meter that is silent until one of your
windows is near its ceiling.** gt itself is unchanged and stays at 0.16.5.

### What it is

How much of a plan allowance has been spent reaches exactly one place: the JSON a **status
line** receives on stdin, every render. `/usage` prints token counts rather than
percentages, nothing persists the percentages to disk, no CLI flag exposes them, and there
is no OpenTelemetry metric for them. So a history of allowance consumption can only be built
by sampling from a status line — and only from inside a session, because a launchd user
agent cannot even list `~/.claude/projects` (`PermissionError` on listdir while `stat`
returns `True`; macOS TCC). A scheduled version of this would fail quietly and for ever.

- `gt_usage.py` — the status line. Records one reading a minute and **displays nothing**
  until the 5-hour, weekly, monthly-spend or context window crosses its threshold. Never
  raises, never blocks, always exits 0; renders in 76ms.
- `gt_usage_brief.py` — one labelled line at session start, carrying the single most useful
  action and the command that quiets it.
- `/gt-usage:gt-usage` — on demand: where the allowance stands, what cutting this session
  would save, four things a person can do, and how to turn any of it off.

Two settings, registered in gt's own registry so `/gt:gt-settings show` lists them beside
everything else: `usage_meter` (`off` · `status` · `login` · `both`) and `usage_alert`
(`early` · `normal` · `late` · `always`). `always` keeps the meter on screen permanently and
is deliberately **not** the default — a line that is always there is read for a day and then
never again, which is the failure `gt_closeout` had already demonstrated by asking the same
question every session until the answer stopped being read.

### Three honesty rules, enforced in the code rather than left to prose

- **A window the plan does not report is ABSENT, never 0%.** Absent and zero are different
  facts, and a meter that shows the second for the first is lying quietly.
- **A reading older than twelve hours is quoted with its age**, not as current.
- **No token count is ever converted into a percentage of an allowance.** Cache writes count
  toward API *rate* limits and cache reads do not; whether either touches a *subscription*
  allowance is undocumented. Inventing that conversion would be a guess wearing the clothes
  of a measurement.

### What it is not

A cost saver. Measured over 180 days on one machine, **65% of prompt-cache write volume was
avoidable** — and the weekly allowance still sat at **13%**. The waste was real and the
scarcity was not. That sentence is in the setting's own `explain` text, because the person
most likely to over-read this meter is the one who just installed it.

### 0.1.1 and 0.1.2: the same bug twice

**Whether a thing is SHOWN and what it ADVISES are different questions.** Under
`usage_alert always` every threshold is negative so the line renders unconditionally — which
is what `always` means — but the advice attached to it must still be earned from the numbers.

- **0.1.1** — the status line advised *"cutting now is cheap"* at 10% of a context window,
  where there is nothing to cut. Found within a minute of the meter first being switched on
  to look at.
- **0.1.2** — the identical defect in the session-start line, claiming *"the weekly window is
  the one to watch"* at 13%, in the file the 0.1.1 fix had not reached. Found while
  disproving a bug report that was itself wrong.

`tests/test_usage_module.py` now asks the question neither file was asked — change **only**
the display level and the advice must not move — of every surface at every level, plus the
inverse so the fix cannot degenerate into "never advise". Proven by mutation: each bug fails
its own named test and nothing else.

### Also

**`gt-flow`'s redaction test asserted something no salt can guarantee.** It required two
renders' sets of truncated hashes not to intersect; a redacted name is a sha256 cut to four
hex characters, so at that fixture's size they collide by chance about once in a thousand
runs. It failed a release check with `{'f-5a76'}` while the code was entirely correct, and
its message — *"the salt is not per render"* — sent the reader to audit the one part that
worked. The test now asserts what it means: the same name must hash **differently** across
two renders, which cannot collide. Widening the hash beyond four characters is filed
separately; a shared page showing two names under one `f-5a76` is a correctness problem, not
just a flaky test.

## gt 0.16.5 — 2026-09-18

**Every defect an adversarial sweep found in 0.16.4, including the two that release
introduced — and the checks that had been reporting clean over what they never verified.**

A three-pass sweep of 0.16.4 (an attack on that day's concurrency work, a claims-versus-code
audit of every script and skill, and a live health check) produced eight high-severity findings
and about twenty more. This release closes all of them. Every behavioural fix was proven by
reverting it and watching its test fail.

**The thread running through the worst of them: three of the four highest findings were CHECKS
THAT REPORT CLEAN OVER SOMETHING THEY NEVER VERIFIED** — the exact failure the Core tier,
`gt_doctor` and `gt_validation` were each built to close. The defect class was not merely
recurring; it was reproducing inside the machinery built to catch it.

### Nothing verified that a Core rule's mechanism was wired. Now something does.

`gt_doctor`'s docstring had advertised a `core rules` check for releases while `CHECKS` did not
contain it — `--only core-rules` was a usage error. The one place that did look, `gt_lint`'s
`core-unenforced`, only asked whether SOME hook occupied the event, never which one: a vault
with any third-party `UserPromptSubmit` hook and `inject_core_rules.sh` **absent** reported the
Core tier enforced.

Both are fixed. `gt_lint` now compares the actual command through a single
`ENFORCEMENT_MECHANISM` map, and `gt_doctor` gained the check it had been claiming — reading
each `level: core` rule in the vault and confirming the specific script implementing its
declared enforcement is wired and present on disk. A failure says what it means: the rule is
**stored but not enforced**.

### `gt_doctor` could not report drift at all

Its component check read an exit code that `gt_components` returns as `0` by design (it is
advisory, and also runs as a SessionStart hook). So the WARN branch, the "drift against"
message and the `apply` fix line were dead code, and the drift text was discarded. It now parses
the report, and distinguishes actionable drift (`stale`/`missing`) from what needs a person
(`differs`/`ahead`/`no-manifest`) from the benign (`extra` — module-installed scripts, which
must not warn for ever).

The same "trusted the exit code" mistake was in **four more checks**: a linter that died with a
traceback reported the vault clean; a crashed worker probe became a warning about "no output";
`wiring` ignored `badpath` rows entirely and said every hook was wired. All now separate *could
not run* from *clean*, and the footer names them — "N check(s) could not run at all, so this
report does not cover them".

### The session registry stopped losing claims — this time including `register`

0.16.4 said "the session claim registry no longer loses claims". That was false: the
compare-and-swap covered `claim` and `beat`, while `register` rebuilt the claim list from
`--files` alone and `--resume` deleted the file first. Re-registering mid-session — which
sessions routinely do — silently dropped everything claimed since, disarming the only
cross-session mutual exclusion the vault has.

`register` now goes through the same CAS: existing claims are carried forward and `--files` is
unioned into them. A dead predecessor's claims are still released, but the count and the paths
are named rather than dropped in silence.

Two more in the same file. **An ambiguous session id now refuses instead of guessing**: with two
registrations (`--new`, or a same-minute collision) the old code picked by sort order, so a
process could claim into another's registration and a `release` could clear claims it did not
own. And **the flock fallback announces itself** — measured, without it the CAS silently loses a
claim in 9 of 10 trials with four writers while every process reports success.

### Two defects 0.16.4 introduced

**The retry duplicated rescued lines into the spool.** `capture_hand_written` sat inside the new
3-attempt loop and is not idempotent: one stale attempt duplicated a rescued line, two
triplicated it, and because the copies land in the SPOOL no re-merge can remove them — the
vault's audit trail permanently recorded an event more than once. The capture stays inside the
retry (a retry happens because the file moved, and what moved may be a further edit that still
needs rescuing); the caller now tracks what it has already captured this run, as a multiset, so
a line genuinely typed twice is still rescued twice.

**An unreadable target was destroyed, permanently.** `current_digest` returned the digest of
empty bytes on any read error, so the `expect` precondition reported "nobody moved underneath
you" about a file nobody could read, and the render overwrote it. 0.16.4's mode preservation —
correct in itself — then copied mode `000` onto the replacement, so the file stayed unreadable
and every later merge destroyed it again. Reading a file that exists but cannot be read now
RAISES; `gt_log` and `gt_adr` turn it into a clean refusal with nothing written.

### Writers that reported success for work they had not done

- **`safe_write`** swallowed strategies 2 and 3 in bare `except: pass` — the same silence 0.16.4
  fixed one rung up. Each now names what failed and what it fell back to. `replay()` no longer
  overwrites a target that reappeared; it reports `still-blocked` and leaves it for a person.
- **`gt_lint --queue`** destroyed ticked items. The review queue is not a projection — a lint
  finding has no upstream checkbox, so a tick lived in that file and nowhere else and carried
  the judgement recomputation cannot reach. Ticks on still-open findings now carry across.
- **Generation receipts** were written after `os.replace` and unguarded, so an unwritable receipt
  crashed *after* the file was replaced and the "kept a copy" note never reached the user.
  Backups are now bounded (count and age), never overwrite each other, and prune only their own
  tool's copies.

### Contracts that had drifted

Nine scripts documented exit codes their code did not honour, and each was decided one way or
the other rather than papered over: `gt_context --json` exited 0 having rendered nothing;
`gt_scan_language` hid a failed pack whenever anything matched; `gt_demote` returned "error
mid-move" for a refusal before anything moved; `vault_init` always exited 0 even holding error
rows. `guard_session_claims` tracebacked on a no-arg call against its own fail-open contract.
`gt_upgrade --dry-run` wrote a working copy inside the vault. `gt_edits` lost attribution in
exactly the worktree case its comment said was handled.

**Behaviour a caller can feel**: `gt_scan_language` now exits 3 rather than 1 when a pack failed
to load and findings exist — a partial scan is could-not-run, not findings.

### Also

`gt_adr`'s dry run named an ADR number the real allocation would not use, ignoring the
high-water file in exactly the case it exists for. `gt_settings` claimed to register every
automatic behaviour while four enforcement hooks had no entry — the claim is narrowed to
*optional* behaviour, deliberately, because a session able to switch off the hook asserting
rules against it is not enforced. `/gt:gt-registry` was cited as a skill and does not exist.
`gt-create` asked a question whose answer changed nothing. Skills citing Core rules by NUMBER
now cite them by name, because numbers are run-time positions over the gated set and renumber
when a setting is switched off.

## gt 0.16.4 — 2026-09-17

**The vault's writers stop losing other people's work, and the release gate stops depending on
someone remembering to run it.**

**The session claim registry no longer loses claims.** `cmd_claim` and the heartbeat were
read-modify-write with a blind `write_text`: two processes sharing a session id, and one set of
claims was gone. That matters more than its size suggests — this registry is what Core rule 1
depends on, what `gt_lint_weekly` reads to decide whether INBOX.md is safe to touch, and what
`gt_demote` reads before moving a note. A lost claim silently disarms the only cross-session
mutual exclusion the vault has. Writes now go through a compare-and-swap that re-runs the whole
decision on a lost race, so two writers' claims **union** rather than one replacing the other,
and a write that never wins fails loudly instead of reporting success. Measured during the fix:
plain compare-and-swap was not enough — check and rename are two syscalls, and with four
concurrent claimers a claim was still lost in about half of runs — so the check and the rename
are held together by an `flock` on the target's own descriptor. No lock file is created and the
kernel drops it however the process exits, so a killed session leaves nothing to wedge.

**`gt_spool.write_if_changed` had a shared temp name.** `<target>.gt-tmp` is deterministic, so
two concurrent `gt_log merge` runs wrote the same scratch file and one rendered the other's
partial bytes into `log.md`. It now uses a random name in the target's own directory, preserves
the target's mode, and removes the temp file on any failure.

**And the decision it was applied to is now a precondition.** `hand_written()` reads the file
and decides which lines to rescue; `write_if_changed` then read it AGAIN and replaced it, so a
save landing between the two reads was lost — not because the rescue failed, but because it had
already been computed against bytes that no longer existed. `write_if_changed` takes an `expect`
digest and returns a distinct `STALE` when the file has moved, and **`gt_log` and `gt_adr` pass
it**, with a bounded retry that refuses loudly rather than either hanging or overwriting. Wiring
it matters as much as having it: a capability nothing calls is this project's own signature
defect. In `gt_adr` the window was worse, because that path REFUSES on hand-written lines —
finding none and then clobbering the save that arrived a moment later defeated the refusal
silently, in the one case it exists to catch.

**`gt_lint --queue` stops eating the worklist, and preserves ticks rather than only backing them
up.** The review queue is not a projection: a lint finding has no upstream checkbox, so a tick
lived in that file and nowhere else, and the truncating write destroyed the only copy. A tick
also carries the judgement recomputation cannot reach — triaged, accepting this one, not now.
Ticks on still-open findings now carry across; a fixed finding stays gone and its old tick does
not resurrect it; anything else that diverges is backed up and named.

**`safe_write` stops claiming success for a degraded write.** When its atomic path failed it fell
through to a plain truncating write and returned as though nothing had changed. It now reports
`direct-degraded`, and the exception that forced it reaches stderr and the ledger instead of a
bare `except: pass`. The fallback is kept deliberately — the defect was the silence, not the
fallback — and the module now says plainly that it is not concurrency-safe, so its name is not
read as a guarantee it does not offer.

**The release gate runs on a machine that is not the author's.** `dev/release-check.sh` described
itself as "the gate every check-in passes, ON THIS MACHINE, before a commit" — manual, local, and
indistinguishable from not having run. A GitHub Actions workflow now runs it on every pull
request and every push to main, and separately enforces that non-merge, non-bot commits carry a
sign-off. Nothing new was written: the test suite, the submission validator, the doc-count gate
and verify-source all already existed and were merely unwired.

The gate also gained a third outcome. PDF freshness is an mtime comparison and git does not store
mtimes, so in a fresh clone that check is meaningless — it would pass or fail on the order the
runner happened to write files. It now reports **SKIP** rather than inventing a verdict: it does
not raise the exit code, but it prints every time, so a clean run never quietly means "every
check but one".

## gt 0.16.3 — 2026-09-17

**Two writes that could destroy something without saying so.**

**`install.sh` refuses when MANIFEST.json is tracked but missing.** A missing manifest used to
return clean and be regenerated further down, so deleting it produced a self-consistent manifest
describing whatever was in the tree — a cheaper attack than tampering, because tampering has to
survive a comparison and deletion removes the comparison. Absence is legitimate for a packaged
install (`package.sh` deliberately does not ship the manifest, so a stale one cannot report drift
that does not exist), and the two cases are distinguishable: a git checkout *tracks* the manifest,
a zip has no git at all. A tree that tracks one and does not have it has had it removed, and
regenerating there would bless exactly what the check exists to catch. The packaged path still
works and now **says out loud** that the component check could not run against that tree, because
a check that did not run must never read as one that passed.

**`gt_tasks.py` no longer overwrites `TASKS.md` in silence.** It wrote the vault-root rollup with
a bare truncating `write_text`: no record of what it had produced, no atomicity, and none of the
`is_generated` / `hand_written` handling its siblings `gt_log.py` and `gt_adr.py` have. A person
ticking a checkbox lost it at the next run with nothing said, and a crash or a sync mid-write
could leave a torn or empty file at the vault root.

The fix is a **generation receipt**: the digest of what was last generated is recorded, and a file
that differs from it is copied aside *before* being replaced, with a message naming where the edit
actually belongs. `TASKS.md` is a projection — a tick there was never going to survive, by design
— so the defect was never that the edit is discarded, only that it was discarded invisibly. The
write is now atomic (tmp + fsync + `os.replace`), which prevents the torn file rather than making
it recoverable: the half-written state never exists. An unchanged file stays silent and accrues no
backups, so the warning keeps its meaning.

## gt 0.16.2 — 2026-09-17

**Two places the machinery could go quiet without saying so, and a refusal that gave the
wrong reason.**

**Core rules are re-asserted after a compaction.** The docs are explicit about what survives:
project-root `CLAUDE.md` and auto memory are *"re-injected from disk"*, while *"context that
hooks added earlier"* is *"summarized with the rest of the conversation"*. gt's Core rules
travel by hook, so after a compaction they existed only as whatever the summariser chose to
keep. `inject_core_rules.sh` is now registered on `SessionStart` with a `compact` matcher as
well as `UserPromptSubmit`, and reads which event fired from its payload rather than naming
one in its output.

The exposure was narrower than it first looked, and saying so precisely matters more than
overselling the fix: `UserPromptSubmit` has no compaction exception, so the next user prompt
always restored the rules verbatim. What was exposed was the *remainder of the turn* in which
an auto-compaction fired. The mechanical tier was never affected — `PreToolUse` guards and the
`Stop` validator are event-driven commands, not context, so compaction cannot weaken them.
What lapsed was the re-assertion, not the backstop.

**A hook can declare a `timeout`, because `SessionEnd` cancels silently.** `SessionEnd` hooks
share a 1.5-second budget, and a hook that reaches it is cancelled with its **output
discarded** — no error, nothing reported to anyone. The report card measured 0.61s of that
1.5s, so it was not truncating; but nothing stood between it and the day a vault got slower,
and its own subprocess ceiling was `timeout=15` — ten times the whole event budget — so a
single slow `git status` would have blown the budget while the script waited patiently. The
module hook schema now carries `timeout` and the report card declares one. `PreCompact` is not
on the reduced-budget list and was never at risk.

**The copyleft refusal knows the current SPDX spellings.** `REFUSED_SPDX` listed only the
deprecated short forms, so `GPL-3.0-or-later` — what any modern licence scanner emits — was
still refused, but as `licence-unknown` rather than as copyleft. A contributor read "not in the
allowed list" and would reasonably open an issue asking for it to be added. Both spellings of
every copyleft family are now listed with the right reason, and the SPDX list version they were
taken from is recorded, because a refusal rule is only as reproducible as the list it was read
against. Fail-closed behaviour is unchanged: the allowlist still does the refusing.

**`gt_demote.py` now performs the refusal it had been promising.** Its docstring said "a file
another session has claimed is never moved" from 0.16.1 onward, and no code did it: there was no
claim check anywhere in the file and no test for one. That is a claim outliving its
implementation, in the docstring of the tool written to stop exactly that — and it was found by
reading the file against its own code during a documentation sweep, not by anything mechanical.
The check now reads the vault's session registry directly (rather than importing the vault-side
`gt_session.py`, which a vault on an older release may not have), ignores a session's own claim,
ignores released and stale ones, and reports in a dry run as well as under `--apply`. **An
unreadable session file refuses**, because "could not check" is not "clear". Seven regression
tests, each proven to fail with the guard removed.

**`/gt:gt-context` says plainly that its envelope is not a security control.** *Adaptive
Attacks Break Defenses Against Indirect Prompt Injection Attacks on LLM Agents* (NAACL 2025
Findings, arXiv:2503.00061) attacked eight published defences and broke all eight, above 50%
attack success in every case — delimiter schemes like this one and trained detectors alike.
The envelope provides *identifiability*: it marks content as someone's definition rather than
as the system speaking. What protects a session is that packs are reviewed before merge, and
that a pack can only reach a session already trusting the vault. `PROTOCOL.md` likewise now
records that human-gated promotion is a **measured** mitigation rather than a preference —
published attack success against agent memory runs 45–85% for compaction poisoning and
correlates directly with how eagerly an agent writes to memory.

## gt 0.16.1 — 2026-09-16

**The definitions reach a session, a note can be moved instead of deleted, and a validation
writes down what it established.**

**`/gt:gt-context` — the Tier D slots finally have a consumer.** The registry's tier system
exists to mark content as reaching the model, and until now nothing did: six slots fed one
offline scanner while `vocabulary`, `validation_rules` and `runbook` — the three built to be
read by a session — had no consumer at all. This renders them inside an explicit
untrusted-data envelope, hard-capped so a pack cannot flood a context, with every line naming
the pack and tier it came from. The envelope does not make the content true; it makes it
identifiable as someone's definition rather than as the system speaking, which is the only
property a renderer can provide. Wiring it into SessionStart is deliberately NOT done —
unattended injection into every session is a different risk from a command someone runs.

**`/gt:gt-optimize --demote` — the first motion the tool has ever had.** 0.16.0 reported and
wrote nothing, because the `--apply` that deleted things was removed after a validation found
seven ways it destroyed notes. Demotion is the shape that can be repaired: it moves a note to
where it costs less —

    global-memory/          read in EVERY session of every project     most expensive
    Projects/<slug>/memory/ read in every session of one project
    Knowledge/<page>.md     read when someone asks for it              cheapest

— and the ORDER is the safety property: write the destination, verify it by reading it back
from disk, and only then replace the source with a pointer. A failure at any step leaves
duplication, which a reader can resolve; the deleting version failed by removal, which no diff
brings back. `memory-bloat` findings now print the `--demote` command that acts on them.

**`/gt:gt-validation` — verification that expires.** Every serious defect in 0.16.0 was a claim
that outlived its implementation: a manifest row shape that stopped matching, a `lint` check
listed as running while wired to nothing, an aggregator counting installed rather than declared
members. Each was true when written, and nothing tied the claim to the code's current state. A
completed validation now records what was verified AND what it could not determine, stamped
with the file's content hash; edit the file and the recorded definition goes visibly stale. A
file with no receipt reports as *unknown*, which is deliberately distinct from clean.

**Five more languages.** Ruby, PHP, Java, C# and SQL ship as definition packs — `filetype`,
`construct`, `naming` and `encoding` — so `/gt:gt-scan` covers them with no code change, which
is the claim the registry was built to make good on. The `community/` tier stays empty: tier
means provenance, and a maintainer-written pack placed there would misrepresent the one thing
tier guarantees.

**Fixed.** `gt_optimize` still described itself as REPORTS ONLY after gaining `--demote` — the
same claim-outliving-code failure, in code written the same day, and exactly what the receipts
above exist to catch.

## gt 0.16.0 · gt-demo, gt-watch, gt-report-card, gt-farm, gt-flow 0.16.0 · gt-wiki 0.2.2 — 2026-09-16

**Contributions arrive by submission and review, not by a plugin runtime; the definitions they
carry get their first consumers; and a security fix for protected paths.**

**Protected paths (security).** `guard_protected_paths` compared paths as `realpath` strings.
On a case-insensitive volume (APFS) `realpath` returns the caller's spelling, not the on-disk
name, so a case- or Unicode-variant of a protected path resolved to the protected file and got
**no prompt**: `Global-Memory/…`, `CORE-RULES/…` and `~/.claude/Settings.json` were all silent
while their canonical spellings asked. The guard now compares **file identity**
`(st_dev, st_ino)` of the nearest existing ancestor, with an NFC + `casefold()` fallback —
never `str.lower()`, which misses `ſ` (U+017F) and the Kelvin sign.

**Contributions.** gt does not run third-party code. Contributions are **submitted, reviewed
and merged** into gt itself, after which they are first-party and held to the release gate.

- `dev/submissions.py` validates a contributed pack before a human reads it. The load-bearing
  rule: a pack's tier is **derived from its slot's reachability**, never believed from what the
  pack declares. A slot that cannot reach model context has no field for prose to live in, and
  that absence is the proof. It also refuses instruction-shaped text, ReDoS-prone and
  backreferencing patterns, subtractive packs, executable content, non-UTF-8 bytes, invisible
  code points, copyleft licences (in the pack *or* its upstream), and missing provenance or DCO.
- `scripts/gt_registry.py` resolves packs into one answer per key and names the source.
  Precedence is **community < core < local**: a merged contribution extends coverage but never
  silently redefines a core default, and the user's own packs always win. Shadowed entries are
  reported rather than dropped, and an unreadable pack is an error rather than a silent gap.
- Packs ship in `packs/core/` and `packs/community/` and are hash-verified in `MANIFEST.json`;
  the release gate re-validates every shipped pack, so "it was reviewed" stays true rather than
  becoming historical.
- `SUBMISSIONS.md` is the contributor-facing spec. gt is MIT permanently: no CLA, no
  relicensing, so a contributor's grant is final and the terms cannot change under them.

**The definitions get consumers.** A registry nothing reads is a registry nobody notices is
broken — the packs shipped hash-verified against a manifest row shape that never matched, and
were refused on every installation for weeks while the tests stayed green. Five commands now
read them, and `gt_registry.py slots` names the five slots that still have none.

- `/gt:gt-scan` checks code against the **language definitions in effect on this machine** —
  naming and encoding, per language. Nothing about any language lives in the script: the
  `filetype` and `construct` slots mean four small packs teach gt a language it has never seen,
  with no code change. It is an aggregator over leaf scanners, and reports how many members
  **ran** alongside what they found.
- `/gt:gt-optimize` reports vault content that costs context and earns nothing back: a fact
  duplicated across memory files, an index row pointing at a file that is gone, a
  `global-memory/` file over budget. It **reports only** — see below.
- `/gt:gt-handoff` gathers what the next session needs, labels every fact with its source and
  verification state, and deliberately refuses to write the design narrative.
- `/gt:gt-allin` runs every check in one command and never pushes or applies anything.
  `/gt:gt-allin-commit` is the separate, deliberate act: it commits only once a passing test
  receipt covers every staged file, and checks that receipt itself, because
  `guard_test_before_commit` is a PreToolUse hook that cannot see a script running git.
- **Local-only `retract`** answers both "this core rule is too noisy for me" and "which
  language packs do I want": `{"retract": [{"lang": "go"}]}` in a vault pack switches Go off
  and is reported as `RETRACTED` rather than hidden. Only vault packs may retract, so a
  contributed pack still cannot retire a core definition.

**The `secrets` slot ships with no consumer.** Credential scanning was built for this release and cut before it shipped, so there is nothing to miss — but the slot and its shape remain for the tool that will do it properly. `gt_registry.py slots` marks every slot nothing reads yet, so a pack written for one is a considered choice.

**Aggregators tell you how many checks ran.** Both aggregators originally counted the
**installed** members rather than the **declared** ones, so a partial install printed
`1 of 1 member(s) ran`, exited 0 having scanned nothing and linted nothing, and offered to
push — the exact failure they exist to prevent. The denominator is now what the command
declares it checks; a crash is no longer read as a finding; members have a timeout; and member
output is escaped and prefixed so it cannot forge the summary line printed above it.

---

## gt 0.15.0 · gt-demo 0.15.0 · gt-wiki 0.2.1 · gt-watch · gt-report-card · gt-farm · gt-flow 0.15.0 — 2026-09-14

**Three parts of gt become modules, and one new module draws the vault's history.** gt now
has sixteen skills; everything optional ships as one of six modules, each removable with
`bash install.sh --without <name>`.

- **`watch`** (plugin `gt-watch`, on): the upstream watch. The command is now
  `/gt-watch:gt-watch` (was `/gt:gt-watch`); its SessionStart hook, `gt_watch.py` and the
  `watch` setting moved with it. `--without watch` also removes the crontab line it
  installed — only the line tagged for it, after a backup.
- **`report-card`** (plugin `gt-report-card`, on): the session report card and the
  close-out question. No command; its PreCompact, SessionEnd and SessionStart hooks and the
  `report_card` and `closeout_check` settings moved with it.
- **`farm`** (plugin `gt-farm`): work packets for an external AI service, now
  `/gt-farm:gt-farm` (was `/gt:gt-farm`). **Off for a fresh install; kept on for upgraders** —
  a machine migration records `farm` as on for a machine upgrading from a gt that shipped
  `/gt:gt-farm`, unless a choice is already recorded. It names no particular AI service and
  its packet directory is configurable (`farm_packet_dir`).
- **`flow`** (plugin `gt-flow`, on, new): `/gt-flow:gt-flow` renders `events.jsonl` as one
  self-contained, offline HTML file — a lane per project, an arrow each time an item climbed
  a level. `--redact` replaces every name with a per-render salted hash and writes nothing if
  its self-check fails; `task.*` events are hidden until clicked or `--tasks`; `--project`,
  `--since`; an `--out` inside the vault is refused.

**Upgrading from 0.14.0 converges.** One install over 0.14.0 ends where a fresh 0.15.0
install made with the same module choices would: no SessionStart, PreCompact or SessionEnd
entry is left pointing at a gt path, your `watch`, `report_card` and `closeout_check` values
are kept, and rolling back to 0.14.0 removes the module plugins it does not know, so it is
not left with two copies of a skill.

**Module format extensions.**
- A module may declare **reporter hooks** and **hook-directory scripts** in `module.json`;
  they are tagged with the module's name, wired only while it is on, and removed when it is
  switched off. A Core-rule enforcement hook can still never belong to a module.
- **Module settings** may carry a long `detail`, printed by `gt_settings.py explain`. A value
  set for a module that is later switched off is kept and shown as not in effect.
- A module's skills may name another module's script; the demo resolves it at run time with
  `gt_demo.sh module-scripts NAME` and skips the act when that module is absent. The watch
  act moved from the core tour into the watch module: nine core acts, plus the wiki, watch
  and flow acts with the default modules.
- `gt_components.py module-states --detail` explains each module's state.
- `dev/check_retired.py` treats a hook script now provided by a module beside the release as
  **moved, not retired**, so install never deletes a live module file, and reports a
  `retired.json` entry naming such a file.

**Operations record movement events as they happen.** `gt_events.py` shipped in 0.13.0 with
nothing calling it; now:
- `gt_log.py add "<line>" --event <kind> --item <path> …` spools a log line and its event in
  one command (`/gt:gt-promote`, `/gt:gt-refresh`); `/gt:gt-review`, `/gt:gt-work` and
  `/gt:gt-ingest` call `gt_events.py emit`.
- `gt_adr.py allocate` emits `adr`; `vault_init.py` emits `create`, `rename`, `merge` and
  `archive` when the operation does something (never under `--dry-run`). `archive` comes only
  from `archive-project` — `gt_closeout.py answer <slug> yes` records a decision, not a move.
- `gt_tasks.py` emits `task.open` / `task.done` for changed checkboxes once the stream has
  been seeded.
- An event can never fail the operation it describes.
- **`gt_events.py backfill`** rebuilds a vault's history from git and `log.md` as
  `actor: backfill` events. `--dry-run` previews with samples; a second run adds nothing.

**Core rule `core_parallel_when_beneficial` reworded:** independent units run concurrently
*within one machine-wide budget shared by every session*, not a budget per session.

**Fixed**
- **Every vault-writing command in a shipped skill names its vault**, so following a skill
  never runs into the vault-write guard's denial. A test feeds every command in every shipped
  `SKILL.md` to the guard itself.
- The release gate's skill-reference step crashed on `golden-thread/0.1.0-archive.zip` and,
  with the exit code unread, printed ok. It now fails when it cannot run.
- The README and MANUAL examples for `gt_log.py` and `gt_adr.py` now pass `--vault`.
- **`--with <module>` puts back the crontab lines `--without` removed.** Turning `watch` off
  and on again reinstalled the module but never its hourly fetch, so the watch report stayed
  on and never changed, silently. The removed lines are now kept (mode 600, under
  `~/.claude/golden-thread/module-cron/`) and restored verbatim once the module is on, after
  a crontab backup. The watch hook also says so when watches exist but no gt-watch cron entry
  does.
- **Rolling back keeps gt-wiki and gt-demo.** `./install.sh 0.14.0` from a 0.15.0 tree skipped
  them, because only each plugin's newest release (built for 0.15.0) was considered. Each
  plugin now installs its newest release whose `requires_gt` admits the gt being installed,
  and the install banner names the release it chose and why.
- **An install no longer overwrites your vault's git hooks, tool edits or `core.hooksPath`.**
  A fresh-context validation reproduced each loss against vaults built by older releases.
  `.githooks/` was copied over on every install, so an uncommitted hook edit was gone with no
  backup; now a hook gt shipped is updated, an uncommitted edit is kept, and a committed one is
  backed up first. A vault tool was replaced when its file was *older on disk* than the
  release's copy — an owner's edit older than a freshly unpacked release was "stale", a
  pristine copy seeded later was "AHEAD". Tools are now compared by content against every
  text gt has shipped (`templates/shipped-hashes.json`, from `dev/shipped_hashes.py`): gt's
  own text is updated, anything else is kept as a local modification. `core.hooksPath` is set
  only when unset. The logic moved from `install.sh` into `scripts/vault_refresh.py`.
- **The install backs up the vault files it may change before its first write.** The only
  backup was gt_upgrade's, taken after the core-rule, CLAUDE.md, hook and tool refreshes, so
  it never held what they replaced. `install-vault-files-<stamp>.tar.gz` is kept only when
  the install changed something in it.
- **What an install puts back or converts is named.** A re-created Core rule, the re-inserted
  CLAUDE.md enforcement section and each converted `log.md` / `decisions.md` (with notes such
  as trailing blank lines dropped) are listed instead of "migrated 1 project(s)". A rule or
  the section listed in `.gt-removed` beside `core-rules/` is never re-created, by
  `install-core-rules` or by the upgrade's core-rules step.
- **A vault upgrade never removes lines from your PROTOCOL.md or CONVENTIONS.md unasked.**
  Releases before 0.14.0 recorded the owner's own document as its merge base; with base ==
  ours a "clean" 3-way merge *is* the template, so an install on a clean vault would have
  replaced the document — dropping owner sections and wikilinks while the dry run said
  "would merge cleanly (-16 lines)" (and "+0 lines" for six overwritten ones). A merge that
  would remove any line of the owner's document is now held: never a pending step, never
  applied by `install.sh`, reported as `needs a person: merge held` with the lines it would
  remove. `run --record-base <doc>` keeps the document (backing up the old base under
  `~/.claude/golden-thread/backups/`); `run --accept-merge <doc>` takes the merge. Dry runs
  report lines added and removed, not a net count.
- **The worker check no longer calls another live session's shells orphans.** It judged a
  worker by CPU and declaration alone, so a second session's SessionStart reported two wait
  loops of a live session as `ORPHAN` and offered `reap`, which would have killed them. It now
  walks each shell's parent chain: a shell with a live `claude` above it belongs to that
  session, is listed as information (session id, claude pid, uptime, and `WAITING on: <what
  it polls>` for a poll loop or a shell whose only children are `sleep`), and is never
  reaped — `reap` refuses it even if classification were wrong. `ORPHAN` now means no
  `claude` remains above it. This session's own undeclared workers are still raised.
- **Migrating or merging `log.md` and `decisions.md` no longer rewrites bytes that are not
  UTF-8.** `gt_log.py`, `gt_adr.py` and `gt_spool.py` read them with `errors="replace"`, so a
  latin-1 `é` in an old log became U+FFFD in both the frozen baseline and the generated file —
  and the round-trip gate compared two already-replaced copies, so it passed. Every read that
  feeds a write now uses `surrogateescape`, which carries any undecodable byte through
  unchanged; the baseline is written from the file's raw bytes and checked against them.
- **Rolling back to a gt from before modules ends where that release's own installer did.**
  `./install.sh 0.12.8` or `0.13.0` from a 0.15.0 tree removed gt-wiki: the releases those
  gts shipped with (0.1.2, 0.1.3) carry no `module.json`, so nothing admitted them. The
  installer now carries which plugin releases each gt release shipped with (`SHIPPED_WITH`,
  checked against git history by a test) and installs exactly those on a rollback. The same
  rollback also ignored `install_demo=no`: those gts carry the demo inside gt, and 0.15.0 had
  dropped the stripping. A `--without demo` this run, a recorded `demo: off`, or
  `install_demo=no` now leaves `/gt:gt-demo` out of that gt, as 0.13.0 did.
- **An upgrade says where moved commands went.** `/gt:gt-watch`, `/gt:gt-farm` and (from
  before 0.14.0) `/gt:gt-demo` simply stopped resolving after the upgrade. The installer
  now prints `Moved: /gt:gt-watch → /gt-watch:gt-watch` once for each skill the previous gt
  had and the new gt left to a module, naming the module to turn on when it is off.
- **An upgrade wires hooks in the same order as a fresh install.** Each writer appended its
  own entry, so an upgrade from 0.13.0/0.14.0 left `guard_protected_paths.sh` last in
  PreToolUse where a fresh install put it first. install.sh and `vault_init.py
  install-core-rules` now both apply one canonical order per event: blocks that are not
  purely gt's (your own hooks, or a block mixing yours with ours) first, in the order you
  had them; then gt's in `HOOK_REGISTRATIONS` order, module hooks after; then any other entry
  pointing into the gt hooks dir. Entries only move — nothing is added or removed by it.
- **File modes are set, not inherited.** `cp` gives a new file the source's mode and keeps an
  existing file's, so a fresh install from an untracked 0700 checkout left hooks at 0711 and
  plugin files at 0700 while an upgrade kept 0755/0644. Everything install.sh copies into the
  plugin cache, the marketplace and the hooks dir is now 0755 for directories, `*.sh` and
  `*.py`, and 0644 for every other file. A file of your own in the hooks dir is not touched.
- **`install-choices.json` is written one way.** `record-choice` wrote it 0644 with sorted
  keys, the machine migration 0600 with unsorted ones. Both now write 0600 — the mode of
  every other state file the installer keeps under `~/.claude` — with sorted keys.
- **`install.sh --vault` keeps an owner's `core.hooksPath` too.** The refresh step already
  did, but the `--vault` connect path (`vault_init.py` seeding) ran `git config core.hooksPath
  .githooks` first, so a custom hooks directory was still replaced. Both paths now set it only
  when it is unset; the chaining hint prints once.
- **Git hooks follow the vault-tool rule.** A `.githooks/` file is replaced only when its bytes
  match a hook some release shipped (`shipped-hashes.json`); an owner's edit, committed or not,
  is kept and reported as `MODIFIED LOCALLY`. Committed edits used to be backed up and replaced.
- **Hand-written lines in a generated `log.md` are kept.** Any render (`gt_log.py add`,
  including the receipt an install writes) rebuilt the file from the spool and deleted lines
  typed into it, with no word. They are now captured into `spool/log/hand-edits-<time>.md` and
  announced; a line starting with a date sorts by that date, an undated line after the
  baseline. `decisions.md` has no slot to guess, so `gt_adr.py merge` refuses and names the
  lines instead.
- **A merge base copied from the owner's own file is never merged against.** A pre-0.14 base
  identical to the document let a merge that only ADDED lines through, re-inserting a section
  the owner had deleted. Such a merge is now always held for a person, whatever it adds or
  removes. Merges also keep the document's bytes and line endings (CRLF stayed CRLF only by
  luck of the text-mode pipeline; it is now bytes end to end).
- **Upgrade and fresh install write the same JSON key order.** The hook EVENT keys in
  `settings.json`, `enabledPlugins` and the plugins in `installed_plugins.json` kept an older
  release's insertion order. Your own events and other marketplaces' plugins stay first, in
  the order you had them; gt's follow in one fixed order.
- **No bytecode is left behind.** Python run by the installer wrote `__pycache__` into the
  plugin cache, the marketplace copy and, on the `--vault` path, the vault's own tools
  directory, differently each run. The installer now runs with `PYTHONDONTWRITEBYTECODE=1` and
  strips `__pycache__` from what it installs; `marketplace.json` gets its mode set like every
  other installed file.
- **The Moved notice reflects where the install ends.** It was computed before the machine
  migrations and said `module farm is off` for an upgrader whose farm a migration then turned
  on. The off/on tail is now added from the final module state.
- **A rollback removes what a newer release wired.** `./install.sh 0.12.8` from a newer tree
  left the newer `guard_protected_paths` hook wired and its files in the hooks dir, which
  0.12.8 reported as drift every session. Hooks-dir files byte-identical to a copy a newer
  release (or module) in the tree ships, and entries for scripts those releases register, are
  removed when the older gt does not ship them; files of your own are left alone.

## gt 0.14.0 · gt-demo 0.14.0 · gt-wiki 0.2.0 — 2026-09-14

**An upgrade now finishes the job without you.** 0.13.0 removed what older releases left
behind; this release adds the other two pieces an upgrade from any older release needs.

- **Machine migrations** (`gt_machine_migrate.py`, run by `install.sh`): one-time changes
  under `~/.claude/` that a skipped release would have made. Each is applied once, judged
  from the machine's actual state rather than a record, stops the install at the first
  failure (`INSTALL INCOMPLETE`, exit 7), and backs up what it changes. The first one records
  your demo choice in `install-choices.json`, ready for optional modules.
- **Vault upgrades are applied by the install** when the vault had no uncommitted changes
  before the install touched it (after a backup; results left uncommitted for review). A
  vault the same install created gets an initial commit. Your own uncommitted work is never
  touched — the install prints the command instead.
- **Your edits are never merged away.** A PROTOCOL.md or CONVENTIONS.md with no base gt
  recorded is never merged unattended: it is reported as *needs a person* until you review
  it and run `gt_upgrade.py run --record-base <doc>`. Connecting an existing vault no longer
  records a base for an edited document. A merge conflict is reported, not re-run on every
  install.
- **`install.sh` installs every plugin the release ships**, discovered by the same rule as the
  release gate, instead of naming gt and gt-wiki. `--list-plugins` shows what it found.

**Optional parts are modules you can decline.**
- A module is a separate plugin in the same marketplace, declared by `module.json` and
  versioned with gt. **`wiki`** (gt-wiki 0.2.0) and **`demo`** (gt-demo 0.14.0) ship as modules,
  both on by default.
- `bash install.sh --without demo` removes a module completely — plugin cache, marketplace
  entry, enabled flag, hooks and hook scripts — and remembers the choice; `--with demo` brings
  it back; `--list-modules` shows each module's state and why.
- **The demo moved out of gt.** It is now `/gt-demo:gt-demo` (was `/gt:gt-demo`). `remove`
  deletes the demo vault and points to `install.sh --without demo`. If you had
  `install_demo: no`, the upgrade keeps the demo off.
- Modules may declare hooks, tagged with the module name and wired only while the module is
  on; a module can never claim a Core-rule enforcement hook. Module settings appear in
  `gt_settings.py show` under the module's name; gt's `install_demo` setting is gone.
- gt skills that point at the wiki say how to install it when it is off, and the version
  check reports a declined module as "not installed by choice".
- `/gt:gt-doctor` gains a modules check; the release gate validates every `module.json` and
  that its `requires_gt` admits the gt being released.

**The demo shows what you have installed.** `/gt-demo:gt-demo` assembles its tour when it
runs: gt's core acts plus one act from each installed module that ships one
(`module.json` → `demo`), placed before the closing act so its work appears in the receipt.
The wiki act now lives in the wiki module — install it and the act appears; remove it and
it is gone. Third-party extensions will not supply act text of their own.

**Fixed, found by this release's regression**
- **New vaults and new projects start current.** Since 0.11.0 `vault_init fresh` created
  `log.md`, and `create-project` created `decisions.md`, in the pre-spool layout, so every
  new vault and project was born one upgrade behind. Now that installs apply upgrades, a
  fresh install would have ended with changes to review on a vault created seconds earlier.
- **`merge-project` no longer loses decisions.** It wrote the source project's ADRs into the
  destination's generated `decisions.md`, and the next re-render deleted them. They now go
  into the destination's spool, the ADR count is verified before anything is removed, and a
  failure part-way leaves both projects as they were.
- **`rename-project` moves the decisions spool** with the project, so the renamed project
  does not read as unmigrated and its next ADR does not restart at 1.
- **`--dry-run` means nothing changes** for `merge-project` (it moved notes and deleted files)
  and `rename-project` (it renamed the folder).

## gt 0.13.0 · gt-wiki 0.1.3 — 2026-09-14

**Upgrading from any older release now lands you on the same install a fresh one would.**
`install.sh` installs only the newest release — it never steps through the ones in
between — but until now it only ever *added*: a hook entry or hook-directory file an older
release installed stayed forever once a newer one stopped shipping it. This release makes
convergence a requirement and the first mechanism behind it.

- **`retired.json` ships in every release** and records everything earlier releases
  installed into `~/.claude/golden-thread/hooks/` or wired into `settings.json` that this
  one no longer does. `install.sh` removes exactly those, after a backup, and prints each
  removal. Anything it does not recognise is reported and left in place — it never guesses.
- **A new release-gate step, `check_retired.py`,** fails any release that stops installing
  or registering something without recording it, so an unrecorded removal cannot ship.
- **Old gt-wiki cache versions are pruned**, as gt's already were.
- **Pending vault upgrades are shown at the end of every install**, with the command to
  apply them. They are reported, not yet applied automatically — see below.
- A test installs 0.12.8 with a vault, upgrades it to 0.13.0, and asserts the hooks,
  hook-directory files and plugin caches match a fresh 0.13.0 install, with the user's own
  hooks and files intact.

**The vault-write guard reads commands, not strings.** It blocked a `git commit` whose
*message* mentioned `gt_log.py add`, and read-only commands that merely named a tool —
`grep`, `sed -n`, `diff`, `cat`, and `--help`. It now objects only when a vault tool is the
program actually being run, including through `python3`, `env`, `sudo`, `bash -c` and
`$(…)`. Every existing denial still holds.

**New and extended tools**
- **`gt_events.py`** (vault tool): structured movement events — `emit`, `merge`,
  `validate`, `list` — on the same per-session spool pattern as the log. Schema v1 is
  validated before anything is written. Other tools start emitting in a later release.
- **`gt_tasks.py --json`** prints the ranked rollup without writing `TASKS.md`. Deadline
  windows can **span days** (`Fri 16:00 → Sun 16:44 America/Chicago`), including spans that
  wrap the week.
- **`gt_lint.py --json`**, and **`gt_lint.py --runbooks`**, which reports lines duplicated
  across projects' runbooks. `/gt:gt-runbook-lint` said a script did the detection; now one
  does.

**The public release no longer assumes one person's machine.**
- `gt_doctor`'s gt-src check reads `gt_src` from `vault-config.json` (or `$GT_SRC`) and
  reports *not configured* instead of checking a hard-coded folder.
- The weekly lint report goes to `lint_report_dir`, else the folder earlier releases used
  if it already exists, else `<vault>/.gt/lint`; it says so when gt-wiki is not installed.
- `/gt:gt-farm` no longer depends on a project only one vault had.
- The demo tour resolves its vault and scripts instead of hard-coding them.
- The announcement check reads Discussion bodies as well as titles, and `0.12.1` no longer
  matches `0.12.10`.

**Release machinery**
- Every build tool discovers plugins through one rule (`dev/plugins.py`) instead of naming
  gt and gt-wiki, so a future module is gated, packaged and published automatically.
- **gt-wiki 0.1.3 ships a `MANIFEST.json`**, and the gate fails any plugin without one.

**Not yet:** applying vault upgrades during install. `gt_upgrade run` refuses a vault with
uncommitted changes, and a vault created moments earlier by the same install is always
uncommitted — so turning it on needs that settled first.

## gt 0.12.9 — 2026-09-13

**The guards were approving tool calls they meant to ignore.** Upgrade promptly.

All three PreToolUse guards — the session-claim guard, the vault-target guard and the
commit-test guard — answered `permissionDecision: "allow"` for every tool call they did not
block, and they run on every tool call. Claude Code's hooks reference defines `allow` as
"skip the interactive permission prompt"; the neutral answer is to exit 0 and print
nothing. So a guard written to stay out of the way was instead waving calls past the
permission prompt you would otherwise have seen. Deny and ask rules in your settings still
applied, which is why nothing looked wrong.

- **Every guard now prints nothing when it has no objection**, including when it fails
  open on an error. Denials are unchanged. A regression test per guard asserts empty
  output for a harmless call, and those tests fail against 0.12.8.
- The commit guard's warn-mode note is still delivered, as context with no decision.

**Protected paths now need a person to see the write.** Until this release nothing but
skill prose stopped a session's Write or Edit tool from changing the files every session
depends on. A new guard, `guard_protected_paths`, and a new setting, `protected_paths`
(`ask` by default, or `off`):

- A Write or Edit to the vault's `core-rules/` or `global-memory/`, to
  `~/.claude/golden-thread/`, or to `~/.claude/settings.json` always shows the permission
  prompt, in any permission mode. Approving it is fine when the change is intended.
- Editing or overwriting an **existing** file in `Sources/` is refused — supersede it with
  a new file. Creating a new source is unaffected.
- Paths are resolved first, so `..` and symlinks do not get around it. It does **not** see
  shell commands that write the same files.

**The report card was never shown.** It runs at `/compact`, automatic compaction and
session end, and output at those events reaches neither you nor the assistant. It now
saves the card, and a new session-start step shows it at the start of your next session —
including the close-out question, which the assistant is told to put to you. Its
docstring also stopped claiming it never writes to the vault: its close-out step appends
`closeout-signals.jsonl`, and `gt_closeout.py ask` / `answer` are now covered by the
vault-target guard.

**The release gate scrubs everything a push publishes.** It used to scan a list of plugin
directories, so files at the repository root were published unscanned — and one that
named internal systems did reach the public repository before it was removed.
`scrub_check.py --repo` scans tracked files plus untracked files that are not ignored, and
the gate now uses it.

Hooks wired by `install.sh` go from 7 to 9: the session-start report-card step and the
protected-path guard. Re-run `bash install.sh` and restart Claude Code.

## gt 0.12.8 — 2026-09-12

**`gt_paths.py` shipped twice, and one of the copies was three releases stale.** If you
have ever seen a drift report you could not clear, this is why.

Two requests arrived from another machine. One asked to promote its installed
`gt_paths.py` into the plugin, on the reading that the installed copy was the richer one
and the source lacked the `gated_by` / `budget_from` keys. Half right, and the diagnosis
inverted:

- `scripts/gt_paths.py` has had those keys since **0.12.4**. The installed copy was
  byte-identical to it.
- `hooks/gt_paths.py` — a **second copy in the same release** — was the 0.12.2-era file
  without them, and had been stale in every release from 0.9.13 through 0.12.7.

Both install to the same destination. `install.sh` copies `hooks/*` first and then
overwrites with `scripts/gt_paths.py`, so the correct file won — **by ordering, not by
design** — while `MANIFEST.json` kept a hash for each path. The drift check therefore had
to disagree with one of them on every machine, forever, and no user action could clear it.

It got worse in 0.12.7. Resolving direction by identity made that row read `stale`, which
is the **auto-appliable** state: with `component_updates=auto` the check would have copied
the 0.12.2 file **over** the correct one, silently disabling the keys the parallel Core
rule reads to know which setting governs it. A phantom that became destructive.

- **`hooks/gt_paths.py` is deleted.** One file, one home: `scripts/`, installed to the
  hooks directory by `install.sh`, exactly as the other hook-dir scripts are.
- **`gt_components.duplicate_destinations()`** reports any destination claimed by more
  than one shipped file, using the same mapper the drift check uses so the two cannot
  disagree about where a file goes. Wired into the release gate: pointed at 0.12.7 it
  names the defect, at 0.12.8 it reads clean.
- Two end-to-end tests make the original report impossible to reproduce: the installed
  copy carries both keys, and a freshly installed tree reports no drift on `gt_paths.py`.

### `gt_upgrade.py status` no longer reports success as failure

Pending migration steps exited 1, so every interactive `/gt:gt-upgrade` rendered as a tool
error — training the reader to ignore a check whose whole job is to be read. `status`
succeeded: it looked, and found pending steps. It now exits 0. Nothing branched on the
code (`gt_doctor` computes pending itself, the skill reads the printed output), verified
before changing it, and the printed output is unchanged and asserted so.

**The test written for that request then found a second bug**: `status --vault
<missing-path>` exited **0** and reported on the nonexistent path as an un-upgraded vault
— "never stamped, every migration is offered" is an alarming thing to print about a typo.
`cmd_run` had always refused a missing vault; `status` never did. It now exits 2, which
also keeps the command able to fail at all now that pending is 0.

Two assertions pinning the old behaviours were **inverted rather than deleted**, each with
its original reasoning recorded, so neither can return from the argument that produced it.

## gt 0.12.7 — 2026-09-12

**The installer checks that what it is about to install matches its manifest, and the
drift check stops claiming a direction it cannot prove.** Two halves of the same failure,
both reported the same day.

### install.sh verifies the source against MANIFEST.json

Requested 2026-09-11 (`install-refuse-stale-manifest`), and the refuse-or-warn question
it existed to settle was decided by the repo owner on 2026-09-12:

- **Refuse** when a file that **executes** (`hooks/`, `scripts/`) disagrees with the
  manifest — exit 6, the file named, the regenerate command named, and **nothing
  installed**.
- **Warn** when only copied files (`templates/`, `skills/`) disagree; the install
  completes.
- **Exempt** a developer's uncommitted or untracked edit, per git, downgrading it to a
  warning. Editing a script and installing to test it is the normal loop here, and a gate
  that fires on the normal loop gets overridden by reflex and then ignored.
- `--force-manifest-mismatch` installs anyway, and says so.

It matters on a machine that is not the one running the release gate: since 0.12.6 the
installer no longer regenerates the manifest, so a second machine installing from `gt-src`
could install files the manifest does not describe, be told nothing, and then report drift
at every session start for a mismatch the installer could have caught once. This earned
its own release the same day it was wanted: 0.12.4 shipped with a stale `MANIFEST.json`,
and only the gate saw it.

### `ahead` no longer means "we guessed from an mtime"

Another machine reported `hooks/gt_paths.py` as `ahead` of the plugin source at every
session start, with the advice *"installed is NEWER — the plugin source needs updating from
it"*. The installed file turned out to be **byte-identical** to what the plugin ships, and
the direction had never been established: `install.sh` copies with plain `cp`, so every
installed file carries the install-time mtime and is **always** newer than its source. Any
content mismatch therefore read as `ahead`, which is never auto-applied — a permanent
warning, pointing the wrong way, with no way out.

- Direction is now decided by **identity first**: a copy whose hash matches the same file
  in another release on disk, or in the plugin cache, is a leftover from that install and
  is `stale` — reportable and applicable.
- When nothing local can establish direction, the state is **`differs`**, which says so
  plainly and prints the three resolutions with real paths: diff them, capture the
  installed copy into the plugin, or delete it and re-install. It is never auto-applied,
  exactly as `ahead` never was.
- `ahead` is still recognised, so nothing that consumed it breaks.

**Also:** the test fixture now regenerates its own manifest, so a working tree mid-edit
does not fail every install test for a reason unrelated to the test. And a test that pinned
"a stale manifest still installs" was **inverted rather than deleted**, with the original
intent recorded in it, so the old assertion cannot come back from the reasoning that
produced it.

## gt 0.12.6 — 2026-09-12

**The install-time machine measurement actually happens now.** Take this if you have
0.12.5.

0.12.5 introduced `parallel_profile`, measured at install so `parallel_max: auto` means
the machine in front of you. It ran as step 6b of `install.sh` — **before** `setup_vault`
creates `vault-config.json`. On a machine that already had a config it worked, which is
every machine the author tested on. On a **fresh** install there was nothing to write into
and the step skipped silently, so the profile appeared only where one already existed and
`auto` fell back to reading the machine live on every call.

Found by installing into a throwaway `HOME` and looking for the value, rather than
trusting the installer's output — which said nothing either way. Measured before and after
on an otherwise empty home: `parallel_profile` ABSENT with the 0.12.5 ordering, and
`{cores: 16, cpu_max: 16, io_max: 32}` with this one (`self-verified`).

- The measurement moved to **after** the vault is configured, and the installer now prints
  what it recorded.
- Two tests assert the **value**, not the delivery: a fresh install records a profile with
  every field, and re-running the installer does **not** overwrite a `parallel_max` or
  `parallel_work` the user chose. Every existing check passed through this bug — the files
  arrived, the hooks wired, the gate was green — because they all asked whether things were
  *installed*, and none asked whether the number was *there*.

Nothing else changed; the 0.12.5 payload is otherwise identical.

## gt 0.12.5 — 2026-09-12

**A tenth Core rule: code is not committed until its tests have been seen to pass. Plus
`auto` now measures your machine instead of guessing, and `publish` is one command.**

- **`core_test_before_commit`** (Core/**Validated**) — enforced by a third `PreToolUse`
  guard, on `git commit`. Evidence is a **receipt**: `tests/run.sh` and
  `dev/release-check.sh` write one when they pass, any project writes one with
  `gt_test_receipt.py record --ok`, and a receipt covers a file only if it is **newer**
  than that file — so editing something after the run invalidates it automatically, with
  nothing to remember. Setting `test_gate`: `off`, `warn`, `auto` (**default** — block
  where the repo has a discoverable test command, warn where it does not), `block`.
  **Per-repo opt-out:** `touch .gt-no-test-gate`. Per-commit: `GT_TEST_GATE=off`. Never
  blocked: docs-only commits, and anything the guard cannot parse — it fails open.

  It exists because 0.12.4 was committed and pushed with a **stale `MANIFEST.json`**. The
  gate that catches exactly that had been run, then several more edits followed. The
  discipline was fine; it had no mechanism.

- **`parallel_max: auto` is now measured, not assumed.** `install.sh` records a
  `parallel_profile` for the machine — cores, physical cores, memory, and the resulting
  `cpu_max` / `io_max` — and re-measures at every upgrade, which is exactly when the
  hardware may have changed. It never touches `parallel_work` or `parallel_max`: the
  profile records the hardware, those record what you will allow. On a 16-core/128 GB
  machine `io_max` comes out at **32**, where 0.12.4 hardcoded 20 — a number that was
  right for the laptop it was chosen on and arbitrary everywhere else.

- **A ceiling you set is checked against the machine.** `set parallel_max 40` on a
  16-core box is refused, with the numbers; a value above core count is accepted with a
  note that it helps I/O-bound work and does nothing for CPU-bound work. `--force`
  covers the case the profile cannot see, like a fan-out bounded by the network.

- **`dev/publish.sh` — publishing is one command.** Gate, committed, pushed, gt-src,
  announced, logged, in order, stopping at the first failure. The requirements are data
  at the top of the script and `--list` prints them; there is no `--skip`, because a step
  you may skip is not a requirement. 0.12.4 shipped with gt-src left a release behind,
  which is invisible from this repo: everything here was correct and only the copy the
  other machine reads was stale.

- **The hook-wiring list had a second copy, and now does not.** `vault_init.py` kept its
  own hand-maintained list of which hooks to register — the same duplication that shipped
  `guard_session_claims.sh` unwired in 0.9.5. It now reads
  `gt_components.HOOK_REGISTRATIONS`, the one declaration `install.sh` already used.

**Two bugs caught by the new rule's own tests**, both worth naming because each would
have shipped silently: the commit guard's heredoc regex referenced a capture group that
did not exist, so the guard threw on every call and the wrapper failed open — an inert
guard that reported nothing; and an import failure in the guard now **announces** that it
is degraded rather than quietly allowing, the same principle as
`inject_core_rules.sh`'s ENFORCEMENT DEGRADED banner.

## gt 0.12.4 — 2026-09-12

**A ninth Core rule: parallel execution is the default for divisible work, in every
project — and a setting that caps it.**

The serial default was never a decision. A loop, a sweep over 43 projects, a backtest and
a test suite all run on one core unless someone says otherwise, and nothing reports the
waste: the work completes, correctly, slowly, and its output is identical to the fast
version. Measured here on the `gt` suite itself — **509s serial against 108s parallel,
4.7x, CPU from roughly one core to 404%**, the same 672 tests passing both ways
(`self-verified`). That speedup had been available on every previous run.

- **`core_parallel_when_beneficial`** (Core/Reminder, designated by the repo owner) —
  work that splits into independent units runs concurrently, up to the configured
  budget; serial execution must be justified rather than assumed. It governs both halves
  of the job: what gets **run** (builds, suites, sweeps, migrations, host fan-out, in any
  language on any host) and what gets **written** — a tool authored for divisible work
  gets a `--jobs` flag defaulting to the budget, because a script that can only run
  serially makes the mistake permanent for everyone who runs it later. Repetition is the
  strongest trigger: the cost is paid on every iteration, and each run still looks fine.
  It also names where parallelism is *forbidden* — shared-file writes, order-dependent
  steps, rate-limited remotes, production changes, and any job whose partial failure
  leaves a state nobody can reason about.
- **Two settings, `parallel_work` and `parallel_max`.** `parallel_work=off` runs
  everything serially *and stops the rule being injected* — a rule the user has switched
  off must stop being asserted, or the registry is decoration. `parallel_max` is `auto`
  (as many as the machine allows) or a worker ceiling; `auto` is deliberately not a
  number, since a stored count is wrong on the next machine and this config syncs
  between them. `gt_settings.parallel_jobs()` is the single place the two turn into a
  worker count, so no caller invents its own policy.
- **The mechanism is generic, not hardcoded.** A rule file may now declare `gated_by:`
  (the setting that can switch it off) and `budget_from:` (the setting whose value is
  appended to the injected line). `inject_core_rules.sh` reads both from the rule, so the
  hook never learns a rule's name — the same reason rule *text* has never lived in a
  script. The injected line carries the current ceiling, because a budget the model
  cannot see is a budget it cannot honour.
- `tests/prun.py` now takes its default worker count from those settings instead of
  keeping a private policy, with `-j` still winning, then `GT_TEST_JOBS`, then the
  settings, then the old I/O-bound fallback for a clone with no `~/.claude` at all.

**What you will notice after upgrading.** Runs that used to occupy one core now occupy
all of them: many processes at once, CPU above 100%, audible fans. Nothing is runaway —
the processes end when the run does. If you would rather it stayed modest,
`gt_settings.py set parallel_max 4` caps it and `set parallel_work off` turns it off
entirely, rule included.

**Two stale counts fixed on the way past.** The docs had said "seven Core rules" since
0.9.10 while eight shipped — `core_explicit_vault_target` was added in 0.12.0 and no
count moved — and the repo-root README's own table listed six of them. Both now state
nine, four validated, counted from the rule files.

## gt 0.12.3 — 2026-09-12

**A name for the installer fix, and a gate so the next one cannot go unnamed.**

The plugin payload is byte-identical to 0.12.2. This release exists because 0.12.2's
installer bug was fixed and committed *without* a version bump, so two published states
both called themselves 0.12.2: one whose `install.sh` wires the Core-rule enforcement
hooks and one whose `install.sh` does not. "Which version wires correctly?" had no
answer. If you have 0.12.2, take this.

- `dev/check_installer_version.py` fails the release gate when `install.sh` or
  `selftest.sh` has changed since the newest version directory was cut — committed or
  still dirty. `MANIFEST.json` hashes only what lives inside a version directory, so the
  file a user actually runs had nothing covering it at all. It decides from git history
  rather than a recorded hash, because regenerating a manifest after editing the
  installer would quietly bless the edit — the very move that caused this.

## gt 0.12.2 — 2026-09-12

**Reported from a second machine: `guard_vault_writes.sh` still unwired.** It was right,
and the bug had survived two fixes because each was confirmed from the one vantage point
where it already worked.

- 0.12.1 put the wiring call inside the block gated on the vault being a **git repo**. A
  vault that is not a repo installed with the enforcement hooks inert — and was then
  reported as no vault at all. Git decides whether the *attribution* hooks can be wired;
  it has nothing to do with an entry in `settings.json`. The wiring now depends only on a
  vault existing.
- **`dev/check_wiring_coverage.py` is a new release gate**, and it is the durable answer:
  it installs into a throwaway home — including the upgrade path, where a vault already
  exists — and then asks every shipped hook, hook-dir script, skill, script, vault tool
  and Core rule whether it reached its destination. Nothing is listed by hand; each set is
  read from the release, so a file shipped tomorrow is covered tomorrow. A hook that ships
  but is registered nowhere is itself a finding.
  Every other check reported "clean" through all three releases: the manifest matched, the
  files were present, the registration list agreed with itself. Only doing the install
  catches an inert hook.

### Earlier in 0.12.2



**A vault is part of the install, not a thing to remember afterwards.**

The enforcement hooks are wired against a vault, so an install without one ends with
them present and inert — and the next session start reports them unwired, which reads
as a broken install.

- `install.sh --vault <path>` creates or connects a vault and wires everything, in one
  command. `GT_VAULT` does the same. `--no-vault` is the deliberate opt-out.
- With no vault and no flag: at a terminal it asks where the vault should go; anywhere
  else — an agent, a pipe, CI — it stops with **exit 4** and says what it needs. It
  never invents a directory or claims `~/.claude/vault-config.json` unasked, because
  where your memory lives is not an installer's decision.
- Every new vault carries `OPEN-IN-OBSIDIAN.md` with its own path filled in: how to
  open a folder as a vault, which plugins the conventions actually rely on (Dataview,
  because `[p:: 1]` inline fields are its syntax; Obsidian Git, because the vault is a
  repo), and which generated files must not be hand-edited.
- `--help` prints real usage. It had been extracting a nearby comment block, and
  printed the rollback instructions instead.

## gt 0.12.1 — 2026-09-12

**A hook that ships inert is worse than one that does not ship.**

0.12.0 added a fourth enforcement hook. `install.sh` wires only the seven hooks it owns;
the enforcement hooks belong to `vault_init.py`, which ran them only when a vault was
*created*. So every machine that already had a vault installed the new hook and never
registered it, and reported it unwired at session start with no instruction that would
fix it — while the component check called the install clean.

- `install.sh` now wires the enforcement hooks whenever a vault is already configured,
  so an upgrade registers a newly shipped hook instead of copying it and stopping. The
  call is idempotent; a second install says "already wired".
- The vault-write guard no longer inspects heredoc bodies. It denied a command that was
  *writing* a script containing a tool call — a guard that fires on a quoted mention is
  one people switch off, and then it guards nothing.
- Only the current and previous releases stay on disk, so 0.11.0 is now in git only.

## gt 0.12.0 — 2026-09-11

**A vault tool must be told which vault it means.**

A session about to migrate a vault copied it to a scratch directory first, to see the diff
before touching anything real. One tool took `--vault`, so that half stayed in the copy;
another resolved the vault from config instead, so the same rehearsal migrated every
`decisions.md` in the live vault. Nothing was lost, and the intent was the exact opposite of
what happened.

- A `PreToolUse` guard denies a vault-mutating tool run that names no target — no `--vault`,
  no `--dry-run`, no `GT_VAULT`. Read-only subcommands are never denied, and anything the
  guard cannot parse with certainty is allowed: it fails open, because a guard that blocks
  wrongly makes every session unusable.
- The release gate enforces that every shipped vault tool accepts `--vault` and `--dry-run`,
  so the gap cannot reopen.
- `install.sh` no longer rewrites `MANIFEST.json` in the tree it installs from. Every run
  used to change its timestamp, dirtying git and silently modifying any shared copy someone
  installed from. The drift protection moved to the release gate, which verifies the manifest
  against the tree on every release.
- The end-of-install skill summary is derived from what was actually installed. It had been a
  hardcoded list, and told new users that a shipped skill did not exist.

**Two new commands, and thirteen fewer release directories.**

- `/gt:gt-upgrade` updates the VAULT after `install.sh` updates the plugin — the step that did
  not exist, which is why adopting 0.11.0 meant reading source for the migrations and running
  them by hand across 43 projects. It records a version stamp, keeps the merge base for
  `PROTOCOL.md` and `CONVENTIONS.md` inside the vault, rehearses with `--dry-run`, refuses a
  dirty tree, backs up before applying, and reports the steps that need a person (a duplicate
  ADR number, a document conflict) rather than guessing at them.
- `/gt:gt-doctor` answers "is this install healthy?" in one command: version, component drift,
  hook wiring, pending migrations, stray workers, unpushed commits, publish-destination drift,
  lint. Exit 2 means a check *could not run*, which is deliberately not the same as clean.
- `sync-gt-src.sh` names the files in the publish destination that it did not write, before
  the backup and the delete. A flat 0.9.13-era `scripts/` and `templates/` had appeared there
  and were removed inside forty lines of rsync output where nobody could see it.
- The migration round-trip gate compared both sides with `rstrip`, so a trailing blank line
  could be dropped while the tool reported a byte-identical round trip. It now compares
  exactly and says so when the rendering differs.
- Only the current and previous releases stay on disk. Earlier ones are in git; `install.sh`
  documents the two-step rollback.

## gt 0.11.0 — 2026-09-11

**`log.md` and `decisions.md` become generated files, so concurrent sessions stop colliding.**

More than one session works a vault at a time and they share one working tree. There are no
branches to collide, so there is no merge and no conflict marker — just a last writer who
wins silently. One session appended a line to `log.md`, committed it, and carried nine more
written by earlier sessions that happened to be uncommitted.

- Each session appends to a spool file only it writes; the shared file is rendered from those
  spools, the same relationship `TASKS.md` already had. New tools: `gt_log.py`, `gt_adr.py`,
  and the shared `gt_spool.py`.
- ADR numbers are allocated with a single atomic exclusive create, so two sessions cannot both
  take ADR-6 — which had already happened. A read-then-write counter does not fix this: that
  is two operations, so both sessions read 5 and both write 6. Verified under contention at
  100 racing process pairs, 200 allocations, zero duplicates.
- Migration (`gt_log.py migrate`, `gt_adr.py migrate <project>`) freezes what is already there
  as a baseline that sorts first — **no history is rewritten and no ADR is renumbered** — and
  refuses unless the merge reproduces the original file byte for byte. It refuses outright on
  a project whose ADR numbers already collide.
- `gt-lint` gains `adr-collision` (which ignores a deliberate `## ADR-6 amendment:`) and
  `generated-hand-edited`.
- The release scrub can extract PDF text on another host, so the machine that builds releases
  no longer needs a PDF library installed to scan for leaked strings.

## gt 0.10.0 — 2026-09-11

**`gt-watch`: watch any git repo, and open the next session with a P0 when it ships a
security fix.**

## gt 0.9.14 / gt-wiki 0.1.2 — 2026-09-11

**The 38 defects the test harness found, and a demo that never touches your vault.**

The first release after a test harness was written for every shipped tool. `gt-demo` runs a
guided tour against its own throwaway vault.

## gt 0.9.13 — 2026-09-10

**The component check verifies that hooks are *wired*, not merely installed.**

A machine had every file installed and the component check reporting clean, while no
session-start hook had ever run — its settings had no entries at all. The manifest recorded
only files, so "components clean" truthfully meant "the files are present" while nothing was
connected to anything.

- `MANIFEST.json` declares the hook entries each release expects, and the check compares them
  against live settings. Two states: `unwired` (nothing references the script) and `badpath`
  (wired, but naming a path that does not exist on this machine).
- `install.sh` registers from that same declaration and verifies what it wrote, so the
  installer and the checker cannot disagree about what "wired" means.
- The install summary moved to the end of the run; hook registration used to print below
  "Restart Claude Code", past what reads as the end of the output.

## gt 0.9.12 — 2026-09-09

**`gt-route` — a routing check for the middle of a session.**

## gt 0.9.11 / gt-wiki 0.1.1 — 2026-09-08

**The linters see the vault the way Obsidian does, and a weekly lint runs itself.**

## gt 0.9.10 — 2026-09-07

**A seventh Core rule: secrets rest only in the store.**

## gt 0.9.9 — 2026-09-05

**The plugin stands on its own, and `selftest.sh` proves it.**

## gt 0.9.8 — 2026-09-05

**An inbox the rollup renders, and a close-out question that learns.**

## gt 0.9.7 — 2026-09-05

**The six findings from the 2026-09-05 review of the public repo.**

## gt 0.9.6 — 2026-09-02

**Two fixes that had lived on one machine since 0.8.0, and two truncation paths in
`safe_write`.**

`write(mode="a")` applied the mode to a temporary file rather than the target, so an append
replaced the file with only the new bytes. An independent validator then found that the first
fix left a second path open: `os.path.exists()` answers False for "I cannot tell" as well as
"not there".

## gt 0.9.5 — 2026-08-31

**The `PreToolUse` guard was copied to every machine and registered on one.**

## gt 0.9.4 — 2026-08-29

**The enforcement layer could not tell what it had installed.**

## gt 0.9.3 — 2026-08-22

**The scaffold now emits a `## Tasks` section.**

## gt 0.9.2 — 2026-08-22

**The secrets validator blocked correct work and missed real credentials.**

## gt 0.9.1 — 2026-08-22

**`gt_lint` could never resolve a project link.**

## gt 0.9.0 — 2026-08-22

**Ships `gt-validate`, and documents how the plugin is actually used.**

## gt 0.6.0 — 2026-08-16

**The two-path Core model, canary rationale, and hook-path corrections.**
