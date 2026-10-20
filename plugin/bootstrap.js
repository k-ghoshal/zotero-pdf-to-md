// bootstrap.js — lifecycle hooks Zotero calls for the PDF to Markdown plugin.
// Zotero registers the .ftl files in locale/; each window still needs the
// file linked before menus use it.

var PdfToMd;

const FTL_FILE = "pdf-to-md.ftl";

function install() {}

// Called after Zotero has initialized, at Zotero start and on install/enable.
function startup({ id, rootURI }) {
  Services.scriptloader.loadSubScript(rootURI + "pdf-to-md.js");
  PdfToMd.init({ id, rootURI });
  // Windows that were already open don't get onMainWindowLoad.
  for (const window of Zotero.getMainWindows()) onMainWindowLoad({ window });
}

function onMainWindowLoad({ window }) {
  window.MozXULElement.insertFTLIfNeeded(FTL_FILE);
}

function onMainWindowUnload({ window }) {}

// Called on Zotero quit and when the plugin is disabled, upgraded or removed.
function shutdown() {
  PdfToMd?.shutdown();
  PdfToMd = undefined;
  for (const window of Zotero.getMainWindows()) {
    window.document.querySelector(`link[href="${FTL_FILE}"]`)?.remove();
  }
}

function uninstall() {}
