#!/usr/bin/env python3
"""Apply one-time structural changes to the MACHINE that a skipped release would have made.

    gt_machine_migrate.py [--home DIR] status --release <release-dir>
    gt_machine_migrate.py [--home DIR] run --release <release-dir> [--dry-run]
                          [--previous-release X.Y.Z]

## Why this exists

Requirement R1 (2026-09-14): one install from any older release must equal a fresh
install of the newest, with nothing legacy stuck. `gt_upgrade.py` migrates the VAULT.
Nothing migrated the machine -- `~/.claude/golden-thread/`, `~/.claude/settings.json`,
`~/.claude/vault-config.json` -- so a one-time change (a setting key moving, a skill
extracted into a module plugin that must stay enabled for upgraders) made by release N
was simply never made for someone who jumped from N-2 to N+1. `install.sh` installs only
the newest release, so every release must carry the knowledge of every earlier one.

## Pending is read from the machine, not from the record

A migration is PENDING when it is not recorded as applied AND its `needed()` says the
machine still needs it. `needed()` inspects real state, so:

  * a machine that never needed a change is not touched, record or no record;
  * a state file copied from another machine, or a `~/.claude` restored from a backup,
    still converges -- a migration recorded applied whose `needed()` is true again is
    pending again, and reported as re-applying.

## The record

`<home>/.claude/golden-thread/machine-state.json`:

    {"version": 1, "applied": {"<id>": {"at": ISO, "release": "x.y.z"}},
     "last_release": "x.y.z"}

written atomically (temp file + os.replace), the previous copy backed up first.

## Rules every migration follows

  * `apply(ctx)` is idempotent: interrupted and re-run, it finishes the job.
  * It backs a file up with `ctx.backup(path)` before changing it.
  * It writes only under `<home>/.claude/` -- the ctx helpers refuse anything else.
  * It never deletes a user's settings key the migration does not name.
  * `run` stops at the first failure: later migrations may depend on earlier ones.

## The previous release

Some migrations depend on what was installed BEFORE this install (0.15.0: a gt that shipped
/gt:gt-farm). By the time install.sh runs the migrator it has already rewritten
installed_plugins.json and pruned the old gt cache, so the migrator cannot read that from
the machine any more. install.sh therefore reads it first, from the gt entry in
installed_plugins.json, and passes it as `--previous-release` (or GT_PREVIOUS_RELEASE).
Without either, `previous_release()` falls back to installed_plugins.json when it names a
different gt than the release being applied, then to machine-state.json's `last_release`.

## Exit codes

  status: 0 whether or not anything is pending; 2 if it could not evaluate (an
          unreadable state file, a `needed()` that raised -- a check that could not
          look is not clean).
  run:    0 all applied or nothing pending; 1 a migration failed; 2 could not evaluate.

## Test hook

`GT_MACHINE_MIGRATIONS_MODULE=<file.py>` replaces the registry with that file's
`MIGRATIONS`. It exists FOR TESTS (ordering, stop-on-failure, re-apply with fixture
migrations) and is not a user feature.
"""
import argparse
import datetime
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_REL = Path(".claude") / "golden-thread" / "machine-state.json"
BACKUPS_REL = Path(".claude") / "golden-thread" / "backups"
STATE_VERSION = 1


class Unevaluable(Exception):
    """Status cannot be determined; exit 2."""


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def release_version(release_dir):
    try:
        return json.loads((Path(release_dir) / ".claude-plugin" / "plugin.json")
                          .read_text(encoding="utf-8"))["version"]
    except Exception:
        return Path(release_dir).name


# ---- context ----------------------------------------------------------------

