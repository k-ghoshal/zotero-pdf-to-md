#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pymupdf4llm<2", "pdfplumber"]
# ///
"""Convert PDFs to Markdown for use as LLM reference material.

From the Zotero plugin:
    python pdf_to_md.py --out <output folder> --jobs <jobs.json>
By hand:
    uv run pdf_to_md.py --out ~/LLM-reference ~/Downloads/papers/*.pdf

For each PDF:
  1. Convert with pymupdf4llm (reading an existing OCR layer first, if any).
  2. Score the text; if it looks like junk, also try pdfplumber, then OCR of
     the rendered pages (2-up sheets split), and keep the best result.
  3. Write to <out>/<name>.md, or to <out>/_needs_review/<name>.md if still junk.
PDFs are never moved or modified. OCR needs Tesseract.

stdout is a short summary; per-file detail goes to stderr.
"""
from __future__ import annotations

import argparse
import contextlib
import functools
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# pymupdf4llm and pdfplumber are imported where used, so either can convert
# if the other is missing or broken.
import pymupdf

# Start of the lines --progress writes to stderr; pdf-to-md.js looks for it.
PROGRESS_PREFIX = "@pdf-to-md-progress"

# ---- Settings ---------------------------------------------------------------
# Subfolder of the output folder for outputs that failed the quality check.
REVIEW_SUBDIR = "_needs_review"
# Overwrite existing .md files? False = skip PDFs already done. --[no-]overwrite.
OVERWRITE = False
# Quality threshold (0-1). Raise it if junk slips through, lower it if good
# papers keep landing in _needs_review.
MIN_SCORE = 0.6
# Remove running headers/footers that repeat on most pages. --[no-]strip-headers.
STRIP_HEADERS = True
# Put a marker such as "[PDF page 5 · printed p. 287]" at the start of every
# page. --[no-]page-markers.
PAGE_MARKERS = True
# Tesseract language(s) for OCR, e.g. "eng+deu". --ocr-language.
OCR_LANGUAGE = "eng"
# Tables as HTML instead of Markdown (keeps merged cells). --[no-]html-tables.
HTML_TABLES = False
# Repair fonts whose symbols copy out as the wrong characters (Wingdings ☞
# as "F", → as "à"). --[no-]repair-symbols.
REPAIR_SYMBOLS = True
# Resolution for the last-resort OCR of rendered pages (with_rendered_ocr).
RENDER_DPI = 300
# -----------------------------------------------------------------------------


@dataclass
class Options:
    strip_headers: bool = STRIP_HEADERS
    page_markers: bool = PAGE_MARKERS
    ocr_language: str = OCR_LANGUAGE
    html_tables: bool = HTML_TABLES
    repair_symbols: bool = REPAIR_SYMBOLS


@dataclass
class Quality:
    score: float  # 0 (junk) .. 1 (clean prose)
    reason: str   # short explanation of the main problem, "" if fine


def assess(text: str, n_pages: int) -> Quality:
    """Heuristic text-quality score. IPA and other non-ASCII letters count as
    letters (str.isalpha), so phonetic transcription isn't penalised."""
    body = re.sub(r"[#*_`>|\-\[\]()!]", " ", text)  # drop Markdown syntax
    stripped = re.sub(r"\s", "", body)
    n = len(stripped)
    per_page = n / max(n_pages, 1)

    if per_page < 200:
        return Quality(0.0, f"almost no text ({per_page:.0f} chars/page) - likely a scan, needs OCR")

    cid = len(re.findall(r"\(cid:\d+\)", text))
    bad = sum(
        1 for c in stripped
        if c == "�" or unicodedata.category(c) in ("Co", "Cn", "Cc")
    )
    alpha = sum(c.isalpha() for c in stripped) / n

    tokens = re.findall(r"\S+", body)
    words = [t.strip(".,;:!?\"'“”‘’") for t in tokens]
    words = [w for w in words if w]
    wordlike = sum(
        1 for w in words
        if w.isalpha() and len(w) <= 25 and re.search(r"[aeiouyAEIOUYɐ-ʯ]", w)
    ) / max(len(words), 1)
    # Text extracted without spaces gives very long "words".
    avg_len = sum(map(len, words)) / max(len(words), 1)

    score = min(alpha / 0.75, 1.0) * 0.4 + min(wordlike / 0.6, 1.0) * 0.6
    reason = ""
    if cid > 20 or bad / n > 0.02:
        score *= 0.3
        reason = "broken font encoding (cid / unknown glyphs)"
    elif avg_len > 12:
        score *= 0.5
        reason = "words run together (missing spaces)"
    elif score < MIN_SCORE:
        reason = "too few real words"
    return Quality(round(score, 2), reason)


# Each converter returns (PDF page number, Markdown/text) for every page, in
# order. PDF page numbers count from 1.

def ensure_tesseract_on_path() -> bool:
    """Add Tesseract's folder to PATH if needed (apps started from the Dock
    get a minimal PATH); return False if it isn't installed."""
    if shutil.which("tesseract"):
        return True
    for d in ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin",
              r"C:\Program Files\Tesseract-OCR"):
        if shutil.which("tesseract", path=d):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            return True
    return False


@contextlib.contextmanager
def fixed_copy(pdf: Path, opts: Options):
    """Yield `pdf` itself, or a temporary copy with these fixes:
    - stamps removed from scanned pages (`remove_scan_stamps`)
    - Wingdings fonts mapped to real symbols, with --repair-symbols. Word,
      for example, maps ☞ to "F" + U+F046 and → to "à" + U+F0E0.
    The original PDF is never modified."""
    fixes = [("remove stamps from scanned pages", remove_scan_stamps,
              "removed stamp text from {} scanned page(s), so OCR text is kept")]
    if opts.repair_symbols:
        fixes.append(("check symbol fonts", add_wingdings_unicode_maps,
                      "added Unicode map to {} Wingdings font(s)"))
        fixes.append(("check fonts without a Unicode map", add_glyph_unicode_maps,
                      "rebuilt the Unicode map of {} font(s) from the installed fonts"))
    with pymupdf.open(pdf) as doc:
        changed = False
        for what, fix, done in fixes:
            try:
                n = fix(doc)
            except Exception as e:  # e.g. encrypted; skip this fix
                print(f"         {pdf.name}: could not {what}: {e}", file=sys.stderr)
                continue
            if n:
                print(f"         {pdf.name}: {done.format(n)}", file=sys.stderr)
                changed = True
        if not changed:
            yield pdf
            return
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / pdf.name
            doc.save(copy)
            yield copy


