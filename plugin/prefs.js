// Default preferences. Change them in Settings → PDF to Markdown or the Config Editor.

// Python that already has the packages. Empty = uv, else a private venv.
pref("extensions.pdf-to-md.pythonPath", "");
// Folder the Markdown files go to (--out). Required; empty = conversion asks for one.
pref("extensions.pdf-to-md.outputDir", "");
// Remove headers/footers that repeat on most pages.
pref("extensions.pdf-to-md.stripHeaders", true);
// Items with several PDFs: "folder" (subfolder per item), "flat" (item-name prefix) or "merge".
pref("extensions.pdf-to-md.multiMode", "folder");
// Replace Markdown files that already exist; false = skip those PDFs.
pref("extensions.pdf-to-md.overwrite", false);
// Put "[PDF page N · printed p. X]" at the start of each page in the Markdown.
pref("extensions.pdf-to-md.pageMarkers", true);
// Tesseract language(s) for OCR, e.g. "eng+deu".
pref("extensions.pdf-to-md.ocrLanguage", "eng");
// Write tables as HTML instead of Markdown (keeps merged cells).
pref("extensions.pdf-to-md.htmlTables", false);
// Repair fonts whose symbols copy out as the wrong characters (Wingdings ☞ as "F", → as "à").
pref("extensions.pdf-to-md.repairSymbols", true);
// Name files for items with one PDF from the item's details ("Author - Year - Title")
// instead of the PDF's name.
pref("extensions.pdf-to-md.metadataNames", true);
// Keep the progress popup above all windows; a click hides it during a run.
pref("extensions.pdf-to-md.keepOnTop", false);
