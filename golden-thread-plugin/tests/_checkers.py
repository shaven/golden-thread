"""Fixture modules with toy CHECKERS, shared by test_gt_check.py and test_gt_apply.py.

One generic checker script, configured by a JSON blob written at its top, so every test
states the behaviour it needs in one line instead of hand-writing a script:

    behaviour  marker (default) | pass | garbage | commitmsg | mutate
    mark       a string whose presence is a finding (rule `rule`, default "marker")
    replace    [old, new]: propose a diff replacing old with new in the finding's file
    propose_file  propose against this file instead of the one with the finding
    raw_diff   propose exactly this diff text
    sleep      seconds to sleep first;  log  a dir to drop <name>.start / <name>.end into
"""
import hashlib
import json
from pathlib import Path

CHECKER = r'''
import difflib, json, os, re, sys, time
CFG = json.loads(%r)
req = json.load(sys.stdin)
root = req["root"]
b = CFG.get("behaviour", "marker")
def stamp(kind):
    if CFG.get("log"):
        with open(os.path.join(CFG["log"], "%%s.%%s" %% (CFG["name"], kind)), "w") as fh:
            fh.write(repr(time.time()))
stamp("start")
if CFG.get("sleep"):
    time.sleep(CFG["sleep"])
stamp("end")
if b == "garbage":
    print("this is not the result shape")
    sys.exit(0)
if b == "commitmsg":
    msg = open(req["message_file"], encoding="utf-8").read()
    ok = re.match(r"^[A-Z]+-[0-9]+ ", msg) is not None
    print(json.dumps({"schema": 1, "verdict": "pass" if ok else "fail",
                      "findings": [] if ok else [{"file": "COMMIT_EDITMSG", "line": 1,
                                                  "rule": "ticket-prefix",
                                                  "message": "no ticket prefix"}]}))
    sys.exit(0)
findings, proposals = [], []
rule = CFG.get("rule", "marker")
for rel in req["files"]:
    p = os.path.join(root, rel)
    text = open(p, encoding="utf-8").read()
    if b == "mutate":
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("tampered\n")
    mark = CFG.get("mark")
    if b == "marker" and mark and mark in text:
        findings.append({"file": rel, "line": text[:text.index(mark)].count("\n") + 1,
                         "rule": rule, "message": "found the marker"})
        target = CFG.get("propose_file") or rel
        if CFG.get("raw_diff") is not None:
            proposals.append({"file": target, "diff": CFG["raw_diff"], "finding": rule,
                              "reason": "raw"})
        elif CFG.get("replace"):
            src = open(os.path.join(root, target), encoding="utf-8").read()
            new = src.replace(CFG["replace"][0], CFG["replace"][1])
            diff = "".join(difflib.unified_diff(src.splitlines(True), new.splitlines(True),
                                                "a/" + target, "b/" + target))
            proposals.append({"file": target, "diff": diff, "finding": rule,
                              "reason": "remove the marker"})
print(json.dumps({"schema": 1, "verdict": "fail" if findings else "pass",
                  "findings": findings, "proposals": proposals}))
'''


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def make_module(root, checkers, name="fx", manifest=True, plugin=None, version="1.0.0"):
    """A module plugin under `root` (a plugin root). `checkers` is a list of
    (decl_dict, cfg_dict): decl is the module.json checker entry minus `script`, cfg the
    generic checker's behaviour. Returns the version dir."""
    plugin = plugin or "gt-%s" % name
    vd = Path(root) / ("golden-thread-%s" % name) / version
    (vd / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (vd / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": plugin, "version": version}))
    (vd / "scripts").mkdir(exist_ok=True)
    decls, scripts = [], []
    for decl, cfg in checkers:
        script = "chk_%s.py" % decl["id"].replace("-", "_")
        cfg = dict(cfg, name=decl["id"])
        (vd / "scripts" / script).write_text(CHECKER % json.dumps(cfg), encoding="utf-8")
        decls.append(dict(decl, script=script))
        scripts.append(script)
    data = {"schema": 1, "name": name, "plugin": plugin, "version": version,
            "requires_gt": ">=0.14.0", "summary": "fixture checker module", "default": "on",
            "scripts": scripts, "checkers": decls}
    (vd / "module.json").write_text(json.dumps(data, indent=1))
    if manifest:
        files = {"scripts/" + s: {"sha256": sha(vd / "scripts" / s),
                                  "bytes": (vd / "scripts" / s).stat().st_size}
                 for s in scripts}
        (vd / "MANIFEST.json").write_text(json.dumps({"version": version, "files": files}))
    return vd