def remove_scan_stamps(doc: "pymupdf.Document", min_image: float = 0.9,
                       max_text: float = 0.05) -> int:
    """Remove the visible text from scanned pages that have no OCR layer yet,
    and return the number of pages changed. Such text is a download stamp
    ("Downloaded from academic.oup.com …", "Reproduced with permission …").
    pymupdf4llm OCRs these pages, but then keeps the OCR text only if *all*
    text on the page is invisible, so the stamp makes it drop the whole page.

    A page counts as scanned if one image covers at least `min_image` of it
    and visible text covers less than `max_text`. Only done if at least half
    the pages count, so a born-digital PDF with a few full-page figures is
    left alone. Pages with invisible text (an existing OCR layer) are skipped:
    the ocr-layer method reads those."""
    pages = []
    for page in doc:
        area = abs(page.rect)
        image = max((abs(pymupdf.Rect(i["bbox"]) & page.rect)
                     for i in page.get_image_info()), default=0)
        trace = page.get_texttrace()
        visible = [s for s in trace if s["type"] != 3 and s["opacity"] > 0]
        if not visible or len(visible) < len(trace) or image < min_image * area:
            continue
        if sum(abs(pymupdf.Rect(s["bbox"]) & page.rect) for s in visible) >= max_text * area:
            continue
        pages.append((page, visible))
    if len(pages) < doc.page_count / 2:
        return 0
    for page, visible in pages:
        for s in visible:
            page.add_redact_annot(s["bbox"], fill=False)
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    return len(pages)


def add_wingdings_unicode_maps(doc: "pymupdf.Document") -> int:
    """Give each simple Wingdings font a ToUnicode map with real symbols:
    an existing map is repaired entry by entry, a missing one is added.
    Returns the number of fonts changed."""
    seen, fixed = set(), 0
    for pno in range(doc.page_count):
        for xref, _ext, _type, basefont, _name, _enc in doc.get_page_fonts(pno):
            if xref in seen:
                continue
            seen.add(xref)
            base = basefont.split("+")[-1].lower()  # drop subset prefix "ABCDEF+"
            if base not in ("wingdings", "wingdings-regular"):
                continue
            if doc.xref_get_key(xref, "Subtype")[1] not in ("/TrueType", "/Type1"):
                continue  # 2-byte (Type0) fonts would need a different map
            kind, value = doc.xref_get_key(xref, "ToUnicode")
            if kind == "xref":
                old = _parse_to_unicode(doc.xref_stream(int(value.split()[0])).decode("latin-1"))
                mapping = {code: _wingdings_char(text) for code, text in old.items()}
                if mapping == old:
                    continue  # already real symbols
            else:
                mapping = WINGDINGS_TO_UNICODE
            cmap_xref = doc.get_new_xref()
            doc.update_object(cmap_xref, "<<>>")
            doc.update_stream(cmap_xref, _to_unicode_cmap(mapping).encode())
            doc.xref_set_key(xref, "ToUnicode", f"{cmap_xref} 0 R")
            fixed += 1
    return fixed


def add_glyph_unicode_maps(doc: "pymupdf.Document", min_match: float = 0.9) -> int:
    """Give two-byte TrueType fonts whose glyphs have no Unicode a ToUnicode
    map taken from the same font installed on this computer, and return the
    number of fonts changed. Mac OS X's Quartz writes such fonts (Identity-H,
    an empty ToUnicode map, an embedded subset without a cmap table), so text
    comes out as glyph numbers: Arial's "b" is glyph 69, which reads as "E".

    Glyph numbers differ between versions of a font, so a glyph is mapped only
    if its width in the installed font matches the width the PDF gives it, and
    a font only if at least `min_match` of its glyphs match."""
    seen, fixed = set(), 0
    for pno in range(doc.page_count):
        for xref, _ext, _type, basefont, _name, _enc in doc.get_page_fonts(pno):
            if xref in seen:
                continue
            seen.add(xref)
            if (doc.xref_get_key(xref, "Subtype")[1] != "/Type0"
                    or doc.xref_get_key(xref, "Encoding")[1] != "/Identity-H"):
                continue
            kind, value = doc.xref_get_key(xref, "ToUnicode")
            if kind == "xref" and _parse_to_unicode(
                    doc.xref_stream(int(value.split()[0])).decode("latin-1")):
                continue  # has a real map
            refs = re.findall(r"(\d+) 0 R", doc.xref_get_key(xref, "DescendantFonts")[1])
            if not refs:
                continue
            cid_font = int(refs[0])
            descriptor = doc.xref_get_key(cid_font, "FontDescriptor")
            file_ref = (doc.xref_get_key(int(descriptor[1].split()[0]), "FontFile2")
                        if descriptor[0] == "xref" else ("null", ""))
            if file_ref[0] != "xref" or b"cmap" in _font_tables(
                    doc.xref_stream(int(file_ref[1].split()[0]))):
                continue  # not embedded TrueType, or the font itself knows its characters
            name = basefont.split("+")[-1]
            installed = system_fonts().get(name)
            if not installed:
                print(f"         font {name} has no Unicode map and isn't installed here,"
                      " so its text can't be repaired", file=sys.stderr)
                continue
            widths = _cid_widths(doc, cid_font)
            mapping = _glyph_map(pymupdf.Font(fontfile=str(installed)), widths, min_match)
            if mapping is None:
                print(f"         font {name}: the installed version doesn't match the PDF's",
                      file=sys.stderr)
                continue
            # Unmatched glyphs would otherwise read as chr(glyph number).
            mapping = {glyph: mapping.get(glyph, "\ufffd") for glyph in widths}
            cmap_xref = doc.get_new_xref()
            doc.update_object(cmap_xref, "<<>>")
            doc.update_stream(cmap_xref, _to_unicode_cmap(mapping, code_bytes=2).encode())
            doc.xref_set_key(xref, "ToUnicode", f"{cmap_xref} 0 R")
            fixed += 1
    return fixed


def _glyph_map(font: "pymupdf.Font", widths: dict[int, float],
               min_match: float = 0.9) -> dict[int, str] | None:
    """Glyph number -> character, from an installed font, for the glyphs whose
    width there matches `widths` (from the PDF); None if too few match."""
    glyph_char: dict[int, int] = {}
    for cp in font.valid_codepoints():  # ascending, so space wins over no-break space
        glyph_char.setdefault(font.has_glyph(cp), cp)
    mapping = {}
    for glyph, width in widths.items():
        cp = glyph_char.get(glyph)
        if cp and abs(font.glyph_advance(cp) * 1000 - width) <= 1:
            mapping[glyph] = chr(cp)
    if not widths or len(mapping) < min_match * len(widths):
        return None
    return mapping


