#!/usr/bin/env python3
"""gt_vault_location -- is this a safe place for a Golden Thread vault? (0.21.4)

    gt_vault_location.py check PATH [--json]   exit 0: no concern; 1: a warning was printed
    gt_vault_location.py default               the recommended vault path: <home>/GoldenThread

WHY. gt writes into its vault all the time: the write queue, logs, findings, hooks that touch
it on every turn. Two kinds of place make that go wrong, and both look harmless when the vault
is new and small:

  * the Documents folder (and, on a Mac, the Desktop). The operating system guards it: macOS
    asks for folder access per app (TCC) and iCloud "Desktop & Documents" may sync it; Windows
    Controlled Folder Access can block writes there and OneDrive folder backup (Known Folder
    Move) silently moves it into OneDrive. gt's heavy writing becomes slow, prompted or refused.
  * a synced or shared drive: iCloud Drive, Dropbox, OneDrive, Google Drive, Box, or a network
    share (SMB, AFP, NFS, CIFS, FUSE, a Windows UNC path or mapped network drive). Once the
    vault is large, two writers and a sync engine produce conflicted copies, half-synced files
    and lost edits -- corruption that shows up long after the choice was made.

WHAT IT DOES. It WARNS. It never refuses: a person may keep a vault in Dropbox on purpose (the
owner does), and every caller lets them confirm and carry on. The recommended place is
<home>/GoldenThread (%USERPROFILE%\\GoldenThread on Windows): local, unsynced, unguarded.

ONE HELPER. install.sh, /gt:gt-setup (gt_setup.py), /gt:gt-init (vault_init.py) and gt_doctor
all ask this module, so the patterns are written once. `assess()` takes the operating system,
the mount table, the environment and the drive-type probe as arguments, so every pattern is
unit-tested for every OS on any machine (tests/test_gt_vault_location.py).

Standard library only; writes nothing; no bytecode.
"""
import sys as _sys
import os as _os
_sys.dont_write_bytecode = True
import json
import ntpath
import os
import platform
import posixpath
import re
import subprocess
import sys

RECOMMENDED_NAME = "GoldenThread"

# Filesystem types that are a network share or a user-space (FUSE) mount -- sync clients such as
# Google Drive for desktop and many network filesystems present themselves this way.
NETWORK_FS = {"smbfs", "afpfs", "nfs", "nfs4", "cifs", "smb3", "smb2", "webdav", "davfs",
              "fuse.sshfs", "sshfs", "ncpfs", "9p", "afs", "ceph", "glusterfs", "lustre",
              "fuse.rclone", "fuse.gcsfuse", "fuse.s3fs", "osxfuse", "macfuse", "fusefs"}

WHY = {
    "documents-Darwin": "macOS guards the Documents folder (it asks for folder access per app, "
                        "and iCloud Desktop & Documents may sync it), so gt's constant writing "
                        "there is slow, prompted or refused",
    "documents-Windows": "Windows guards the Documents folder (Controlled Folder Access can "
                         "block writes, and OneDrive folder backup moves it into OneDrive), so "
                         "gt's constant writing there is slow, blocked or synced",
    "documents-Linux": "desktop sync and backup tools commonly target the Documents folder; "
                       "gt writes there constantly, so keep the vault in a folder of its own",
    "desktop": "macOS guards the Desktop folder like Documents, and iCloud Desktop & Documents "
               "may sync it, so gt's constant writing there is slow, prompted or refused",
    "synced": "a sync engine and gt both writing the same files causes conflicted copies, "
              "half-synced files and lost edits once the vault is large",
    "network": "a shared or network drive is slow for gt's many small writes, and two machines "
               "or users writing it cause conflicts and corruption once the vault is large",
    "volume": "a separate volume can be unmounted, asleep or shared; gt's hooks then fail and "
              "writes can be lost",
}


def _is_windows(system):
    return system == "Windows"


def _pm(system):
    return ntpath if _is_windows(system) else posixpath


def _norm(p, system):
    pm = _pm(system)
    p = pm.normpath(p)
    if _is_windows(system):
        p = p.replace("/", "\\")
    return p.lower() if system in ("Windows", "Darwin") else p


def _under(path, root, system):
    """path is root or inside it (case-insensitive on macOS and Windows)."""
    if not root:
        return False
    sep = "\\" if _is_windows(system) else "/"
    p, r = _norm(path, system), _norm(root, system)
    return p == r or p.startswith(r.rstrip(sep) + sep)


