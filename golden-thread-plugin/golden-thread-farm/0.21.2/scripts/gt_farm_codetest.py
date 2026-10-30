#!/usr/bin/env python3
"""gt_farm_codetest.py -- does an external LLM write good code? A shipped, repeatable coding-quality test
(gt 0.22.0, gt-farm 0.21.2). It feeds the same verdict file the rest of the farm already reads.

    gt_farm_codetest.py run    SUBJECT [SUBJECT ...] [options]   # agent sessions on a fixture project
    gt_farm_codetest.py pack   OUTDIR                            # anonymous judge packs + the key
    gt_farm_codetest.py judge  OUTDIR [--runner CMD] [--manual]  # a blind Claude judge scores every pack
    gt_farm_codetest.py score  OUTDIR [--record ENGINE]          # scoreboard; --record stores the verdicts
    gt_farm_codetest.py all    SUBJECT [SUBJECT ...] [--record ENGINE]   # run, pack, judge, score
    gt_farm_codetest.py validate                                 # prove the fixture itself is sound

    SUBJECT: a service-table engine (a `chat-completions` row of the farm service table) or
             `claude:haiku`, `claude:sonnet`, `claude:opus` -- the references.
    options: --fixture ID  --tasks fix,feature  --repeat N  --out DIR  --max-turns N  --timeout S
             --claude CMD  --budget USD  --dry-run  --services FILE  --vault V  --json

WHY. The onboarding calibration (gt_farm_calibrate.py) measures routine work: quotes, field extraction,
classification. It says nothing about CODE, and passing tests is not the same as good code: in the run that
motivated this test (2026-10-19) a local model passed all eight hidden acceptance tests of a feature task and
a blind judge still rejected its change, because it snapshotted a setting at construction and kept a second
running total beside the ledger. So this test has two halves, and a verdict needs both.

THE TEST. A fixture project (templates/codetest/<id>/, original, stdlib-only, ships with gt-farm) and two tasks:
    fix      three planted bugs, failing tests; find the root causes, do not edit tests/.
    feature  a written spec; implement it, update the docs, add your own tests, keep the suite green.
Each SUBJECT gets a fresh copy of the project and the same prompt, and runs as a real Claude Code agent session
(`claude -p`, headless): a farm engine through ANTHROPIC_BASE_URL (llama.cpp's /v1/messages), a Claude model
directly. The session gets no gt hooks and no vault. After the session THIS script grades the result itself:
the whole suite, the hidden acceptance tests (feature), and whether tests/ was left alone (fix). Then the
diffs of all subjects are shuffled into anonymous packs and a zero-context Claude judge scores each one
(acceptable, quality 1-5) without knowing who wrote what. The judge's key is kept outside the packs.

THE VERDICT per code class (`code-fix`, `code-feature`), the same words as the rest of the calibration:
    can-take     in EVERY run: suite green, hidden tests >= 90%, tests/ untouched (fix), the judge says
                 acceptable, mean quality >= 4 -- and at least 2 runs. One run is never enough to take code
                 without a check.
    review-only  functionally right in every run (green, hidden >= 75%) but not can-take: Claude reviews the
                 change before it is used. This is where a model that passes tests with poor design lands.
    not-suitable anything less.
An INVALID run (the engine answered with HTTP errors, no turns happened) is never scored against the model:
it is reported and repeated. A health endpoint answering "ok" is not proof; a preflight request must return
a real reply before a session starts.

WHERE THE RESULT LIVES. `score --record ENGINE` (or `all --record ENGINE`) merges the two code classes into
the engine's model calibration record -- the file the farm reads (Projects/golden-thread/farm/calibrations/
and ~/.claude/golden-thread/farm/engine-calibration.json) -- through gt_farm_calibration.save, so the vault
write goes through the write queue. gt_farm_calibration.automatic_ok(info, "code-fix") and start_rung(...)
then answer from it. The record gets `code_test` (fixture, version, date, references); a record made by an
older CODE_TEST_VERSION is treated as not measured for the code classes only. The ladder's own table
(farm-ladder.tsv) is not changed: it names no code class, so nothing routes code work to an engine until
the owner adds a row.

COST. A farm engine's calls are free. Claude references cost tokens (a few cents to under half a dollar per
task per model, at list price, measured 2026-10-19) and each judge call is Opus-class; the estimate is printed
first and the budget guards of gt_farm_calibrate.py apply. `--dry-run` prints the plan and calls nothing.

Exit: 0 ok | 1 refused (budget) | 2 usage or a missing piece (claude, git, gt core, fixture) |
3 no subject could run (engine down, preflight failed).
"""
import argparse
import collections
import datetime
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
sys.dont_write_bytecode = True
import gt_farm_calibrate as cc                                       # noqa: E402
import gt_farm_calibration as cal                                    # noqa: E402
import gt_farm_probes as probes                                      # noqa: E402

OK, REFUSED, USAGE, NONE = 0, 1, 2, 3
FIXTURE_ROOT = os.path.normpath(os.path.join(HERE, "..", "templates", "codetest"))
DEFAULT_FIXTURE = "ledgerlite"
TASK_CLASS = {"fix": "code-fix", "feature": "code-feature"}
ALLOWED_TOOLS = ["Bash(python3:*)", "Bash(git diff:*)", "Read", "Edit", "Write", "Glob", "Grep"]
CAN_TAKE_RUNS = 2          # fewer runs than this can never be can-take
CAN_TAKE_HIDDEN, REVIEW_HIDDEN = 0.9, 0.75
CAN_TAKE_QUALITY, REVIEW_QUALITY = 4.0, 2.0
DIFF_CAP = 40000
# list-price cents per agent session by reference model (measured 2026-10-19, ledgerlite-class fixtures)
REF_USD = {"haiku": 0.03, "sonnet": 0.2, "opus": 0.5}
JUDGE_USD = 0.5
GIT = ["git", "-c", "user.email=codetest@localhost", "-c", "user.name=codetest"]