def _cid_widths(doc: "pymupdf.Document", cid_font: int) -> dict[int, float]:
    """The /W array of a CID font: glyph number -> width (1/1000 em)."""
    kind, value = doc.xref_get_key(cid_font, "W")
    if kind == "xref":
        value = doc.xref_object(int(value.split()[0]))
    tokens = re.findall(r"\[|\]|-?[\d.]+", value)[1:-1]  # inside the outer [ ]
    widths, i = {}, 0
    while i < len(tokens):
        first = int(float(tokens[i]))
        if tokens[i + 1] == "[":  # c [w1 w2 ...]
            j = i + 2
            while tokens[j] != "]":
                widths[first + j - i - 2] = float(tokens[j])
                j += 1
            i = j + 1
        else:  # c_first c_last w
            for glyph in range(first, int(float(tokens[i + 1])) + 1):
                widths[glyph] = float(tokens[i + 2])
            i += 3
    return widths


def _font_tables(data: bytes) -> set[bytes]:
    """The table tags of a TrueType/OpenType font file."""
    try:
        n = struct.unpack(">H", data[4:6])[0]
        return {data[12 + 16 * i:16 + 16 * i] for i in range(n)}
    except struct.error:
        return set()


FONT_DIRS = ("/System/Library/Fonts", "/Library/Fonts", "~/Library/Fonts",
             r"C:\Windows\Fonts", "~/AppData/Local/Microsoft/Windows/Fonts",
             "/usr/share/fonts", "/usr/local/share/fonts", "~/.local/share/fonts", "~/.fonts")


@functools.cache
def system_fonts() -> dict[str, Path]:
    """PostScript name -> file, for the .ttf/.otf fonts installed here."""
    found: dict[str, Path] = {}
    for d in FONT_DIRS:
        root = Path(d).expanduser()
        if not root.is_dir():
            continue
        for f in root.rglob("*"):
            if f.suffix.lower() in (".ttf", ".otf"):
                name = _postscript_name(f)
                if name:
                    found.setdefault(name, f)
    return found


def _postscript_name(path: Path) -> str | None:
    """Name ID 6 from a font file's name table."""
    try:
        with open(path, "rb") as fh:
            n = struct.unpack(">H", fh.read(12)[4:6])[0]
            directory = fh.read(16 * n)
            for i in range(n):
                tag, _sum, offset, length = struct.unpack(">4sIII", directory[16 * i:16 * i + 16])
                if tag == b"name":
                    fh.seek(offset)
                    table = fh.read(length)
                    break
            else:
                return None
        _fmt, count, strings = struct.unpack(">HHH", table[:6])
        for i in range(count):
            platform, _enc, _lang, name_id, length, offset = struct.unpack(
                ">6H", table[6 + 12 * i:18 + 12 * i])
            if name_id == 6:
                raw = table[strings + offset:strings + offset + length]
                return raw.decode("utf-16-be" if platform in (0, 3) else "latin-1").strip("\0")
    except (OSError, struct.error, UnicodeDecodeError):
        return None
    return None


def _wingdings_char(text: str) -> str:
    """The real symbol for what a broken map says a Wingdings glyph is.
    A private-use character U+F0xx carries the Wingdings code xx; otherwise
    a single character up to U+00FF is taken as the code itself."""
    pua = [c for c in text if 0xF000 <= ord(c) <= 0xF0FF]
    if pua:
        code = ord(pua[0]) - 0xF000
    elif len(text) == 1 and ord(text) <= 0xFF:
        code = ord(text)
    else:
        return text
    return WINGDINGS_TO_UNICODE.get(code, text)


def _parse_to_unicode(cmap: str) -> dict[int, str]:
    """Read the bfchar/bfrange entries of a ToUnicode CMap: code -> text."""
    def text(hexstr: str) -> str:
        return bytes.fromhex(hexstr).decode("utf-16-be", errors="replace")

    mapping: dict[int, str] = {}
    for block in re.findall(r"beginbfchar(.*?)endbfchar", cmap, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f\s]*)>", block):
            mapping[int(src, 16)] = text(dst.replace(" ", ""))
    for block in re.findall(r"beginbfrange(.*?)endbfrange", cmap, re.S):
        for lo, hi, rest in re.findall(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f\s]*>|\[[^\]]*\])", block
        ):
            lo, hi = int(lo, 16), int(hi, 16)
            if rest.startswith("["):
                for i, dst in enumerate(re.findall(r"<([0-9A-Fa-f\s]*)>", rest)):
                    mapping[lo + i] = text(dst.replace(" ", ""))
            else:
                start = bytes.fromhex(rest.strip("<>").replace(" ", ""))
                base = int.from_bytes(start, "big")
                for i in range(hi - lo + 1):
                    mapping[lo + i] = text((base + i).to_bytes(len(start), "big").hex())
    return mapping


def _to_unicode_cmap(mapping: dict[int, str], code_bytes: int = 1) -> str:
    """A ToUnicode CMap: code (one or two bytes) -> Unicode (UTF-16BE)."""
    def utf16(ch: str) -> str:
        return ch.encode("utf-16-be").hex().upper()
    hex_width = 2 * code_bytes
    entries = [f"<{code:0{hex_width}X}> <{utf16(ch)}>" for code, ch in sorted(mapping.items())]
    chunks = [entries[i:i + 100] for i in range(0, len(entries), 100)]  # max 100 per block
    body = "".join(
        f"{len(c)} beginbfchar\n" + "\n".join(c) + "\nendbfchar\n" for c in chunks
    )
    return (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
        f"1 begincodespacerange\n<{0:0{hex_width}X}> <{256 ** code_bytes - 1:0{hex_width}X}>\nendcodespacerange\n"
        f"{body}endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
    )


def check_password(pdf: Path) -> None:
    with pymupdf.open(pdf) as doc:
        if doc.needs_pass:
            raise RuntimeError("password-protected")


def _numbered(chunks: list[dict]) -> list[tuple[int, str]]:
    """pymupdf4llm page chunks -> (PDF page number, text). The layout mode
    calls the key "page_number", the simpler mode "page"."""
    out = []
    for i, c in enumerate(chunks):
        meta = c.get("metadata") or {}
        out.append((meta.get("page_number") or meta.get("page") or i + 1, c["text"]))
    return out