def _parts(path, system):
    pm = _pm(system)
    drive, rest = pm.splitdrive(pm.normpath(path))
    return [x for x in re.split(r"[\\/]+", rest) if x]


def _real_mounts(system):
    """[(mountpoint, fstype)] of this machine; [] when it cannot be read."""
    out = []
    try:
        if system == "Linux":
            with open("/proc/self/mounts", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    f = line.split()
                    if len(f) >= 3:
                        mp = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), f[1])
                        out.append((mp, f[2]))
        elif system == "Darwin":
            r = subprocess.run(["/sbin/mount"], capture_output=True, text=True, timeout=10)
            for line in r.stdout.splitlines():
                m = re.match(r"^.+? on (.+) \(([^,)]+)", line)
                if m:
                    out.append((m.group(1), m.group(2).strip()))
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    return out


def _real_drive_type(drive):
    """Windows GetDriveTypeW for 'X:\\' -> int (4 = DRIVE_REMOTE); None elsewhere or on error."""
    try:
        import ctypes
        return int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(drive)))
    except Exception:                               # noqa: BLE001
        return None


def home_dir(env=None, system=None):
    env = os.environ if env is None else env
    system = system or platform.system()
    if _is_windows(system):
        return env.get("USERPROFILE") or env.get("HOME") or os.path.expanduser("~")
    return env.get("HOME") or os.path.expanduser("~")


def recommended(env=None, system=None):
    """<home>/GoldenThread: ~/GoldenThread on POSIX, %USERPROFILE%\\GoldenThread on Windows."""
    system = system or platform.system()
    return _pm(system).join(home_dir(env, system), RECOMMENDED_NAME)


