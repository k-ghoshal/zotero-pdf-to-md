#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pymupdf4llm<2", "pdfplumber", "fonttools"]
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
import math
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
    unreadable: int = 0  # characters that are U+FFFD, private-use or control codes


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
    return Quality(round(score, 2), reason, bad)


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


# Fonts whose text stays unreadable because they aren't installed here, by PDF
# (str of its path), for the summary: installing them may fix the text.
MISSING_FONTS: dict[str, set[str]] = {}
# Filled by the font fixes while `fixed_copy` runs them on one PDF.
_missing_now: set[str] = set()


@contextlib.contextmanager
def fixed_copy(pdf: Path, opts: Options):
    """Yield `pdf` itself, or a temporary copy with the fixes below applied.
    Stamps on scanned pages and OCR text past the page edge are always fixed;
    the font fixes run with --repair-symbols. The original PDF is never
    modified."""
    fixes = [("remove stamps from scanned pages", remove_scan_stamps,
              "removed stamp text from {} scanned or outlined page(s), so OCR text is kept"),
             ("check OCR text past the page edge", widen_pages_to_ocr_text,
              "widened {} page(s) whose OCR text runs past the page edge")]
    if opts.repair_symbols:
        fixes.append(("check symbol fonts", add_wingdings_unicode_maps,
                      "added Unicode map to {} Wingdings font(s)"))
        fixes.append(("check fonts without a Unicode map", add_glyph_unicode_maps,
                      "rebuilt the Unicode map of {} font(s) from the installed fonts"))
        fixes.append(("check glyph names without a Unicode map", add_name_unicode_maps,
                      "mapped glyphs of {} font(s) by their glyph names"))
        fixes.append(("check glyphs against the installed fonts", add_outline_unicode_maps,
                      "mapped glyphs of {} font(s) by their outlines in the installed font"))
        fixes.append(("check accent, arrow and ligature glyphs without a Unicode map", add_shape_unicode_maps,
                      "mapped accent, arrow and ligature glyphs of {} font(s) by their shape"))
        fixes.append(("check phonetic and pi font glyphs against their names", add_pi_unicode_maps,
                      "remapped glyphs of {} phonetic or pi font(s) by their shape"))
    _missing_now.clear()
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
        if _missing_now and (still := _fonts_with_junk(doc) & _missing_now):
            MISSING_FONTS[str(pdf)] = still
        if not changed:
            yield pdf
            return
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / pdf.name
            doc.save(copy)
            yield copy


def widen_pages_to_ocr_text(doc: "pymupdf.Document", min_share: float = 0.02) -> int:
    """Widen pages whose invisible (OCR) text runs past the page edge, so it
    can all be read, and return the number of pages changed. A page is
    changed only if more than `min_share` of its OCR text lies outside it;
    unrotated pages only."""
    widened = 0
    for page in doc:
        if page.rotation:
            continue
        hidden = [pymupdf.Rect(s["bbox"]) for s in page.get_texttrace() if s["type"] == 3]
        edge = page.rect + (-2, -2, 2, 2)
        outside = [r for r in hidden if not r.is_empty and r not in edge]
        if not outside or len(outside) <= min_share * len(hidden):
            continue
        target = pymupdf.Rect(page.rect)
        for r in outside:
            target |= r
        box = target * ~page.transformation_matrix  # page coordinates -> PDF coordinates
        box.normalize()
        page.set_mediabox(box)
        widened += 1
    return widened


def remove_scan_stamps(doc: "pymupdf.Document", min_image: float = 0.9,
                       max_text: float = 0.05) -> int:
    """Remove the visible text from scanned pages that have no OCR layer yet,
    and return the number of pages changed. Such text is a download stamp,
    and pymupdf4llm drops the page's OCR text when any visible text is left.

    A page counts as scanned if one image covers at least `min_image` of it
    and visible text covers less than `max_text`, or if its text is drawn as
    outlines (`_text_drawn_as_paths`). Only done if at least half the pages
    count. Pages with invisible text (an existing OCR layer) are skipped."""
    pages = []
    for page in doc:
        area = abs(page.rect)
        image = max((abs(pymupdf.Rect(i["bbox"]) & page.rect)
                     for i in page.get_image_info()), default=0)
        trace = page.get_texttrace()
        visible = [s for s in trace if s["type"] != 3 and s["opacity"] > 0]
        if not visible or len(visible) < len(trace):
            continue
        if sum(abs(pymupdf.Rect(s["bbox"]) & page.rect) for s in visible) >= max_text * area:
            continue
        if image < min_image * area and not _text_drawn_as_paths(page, max_text):
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


# A page with at least this many path segments and almost no text has its text
# drawn as outlines.
OUTLINE_MIN_PATHS = 1000


def _text_drawn_as_paths(page: "pymupdf.Page", max_text: float = 0.05) -> bool:
    """True if the page's text is drawn as filled paths rather than written in
    a font: many path segments and visible text on less than `max_text` of the
    page."""
    area = abs(page.rect)
    text = sum(abs(pymupdf.Rect(s["bbox"]) & page.rect) for s in page.get_texttrace()
               if s["type"] != 3 and s["opacity"] > 0)
    if text >= max_text * area:
        return False
    return sum(len(d["items"]) for d in page.get_cdrawings()) >= OUTLINE_MIN_PATHS


