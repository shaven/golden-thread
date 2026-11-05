#!/usr/bin/env python3
"""gt_transcribe.py -- bring an audio or video file into Sources/ with a local transcript (0.22.0, phase 87).

    gt_transcribe.py FILE --vault V [--model PATH] [--max-bytes N] [--max-seconds N] [--timeout S] [--json]

The original is copied byte for byte into `<vault>/Sources/YYYY-MM-DD <name>` and hashed (the same
audited store as gt_ingest_binary.py). A local speech-to-text then writes a timestamped transcript
beside it, `... (transcript).md`, marked `derived: true` with the source hash, the tool and the
model's file name. Each line is `[HH:MM:SS] text`, so a finding can cite the file and the timestamp.

Local only. The tools are `whisper-cli` (whisper.cpp), and `ffmpeg` for anything whisper.cpp does not
read (video, m4a: it is converted to 16 kHz mono wav in a temp dir that is always removed). The model
is a file the owner names (`--model`, GT_TRANSCRIBE_MODEL, then the `transcribe_model` setting): gt downloads
nothing and installs nothing, and this module imports no network library. With no tool or no model the original is still
stored and the status is `no-transcriber` with the reason (exit 3). The transcript goes through
gt_intake_scan.py before it is written (`held-by-scan`, exit 4: kind and rule only).

Refused before anything is stored (exit 1): a file that is not audio or video (its type named), a
link, an unsafe name, over the size cap (2 GB) or, when ffprobe is on PATH, over the duration cap
(4 h). Nothing from the media or the transcript is ever printed or put in an error message.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gt_ingest_binary as gib                                   # noqa: E402

MAX_BYTES = 2 * 1024 * 1024 * 1024
MAX_SECONDS = 4 * 3600
TIMEOUT = 3600
WHISPER_READS = {"wav", "mp3", "flac", "ogg"}
MEDIA = ((b"fLaC", "flac"), (b"OggS", "ogg"), (b"ID3", "mp3"), (b"\xff\xfb", "mp3"), (b"\xff\xf3", "mp3"),
         (b"\xff\xf2", "mp3"), (b"\x1a\x45\xdf\xa3", "mkv"))
NO_MODEL = ("no speech model: set transcribe_model (gt_settings.py set transcribe_model PATH) to a whisper.cpp "
            "model file, or give --model or GT_TRANSCRIBE_MODEL. The recommended model is whisper.cpp small.en, "
            "which you download yourself (gt downloads nothing)")
STAMP = re.compile(r"^\[(\d+):(\d\d):(\d\d)(?:\.\d+)?\s*-->.*?\]\s*(.*\S)\s*$")


def detect(head):
    """-> (label, is_media). Media is recognised by its signature, not its extension."""
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav", True
    if head[4:8] == b"ftyp":
        return "mp4/m4a/mov", True
    for magic, label in MEDIA:
        if head.startswith(magic):
            return label, True
    kind, label = gib.detect(head)
    return label, False


def parse_whisper(out):
    """whisper-cli prints `[00:00:00.000 --> 00:00:04.000]   text`: -> ['[00:00:00] text', ...]."""
    lines = []
    for raw in out.splitlines():
        m = STAMP.match(raw.strip())
        if m:
            lines.append("[%02d:%s:%s] %s" % (int(m.group(1)), m.group(2), m.group(3), m.group(4)))
    return lines


def _probe(wanted):
    return shutil.which(wanted)


def _run(argv, timeout):
    return subprocess.run(argv, capture_output=True, timeout=timeout, shell=False, stdin=subprocess.DEVNULL)


def transcribe(path, label, model, timeout, max_seconds):
    """-> (lines, None) or (None, reason). Temp files live only inside this call."""
    whisper = _probe("whisper-cli")
    if not whisper:
        return None, "no local transcriber: whisper-cli (whisper.cpp) is not on PATH"
    if not model or not os.path.isfile(model):
        return None, NO_MODEL
    with tempfile.TemporaryDirectory(prefix="gt-transcribe-") as tmp:
        wav = path
        if label not in WHISPER_READS:
            ff = _probe("ffmpeg")
            if not ff:
                return None, "ffmpeg is not on PATH, and %s needs converting to wav first" % label
            wav = os.path.join(tmp, "in.wav")
            r = _run([ff, "-nostdin", "-v", "error", "-i", path, "-t", str(max_seconds), "-ar", "16000",
                      "-ac", "1", "-f", "wav", wav], timeout)
            if r.returncode != 0:
                raise gib.Refused("the conversion to wav failed")
        r = _run([whisper, "-m", model, "-f", wav], timeout)
        if r.returncode != 0:
            raise gib.Refused("the transcriber failed (exit %d)" % r.returncode)
        lines = parse_whisper(r.stdout.decode("utf-8", "replace"))
        if not lines:
            raise gib.Refused("the transcriber produced no timestamped text")
        return lines, None


def _duration(path):
    probe = _probe("ffprobe")
    if not probe:
        return None
    try:
        r = _run([probe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path], 60)
        return float(r.stdout.decode().strip()) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def run(path, vault, model, max_bytes=MAX_BYTES, max_seconds=MAX_SECONDS, timeout=TIMEOUT):
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            raise gib.Refused("not a regular file (a link or a missing file is refused)")
        name = gib.safe_name(os.path.basename(path))
        if name is None:
            raise gib.Refused("unsafe file name")
        size = os.path.getsize(path)
        if size > max_bytes:
            raise gib.Refused("size over the cap (%d > %d bytes)" % (size, max_bytes))
        with open(path, "rb") as fh:
            head = fh.read(16)
        label, is_media = detect(head)
        if not is_media:
            raise gib.Refused("not audio or video (type: %s)" % label)
        dur = _duration(path)
        if dur is not None and dur > max_seconds:
            raise gib.Refused("duration over the cap (%d > %d seconds)" % (dur, max_seconds))
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        sha = h.hexdigest()
        sources = os.path.join(vault, "Sources")
        os.makedirs(sources, exist_ok=True)
        with open(path, "rb") as fh:
            stored, new = _store_stream(sources, "%s %s" % (datetime.date.today().isoformat(), name), fh, sha)
        rep = {"status": "transcribed", "type": label, "source": "Sources/" + stored, "sha256": sha,
               "stored_new": new}
        lines, why = transcribe(path, label, model, timeout, max_seconds)
    except gib.Refused as exc:
        return 1, {"status": "refused", "reason": str(exc)}
    except subprocess.TimeoutExpired:
        return 1, {"status": "refused", "reason": "a tool ran past its time limit"}
    except OSError as exc:
        return 1, {"status": "refused", "reason": "could not read or store the file (%s)" % exc.__class__.__name__}
    if why:
        rep.update(status="no-transcriber", reason=why)
        return 3, rep
    text = "\n".join(lines)
    verdict, found = gib.intake_scan(text)
    if verdict != "clean":
        rep.update(status="held-by-scan", findings=found,
                   next="the transcript was not written: the intake scan %s" % (
                       "flagged it (kind and rule above)" if found else "could not complete"))
        return 4, rep
    md = "%s (transcript).md" % os.path.splitext(stored)[0]
    front = ("---\ntitle: %s\nderived: true\nsource_file: \"Sources/%s\"\nsource_sha256: %s\n"
             "extractor: whisper-cli\nmodel: %s\nextracted: %s\n---\n\n" % (
                 os.path.splitext(name)[0], stored, sha, os.path.basename(model),
                 datetime.date.today().isoformat()))
    try:
        gib._create_exclusive(os.path.join(sources, md), (front + text + "\n").encode("utf-8"))
    except FileExistsError:
        rep["derived_existed"] = True
    rep["derived"] = "Sources/" + md
    return 0, rep


def _store_stream(sources, name, fh, sha):
    """The same exclusive-create store as gt_ingest_binary.store, copying from a file object."""
    stem, ext = os.path.splitext(name)
    for cand in (name, "%s-%s%s" % (stem, sha[:8], ext)):
        dest = os.path.join(sources, cand)
        try:
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
        except FileExistsError:
            h = hashlib.sha256()
            with open(dest, "rb") as ex:
                for chunk in iter(lambda: ex.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() == sha:
                return cand, False
            continue
        fh.seek(0)
        with os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(fh, out)
        return cand, True
    raise gib.Refused("a different file already holds this name and hash")


def resolve_model(flag):
    """--model, then $GT_TRANSCRIBE_MODEL, then the transcribe_model setting; ~ expanded. None when none is set."""
    val = flag or os.environ.get("GT_TRANSCRIBE_MODEL")
    if not val:
        try:
            import gt_settings
            val = gt_settings.get("transcribe_model")
        except Exception:                                        # noqa: BLE001
            val = None
        if val in ("", "none"):
            val = None
    return os.path.expanduser(val) if val else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("file")
    ap.add_argument("--vault", required=True)
    ap.add_argument("--model", default=None,
                    help="a whisper.cpp model file (default: $GT_TRANSCRIBE_MODEL, then the transcribe_model setting)")
    ap.add_argument("--max-bytes", type=int, default=MAX_BYTES)
    ap.add_argument("--max-seconds", type=int, default=MAX_SECONDS)
    ap.add_argument("--timeout", type=int, default=TIMEOUT)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not os.path.isdir(a.vault):
        print("gt_transcribe: the vault %s does not exist" % a.vault, file=sys.stderr)
        return 2
    code, rep = run(a.file, a.vault, resolve_model(a.model), a.max_bytes, a.max_seconds, a.timeout)
    if a.json:
        print(json.dumps(rep, indent=2))
    else:
        print("%s%s" % (rep["status"], ": " + rep["reason"] if "reason" in rep else ""))
        for k in ("source", "derived", "next"):
            if k in rep:
                print("  %s: %s" % (k, rep[k]))
    return code


if __name__ == "__main__":
    sys.exit(main())