class Ctx:
    """What a migration is handed. Every write helper stays inside <home>/.claude/."""

    def __init__(self, home, release_dir, release, dry_run, mid=None, previous=None):
        self.home = Path(home)
        self.previous = previous
        self.claude = self.home / ".claude"
        self.gt_dir = self.claude / "golden-thread"
        self.release_dir = Path(release_dir)
        self.release = release
        self.dry_run = dry_run
        self.id = mid
        self._backup_dir = None

    def inside(self, path):
        """The resolved path, or ValueError if it is outside <home>/.claude/."""
        p = Path(path)
        if not p.is_absolute():
            p = self.home / p
        root = os.path.realpath(self.claude)
        real = os.path.realpath(p)
        if real != root and not real.startswith(root + os.sep):
            raise ValueError("refused: %s is outside %s" % (p, self.claude))
        return p

    def backup(self, path):
        """Copy `path` to backups/machine-<id>-<stamp>/<path relative to home>.

        Returns the copy, or None if there was nothing to back up. Never in a dry run.
        """
        p = self.inside(path)
        if self.dry_run or not p.exists():
            return None
        if self._backup_dir is None:
            self._backup_dir = self.home / BACKUPS_REL / (
                "machine-%s-%s" % (self.id or "state", _stamp()))
        rel = Path(os.path.realpath(p)).relative_to(os.path.realpath(self.home))
        dest = self._backup_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if p.is_dir():
            shutil.copytree(p, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(p, dest)
        return dest

    def read_json(self, path, default=None):
        """Parsed JSON, or `default` when the file is absent. A corrupt file raises."""
        p = Path(path) if Path(path).is_absolute() else self.home / path
        if not p.is_file():
            return default
        return json.loads(p.read_text(encoding="utf-8"))

    def write_json(self, path, data, sort_keys=False):
        """Atomic write (temp file in the same directory, then os.replace)."""
        p = self.inside(path)
        if self.dry_run:
            return
        atomic_write_json(p, data, sort_keys=sort_keys)


def atomic_write_json(path, data, sort_keys=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, sort_keys=sort_keys) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---- migrations -------------------------------------------------------------
#
# Each: id, introduced, description, needed(ctx) -> bool, apply(ctx) -> None (raise on
# failure). ORDER MATTERS -- later entries may assume earlier ones ran. Append only; an
# id, once shipped, is never renamed or reused.

class Migration:
    def __init__(self, id, introduced, description, needed, apply):
        self.id, self.introduced, self.description = id, introduced, description
        self.needed, self.apply = needed, apply


VAULT_CONFIG = Path(".claude") / "vault-config.json"
INSTALL_CHOICES = Path(".claude") / "golden-thread" / "install-choices.json"


def _demo_choice(value):
    # Mirrors install.sh exactly: `str(d.get('install_demo') or 'yes').strip().lower()`
    # and only "no" skips the demo. Anything else installed it, so it records "on".
    return "off" if str(value or "yes").strip().lower() == "no" else "on"


def _choices(ctx):
    data = ctx.read_json(INSTALL_CHOICES, default=None)
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError("%s is not a JSON object" % INSTALL_CHOICES)
    return data


def needed_install_choices(ctx):
    cfg = ctx.read_json(VAULT_CONFIG, default=None)
    if not isinstance(cfg, dict) or "install_demo" not in cfg:
        return False
    data = _choices(ctx) or {}
    choices = data.get("choices")
    return not (isinstance(choices, dict) and "demo" in choices)


def apply_install_choices(ctx):
    cfg = ctx.read_json(VAULT_CONFIG, default=None)
    if not isinstance(cfg, dict) or "install_demo" not in cfg:
        return
    data = _choices(ctx) or {}
    choices = data.get("choices")
    if choices is not None and not isinstance(choices, dict):
        raise ValueError("%s: 'choices' is not an object; refusing to overwrite it"
                         % INSTALL_CHOICES)
    choices = dict(choices or {})
    if "demo" in choices:
        return
    choices["demo"] = _demo_choice(cfg["install_demo"])
    out = dict(data)
    out.setdefault("version", 1)
    out["choices"] = choices
    ctx.backup(INSTALL_CHOICES)
    # vault-config.json is deliberately NOT modified: install.sh still reads
    # install_demo until modules ship. Removing it is a later migration.
    # sort_keys: the same bytes gt_components.record_choice writes (0.15.0, R1); both 0600.
    ctx.write_json(INSTALL_CHOICES, out, sort_keys=True)


def _vkey(v):
    try:
        return tuple(int(x) for x in str(v).strip().split("."))
    except (TypeError, ValueError):
        return None


INSTALLED_PLUGINS = Path(".claude") / "plugins" / "installed_plugins.json"


def previous_release(ctx):
    """The gt version installed before this install, or None when there is no evidence.

    In order: what install.sh read before it wrote anything (ctx.previous); the gt entry in
    installed_plugins.json when it is not the release being applied (the migrator run by
    hand before an install); machine-state.json's last_release (0.14.0 and later write it
    at the end of every install, so during an install it still names the previous one).
    """
    if ctx.previous and _vkey(ctx.previous):
        return ctx.previous
    try:
        entry = ((ctx.read_json(INSTALLED_PLUGINS, default={}) or {}).get("plugins") or {}) \
            .get("gt@golden-thread-plugin")
        ver = entry[0].get("version") if isinstance(entry, list) and entry else None
    except (ValueError, AttributeError, TypeError):
        ver = None
    if ver and _vkey(ver) and ver != ctx.release:
        return ver
    try:
        last = (ctx.read_json(STATE_REL, default={}) or {}).get("last_release")
    except (ValueError, AttributeError):
        last = None
    return last if last and _vkey(last) and last != ctx.release else None


# gt releases that shipped /gt:gt-farm inside gt: added in 0.9.4, moved to the farm module
# (default OFF) in 0.15.0.
FARM_IN_GT = ((0, 9, 4), (0, 15, 0))


def needed_farm_kept_for_upgraders(ctx):
    """An upgrade from a gt that had /gt:gt-farm, with no farm choice recorded.

    Fresh installs get the module default (off). Someone who already had the skill must
    not lose it silently because it moved into a module: record farm=on for them. A
    recorded choice either way -- including --without farm on this very install, which
    install.sh records before migrations run -- is never overridden.
    """
    prev = _vkey(previous_release(ctx))
    if prev is None or not (FARM_IN_GT[0] <= prev < FARM_IN_GT[1]):
        return False
    choices = (_choices(ctx) or {}).get("choices")
    return not (isinstance(choices, dict) and "farm" in choices)


def apply_farm_kept_for_upgraders(ctx):
    data = _choices(ctx) or {}
    choices = data.get("choices")
    if choices is not None and not isinstance(choices, dict):
        raise ValueError("%s: 'choices' is not an object; refusing to overwrite it"
                         % INSTALL_CHOICES)
    choices = dict(choices or {})
    if "farm" in choices:
        return
    choices["farm"] = "on"
    out = dict(data)
    out.setdefault("version", 1)
    out["choices"] = choices
    ctx.backup(INSTALL_CHOICES)
    # sort_keys: the same bytes gt_components.record_choice writes (0.15.0, R1); both 0600.
    ctx.write_json(INSTALL_CHOICES, out, sort_keys=True)


MIGRATIONS = (
    Migration("install-choices-from-vault-config", "0.14.0",
              "record install_demo from vault-config.json as the demo install choice",
              needed_install_choices, apply_install_choices),
    # watch and report card also left gt in 0.15.0, but their modules default ON, so an
    # upgrader keeps them with no choice recorded. A `watch` or `report_card` SETTING of
    # off is a setting, not an uninstall, and is deliberately not turned into a choice.
    Migration("farm-kept-for-upgraders", "0.15.0",
              "keep /gt-farm:gt-farm for a machine upgrading from a gt that shipped /gt:gt-farm",
              needed_farm_kept_for_upgraders, apply_farm_kept_for_upgraders),
)


def _field(m, name):
    return m[name] if isinstance(m, dict) else getattr(m, name)


def registry():
    """The shipped MIGRATIONS, or -- for tests only -- GT_MACHINE_MIGRATIONS_MODULE's."""
    path = os.environ.get("GT_MACHINE_MIGRATIONS_MODULE")
    if not path:
        items = MIGRATIONS
    else:
        try:
            spec = importlib.util.spec_from_file_location("gt_machine_migrations_fixture", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            items = mod.MIGRATIONS
        except Exception as exc:
            raise Unevaluable("could not load registry %s: %s" % (path, exc))
    out, seen = [], set()
    for m in items:
        mig = Migration(*(_field(m, k) for k in
                          ("id", "introduced", "description", "needed", "apply")))
        if mig.id in seen:
            raise Unevaluable("duplicate migration id: %s" % mig.id)
        seen.add(mig.id)
        out.append(mig)
    return out


# ---- state ------------------------------------------------------------------

def read_state(home):
    p = Path(home) / STATE_REL
    if not p.is_file():
        return {"version": STATE_VERSION, "applied": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise Unevaluable("unreadable %s: %s" % (p, exc))
    if not isinstance(data, dict) or not isinstance(data.get("applied", {}), dict):
        raise Unevaluable("malformed %s" % p)
    data.setdefault("applied", {})
    data.setdefault("version", STATE_VERSION)
    return data


def write_state(home, state):
    p = Path(home) / STATE_REL
    if p.is_file():
        dest = Path(home) / BACKUPS_REL / ("machine-state-%s" % _stamp()) / STATE_REL
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)
    atomic_write_json(p, state)


def plan(home, release_dir, release, dry_run, previous=None):
    """-> (state, [(migration, reapply)]) for everything pending, in registry order."""
    state = read_state(home)
    todo = []
    for m in registry():
        ctx = Ctx(home, release_dir, release, dry_run, m.id, previous)
        try:
            need = bool(m.needed(ctx))
        except Exception as exc:
            raise Unevaluable("%s: needed() failed: %s" % (m.id, exc))
        if need:
            todo.append((m, m.id in state["applied"]))
    return state, todo


# ---- commands ---------------------------------------------------------------

def cmd_status(a, release):
    _state, todo = plan(a.home, a.release, release, True, a.previous_release)
    if not todo:
        print("Machine migrations: none pending")
        return 0
    print("Machine migrations pending:")
    for m, reapply in todo:
        print("  [%s] %s  %s%s" % (m.introduced, m.id, m.description,
                                   "  (re-applying: recorded applied, needed again)"
                                   if reapply else ""))
    return 0


def cmd_run(a, release):
    state, todo = plan(a.home, a.release, release, a.dry_run, a.previous_release)
    if a.dry_run:
        if not todo:
            print("Machine migrations: none pending")
        for m, reapply in todo:
            print("would apply %s%s" % (m.id, " (re-applying)" if reapply else ""))
        return 0
    for m, reapply in todo:
        ctx = Ctx(a.home, a.release, release, False, m.id, a.previous_release)
        try:
            m.apply(ctx)
            if m.needed(ctx):
                raise RuntimeError("still needed after apply")
        except Exception as exc:
            print("FAILED %s: %s" % (m.id, exc))
            return 1
        state["applied"][m.id] = {"at": now_iso(), "release": release}
        write_state(a.home, state)      # record each as it lands: an interrupt loses none
        print("applied %s%s" % (m.id, " (re-applied)" if reapply else ""))
    if not todo and not (Path(a.home) / STATE_REL).parent.is_dir():
        # Nothing to do on a machine with no golden-thread dir: do not create one.
        return 0
    state["version"] = STATE_VERSION
    state["last_release"] = release
    write_state(a.home, state)
    return 0


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", default=argparse.SUPPRESS,
                        help="home directory holding .claude/ (default: $HOME)")
    common.add_argument("--dry-run", "-n", action="store_true", default=argparse.SUPPRESS,
                        help="report what would apply; write nothing")
    common.add_argument("--release", default=argparse.SUPPRESS,
                        help="the release version directory (default: this script's)")
    common.add_argument("--previous-release", default=argparse.SUPPRESS,
                        help="the gt version installed before this install (install.sh "
                             "passes it; default: $GT_PREVIOUS_RELEASE, then the machine)")
    p = argparse.ArgumentParser(description="one-time machine migrations", parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", parents=[common])
    sub.add_parser("run", parents=[common])
    a = p.parse_args(argv)
    for key, default in (("home", None), ("dry_run", False), ("release", None),
                         ("previous_release", None)):
        if not hasattr(a, key):
            setattr(a, key, default)
    a.home = a.home or os.environ.get("HOME") or str(Path.home())
    a.release = a.release or str(HERE.parent)
    a.previous_release = (a.previous_release or os.environ.get("GT_PREVIOUS_RELEASE") or "").strip() or None
    if not Path(a.home).is_dir():
        print("no such home: %s" % a.home, file=sys.stderr)
        return 2
    if not Path(a.release).is_dir():
        print("no such release directory: %s" % a.release, file=sys.stderr)
        return 2
    release = release_version(a.release)
    try:
        return {"status": cmd_status, "run": cmd_run}[a.cmd](a, release)
    except Unevaluable as exc:
        print("Machine migrations: cannot evaluate: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
