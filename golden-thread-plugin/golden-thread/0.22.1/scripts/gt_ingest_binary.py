#!/usr/bin/env python3
"""gt_ingest_binary.py -- bring a PDF or an image into Sources/ (0.22.0, phase 86a).

    gt_ingest_binary.py FILE --vault V [--max-bytes N] [--max-pages N] [--json]
    gt_ingest.py --binary FILE --vault V ...                  (the same thing)

The original is copied byte for byte into `<vault>/Sources/YYYY-MM-DD <name>` (exclusive create,
never overwritten) and hashed. A PDF with a text layer is read by a small standard-library reader
(no dependency) and the text is written beside it as `... (text).md`, marked `derived: true` with
the source hash and the tool name. Nothing is guessed at:

  * a scanned PDF or an image is NOT read unless a local OCR is already on PATH (`tesseract`, and
    `pdftoppm` for a PDF); otherwise the original is kept and the status is `needs-ocr` with the
    exact next step. gt never installs a tool;
  * any other binary is refused with its type named, and stored nowhere;
  * a hostile file is refused before anything is stored: over the size cap (25 MB), over the page
    cap (200), a stream that inflates past its cap or at an absurd ratio (a decompression bomb),
    an encrypted PDF, a symlink, a file name carrying control characters or a path;
  * the extracted text goes through gt_intake_scan.py (its own process) before it is written; a
    finding holds the text back (`held-by-scan`) and only the rule id and kind are reported.

Nothing from the document is ever printed or put in an error message. Exit: 0 ingested | 1 refused |
2 usage | 3 needs OCR (original stored) | 4 held by the intake scan (original stored).
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
import unicodedata
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_BYTES = 25 * 1024 * 1024
MAX_PAGES = 200
MAX_STREAM = 128 * 1024 * 1024          # one inflated stream
MAX_TOTAL = 256 * 1024 * 1024           # every stream of the file
RATIO_FLOOR = 16 * 1024 * 1024          # a ratio check only above this many inflated bytes
MAX_RATIO = 500
MIN_TEXT_CHARS = 25                     # letters in words of 3+ that make a text layer real
TOOL_TIMEOUT = 120
IMAGE_MAGIC = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpeg"), (b"GIF87a", "gif"),
               (b"GIF89a", "gif"), (b"II*\x00", "tiff"), (b"MM\x00*", "tiff"), (b"BM", "bmp"))
OTHER_MAGIC = ((b"PK\x03\x04", "zip (docx, xlsx, pptx or an archive)"), (b"\xd0\xcf\x11\xe0", "ole (old Office)"),
               (b"\x7fELF", "elf program"), (b"MZ", "windows program"), (b"\xcf\xfa\xed\xfe", "mach-o program"),
               (b"\x1f\x8b", "gzip"), (b"Rar!", "rar archive"), (b"7z\xbc\xaf", "7z archive"),
               (b"RIFF", "riff (wav, avi or webp)"), (b"ID3", "mp3 audio"), (b"OggS", "ogg media"))


class Refused(Exception):
    """A refusal. The message names a reason, never any document content."""


def safe_name(name):
    """The file name to store under, or None when the name itself is hostile (control or format
    characters, '.' or '..', empty, a leading '-'). A path in it is cut to its last part and any
    character outside letters, digits, space and . _ ( ) - becomes '_'."""
    if not name or any(unicodedata.category(c)[0] == "C" for c in name):
        return None
    last = re.split(r"[\\/]", name)[-1]
    if last in ("", ".", "..") or last.startswith("-"):
        return None
    last = re.sub(r"[^A-Za-z0-9 ._()\-]", "_", last)
    return last[:150]


def detect(head):
    """-> ("pdf"|"image"|"other"|"text", label)."""
    if head.startswith(b"%PDF-"):
        return "pdf", "pdf"
    for magic, label in IMAGE_MAGIC:
        if head.startswith(magic):
            return "image", label
    for magic, label in OTHER_MAGIC:
        if head.startswith(magic):
            return "other", label
    return "text", "no binary signature"


_STREAM = re.compile(rb"<<((?:(?!>>\s*stream)[\s\S])*?)>>\s*stream\r?\n", re.S)
_PAGE = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")


def _inflate(raw, budget):
    d = zlib.decompressobj()
    out, total, buf = [], 0, raw
    while buf or d.unconsumed_tail:
        try:
            chunk = d.decompress(buf, 1 << 20)
        except zlib.error:
            break
        total += len(chunk)
        if total > MAX_STREAM or total > budget:
            raise Refused("decompressed size over the cap (a decompression bomb)")
        if total > RATIO_FLOOR and total / max(len(raw), 1) > MAX_RATIO:
            raise Refused("decompressed at an absurd ratio (a decompression bomb)")
        out.append(chunk)
        buf = d.unconsumed_tail
        if d.eof:
            break
    return b"".join(out), total


def _streams(data):
    """Yield (dict bytes, decoded stream bytes) for each stream, bounded."""
    spent = 0
    for m in _STREAM.finditer(data):
        end = data.find(b"endstream", m.end())
        if end < 0:
            continue
        raw = data[m.end():end].rstrip(b"\r\n")
        d = m.group(1)
        if b"FlateDecode" in d:
            body, n = _inflate(raw, MAX_TOTAL - spent)
            spent += n
        elif re.search(rb"/Filter", d):
            continue                      # a filter this reader does not handle (images, LZW)
        else:
            body = raw
        yield d, body


_STR = re.compile(rb"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>")


def _unescape(s):
    out, i = bytearray(), 0
    while i < len(s):
        c = s[i:i + 1]
        if c == b"\\" and i + 1 < len(s):
            n = s[i + 1:i + 2]
            if n in b"nrtbf":
                out += {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}[n]
                i += 2
            elif n.isdigit():
                j = i + 1
                while j < len(s) and j < i + 4 and s[j:j + 1].isdigit():
                    j += 1
                out.append(int(s[i + 1:j], 8) & 255)
                i = j
            else:
                out += n
                i += 2
        else:
            out += c
            i += 1
    return bytes(out)


def _pieces(block):
    """The text pieces of one BT..ET block, in order, with a newline at each line move."""
    out = []
    for m in re.finditer(rb"\[((?:\\.|[^\]\\])*)\]\s*TJ|(\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>)\s*(?:Tj|'|\")"
                         rb"|\b(T\*|Td|TD|Tm)\b", block):
        if m.group(3):
            if out and out[-1] != "\n":
                out.append("\n")
            continue
        parts = _STR.findall(m.group(1)) if m.group(1) is not None else [m.group(2)]
        for p in parts:
            if p.startswith(b"("):
                b = _unescape(p[1:-1])
            else:
                h = re.sub(rb"\s", b"", p[1:-1])
                b = bytes.fromhex((h + b"0" * (len(h) % 2)).decode("ascii")) if h else b""
            out.append(b.decode("cp1252", "replace"))
    return out


def pdf_text(data, max_pages=MAX_PAGES):
    """-> the text layer of a PDF, "" when it has none. Raises Refused on a hostile file."""
    if re.search(rb"/Encrypt\b", data):
        raise Refused("encrypted pdf")
    pages = len(_PAGE.findall(data))
    if pages > max_pages:
        raise Refused("pages over the cap (%d > %d)" % (pages, max_pages))
    lines = []
    for _d, body in _streams(data):
        if b"BT" not in body or b"ET" not in body:
            continue
        for block in re.findall(rb"\bBT\b([\s\S]*?)\bET\b", body):
            piece = "".join(_pieces(block)).strip()
            if piece:
                lines.append(piece)
    text = "\n".join(lines)
    text = "".join(c for c in text if c in "\n\t" or unicodedata.category(c)[0] != "C")
    return text if _readable(text) else ""


def _readable(text):
    """Real prose, not glyph codes: a font without a plain encoding (most subset fonts) gives one
    odd character per line, which must read as 'no text layer' rather than be stored as text."""
    words = re.findall(r"[A-Za-z]{3,}", text)
    solid = sum(len(w) for w in words)
    return len(words) >= 5 and solid >= MIN_TEXT_CHARS and solid >= 0.5 * len(re.sub(r"\s", "", text))


def _run(argv, path_env=None):
    return subprocess.run(argv, capture_output=True, timeout=TOOL_TIMEOUT, shell=False,
                          stdin=subprocess.DEVNULL, env=path_env)


def ocr(kind, path):
    """-> (text, tool) from a local pdftotext or tesseract (and pdftoppm for a scanned PDF) already on
    PATH, or None. Tools are found at run time; gt never installs one."""
    if kind == "pdf":
        ptt = shutil.which("pdftotext")        # a text layer the stdlib reader cannot decode
        if ptt:
            try:
                r = _run([ptt, "-l", str(MAX_PAGES), path, "-"])
                if r.returncode == 0 and _readable(r.stdout.decode("utf-8", "replace")):
                    return r.stdout.decode("utf-8", "replace"), "pdftotext"
            except (OSError, subprocess.SubprocessError):
                pass
    tess = shutil.which("tesseract")
    if not tess:
        return None
    try:
        if kind == "image":
            r = _run([tess, path, "stdout"])
            return (r.stdout.decode("utf-8", "replace"), "tesseract") if r.returncode == 0 else None
        ppm = shutil.which("pdftoppm")
        if not ppm:
            return None
        with tempfile.TemporaryDirectory(prefix="gt-ocr-") as tmp:
            r = _run([ppm, "-r", "200", "-l", str(MAX_PAGES), "-png", path, os.path.join(tmp, "p")])
            if r.returncode != 0:
                return None
            texts = []
            for f in sorted(os.listdir(tmp)):
                t = _run([tess, os.path.join(tmp, f), "stdout"])
                if t.returncode != 0:
                    return None
                texts.append(t.stdout.decode("utf-8", "replace"))
            return "\n".join(texts), "tesseract"
    except (OSError, subprocess.SubprocessError):
        return None


def intake_scan(text):
    """-> (verdict, [{"kind", "rule"} ...]); the text is scanned in a scratch dir outside the vault."""
    with tempfile.TemporaryDirectory(prefix="gt-binscan-") as tmp:
        with open(os.path.join(tmp, "extracted.md"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        try:
            r = subprocess.run([sys.executable, os.path.join(HERE, "gt_intake_scan.py"), tmp, "--json"],
                               capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
            rep = json.loads(r.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            return "incomplete", []
    found = []
    for unit in rep.get("units", []):
        for f in unit.get("findings", []):
            found.append({"kind": f.get("kind"), "rule": f.get("rule")})
    if r.returncode == 0:
        return "clean", []
    return ("findings" if r.returncode == 1 or found else "incomplete"), found


def _create_exclusive(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def store(sources, name, data, sha):
    """Write the original once. -> (stored file name, newly written). Same name and same bytes is
    already stored; the same name with other bytes gets the hash in its name."""
    for cand in (name, "%s-%s%s" % (os.path.splitext(name)[0], sha[:8], os.path.splitext(name)[1])):
        dest = os.path.join(sources, cand)
        try:
            _create_exclusive(dest, data)
            return cand, True
        except FileExistsError:
            with open(dest, "rb") as fh:
                if hashlib.sha256(fh.read()).hexdigest() == sha:
                    return cand, False
    raise Refused("a different file already holds this name and hash")


def ingest(path, vault, max_bytes=MAX_BYTES, max_pages=MAX_PAGES):
    """-> (exit code, report dict)."""
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            raise Refused("not a regular file (a link or a missing file is refused)")
        name = safe_name(os.path.basename(path))
        if name is None:
            raise Refused("unsafe file name")
        size = os.path.getsize(path)
        if size > max_bytes:
            raise Refused("size over the cap (%d > %d bytes)" % (size, max_bytes))
        with open(path, "rb") as fh:
            data = fh.read(max_bytes + 1)
        kind, label = detect(data[:16])
        if kind in ("other", "text"):
            raise Refused("not a pdf or an image (type: %s)" % label)
        sha = hashlib.sha256(data).hexdigest()
        text, tool = "", None
        if kind == "pdf":
            text = pdf_text(data, max_pages)
            tool = "stdlib-pdf-text"
        sources = os.path.join(vault, "Sources")
        os.makedirs(sources, exist_ok=True)
        if not text:
            got = ocr(kind, path)
            if got and got[0].strip():
                text, tool = got
        stored, new = store(sources, "%s %s" % (datetime.date.today().isoformat(), name), data, sha)
    except Refused as exc:
        return 1, {"status": "refused", "reason": str(exc)}
    except OSError as exc:
        return 1, {"status": "refused", "reason": "could not read or store the file (%s)" % exc.__class__.__name__}
    rep = {"status": "ingested", "kind": kind, "type": label, "source": "Sources/" + stored,
           "sha256": sha, "stored_new": new}
    if not text.strip():
        rep.update(status="needs-ocr", next=(
            "the file has no text layer: install tesseract%s yourself (gt never installs a tool), or read it "
            "by eye, then run this again" % (" and pdftoppm (poppler)" if kind == "pdf" else "")))
        return 3, rep
    verdict, found = intake_scan(text)
    if verdict != "clean":
        rep.update(status="held-by-scan", findings=found,
                   next="the extracted text was not written: the intake scan %s" % (
                       "flagged it (kind and rule above)" if found else "could not complete"))
        return 4, rep
    md_name = "%s (text).md" % os.path.splitext(stored)[0]
    front = ("---\ntitle: %s\nderived: true\nsource_file: \"Sources/%s\"\nsource_sha256: %s\n"
             "extractor: %s\nextracted: %s\n---\n\n" % (
                 os.path.splitext(name)[0], stored, sha, tool, datetime.date.today().isoformat()))
    try:
        _create_exclusive(os.path.join(sources, md_name), (front + text.strip() + "\n").encode("utf-8"))
        rep["derived"] = "Sources/" + md_name
    except FileExistsError:
        rep["derived"] = "Sources/" + md_name
        rep["derived_existed"] = True
    rep["extractor"] = tool
    return 0, rep


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("file")
    ap.add_argument("--vault", required=True)
    ap.add_argument("--max-bytes", type=int, default=MAX_BYTES)
    ap.add_argument("--max-pages", type=int, default=MAX_PAGES)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not os.path.isdir(a.vault):
        print("gt_ingest_binary: the vault %s does not exist" % a.vault, file=sys.stderr)
        return 2
    code, rep = ingest(a.file, a.vault, a.max_bytes, a.max_pages)
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
