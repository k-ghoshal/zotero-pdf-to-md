# PDF to Markdown for Zotero

A Zotero plugin that converts the PDFs of selected items into Markdown files, using
[pymupdf4llm](https://github.com/pymupdf/pymupdf4llm) with [pdfplumber](https://github.com/jsvine/pdfplumber) as a fallback.

Select items → right-click → **📝 PDF to Markdown**. Good conversions go to your output folder,
doubtful ones to `_needs_review/`.

- Reads scans with an existing OCR layer, and OCRs scans without one (needs Tesseract).
- Scores each result; garbled text goes to `_needs_review/`.
- Strips running headers, footers and page numbers.
- Handles books with one PDF per chapter.
- Installs its own Python packages on first use.
- Runs locally. PDFs are never uploaded, moved or modified.

## Requirements

- Zotero 9 or 10.
- [uv](https://docs.astral.sh/uv/) (recommended) or Python 3. The packages are installed on first use.
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- Optional: [Tesseract](https://github.com/tesseract-ocr/tesseract) for scans with no text layer,
  plus `tesseract-lang` for languages other than English.
  ```bash
  brew install tesseract tesseract-lang
  ```

Tested on macOS. Windows and Linux are untested.

## Installation

1. Download `pdf-to-md.xpi` from [Releases](https://github.com/k-ghoshal/zotero-pdf-to-md/releases).
2. In Zotero: **Tools → Plugins** → gear icon → **Install Plugin From File…** → select the `.xpi`.
3. In **Zotero → Settings → PDF to Markdown**, choose an **output folder**.

## Usage

Select items or PDF attachments, then right-click → **📝 PDF to Markdown**.

A popup lists the files while they convert, with a spinner on the current one and progress in its
title (e.g. "3 of 7 · 1:42"). Finished files are marked ✓ (converted or already done), ⚠ (needs
review) or ✗ (failed). At the end it shows a summary:

```
3 converted → /Users/you/output-folder
1 skipped (already converted)
needs review: Some Scan.pdf: almost no text (0 chars/page) - likely a scan, needs OCR
```

Conversions started during a run are queued. The first run is slower while packages install; long
scans with OCR can take a minute or two.

If the popup goes behind other windows, **Tools → PDF to Markdown: Show Progress** (or
**📝 Show Conversion Progress** in the item context menu, during a run) brings it back. After a run,
it shows the last summary again.

### Item pane

Items with PDFs get a **PDF to Markdown** section in the item pane, showing each output file's state:

```
✓ 01 - Chapter 1 — converted
⚠ 02 - Chapter 2 — needs review
– 03 - Chapter 3 — not converted
```

During a run it shows "converting…" or "queued". The section header has two buttons, also
available from the **Locate** button's drop-down in the side strip:

- **Convert:** converts the item (or, from Locate, the whole library selection).
- **Show in output folder:** reveals the item's Markdown file, or opens the output folder.

### Output

| Item | File |
|---|---|
| One PDF | `Author - Year - Title.md` (or the PDF's name, if that setting is off or the PDF has no parent item) |
| Several PDFs | see below |
| Failed the quality check | same name under `_needs_review/` |

Items with several PDFs (e.g. a book with chapters):

| Setting | Result |
|---|---|
| **Subfolder** (default) | `Smith - 2020 - A Book/01 - Chapter 1.md`, … |
| **No subfolder** | `Smith - 2020 - A Book - 01 - Chapter 1.md`, … |
| **Merge into one file** | `Smith - 2020 - A Book.md`, one section per PDF |

Item names follow your Zotero file-naming format. Chapters are numbered in attachment-title order.

Each file starts with a header:

```yaml
---
title: "This is the Name of a Real Paper"
authors: ["Day, Beautiful", "Clouds, Storm"]
year: 2017
item_type: "conferencePaper"
publication: "Proceedings of the 34th West Coast Conference on Weather"
zotero: "zotero://select/library/items/ABCD1234"
attachment_keys: ["EFGH5678"]
source: "/Users/you/Zotero/storage/EFGH5678/Day and Storm - 2017 - This is the Name of a Real Paper.pdf"
converted: 2026-10-16
method: pymupdf4llm
quality: 1.0
header_footer_lines_removed: 42
page_markers: "[PDF page N] starts each page; 'printed p. X' is the number printed on it, where known"
---

[PDF page 1 · printed p. 283]

# First line of the paper …
```

- **Page markers:** each page starts with its PDF page number and, where found, its printed page number.
- **Re-converting:** files that already exist are skipped unless **Overwrite** is on. Files in
  `_needs_review/` are always retried.
- **Matching:** files are matched to PDFs by `attachment_keys`, so renamed PDFs or changed titles
  don't create duplicates. A different paper with the same name is saved as `… (2).md`.

## Settings

**Zotero → Settings → PDF to Markdown**

| Setting | Default | |
|---|---|---|
| Output folder | none (required) | Where Markdown files are written. |
| Name files from item details | on | "Author - Year - Title" instead of the PDF's name, for items with one PDF. |
| Remove repeating headers and footers | on | Strips lines repeated at the top or bottom of most pages. |
| Overwrite Markdown files that already exist | off | When off, already converted PDFs are skipped. |
| Handling items with several PDFs | Subfolder | Subfolder, no subfolder, or merge into one file. |

**Advanced:**

| Setting | Default | |
|---|---|---|
| Mark the start of each page | on | Adds `[PDF page N · printed p. X]` markers. |
| Repair broken symbol encodings | on | Fixes Wingdings symbols such as ☞ and → that otherwise come out as "F" and "à". |
| Write tables as HTML | off | Keeps merged cells in complex tables. |
| OCR language(s) | `eng` | Tesseract languages, e.g. `eng+deu`. |
| Keep the progress popup on top | off | The popup stays above all windows, including other apps' windows; click it to hide it during a run. |
| Python interpreter | empty | Empty = managed by the plugin. A custom Python needs `pymupdf4llm<2` and `pdfplumber`. |

## How it works

For each PDF, the bundled script [`plugin/pdf_to_md.py`](plugin/pdf_to_md.py):

1. Converts with pymupdf4llm (reading an existing OCR layer if there is one, otherwise OCRing pages
   without text).
2. Scores the result from 0 to 1 (real words, broken encodings, run-together words, empty pages).
3. Below 0.6, also tries pdfplumber, then OCR of the rendered pages (needs Tesseract; two-page
   landscape sheets are split first), and keeps the best result.
4. Strips headers and footers and writes to the output folder or `_needs_review/`.

It also repairs Wingdings symbols and fonts with no Unicode map, removes download stamps that stop
scans from being OCRed, and restores the text of examples and small tables that the
layout detection mislabels as formulas.

The script runs without Zotero too:

```bash
uv run plugin/pdf_to_md.py --out ~/pdf-to-md ~/Downloads/papers/*.pdf
```

See `--help` for options.

## Troubleshooting

- **Errors:** turn on **Help → Debug Output Logging**, convert again, then **View Output** and look
  for lines starting with `PdfToMd`.
- **Scan in `_needs_review/` with "almost no text":** install Tesseract.
- **"not found (not synced/downloaded?)":** open the PDF in Zotero once to download it.
- **"Neither uv nor Python 3 was found":** install uv, or set **Python interpreter** under Advanced.

## Building from source

Raise `version` in `plugin/manifest.json`, then:

```bash
cd plugin && zip -r ../pdf-to-md.xpi manifest.json bootstrap.js pdf-to-md.js prefs.js preferences.xhtml preferences.js preferences.css pdf_to_md.py icon.svg icon-16.svg icon-20.svg locale
```

Zotero only replaces an installed plugin with a higher version.

## License

[GNU AGPL v3.0](LICENSE) or later. Copyright © 2026 Kaustubh Ghoshal.
