#!/usr/bin/env python3
"""gt_farm_engines_check.py -- qualify each farm engine FROM THIS MACHINE (gt-farm, gt 0.22.0).

    gt_farm_send.py engines check [NAME] [--services FILE] [--json] [--strict] [--timeout S]
                                  [--latency-timeout S] [--no-latency]

An engine earns its place on each machine: the route, the login and the model differ per machine, so every
check runs from the machine that will send. One line per engine, a failure naming its check:

  1 reachable      the engine answers (/health or /v1/models) within --timeout
  2 authenticated  with a key_file: the key is accepted, and a request without it is refused; with no
                   key_file the engine is `open` -- a WARNING (any machine that can reach it can use it),
                   never a failure, because until the secrets store writes keys engines stay on the LAN
  3 model          the model id the server reports matches the row's `model`
  4 calibrated     the model has a calibration record (gt_farm_calibrate.py); an uncalibrated model gets
                   no automatic work
  5 latency class  one timed prompt of about 512 tokens: fast under 10 s, slow 10-60 s, bulk-only over
                   60 s. An input to ranking (phase 57), never a failure.

    gt_farm_send.py engines status [--refresh] [--stale-min N] [--json]       (0.22.0, phase 59)
    gt_farm_send.py engines schedule --install [--every N] [--dry-run] | --remove | --status

`status` shows, per local engine, whether it is available now and since when, when and from which machine it
was last checked, and its down periods in the last day and week. It READS the last check; it contacts no
engine unless --refresh (which runs the checks, without the timed prompt). A status older than --stale-min
(default 30) is shown `stale`, never `available`. A failed packet records the engine as down by itself
(note_down). History is an append-only file of state changes, one line each, with no URL, key or text.

SAFE BY CONSTRUCTION. It sends no packet content (the timed prompt is filler text), prints no key (the
key is read from the row's key_file and goes only into an Authorization header), and keeps no URL and no
key in its cache. The cache (~/.claude/golden-thread/farm/engines-check.json, mode 600) holds, per engine
name: usable, tier, rung, the latency class and the names of the checks that failed. Exit 0 unless
--strict, which exits 1 when any engine is not usable.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import gt_farm_adapters as ADP  # noqa: E402
import gt_farm_calibration as cal  # noqa: E402
import gt_farm_send as fs  # noqa: E402

FAST_S, SLOW_S = 10.0, 60.0
FILLER_WORDS = 380                       # about 512 tokens of English filler
CACHE_MAX_AGE_S = 900


def latency_class(seconds):
    return "fast" if seconds < FAST_S else "slow" if seconds < SLOW_S else "bulk-only"


def now():
    """The clock; GT_FARM_NOW fixes it for tests."""
    try:
        return float(os.environ.get("GT_FARM_NOW") or time.time())
    except ValueError:
        return time.time()


def history_path():
    return os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "farm", "engines-history.jsonl")


def state_of(r):
    """available | unavailable | refused | mismatched, from one check_engine() result."""
    ck = r["checks"]
    if not ck["reachable"]["ok"]:
        return "unavailable"
    if not ck["authenticated"]["ok"]:
        return "refused"
    if not ck["model"]["ok"]:
        return "mismatched"
    return "available"


def _history():
    rows = []
    try:
        with open(history_path(), encoding="utf-8") as fh:
            for ln in fh:
                try:
                    d = json.loads(ln)
                    if isinstance(d, dict) and "engine" in d and "state" in d:
                        rows.append(d)
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def record_state(name, state, at=None):
    """Append one history line when this engine's state, as seen from this machine, changed."""
    machine, at = fs.machine_name(), now() if at is None else at
    last = None
    for d in _history():
        if d.get("engine") == name and d.get("machine") == machine:
            last = d
    if last and last.get("state") == state:
        return False
    path = history_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"at": at, "machine": machine, "engine": name, "state": state}, sort_keys=True) + "\n")
        return True
    except OSError:
        return False


def cache_path():
    return os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "farm", "engines-check.json")


