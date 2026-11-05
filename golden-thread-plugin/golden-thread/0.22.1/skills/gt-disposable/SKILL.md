---
name: gt-disposable
description: "Run work a gate refuses on this machine in a throwaway VM or container instead, without loosening the gate: the tree is copied in, the command runs, you get the exit code and output, the guest is destroyed. Use when the user says: run this in a disposable VM, run this in a throwaway container, run it somewhere disposable, run the tests in a sandbox VM, my test run is blocked can it run elsewhere, run this without touching my machine."
plain:
  - cmd: "/gt:gt-disposable"
    does: "Runs a blocked command or test run in a throwaway VM or container, then destroys it."
    how: "Copies the work folder in (nothing is shared or mounted, and your Claude folder, the vault, keys and credentials never go), runs the command there, brings back its exit code and output, and removes the guest even if the run failed. It shows the plan first."
    detail: "backends: tart, docker, podman, and bwrap on Linux; `gt_disposable.py backends` lists which exist here. Not the same as gt sandbox mode, which fences Claude's own tools."
    when: "A test or command is refused here and loosening the guard is the wrong answer."
    next: "/gt:gt-doctor, /gt:gt-grant"
    hear: "run this in a disposable VM|run it somewhere disposable|run this in a throwaway container"
model_intent: balanced
---

# Disposable runner

Blocked work finishes in a guest that is thrown away; no guard on this machine is loosened. This is
not gt sandbox mode (`gt_sandbox.py`), which contains Claude's own tools.

## Steps

**Step 1 - Find out why it was blocked.** If the user has not said, run
`python3 <base_dir>/../../scripts/gt_doctor.py testing`. A blocker with a cheap fix (a missing allow
rule) is better fixed than sandboxed; offer the disposable run when the work must not run here at all.

**Step 2 - See what exists.**

```bash
python3 <base_dir>/../../scripts/gt_disposable.py backends
```

No usable backend: say so and stop; the `disposable` row of /gt:gt-doctor names the fix. Do not run the
command on this machine instead.

**Step 3 - Show the plan and ask.**

```bash
python3 <base_dir>/../../scripts/gt_disposable.py plan --tree <the work tree> -- <command...>
```

Read the target, image, number of files copied and the network line back to the user. It refuses a
tree that is or contains `~/.claude` or the vault; never work around that. Add `--network` only when
the user says the work needs it (tart cannot be cut off from the network).

**Step 4 - Run it, once the user says yes.**

```bash
python3 <base_dir>/../../scripts/gt_disposable.py run --tree <the work tree> --timeout 900 -- <command...>
```

Report the exit code and the output. The last line says whether teardown completed; if it says
`TEARDOWN INCOMPLETE`, show it and give the user the guest name to remove by hand.

Nothing is copied back from the guest except output. If the work must change files, say what changed
from the output and apply it here with the normal tools.
