// pdf-to-md.js — the plugin's logic, loaded by bootstrap.js on startup.
// Adds the item context menu entry, Locate menu entries, an item pane
// section and a settings pane.

PdfToMd = {
  id: null, // plugin ID from manifest.json
  rootURI: null, // where the plugin's files are, ending in "/"
  menuID: null, // returned by MenuManager.registerMenu, used to remove it
  locateMenuID: null, // same, for the entries in the item pane's Locate menu
  toolsMenuID: null, // same, for "Show Progress" in the Tools menu
  sectionID: null, // returned by ItemPaneManager.registerSection
  prefsPaneID: null, // returned by PreferencePanes.register, to open the pane
  sectionRefreshers: new Map(), // section body -> its refresh(), for after a run
  currentRun: null, // the conversion in progress (see handleConvertCommand)
  lastRun: null, // the last finished run, for Show Progress afterwards

  init({ id, rootURI }) {
    this.id = id;
    this.rootURI = rootURI;
    this.addMenu();
    this.addLocateMenu();
    this.addSection();
    this.registerPrefsPane();
    this.installScript().catch((e) =>
      Zotero.logError("PdfToMd script copy: " + e)
    );
  },

  shutdown() {
    // Zotero removes the menu by itself when the plugin is disabled or
    // uninstalled, but not on an upgrade, so remove it here every time.
    if (this.menuID) Zotero.MenuManager.unregisterMenu(this.menuID);
    if (this.locateMenuID) Zotero.MenuManager.unregisterMenu(this.locateMenuID);
    if (this.toolsMenuID) Zotero.MenuManager.unregisterMenu(this.toolsMenuID);
    this.menuID = this.locateMenuID = this.toolsMenuID = null;
    if (this.sectionID) Zotero.ItemPaneManager.unregisterSection(this.sectionID);
    this.sectionID = null;
    this.sectionRefreshers.clear();
    // A run in progress: its script keeps going, but the popup can't update.
    this.currentRun?.stopTimer?.();
    this.currentRun?.view?.pw.close();
    this.lastRun?.view?.pw.close();
  },

  // The context menu of library items: Convert, and during a run "Show
  // Conversion Progress". Also "Show Progress" in the Tools menu. Labels are
  // in locale/en-US/pdf-to-md.ftl.
  addMenu() {
    this.menuID = Zotero.MenuManager.registerMenu({
      menuID: "pdf-to-md",
      pluginID: this.id,
      target: "main/library/item",
      menus: [
        {
          menuType: "menuitem",
          l10nID: "pdf-to-md-menu",
          onCommand: (_event, context) =>
            this.handleConvertCommand(context?.items || this.selectedItems()),
        },
        {
          menuType: "menuitem",
          l10nID: "pdf-to-md-menu-progress",
          onShowing: (_event, context) => context.setVisible(!!this.currentRun),
          onCommand: () => this.showProgressCommand(),
        },
      ],
    });
    this.toolsMenuID = Zotero.MenuManager.registerMenu({
      menuID: "pdf-to-md-tools",
      pluginID: this.id,
      target: "main/menubar/tools",
      menus: [
        {
          menuType: "menuitem",
          l10nID: "pdf-to-md-tools-progress",
          onCommand: () => this.showProgressCommand(),
        },
      ],
    });
  },

  // ---- Item pane section ----------------------------------------------------
  // Shown for items with PDFs: Convert and Show buttons in the header, and one
  // status line per Markdown file. Hook options: Zotero's
  // xpcom/pluginAPI/itemPaneManager.js.

  addSection() {
    this.sectionID = Zotero.ItemPaneManager.registerSection({
      paneID: "pdf-to-md",
      pluginID: this.id,
      // Zotero shows these at 16 and 20 px without scaling, so they need their own sizes.
      header: { l10nID: "pdf-to-md-section", icon: this.rootURI + "icon-16.svg" },
      sidenav: { l10nID: "pdf-to-md-section", icon: this.rootURI + "icon-20.svg" },
      sectionButtons: [
        {
          type: "convert",
          icon: "chrome://zotero/skin/16/universal/export.svg",
          l10nID: "pdf-to-md-section-convert",
          onClick: ({ item }) => this.handleConvertCommand([this.sectionTarget(item)]),
        },
        {
          type: "open-folder",
          icon: "chrome://zotero/skin/16/universal/folder-open.svg",
          l10nID: "pdf-to-md-section-open",
          onClick: ({ item }) =>
            this.revealOutput(item).catch((e) => {
              Zotero.logError("PdfToMd open folder: " + e);
              this.notify("Could not open the output folder: " + (e.message || e), true);
            }),
        },
      ],
      onInit: ({ body, refresh }) => this.sectionRefreshers.set(body, refresh),
      onDestroy: ({ body }) => this.sectionRefreshers.delete(body),
      // Only shown for items that have PDFs.
      onItemChange: ({ item, setEnabled }) => {
        setEnabled(this.sectionJobs(item).length > 0);
      },
      onRender: ({ body, doc }) => {
        body.replaceChildren(this.htmlElement(doc, "Checking…"));
      },
      onAsyncRender: async ({ body, doc, item, setSectionSummary, setSectionButtonStatus }) => {
        setSectionButtonStatus("open-folder", { disabled: !this.getPref("outputDir").trim() });
        const { summary, lines, busy } = await this.sectionStatus(item);
        // Already queued or converting: another click would do nothing.
        setSectionButtonStatus("convert", { disabled: busy });
        setSectionSummary(summary);
        body.replaceChildren(...lines.map((line) => this.htmlElement(doc, line)));
      },
    });
  },

  // Show the item's Markdown in Finder (a converted file first, else its copy
  // in _needs_review); if there is none yet, open the output folder.
  async revealOutput(item) {
    const outDir = this.getPref("outputDir").trim();
    if (!outDir) {
      this.notify("Choose an output folder first (Settings → PDF to Markdown).", true);
      this.openSettings();
      return;
    }
    const found = [];
    for (const job of this.sectionJobs(item)) {
      const file = await this.findOutput(job, outDir);
      if (file) found.push(file);
    }
    const best = found.find((f) => !f.inReview) || found[0];
    if (best) return Zotero.File.reveal(best.path);
    if (!(await IOUtils.exists(outDir))) {
      this.notify(`The output folder doesn't exist yet: ${outDir}`, true);
      return;
    }
    Zotero.launchFile(outDir);
  },

  // ---- Finding existing Markdown files --------------------------------------
  // Files are found by the attachment keys in their frontmatter
  // (attachment_keys: [...]), whatever their name. The index reads the first
  // 8 KB of each .md and is cached for 30 s, or until the next run.

  outputIndexCache: null, // { dir, builtAt, entries: [{ path, inReview, keys }] }

  async outputIndex(outDir) {
    const cache = this.outputIndexCache;
    if (cache && cache.dir === outDir && Date.now() - cache.builtAt < 30000) return cache;
    const reviewDir = PathUtils.join(outDir, "_needs_review");
    const entries = [];
    const walk = async (dir, inReview) => {
      let children;
      try {
        children = await IOUtils.getChildren(dir);
      } catch (e) {
        return; // folder missing
      }
      for (const child of children.sort()) {
        if (!inReview && child === reviewDir) continue;
        if (child.endsWith(".md")) {
          const keys = await this.readAttachmentKeys(child);
          if (keys) entries.push({ path: child, inReview, keys });
        } else if ((await IOUtils.stat(child).catch(() => null))?.type === "directory") {
          await walk(child, inReview);
        }
      }
    };
    await walk(outDir, false);
    await walk(reviewDir, true);
    this.outputIndexCache = { dir: outDir, builtAt: Date.now(), entries };
    return this.outputIndexCache;
  },

  // Sorted, comma-joined attachment keys from a .md file's frontmatter, or null.
  async readAttachmentKeys(path) {
    try {
      const head = new TextDecoder().decode(await IOUtils.read(path, { maxBytes: 8192 }));
      const front = head.match(/^---\n([\s\S]*?)\n---/);
      const line = front && front[1].match(/^attachment_keys:\s*(\[.*\])\s*$/m);
      return line ? JSON.parse(line[1]).sort().join(",") : null;
    } catch (e) {
      return null;
    }
  },

  // { path, inReview } of a job's existing .md, or null. Looked up by
  // attachment keys (output folder before _needs_review); files without keys
  // are found by expected name.
  async findOutput(job, outDir) {
    const want = [...(job.keys || [])].sort().join(",");
    if (want) {
      const { entries } = await this.outputIndex(outDir);
      const matches = entries.filter((e) => e.keys === want);
      const best = matches.find((e) => !e.inReview) || matches[0];
      if (best) return { path: best.path, inReview: best.inReview };
    }
    const parts = (job.out + ".md").split("/");
    for (const [base, inReview] of [[[outDir], false], [[outDir, "_needs_review"], true]]) {
      const path = PathUtils.join(...base, ...parts);
      // A file with keys belongs to whatever PDF its keys say, not this one.
      if ((await IOUtils.exists(path)) && !(await this.readAttachmentKeys(path))) {
        return { path, inReview };
      }
    }
    return null;
  },

  refreshSections() {
    this.outputIndexCache = null; // a run may have written files
    for (const refresh of this.sectionRefreshers.values()) {
      refresh().catch((e) => Zotero.logError("PdfToMd section refresh: " + e));
    }
  },

  htmlElement(doc, text) {
    const div = doc.createElementNS("http://www.w3.org/1999/xhtml", "div");
    div.textContent = text;
    return div;
  },

  // The item the section stands for: in the reader the pane may show the
  // PDF attachment, but outputs are named after its parent item.
  sectionTarget(item) {
    if (item?.isAttachment() && item.parentItemID) {
      return Zotero.Items.get(item.parentItemID);
    }
    return item;
  },

  // The jobs a conversion of this item would run (all of its PDFs).
  sectionJobs(item) {
    const target = this.sectionTarget(item);
    if (!target || !(target.isRegularItem() || this.isPdf(target))) return [];
    return this.buildJobs(this.getPref("multiMode") || "folder", [target]);
  },

  // { summary, lines, busy } for the section: for each job, whether it is
  // queued or converting in the current run, else whether its .md file
  // exists in the output folder or in _needs_review (see findOutput).
  // busy = some of the item's jobs are queued or converting.
  async sectionStatus(item) {
    const outDir = this.getPref("outputDir").trim();
    if (!outDir) {
      return {
        summary: "No output folder",
        lines: ["Set an output folder in Settings → PDF to Markdown."],
      };
    }
    const jobs = this.sectionJobs(item);
    // For a book in flat mode, outputs start with "<item> - "; drop that.
    const prefix = jobs.length > 1 ? this.itemLabel(this.sectionTarget(item)) + " - " : null;
    const files = await Promise.all(jobs.map((job) => this.findOutput(job, outDir)));
    // Jobs of the current run that haven't finished: key -> queued/start.
    const active = new Map(
      (this.currentRun?.rows || []).filter((r) => this.isActive(r)).map((r) => [r.key, r.state])
    );
    const states = files.map((f, i) =>
      active.get(this.jobKey(jobs[i])) || (!f ? "missing" : f.inReview ? "review" : "converted")
    );
    const mark = { converted: "✓", review: "⚠", missing: "–", queued: "⋯", start: "↻" };
    const word = {
      converted: "converted",
      review: "needs review",
      missing: "not converted",
      queued: "queued",
      start: "converting…",
    };
    const lines = jobs.map((job, i) => {
      // The existing file's name, else the name a conversion would use; for
      // a book, just "01 - chapter".
      let name = files[i] ? PathUtils.filename(files[i].path).replace(/\.md$/, "") : job.out.split("/").pop();
      if (prefix && name.startsWith(prefix)) name = name.slice(prefix.length);
      return `${mark[states[i]]} ${name} — ${word[states[i]]}`;
    });
    const done = states.filter((s) => s === "converted").length;
    const review = states.filter((s) => s === "review").length;
    let summary;
    if (states.includes("start")) summary = "Converting…";
    else if (states.includes("queued")) summary = "Queued";
    else if (done === jobs.length) summary = "Converted";
    else if (review === jobs.length) summary = "Needs review";
    else if (!done && !review) summary = "Not converted";
    else {
      summary = `${done} of ${jobs.length} converted`;
      if (review) summary += `, ${review} need review`;
    }
    const busy = states.some((s) => s === "queued" || s === "start");
    return { summary, lines, busy };
  },

  // "Convert to Markdown" and "Show in Output Folder" in the Locate drop-down
  // of the item pane's side strip. Zotero doesn't call onShowing for this
  // menu, so the entries can't hide: items without PDFs get a message.
  addLocateMenu() {
    const forItem = (context, action) => {
      const item = context?.items?.[0];
      if (!item || !this.sectionJobs(item).length) {
        this.notify("This item has no PDF to convert.", true);
        return;
      }
      action(item);
    };
    this.locateMenuID = Zotero.MenuManager.registerMenu({
      menuID: "pdf-to-md-locate",
      pluginID: this.id,
      target: "sidenav/locate",
      menus: [
        { menuType: "separator" },
        {
          menuType: "menuitem",
          l10nID: "pdf-to-md-locate-convert",
          icon: "chrome://zotero/skin/16/universal/export.svg",
          onCommand: (_event, context) => {
            // Zotero passes only the pane's item; in the library, convert the
            // whole selection if that item is part of it.
            const item = context?.items?.[0];
            const selected = context?.tabType === "library" ? this.selectedItems() : [];
            if (item && selected.length > 1 && selected.some((i) => i.id === item.id)) {
              this.handleConvertCommand(selected);
              return;
            }
            forItem(context, (one) => this.handleConvertCommand([this.sectionTarget(one)]));
          },
        },
        {
          menuType: "menuitem",
          l10nID: "pdf-to-md-locate-open",
          icon: "chrome://zotero/skin/16/universal/folder-open.svg",
          onCommand: (_event, context) =>
            forItem(context, (item) =>
              this.revealOutput(item).catch((e) => {
                Zotero.logError("PdfToMd open folder: " + e);
                this.notify("Could not open the output folder: " + (e.message || e), true);
              })
            ),
        },
      ],
    });
  },

  // Settings → PDF to Markdown. Zotero removes the pane itself on shutdown.
  registerPrefsPane() {
    Zotero.PreferencePanes.register({
      pluginID: this.id,
      src: this.rootURI + "preferences.xhtml",
      scripts: [this.rootURI + "preferences.js"],
      stylesheets: [this.rootURI + "preferences.css"],
      label: "PDF to Markdown",
    })
      .then((paneID) => (this.prefsPaneID = paneID))
      .catch((e) => Zotero.logError("PdfToMd prefs pane: " + e));
  },

  openSettings() {
    Zotero.Utilities.Internal.openPreferences(this.prefsPaneID);
  },

  // ---- Selection → jobs ---------------------------------------------------
  // A job is one output file: { pdfs: [path, ...], keys: [attachment key, ...],
  // out: "relative/name" (no ".md"), meta: {...} }. One PDF per job, except in
  // merge mode.
  //
  // Items with one PDF: <item>.md (metadataNames pref), else <pdf name>.md.
  // PDFs with no parent item: <pdf name>.md.
  // Items with several PDFs, by the multiMode pref:
  //   folder (default) <item>/01 - <pdf name>.md
  //   flat             <item> - 01 - <pdf name>.md
  //   merge            <item>.md   (all of the item's PDFs, in order)
  // <item> is Zotero's file-name format for the item (by default
  // "Author - Year - Title"). The number is the PDF's position among all the
  // item's PDFs.

  isPdf(att) {
    if (!att || !att.isAttachment()) return false;
    const path = att.getFilePath();
    return !!path && path.toLowerCase().endsWith(".pdf");
  },

  // An item's PDF attachments in the order Zotero lists them (by title, with
  // numbers compared as numbers so "Chapter 2" comes before "Chapter 10").
  itemPdfs(item) {
    const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });
    return item
      .getAttachments()
      .map((id) => Zotero.Items.get(id))
      .filter((att) => this.isPdf(att))
      .sort(
        (a, b) =>
          collator.compare(a.getField("title"), b.getField("title")) ||
          collator.compare(a.getFilePath(), b.getFilePath())
      );
  },

  itemLabel(item) {
    let label = "";
    try {
      label = Zotero.Attachments.getFileBaseNameFromItem(item);
    } catch (e) {}
    if (!label) label = item.getField("title") || "Untitled " + item.key;
    label = Zotero.File.getValidFileName(label).trim();
    return label.length > 120 ? label.slice(0, 120).trim() : label;
  },

  pdfStem(path) {
    return PathUtils.filename(path).replace(/\.pdf$/i, "");
  },

  selectedItems() {
    return Zotero.getActiveZoteroPane()?.getSelectedItems() || [];
  },

  buildJobs(mode, items) {
    // Group the items by parent item. A parent item means all of its PDFs;
    // a PDF attachment means just that PDF.
    const groups = new Map(); // key -> { parent, chosen: Set of attachment ids }
    const addTo = (key, parent, atts) => {
      if (!groups.has(key)) groups.set(key, { parent, chosen: new Set() });
      for (const att of atts) groups.get(key).chosen.add(att.id);
    };
    for (const item of items) {
      if (item.isRegularItem()) {
        addTo(item.id, item, this.itemPdfs(item));
      } else if (this.isPdf(item)) {
        const parent = item.parentItemID ? Zotero.Items.get(item.parentItemID) : null;
        if (parent) addTo(parent.id, parent, [item]);
        else addTo("standalone-" + item.id, null, [item]);
      }
    }

    const metadataNames = this.getBoolPref("metadataNames", true);
    const jobs = [];
    for (const { parent, chosen } of groups.values()) {
      const all = parent ? this.itemPdfs(parent) : [];
      // Item details for the frontmatter.
      let meta = null;
      try {
        meta = parent ? this.itemMeta(parent) : null;
      } catch (e) {
        Zotero.logError("PdfToMd item details: " + e);
      }
      if (all.length < 2) {
        for (const id of chosen) {
          const att = Zotero.Items.get(id);
          const path = att.getFilePath();
          const out = parent && metadataNames ? this.itemLabel(parent) : this.pdfStem(path);
          jobs.push({ pdfs: [path], keys: [att.key], out, ...(meta && { meta }) });
        }
        continue;
      }
      const label = this.itemLabel(parent);
      if (mode === "merge") {
        jobs.push({
          pdfs: all.map((att) => att.getFilePath()),
          keys: all.map((att) => att.key),
          out: label,
          ...(meta && { meta }),
        });
        continue;
      }
      const width = Math.max(2, String(all.length).length);
      all.forEach((att, i) => {
        if (!chosen.has(att.id)) return;
        const path = att.getFilePath();
        const name = `${String(i + 1).padStart(width, "0")} - ${this.pdfStem(path)}`;
        jobs.push({
          pdfs: [path],
          keys: [att.key],
          out: mode === "flat" ? `${label} - ${name}` : `${label}/${name}`,
          meta: { ...(meta || {}), attachment: att.getField("title") },
        });
      });
    }
    // Drop duplicates (an item and one of its PDFs both selected). Different
    // PDFs with the same name are kept; the script names the second "(2)".
    const seen = new Set();
    return jobs.filter((job) => {
      const key = this.jobKey(job);
      return !seen.has(key) && seen.add(key);
    });
  },

  // Bibliographic details of a regular item, for the frontmatter. Empty
  // fields are left out.
  itemMeta(item) {
    const field = (name) => {
      try {
        return item.getField(name, false, true) || ""; // true: base-mapped (bookTitle etc.)
      } catch (e) {
        return "";
      }
    };
    const people = (type) =>
      item
        .getCreatorsJSON()
        .filter((c) => c.creatorType === type)
        .map((c) => (c.name ? c.name : [c.lastName, c.firstName].filter(Boolean).join(", ")));
    const library = Zotero.Libraries.get(item.libraryID);
    const meta = {
      title: field("title"),
      authors: people("author"),
      editors: people("editor"),
      year: Number(Zotero.Date.strToDate(field("date")).year) || "",
      item_type: item.itemType,
      publication: field("publicationTitle"),
      doi: field("DOI"),
      url: field("url"),
      zotero:
        library?.libraryType === "group"
          ? `zotero://select/groups/${library.groupID}/items/${item.key}`
          : `zotero://select/library/items/${item.key}`,
    };
    for (const [k, v] of Object.entries(meta)) {
      if (!v || (Array.isArray(v) && !v.length)) delete meta[k];
    }
    return meta;
  },

  // ---- Running the script ---------------------------------------------------
  // The script is copied from the .xpi to <Zotero data dir>/pdf-to-md at
  // startup. It runs with, in order:
  //   1. the pythonPath pref, if set (packages are the user's job)
  //   2. `uv run --script`, which installs the header's packages
  //   3. a private venv in <data dir>/pdf-to-md/venv, set up with pip
  // Arguments are passed without a shell.

  SCRIPT_NAME: "pdf_to_md.py",

  getPref(key) {
    return Zotero.Prefs.get("extensions.pdf-to-md." + key, true) || "";
  },

  getBoolPref(key, fallback) {
    const value = Zotero.Prefs.get("extensions.pdf-to-md." + key, true);
    return typeof value === "boolean" ? value : fallback;
  },

  workDir() {
    return PathUtils.join(Zotero.DataDirectory.dir, "pdf-to-md");
  },

  // Search PATH plus common install locations. GUI apps on macOS get a
  // minimal PATH, so tools in ~/.local/bin or Homebrew aren't found otherwise.
  async findExecutable(names) {
    const { Subprocess } = ChromeUtils.importESModule(
      "resource://gre/modules/Subprocess.sys.mjs"
    );
    // These go before PATH, and /usr/bin last, so macOS's old
    // /usr/bin/python3 is the last resort.
    const home = Services.dirsvc.get("Home", Ci.nsIFile).path;
    const extra = [PathUtils.join(home, ".local", "bin"), PathUtils.join(home, ".cargo", "bin")];
    if (!Zotero.isWin) extra.push("/opt/homebrew/bin", "/usr/local/bin");
    const sep = Zotero.isWin ? ";" : ":";
    const env = Subprocess.getEnvironment();
    // pathSearch throws on the first relative or empty entry (PathUtils.join
    // rejects it) instead of skipping it, so keep absolute directories only.
    const searchPath = [...extra, ...(env.PATH || "").split(sep), Zotero.isWin ? "" : "/usr/bin"]
      .filter((dir) => dir && PathUtils.isAbsolute(dir))
      .join(sep);
    for (const name of names) {
      try {
        return await Subprocess.pathSearch(name, { PATH: searchPath });
      } catch (e) {}
    }
    return null;
  },

  bundledScriptPath() {
    return PathUtils.join(this.workDir(), this.SCRIPT_NAME);
  },

  // Copy the bundled script out of the .xpi. Runs at startup, so an upgraded
  // plugin always replaces the old copy.
  async installScript() {
    const url = this.rootURI + this.SCRIPT_NAME;
    const text = Zotero.File.getResourceAsync
      ? await Zotero.File.getResourceAsync(url)
      : await Zotero.File.getContentsFromURLAsync(url);
    await IOUtils.makeDirectory(this.workDir(), {
      ignoreExisting: true,
      createAncestors: true,
    });
    await IOUtils.writeUTF8(this.bundledScriptPath(), text);
  },

  // { path, text } of the script's copy, re-copied if it has gone missing.
  async prepareScript() {
    const path = this.bundledScriptPath();
    if (!(await IOUtils.exists(path))) await this.installScript();
    return { path, text: await IOUtils.readUTF8(path) };
  },

  // Packages from the script's PEP 723 header: # dependencies = ["a", "b"]
  scriptDependencies(text) {
    const m = text.match(/^# dependencies = \[(.*)\]\s*$/m);
    return m ? [...m[1].matchAll(/"([^"]+)"/g)].map((d) => d[1]) : [];
  },

  // Returns { command, args } that runs the script, setting up packages if needed.
  async prepareRunner(script, progress) {
    const configured = this.getPref("pythonPath").trim();
    if (configured) return { command: configured, args: [script.path] };

    const uv = await this.findExecutable(["uv"]);
    if (uv) {
      return { command: uv, args: ["run", "--quiet", "--script", script.path] };
    }

    const venv = PathUtils.join(this.workDir(), "venv");
    const venvPython = Zotero.isWin
      ? PathUtils.join(venv, "Scripts", "python.exe")
      : PathUtils.join(venv, "bin", "python");
    const deps = this.scriptDependencies(script.text);
    // Reinstall when the script's package list changes.
    const marker = PathUtils.join(venv, "deps.txt");
    const wanted = deps.join("\n");
    const installed = (await IOUtils.exists(marker))
      ? await IOUtils.readUTF8(marker)
      : null;
    if (installed === wanted && (await IOUtils.exists(venvPython))) {
      return { command: venvPython, args: [script.path] };
    }

    const python = await this.findExecutable(["python3", "python"]);
    if (!python) {
      throw new Error(
        "Neither uv nor Python 3 was found. Install uv (https://docs.astral.sh/uv/) or Python 3, or set the Python interpreter in Settings → PDF to Markdown."
      );
    }
    progress("installing Python packages (first run, about a minute)…");
    if (!(await IOUtils.exists(venvPython))) {
      await this.runChecked(python, ["-m", "venv", venv], "creating the Python environment");
    }
    await this.runChecked(
      venvPython,
      ["-m", "pip", "install", "--disable-pip-version-check", "--upgrade", ...deps],
      "installing Python packages"
    );
    await IOUtils.writeUTF8(marker, wanted);
    return { command: venvPython, args: [script.path] };
  },

  async runChecked(command, args, what) {
    const { exitCode, stdout, stderr } = await this.run(command, args);
    Zotero.debug(`PdfToMd: ${what}: exit ${exitCode}\n${stdout}\n${stderr}`);
    if (exitCode !== 0) {
      throw new Error(
        `Failed ${what} (exit ${exitCode}): ` +
          (stderr.trim().split("\n").pop() || "see Help → Debug Output")
      );
    }
  },

  // ---- Conversion runs ------------------------------------------------------
  // A run converts queued jobs one batch (script call) at a time, until none
  // are left. run.rows has one row per job: { job, key, label, itemType,
  // state }, state = queued, start (converting), ok, review, skipped or failed.

  // Converts `items` (parent items and/or PDF attachments). During a run, the
  // new jobs are queued.
  async handleConvertCommand(items) {
    const outputDir = this.getPref("outputDir").trim();
    if (!outputDir) {
      this.notify("Choose an output folder first (Settings → PDF to Markdown).", true);
      this.openSettings();
      return;
    }
    const jobs = this.buildJobs(this.getPref("multiMode") || "folder", items);
    if (jobs.length === 0) {
      this.notify("No PDF attachments in the selection.", true);
      return;
    }
    let run = this.currentRun;
    if (run) {
      // Skip jobs that are already waiting or converting.
      const active = new Set(run.rows.filter((r) => this.isActive(r)).map((r) => r.key));
      const fresh = jobs.filter((job) => !active.has(this.jobKey(job)));
      if (fresh.length) run.pending.push(...this.addRows(run, fresh));
      // Open the popup again, in case it went behind the main window.
      this.showProgress(run);
      this.refreshSections();
      return;
    }

    run = this.currentRun = { rows: [], pending: [], batches: [], startedAt: Date.now() };
    run.pending.push(...this.addRows(run, jobs));
    const { setInterval, clearInterval } = ChromeUtils.importESModule(
      "resource://gre/modules/Timer.sys.mjs"
    );
    // Turns the spinner, and updates the elapsed time once a second.
    let frame = 0;
    run.timer = setInterval(() => {
      this.spin(run, frame);
      if (++frame % 8 === 0) this.paintProgress(run);
    }, 125);
    run.stopTimer = () => clearInterval(run.timer);
    this.showProgress(run);
    this.refreshSections();
    try {
      while (run.pending.length) {
        await this.runBatch(run, run.pending.splice(0));
      }
    } finally {
      run.stopTimer();
      this.currentRun = null;
      this.lastRun?.view?.pw.close();
      this.lastRun = run;
      this.showProgress(run, true);
      this.refreshSections();
    }
  },

  jobKey(job) {
    return job.out + "\n" + job.pdfs.join("\n");
  },

  isActive(row) {
    return row.state === "queued" || row.state === "start";
  },

  // Adds a queued row per job to the run; returns the new rows.
  addRows(run, jobs) {
    const rows = jobs.map((job) => ({
      job,
      key: this.jobKey(job),
      label: job.out.split("/").pop(),
      itemType: job.meta?.item_type || "attachmentPDF",
      state: "queued",
    }));
    run.rows.push(...rows);
    return rows;
  },

  // One script call for `rows`. Errors are recorded in run.batches, so the
  // jobs queued after them still run.
  async runBatch(run, rows) {
    const jobs = rows.map((r) => r.job);
    const nPdfs = jobs.reduce((n, job) => n + job.pdfs.length, 0);
    const setState = (row, state) => {
      row.state = state;
      this.paintProgress(run);
      this.refreshSections();
    };
    let jobsFile = null;
    try {
      // Prefs are read per batch.
      const outputDir = this.getPref("outputDir").trim();
      if (!outputDir) throw new Error("No output folder set (Settings → PDF to Markdown).");
      const script = await this.prepareScript();
      const runner = await this.prepareRunner(script, (text) => {
        run.phase = text;
        this.paintProgress(run);
      });
      run.phase = null;

      await IOUtils.makeDirectory(this.workDir(), {
        ignoreExisting: true,
        createAncestors: true,
      });
      jobsFile = PathUtils.join(this.workDir(), `jobs-${Date.now()}.json`);
      await IOUtils.writeUTF8(jobsFile, JSON.stringify(jobs, null, 1));
      const prefix = this.PROGRESS_PREFIX + " ";
      const { exitCode, stdout, stderr } = await this.run(
        runner.command,
        [
          ...runner.args,
          "--out",
          outputDir,
          this.getBoolPref("stripHeaders", true)
            ? "--strip-headers"
            : "--no-strip-headers",
          this.getBoolPref("overwrite", false) ? "--overwrite" : "--no-overwrite",
          this.getBoolPref("pageMarkers", true) ? "--page-markers" : "--no-page-markers",
          this.getBoolPref("htmlTables", false) ? "--html-tables" : "--no-html-tables",
          this.getBoolPref("repairSymbols", true) ? "--repair-symbols" : "--no-repair-symbols",
          "--ocr-language",
          this.getPref("ocrLanguage").trim() || "eng",
          "--progress",
          "--jobs",
          jobsFile,
        ],
        // The script's progress lines: {"job": index, "state": …}.
        (line) => {
          if (!line.startsWith(prefix)) return false;
          try {
            const { job, state } = JSON.parse(line.slice(prefix.length));
            if (rows[job]) setState(rows[job], state);
          } catch (e) {
            Zotero.logError("PdfToMd progress line: " + line);
          }
          return true;
        }
      );
      Zotero.debug(
        `PdfToMd: ${runner.command} ${runner.args.join(" ")}: exit ${exitCode}\nstdout:\n${stdout}\nstderr:\n${stderr}`
      );
      if (exitCode === 0) {
        run.batches.push({ text: stdout.trim() || `Script finished (${nPdfs} PDF(s)).` });
      } else {
        run.batches.push({
          text:
            `Script failed (exit ${exitCode}): ` +
            (stderr.trim().split("\n").pop() || "see Help → Debug Output"),
          isError: true,
        });
      }
    } catch (e) {
      Zotero.logError("PdfToMd python: " + e);
      run.batches.push({ text: "Could not run script: " + (e.message || e), isError: true });
    } finally {
      run.phase = null;
      if (jobsFile) await IOUtils.remove(jobsFile, { ignoreAbsent: true });
      // Jobs the script never reported on (it crashed or didn't start).
      for (const row of rows) if (this.isActive(row)) row.state = "failed";
    }
  },

  PROGRESS_PREFIX: "@pdf-to-md-progress", // same as in pdf_to_md.py
  PROGRESS_ROWS: 10, // rows shown at once; the headline counts the rest

  // Show Progress (Tools menu, context menu): the running conversion's
  // popup, or the last run's summary.
  showProgressCommand() {
    const run = this.currentRun || this.lastRun;
    if (!run) {
      this.notify("No conversion has run since Zotero started.");
      return;
    }
    this.showProgress(run, run !== this.currentRun);
  },

  // Opens the run's popup, replacing any open one so it comes to the front.
  // `final`: the run has ended; show the summary and close on a timer or click.
  // With the keepOnTop setting, the popup floats above all windows, and a
  // click hides it during a run too (Show Progress brings it back).
  showProgress(run, final = false) {
    run.view?.pw.close();
    const onTop = this.getBoolPref("keepOnTop", false);
    const pw = new Zotero.ProgressWindow({
      closeOnClick: final || onTop,
      window: onTop ? this.onTopParent() : undefined,
    });
    const view = (run.view = { pw, ui: [] });
    // Once closed (click, timer), stop updating it: Zotero keeps every call to
    // a closed popup, waiting for it to load again, and the spinner calls 8×/s.
    const close = pw.close;
    pw.close = () => {
      if (run.view === view) run.view = null;
      close();
    };
    pw.show();
    this.paintProgress(run, final);
    if (onTop && !final) {
      pw.addDescription("Click to hide. Tools → PDF to Markdown: Show Progress brings it back.");
    }
    if (final) {
      const isError = run.batches.some((b) => b.isError);
      // One count line for the whole run, then the other summary lines.
      const count = (state) => run.rows.filter((r) => r.state === state).length;
      const head = [`${count("ok")} converted → ${this.getPref("outputDir").trim()}`];
      if (count("skipped")) head.push(`${count("skipped")} skipped (already converted)`);
      const rest = run.batches
        .flatMap((b) => b.text.split("\n"))
        .filter((l) => !/^\d+ (converted →|skipped \(already converted\))/.test(l));
      const lines = [...new Set([...head, ...rest])];
      pw.addDescription(
        lines.slice(0, 8).join("\n") +
          (lines.length > 8 ? `\n… (${lines.length - 8} more lines in Debug Output)` : "")
      );
      pw.startCloseTimer(isError ? 10000 : 8000);
    }
  },

  // A stand-in for the main window that Zotero.ProgressWindow opens its
  // popup from. Zotero asks for a dependent window, which drops behind the
  // main window when that is clicked; this asks for an always-on-top one,
  // the option Zotero itself uses when there is no main window.
  onTopParent() {
    const main = Zotero.getMainWindow();
    if (!main) return undefined;
    return {
      openDialog: (url, name, features) =>
        main.openDialog(url, name, features.replace(/,?dependent=yes/, "") + ",alwaysontop=yes"),
      addEventListener: (...args) => main.addEventListener(...args),
      get closed() {
        return main.closed;
      },
    };
  },

  // Brings the popup's headline and rows up to date with run.rows. The rows
  // are a window of PROGRESS_ROWS jobs that moves along as jobs finish.
  paintProgress(run, final = false) {
    const view = run.view;
    if (!view) return;
    const { rows } = run;
    const finished = rows.filter((r) => !this.isActive(r)).length;
    let status;
    if (final) {
      status = run.batches.some((b) => b.isError) ? "error" : "done";
    } else if (run.phase) {
      status = run.phase;
    } else {
      const secs = Math.floor((Date.now() - run.startedAt) / 1000);
      const time = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
      status = `${Math.min(finished + 1, rows.length)} of ${rows.length} · ${time}`;
    }
    view.pw.changeHeadline(`PDF to Markdown — ${status}`);

    const size = Math.min(rows.length, this.PROGRESS_ROWS);
    const firstActive = rows.findIndex((r) => this.isActive(r));
    const start = Math.max(0, Math.min((firstActive < 0 ? rows.length : firstActive) - 2, rows.length - size));
    view.start = start;
    while (view.ui.length < size) {
      const row = rows[start + view.ui.length];
      view.ui.push(new view.pw.ItemProgress(row.itemType, row.label));
    }
    const text = {
      queued: (l) => l,
      start: (l) => l,
      ok: (l) => `✓ ${l}`,
      skipped: (l) => `✓ ${l} — already converted`,
      review: (l) => `⚠ ${l} — needs review`,
      failed: (l) => `✗ ${l} — failed`,
    };
    view.ui.forEach((ui, i) => {
      const row = rows[start + i];
      ui.setText((text[row.state] || text.queued)(row.label));
      if (row.state === "start") return; // spin() draws its icon
      ui.setItemTypeAndIcon(row.itemType);
      if (row.state !== "queued") ui.setProgress(100);
      // Queued rows are dim. (_hbox exists once the popup has loaded.)
      if (ui._hbox) ui._hbox.style.opacity = row.state === "queued" ? "0.5" : "1";
    });
  },

  // Turns the spinner of the converting row (Zotero's progress arcs).
  spin(run, frame) {
    const view = run.view;
    if (!view) return;
    view.ui.forEach((ui, i) => {
      if (run.rows[view.start + i]?.state !== "start") return;
      ui.setProgress(5 + (frame % 19) * 5);
      if (ui._hbox) ui._hbox.style.opacity = "1"; // setProgress fades by percent
    });
  },

  // Runs a command without a shell. `onStderrLine(line)`, if given, sees
  // each stderr line as it arrives; lines it returns true for are left out
  // of the returned stderr.
  async run(command, args, onStderrLine) {
    const { Subprocess } = ChromeUtils.importESModule(
      "resource://gre/modules/Subprocess.sys.mjs"
    );
    const proc = await Subprocess.call({
      command,
      arguments: args,
      stderr: "pipe",
    });
    proc.stdin.close();
    const readAll = async (pipe, onLine) => {
      let out = "";
      let partial = "";
      let chunk;
      while ((chunk = await pipe.readString())) {
        if (!onLine) {
          out += chunk;
          continue;
        }
        const lines = (partial + chunk).split("\n");
        partial = lines.pop();
        for (const line of lines) if (!onLine(line)) out += line + "\n";
      }
      if (partial && !(onLine && onLine(partial))) out += partial;
      return out;
    };
    const [stdout, stderr] = await Promise.all([
      readAll(proc.stdout),
      readAll(proc.stderr, onStderrLine),
    ]);
    const { exitCode } = await proc.wait();
    return { exitCode, stdout, stderr };
  },

  notify(text, isError = false) {
    const pw = new Zotero.ProgressWindow({ closeOnClick: true });
    pw.changeHeadline(isError ? "PDF to Markdown — error" : "PDF to Markdown");
    // Keep the popup readable if the script prints a lot.
    const lines = String(text).split("\n");
    const shown = lines.slice(0, 8).join("\n") +
      (lines.length > 8 ? `\n… (${lines.length - 8} more lines in Debug Output)` : "");
    pw.addDescription(shown);
    pw.show();
    pw.startCloseTimer(isError ? 10000 : 6000);
  },
};