def with_pymupdf4llm(pdf: Path, opts: Options, **ocr) -> list[tuple[int, str]]:
    """`ocr`: extra OCR options for pymupdf4llm (force_ocr, ocr_dpi)."""
    import pymupdf4llm

    check_password(pdf)
    kwargs = {"page_chunks": True, "show_progress": False, "ocr_language": opts.ocr_language, **ocr}
    if opts.html_tables:
        kwargs["table_output"] = "html"
    chunks = pymupdf4llm.to_markdown(str(pdf), **kwargs)
    with pymupdf.open(pdf) as doc:
        for chunk, (n, _) in zip(chunks, _numbered(chunks)):
            chunk["text"] = restore_formula_text(doc[n - 1], chunk)
    return _numbered(chunks)


def restore_formula_text(page: "pymupdf.Page", chunk: dict) -> str:
    """Insert the text of "formula" regions, which pymupdf4llm leaves out.
    The label often lands on numbered examples and small tables. Each
    region's words go in a ```text block that keeps lines and columns."""
    text = chunk["text"]
    boxes = [b for b in chunk.get("page_boxes") or [] if b.get("class") == "formula"]
    # Work from the end of the page so earlier positions stay valid.
    for box in sorted(boxes, key=lambda b: b["pos"][0], reverse=True):
        block = region_text(page, pymupdf.Rect(box["bbox"]))
        if not block:
            continue
        start, end = box["pos"]
        text = (text[:start].rstrip("\n") + f"\n\n```text\n{block}\n```\n\n"
                + text[end:].lstrip("\n"))
    return text


def region_text(page: "pymupdf.Page", rect: "pymupdf.Rect") -> str:
    """The words inside `rect`, line by line, with gaps between words turned
    into spaces so columns roughly line up in a monospaced block."""
    words = page.get_text("words", clip=rect, sort=True)  # x0, y0, x1, y1, word, ...
    if not words:
        return ""
    lines: list[list[tuple]] = []
    for w in sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        mid, height = (w[1] + w[3]) / 2, w[3] - w[1]
        if lines and abs(mid - lines[-1][0][5]) <= height / 2:
            lines[-1].append((*w[:5], lines[-1][0][5]))
        else:
            lines.append([(*w[:5], mid)])
    char_w = sum((w[2] - w[0]) / max(len(w[4]), 1) for w in words) / len(words)
    out = []
    for line in lines:
        line.sort(key=lambda w: w[0])
        s = ""
        for w in line:
            col = round((w[0] - rect.x0) / char_w)
            s += " " * max(1 if s else 0, col - len(s)) + w[4]
        out.append(s.rstrip())
    # Drop the common left indent.
    indent = min(len(l) - len(l.lstrip()) for l in out if l.strip())
    return "\n".join(l[indent:] for l in out)


def with_pymupdf4llm_ocr_layer(pdf: Path, opts: Options) -> list[tuple[int, str]]:
    """pymupdf4llm's non-layout mode, keeping invisible (OCR layer) text.
    The default mode drops that layer on pages that also have visible text,
    such as a publisher stamp. ignore_alpha=True means "keep invisible text"."""
    from pymupdf4llm.helpers import pymupdf_rag

    check_password(pdf)
    kwargs = {"ignore_alpha": True, "page_chunks": True, "show_progress": False}
    if opts.html_tables:
        kwargs["table_output"] = "html"
    return _numbered(pymupdf_rag.to_markdown(str(pdf), **kwargs))


def with_rendered_ocr(pdf: Path, opts: Options) -> list[tuple[int, str, str]]:
    """Last resort: render every page to an image and OCR that, ignoring the
    text layer. For text layers that can't be read, e.g. Type 3 bitmap fonts
    without a Unicode map (old TeX via dvips/Ghostscript). Sheets with two
    pages side by side (`is_two_up`) are split, so each page is OCRed and
    numbered on its own. Returns (PDF page, text, "left"/"right"/"") triples."""
    if not shutil.which("tesseract"):
        raise RuntimeError("Tesseract not found")
    check_password(pdf)
    with pymupdf.open(pdf) as doc, tempfile.TemporaryDirectory() as tmp:
        split = is_two_up(doc)
        images, where = pymupdf.open(), []
        for page in doc:
            r = page.rect
            halves = ([("left", r.x0, r.x0 + r.width / 2), ("right", r.x0 + r.width / 2, r.x1)]
                      if split else [("", r.x0, r.x1)])
            for half, x0, x1 in halves:
                clip = pymupdf.Rect(x0, r.y0, x1, r.y1)
                pix = page.get_pixmap(dpi=RENDER_DPI, clip=clip, colorspace=pymupdf.csGRAY)
                new = images.new_page(width=clip.width, height=clip.height)
                new.insert_image(new.rect, pixmap=pix)
                where.append((page.number + 1, half))
        rendered = Path(tmp) / "rendered.pdf"
        images.save(rendered, deflate=True)
        images.close()
        pages = with_pymupdf4llm(rendered, opts, force_ocr=True, ocr_dpi=RENDER_DPI)
    return [(n, text, half) for (n, half), (_, text) in zip(where, pages)]


def is_two_up(doc: "pymupdf.Document", min_share: float = 0.8) -> bool:
    """True if the sheets hold two pages side by side: landscape, with words
    on both sides of the middle and none across it, on at least `min_share`
    of the sheets that have text. Needs a text layer, even a garbled one."""
    with_text = two_up = 0
    for page in doc:
        r = page.rect
        if r.width < 1.2 * r.height:
            return False
        words = page.get_text("words")
        if not words:
            continue
        with_text += 1
        mid, gap = (r.x0 + r.x1) / 2, 0.02 * r.width
        if (any(w[2] < mid for w in words) and any(w[0] > mid for w in words)
                and not any(w[0] < mid + gap and w[2] > mid - gap for w in words)):
            two_up += 1
    return with_text > 0 and two_up >= min_share * with_text


def has_ocr_layer(pdf: Path, max_pages: int = 5) -> bool:
    """True if the first pages contain invisible text (PDF render mode 3)."""
    try:
        with pymupdf.open(pdf) as doc:
            for page in doc.pages(0, min(max_pages, doc.page_count)):
                if any(span["type"] == 3 for span in page.get_texttrace()):
                    return True
    except Exception:
        pass
    return False


def with_pdfplumber(pdf: Path, opts: Options) -> list[tuple[int, str]]:
    import pdfplumber

    with pdfplumber.open(pdf) as doc:
        return [
            (i + 1, p.extract_text(x_tolerance=1.2, y_tolerance=3) or "")
            for i, p in enumerate(doc.pages)
        ]