def say(*parts):
    print(*parts, file=sys.stderr if cc.TO_ERR else sys.stdout)


# ---- the fixture ------------------------------------------------------------------------------------

def load_fixture(fid):
    root = os.path.join(FIXTURE_ROOT, fid)
    try:
        with open(os.path.join(root, "fixture.json"), encoding="utf-8") as fh:
            fx = json.load(fh)
    except (OSError, ValueError) as exc:
        raise SystemExit("fixture %r not found or unreadable under %s (%s)" % (fid, FIXTURE_ROOT, exc))
    fx["_root"] = root
    return fx


def _json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def apply_edits(tree, edits):
    """Exact-string edits; each `old` must occur exactly once. Raises ValueError otherwise."""
    for e in edits:
        p = os.path.join(tree, e["file"])
        with open(p, encoding="utf-8") as fh:
            s = fh.read()
        if s.count(e["old"]) != 1:
            raise ValueError("%s: 'old' occurs %d times, expected 1" % (e["file"], s.count(e["old"])))
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(s.replace(e["old"], e["new"]))


def prepare(fx, task, dest, edits=None):
    """A fresh git work tree at dest: the base project, the planted bugs for `fix`, one commit. The commit
    is the state the agent starts from, so `git diff` afterwards is exactly what the agent changed."""
    shutil.copytree(os.path.join(fx["_root"], "base"), dest,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if task == "fix":
        apply_edits(dest, edits if edits is not None else fx["tasks"]["fix"]["plants"])
    with open(os.path.join(dest, ".gitignore"), "w") as fh:
        fh.write("__pycache__/\n*.pyc\n")
    for c in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "start"]):
        subprocess.run(GIT[:1] + (GIT[1:] if c[0] == "commit" else []) + c, cwd=dest, check=True,
                       capture_output=True)


def run_tests(cmd, cwd, timeout=300):
    """-> (ok, ran, failed, tail). `cmd` is a shell string (the fixture's test_command or a module run)."""
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, 0, 0, "timed out after %ds" % timeout
    out = (p.stdout or "") + (p.stderr or "")
    m = re.search(r"Ran (\d+) tests?", out)
    ran = int(m.group(1)) if m else 0
    f = re.search(r"FAILED \((?:failures=(\d+))?(?:, )?(?:errors=(\d+))?", out)
    failed = (int(f.group(1) or 0) + int(f.group(2) or 0)) if f else 0
    return p.returncode == 0 and ran > 0, ran, failed, out[-600:]


def git(cwd, *args):
    return subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True).stdout


def validate_fixture(fx):
    """Prove the fixture is sound: base green, plants break >= 4 tests, reference fix restores green,
    hidden tests fail on the clean base (>= 6) and all pass with the reference feature. -> (ok, [lines])."""
    lines, ok = [], True
    cmd = fx["test_command"]

    def check(label, cond, detail):
        nonlocal ok
        ok = ok and cond
        lines.append("%s %s: %s" % ("ok  " if cond else "FAIL", label, detail))

    with tempfile.TemporaryDirectory(prefix="codetest_val_") as tmp:
        base = os.path.join(tmp, "base")
        prepare(fx, "feature", base)
        g, ran, failed, _ = run_tests(cmd, base)
        check("base suite green", g, "%d tests" % ran)
        planted = os.path.join(tmp, "planted")
        prepare(fx, "fix", planted)
        g, ran, failed, _ = run_tests(cmd, planted)
        check("planted copy fails >= 4 tests", (not g) and failed >= 4, "%d of %d fail" % (failed, ran))
        apply_edits(planted, _json(os.path.join(fx["_root"], "reference", "fix.json")))
        g, ran, failed, _ = run_tests(cmd, planted)
        check("planted + reference fix green", g, "%d tests" % ran)
        hid = fx["tasks"]["feature"]
        shutil.copy(os.path.join(fx["_root"], hid["hidden_test"]), os.path.join(base, "tests", os.path.basename(hid["hidden_test"])))
        mod = "python3 -m unittest %s" % hid["hidden_module"]
        g, ran, failed, _ = run_tests(mod, base)
        check("hidden tests fail on the clean base (>= 6)", failed >= 6, "%d of %d fail" % (failed, ran))
        os.remove(os.path.join(base, "tests", os.path.basename(hid["hidden_test"])))
        apply_edits(base, _json(os.path.join(fx["_root"], "reference", "feature.json")))
        g, ran, failed, _ = run_tests(cmd, base)
        check("base + reference feature: suite green", g, "%d tests" % ran)
        shutil.copy(os.path.join(fx["_root"], hid["hidden_test"]), os.path.join(base, "tests", os.path.basename(hid["hidden_test"])))
        g, ran, failed, _ = run_tests(mod, base)
        check("base + reference feature: hidden tests all pass", g and failed == 0, "%d tests" % ran)
        poor_ref = os.path.join(fx["_root"], "reference", "poor_feature.json")
        if os.path.isfile(poor_ref):
            poor = os.path.join(tmp, "poor")
            prepare(fx, "feature", poor)
            apply_edits(poor, _json(poor_ref))
            g, ran, failed, _ = run_tests(cmd, poor)
            shutil.copy(os.path.join(fx["_root"], hid["hidden_test"]), os.path.join(poor, "tests", os.path.basename(hid["hidden_test"])))
            hg, hran, hfailed, _ = run_tests(mod, poor)
            check("poor-but-passing reference passes the suite and every hidden test (a test-only grader cannot "
                  "tell it from the good one)", g and hg and hfailed == 0, "%d tests + %d hidden" % (ran, hran))
    return ok, lines