def has_outlined_text(pdf: Path) -> bool:
    """True if at least half the pages have their text drawn as outlines."""
    try:
        with pymupdf.open(pdf) as doc:
            return sum(map(_text_drawn_as_paths, doc)) >= doc.page_count / 2 > 0
    except Exception:
        return False


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
    number of fonts changed. Fonts with Identity-H encoding, an empty
    ToUnicode map and an embedded subset without a cmap table qualify.

    A glyph is mapped only if its width in the installed font matches the
    width the PDF gives it, and a font only if at least `min_match` of its
    glyphs match."""
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
                _note_missing(name, "has no Unicode map")
                continue
            widths = _cid_widths(doc, cid_font)
            mapping = _glyph_map(pymupdf.Font(fontbuffer=_font_data(*installed)), widths, min_match)
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


# Pieces of tall brackets, braces and integrals (Adobe's names; their standard
# Unicode is in the private-use area).
BRACKET_PIECES = {
    "parenlefttp": "⎛", "parenleftex": "⎜", "parenleftbt": "⎝",
    "parenrighttp": "⎞", "parenrightex": "⎟", "parenrightbt": "⎠",
    "bracketlefttp": "⎡", "bracketleftex": "⎢", "bracketleftbt": "⎣",
    "bracketrighttp": "⎤", "bracketrightex": "⎥", "bracketrightbt": "⎦",
    "bracelefttp": "⎧", "braceleftmid": "⎨", "braceleftbt": "⎩",
    "bracerighttp": "⎫", "bracerightmid": "⎬", "bracerightbt": "⎭", "braceex": "⎪",
    "integraltp": "⌠", "integralbt": "⌡", "integralex": "⎮",
    "arrowvertex": "⏐", "arrowhorizex": "⎯",
}
# Glyph names outside Adobe's list: Monotype's Times Phonetic IPA fonts. Diacritics are combining marks; the font writes them after the letter.
IPA_GLYPH_NAMES = {
    "ash": "æ", "bhooktop": "ɓ", "ccurlytail": "ɕ", "dhooktop": "ɗ", "drighttail": "ɖ",
    "epsilonreversed": "ɜ", "gcursive": "ɡ", "ghooktop": "ɠ", "hcrossed": "ħ",
    "hhooktop": "ɦ", "ibarred": "ɨ", "jdotlessbarred": "ɟ", "lbelted": "ɬ",
    "lrighttail": "ɭ", "nlefthookatleft": "ɲ", "nrighttail": "ɳ", "OEsmalllig": "ɶ",
    "ramshorns": "ɤ", "srighttail": "ʂ", "trighttail": "ʈ", "ubarred": "ʉ",
    "vinverted": "ʌ", "yogh": "ʒ", "zcurlytail": "ʑ", "pipe": "ǀ",
    "lengthmark": "ː", "vertstrokesuperior": "ˈ", "thinspace": " ",
    "underring": "̥", "syllabicitymark": "̩", "archsubscript": "̯",
    "loweringsubscript": "̞", "raisingsubscript": "̝", "wedgesubscript": "̬",
    "halfringrightsubscript": "̹", "halfringleftsubscript": "̜",
    "midtilde": "̴", "umlaut": "̈",
}
# Small capitals in phonetic fonts are IPA letters (ɪ, ʀ, ɢ ...), not small caps.
IPA_SMALL_CAPS = {"A": "ᴀ", "B": "ʙ", "E": "ᴇ", "G": "ɢ", "H": "ʜ", "I": "ɪ",
                  "L": "ʟ", "N": "ɴ", "R": "ʀ", "U": "ᴜ", "Y": "ʏ"}
# Glyph-name endings for variants of a character: Adobe's small caps, old-style
# figures etc., TeX's sizes of delimiters (CMEX: parenleftBig).
NAME_SUFFIXES = ("oldstyleit", "oldstyle", "small", "superior", "inferior", "alt",
                 "bigg", "Bigg", "big", "Big", "text", "display", "wider", "wide")


def add_name_unicode_maps(doc: "pymupdf.Document") -> int:
    """Give fonts a ToUnicode entry for each glyph whose text comes out as junk
    (U+FFFD, a control or private-use character) when its glyph name or that
    private-use character says what it is, and return the number of fonts
    changed. Handles names MuPDF doesn't read: Adobe's legacy variant names
    (Asmall, zerooldstyle, braceex), suffixed names (parenleftBig, a.sc),
    `IPA_GLYPH_NAMES`, numeric names ("227b", "C0070") and Symbol's U+F0xx.
    Simple fonts are checked code by code, two-byte fonts through their
    ToUnicode map. Only fonts whose text contains junk are looked at."""
    seen, fixed = set(), 0
    junk_fonts = _fonts_with_junk(doc)
    for pno in range(doc.page_count):
        for xref, _ext, ftype, basefont, _name, _enc in doc.get_page_fonts(pno):
            if xref in seen:
                continue
            seen.add(xref)
            name = basefont.split("+")[-1]
            if name not in junk_fonts:
                continue
            if ftype == "Type0":
                found = {code: new for code, text in _to_unicode_map(doc, xref).items()
                         if _is_junk(text)
                         and (new := _repaired_text(text, None, name))}
                code_bytes = 2
            elif ftype in ("Type1", "MMType1", "TrueType"):
                found = _repaired_codes(doc, xref, name)
                code_bytes = 1
            else:
                continue
            if found:
                _add_to_unicode(doc, xref, found, code_bytes, name)
                fixed += 1
    return fixed


def _add_to_unicode(doc: "pymupdf.Document", xref: int, found: dict[int, str],
                    code_bytes: int, name: str) -> None:
    """Merge `found` (code -> text) into the ToUnicode map of font `xref`."""
    cmap_xref = doc.get_new_xref()
    doc.update_object(cmap_xref, "<<>>")
    doc.update_stream(cmap_xref, _to_unicode_cmap({**_to_unicode_map(doc, xref), **found},
                                                  code_bytes).encode())
    doc.xref_set_key(xref, "ToUnicode", f"{cmap_xref} 0 R")
    print(f"         font {name}: " + ", ".join(
        f"{c:#04x}={t}" for c, t in list(found.items())[:12])
        + (f" (+{len(found) - 12} more)" if len(found) > 12 else ""), file=sys.stderr)


def _to_unicode_map(doc: "pymupdf.Document", xref: int) -> dict[int, str]:
    """The ToUnicode map of font `xref` (code -> text), empty if it has none."""
    kind, value = doc.xref_get_key(xref, "ToUnicode")
    if kind != "xref":
        return {}
    return _parse_to_unicode(doc.xref_stream(int(value.split()[0])).decode("latin-1"))


def _simple_widths(doc: "pymupdf.Document", xref: int) -> dict[int, float]:
    """Code -> width (1/1000 em) of a simple font, from FirstChar and Widths."""
    kind, value = doc.xref_get_key(xref, "FirstChar")
    first = int(value) if kind == "int" else 0
    kind, value = doc.xref_get_key(xref, "Widths")
    if kind == "null":
        return {}
    if kind == "xref":
        value = doc.xref_object(int(value.split()[0]))
    widths = [float(w) for w in re.findall(r"-?[\d.]+", value)]
    return {first + i: w for i, w in enumerate(widths) if first + i < 256}


def _fonts_with_junk(doc: "pymupdf.Document") -> set[str]:
    """Names (without subset prefix) of the fonts whose text contains junk."""
    fonts = set()
    for page in doc:
        for span in page.get_texttrace():
            name = span["font"].split("+")[-1]
            if name not in fonts and any(_is_junk(chr(c[0])) for c in span["chars"]):
                fonts.add(name)
    return fonts


def _repaired_codes(doc: "pymupdf.Document", xref: int, font: str) -> dict[int, str]:
    """For a simple font: code -> text for the codes MuPDF reads as junk and
    that their text or glyph name can repair."""
    widths = _simple_widths(doc, xref)
    codes = ([code for code, w in widths.items() if w]
             if widths else list(range(32, 256)))  # standard 14 fonts have no widths
    text = _extracted_text(doc, xref, codes)
    junk = [code for code in codes if _is_junk(text.get(code, ""))]
    if not junk:
        return {}
    names = _glyph_names(doc, xref)
    found = {}
    for code in junk:
        new = _repaired_text(text.get(code, ""), names.get(code), font)
        if new:
            found[code] = new
    # Numbers as names, only where the whole font follows one scheme (most such
    # names are glyph or slot numbers that say nothing):
    # "C0070": Latin-1 in decimal, if (nearly) all names look like that
    numbered = {code: int(m[1]) for code, n in names.items() if (m := re.fullmatch(r"C(\d{4})", n))}
    if numbered and len(numbered) >= 0.8 * len(names):
        found.update({code: chr(n) for code, n in numbered.items()
                      if code in junk and 32 <= n < 256 and not _is_junk(chr(n))})
    # "2264" = ≤: Unicode in hex, if some names have the letters a-f ("227b")
    hexed = {code: n for code, n in names.items() if re.fullmatch(r"\d[0-9a-fA-F]{3,4}", n)}
    if any(re.search(r"[a-fA-F]", n) for n in hexed.values()):
        # MuPDF reads all-digit names as decimal: "2208" gives U+08A0, not ∈
        found.update({code: chr(int(n, 16)) for code, n in hexed.items()
                      if (code in junk or n.isdigit() and text.get(code) == chr(int(n)))
                      and not _is_junk(chr(int(n, 16)))})
    return found


def _repaired_text(text: str, glyph: str | None, font: str) -> str | None:
    """What a glyph really is, from the junk text MuPDF gives for it and its
    glyph name (if known); None if neither tells."""
    base = font.split(",")[0].split("-")[0].removesuffix("MT").lower()
    if len(text) == 1 and 0xF020 <= ord(text) <= 0xF0FF:
        if base == "symbol":
            return _symbol_encoding().get(ord(text) - 0xF000)
        if base == "wingdings":
            return WINGDINGS_TO_UNICODE.get(ord(text) - 0xF000)
    if len(text) == 1 and 0xE000 <= ord(text) <= 0xF8FF and not glyph:
        glyph = _legacy_glyph_name(ord(text))  # e.g. U+F761 -> Asmall
    return _glyph_name_text(glyph, font) if glyph else None


def _glyph_name_text(glyph: str, font: str) -> str | None:
    """The text a glyph name stands for, if it is a real character."""
    from fontTools import agl

    base = glyph.split(".")[0]  # a.sc, one.oldstyle
    if base in BRACKET_PIECES or base in IPA_GLYPH_NAMES:
        return BRACKET_PIECES.get(base) or IPA_GLYPH_NAMES[base]
    text = agl.toUnicode(base) if base else ""
    if text and not _is_junk(text):
        return text
    for suffix in NAME_SUFFIXES:
        stem = base.removesuffix(suffix)
        if stem != base and stem:
            text = _glyph_name_text(stem, font)
            if text and suffix == "small":  # small capitals
                if re.search(r"ipa|phon|sil|doulos|charis|gentium", font, re.I):
                    return IPA_SMALL_CAPS.get(text.upper(), text.lower())
                return text.lower()
            return text
    return None


@functools.cache
def _legacy_names() -> dict[int, str]:
    from fontTools import agl

    return {cp: name for name, cps in agl.LEGACY_AGL2UV.items() for cp in cps
            if 0xE000 <= cp <= 0xF8FF}


def _legacy_glyph_name(cp: int) -> str | None:
    """Adobe's glyph name for one of its private-use characters (U+F761 = Asmall)."""
    return _legacy_names().get(cp)


def _is_junk(text: str) -> bool:
    """Empty, or U+FFFD, a control, private-use or unassigned character."""
    return not text or any(c == "�" or unicodedata.category(c) in ("Cc", "Co", "Cn")
                           for c in text)


def _extracted_text(doc: "pymupdf.Document", xref: int, codes: list[int]) -> dict[int, str]:
    """What MuPDF extracts for each one-byte code of font `xref`: each code is
    written on its own line of a scratch page, which is removed again."""
    page = doc.new_page(width=100, height=12 * len(codes) + 20)
    try:
        stream = doc.get_new_xref()
        doc.update_object(stream, "<<>>")
        doc.update_stream(stream, "".join(
            f"BT /F0 10 Tf 10 {12 * i + 10} Td <{code:02X}> Tj ET\n"
            for i, code in enumerate(codes)).encode())
        doc.xref_set_key(page.xref, "Resources", f"<< /Font << /F0 {xref} 0 R >> >>")
        page.set_contents(stream)
        text: dict[int, str] = {}
        for block in page.get_text("rawdict", flags=0)["blocks"]:
            for line in block.get("lines", ()):
                for span in line["spans"]:
                    for c in span["chars"]:
                        i = round((page.rect.height - c["origin"][1] - 10) / 12)
                        if 0 <= i < len(codes):
                            text[codes[i]] = text.get(codes[i], "") + c["c"]
        return text
    finally:
        doc.delete_page(doc.page_count - 1)


def _glyph_names(doc: "pymupdf.Document", xref: int) -> dict[int, str]:
    """Code -> glyph name for a simple font: the PDF's /Differences, else the
    embedded font's own encoding (Type 1, CFF) or its cmap and post table
    (TrueType)."""
    names: dict[int, str] = {}
    descriptor = doc.xref_get_key(xref, "FontDescriptor")
    if descriptor[0] == "xref":
        try:
            names = _embedded_glyph_names(doc, int(descriptor[1].split()[0]))
        except Exception:  # a damaged or unusual font file: Differences only
            names = {}
    kind, value = doc.xref_get_key(xref, "Encoding")
    if kind == "xref":
        value = doc.xref_get_key(int(value.split()[0]), "Differences")[1]
    elif kind == "dict":
        value = re.search(r"/Differences\s*\[([^\]]*)\]", value)
        value = value[1] if value else ""
    else:
        value = ""
    code = 0
    for token in re.findall(r"/[^\s/\[\]]+|\d+", value or ""):
        if token.startswith("/"):
            names[code] = token[1:]
            code += 1
        else:
            code = int(token)
    return names