def get_code(url, timeout, key=""):
    """-> the HTTP status of a GET, or None when nothing answered."""
    req = urllib.request.Request(url, headers={"User-Agent": "gt-farm/%s" % fs.GT_VERSION,
                                               **({"Authorization": "Bearer " + key} if key else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(64)
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def _same_model(configured, reported):
    a, b = str(configured or "").strip().lower(), str(reported or "").strip().lower()
    return not a or (bool(b) and (a == b or a in b or b in a))


def _filler():
    return " ".join(["The quick brown fox jumps over the lazy dog."] * (FILLER_WORDS // 9))


def _trust(engine, vault, st, info):
    """The trust the engine really gets (`cal.effective_trust`), never raising: when in doubt, read-only."""
    try:
        return cal.effective_trust(engine, vault, st=st, info=info)
    except Exception:                                           # noqa: BLE001
        return {"set": (engine.get("trust") or "read-only"), "mode": "read-only", "code": "uncalibrated",
                "why": "the calibration could not be read", "warn": ""}


def check_lotr_engine(engine, vault, latency=True, latency_timeout=90.0, status_fn=None):
    """The same five checks for a `transport: lotr` row (phase 73), answered through the gateway client the
    farm is enrolled as. reachable = the daemon answers and the connection has a usable (non-consent) op;
    authenticated = the farm has its own client secret (made by `lotr app-connect`) and the gateway took it.
    The result names no connection id, op or URL."""
    res = {"name": engine["name"], "tier": engine.get("tier", "home"), "rung": engine.get("rung"),
           "checks": {}, "warnings": [], "usable": False}
    ck = res["checks"]

    def skip(why, names=("authenticated", "model", "calibrated")):
        for n in names:
            ck[n] = {"ok": False, "state": "skipped", "why": why}
        ck["latency"] = {"class": "unknown", "seconds": None, "why": why}
        return res

    try:
        client = fs.lotr_client(engine)
        op, tier, bad = fs._lotr_pick(client, engine)
    except fs.LotrUnavailable as e:
        ck["reachable"] = {"ok": False, "why": str(e)[:200]}
        return skip("not reachable")
    except fs.LotrError as e:
        code = str(getattr(e, "code", "error"))
        down = code in fs.GATEWAY_DOWN or code in fs.UPSTREAM_DOWN
        ck["reachable"] = {"ok": False, "why": ("the gateway daemon did not answer (%s)" % code) if down else
                           fs._scrub("the gateway refused the farm client (%s)" % code)}
        return skip("not reachable")
    if bad:
        ck["reachable"] = {"ok": False, "why": fs._scrub(bad)[:200]}
        return skip("not reachable")
    ck["reachable"] = {"ok": True, "why": "the gateway answered and the connection has a usable operation"}
    ck["authenticated"] = {"ok": True, "state": "enrolled", "why": "the farm is enrolled as a gateway client"}
    model = (engine.get("model") or "").strip()
    ck["model"] = {"ok": bool(model), "why": "the row names the model" if model else
                   "the row names no model; a gateway reply cannot say which model answered, so name it"}
    if not model:
        ck["calibrated"] = {"ok": False, "state": "skipped", "why": "no model to look up"}
        ck["latency"] = {"class": "unknown", "seconds": None, "why": "no model"}
        return res
    # the same identity calibration saved the record under (connection, op, model and what the connection says of
    # itself): a lookup with no fingerprint finds nothing, which once made every lotr row read `uncalibrated`
    info = cal.probe_engine(engine)
    st = (status_fn or cal.status)(info, vault)
    ck["calibrated"] = {"ok": st["state"] == "calibrated", "state": st["state"], "why": st["why"]}
    res["trust"] = _trust(engine, vault, st, info)
    ck["latency"] = {"class": "unknown", "seconds": None, "why": "not timed"}
    if latency:
        t0 = time.time()
        try:
            r = fs.send_lotr(engine, _filler() + "\nReply with the single word: ok",
                             argparse.Namespace(max_tokens=4, no_bind=True, timeout=latency_timeout))   # through the row's api adapter
            secs = time.time() - t0
            ck["latency"] = ({"class": latency_class(secs), "seconds": round(secs, 2), "why": "timed"}
                             if r.get("http") == 200 else
                             {"class": "unknown", "seconds": None, "why": "the timed prompt got no reply"})
        except Exception:                                       # noqa: BLE001 -- a timeout is the answer here
            ck["latency"] = {"class": "bulk-only", "seconds": None, "why": "no answer within %gs" % latency_timeout}
    res["usable"] = all(ck[n]["ok"] for n in ("reachable", "authenticated", "model", "calibrated"))
    return res


def check_engine(engine, vault, timeout=5.0, latency=True, latency_timeout=90.0, status_fn=None):
    if engine.get("transport") == fs.LOTR_TRANSPORT:
        return check_lotr_engine(engine, vault, latency, latency_timeout, status_fn)
    res = {"name": engine["name"], "tier": engine.get("tier", "home"), "rung": engine.get("rung"),
           "checks": {}, "warnings": [], "usable": False}
    ck = res["checks"]
    key, key_problem = "", ""
    if engine.get("key_file"):
        try:
            key = fs.load_key(engine)
        except RuntimeError as e:
            key_problem = str(e).split(": ", 1)[0]              # "cannot read key file for NAME", no path, no value
    info = cal.probe_engine(engine, timeout, key)
    ck["reachable"] = {"ok": bool(info.get("up")),
                       "why": "answered" if info.get("up") else "no answer within %gs (route, VPN or the engine is down)" % timeout}
    if not info.get("up"):
        for name in ("authenticated", "model", "calibrated"):
            ck[name] = {"ok": False, "state": "skipped", "why": "not reachable"}
        ck["latency"] = {"class": "unknown", "seconds": None, "why": "not reachable"}
        return res
    base = engine["base_url"]
    # 2. authenticated
    if key_problem:
        ck["authenticated"] = {"ok": False, "state": "failed", "why": key_problem}
    elif not engine.get("key_file"):
        ck["authenticated"] = {"ok": True, "state": "open", "why": "no key_file: the engine is open to any machine that can reach it"}
        res["warnings"].append("%s is open: no key_file, so any machine that can reach it can use it" % engine["name"])
    else:
        with_key, without = get_code(base + "/models", timeout, key), get_code(base + "/models", timeout)
        if with_key != 200:
            ck["authenticated"] = {"ok": False, "state": "failed", "why": "the key was not accepted (HTTP %s)" % with_key}
        else:
            ck["authenticated"] = {"ok": True, "state": "keyed", "why": "key accepted"}
            if without == 200:
                res["warnings"].append("%s also answers without the key (the server does not require one)" % engine["name"])
    if not ck["authenticated"]["ok"]:                           # a refused key hides the model: nothing more to read
        ck["model"] = {"ok": False, "state": "skipped", "why": "not authenticated"}
        ck["calibrated"] = {"ok": False, "state": "skipped", "why": "not authenticated"}
        ck["latency"] = {"class": "unknown", "seconds": None, "why": "not authenticated"}
        return res
    # 3. model
    reported = info.get("model_id")
    same = _same_model(engine.get("model"), reported) and bool(reported)
    ck["model"] = {"ok": same, "why": "serves %s" % reported if same else
                   "the server reports %r, the row says %r" % (reported, engine.get("model"))}
    # 4. calibrated
    st = (status_fn or cal.status)(info, vault)
    ck["calibrated"] = {"ok": st["state"] == "calibrated", "state": st["state"], "why": st["why"]}
    res["trust"] = _trust(engine, vault, st, info)
    # 5. latency (only worth timing an engine we would use)
    ck["latency"] = {"class": "unknown", "seconds": None, "why": "not timed"}
    if latency and ck["authenticated"]["ok"] and ck["model"]["ok"]:
        adapter = ADP.get(engine.get("api"))
        payload = adapter.build([{"role": "user", "content": _filler() + "\nReply with the single word: ok"}],
                                {"model": engine.get("model") or reported, "max_tokens": 4, "temperature": 0})
        if adapter.model_in_path:
            payload.pop("model", None)
        t0 = time.time()
        try:
            code, _body = fs.post(engine, payload, latency_timeout, key)
            secs = time.time() - t0
            ck["latency"] = ({"class": latency_class(secs), "seconds": round(secs, 2), "why": "timed"} if code == 200
                             else {"class": "unknown", "seconds": None, "why": "the timed prompt got HTTP %s" % code})
        except Exception:                                       # noqa: BLE001 -- a timeout is the answer here
            ck["latency"] = {"class": "bulk-only", "seconds": None, "why": "no answer within %gs" % latency_timeout}
    res["usable"] = all(ck[n]["ok"] for n in ("reachable", "authenticated", "model", "calibrated"))
    return res


def failed_checks(r):
    return [n for n in ("reachable", "authenticated", "model", "calibrated") if not r["checks"][n]["ok"]]


def _trust_fields(r):
    """The trust facts the cache keeps: the row's setting, what it really gets and why (no URL, key or text)."""
    tr = r.get("trust") or {}
    return {"set": tr.get("set"), "mode": tr.get("mode"), "code": tr.get("code"),
            "note": cal.downgrade_note(tr, r["name"])}


def write_cache(results):
    t = now()
    doc = {"v": 1, "at": t, "machine": fs.machine_name(),
           "engines": {r["name"]: {"usable": r["usable"], "tier": r["tier"], "rung": r["rung"],
                                   "latency_class": r["checks"]["latency"]["class"],
                                   "failed": failed_checks(r), "state": state_of(r), "checked_at": t,
                                   "trust": _trust_fields(r)}
                       for r in results}}
    for r in results:
        record_state(r["name"], state_of(r), t)
    path = cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(doc, fh, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass


def _raw_cache():
    try:
        with open(cache_path(), encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) and doc.get("machine") == fs.machine_name() else None
    except (OSError, ValueError, TypeError):
        return None


def note_down(name):
    """A packet to this engine failed (it is down): record it now, without a check. Updates the engine's
    entry in the cache (leaving the cache's own age alone, so ranking still re-checks on its schedule) and
    appends the state change. Never raises."""
    try:
        t = now()
        doc = _raw_cache() or {"v": 1, "at": 0, "machine": fs.machine_name(), "engines": {}}
        e = doc["engines"].setdefault(name, {"tier": "home", "rung": None, "latency_class": "unknown"})
        e.update(usable=False, state="unavailable", checked_at=t,
                 failed=sorted(set(e.get("failed") or []) | {"reachable"}))
        path = cache_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(doc, fh, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
        record_state(name, "unavailable", t)
    except Exception:                                           # noqa: BLE001
        pass


def status(stale_min=30.0):
    """-> [row] per engine in the last check for this machine: state (or `stale`), since, checked_at, machine,
    down_day, down_week. Reads files only."""
    doc = _raw_cache() or {}
    t, hist, out = now(), _history(), []
    for name, e in sorted((doc.get("engines") or {}).items()):
        mine = [d for d in hist if d.get("engine") == name and d.get("machine") == doc.get("machine")]
        cur = e.get("state") or ("available" if e.get("usable") else "unavailable")
        since = e.get("checked_at") or doc.get("at") or 0
        for i in range(len(mine) - 1, -1, -1):      # the start of the current run of this state
            if mine[i]["state"] != cur:
                since = mine[i + 1]["at"] if i + 1 < len(mine) else since
                break
        else:
            since = mine[0]["at"] if mine else since
        checked = e.get("checked_at") or doc.get("at") or 0
        stale = t - checked > stale_min * 60
        downs = [d["at"] for d in mine if d["state"] == "unavailable"]
        out.append({"name": name, "state": "stale" if stale else cur, "last_state": cur, "since": since,
                    "checked_at": checked, "machine": doc.get("machine"), "tier": e.get("tier"), "rung": e.get("rung"),
                    "trust": e.get("trust") or {},
                    "down_day": sum(1 for a in downs if t - a <= 86400), "down_week": sum(1 for a in downs if t - a <= 7 * 86400)})
    return out


def status_lines(rows, stale_min):
    out = []
    for r in rows:
        ago = lambda ts: "%dm ago" % max(0, round((now() - ts) / 60)) if now() - ts < 7200 else time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
        state = r["state"] if r["state"] != "stale" else "STALE (was %s)" % r["last_state"]
        out.append("%-16s %-22s since %s · checked %s from %s · down %d× today, %d× this week" % (
            r["name"], state, time.strftime("%Y-%m-%d %H:%M", time.localtime(r["since"])), ago(r["checked_at"]),
            r["machine"], r["down_day"], r["down_week"]))
        note = (r.get("trust") or {}).get("note")
        if note:
            out.append("    " + note)
        elif (r.get("trust") or {}).get("mode") in ("read-only", "autonomous"):
            out[-1] += " · trust: %s" % r["trust"]["mode"]
    count = cal.downgrade_count_line([r.get("trust") if (r.get("trust") or {}).get("note") else None for r in rows])
    if count:
        out.append(count)
    return out


def read_cache(max_age=CACHE_MAX_AGE_S):
    """-> the cached result for THIS machine if it is fresh, else None (phase 57 reads it)."""
    try:
        with open(cache_path(), encoding="utf-8") as fh:
            doc = json.load(fh)
        if doc.get("machine") == fs.machine_name() and time.time() - float(doc.get("at", 0)) <= max_age:
            return doc
    except (OSError, ValueError, TypeError):
        pass
    return None


def line(r):
    ck = r["checks"]
    parts = []
    for n, label in (("reachable", "reachable"), ("authenticated", "authenticated"), ("model", "model"), ("calibrated", "calibrated")):
        c = ck[n]
        if c.get("state") == "skipped":
            parts.append("%s skipped" % label)
        elif c["ok"]:
            extra = " (%s)" % c["state"] if n in ("authenticated",) and c.get("state") in ("open", "keyed") else ""
            parts.append("%s ok%s" % (label, extra))
        else:
            parts.append("%s FAIL (%s)" % (label, c["why"]))
    lat = ck["latency"]
    parts.append("latency %s%s" % (lat["class"], " %.1fs" % lat["seconds"] if lat.get("seconds") else ""))
    tr = r.get("trust") or {}
    note = cal.downgrade_note(tr, r["name"])
    if note:                                    # the trust FIRST: "calibrated ok" must not read as "will run alone"
        parts.insert(0, note)
    elif tr.get("mode") in ("read-only", "autonomous"):
        parts.insert(0, "trust: %s" % tr["mode"])
    return "%-16s rung %s %-6s %-10s %s" % (r["name"], r["rung"], r["tier"], "USABLE" if r["usable"] else "NOT USABLE",
                                           " · ".join(parts))


def main_status(argv):
    ap = argparse.ArgumentParser(prog="gt_farm_send.py engines status")
    ap.add_argument("--refresh", action="store_true", help="run the checks first (no timed prompt)")
    ap.add_argument("--stale-min", type=float, default=30.0)
    ap.add_argument("--services")
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    vault = fs.vault_path()
    engines = cal.table_engines(a.services, vault, include_lotr=True)
    if not engines:
        msg = "no local engine in the service table"
        print(json.dumps({"engines": [], "note": msg}) if a.json else msg)
        return 0
    if a.refresh:
        write_cache([check_engine(e, vault, a.timeout, latency=False) for e in engines])
    rows = status(a.stale_min)
    if a.json:
        print(json.dumps({"machine": fs.machine_name(), "engines": rows}, sort_keys=True))
    elif not rows:
        print("no status recorded yet: run `gt_farm_send.py engines status --refresh`")
    else:
        print("engines from %s:" % fs.machine_name())
        for ln in status_lines(rows, a.stale_min):
            print("  " + ln)
    return 0


# ---- the session-start step (phase 76): keep the status current, say it only when something is wrong ----

REFRESH_MODES = ("session-start", "scheduled", "off")
REFRESH_CAP_S = 20                       # the detached refresh is killed after this long
REFRESH_EVERY_MIN = 30                   # at most one automatic refresh per this many minutes
DOWN_STATES = ("unavailable", "refused", "mismatched")
_WATCHDOG = ("import subprocess, sys\n"
             "try:\n"
             "    subprocess.run(sys.argv[2:], timeout=float(sys.argv[1]), stdin=subprocess.DEVNULL,\n"
             "                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
             "except Exception:\n"
             "    pass\n")


def refresh_mode():
    """farm_engine_refresh: session-start (default) | scheduled | off. Anything else is the default."""
    v = fs.setting("farm_engine_refresh", "session-start").lower()
    return v if v in REFRESH_MODES else "session-start"


def refresh_stamp_path():
    return os.path.join(os.path.expanduser("~"), ".claude", "golden-thread", "farm", "engines-refresh.stamp")


def _spawn(argv):
    """Run `argv` detached, silent, never waited for (the hook must not slow a session)."""
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200        # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(argv, **kw)


def session_refresh(now_s=None, runner=None, mode=None, every_min=REFRESH_EVERY_MIN, engines=None):
    """SessionStart: start `engines status --refresh` detached and bounded (REFRESH_CAP_S) when the mode is
    session-start, a local engine exists and none was started in the last `every_min` minutes. The stamp is
    written first, so two sessions starting together fire one. Returns True when it started one. Fail-open,
    silent, never contacts an engine itself."""
    try:
        if (mode or refresh_mode()) != "session-start":
            return False
        if engines is None:
            engines = cal.table_engines(None, fs.vault_path(), include_lotr=True)
        if not engines:
            return False
        t = time.time() if now_s is None else now_s
        stamp = refresh_stamp_path()
        try:
            with open(stamp, encoding="utf-8") as fh:
                last = float(fh.read().strip())
        except (OSError, ValueError):
            last = None
        if last is not None and 0 <= t - last < every_min * 60:
            return False
        os.makedirs(os.path.dirname(stamp), exist_ok=True)
        with open(stamp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("%d\n" % t)
        (runner or _spawn)([sys.executable, "-c", _WATCHDOG, str(REFRESH_CAP_S), sys.executable,
                            os.path.join(HERE, "gt_farm_send.py"), "engines", "status", "--refresh"])
        return True
    except Exception:                                            # noqa: BLE001 -- never blocks a session
        return False


def _since(ts):
    t = time.localtime(ts)
    return time.strftime("%H:%M" if time.strftime("%Y%m%d", t) == time.strftime("%Y%m%d", time.localtime(now())) else "%m-%d %H:%M", t)


def session_line(mode=None, stale_min=30.0, rows=None):
    """-> 'farm engines: 5 of 7 available, engine-5 down since 14:02' or None. Silent when no engine has a
    status, and when every engine is available. A stale status is named only when no refresh is coming
    (mode is not session-start). Reads files only; no URL, key or packet text."""
    try:
        mode = mode or refresh_mode()
        rows = status(stale_min) if rows is None else rows
        if not rows:
            return None
        down = [r for r in rows if r["state"] in DOWN_STATES]
        stale = [r for r in rows if r["state"] == "stale"] if mode != "session-start" else []
        if not down and not stale:
            return None
        text = "farm engines: %d of %d available" % (sum(1 for r in rows if r["state"] == "available"), len(rows))
        named = ["%s down since %s" % (r["name"], _since(r["since"])) for r in down[:2]]
        if len(down) > 2:
            named.append("+%d more down" % (len(down) - 2))
        if stale:
            named.append("%d status stale (last check %s)" % (len(stale), _since(min(r["checked_at"] for r in stale))))
        return text + ", " + ", ".join(named)
    except Exception:                                            # noqa: BLE001
        return None


# ---- the scheduled refresh: ONE periodic job, registered only when the owner asks ----------------

SCHEDULE_JOB = "farm-engines"


def schedule_module():
    """gt core's gt_schedule.py (the one scheduling helper: launchd, systemd --user, cron, Task Scheduler), or None."""
    return cal.core_module("gt_schedule")


def main_schedule(argv, sched=None, mode=None):
    """`engines schedule --install|--remove|--status [--every N] [--dry-run]`. Registers nothing unless
    farm_engine_refresh is `scheduled` AND this command is run; removal and status work in any mode."""
    ap = argparse.ArgumentParser(prog="gt_farm_send.py engines schedule",
                                 description="register ONE periodic job that runs `engines status --refresh`")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--install", action="store_true", help="register the job (setting must be `scheduled`)")
    g.add_argument("--remove", action="store_true", help="unregister it and delete its files and logs")
    g.add_argument("--status", action="store_true", help="read back what is registered")
    ap.add_argument("--every", type=int, default=30, metavar="MINUTES",
                    help="the period (default 30; 5-30 dividing the hour, or whole hours up to 1440)")
    ap.add_argument("--dry-run", action="store_true", help="with --install: print what would be installed, install nothing")
    a = ap.parse_args(argv)
    mode = mode or refresh_mode()
    sched = sched or schedule_module()
    if sched is None:
        print("farm engine schedule: gt core's gt_schedule.py was not found, so nothing was changed", file=sys.stderr)
        return 1
    label = sched.label_for(SCHEDULE_JOB)
    if a.remove:
        return sched.main(["remove", SCHEDULE_JOB])
    if a.status:
        return _schedule_status(sched, label, mode)
    if mode != "scheduled":
        print("farm engine schedule: not installed: farm_engine_refresh is %r; set it with `gt_settings.py set "
              "farm_engine_refresh scheduled`, then run this again" % mode, file=sys.stderr)
        return 1
    if not sched.valid_every(a.every):
        print("farm engine schedule: --every %s is not a period every scheduler keeps exactly: 5, 10, 12, 15, 20, "
              "30 minutes, or 60, 120, 180, 240, 360, 480, 720, 1440" % a.every, file=sys.stderr)
        return 2
    if a.dry_run:
        doc = sched.build_plist(SCHEDULE_JOB, None, [], 0, 0, None, every_min=a.every)
        print("dry run: would install %s on %s:" % (label, sched.platform_kind()))
        print("  every %d minutes run: %s" % (a.every, " ".join(doc["ProgramArguments"])))
        print("  job file: %s" % sched.job_file(SCHEDULE_JOB))
        print("  logs: %s, %s" % (doc["StandardOutPath"], doc["StandardErrorPath"]))
        print("dry run: nothing was registered")
        return 0
    return sched.main(["install", SCHEDULE_JOB, "--every", str(a.every)])


def _schedule_status(sched, label, mode):
    doc = sched.read_doc(SCHEDULE_JOB) if sched.job_file(SCHEDULE_JOB).is_file() else None
    if not doc:
        print("farm engine schedule: not installed (%s); farm_engine_refresh is %s" % (label, mode))
        return 0
    every = int(doc.get("StartInterval") or 0) // 60
    registered = sched.job_registered(SCHEDULE_JOB)
    code, problems = sched.job_status(SCHEDULE_JOB)
    print("farm engine schedule: installed, %s, every %d minutes, %s, last exit %s; farm_engine_refresh is %s"
          % (label, every, {True: "registered", False: "on disk but NOT registered", None: "registration unknown"}[registered],
             code if code is not None else "(not yet run)", mode))
    for p in problems:
        print("  problem: %s" % p)
    if mode != "scheduled":
        print("  the setting is %s, not scheduled: remove the job with `gt_farm_send.py engines schedule --remove`" % mode)
    return 1 if problems else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["status"]:
        return main_status(argv[1:])
    if argv[:1] == ["schedule"]:
        return main_schedule(argv[1:])
    if argv[:1] == ["check"]:
        argv = argv[1:]
    ap = argparse.ArgumentParser(prog="gt_farm_send.py engines check", description=__doc__.split("\n")[1])
    ap.add_argument("name", nargs="?", help="one engine (a service-table row name); default every local engine")
    ap.add_argument("--services", help="the service table (default: farm_services in vault-config.json)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="exit 1 when any engine is not usable")
    ap.add_argument("--timeout", type=float, default=5.0, help="seconds each reachability probe may take")
    ap.add_argument("--latency-timeout", type=float, default=90.0)
    ap.add_argument("--no-latency", action="store_true", help="skip the timed prompt")
    a = ap.parse_args(argv)
    vault = fs.vault_path()
    engines = cal.table_engines(a.services, vault, service=a.name, include_lotr=True)
    if not engines:
        msg = ("no engine named %r in the service table" % a.name) if a.name else "no local engine in the service table"
        print(json.dumps({"engines": [], "note": msg}) if a.json else msg)
        return 0
    results = [check_engine(e, vault, a.timeout, not a.no_latency, a.latency_timeout) for e in engines]
    write_cache(results)
    if a.json:
        print(json.dumps({"machine": fs.machine_name(), "engines": results}, sort_keys=True))
    else:
        print("engines from %s:" % fs.machine_name())
        for r in results:
            print("  " + line(r))
            for w in r["warnings"]:
                print("      warning: " + w)
        count = cal.downgrade_count_line([r.get("trust") if cal.downgrade_note(r.get("trust"), r["name"]) else None for r in results])
        if count:
            print("  " + count)
    return 1 if a.strict and not all(r["usable"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