def assess(path, system=None, home=None, env=None, mounts=None, drive_type=None,
           realpath=None):
    """-> [ {"code", "label", "why"} ], empty when the path raises no concern.

    system: "Darwin" | "Linux" | "Windows" (default: this machine). home, env, mounts
    ([(mountpoint, fstype)]), drive_type (drive -> int) and realpath default to this machine's
    and are only read when system IS this machine; tests pass them for any OS."""
    here = platform.system()
    system = system or here
    env = (os.environ if system == here else {}) if env is None else env
    home = home or home_dir(env, system)
    pm = _pm(system)
    if not path:
        return []
    path = os.path.expanduser(path) if system == here else path
    if path.startswith("~"):
        path = home + path[1:]
    if not pm.isabs(path) and not (_is_windows(system) and path.startswith(("\\\\", "//"))):
        path = pm.join(os.getcwd() if system == here else home, path)
    candidates = [path]
    rp = realpath if realpath is not None else (os.path.realpath if system == here else None)
    if rp:
        try:
            r = rp(path)
            if r and _norm(r, system) != _norm(path, system):
                candidates.append(r)
        except (OSError, ValueError):
            pass
    # The same folder spelled through the home's real path (/private/var/... for /var/...,
    # a symlinked home) is still under home: compare both spellings.
    if rp:
        try:
            rh = rp(home)
        except (OSError, ValueError):
            rh = None
        if rh and _norm(rh, system) != _norm(home, system):
            for c in list(candidates):
                if _under(c, rh, system):
                    candidates.append(home + c[len(rh):])
    found, seen = [], set()

    def add(code, label, why):
        if code not in seen:
            seen.add(code)
            found.append({"code": code, "label": label, "why": WHY[why]})

    for p in candidates:
        parts = _parts(p, system)
        low = [x.lower() for x in parts]
        # --- Documents / Desktop ------------------------------------------------------------
        if _under(p, pm.join(home, "Documents"), system):
            add("documents", "inside the Documents folder",
                "documents-%s" % (system if system in ("Darwin", "Windows") else "Linux"))
        if system == "Darwin" and _under(p, pm.join(home, "Desktop"), system):
            add("desktop", "on the Desktop", "desktop")
        # --- cloud sync folders ---------------------------------------------------------------
        if system == "Darwin":
            if _under(p, pm.join(home, "Library", "Mobile Documents"), system):
                add("icloud", "in iCloud Drive (~/Library/Mobile Documents)", "synced")
            cs = pm.join(home, "Library", "CloudStorage")
            if _under(p, cs, system):
                rest = _parts(p, system)[len(_parts(cs, system)):]
                prov = (rest[0] if rest else "").lower()
                if prov.startswith("dropbox"):
                    add("dropbox", "in a Dropbox folder", "synced")
                elif prov.startswith("onedrive"):
                    add("onedrive", "in a OneDrive folder", "synced")
                elif prov.startswith("googledrive"):
                    add("google-drive", "in Google Drive", "synced")
                elif prov.startswith("box"):
                    add("box", "in a Box folder", "synced")
                else:
                    add("cloud-storage", "in a cloud-synced folder (~/Library/CloudStorage)",
                        "synced")
        if "icloud drive" in low or "icloud~com~apple~clouddocs" in low \
                or "com~apple~clouddocs" in low:
            add("icloud", "in iCloud Drive", "synced")
        if any(x == "dropbox" or x.startswith("dropbox (") or x.startswith("dropbox-")
               for x in low):
            add("dropbox", "in a Dropbox folder", "synced")
        if any(x == "onedrive" or x.startswith("onedrive - ") or x.startswith("onedrive-")
               for x in low):
            add("onedrive", "in a OneDrive folder", "synced")
        for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
            if env.get(var) and _under(p, env[var], system):
                add("onedrive", "in a OneDrive folder", "synced")
        if "my drive" in low or "google drive" in low or any(x.startswith("googledrive-")
                                                             for x in low):
            add("google-drive", "in Google Drive", "synced")
        if "box sync" in low or (parts and _under(p, pm.join(home, "Box"), system)) \
                or any(x.startswith("box-box") for x in low):
            add("box", "in a Box folder", "synced")
        # --- network and other volumes ------------------------------------------------------
        if _is_windows(system):
            if p.startswith(("\\\\", "//")):
                add("network", "on a network share (%s)" % p.replace("/", "\\")[:40], "network")
            else:
                drive = ntpath.splitdrive(p)[0]
                if drive:
                    dt = drive_type(drive + "\\") if drive_type else (
                        _real_drive_type(drive + "\\") if system == here else None)
                    if dt == 4:
                        add("network", "on a mapped network drive (%s)" % drive.upper(),
                            "network")
        else:
            mt = mounts if mounts is not None else (_real_mounts(system) if system == here
                                                    else [])
            best = None
            for mp, fs in mt:
                if _under(p, mp, system) and (best is None or len(mp) > len(best[0])):
                    best = (mp, fs)
            if best:
                fs = best[1].lower()
                if fs in NETWORK_FS or fs.startswith("fuse"):
                    add("network", "on a network or shared drive (%s at %s)" % (fs, best[0]),
                        "network")
            if system == "Darwin" and _under(p, "/Volumes", system) and len(parts) >= 2:
                vol = "/Volumes/" + parts[1]
                boot = False
                try:
                    boot = system == here and os.path.realpath(vol) == "/"
                except OSError:
                    boot = False
                if not boot and "network" not in seen:
                    add("volume", "on another volume (%s)" % vol, "volume")
    return found


def warning(path, found, rec=None, width=88):
    """The plain-words warning for `found` (from assess): lines no wider than `width` (fixed-width
    text must not soft-wrap in a terminal), no trailing newline."""
    import textwrap
    rec = rec or recommended()
    lines = ["⚠ Vault location: %s" % path]

    def para(text):
        lines.extend(textwrap.wrap(text, width=width, initial_indent="  ",
                                   subsequent_indent="  ", break_on_hyphens=False,
                                   break_long_words=False))
    for f in found:
        para("It is %s: %s." % (f["label"], f["why"]))
    para("Recommended instead: %s (a plain local folder, not synced, not guarded)." % rec)
    para("This is a warning, not a refusal: you can keep this path.")
    return "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["default"]:
        print(recommended())
        return 0
    if len(argv) >= 2 and argv[0] == "check":
        path = argv[1]
        found = assess(path)
        if "--json" in argv[2:]:
            print(json.dumps({"path": path, "risky": bool(found), "findings": found,
                              "recommended": recommended()}, indent=1))
        elif found:
            print(warning(path, found))
        return 1 if found else 0
    print(__doc__.split("\n\n")[1] if __doc__ else "usage: check PATH | default")
    return 2


if __name__ == "__main__":
    sys.exit(main())