def _embedded_glyph_names(doc: "pymupdf.Document", descriptor: int) -> dict[int, str]:
    import io

    for key in ("FontFile", "FontFile3", "FontFile2"):
        kind, value = doc.xref_get_key(descriptor, key)
        if kind != "xref":
            continue
        data = doc.xref_stream(int(value.split()[0]))
        if key == "FontFile":  # Type 1: "dup 65 /A put" in the clear-text part
            clear = data[:data.find(b"eexec")].decode("latin-1")
            return {int(c): n for c, n in re.findall(r"dup (\d+) ?/(\S+) put", clear)}
        if key == "FontFile3":
            from fontTools.cffLib import CFFFontSet

            if doc.xref_get_key(int(value.split()[0]), "Subtype")[1] not in ("/Type1C", "null"):
                return {}
            cff = CFFFontSet()
            cff.decompile(io.BytesIO(data), None)
            encoding = cff[cff.fontNames[0]].Encoding
            if not isinstance(encoding, list):
                return {}  # StandardEncoding etc.: MuPDF knows those
            return {code: n for code, n in enumerate(encoding) if n and n != ".notdef"}
        from fontTools.ttLib import TTFont

        font = TTFont(io.BytesIO(data))
        if "cmap" not in font or "post" not in font:
            return {}
        names = {}
        for table in font["cmap"].tables:
            if (table.platformID, table.platEncID) in ((3, 0), (1, 0)):
                for code, glyph in table.cmap.items():
                    names.setdefault(code & 0xFF, glyph)  # symbolic fonts use F0xx
        return names
    return {}


@functools.cache
def _symbol_encoding() -> dict[int, str]:
    """The Symbol font's encoding, code -> character, as MuPDF reads it."""
    with pymupdf.open() as doc:
        doc.new_page()
        font = doc.get_new_xref()
        doc.update_object(font, "<< /Type /Font /Subtype /Type1 /BaseFont /Symbol >>")
        text = _extracted_text(doc, font, list(range(32, 256)))
    # Pieces of tall brackets come out as Adobe's private-use characters.
    text = {code: _repaired_text(t, None, "") if _is_junk(t) else t for code, t in text.items()}
    return {code: t for code, t in text.items() if t}


# How far apart (1/1000 em) the outline measures of a PDF glyph and an installed
# glyph may be for them to count as the same drawing.
OUTLINE_MATCH = 8.0


def add_outline_unicode_maps(doc: "pymupdf.Document") -> int:
    """Give fonts a ToUnicode entry for each glyph that still comes out as junk
    when the font installed here under the same name has the same drawing, and
    return the number of fonts changed. For fonts whose glyph names say
    nothing (g1, g2 ...).

    A PDF glyph matches an installed glyph with the same width (±1.5/1000 em)
    whose outline has the same area, centre, spread and bounding box (each
    within OUTLINE_MATCH/1000 em). Of several close matches the lowest code
    point wins. Installed glyphs without a code point get one from the font's
    substitutions (`_glyph_chars`). Only fonts whose text has junk are looked
    at."""
    seen, fixed = set(), 0
    junk_fonts = _fonts_with_junk(doc)
    junk_glyphs: dict[str, set[int]] | None = None
    for pno in range(doc.page_count):
        for xref, _ext, ftype, basefont, _name, _enc in doc.get_page_fonts(pno):
            if xref in seen:
                continue
            seen.add(xref)
            name = basefont.split("+")[-1]
            if name not in junk_fonts or ftype not in ("Type1", "MMType1", "TrueType", "Type0"):
                continue
            if not (installed := system_fonts().get(name)):
                _note_missing(name, "has glyphs without Unicode")
                continue
            try:
                if ftype in ("Type1", "MMType1", "TrueType"):
                    found, code_bytes = _outline_codes(doc, xref, installed), 1
                elif ftype == "Type0":
                    if junk_glyphs is None:
                        junk_glyphs = _junk_glyphs(doc)
                    found = _outline_cids(doc, xref, installed, junk_glyphs.get(basefont, set()))
                    code_bytes = 2
                else:
                    continue
            except Exception as e:  # a damaged or unusual font file
                print(f"         font {name}: could not compare outlines: {e}", file=sys.stderr)
                continue
            if found:
                _add_to_unicode(doc, xref, found, code_bytes, name)
                fixed += 1
    return fixed


def _note_missing(name: str, problem: str) -> None:
    """Log (once per PDF) that font `name` would be needed to repair its text,
    and remember it for the summary. Names without three lowercase letters in
    a row are publisher-private and skipped."""
    if name in _missing_now or not re.search(r"[a-z]{3}", name):
        return
    _missing_now.add(name)
    print(f"         font {name} {problem} and isn't installed here,"
          " so its text can't be repaired", file=sys.stderr)


def _outline_codes(doc: "pymupdf.Document", xref: int,
                   installed: tuple[Path, int]) -> dict[int, str]:
    """For a simple font: code -> text for the codes MuPDF reads as junk whose
    glyph the installed font has."""
    widths = _simple_widths(doc, xref)
    text = _extracted_text(doc, xref, list(widths))
    junk = [code for code in widths if _is_junk(text.get(code, ""))]
    embedded = _embedded_outlines(doc, xref) if junk else None
    if not embedded:
        return {}
    glyphs, _order, scale, names = embedded
    names = {**names, **_glyph_names(doc, xref)}
    index = _outline_index(*installed)
    found = {}
    for code in junk:
        glyph = names.get(code)
        if glyph and glyph in glyphs and (new := index.match(glyphs[glyph], glyphs, scale,
                                                             widths[code])):
            found[code] = new
    return found