_PAGE_NUMBER_LINE = re.compile(r"^[\W_]*(\d{1,4})[\W_]*$")


def printed_page_numbers(pdf: Path, pages: list[tuple[int, str]], edge: int = 3,
                         labels: bool = True) -> dict[int, str]:
    """Printed page numbers by PDF page number, where they can be found.

    1. The PDF's page labels, if it has them.
    2. Otherwise lines that are just a number among the first/last `edge`
       lines of a page, if they share the most common offset from the PDF
       page number on at least 3 pages and 30% of pages.
    Pages between two found pages are filled in; pages before the first or
    after the last are left out. Run before header/footer stripping.
    """
    printed: dict[int, str] = {}
    try:
        with pymupdf.open(pdf) if labels else contextlib.nullcontext([]) as doc:
            for n, _ in pages:
                label = doc[n - 1].get_label() if 0 < n <= len(doc) else ""
                if label and label != str(n):
                    printed[n] = label
    except Exception:
        pass
    if printed:
        return printed

    candidates = []
    for n, text in pages:
        lines = [l for l in text.splitlines() if l.strip()]
        for line in lines[:edge] + lines[-edge:]:
            line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
            m = _PAGE_NUMBER_LINE.match(line.strip())
            if m:
                candidates.append((n, int(m.group(1))))
    offsets: dict[int, int] = {}
    for n, num in candidates:
        offsets[num - n] = offsets.get(num - n, 0) + 1
    if not offsets:
        return {}
    offset, count = max(offsets.items(), key=lambda kv: kv[1])
    if count < max(3, 0.3 * len(pages)):
        return {}
    found = {n: num for n, num in candidates if num - n == offset}
    # Fill in pages between found pages.
    known = sorted(found)
    for before, after in zip(known, known[1:]):
        for n in range(before + 1, after):
            found[n] = n + offset
    return {n: str(num) for n, num in found.items()}


def page_marker(n: int, printed: str | None, half: str = "") -> str:
    where = f"PDF page {n}, {half} half" if half else f"PDF page {n}"
    if printed and (half or printed != str(n)):
        return f"[{where} · printed p. {printed}]"
    return f"[{where}]"


MAX_EDGE_LINE = 200  # longest line strip_repeated_edges will treat as a header/footer


def _norm_edge_line(line: str) -> str:
    """Normalise a header/footer line so repeats match across pages."""
    line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)  # [text](url) -> text
    line = re.sub(r"[#*_`>]", "", line)                     # Markdown emphasis/headings
    line = re.sub(r"\d+", "#", line)                        # page numbers differ per page
    return re.sub(r"\s+", " ", line).strip().lower()


def strip_repeated_edges(pages: list[str], edge: int = 3, min_share: float = 0.4) -> tuple[list[str], int]:
    """Remove lines that repeat among the first/last `edge` non-blank lines of
    at least `min_share` of the pages (and at least 3): running heads, page
    numbers, publisher stamps. Below 0.5 so odd/even running heads count.
    The top of page 1 and lines over MAX_EDGE_LINE characters are kept.
    Returns (pages, number of lines removed)."""
    if len(pages) < 3:
        return pages, 0

    def edge_indices(lines: list[str], first: bool) -> list[int]:
        nonblank = [i for i, l in enumerate(lines) if l.strip()]
        top = [] if first else nonblank[:edge]
        return sorted(set(top + nonblank[-edge:]))

    def edge_key(line: str) -> str:
        # Code fences (from restore_formula_text) must stay paired.
        if line.lstrip().startswith("```") or len(line.strip()) > MAX_EDGE_LINE:
            return ""
        return _norm_edge_line(line)

    split = [p.splitlines() for p in pages]
    counts: dict[str, int] = {}
    for pno, lines in enumerate(split):
        seen = {edge_key(lines[i]) for i in edge_indices(lines, pno == 0)}
        for key in seen - {""}:
            counts[key] = counts.get(key, 0) + 1
    needed = max(3, min_share * len(pages))
    repeated = {k for k, c in counts.items() if c >= needed}
    if not repeated:
        return pages, 0

    removed = 0
    out = []
    for pno, lines in enumerate(split):
        drop = {i for i in edge_indices(lines, pno == 0) if edge_key(lines[i]) in repeated}
        removed += len(drop)
        out.append("\n".join(l for i, l in enumerate(lines) if i not in drop))
    return out, removed


def convert(pdf: Path, opts: Options) -> tuple[str, str, Quality, int]:
    """Return (markdown, method, quality, header/footer lines removed) from the
    first method that scores well enough, else the best-scoring one."""
    with fixed_copy(pdf, opts) as src:
        return _convert(src, opts)


def _convert(pdf: Path, opts: Options) -> tuple[str, str, Quality, int]:
    methods = [("pymupdf4llm", with_pymupdf4llm), ("pdfplumber", with_pdfplumber),
               ("rendered-ocr", with_rendered_ocr)]  # slow; only if the others fail
    if has_ocr_layer(pdf):
        # Already OCRed: read that text layer first.
        methods.insert(0, ("pymupdf4llm-ocr-layer", with_pymupdf4llm_ocr_layer))
    tried: list[tuple[str, str, Quality, int]] = []
    errors = []
    for method, fn in methods:
        try:
            # Libraries print progress/OCR notes; keep stdout for the summary.
            with contextlib.redirect_stdout(sys.stderr):
                pages = fn(pdf, opts)
        except Exception as e:  # one library failing shouldn't stop the other
            errors.append(f"{method}: {e}")
            continue
        numbers = [p[0] for p in pages]
        texts = [p[1] for p in pages]
        halves = [p[2] if len(p) > 2 else "" for p in pages]
        # Split sheets: count printed numbers per half, ignoring the PDF's labels.
        seq = list(range(1, len(pages) + 1)) if any(halves) else numbers
        printed = (printed_page_numbers(pdf, list(zip(seq, texts)), labels=not any(halves))
                   if opts.page_markers else {})
        removed = 0
        if opts.strip_headers:
            texts, removed = strip_repeated_edges(texts)
        # Score the text without markers, so they can't make junk look fine.
        q = assess("\n\n".join(t.strip() for t in texts if t.strip()), len(texts))
        if opts.page_markers:
            text = "\n\n".join(
                (page_marker(n, printed.get(i), half) + "\n\n" + t.strip()).strip()
                for n, i, half, t in zip(numbers, seq, halves, texts)
            ) + "\n"
        else:
            text = "\n\n".join(t.strip() for t in texts if t.strip()) + "\n"
        tried.append((text, method, q, removed))
        if q.score >= MIN_SCORE:
            break  # good enough; don't bother with the fallback
    if not tried:
        raise RuntimeError("; ".join(errors))
    return max(tried, key=lambda t: t[2].score)