def judge_canary(fx, a):
    """Prove the JUDGE: the good reference and the poor-but-test-passing reference (reference/poor_feature.json)
    are shuffled into one pack and judged blind; the poor one must score lower than the good one. A test
    whose judge cannot tell them apart cannot be trusted to find the design flaw it exists to find.
    -> (ok, [lines])."""
    poor_path = os.path.join(fx["_root"], "reference", "poor_feature.json")
    if not os.path.isfile(poor_path):
        return False, ["FAIL judge canary: the fixture has no reference/poor_feature.json"]
    core = cal.core_module("gt_model_calibrate")
    runner = (a.runner or os.environ.get("GT_CALIBRATION_RUNNER") or core.saved_runner()) if core else None
    if core is None or not runner:
        return False, ["FAIL judge canary: no runner (gt core not found, or --runner / $GT_CALIBRATION_RUNNER / "
                       "model_calibration_runner is not set)"]
    diffs = {}
    with tempfile.TemporaryDirectory(prefix="codetest_canary_") as tmp:
        for name, ref in (("good", "feature.json"), ("poor", "poor_feature.json")):
            ws = os.path.join(tmp, name)
            prepare(fx, "feature", ws)
            apply_edits(ws, _json(os.path.join(fx["_root"], "reference", ref)))
            subprocess.run(["git", "add", "-A"], cwd=ws, capture_output=True)
            diffs[name] = git(ws, "diff", "--cached", "-U12")
    order = ["good", "poor"]
    random.Random(fx["id"] + "|canary").shuffle(order)
    parts = [JUDGE_HEAD, "## The task the coding agents were given", "", fx["tasks"]["feature"]["prompt"].rstrip(), "",
             CRITERIA["feature"], "", "(Neither output adds tests or docs; judge the code change only.)", ""]
    for lab, name in zip("AB", order):
        parts += ["## Output %s" % lab, "", "```diff", diffs[name][:DIFF_CAP], "```", ""]
    parts.append(JUDGE_TAIL)
    res = cc.run_runner(core, runner, a.judge_model, "high", "\n".join(parts))
    if not isinstance(res, dict) or not all(isinstance(res.get(l), dict) and "quality" in res[l] for l in "AB"):
        return False, ["FAIL judge canary: the runner returned no usable verdicts"]
    q = {name: float(res[lab]["quality"]) for lab, name in zip("AB", order)}
    acc = {name: bool(res[lab].get("acceptable")) for lab, name in zip("AB", order)}
    good = q["good"] > q["poor"]
    return good, ["%s judge canary: good reference quality %.0f (acceptable %s), poor-but-passing reference quality "
                  "%.0f (acceptable %s)%s" % ("ok  " if good else "FAIL", q["good"], acc["good"], q["poor"], acc["poor"],
                                              "" if good else " -- the judge cannot tell them apart")]


# ---- subjects ---------------------------------------------------------------------------------------

def resolve_subjects(names, a, vault):
    """-> [subject dict] or raises SystemExit with the reason. Subject: {name, kind, model, base, ref}."""
    out = []
    for n in names:
        if n.startswith("claude:"):
            m = n.split(":", 1)[1]
            if m not in REF_USD:
                raise SystemExit("unknown reference %r: use claude:haiku, claude:sonnet or claude:opus" % n)
            out.append({"name": n, "kind": "claude", "model": m})
            continue
        found = cal.table_engines(a.services, vault, service=n, local_only=False)
        if not found:
            raise SystemExit("%r is neither claude:<model> nor a chat-completions row of the farm service table" % n)
        e = found[0]
        if e.get("key_file"):
            raise SystemExit("%s has a key_file: the code test does not send credentials to an engine. Run it on a "
                             "local engine without one." % n)
        out.append({"name": n, "kind": "engine", "model": e.get("model") or "", "engine": e,
                    "base": re.sub(r"/v1/?$", "", e["base_url"])})
    return out