def _outline_cids(doc: "pymupdf.Document", xref: int, installed: tuple[Path, int],
                  junk: set[int]) -> dict[int, str]:
    """For a two-byte (Identity-H) font: code -> text for the glyphs in `junk`
    (glyph numbers in the embedded font) that the installed font has."""
    if not junk or doc.xref_get_key(xref, "Encoding")[1] != "/Identity-H":
        return {}
    refs = re.findall(r"(\d+) 0 R", doc.xref_get_key(xref, "DescendantFonts")[1])
    embedded = _embedded_outlines(doc, int(refs[0])) if refs else None
    if not embedded:
        return {}
    cid_font = int(refs[0])
    glyphs, order, scale, _names = embedded
    kind, value = doc.xref_get_key(cid_font, "CIDToGIDMap")
    if kind == "xref":  # two bytes per CID: its glyph number
        data = doc.xref_stream(int(value.split()[0]))
        cid_of = {struct.unpack(">H", data[i:i + 2])[0]: i // 2 for i in range(0, len(data) - 1, 2)}
    else:
        cid_of = {}
    widths = _cid_widths(doc, cid_font)
    kind, value = doc.xref_get_key(cid_font, "DW")
    default = float(value) if kind in ("int", "real") else 1000.0
    index = _outline_index(*installed)
    found = {}
    for gid in sorted(junk):
        if gid >= len(order):
            continue
        glyph = order[gid]
        if glyph not in glyphs:
            continue
        if glyph.startswith("cid") and glyph[3:].isdigit():  # CID-keyed CFF
            cid = int(glyph[3:])
        else:
            cid = cid_of.get(gid, gid) if cid_of else gid
        if (new := index.match(glyphs[glyph], glyphs, scale, widths.get(cid, default))):
            found[cid] = new
    return found


def _junk_glyphs(doc: "pymupdf.Document") -> dict[str, set[int]]:
    """Font name (with subset prefix) -> glyph numbers whose text is junk."""
    glyphs: dict[str, set[int]] = {}
    for page in doc:
        for span in page.get_texttrace():
            for c in span["chars"]:
                if _is_junk(chr(c[0])):
                    glyphs.setdefault(span["font"], set()).add(c[1])
    return glyphs


def _embedded_outlines(doc: "pymupdf.Document", font_xref: int):
    """(glyph set, glyph order, scale to 1/1000 em, code -> glyph name from the
    font's own cmap) for the font program embedded in a font dictionary, or
    None if there is none."""
    import io

    descriptor = doc.xref_get_key(font_xref, "FontDescriptor")
    if descriptor[0] != "xref":
        return None
    descriptor = int(descriptor[1].split()[0])
    for key in ("FontFile3", "FontFile2", "FontFile"):
        kind, value = doc.xref_get_key(descriptor, key)
        if kind != "xref":
            continue
        stream = int(value.split()[0])
        data = doc.xref_stream(stream)
        if key == "FontFile3" and doc.xref_get_key(stream, "Subtype")[1] != "/OpenType":
            from fontTools.cffLib import CFFFontSet

            cff = CFFFontSet()
            cff.decompile(io.BytesIO(data), None)
            top = cff[cff.fontNames[0]]
            return top.CharStrings, list(top.charset), top.FontMatrix[0] * 1000, {}
        if key == "FontFile":  # Type 1
            from fontTools.t1Lib import T1Font

            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "font.pfa"
                path.write_bytes(data)
                font = T1Font(str(path), kind="OTHER")
                glyphs = font.getGlyphSet()
            return glyphs, list(glyphs.keys()), font["FontMatrix"][0] * 1000, {}
        from fontTools.ttLib import TTFont

        font = TTFont(io.BytesIO(data))
        names = {}
        if "cmap" in font:
            for table in font["cmap"].tables:
                if (table.platformID, table.platEncID) in ((3, 0), (1, 0)):
                    for code, glyph in table.cmap.items():
                        names.setdefault(code & 0xFF, glyph)  # symbolic fonts use F0xx
        return font.getGlyphSet(), font.getGlyphOrder(), 1000 / font["head"].unitsPerEm, names
    return None


def _outline_measures(glyph, glyphs, scale: float) -> tuple[float, ...] | None:
    """Area (as its square root), centre, spread and bounding box of a glyph's
    outline in 1/1000 em; None for an empty glyph (a space)."""
    from fontTools.pens.boundsPen import BoundsPen
    from fontTools.pens.statisticsPen import StatisticsPen

    bounds = BoundsPen(glyphs)
    glyph.draw(bounds)
    if not bounds.bounds:
        return None
    stats = StatisticsPen(glyphset=glyphs)
    try:
        glyph.draw(stats)
        moments = (math.sqrt(abs(stats.area)), stats.meanX, stats.meanY,
                   stats.stddevX, stats.stddevY)  # area's sign is just the direction
    except ZeroDivisionError:  # no area (lines only)
        moments = (0, 0, 0, 0, 0)
    return tuple(v * scale for v in (*moments, *bounds.bounds))


class _OutlineIndex:
    """An installed font's glyphs by width, for `match`."""

    def __init__(self, path: Path, number: int):
        from fontTools.ttLib import TTFont

        font = TTFont(path, fontNumber=number, lazy=True)
        self.glyphs = font.getGlyphSet()
        self.scale = 1000 / font["head"].unitsPerEm
        self.chars = _glyph_chars(font)
        self.by_width: dict[int, list[str]] = {}
        for glyph, (advance, _lsb) in font["hmtx"].metrics.items():
            self.by_width.setdefault(round(advance * self.scale), []).append(glyph)
        self.measures: dict[str, tuple[float, ...] | None] = {}
        # The other fonts of a .ttc collection, which may give a code point to a
        # glyph this one draws but doesn't encode. Glyphs are paired by number:
        # glyph -> (text, that font's glyph set, its glyph name, its scale).
        self.siblings: dict[str, tuple[str, object, str, float]] = {}
        if Path(path).suffix.lower() == ".ttc":
            from fontTools.ttLib import TTCollection

            order = font.getGlyphOrder()
            for k, sibling in enumerate(TTCollection(path, lazy=True).fonts):
                theirs = sibling.getGlyphOrder()
                if k == number or len(theirs) != len(order):
                    continue
                glyphs, scale = sibling.getGlyphSet(), 1000 / sibling["head"].unitsPerEm
                chars = _glyph_chars(sibling)
                for gid, glyph in enumerate(theirs):
                    mine = order[gid]
                    if glyph in chars and mine not in self.chars:
                        self.siblings.setdefault(mine, (chars[glyph], glyphs, glyph, scale))

    def match(self, glyph, glyphs, scale: float, width: float) -> str | None:
        """The text of the installed glyph with this width and drawing, if any."""
        target = _outline_measures(glyph, glyphs, scale)
        scored = []
        for w in range(round(width) - 1, round(width) + 2):
            for candidate in self.by_width.get(w, ()):
                if candidate not in self.measures:
                    self.measures[candidate] = _outline_measures(
                        self.glyphs[candidate], self.glyphs, self.scale)
                m = self.measures[candidate]
                if (m is None) != (target is None):
                    continue
                d = 0 if m is None else max(abs(a - b) for a, b in zip(m, target))
                scored.append((d, candidate))
        if target is None:  # a space of the same width
            return " " if any(self.chars.get(g, "").isspace() for _, g in scored) else None
        if not scored or (best := min(d for d, _ in scored)) > OUTLINE_MATCH:
            return None
        texts = [text for d, g in scored if d <= best + 4
                 and (text := self.text(g)) and not _is_junk(text)]
        return min(texts, key=lambda t: [ord(c) for c in t]) if texts else None

    def text(self, glyph: str) -> str | None:
        """The glyph's text: from this font, else from a font of the same
        collection whose glyph of that name has the same outline."""
        if glyph in self.chars:
            return self.chars[glyph]
        if glyph not in self.siblings:
            return None
        text, glyphs, name, scale = self.siblings[glyph]
        mine = self.measures.get(glyph) or _outline_measures(self.glyphs[glyph], self.glyphs, self.scale)
        theirs = _outline_measures(glyphs[name], glyphs, scale)
        if mine and theirs and max(abs(a - b) for a, b in zip(mine, theirs)) <= 0.5:
            return text
        return None


@functools.cache
def _outline_index(path: Path, number: int) -> _OutlineIndex:
    return _OutlineIndex(path, number)


def _glyph_chars(font) -> dict[str, str]:
    """Glyph name -> text for an installed font: its cmap (lowest code point
    first), then glyphs reached from those through substitutions (alternates,
    ligatures, small caps) or the MATH table's taller and wider variants."""
    chars: dict[str, str] = {}
    for cp, glyph in sorted(font.getBestCmap().items()):
        chars.setdefault(glyph, chr(cp))
    links: list[tuple[tuple[str, ...], str]] = []  # (glyphs, glyph they become)
    if "GSUB" in font and font["GSUB"].table.LookupList:
        for lookup in font["GSUB"].table.LookupList.Lookup:
            for sub in lookup.SubTable:
                sub = getattr(sub, "ExtSubTable", sub)
                for a, b in (getattr(sub, "mapping", None) or {}).items():
                    if isinstance(b, str):  # single substitution (not multiple)
                        links.append(((a,), b))
                for a, bs in (getattr(sub, "alternates", None) or {}).items():
                    links += [((a,), b) for b in bs]
                for a, ligatures in (getattr(sub, "ligatures", None) or {}).items():
                    links += [((a, *lig.Component), lig.LigGlyph) for lig in ligatures]
    if "MATH" in font and (variants := font["MATH"].table.MathVariants):
        for kind in ("Vert", "Horiz"):
            coverage = getattr(variants, f"{kind}GlyphCoverage")
            constructions = getattr(variants, f"{kind}GlyphConstruction")
            for glyph, c in zip(coverage.glyphs if coverage else (), constructions or ()):
                links += [((glyph,), v.VariantGlyph) for v in c.MathGlyphVariantRecord or ()]
    for _ in range(2):  # chains: a -> a.alt -> a.alt.big
        for source, target in links:
            if target not in chars and all(g in chars for g in source):
                chars[target] = "".join(chars[g] for g in source)
    return chars


# Accents with their combining forms. A PDF font may hold them as separate
# glyphs next to the letter; the shape tells which accent a glyph is. Arrows
# are matched the same way (SYMBOLS).
ACCENTS = {"´": "́", "`": "̀", "¯": "̄", "˘": "̆", "ˆ": "̂",
           "˜": "̃", "¨": "̈", "ˇ": "̌", "˙": "̇", "˚": "̊",
           "˝": "̋", "¸": "̧", "˛": "̨"}
# Other glyphs identified by shape: the same character. − + = also keep a minus
# from passing for a macron.
SYMBOLS = "→←↑↓↔≈≠≤≥−+=∼"
# Ligatures identified by shape, and the text they are mapped to. fi and fl (ffi
# and ffl) are told apart by `_right_edge_step`: a step of at least
# LIGATURE_I_STEP em means i. Ligatures must be within LIGATURE_MAX_DISTANCE.
LIGATURES = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl"}
LIGATURE_PAIRS = {"ﬁ": "ﬂ", "ﬂ": "ﬁ", "ﬃ": "ﬄ", "ﬄ": "ﬃ"}
LIGATURE_I_STEP = 0.14
LIGATURE_MAX_DISTANCE = 0.8
# Side of the grid glyph shapes are compared on.
GRID = 32
# Glyphs are drawn at this size (pt = px at 72 dpi) on the baseline at this row of
# a 100 x 100 scratch page.
DRAW_SIZE, DRAW_BASELINE = 60, 60
# How far (in em) a glyph's ink may sit above or below a candidate's: a macron
# and a minus have the same shape and differ only in height. And how many times
# larger or smaller its ink may be (a period is not an arrow).
MAX_HEIGHT_SHIFT, MAX_SIZE_RATIO = 0.2, 2.0
# A combining mark that the PDF writes *before* its letter is mapped to
# MARK_BEFORE + (mark - U+0300) and moved behind the letter by `fix_prefix_accents`.
MARK_BEFORE = 0xE100


def add_shape_unicode_maps(doc: "pymupdf.Document", max_distance: float = 2.0,
                            min_margin: float = 0.5) -> int:
    """Give simple fonts whose accent and arrow glyphs have no Unicode a ToUnicode
    entry for each such glyph, and return the number of fonts changed.
    Ligatures (`LIGATURES`) are matched too and mapped to their letters.

    Each glyph without a usable mapping is drawn with the PDF's own font and
    compared (centre line against centre line) with the references from
    `_reference_shapes`; it is mapped only if the best match is within
    `max_distance` and at least `min_margin` closer than the next one. An
    accent written before its letter is mapped to a placeholder that
    `fix_prefix_accents` moves behind the letter."""
    seen, fixed = set(), 0
    todo: dict[int, tuple[str, dict[int, str]]] = {}  # font xref -> (name, unmapped codes)
    old_maps: dict[int, dict[int, str]] = {}
    for pno in range(doc.page_count):
        for xref, _ext, _type, basefont, _name, _enc in doc.get_page_fonts(pno):
            if xref in seen:
                continue
            seen.add(xref)
            if doc.xref_get_key(xref, "Subtype")[1] not in ("/TrueType", "/Type1"):
                continue
            old = {}
            kind, value = doc.xref_get_key(xref, "ToUnicode")
            if kind == "xref":
                old = _parse_to_unicode(doc.xref_stream(int(value.split()[0])).decode("latin-1"))
            codes = _unmapped_codes(doc, xref, old)
            if codes:
                todo[xref] = (basefont.split("+")[-1], codes)
                old_maps[xref] = old
    if not todo:
        return 0
    used = _used_codes(doc, {name for name, _ in todo.values()})
    refs = _reference_shapes()
    for xref, (name, codes) in todo.items():
        found = {}
        for code in codes:
            if code not in used.get(name, ()):
                continue  # never drawn in the text: nothing to repair
            match = _match_accent(doc, xref, code, refs, max_distance, min_margin)
            if match:
                found[code] = match
        if not found:
            continue
        before = _accents_come_first(doc, name, {c: a for c, a in found.items() if a in ACCENTS})
        mapping = dict(old_maps[xref])
        for code, shape in found.items():
            mark = ACCENTS.get(shape)
            if not mark:
                mapping[code] = LIGATURES.get(shape, shape)
            else:
                mapping[code] = chr(MARK_BEFORE + ord(mark) - 0x300) if before.get(code) else mark
        cmap_xref = doc.get_new_xref()
        doc.update_object(cmap_xref, "<<>>")
        doc.update_stream(cmap_xref, _to_unicode_cmap(mapping).encode())
        doc.xref_set_key(xref, "ToUnicode", f"{cmap_xref} 0 R")
        print(f"         font {name}: glyph(s) "
              + ", ".join(f"{c:#04x}={a}" for c, a in found.items()), file=sys.stderr)
        fixed += 1
    return fixed


def _used_codes(doc: "pymupdf.Document", fonts: set[str]) -> dict[str, set[int]]:
    """Per font name, the codes of glyphs MuPDF can't map that the text
    contains. With TEXT_CID_FOR_UNKNOWN_UNICODE it gives the code itself for
    them (a control character below 32, "!" for code 33), without it U+FFFD;
    the text is read both ways to tell. Only simple fonts with 8-bit codes
    are looked at."""
    used: dict[str, set[int]] = {}

    def chars(page: "pymupdf.Page", flags: int) -> list[tuple[str, str]]:
        return [(span["font"], c["c"]) for block in page.get_text("rawdict", flags=flags)["blocks"]
                for line in block.get("lines", ()) for span in line["spans"]
                for c in span["chars"]]

    keep = pymupdf.TEXT_PRESERVE_LIGATURES | pymupdf.TEXT_PRESERVE_WHITESPACE  # same chars both ways
    for page in doc:
        coded = chars(page, keep | pymupdf.TEXT_CID_FOR_UNKNOWN_UNICODE)
        plain = chars(page, keep)
        if len(plain) != len(coded):  # not expected; then only codes below 32 count
            plain = [("", "x")] * len(coded)
        for (font, c), (_, p) in zip(coded, plain):
            if font in fonts and ord(c) < 256 and c not in "\n\t\r" and (
                    ord(c) < 32 or _is_junk(p)):
                used.setdefault(font, set()).add(ord(c))
    return used


def _unmapped_codes(doc: "pymupdf.Document", xref: int, mapping: dict[int, str]) -> dict[int, str]:
    """Character codes a simple font draws (non-zero width) that its map and its
    glyph names don't turn into a real character: code -> glyph name or ''."""
    kind, value = doc.xref_get_key(xref, "FirstChar")
    first = int(value) if kind == "int" else 0
    kind, value = doc.xref_get_key(xref, "Widths")
    if kind == "xref":
        value = doc.xref_object(int(value.split()[0]))
    widths = [float(w) for w in re.findall(r"-?[\d.]+", value if kind != "null" else "")]
    names = {}
    kind, value = doc.xref_get_key(xref, "Encoding")
    if kind == "xref":
        value = doc.xref_get_key(int(value.split()[0]), "Differences")[1]
        code = 0
        for token in re.findall(r"/[^\s/\[\]]+|\d+", value or ""):
            if token.startswith("/"):
                names[code] = token[1:]
                code += 1
            else:
                code = int(token)
    codes = {}
    for i, width in enumerate(widths):
        code = first + i
        if not width or code > 255:
            continue
        text = mapping.get(code)
        if text is not None:
            if text and all(unicodedata.category(c) not in ("Cc", "Co", "Cn") and c != "�"
                            for c in text):
                continue
        elif names.get(code) and pymupdf.glyph_name_to_unicode(names[code]) not in (0, 0xFFFD):
            continue  # a standard glyph name; MuPDF reads it
        elif code >= 32 and code not in names:
            continue  # plain encoding (WinAnsi etc.)
        codes[code] = names.get(code, "")
    return codes


def _ink(pix: "pymupdf.Pixmap", size: int = GRID) -> list[float] | None:
    """A grey pixmap cropped to its ink and resampled to size x size (centred,
    aspect ratio kept): the share of dark pixels in each cell."""
    w, h, data = pix.width, pix.height, pix.samples
    dark = [(x, y) for y in range(h) for x in range(w) if data[y * w + x] < 128]
    if not dark:
        return None
    x0, x1 = min(d[0] for d in dark), max(d[0] for d in dark) + 1
    y0, y1 = min(d[1] for d in dark), max(d[1] for d in dark) + 1
    scale = size / max(x1 - x0, y1 - y0)
    tw, th = max(round((x1 - x0) * scale), 1), max(round((y1 - y0) * scale), 1)
    out = [0.0] * (size * size)
    ox, oy = (size - tw) // 2, (size - th) // 2
    for ty in range(th):
        for tx in range(tw):
            sx0, sx1 = x0 + tx * (x1 - x0) // tw, max(x0 + (tx + 1) * (x1 - x0) // tw, x0 + tx * (x1 - x0) // tw + 1)
            sy0, sy1 = y0 + ty * (y1 - y0) // th, max(y0 + (ty + 1) * (y1 - y0) // th, y0 + ty * (y1 - y0) // th + 1)
            cell = [data[y * w + x] < 128 for y in range(sy0, sy1) for x in range(sx0, sx1)]
            out[(oy + ty) * size + ox + tx] = sum(cell) / len(cell)
    return out


def _ink_place(pix: "pymupdf.Pixmap") -> tuple[float, float]:
    """Where a glyph's ink is, in em (drawn as in `_match_accent`): its middle
    above the baseline, and its larger side."""
    w, data = pix.width, pix.samples
    dark = [(x, y) for y in range(pix.height) for x in range(w) if data[y * w + x] < 128]
    if not dark:
        return 0.0, 0.0
    xs, ys = [d[0] for d in dark], [d[1] for d in dark]
    return ((DRAW_BASELINE - sum(ys) / len(ys)) / DRAW_SIZE,
            max(max(xs) - min(xs), max(ys) - min(ys)) / DRAW_SIZE)


def _skeleton(grid: list[float], size: int = GRID) -> list[tuple[int, int]]:
    """The centre line of an ink grid (Zhang-Suen thinning), as (x, y) cells.
    Thinning makes the comparison independent of stroke thickness: a macron is
    a thin bar in one font and a thick one in another."""
    img = [[1 if grid[y * size + x] > 0.3 else 0 for x in range(size)] for y in range(size)]

    def around(y: int, x: int) -> list[int]:  # clockwise from the cell above
        def at(j: int, i: int) -> int:
            return img[j][i] if 0 <= j < size and 0 <= i < size else 0
        return [at(y - 1, x), at(y - 1, x + 1), at(y, x + 1), at(y + 1, x + 1),
                at(y + 1, x), at(y + 1, x - 1), at(y, x - 1), at(y - 1, x - 1)]

    changed = True
    while changed:
        changed = False
        for step in (0, 1):
            remove = []
            for y in range(size):
                for x in range(size):
                    if not img[y][x]:
                        continue
                    p = around(y, x)
                    if not 2 <= sum(p) <= 6 or sum(p[i] == 0 and p[(i + 1) % 8] == 1
                                                     for i in range(8)) != 1:
                        continue
                    if step == 0 and (p[0] * p[2] * p[4] or p[2] * p[4] * p[6]):
                        continue
                    if step == 1 and (p[0] * p[2] * p[6] or p[0] * p[4] * p[6]):
                        continue
                    remove.append((y, x))
            for y, x in remove:
                img[y][x] = 0
            changed = changed or bool(remove)
    return [(x, y) for y in range(size) for x in range(size) if img[y][x]]


def _distance(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> float:
    """How far two centre lines are apart, in grid cells: the larger of the two
    mean distances from a point of one to the nearest point of the other."""
    if not a or not b:
        return float("inf")

    def mean(p, q):
        return sum(min(math.hypot(x - u, y - v) for u, v in q) for x, y in p) / len(p)

    return max(mean(a, b), mean(b, a))


@functools.cache
def _reference_shapes() -> dict[str, list[tuple[list[tuple[int, int]], tuple[float, float]]]]:
    """Each accent, symbol and ligature drawn in Times and Helvetica (the fonts
    built into PyMuPDF; ligatures also in italic): its centre line and `_ink_place`."""
    shapes: dict[str, list[tuple[list[tuple[int, int]], tuple[float, float]]]] = {}
    with pymupdf.open() as ref:
        for accent in [*ACCENTS, *SYMBOLS, *LIGATURES]:
            # italic ligatures differ too much from upright ones
            for font in ("tiro", "helv", "tiit", "heit") if accent in LIGATURES else ("tiro", "helv"):
                page = ref.new_page(width=100, height=100)
                writer = pymupdf.TextWriter(page.rect)
                writer.append((30, DRAW_BASELINE), accent, font=pymupdf.Font(font),
                              fontsize=DRAW_SIZE)
                writer.write_text(page)
                pix = page.get_pixmap(colorspace=pymupdf.csGRAY, alpha=False)
                ink = _ink(pix)
                if ink:
                    shapes.setdefault(accent, []).append((_skeleton(ink), _ink_place(pix)))
    return shapes


def _match_accent(doc: "pymupdf.Document", xref: int, code: int,
                  refs: dict[str, list[tuple[list[tuple[int, int]], tuple[float, float]]]],
                  max_distance: float,
                  min_margin: float) -> str | None:
    """Draw glyph `code` of font object `xref` and return the accent it matches."""
    page = doc.new_page(width=100, height=100)
    try:
        stream = doc.get_new_xref()
        doc.update_object(stream, "<<>>")
        doc.update_stream(stream, f"BT /F0 {DRAW_SIZE} Tf 30 {100 - DRAW_BASELINE} Td "
                                  f"<{code:02X}> Tj ET".encode())
        doc.xref_set_key(page.xref, "Resources", f"<< /Font << /F0 {xref} 0 R >> >>")
        page.set_contents(stream)
        pix = page.get_pixmap(colorspace=pymupdf.csGRAY, alpha=False)
        ink, (height, size) = _ink(pix), _ink_place(pix)
    finally:
        doc.delete_page(doc.page_count - 1)
    if not ink:
        return None
    skeleton = _skeleton(ink)
    # A candidate drawn at another height (macron vs minus) or size doesn't count.
    ranked = sorted((min((_distance(skeleton, r) for r, (h, z) in shapes
                          if abs(h - height) <= MAX_HEIGHT_SHIFT
                          and 1 / MAX_SIZE_RATIO <= size / z <= MAX_SIZE_RATIO),
                         default=float("inf")), shape)
                    for shape, shapes in refs.items())
    best, shape = ranked[0]
    if shape in LIGATURE_PAIRS:  # fi or fl: the centre lines are too alike to tell
        partner = LIGATURE_PAIRS[shape]
        ends_in_i = _right_edge_step(pix) >= LIGATURE_I_STEP
        shape = min(shape, partner) if ends_in_i else max(shape, partner)  # ﬁ < ﬂ, ﬃ < ﬄ
        ranked = [r for r in ranked if r[1] != partner]
    if shape in LIGATURES:
        max_distance = min(max_distance, LIGATURE_MAX_DISTANCE)
    clear = all(d - best >= min_margin for d, _ in ranked[1:])
    return shape if best <= max_distance and clear else None


def _right_edge_step(pix: "pymupdf.Pixmap") -> float:
    """The largest step to the left, in em, of a glyph's right edge going up
    from the baseline row by row. A ligature ending in i has one just above the
    i (the edge moves to the f), one ending in l doesn't."""
    w, data = pix.width, pix.samples
    right: dict[int, int] = {}
    for i, v in enumerate(data):
        if v < 128:
            right[i // w] = max(right.get(i // w, -1), i % w)
    steps = [right[y] - right[y - 1] for y in range(min(right) + 1, DRAW_BASELINE)
             if y in right and y - 1 in right] if right else []
    return max(steps, default=0) / DRAW_SIZE

# Phonetic, pi and TeX math fonts often put IPA letters and symbols in ASCII
# slots under the slot's standard name, so MuPDF reads a real-looking character.
# TeX's TIPA fonts have a fixed encoding (`TIPA_TO_UNICODE`), so no shapes are needed.
PI_FONT = re.compile(r"phonet|ipa|mathematicalpi|commpi|^texcm|^cmsy\d|^advp", re.I)
TIPA_FONT = re.compile(r"^(tex-)?[tx]ipa", re.I)
# What such a glyph may really be. 〈 〉 are drawn from Symbol and written as ⟨ ⟩.
PI_CANDIDATES = ("ːˑ†‡§©ɪɔəʃʰʲʷˠɛʊŋɡʒθðæɑʌɐɨʉɯɣʔʕɾɹʁʀβɸχçɲɟʎɬɮʂʐɳɖʈɽʧʤʦ〈〉"
                 "ɜɤɒɥɦɱʋɕʍʏøœɭɻʟɴɰłṇṃḷṛṭḍṣ" + SYMBOLS)
# Shapes that are never the answer: when one is closest, the glyph is left alone,
# and they don't count as runners-up. Brackets, rare letters that look like
# common ones, vowels with a tilde or macron, and l.
PI_DECOYS = "()[]{}|lɘɵɞʡʢɠʛɗɓħʜʙʑʝɫʄãõĩũẽāōīūē"
# Greek, from Symbol, as further decoys. Not in fonts named phon/ipa, where
# these shapes are IPA.
PI_GREEK_DECOYS = "αδζηικλμνξπρστυφψωΓΔΘΛΞΠΣΦΨΩ"
# Not in any built-in font, so drawn as these letters side by side.
PI_COMPOSED = {"ʧ": "tʃ", "ʤ": "dʒ", "ʦ": "ts"}
PI_ALIASES = {"〈": "⟨", "〉": "⟩"}
# Letters differ from their small-capital IPA forms (I, ɪ) only in size, so the
# glyph's height and size must agree more closely than for accents.
PI_HEIGHT_SHIFT, PI_SIZE_RATIO = 0.12, 1.4
# A candidate wins if it is within PI_MAX_DISTANCE, at least PI_MARGIN closer
# than the character the name claims, and PI_RUNNER_UP closer than the next.
PI_MAX_DISTANCE, PI_MARGIN, PI_RUNNER_UP = 1.5, 0.5, 0.25
# Added to the distance when glyph and reference differ in their number of
# pieces (`_parts`).
PI_PARTS_PENALTY = 0.6
# Greek fonts are left alone: Greek letters are not among the candidates.
PI_SKIP = re.compile(r"greek|^advphong", re.I)
# Only fonts named as phonetic may hide letters under digits; elsewhere oldstyle
# figures look like ɪ ʒ ɡ.
PHONETIC_FONT = re.compile(r"phon|ipa", re.I)


def add_pi_unicode_maps(doc: "pymupdf.Document") -> int:
    """Check each character that the text of a phonetic, pi or TeX math font
    (`PI_FONT`) uses against the glyph drawn for it, and give the font a
    ToUnicode entry where the glyph is clearly something else (`PI_CANDIDATES`).
    Return the number of fonts changed. The glyph is compared (centre lines,
    as in `add_shape_unicode_maps`) with the claimed character and with each
    candidate, drawn in Times, Helvetica and PyMuPDF's Droid Sans Fallback
    (which has the IPA letters). TIPA fonts get their encoding's table instead,
    superscript fonts modifier letters."""
    used: dict[str, set[str]] = {}
    for page in doc:
        for span in page.get_texttrace():
            name = span["font"].split("+")[-1]
            if PI_FONT.search(name):
                used.setdefault(name, set()).update(
                    chr(c[0]) for c in span["chars"] if not _is_junk(chr(c[0])))
    if not used:
        return 0
    seen, fixed = set(), 0
    for pno in range(doc.page_count):
        for xref, _ext, _type, basefont, _name, _enc in doc.get_page_fonts(pno):
            name = basefont.split("+")[-1]
            if xref in seen or name not in used or PI_SKIP.search(name):
                continue
            seen.add(xref)
            if doc.xref_get_key(xref, "Subtype")[1] not in ("/TrueType", "/Type1"):
                continue
            text = _extracted_text(doc, xref, list(range(256)))
            if TIPA_FONT.search(name):
                found = {code: TIPA_TO_UNICODE[code] for code, claimed in text.items()
                         if code in TIPA_TO_UNICODE and claimed != TIPA_TO_UNICODE[code]
                         and (code < 128 or unicodedata.category(TIPA_TO_UNICODE[code])
                              not in ("Ll", "Lu", "Lo") or not _is_accent_glyph(doc, xref, code))}
                text = {}
            else:
                found = {}
            raised = "superscript" in name.lower()
            glyphs = {code: _draw_glyph(doc, xref, code) for code, claimed in text.items()
                      if claimed in used[name] and claimed.isascii() and claimed.isalpha()}
            small_caps = _is_small_caps(glyphs, text)
            for code, claimed in text.items():
                # Pi fonts hide symbols under the names of WinAnsi characters.
                if code < 33 or claimed not in used[name] or claimed.isspace() \
                        or not claimed.encode("cp1252", "ignore") \
                        or small_caps and claimed.isascii() and claimed.islower():
                    continue
                if raised:  # PhoneticaSuperscript: j is ʲ; shapes can't tell 1 from ɪ here
                    new = _modifier_letter(claimed)
                else:
                    glyph = glyphs[code] if code in glyphs else _draw_glyph(doc, xref, code)
                    new = glyph and _match_pi_glyph(
                        glyph, claimed, PI_DECOYS + ("" if PHONETIC_FONT.search(name)
                                                     else PI_GREEK_DECOYS))
                    if new and claimed.isdigit() and new.isalpha() \
                            and not PHONETIC_FONT.search(name):
                        new = None
                if new:
                    found[code] = new
            if found:
                _add_to_unicode(doc, xref, found, 1, name)
                fixed += 1
    return fixed


def _draw_glyph(doc: "pymupdf.Document", xref: int, code: int):
    """Glyph `code` of font `xref`: its centre line, `_ink_place` and `_parts`,
    or None."""
    page = doc.new_page(width=100, height=100)
    try:
        stream = doc.get_new_xref()
        doc.update_object(stream, "<<>>")
        doc.update_stream(stream, f"BT /F0 {DRAW_SIZE} Tf 30 {100 - DRAW_BASELINE} Td "
                                  f"<{code:02X}> Tj ET".encode())
        doc.xref_set_key(page.xref, "Resources", f"<< /Font << /F0 {xref} 0 R >> >>")
        page.set_contents(stream)
        pix = page.get_pixmap(colorspace=pymupdf.csGRAY, alpha=False)
        ink = _ink(pix)
        return (_skeleton(ink), _ink_place(pix), _parts(pix)) if ink else None
    finally:
        doc.delete_page(doc.page_count - 1)


def _parts(pix: "pymupdf.Pixmap") -> int:
    """How many separate pieces of ink a glyph has (ː 2, ɪ 1, ō 2, ð 1), not
    counting specks under 2% of the ink."""
    w, h, data = pix.width, pix.height, pix.samples
    dark = {(x, y) for y in range(h) for x in range(w) if data[y * w + x] < 128}
    sizes = []
    while dark:
        todo = [dark.pop()]
        size = 0
        while todo:
            x, y = todo.pop()
            size += 1
            for p in ((x + i, y + j) for i in (-1, 0, 1) for j in (-1, 0, 1)):
                if p in dark:
                    dark.remove(p)
                    todo.append(p)
        sizes.append(size)
    return sum(size >= 0.02 * sum(sizes) for size in sizes)


def _is_accent_glyph(doc: "pymupdf.Document", xref: int, code: int) -> bool:
    """Whether the glyph sits high like an accent (ink middle above 0.5 em).
    Some TIPA subsets hold accents where T3 has letters above 127."""
    glyph = _draw_glyph(doc, xref, code)
    return bool(glyph) and glyph[1][0] > 0.5


def _is_small_caps(glyphs: dict, text: dict[int, str]) -> bool:
    """Whether most of a font's lowercase letters look like their capitals.
    Letters whose capital has the same shape (c o s v w x z) don't count."""
    votes = []
    for code, glyph in glyphs.items():
        char = text[code]
        if not glyph or not char.islower() or char in "cosvwxz":
            continue
        def distance(c):
            return min((_distance(glyph[0], r) for r, _p, _n in _pi_reference_shapes(c, PI_STYLES)),
                       default=float("inf"))
        votes.append(distance(char.upper()) < distance(char))
    return len(votes) >= 3 and sum(votes) >= 0.6 * len(votes)


def _modifier_letter(char: str) -> str | None:
    """The superscript form of a lowercase ASCII letter (j -> ʲ), if Unicode has one."""
    if not ("a" <= char <= "z"):
        return None
    for prefix in ("MODIFIER LETTER SMALL ", "SUPERSCRIPT LATIN SMALL LETTER "):
        with contextlib.suppress(KeyError):
            return unicodedata.lookup(prefix + char.upper())
    return None


def _match_pi_glyph(glyph, claimed: str, decoys: str = PI_DECOYS) -> str | None:
    """What a glyph (`_draw_glyph`) really is, if clearly not `claimed`."""
    skeleton, (height, size), parts = glyph

    def distance(char: str) -> float:
        return min((_distance(skeleton, r) + (n not in (parts, None)) * PI_PARTS_PENALTY
                    for r, (h, z), n in _pi_reference_shapes(char)
                    if abs(h - height) <= PI_HEIGHT_SHIFT
                    and 1 / PI_SIZE_RATIO <= size / z <= PI_SIZE_RATIO),
                   default=float("inf"))

    # The claimed character also in italic and bold.
    claimed_distance = min(distance(claimed), min(
        (_distance(skeleton, r) for r, (h, z), _n in _pi_reference_shapes(claimed, PI_STYLES)
         if abs(h - height) <= PI_HEIGHT_SHIFT and 1 / PI_SIZE_RATIO <= size / z <= PI_SIZE_RATIO),
        default=float("inf")))
    if claimed_distance <= PI_MARGIN:  # nothing can win by the margin: skip the candidates
        return None
    ranked = sorted((distance(c), c) for c in PI_CANDIDATES + decoys if c != claimed)
    best, char = ranked[0]
    second = next(d for d, c in ranked[1:] if c not in decoys)
    # Small capitals: a lowercase letter drawn as its capital (i as I) is not ɪ.
    if claimed.isascii() and claimed.islower() and char.isalpha() and min(
            (_distance(skeleton, r) for r, _p, _n in
             _pi_reference_shapes(claimed.upper(), PI_STYLES)), default=float("inf")) <= best:
        return None
    if char not in decoys and best <= PI_MAX_DISTANCE \
            and best <= claimed_distance - PI_MARGIN and second - best >= PI_RUNNER_UP:
        return PI_ALIASES.get(char, char)
    return None


# IPA letters that are Latin letters turned or mirrored, so a serif form of them
# can be drawn from Times.
PI_TURNED = {"ɔ": ("c", "mirror"), "ə": ("e", "turn"), "ɐ": ("a", "turn"),
             "ɯ": ("m", "turn"), "ɹ": ("r", "turn")}


# Fonts for the claimed character and small-capital checks: Times and Helvetica,
# regular, italic and bold.
PI_STYLES = ("tiro", "helv", "tiit", "heit", "tibo", "hebo")


@functools.cache
def _pi_reference_shapes(char: str, fonts: tuple[str, ...] = ("tiro", "helv", "cjk", "symbol")) -> list[tuple[list[tuple[int, int]], tuple[float, float], int | None]]:
    """`char` drawn in each built-in font that has it (else composed: `PI_COMPOSED`,
    dot-below letters), plus its turned or mirrored Latin letter (`PI_TURNED`):
    centre line, `_ink_place` and `_parts` (None for composed letters)."""
    shapes = []
    base, how = PI_TURNED.get(char, (None, None))
    # No built-in font has the dot-below letters (ṇ ṭ): drawn as letter + period.
    letter, mark = (unicodedata.normalize("NFD", char) + " ")[:2]
    with pymupdf.open() as ref:
        for name in fonts:
            font = pymupdf.Font(name)
            for c in (char, base):
                dot = c == char and mark == "\u0323" and not font.has_glyph(ord(c))
                if dot:
                    c = letter
                elif c == char and c in PI_COMPOSED and not font.has_glyph(ord(c)):
                    c = PI_COMPOSED[c]
                if not c or not all(font.has_glyph(ord(x)) for x in c):
                    continue
                page = ref.new_page(width=100, height=100)
                writer = pymupdf.TextWriter(page.rect)
                writer.append((30, DRAW_BASELINE), c, font=font, fontsize=DRAW_SIZE)
                if dot:
                    x = 30 + (font.text_length(c, DRAW_SIZE) - font.text_length(".", DRAW_SIZE)) / 2
                    writer.append((x, DRAW_BASELINE + DRAW_SIZE / 4), ".", font=font,
                                  fontsize=DRAW_SIZE)
                writer.write_text(page)
                pix = page.get_pixmap(colorspace=pymupdf.csGRAY, alpha=False)
                ink = _ink(pix)
                if not ink:
                    continue
                if c == base:
                    rows = [ink[y * GRID:(y + 1) * GRID][::-1] for y in range(GRID)]
                    ink = [v for row in (rows[::-1] if how == "turn" else rows) for v in row]
                # Letters drawn side by side (ʧ as tʃ) may touch in the real glyph.
                parts = None if c == PI_COMPOSED.get(char) else _parts(pix)
                shapes.append((_skeleton(ink), _ink_place(pix), parts))
    return shapes


def _accents_come_first(doc: "pymupdf.Document", font: str, found: dict[int, str],
                        max_samples: int = 40) -> dict[int, bool]:
    """For each accent code: is it written before its letter? Decided by which
    neighbour, the next or the previous character, it overlaps more in the text."""
    votes = {code: [0, 0] for code in found}  # [before, after]
    for page in doc:
        if all(sum(v) >= max_samples for v in votes.values()):
            break
        for block in page.get_text("rawdict")["blocks"]:
            for line in block.get("lines", ()):
                chars = [(c, span["font"]) for span in line["spans"] for c in span["chars"]]
                for i, (c, f) in enumerate(chars):
                    if f != font or len(c["c"]) != 1 or ord(c["c"]) not in votes:
                        continue
                    x0, x1 = c["bbox"][0], c["bbox"][2]
                    def overlap(other):
                        o = min(x1, other["bbox"][2]) - max(x0, other["bbox"][0])
                        return o / max(min(x1 - x0, other["bbox"][2] - other["bbox"][0]), 1e-6)
                    nxt = overlap(chars[i + 1][0]) if i + 1 < len(chars) else 0
                    prv = overlap(chars[i - 1][0]) if i else 0
                    if nxt != prv:
                        votes[ord(c["c"])][0 if nxt > prv else 1] += 1
    # No evidence means after, Unicode's order.
    return {code: v[0] > v[1] for code, v in votes.items()}


def fix_prefix_accents(text: str) -> str:
    """Put accents that the PDF writes before their letter (see
    `add_shape_unicode_maps`) behind it, composing é etc. A dotless ı or ȷ
    under an accent becomes i or j, since the accent replaces the dot."""
    def move(m: re.Match) -> str:
        mark = chr(ord(m["mark"]) - MARK_BEFORE + 0x300)
        letter = m["letter"]
        if mark not in "\u0327\u0328":  # accents above replace the dot
            letter = {"ı": "i", "ȷ": "j"}.get(letter, letter)
        return m["style"] + unicodedata.normalize("NFC", letter + mark)

    text = re.sub(r"(?P<mark>[\ue100-\ue16f])(?P<style>[*_`]*)(?P<letter>[^\W\d_])", move, text)
    # An accent with nothing to sit on: keep it as a plain combining mark.
    return re.sub(r"[\ue100-\ue16f]", lambda m: chr(ord(m.group()) - MARK_BEFORE + 0x300), text)


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
             # Microsoft Office for Mac keeps its fonts (Cambria, Calibri ...) to itself
             "/Applications/Microsoft Word.app/Contents/Resources/DFonts",
             "/Applications/Microsoft PowerPoint.app/Contents/Resources/DFonts",
             "/Applications/Microsoft Excel.app/Contents/Resources/DFonts",
             r"C:\Windows\Fonts", "~/AppData/Local/Microsoft/Windows/Fonts",
             "/usr/share/fonts", "/usr/local/share/fonts", "~/.local/share/fonts", "~/.fonts")


@functools.cache
def system_fonts() -> dict[str, tuple[Path, int]]:
    """PostScript name -> (file, number of the font in a .ttc collection),
    for the .ttf/.otf/.ttc fonts installed here."""
    found: dict[str, tuple[Path, int]] = {}
    for d in FONT_DIRS:
        root = Path(d).expanduser()
        if not root.is_dir():
            continue
        for f in root.rglob("*"):
            if f.suffix.lower() in (".ttf", ".otf", ".ttc"):
                for number, name in enumerate(_postscript_names(f)):
                    if name:
                        found.setdefault(name, (f, number))
    return found


def _font_data(path: Path, number: int) -> bytes:
    """The font file, or one font out of a .ttc collection."""
    if path.suffix.lower() != ".ttc":
        return path.read_bytes()
    import io

    from fontTools.ttLib import TTFont

    out = io.BytesIO()
    TTFont(path, fontNumber=number).save(out)
    return out.getvalue()


def _postscript_names(path: Path) -> list[str | None]:
    """Name ID 6 of each font in a font file (several in a .ttc collection)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
            if head[:4] == b"ttcf":
                n = struct.unpack(">I", head[8:12])[0]
                offsets = struct.unpack(f">{n}I", fh.read(4 * n))
            else:
                offsets = (0,)
            return [_postscript_name(fh, offset) for offset in offsets]
    except (OSError, struct.error):
        return []


def _postscript_name(fh, start: int) -> str | None:
    """Name ID 6 from the name table of the font whose directory is at `start`."""
    try:
        fh.seek(start)
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
    # ignore_images: otherwise a scan on a widened page counts as a figure and
    # the text over it is dropped.
    kwargs = {"ignore_alpha": True, "ignore_images": True, "page_chunks": True,
              "show_progress": False}
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
MIN_EDGE_PIECE = 20  # shortest piece of a repeated header/footer line that is removed too


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

    def is_repeated(line: str, bottom: bool) -> bool:
        # At the bottom, a long piece of a repeated line counts too, for stamps
        # split over two lines on some pages. Not at the top or as a heading,
        # where a split title can look like pieces of the running head.
        key = edge_key(line)
        if key in repeated:
            return True
        return (bottom and len(key) >= MIN_EDGE_PIECE and not line.lstrip().startswith("#")
                and any(key in r for r in repeated))

    removed = 0
    out = []
    for pno, lines in enumerate(split):
        bottom = set([i for i, l in enumerate(lines) if l.strip()][-edge:])
        drop = {i for i in edge_indices(lines, pno == 0) if is_repeated(lines[i], i in bottom)}
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
    elif has_outlined_text(pdf):
        # Text drawn as outlines: pymupdf4llm's output scores well but is one
        # "picture text" block per page, so OCR the rendered pages first.
        methods.insert(0, methods.pop())
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
        texts = [fix_prefix_accents(p[1]) for p in pages]
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
    unreadable = sum(p.quality.unreadable for p in parts)
    if unreadable:
        # Below the review threshold, but a reader should know the text has holes.
        lines.append(f"unreadable_characters: {unreadable}  # U+FFFD / control codes from broken font encodings")
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
    unreadable_files, missing_fonts = [], set()
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
        if any(p.quality.unreadable for p in parts):
            unreadable_files.append(name)
            for pdf in job["pdfs"]:
                missing_fonts |= MISSING_FONTS.get(str(pdf), set())

        if quality.score >= MIN_SCORE:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(content, encoding="utf-8")
            # Drop a stale copy from an earlier run that needed review.
            review_out.unlink(missing_ok=True)
            index.set_converted(keys, paths, rel)
            done.append(name)
            note = (f"  ({sum(p.quality.unreadable for p in parts)} unreadable characters)"
                    if any(p.quality.unreadable for p in parts) else "")
            print(f"ok       {name}  [{quality.score}]{note}", file=sys.stderr)
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
    if unreadable_files:
        print(f"{len(unreadable_files)} file(s) with unreadable characters"
              " (see unreadable_characters in the file header)")
    if missing_fonts:
        names = sorted(missing_fonts)
        print("installing these fonts may fix them: " + ", ".join(names[:4])
              + (f" (+{len(names) - 4} more)" if len(names) > 4 else ""))
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

# TIPA's T3 encoding (TeX fonts tipa*, xipa*) -> Unicode, for add_pi_unicode_maps().
# From TeX Live's tipa/t3enc.def (code -> command) and
# xunicode.sty (command -> Unicode), with Ɣ Ʊ ͐ corrected to ɣ ʊ ˃ and ȷ ꜜ ꜛ
# added. Accents, 0x20 (ʹ, but read as a space) and codes that are their own
# character are left out.
TIPA_TO_UNICODE = {
    0x19: 'ı', 0x1A: 'ȷ', 0x22: 'ˈ', 0x30: 'ʉ', 0x31: 'ɨ', 0x32: 'ʌ', 0x33: 'ɜ',
    0x34: 'ɥ', 0x35: 'ɐ', 0x36: 'ɒ', 0x37: 'ɤ', 0x38: 'ɵ', 0x39: 'ɘ', 0x3A: 'ː', 0x3B: 'ˑ',
    0x40: 'ə', 0x41: 'ɑ', 0x42: 'β', 0x43: 'ɕ', 0x44: 'ð', 0x45: 'ɛ', 0x46: 'ɸ', 0x47: 'ɣ',
    0x48: 'ɦ', 0x49: 'ɪ', 0x4A: 'ʝ', 0x4B: 'ʁ', 0x4C: 'ʎ', 0x4D: 'ɱ', 0x4E: 'ŋ', 0x4F: 'ɔ',
    0x50: 'ʔ', 0x51: 'ʕ', 0x52: 'ɾ', 0x53: 'ʃ', 0x54: 'θ', 0x55: 'ʊ', 0x56: 'ʋ', 0x57: 'ɯ',
    0x58: 'χ', 0x59: 'ʏ', 0x5A: 'ʒ', 0x5C: 'ʽ', 0x5E: '˺', 0x5F: '˹', 0x67: 'ɡ', 0x7B: 'ǁ',
    0x7C: 'ǀ', 0x7D: 'ǂ', 0x7E: '˞', 0x7F: 'ˌ', 0x92: 'ǀ', 0x93: '‖', 0x94: 'ꜜ', 0x95: 'ꜛ',
    0x96: '↖', 0x97: '↘', 0xA0: 'ƀ', 0xA1: 'đ', 0xA2: 'ȡ', 0xA3: 'ᶑ', 0xA4: 'ᴇ', 0xA5: 'g',
    0xA6: 'ƪ', 0xA7: 'ʅ', 0xA8: 'ᴊ', 0xA9: 'ʞ', 0xAA: 'ł', 0xAB: 'λ', 0xAC: 'ƛ', 0xAD: 'ɮ',
    0xAE: 'ȵ', 0xB0: 'ω', 0xB2: 'ʆ', 0xB3: 'ƫ', 0xB4: 'ȶ', 0xB5: 'ʦ', 0xB6: 'ʮ', 0xB7: 'ʯ',
    0xB8: 'ʓ', 0xB9: 'ƹ', 0xBA: 'Ь', 0xBB: 'Ъ', 0xBC: 'ˀ', 0xBD: '˂', 0xBE: '˃', 0xC0: 'ᴀ',
    0xC1: 'ƈ', 0xC2: 'ʗ', 0xC3: 'ʤ', 0xC4: 'ɚ', 0xC5: 'ʚ', 0xC6: 'ɞ', 0xC7: 'ɝ', 0xC8: 'ɣ',
    0xC9: 'ʛ', 0xCA: 'ɧ', 0xCB: 'ʜ', 0xCC: 'ɩ', 0xCD: 'ɟ', 0xCE: 'ƙ', 0xCF: 'ʟ', 0xD0: 'ɮ',
    0xD1: 'ɷ', 0xD2: 'ƥ', 0xD3: 'ɋ', 0xD4: 'ɼ', 0xD5: 'ɺ', 0xD6: 'ƭ', 0xD7: 'Œ', 0xD8: 'ʇ',
    0xD9: 'ʧ', 0xDA: 'ᴜ', 0xDB: 'ʖ', 0xDC: 'ʡ', 0xDD: 'ʢ', 0xDE: 'ȥ', 0xDF: 'Ƿ', 0xE0: 'ʙ',
    0xE1: 'ɓ', 0xE2: 'ɗ', 0xE3: 'ɖ', 0xE4: 'ɠ', 0xE5: 'ɢ', 0xE8: 'ħ', 0xE9: 'ɟ', 0xEA: 'ʄ',
    0xEB: 'ɫ', 0xEC: 'ɬ', 0xED: 'ɭ', 0xEE: 'ɰ', 0xEF: 'ɳ', 0xF0: 'ɴ', 0xF1: 'ɲ', 0xF2: 'ʘ',
    0xF3: 'ɽ', 0xF4: 'ɹ', 0xF5: 'ɻ', 0xF6: 'ʀ', 0xF7: 'œ', 0xF9: 'ʂ', 0xFA: 'ʈ', 0xFB: 'ʍ',
    0xFC: 'ʐ', 0xFD: 'ʑ', 0xFF: 'ƕ',
}


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