@dataclass
class Part:
    pdf: Path
    text: str
    method: str
    quality: Quality
    removed: int  # header/footer lines removed


# Order of the item details (from the plugin) at the top of the frontmatter.
META_KEYS = ("title", "authors", "editors", "year", "item_type", "publication",
             "doi", "url", "zotero", "attachment")


def frontmatter(parts: list[Part], quality: Quality, page_markers: bool,
                meta: dict | None = None, keys: list[str] | None = None) -> str:
    # json.dumps gives valid YAML for strings (double-quoted) and lists (flow
    # sequences), whatever characters they contain.
    lines = ["---"]
    for key in META_KEYS:
        value = (meta or {}).get(key)
        if value not in (None, "", []):
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    if keys:
        # Identifies the PDF(s) whatever the file names (see OutputIndex).
        lines.append(f"attachment_keys: {json.dumps(keys)}")
    if len(parts) == 1:
        p = parts[0]
        lines += [
            f"source: {json.dumps(str(p.pdf))}",
            f"converted: {date.today().isoformat()}",
            f"method: {p.method}",
            f"quality: {p.quality.score}",
            f"header_footer_lines_removed: {p.removed}",
        ]
    else:
        lines.append("sources:")
        for p in parts:
            lines += [
                f"  - path: {json.dumps(str(p.pdf))}",
                f"    method: {p.method}",
                f"    quality: {p.quality.score}",
                f"    header_footer_lines_removed: {p.removed}",
            ]
        lines += [
            f"converted: {date.today().isoformat()}",
            f"quality: {quality.score}  # lowest of the sources",
        ]
    if page_markers:
        # Tells the reader (or an LLM) how to cite pages.
        lines.append('page_markers: "[PDF page N] starts each page; '
                     '\'printed p. X\' is the number printed on it, where known"')
    return "\n".join(lines) + "\n---\n\n"


def load_jobs(args: argparse.Namespace) -> list[dict]:
    """Jobs: [{"pdfs": [path, ...], "out": "relative/name"}], one output file
    each ("out" has no .md). The plugin writes them to a JSON file (--jobs);
    PDFs given on the command line become one job each, named after the PDF."""
    if args.jobs:
        jobs = json.loads(Path(args.jobs).read_text(encoding="utf-8"))
    else:
        jobs = [{"pdfs": [a], "out": Path(a).stem} for a in args.pdfs if a.lower().endswith(".pdf")]
    for job in jobs:
        job["pdfs"] = [Path(p).expanduser() for p in job["pdfs"]]
    return jobs


def read_identity(md: Path) -> tuple[frozenset[str], frozenset[str]]:
    """(Zotero attachment keys, source PDF paths) from a Markdown file's
    frontmatter; empty sets if it has none (e.g. a file not made here)."""
    keys: frozenset[str] = frozenset()
    paths: set[str] = set()
    try:
        with md.open(encoding="utf-8") as f:
            if f.readline().strip() != "---":
                return keys, frozenset()
            for _, line in zip(range(200), f):
                if line.strip() == "---":
                    break
                if line.startswith("attachment_keys:"):
                    keys = frozenset(json.loads(line.split(":", 1)[1]))
                m = re.match(r'\s*(?:-\s*path|source):\s*(".*")\s*$', line)
                if m:
                    paths.add(json.loads(m.group(1)))
    except (OSError, ValueError):
        pass
    return keys, frozenset(paths)


class OutputIndex:
    """Which existing Markdown file was made from which PDFs, from the
    frontmatter of every .md in the output folder and _needs_review.

    Files are matched by Zotero attachment keys, or by source paths if they
    have no keys. The output folder wins over _needs_review."""

    def __init__(self, out_dir: Path, review_dir: Path):
        self.out_dir, self.review_dir = out_dir, review_dir
        self.by_keys: dict[frozenset, tuple[Path, bool]] = {}
        self.by_paths: dict[frozenset, tuple[Path, bool]] = {}
        for base, in_review in ((out_dir, False), (review_dir, True)):
            if not base.is_dir():
                continue
            for md in sorted(base.rglob("*.md")):
                if not in_review and review_dir in md.parents:
                    continue  # _needs_review is inside the output folder
                keys, paths = read_identity(md)
                self.add(keys, paths, md.relative_to(base), in_review)

    def add(self, keys, paths, rel: Path, in_review: bool) -> None:
        if keys:
            self.by_keys.setdefault(frozenset(keys), (rel, in_review))
        if paths:
            self.by_paths.setdefault(frozenset(paths), (rel, in_review))

    def set_converted(self, keys, paths, rel: Path) -> None:
        """Record a file just written to the output folder (replacing any
        entry for its copy in _needs_review)."""
        if keys:
            self.by_keys[frozenset(keys)] = (rel, False)
        if paths:
            self.by_paths[frozenset(paths)] = (rel, False)

    def find(self, keys, paths) -> tuple[Path, bool] | None:
        """(relative path, in _needs_review) of this job's existing file."""
        if keys and frozenset(keys) in self.by_keys:
            return self.by_keys[frozenset(keys)]
        return self.by_paths.get(frozenset(paths))

    def free_name(self, rel: Path) -> Path:
        """`rel`, or "name (2).md", "(3)"… if a file of that name (made from
        other PDFs, since find() didn't match it) exists in either folder."""
        for n in range(1, 1000):
            cand = rel if n == 1 else rel.with_name(f"{rel.stem} ({n}){rel.suffix}")
            if not (self.out_dir / cand).exists() and not (self.review_dir / cand).exists():
                return cand
        return rel


def safe_relative(out: str) -> Path | None:
    """The job's output path, or None if it would leave the output folder."""
    rel = Path(out + ".md")
    if rel.is_absolute() or ".." in rel.parts:
        return None
    return rel