def preflight(subject, timeout=180):
    """A real reply from /v1/messages, not just a health 'ok'. -> (ok, why)."""
    if subject["kind"] == "claude":
        return True, ""
    # 512, not 16: a reasoning model spends its first tokens thinking and would look empty (gpt-oss-120b does)
    body = json.dumps({"model": subject["model"], "max_tokens": 512,
                       "messages": [{"role": "user", "content": "Reply with the single word OK."}]}).encode()
    req = urllib.request.Request(subject["base"] + "/v1/messages", data=body, method="POST",
                                 headers={"content-type": "application/json", "x-api-key": "local",
                                          "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, "no reply from %s/v1/messages (%s)" % (subject["base"], str(exc)[:120])
    text = "".join(b.get("text", "") for b in d.get("content") or [] if isinstance(b, dict))
    return (True, "") if text.strip() else (False, "the engine answered but the reply was empty")


# ---- one agent session ------------------------------------------------------------------------------

def summarize_stream(path):
    tools, res, retries, assistant, maxin = collections.Counter(), None, 0, 0, 0
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return {"turns": 0, "tools": {}, "api_retries": 0, "assistant_msgs": 0, "max_ctx": 0, "cost_usd": None, "is_error": True}
    with fh:
        for line in fh:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "system" and e.get("subtype") == "api_retry":
                retries += 1
            elif e.get("type") == "assistant":
                assistant += 1
                u = (e.get("message") or {}).get("usage") or {}
                maxin = max(maxin, sum(int(u.get(k) or 0) for k in
                                       ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")))
                for c in (e.get("message") or {}).get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        tools[c.get("name")] += 1
            elif e.get("type") == "result":
                res = e
    return {"turns": (res or {}).get("num_turns", assistant), "tools": dict(tools), "api_retries": retries,
            "assistant_msgs": assistant, "max_ctx": maxin, "cost_usd": (res or {}).get("total_cost_usd"),
            "is_error": bool((res or {}).get("is_error", res is None))}


def agent_command(subject, prompt, a):
    cmd = [a.claude, "-p", prompt, "--output-format", "stream-json", "--verbose", "--permission-mode", "acceptEdits",
           "--allowedTools"] + ALLOWED_TOOLS + ["--max-turns", str(a.max_turns)]
    env = dict(os.environ)
    cfg = None
    if subject["kind"] == "claude":
        cmd += ["--model", subject["model"], "--setting-sources", "project"]
    else:
        cfg = tempfile.mkdtemp(prefix="codetest_cfg_")
        env.update(CLAUDE_CONFIG_DIR=cfg, ANTHROPIC_BASE_URL=subject["base"], ANTHROPIC_AUTH_TOKEN="local",
                   ANTHROPIC_MODEL=subject["model"], ANTHROPIC_SMALL_FAST_MODEL=subject["model"],
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    return cmd, env, cfg


def run_one(fx, subject, task, rep, a, outdir):
    """One session + grading. -> the result dict, also written to outdir/<subject>-<task>-<rep>/result.json."""
    tag = "%s-%s-%d" % (re.sub(r"[^A-Za-z0-9_.-]+", "_", subject["name"]), task, rep)
    d = os.path.join(outdir, "runs", tag)
    os.makedirs(d, exist_ok=True)
    ws = os.path.join(d, "ws")
    if os.path.isdir(ws):
        shutil.rmtree(ws)
    prepare(fx, task, ws)
    t = fx["tasks"][task]
    cmd, env, cfg = agent_command(subject, t["prompt"], a)
    stream = os.path.join(d, "agent.jsonl")
    t0 = time.time()
    timed_out = False
    try:
        with open(stream, "w", encoding="utf-8") as so, open(os.path.join(d, "agent.err"), "w") as se:
            subprocess.run(cmd, cwd=ws, env=env, stdout=so, stderr=se, timeout=a.timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    except OSError as exc:
        return {"subject": subject["name"], "task": task, "rep": rep, "valid": False,
                "invalid_reason": "could not start %s: %s" % (a.claude, exc)}
    finally:
        if cfg:
            shutil.rmtree(cfg, ignore_errors=True)
    secs = round(time.time() - t0, 1)
    s = summarize_stream(stream)
    r = {"subject": subject["name"], "kind": subject["kind"], "task": task, "rep": rep, "secs": secs,
         "turns": s["turns"], "tools": s["tools"], "max_ctx": s["max_ctx"], "api_retries": s["api_retries"],
         "cost_usd": s["cost_usd"] if subject["kind"] == "claude" else 0.0, "timed_out": timed_out}
    # an INVALID run is the harness's or the engine's failure, never a score against the model
    if s["assistant_msgs"] == 0:
        r.update(valid=False, invalid_reason="the session produced no assistant turn (%d API retries%s)" % (
            s["api_retries"], ", timed out" if timed_out else ""))
        _save(d, r)
        return r
    # grade: the whole suite, then the hidden tests; diff first so graders' files stay out of it
    subprocess.run(["git", "add", "-A"], cwd=ws, capture_output=True)
    diff = git(ws, "diff", "--cached", "-U12")
    names = git(ws, "diff", "--cached", "--name-status").splitlines()
    touched = [n for n in names if re.search(r"\ttests/", n)]
    r["tests_touched"] = bool(touched) if task == "fix" else None
    ok, ran, failed, tail = run_tests(fx["test_command"], ws)
    r.update(suite_ok=ok, suite_ran=ran, suite_failed=failed, diff_chars=len(diff))
    if task == "feature":
        h = t
        dst = os.path.join(ws, "tests", os.path.basename(h["hidden_test"]))
        shutil.copy(os.path.join(fx["_root"], h["hidden_test"]), dst)
        hok, hran, hfailed, _ = run_tests("python3 -m unittest %s" % h["hidden_module"], ws)
        os.remove(dst)
        r.update(hidden_ran=hran, hidden_failed=hfailed, hidden_pass=round((hran - hfailed) / hran, 3) if hran else 0.0)
    with open(os.path.join(d, "diff.patch"), "w", encoding="utf-8") as fh:
        fh.write(diff)
    r["valid"] = True
    shutil.rmtree(ws, ignore_errors=True)
    _save(d, r)
    return r


def _save(d, r):
    with open(os.path.join(d, "result.json"), "w", encoding="utf-8") as fh:
        json.dump(r, fh, indent=1, sort_keys=True)


def load_results(outdir):
    out = []
    runs = os.path.join(outdir, "runs")
    for n in sorted(os.listdir(runs)) if os.path.isdir(runs) else []:
        try:
            with open(os.path.join(runs, n, "result.json"), encoding="utf-8") as fh:
                out.append(json.load(fh))
        except (OSError, ValueError):
            continue
    return out


# ---- the blind packs and the judge ----------------------------------------------------------------

CRITERIA = {
    "fix": ("Judge each output on: (1) the ROOT CAUSE of each planted bug fixed, not a workaround or a hack that "
            "fits the tests; (2) minimality and no collateral changes; (3) tests/ untouched as instructed; (4) "
            "readability of the fix and whether any added comment earns its place."),
    "feature": ("Judge each output on: (1) spec adherence, every clause of the task; (2) correctness and edge cases "
                "(where the new check sits among the existing ones, rounding, how the figure is derived -- from the "
                "ledger, or from a second running total beside it); (3) design: configuration read live, one source "
                "of truth, no state snapshotted at construction; (4) the agent's OWN tests: do they cover the spec's "
                "edge cases through the public behaviour and would they catch a regression, or do they poke "
                "internals; (5) docs updated; (6) minimal and idiomatic for the surrounding code."),
}
JUDGE_HEAD = ("You are a zero-context adjudicator of coding-agent output. Below are a task and several anonymous "
              "outputs (A, B, C ...), each the diff a coding agent made. You do not know which model wrote which and "
              "must not guess. You cannot run anything: judge from the diffs, which carry context lines. Use the same "
              "bar for all. Passing tests is NOT enough: a change that passes every test but is worse code than a "
              "clean alternative is not acceptable.\n")
JUDGE_TAIL = ('\nFor every output give acceptable (true/false: would a careful owner merge it as it stands), quality '
              '1-5 (5 = as good as a careful expert, 3 = usable with light editing, 1 = useless) and up to 3 short, '
              'concrete notes. Reply with ONE JSON object and nothing else: {"A": {"acceptable": true, "quality": 4, '
              '"notes": ["..."]}, "B": {...}}\n')


def build_packs(fx, outdir):
    """-> {pack-id: {label: run-tag}} written to outdir/packs/<id>.md and outdir/key.json (the key is never in a pack)."""
    results = [r for r in load_results(outdir) if r.get("valid")]
    groups = collections.defaultdict(list)
    for r in results:
        groups[(r["task"], r["rep"])].append(r)
    packs, key = {}, {}
    pdir = os.path.join(outdir, "packs")
    os.makedirs(pdir, exist_ok=True)
    for (task, rep), rs in sorted(groups.items()):
        if len(rs) < 2:
            continue                                    # a lone output has nothing to be judged against
        pid = "%s-%d" % (task, rep)
        rnd = random.Random("%s|%s" % (os.path.basename(os.path.abspath(outdir)), pid))
        order = list(rs)
        rnd.shuffle(order)
        labels = {chr(ord("A") + i): "%s-%s-%d" % (re.sub(r"[^A-Za-z0-9_.-]+", "_", r["subject"]), task, rep)
                  for i, r in enumerate(order)}
        parts = [JUDGE_HEAD, "## The task the coding agents were given", "", fx["tasks"][task]["prompt"].rstrip(), "",
                 CRITERIA[task], ""]
        for lab, tag in labels.items():
            try:
                with open(os.path.join(outdir, "runs", tag, "diff.patch"), encoding="utf-8") as fh:
                    diff = fh.read()
            except OSError:
                diff = ""
            parts += ["## Output %s" % lab, "", "```diff", (diff[:DIFF_CAP] or "(no changes)"), "```", ""]
        parts.append(JUDGE_TAIL)
        with open(os.path.join(pdir, pid + ".md"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(parts))
        key[pid] = labels
        packs[pid] = labels
    with open(os.path.join(outdir, "key.json"), "w", encoding="utf-8") as fh:
        json.dump(key, fh, indent=1)
    return packs


def judge_packs(outdir, a, core, runner, model):
    done = 0
    pdir = os.path.join(outdir, "packs")
    for n in sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []:
        if not n.endswith(".md"):
            continue
        pid = n[:-3]
        vpath = os.path.join(pdir, pid + ".verdicts.json")
        if os.path.exists(vpath):
            continue
        with open(os.path.join(pdir, n), encoding="utf-8") as fh:
            prompt = fh.read()
        res = cc.run_runner(core, runner, model, "high", prompt)
        if not isinstance(res, dict) or not any(isinstance(v, dict) and "quality" in v for v in res.values()):
            say("  judge: %s: no usable verdicts from the runner; left for --manual" % pid)
            continue
        with open(vpath, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=1)
        done += 1
    return done


# ---- scoring ----------------------------------------------------------------------------------------

def functional(r, task):
    if not r.get("valid") or not r.get("suite_ok"):
        return False
    if task == "fix":
        return not r.get("tests_touched")
    return (r.get("hidden_pass") or 0.0) >= REVIEW_HIDDEN


def class_verdict(runs, judged):
    """runs: valid result dicts of one subject for one task; judged: [{acceptable, quality}] parallel to runs
    (None where not judged). -> (verdict, why)."""
    if not runs:
        return "unscored", "no valid run"
    task = runs[0]["task"]
    if not all(functional(r, task) for r in runs):
        return "not-suitable", "the result was not functionally right in every run"
    scored = [j for j in judged if j]
    if not scored:
        return "review-only", "functionally right, but no judge verdict on record yet"
    mean_q = sum(j["quality"] for j in scored) / len(scored)
    hidden_ok = task == "fix" or all((r.get("hidden_pass") or 0) >= CAN_TAKE_HIDDEN for r in runs)
    if (len(runs) >= CAN_TAKE_RUNS and hidden_ok and all(j["acceptable"] for j in scored) and mean_q >= CAN_TAKE_QUALITY):
        return "can-take", "green, acceptable and mean quality %.1f in %d runs" % (mean_q, len(runs))
    if len(runs) < CAN_TAKE_RUNS and hidden_ok and all(j["acceptable"] for j in scored) and mean_q >= CAN_TAKE_QUALITY:
        return "review-only", "good, but one run is not enough to take code without a check (--repeat 2)"
    if mean_q >= REVIEW_QUALITY:
        return "review-only", "functionally right; the judge's mean quality was %.1f%s" % (
            mean_q, "" if all(j["acceptable"] for j in scored) else " and not every run was acceptable")
    return "not-suitable", "the judge's mean quality was %.1f" % mean_q


def decode_verdicts(outdir):
    """-> {run-tag: {acceptable, quality, notes}} from every packs/*.verdicts.json and the key."""
    try:
        with open(os.path.join(outdir, "key.json"), encoding="utf-8") as fh:
            key = json.load(fh)
    except (OSError, ValueError):
        return {}
    out = {}
    for pid, labels in key.items():
        try:
            with open(os.path.join(outdir, "packs", pid + ".verdicts.json"), encoding="utf-8") as fh:
                v = json.load(fh)
        except (OSError, ValueError):
            continue
        for lab, tag in labels.items():
            j = v.get(lab)
            if isinstance(j, dict) and isinstance(j.get("quality"), (int, float)):
                out[tag] = {"acceptable": bool(j.get("acceptable")), "quality": float(j["quality"]),
                            "notes": [str(x)[:200] for x in (j.get("notes") or [])][:3]}
    return out


def tag_of(r):
    return "%s-%s-%d" % (re.sub(r"[^A-Za-z0-9_.-]+", "_", r["subject"]), r["task"], r["rep"])


def score(outdir):
    """-> {subject: {class: {verdict, why, kind}}} plus the table rows."""
    results = load_results(outdir)
    verdicts = decode_verdicts(outdir)
    subs = collections.OrderedDict()
    for r in results:
        subs.setdefault(r["subject"], []).append(r)
    board, rows = {}, []
    for sname, rs in subs.items():
        board[sname] = {}
        for task, cls in TASK_CLASS.items():
            mine = [r for r in rs if r["task"] == task]
            if not mine:
                continue
            valid = [r for r in mine if r.get("valid")]
            judged = [verdicts.get(tag_of(r)) for r in valid]
            v, why = class_verdict(valid, judged)
            scored = [j for j in judged if j]
            k = {"verdict": v, "why": why, "class": cls, "runs": len(valid), "invalid_runs": len(mine) - len(valid),
                 "scored": len(valid), "schema_pass": round(sum(functional(r, task) for r in valid) / len(valid), 3) if valid else None,
                 "check_pass": round(sum((r.get("hidden_pass") or 0) for r in valid) / len(valid), 3) if valid and task == "feature" else None,
                 "all_acceptable": bool(scored) and all(j["acceptable"] for j in scored),
                 "agreement": round(sum(j["quality"] for j in scored) / len(scored) / 5.0, 3) if scored else None,
                 "mean_quality": round(sum(j["quality"] for j in scored) / len(scored), 2) if scored else None,
                 "latency_s": round(sum(r["secs"] for r in valid) / len(valid), 1) if valid else None,
                 "tok_per_s": None, "turns": round(sum(r["turns"] for r in valid) / len(valid), 1) if valid else None}
            board[sname][cls] = k
            rows.append((sname, task, k, valid))
    return board, rows


def table_lines(rows):
    head = "%-20s %-8s %-13s %5s %6s %5s %5s %7s  %s" % ("subject", "task", "verdict", "suite", "hidden", "qual", "turns", "secs", "why")
    out = [head, "-" * len(head)]
    for sname, task, k, valid in rows:
        suite = "-" if not valid else "%d/%d" % (sum(1 for r in valid if r.get("suite_ok")), len(valid))
        hid = "-" if k["check_pass"] is None else "%d%%" % round(k["check_pass"] * 100)
        out.append("%-20s %-8s %-13s %5s %6s %5s %5s %7s  %s" % (
            sname[:20], task, k["verdict"], suite, hid, "-" if k["mean_quality"] is None else "%.1f" % k["mean_quality"],
            "-" if k["turns"] is None else "%.0f" % k["turns"], "-" if k["latency_s"] is None else "%.0f" % k["latency_s"],
            k["why"] + (" (%d invalid run(s) excluded)" % k["invalid_runs"] if k["invalid_runs"] else "")))
    return out


def record_into_calibration(engine_name, board, outdir, a, vault, fx, references):
    """Merge the code classes of one engine into its calibration record. -> (stored, message)."""
    found = cal.table_engines(a.services, vault, service=engine_name, local_only=False)
    if not found:
        return None, "%r is not a service-table engine, so there is no record to merge into" % engine_name
    engine = found[0]
    info = cal.probe_engine(engine)
    if not info["up"]:
        return None, "%s did not answer, so its model fingerprint is unknown and the record was not written" % engine_name
    mine = board.get(engine_name) or {}
    if not mine:
        return None, "no code-test result for %s in %s" % (engine_name, outdir)
    rec, _where = cal.find(info, vault)
    if rec is None:
        adj = cal.adjudicator_alias(vault)
        rec = cal.new_record(info, engine, {}, None, {}, True, adj)
        rec["probes_per_class"] = 0
    rec = merge_code_kinds(rec, mine, fx, references, cal.rung_of(engine))
    stored = cal.save(rec, vault)
    return stored, rec["summary"]


def merge_code_kinds(rec, mine, fx, references, rung):
    """-> a copy of the calibration record with the two code classes set from `mine` ({class: kind}). The
    classes measured by the onboarding calibration are left exactly as they were."""
    rec = dict(rec)
    rec.pop("_path", None)
    kinds = dict(rec.get("kinds") or {})
    for cls, k in mine.items():
        kinds[cls] = {x: k[x] for x in ("verdict", "class", "runs", "scored", "schema_pass", "check_pass", "all_acceptable",
                                         "agreement", "mean_quality", "latency_s", "tok_per_s", "turns", "why")}
    rec["kinds"] = kinds
    rec["code_test"] = {"fixture": fx["id"], "version": probes.CODE_TEST_VERSION, "fixture_version": fx.get("version"),
                        "date": datetime.date.today().isoformat(), "references": list(references)}
    rec["recommended_rungs"] = cal.recommend(kinds, rung)
    rec["summary"] = cal.summary_line(kinds, rec.get("injection"))
    return rec


# ---- commands ---------------------------------------------------------------------------------------

def cmd_validate(a):
    fx = load_fixture(a.fixture)
    ok, lines = validate_fixture(fx)
    if a.judge:
        jok, jl = judge_canary(fx, a)
        ok, lines = ok and jok, lines + jl
    elif ok:
        lines.append("note judge canary not run (costs one judge call): add --judge to prove the judge tells good code from test-passing poor code")
    say("Fixture %s (version %s):" % (fx["id"], fx.get("version")))
    for l in lines:
        say("  " + l)
    say("The fixture is %s." % ("sound" if ok else "NOT sound: do not use it for a verdict"))
    return OK if ok else USAGE


def plan_cost(subjects, tasks, repeat, with_judge):
    usd = sum(REF_USD[s["model"]] * len(tasks) * repeat for s in subjects if s["kind"] == "claude")
    if with_judge:
        usd += JUDGE_USD * len(tasks) * repeat
    return round(usd, 2)


def cmd_run(a, vault):
    if shutil.which(a.claude) is None:
        say("`%s` was not found: the test drives Claude Code headless. Install it or pass --claude PATH." % a.claude)
        return USAGE
    if shutil.which("git") is None:
        say("git was not found: the test needs it to prepare and diff the project.")
        return USAGE
    fx = load_fixture(a.fixture)
    tasks = [t.strip() for t in a.tasks.split(",") if t.strip()]
    bad = [t for t in tasks if t not in TASK_CLASS or t not in fx["tasks"]]
    if bad:
        say("unknown task %s; the fixture has: %s" % (", ".join(bad), ", ".join(fx["tasks"])))
        return USAGE
    subjects = resolve_subjects(a.subjects, a, vault)
    usd = plan_cost(subjects, tasks, a.repeat, with_judge=False)
    say("Plan: %d subject(s) x %s x %d run(s) on fixture %s. Estimated Claude cost of the sessions: %.2f USD "
        "(engines are free; the judge is extra)." % (len(subjects), "+".join(tasks), a.repeat, fx["id"], usd))
    if a.dry_run:
        say("Dry run: nothing was started.")
        return OK
    core = cal.core_module("gt_model_calibrate")
    if core is not None and any(s["kind"] == "claude" for s in subjects):
        why = cc.guard(core, {"estimate_usd": usd, "budget_usd": cc.budget_usd(core, a)}, a)
        if why:
            say(why)
            return REFUSED
    os.makedirs(os.path.join(a.out, "runs"), exist_ok=True)
    ran = 0
    for s in subjects:
        ok, why = preflight(s)
        if not ok:
            say("%s: preflight failed (%s); not started. A health endpoint answering 'ok' is not enough: restart the "
                "engine and check its log for GPU or memory errors." % (s["name"], why))
            continue
        for task in tasks:
            for rep in range(1, a.repeat + 1):
                say("%s %s run %d ..." % (s["name"], task, rep))
                r = run_one(fx, s, task, rep, a, a.out)
                ran += 1
                if r.get("valid"):
                    say("  -> suite %s, %s turns, %.0f s%s" % (
                        "green" if r.get("suite_ok") else "RED (%s failing)" % r.get("suite_failed"), r.get("turns"), r.get("secs", 0),
                        (", hidden %d%%" % round(r["hidden_pass"] * 100)) if r.get("hidden_pass") is not None else ""))
                else:
                    say("  -> INVALID, not scored: %s" % r.get("invalid_reason"))
    if cc.TO_ERR:
        print(json.dumps({"out": a.out, "results": load_results(a.out)}))
    return OK if ran else NONE


def cmd_pack(a):
    fx = load_fixture(a.fixture)
    packs = build_packs(fx, a.outdir)
    if not packs:
        say("no pack built: a pack needs at least two valid outputs for the same task and run number.")
        return NONE
    for pid in packs:
        say("pack %s: %d anonymous outputs -> %s" % (pid, len(packs[pid]), os.path.join(a.outdir, "packs", pid + ".md")))
    say("The key is %s. Do not show it to a judge." % os.path.join(a.outdir, "key.json"))
    return OK


def cmd_judge(a):
    pdir = os.path.join(a.outdir, "packs")
    if not os.path.isdir(pdir):
        say("no packs yet: run `pack` first.")
        return USAGE
    if a.manual:
        for n in sorted(os.listdir(pdir)):
            if n.endswith(".md"):
                say("Give %s to a zero-context judge; it must write %s" % (
                    os.path.join(pdir, n), os.path.join(pdir, n[:-3] + ".verdicts.json")))
        return OK
    core = cal.core_module("gt_model_calibrate")
    if core is None:
        say("gt core (gt_model_calibrate.py) was not found: the judge runs through its runner. Use --manual, or install gt.")
        return USAGE
    runner = a.runner or os.environ.get("GT_CALIBRATION_RUNNER") or core.saved_runner()
    if not runner:
        say("no runner: pass --runner CMD, set $GT_CALIBRATION_RUNNER or the model_calibration_runner setting, or use --manual.")
        return USAGE
    n = judge_packs(a.outdir, a, core, runner, a.judge_model)
    say("judged %d pack(s) with %s." % (n, a.judge_model))
    return OK


def cmd_score(a, vault):
    fx = load_fixture(a.fixture)
    board, rows = score(a.outdir)
    if not rows:
        say("nothing to score in %s" % a.outdir)
        return NONE
    for l in table_lines(rows):
        say(l)
    if not decode_verdicts(a.outdir):
        say("\nNo judge verdicts yet: verdicts here are provisional (review-only at best). Run `pack` and `judge`.")
    if a.record:
        refs = sorted({s for s in board if s.startswith("claude:")})
        stored, msg = record_into_calibration(a.record, board, a.outdir, a, vault, fx, refs)
        if stored is None:
            say("\nNot recorded: " + msg)
            return NONE
        v = stored["vault"]
        say("\nRecorded in the calibration record of %s: %s\n  stored: %s" % (
            a.record, msg, ("vault %s (%s)" % (v["path"], v["decision"])) if v else "local cache only (no vault)"))
    if a.json:
        print(json.dumps({"board": board}))
    return OK


def cmd_all(a, vault):
    rc = cmd_run(a, vault)
    if rc != OK or a.dry_run:
        return rc
    a.outdir = a.out
    rc = cmd_pack(a)
    if rc == OK:
        a.manual = False
        rc = cmd_judge(a)
    return cmd_score(a, vault) if rc in (OK, NONE, USAGE) else rc


def build_parser():
    ap = argparse.ArgumentParser(prog="gt_farm_codetest.py", description="Coding-quality test for farm engines")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--fixture", default=DEFAULT_FIXTURE)
        p.add_argument("--services")
        p.add_argument("--vault")
        p.add_argument("--json", action="store_true")

    def runopts(p):
        p.add_argument("subjects", nargs="+", metavar="SUBJECT")
        p.add_argument("--tasks", default="fix,feature")
        p.add_argument("--repeat", type=int, default=1)
        p.add_argument("--out", default=None, help="results folder (default: ~/.claude/golden-thread/farm/codetest/<date>-<time>)")
        p.add_argument("--max-turns", type=int, default=60)
        p.add_argument("--timeout", type=float, default=3600.0, help="seconds per agent session")
        p.add_argument("--claude", default="claude")
        p.add_argument("--budget", type=float)
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--runner")
        p.add_argument("--judge-model", default="opus", choices=("haiku", "sonnet", "opus"))
        p.add_argument("--record", metavar="ENGINE")
        common(p)

    runopts(sub.add_parser("run"))
    runopts(sub.add_parser("all"))
    p = sub.add_parser("pack"); p.add_argument("outdir"); common(p)
    p = sub.add_parser("judge"); p.add_argument("outdir"); p.add_argument("--runner"); p.add_argument("--manual", action="store_true")
    p.add_argument("--judge-model", default="opus", choices=("haiku", "sonnet", "opus")); common(p)
    p = sub.add_parser("score"); p.add_argument("outdir"); p.add_argument("--record", metavar="ENGINE"); common(p)
    p = sub.add_parser("validate"); p.add_argument("--judge", action="store_true", help="also prove the judge (one call)")
    p.add_argument("--runner"); p.add_argument("--judge-model", default="opus", choices=("haiku", "sonnet", "opus")); common(p)
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    if getattr(a, "json", False):
        cc.TO_ERR.append(1)
    vault = a.vault or cal.fs.vault_path()
    if hasattr(a, "out") and a.out is None:
        a.out = os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "farm", "codetest",
                             datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    try:
        if a.cmd == "validate":
            return cmd_validate(a)
        if a.cmd == "run":
            return cmd_run(a, vault)
        if a.cmd == "pack":
            return cmd_pack(a)
        if a.cmd == "judge":
            return cmd_judge(a)
        if a.cmd == "score":
            return cmd_score(a, vault)
        if a.cmd == "all":
            a.manual = False
            return cmd_all(a, vault)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            say(exc.code)
            return USAGE
        raise
    return USAGE


if __name__ == "__main__":
    sys.exit(main())
