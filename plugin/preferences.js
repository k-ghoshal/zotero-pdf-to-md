// preferences.js — the "Choose…" buttons of the settings pane (the fields are
// bound to prefs by their `preference` attribute).
//
// Pane scripts run in a sandbox, so inline oncommand="…" handlers can't see
// these globals. Buttons carry data-pick="<pref key>" (and data-folder="true")
// and one document listener handles them.

var PdfToMdPrefs = {
  async pick(key, folder) {
    const pref = "extensions.pdf-to-md." + key;
    const { FilePicker } = ChromeUtils.importESModule(
      "chrome://zotero/content/modules/filePicker.mjs"
    );
    const fp = new FilePicker();
    fp.init(
      window,
      folder ? "Choose folder" : "Choose file",
      folder ? fp.modeGetFolder : fp.modeOpen
    );
    const current = Zotero.Prefs.get(pref, true);
    if (current) {
      try {
        fp.displayDirectory = folder ? current : PathUtils.parent(current);
      } catch (e) {}
    }
    if ((await fp.show()) !== fp.returnOK) return;

    Zotero.Prefs.set(pref, fp.file, true);
    const input = document.getElementById("pdf-to-md-pref-" + key);
    if (input) input.value = fp.file;
  },

  onCommand(event) {
    const button = event.target.closest && event.target.closest("[data-pick]");
    if (!button) return;
    PdfToMdPrefs.pick(
      button.getAttribute("data-pick"),
      button.getAttribute("data-folder") === "true"
    ).catch((e) => Zotero.logError("PdfToMd file picker: " + e));
  },
};

document.addEventListener("command", PdfToMdPrefs.onCommand);
// Remove the listener when the pane is unloaded.
document.addEventListener(
  "unload",
  (event) => {
    if (event.target.id === "pdf-to-md-prefs") {
      document.removeEventListener("command", PdfToMdPrefs.onCommand);
    }
  },
  true
);