def resolve_ocr_language(wanted: str) -> tuple[str, str | None]:
    """Return (Tesseract languages to use, warning or None). Languages that
    aren't installed are dropped, falling back to "eng". Call after
    ensure_tesseract_on_path()."""
    langs = [l for l in re.split(r"[+,\s]+", wanted.strip()) if l]
    invalid = [l for l in langs if not re.fullmatch(r"[A-Za-z_]+", l)]
    langs = [l for l in langs if l not in invalid] or ["eng"]
    try:
        tessdata = Path(pymupdf.get_tessdata())
    except Exception:
        return "+".join(langs), None  # no Tesseract; the summary says so if it matters
    missing = invalid + [l for l in langs if not (tessdata / f"{l}.traineddata").exists()]
    usable = [l for l in langs if l not in missing] or ["eng"]
    if not missing:
        return "+".join(usable), None
    return "+".join(usable), (
        f"OCR language not installed: {', '.join(missing)} "
        f"(macOS: brew install tesseract-lang); OCR used {'+'.join(usable)}"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Convert PDFs to Markdown.")
    parser.add_argument("--out", type=Path, required=True,
                        help="output folder for the Markdown files (required)")
    parser.add_argument("--strip-headers", action=argparse.BooleanOptionalAction,
                        default=STRIP_HEADERS,
                        help="remove headers/footers repeated on most pages "
                             f"(default: {STRIP_HEADERS})")
    parser.add_argument("--overwrite", action=argparse.BooleanOptionalAction,
                        default=OVERWRITE,
                        help=f"replace existing .md files (default: {OVERWRITE})")
    parser.add_argument("--page-markers", action=argparse.BooleanOptionalAction,
                        default=PAGE_MARKERS,
                        help=f"mark the start of each page (default: {PAGE_MARKERS})")
    parser.add_argument("--ocr-language", default=OCR_LANGUAGE,
                        help=f"Tesseract language(s) for OCR, e.g. eng+deu (default: {OCR_LANGUAGE})")
    parser.add_argument("--html-tables", action=argparse.BooleanOptionalAction,
                        default=HTML_TABLES,
                        help=f"write tables as HTML instead of Markdown (default: {HTML_TABLES})")
    parser.add_argument("--repair-symbols", action=argparse.BooleanOptionalAction,
                        default=REPAIR_SYMBOLS,
                        help="repair broken symbol encodings, e.g. Wingdings ☞ read as F "
                             f"(default: {REPAIR_SYMBOLS})")
    parser.add_argument("--jobs", help="JSON job list written by the Zotero plugin")
    parser.add_argument("--progress", action="store_true",
                        help=f"print a '{PROGRESS_PREFIX} {{...}}' line to stderr as each job "
                             "starts and ends (for the Zotero plugin)")
    parser.add_argument("pdfs", nargs="*")
    args = parser.parse_args(argv)
    out_dir = args.out.expanduser()
    review_dir = out_dir / REVIEW_SUBDIR

    jobs = load_jobs(args)
    if not jobs:
        print("No PDF paths given.", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    has_tesseract = ensure_tesseract_on_path()
    ocr_language, ocr_warning = resolve_ocr_language(args.ocr_language)
    opts = Options(
        strip_headers=args.strip_headers,
        page_markers=args.page_markers,
        ocr_language=ocr_language,
        html_tables=args.html_tables,
        repair_symbols=args.repair_symbols,
    )
    # pymupdf's own messages bypass redirect_stdout; keep stdout for the summary.
    try:  # set_messages(stream=…) is missing in old PyMuPDF versions
        pymupdf.set_messages(stream=sys.stderr)
    except Exception:
        pass

    def progress(i: int, state: str) -> None:
        # state: start, ok, review, skipped or failed.
        if args.progress:
            print(PROGRESS_PREFIX, json.dumps({"job": i, "state": state}), file=sys.stderr, flush=True)

    index = OutputIndex(out_dir, review_dir)
    done, skipped, review, failed = [], [], [], []
    for i, job in enumerate(jobs):
        progress(i, "start")
        name = job["out"]
        rel = safe_relative(name)
        if rel is None:
            failed.append(f"{name}: output name leaves the output folder")
            progress(i, "failed")
            continue
        missing = [p.name for p in job["pdfs"] if not p.is_file()]
        if missing:
            failed.append(f"{name}: not found (not synced/downloaded?): {', '.join(missing)}")
            print(f"FAILED   {name}: missing {missing}", file=sys.stderr)
            progress(i, "failed")
            continue
        keys = job.get("keys") or []
        paths = [str(p) for p in job["pdfs"]]
        found = index.find(keys, paths)
        if found:
            # Already converted (or in review) under this name or an older one.
            existing, in_review = found
            if existing != rel:
                print(f"         {name}: existing file is {existing}", file=sys.stderr)
            rel, name = existing, str(existing.with_suffix(""))
            if not in_review and not args.overwrite:
                skipped.append(name)
                progress(i, "skipped")
                continue
        else:
            free = index.free_name(rel)
            if free != rel:
                print(f"         {name}: name taken by a different PDF; using {free}", file=sys.stderr)
            rel, name = free, str(free.with_suffix(""))
        out, review_out = out_dir / rel, review_dir / rel

        parts: list[Part] = []
        try:
            for pdf in job["pdfs"]:
                text, method, q, removed = convert(pdf, opts)
                parts.append(Part(pdf, text, method, q, removed))
                print(f"         {pdf.name}  [{method}, {q.score}, -{removed} header/footer lines]", file=sys.stderr)
        except Exception as e:  # a merged file with a chapter missing would mislead
            failed.append(f"{name}: {pdf.name}: {e}")
            print(f"FAILED   {name}: {pdf.name}: {e}", file=sys.stderr)
            progress(i, "failed")
            continue

        if len(parts) == 1:
            quality = parts[0].quality
            body = parts[0].text
        else:
            worst = min(parts, key=lambda p: p.quality.score)
            reason = f"{worst.pdf.name}: {worst.quality.reason}" if worst.quality.reason else ""
            quality = Quality(worst.quality.score, reason)
            # One top-level heading per source PDF, in order.
            body = "\n\n".join(f"# {p.pdf.stem}\n\n{p.text}" for p in parts)
        content = frontmatter(parts, quality, opts.page_markers, job.get("meta"), keys) + body

        if quality.score >= MIN_SCORE:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(content, encoding="utf-8")
            # Drop a stale copy from an earlier run that needed review.
            review_out.unlink(missing_ok=True)
            index.set_converted(keys, paths, rel)
            done.append(name)
            print(f"ok       {name}  [{quality.score}]", file=sys.stderr)
            progress(i, "ok")
        else:
            review_out.parent.mkdir(parents=True, exist_ok=True)
            review_out.write_text(content, encoding="utf-8")
            index.add(keys, paths, rel, True)
            review.append(f"{name}: {quality.reason}")
            print(f"REVIEW   {name}  [{quality.score}] {quality.reason}", file=sys.stderr)
            progress(i, "review")

    # Summary on stdout; per-file detail went to stderr.
    print(f"{len(done)} converted → {out_dir}")
    if skipped:
        print(f"{len(skipped)} skipped (already converted)")
    for r in review:
        print(f"needs review: {r}")
    if ocr_warning:
        print(ocr_warning)
    if review and not has_tesseract:
        print("Tesseract not found, so scans can't be OCRed (macOS: brew install tesseract)")
    for f in failed:
        print(f"failed: {f}")
    return 1 if failed and not done else 0


# Wingdings character code -> Unicode, for add_wingdings_unicode_maps().
# From Wikipedia's Wingdings character table, with Supplemental Arrows-C
# replaced by plain arrows (→ ← ↑ ↓ ↔ ↕ ↖ ↗ ↘ ↙). Codes with no Unicode
# equivalent (space, 0x7F, 0xFF) are left out.
WINGDINGS_TO_UNICODE = {
    0x21: '🖉', 0x22: '✂', 0x23: '✁', 0x24: '👓', 0x25: '🕭', 0x26: '🕮', 0x27: '🕯', 0x28: '🕿',
    0x29: '✆', 0x2A: '🖂', 0x2B: '🖃', 0x2C: '📪', 0x2D: '📫', 0x2E: '📬', 0x2F: '📭', 0x30: '🗀',
    0x31: '🗁', 0x32: '🗎', 0x33: '🗏', 0x34: '🗐', 0x35: '🗄', 0x36: '⏳', 0x37: '🖮', 0x38: '🖰',
    0x39: '🖲', 0x3A: '🖳', 0x3B: '🖴', 0x3C: '🖫', 0x3D: '🖬', 0x3E: '✇', 0x3F: '✍', 0x40: '🖎',
    0x41: '✌', 0x42: '🖏', 0x43: '👍', 0x44: '👎', 0x45: '☜', 0x46: '☞', 0x47: '☝', 0x48: '🖗',
    0x49: '🖐', 0x4A: '☺', 0x4B: '😐', 0x4C: '☹', 0x4D: '💣', 0x4E: '🕱', 0x4F: '🏳', 0x50: '🏱',
    0x51: '✈', 0x52: '☼', 0x53: '🌢', 0x54: '❄', 0x55: '🕆', 0x56: '✞', 0x57: '🕈', 0x58: '✠',
    0x59: '✡', 0x5A: '☪', 0x5B: '☯', 0x5C: '🕉', 0x5D: '☸', 0x5E: '♈', 0x5F: '♉', 0x60: '♊',
    0x61: '♋', 0x62: '♌', 0x63: '♍', 0x64: '♎', 0x65: '♏', 0x66: '♐', 0x67: '♑', 0x68: '♒',
    0x69: '♓', 0x6A: '🙰', 0x6B: '🙵', 0x6C: '⚫', 0x6D: '🔾', 0x6E: '◼', 0x6F: '🞏', 0x70: '🞐',
    0x71: '❑', 0x72: '❒', 0x73: '🞟', 0x74: '⧫', 0x75: '◆', 0x76: '❖', 0x77: '🞙', 0x78: '⌧',
    0x79: '⮹', 0x7A: '⌘', 0x7B: '🏵', 0x7C: '🏶', 0x7D: '🙶', 0x7E: '🙷', 0x80: '🄋', 0x81: '➀',
    0x82: '➁', 0x83: '➂', 0x84: '➃', 0x85: '➄', 0x86: '➅', 0x87: '➆', 0x88: '➇', 0x89: '➈',
    0x8A: '➉', 0x8B: '🄌', 0x8C: '➊', 0x8D: '➋', 0x8E: '➌', 0x8F: '➍', 0x90: '➎', 0x91: '➏',
    0x92: '➐', 0x93: '➑', 0x94: '➒', 0x95: '➓', 0x96: '🙢', 0x97: '🙠', 0x98: '🙡', 0x99: '🙣',
    0x9A: '🙞', 0x9B: '🙜', 0x9C: '🙝', 0x9D: '🙟', 0x9E: '∙', 0x9F: '•', 0xA0: '⬝', 0xA1: '⭘',
    0xA2: '🞆', 0xA3: '🞈', 0xA4: '🞊', 0xA5: '🞋', 0xA6: '🔿', 0xA7: '▪', 0xA8: '🞎', 0xA9: '🟁',
    0xAA: '🟅', 0xAB: '★', 0xAC: '🟋', 0xAD: '🟏', 0xAE: '🟓', 0xAF: '🟑', 0xB0: '⯐', 0xB1: '⌖',
    0xB2: '⯎', 0xB3: '⯏', 0xB4: '⯑', 0xB5: '✪', 0xB6: '✰', 0xB7: '🕐', 0xB8: '🕑', 0xB9: '🕒',
    0xBA: '🕓', 0xBB: '🕔', 0xBC: '🕕', 0xBD: '🕖', 0xBE: '🕗', 0xBF: '🕘', 0xC0: '🕙', 0xC1: '🕚',
    0xC2: '🕛', 0xC3: '⮰', 0xC4: '⮱', 0xC5: '⮲', 0xC6: '⮳', 0xC7: '⮴', 0xC8: '⮵', 0xC9: '⮶',
    0xCA: '⮷', 0xCB: '🙪', 0xCC: '🙫', 0xCD: '🙕', 0xCE: '🙔', 0xCF: '🙗', 0xD0: '🙖', 0xD1: '🙐',
    0xD2: '🙑', 0xD3: '🙒', 0xD4: '🙓', 0xD5: '⌫', 0xD6: '⌦', 0xD7: '⮘', 0xD8: '⮚', 0xD9: '⮙',
    0xDA: '⮛', 0xDB: '⮈', 0xDC: '⮊', 0xDD: '⮉', 0xDE: '⮋', 0xDF: '←', 0xE0: '→', 0xE1: '↑',
    0xE2: '↓', 0xE3: '↖', 0xE4: '↗', 0xE5: '↙', 0xE6: '↘', 0xE7: '←', 0xE8: '→', 0xE9: '↑',
    0xEA: '↓', 0xEB: '↖', 0xEC: '↗', 0xED: '↙', 0xEE: '↘', 0xEF: '⇦', 0xF0: '⇨', 0xF1: '⇧',
    0xF2: '⇩', 0xF3: '⬄', 0xF4: '⇳', 0xF5: '⬁', 0xF6: '⬀', 0xF7: '⬃', 0xF8: '⬂', 0xF9: '🢬',
    0xFA: '🢭', 0xFB: '🗶', 0xFC: '✓', 0xFD: '🗷', 0xFE: '🗹',
}


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
