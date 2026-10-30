/* GT Panel: the Golden Thread buttons as a side panel in Obsidian.
 *
 * A click copies the button's prompt to the clipboard (the default, and the fallback for every
 * other route); you paste it into Claude yourself. Two opt-in routes push the prompt instead
 * (89g): into Claudian's chat box, or pasted into an open Terminal-plugin tab. Each has its own
 * tested-versions list and a method probe; anything that fails the probe says why and falls back
 * to the clipboard. Only the button's own prompt is pushed, as one line. Nothing is submitted
 * unless the button says so AND the owner chose that once. Nothing is run, nothing is sent over a
 * network, and no vault note is read. The only files read are menu.json, status.json and
 * profile.json in this plugin's own folder, written by `gt_home.py panel` (and `refresh`).
 *
 * Allowance line (89f): one line from status.json, with the reading's age worked out when it
 * is drawn. Claudian model sync (89h): on the command only, it shows what would change from
 * gt's model profile (profile.json), writes Claudian's `model` and `effortLevel` only after a
 * confirm click, and keeps the old values for an undo. */
"use strict";
const obsidian = require("obsidian");

const VIEW = "gt-panel-view";
const CONTROL = /[\u0000-\u0008\u000b-\u001f\u007f]/;
const ANY_CONTROL = /[\u0000-\u001f\u007f]+/g;     // newline and tab too: a pushed prompt is one line

function cleanMenu(raw) {
  let data;
  try { data = JSON.parse(raw); } catch (e) { return []; }
  const list = data && Array.isArray(data.buttons) ? data.buttons : [];
  return list.filter((b) => b && typeof b.label === "string" && typeof b.prompt === "string"
    && b.label.trim() && b.prompt.trim() && !CONTROL.test(b.prompt) && !CONTROL.test(b.label));
}

/* a button asks to be submitted only with a literal true; the owner's choice is separate */
function wantsSubmit(b) { return !!b && b.submit === true; }

function oneLine(s) { return String(s).replace(ANY_CONTROL, " ").trim(); }

function isNum(x) { return typeof x === "number" && isFinite(x); }

function ageWords(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 90) return "just now";
  if (s < 90 * 60) return Math.floor(s / 60) + " minutes ago";
  if (s < 48 * 3600) return Math.floor(s / 3600) + " hours ago";
  return Math.floor(s / 86400) + " days ago";
}

/* status.json -> the one allowance line, or null for nothing to show. Mirrors the per-turn
 * note (gt_turn_notes.py): off shows nothing; a window past its reset time is not quoted; the
 * line appears only when a live window is at or above the threshold; the age is always there. */
function allowanceLine(status, nowMs) {
  if (!status || typeof status !== "object" || Array.isArray(status) || status.enabled !== true) return null;
  const r = status.reading;
  if (r === null || r === undefined) return "Allowance: no saved reading yet";
  if (typeof r !== "object" || !isNum(r.ts)) return null;
  const now = nowMs / 1000;
  const thr = isNum(status.threshold) ? status.threshold : 90;
  const parts = [];
  let hot = false;
  for (const [label, key] of [["5-hour", "five_hour"], ["weekly", "seven_day"]]) {
    const v = r[key];
    if (!isNum(v)) continue;
    const reset = r[key + "_resets"];
    if (isNum(reset) && reset < now) continue;
    parts.push(label + " " + Math.round(v) + "%");
    hot = hot || v >= thr;
  }
  const age = "saved " + ageWords(now - r.ts);
  if (!parts.length) return "Allowance: last reading is out of date (" + age + ")";
  if (!hot) return null;
  return parts.join(" \u00b7 ") + " \u00b7 " + age;
}

/* ---- send routes (89g). Each push route is tested against these plugin versions only; any
 * other version, a missing plugin or a missing method refuses that route (clipboard instead). */
const CLAUDIAN_ID = "realclaudian";
const CLAUDIAN_TESTED = ["2.3.16"];
const ROUTES = {
  claudian: { id: CLAUDIAN_ID, name: "Claudian", tested: CLAUDIAN_TESTED },
  terminal: { id: "terminal", name: "Terminal", tested: ["3.28.0"] },
};
const TERMINAL_VIEW = "terminal:terminal";
const TESTED = { claudian: CLAUDIAN_TESTED, terminal: ROUTES.terminal.tested };

/* -> {ok:true, plugin, target?} or {ok:false, reason}. Looks only; changes nothing. */
function probeRoute(route, app) {
  const r = ROUTES[route];
  if (!r) return { ok: false, reason: "Unknown send route." };
  const reg = app && app.plugins && app.plugins.plugins;
  const p = reg && reg[r.id];
  if (!p || typeof p !== "object") return { ok: false, reason: r.name + " is not installed or not turned on in this vault." };
  const version = p.manifest && p.manifest.version;
  if (r.tested.indexOf(version) < 0) {
    return { ok: false, reason: r.name + " " + version + " has not been tested with the GT Panel (tested: " +
             r.tested.join(", ") + "). The prompt was copied instead." };
  }
  if (route === "claudian") {
    if (typeof p.activateView !== "function" || typeof p.getView !== "function") {
      return { ok: false, reason: "This Claudian version has no activateView/getView. The prompt was copied instead." };
    }
    return { ok: true, plugin: p };
  }
  const ws = app.workspace;
  const leaves = ws && typeof ws.getLeavesOfType === "function" ? ws.getLeavesOfType(TERMINAL_VIEW) : [];
  const term = (l) => l && l.view && l.view.emulator && l.view.emulator.terminal;
  if (!leaves.length) return { ok: false, reason: "No Terminal tab is open. The prompt was copied instead." };
  const usable = leaves.filter((l) => term(l) && typeof term(l).paste === "function");
  if (!usable.length) return { ok: false, reason: "This Terminal version has no paste method. The prompt was copied instead." };
  const leaf = usable.find((l) => ws.activeLeaf === l) || usable[0];
  return { ok: true, plugin: p, target: term(leaf) };
}

/* ---- Claudian model sync (89h). Tested against these Claudian versions only; any other
 * version, or a settings object that is not the shape that was tested, refuses the route. */
const EFFORTS = ["low", "medium", "high", "xhigh", "max"];
const MODEL_TEXT = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

/* profile.json + Claudian's live settings -> {ok, reason} or {ok, changes:[{key, from, to}]}.
 * One direction only: gt's profile to Claudian. A model with no effort leaves the effort alone. */
function planSync(profile, settings, version) {
  const no = (reason) => ({ ok: false, reason });
  if (CLAUDIAN_TESTED.indexOf(version) < 0) {
    return no("Claudian " + version + " has not been tested with the GT Panel (tested: " +
              CLAUDIAN_TESTED.join(", ") + "). Nothing was changed.");
  }
  if (!profile || typeof profile !== "object" || !profile.profile) {
    return no("gt has no model profile recorded. Run /gt:gt-settings models, then gt_home.py refresh.");
  }
  if (profile.profile === "inherit" || !profile.model) {
    return no("gt's model profile is inherit: gt does not choose a model, so there is nothing to sync.");
  }
  if (typeof profile.model !== "string" || !MODEL_TEXT.test(profile.model)) return no("gt's model name is not usable.");
  if (profile.effort !== null && profile.effort !== undefined && EFFORTS.indexOf(profile.effort) < 0) {
    return no("gt's effort level is not one Claudian takes.");
  }
  if (!settings || typeof settings !== "object" || Array.isArray(settings) || typeof settings.model !== "string") {
    return no("Claudian's settings are not in the shape the GT Panel was tested with. Nothing was changed.");
  }
  const changes = [];
  if (settings.model !== profile.model) changes.push({ key: "model", from: settings.model, to: profile.model });
  if (profile.effort && settings.effortLevel !== profile.effort) {
    changes.push({ key: "effortLevel", from: settings.effortLevel === undefined ? null : settings.effortLevel, to: profile.effort });
  }
  return { ok: true, changes };
}

class SyncModal extends obsidian.Modal {
  constructor(app, changes, onApply) {
    super(app);
    this.changes = changes;
    this.onApply = onApply;
    this.done = null;
  }
  onOpen() {
    const el = this.contentEl;
    el.empty();
    el.createEl("h3", { text: "Set Claudian to gt's model profile?" });
    for (const c of this.changes) {
      el.createDiv({ cls: "gt-panel-note",
        text: (c.key === "model" ? "Model" : "Effort") + ": " + (c.from === null ? "(not set)" : c.from) + "  \u2192  " + c.to });
    }
    el.createDiv({ cls: "gt-panel-note", text: "Nothing is written until you confirm. Undo is in the command list." });
    const ok = el.createEl("button", { cls: "mod-cta", text: "Change Claudian" });
    ok.addEventListener("click", () => this.confirm());
    const no = el.createEl("button", { text: "Cancel" });
    no.addEventListener("click", () => this.cancel());
  }
  confirm() { this.done = this.onApply(); this.close(); return this.done; }
  cancel() { this.close(); }
}

class AskSubmitModal extends obsidian.Modal {
  constructor(app) {
    super(app);
    this.answered = false;
    this.promise = new Promise((res) => { this.resolve = res; });
  }
  ask() { this.open(); return this.promise; }
  onOpen() {
    const el = this.contentEl;
    el.empty();
    el.createEl("h3", { text: "Let the panel press Enter for you?" });
    el.createDiv({ cls: "gt-panel-note",
      text: "Some buttons ask to be sent at once. Choose once: the panel presses Enter for those buttons, or it only pastes and you press Enter. You can change it later in the command list." });
    const yes = el.createEl("button", { cls: "mod-warning", text: "Press Enter for me" });
    yes.addEventListener("click", () => this.choose(true));
    const no = el.createEl("button", { cls: "mod-cta", text: "Paste only" });
    no.addEventListener("click", () => this.choose(false));
  }
  choose(v) { this.answered = true; this.resolve(v); this.close(); }
  onClose() { if (!this.answered) this.resolve(null); }    // closed with no answer: paste only, ask again later
}

/* status.json + profile.json -> the strip's lines, in order: warnings, start-up state, allowance */
function stripLines(status, profile, nowMs) {
  const out = [];
  const ok = status && typeof status === "object" && !Array.isArray(status);
  if (!ok) {
    return [{ kind: "startup", text: "gt: no panel data yet. In a terminal run: gt_home.py refresh --vault <your vault>" }];
  }
  if (Array.isArray(status.warnings)) {
    const w = status.warnings.filter((x) => typeof x === "string").map((x) => oneLine(x).slice(0, 300)).filter(Boolean);
    for (const t of w.slice(0, 3)) out.push({ kind: "warning", text: t });
  }
  let s = "gt: ";
  const name = profile && typeof profile.profile === "string" && /^[A-Za-z0-9-]{1,24}$/.test(profile.profile) ? profile.profile : null;
  if (!name) s += "no model profile recorded";
  else {
    s += "model profile " + name;
    if (typeof profile.model === "string" && MODEL_TEXT.test(profile.model)) {
      s += " (" + profile.model + (typeof profile.effort === "string" && EFFORTS.indexOf(profile.effort) >= 0 ? " " + profile.effort : "") + ")";
    }
  }
  if (isNum(status.written)) s += " \u00b7 state saved " + ageWords(nowMs / 1000 - status.written);
  out.push({ kind: "startup", text: s });
  const a = allowanceLine(status, nowMs);
  if (a) out.push({ kind: "allowance", text: a });
  return out;
}

class GtPanelView extends obsidian.ItemView {
  constructor(leaf, plugin) { super(leaf); this.plugin = plugin; }
  getViewType() { return VIEW; }
  getDisplayText() { return "GT Panel"; }
  getIcon() { return "list-checks"; }

  async onOpen() { await this.render(); }

  async render() {
    const root = this.contentEl;
    root.empty();
    const lines = stripLines(await this.plugin.loadJson("status.json"), await this.plugin.loadJson("profile.json"), Date.now());
    for (const l of lines) root.createDiv({ cls: l.kind === "warning" ? "gt-panel-warning" : "gt-panel-allowance", text: l.text });
    const buttons = await this.plugin.loadMenu();
    if (!buttons.length) {
      root.createDiv({ cls: "gt-panel-note",
        text: "No buttons yet. In a terminal run: gt_home.py panel --vault <your vault>" });
      return;
    }
    const list = root.createDiv({ cls: "gt-panel-list" });
    for (const b of buttons) {
      const btn = list.createEl("button", { cls: "gt-panel-btn", text: b.label });
      btn.addEventListener("click", () => this.plugin.send(b));
    }
    root.createDiv({ cls: "gt-panel-note",
      text: "A click sends the prompt by the route you chose (default: copies it). Paste it into Claude and press Enter." });
  }
}

class GtPanelPlugin extends obsidian.Plugin {
  async onload() {
    this.registerView(VIEW, (leaf) => new GtPanelView(leaf, this));
    this.addRibbonIcon("list-checks", "GT Panel", () => this.show());
    this.addCommand({ id: "open", name: "Open the GT Panel", callback: () => this.show() });
    for (const [id, route, label] of [["route-clipboard", "clipboard", "copy to the clipboard (default)"],
                                       ["route-claudian", "claudian", "put it in Claudian's chat box"],
                                       ["route-terminal", "terminal", "paste it into the open Terminal tab"]]) {
      this.addCommand({ id, name: "Buttons: " + label, callback: () => this.setRoute(route) });
    }
    this.addCommand({ id: "submit-on", name: "Buttons that ask for it: press Enter for me",
                      callback: () => this.setSubmit("on") });
    this.addCommand({ id: "submit-off", name: "Buttons: paste only, never press Enter",
                      callback: () => this.setSubmit("off") });
    this.addCommand({ id: "sync-claudian", name: "Set Claudian's model to gt's profile (shows changes first)",
                      callback: () => this.syncClaudian() });
    this.addCommand({ id: "undo-claudian-sync", name: "Undo the last Claudian model sync",
                      callback: () => this.undoClaudianSync() });
    // the age in the allowance line moves on while the panel stays open
    this.registerInterval(window.setInterval(() => this.refreshViews(), 60000));
  }

  async refreshViews() {
    for (const leaf of this.app.workspace.getLeavesOfType(VIEW)) {
      if (leaf.view && typeof leaf.view.render === "function") await leaf.view.render();
    }
  }

  /* the panel's own settings (data.json); the model sync's lastSync lives beside them */
  async getSettings() {
    const d = (await this.loadData()) || {};
    return { route: d.route === "claudian" || d.route === "terminal" ? d.route : "clipboard",
             submit: d.submit === "on" || d.submit === "off" ? d.submit : null };
  }

  async updateData(patch) {
    const d = (await this.loadData()) || {};
    await this.saveData(Object.assign({}, d, patch));
  }

  async setRoute(route) {
    await this.updateData({ route });
    new obsidian.Notice("GT Panel buttons: " + (route === "clipboard" ? "copy to the clipboard" : "send to " + ROUTES[route].name) + ".");
  }

  async setSubmit(v) {
    await this.updateData({ submit: v });
    new obsidian.Notice(v === "on" ? "Buttons that ask for it will press Enter after pasting." : "Buttons will only paste; you press Enter.");
  }

  /* one click: by the chosen route, else the clipboard. Never submits unless the button asks and
   * the owner has chosen it; a refused or failed route copies instead and says why. */
  async send(b) {
    const s = await this.getSettings();
    if (s.route === "clipboard") return this.copy(b);
    const text = oneLine(b.prompt);
    const probe = probeRoute(s.route, this.app);
    if (!text || !probe.ok) {
      if (text) new obsidian.Notice(probe.reason, 12000);
      return this.copy(b);
    }
    let submit = false;
    if (wantsSubmit(b)) {
      if (s.submit === "on") submit = true;
      else if (s.submit === null) {
        const a = await new AskSubmitModal(this.app).ask();
        if (a !== null) { await this.updateData({ submit: a ? "on" : "off" }); submit = a; }
      }
    }
    const res = s.route === "claudian" ? await this.pushClaudian(probe.plugin, text, submit)
                                       : this.pushTerminal(probe.target, text, submit);
    if (!res.ok) { new obsidian.Notice(res.reason, 12000); return this.copy(b); }
    new obsidian.Notice((submit && res.submitted ? "Sent: " : "Pasted: ") + b.label + (submit && !res.submitted ? ". Press Enter." : ""));
  }

  async pushClaudian(c, text, submit) {
    try {
      await c.activateView();
      const view = c.getView();
      if (!view || typeof view.appendToActiveInput !== "function") {
        return { ok: false, reason: "Claudian's chat has no appendToActiveInput method. The prompt was copied instead." };
      }
      if (view.appendToActiveInput(text) !== true) return { ok: false, reason: "Claudian's chat is not ready. The prompt was copied instead." };
      let submitted = false;
      if (submit) {
        const tab = typeof view.getActiveTab === "function" ? view.getActiveTab() : null;
        const el = tab && tab.dom && tab.dom.inputEl;
        const win = el && el.ownerDocument && el.ownerDocument.defaultView;
        if (win && typeof win.KeyboardEvent === "function" && typeof el.dispatchEvent === "function") {
          el.dispatchEvent(new win.KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true, cancelable: true }));
          submitted = true;
        }
      }
      return { ok: true, submitted };
    } catch (e) {
      return { ok: false, reason: "Claudian would not take the prompt. The prompt was copied instead." };
    }
  }

  pushTerminal(term, text, submit) {
    try {
      term.paste(text);
      let submitted = false;
      if (submit && typeof term.input === "function") { term.input("\r", true); submitted = true; }
      return { ok: true, submitted };
    } catch (e) {
      return { ok: false, reason: "The Terminal would not take the prompt. The prompt was copied instead." };
    }
  }

  async loadJson(name) {
    try { return JSON.parse(await this.app.vault.adapter.read(this.manifest.dir + "/" + name)); }
    catch (e) { return null; }
  }

  /* Claudian's plugin object, through Obsidian's own plugin registry; null when absent or when
   * the object cannot be saved (the route was tested only with a saveSettings method). */
  getClaudian() {
    const reg = this.app.plugins && this.app.plugins.plugins;
    const c = reg && reg[CLAUDIAN_ID];
    return c && typeof c === "object" ? c : null;
  }

  async syncClaudian() {
    const c = this.getClaudian();
    if (!c) { new obsidian.Notice("Claudian is not installed or not turned on in this vault."); return; }
    const version = c.manifest && c.manifest.version;
    if (typeof c.saveSettings !== "function") {
      new obsidian.Notice("This Claudian version cannot be set from the GT Panel (no saveSettings). Nothing was changed.", 10000);
      return;
    }
    const plan = planSync(await this.loadJson("profile.json"), c.settings, version);
    if (!plan.ok) { new obsidian.Notice(plan.reason, 12000); return; }
    if (!plan.changes.length) { new obsidian.Notice("Claudian already matches gt's model profile."); return; }
    new SyncModal(this.app, plan.changes, () => this.applySync(c, plan.changes)).open();
  }

  async applySync(c, changes) {
    const before = {};
    for (const ch of changes) before[ch.key] = c.settings[ch.key];
    try {
      for (const ch of changes) c.settings[ch.key] = ch.to;
      await c.saveSettings();
    } catch (e) {
      for (const ch of changes) {
        if (before[ch.key] === undefined) delete c.settings[ch.key]; else c.settings[ch.key] = before[ch.key];
      }
      new obsidian.Notice("Claudian was not changed: it would not save.", 10000);
      return false;
    }
    await this.updateData({ lastSync: { before, after: Object.fromEntries(changes.map((ch) => [ch.key, ch.to])) } });
    new obsidian.Notice("Claudian set to gt's profile. Undo is in the command list.");
    return true;
  }

  async undoClaudianSync() {
    const c = this.getClaudian();
    const d = await this.loadData();
    const last = d && d.lastSync;
    if (!last || !last.before) { new obsidian.Notice("No Claudian sync to undo."); return; }
    if (!c || typeof c.saveSettings !== "function") { new obsidian.Notice("Claudian is not available to undo."); return; }
    const keep = {};
    try {
      for (const k of Object.keys(last.before)) {
        if (k !== "model" && k !== "effortLevel") continue;
        keep[k] = c.settings[k];
        if (last.before[k] === undefined) delete c.settings[k]; else c.settings[k] = last.before[k];
      }
      await c.saveSettings();
    } catch (e) {
      for (const k of Object.keys(keep)) c.settings[k] = keep[k];
      new obsidian.Notice("Claudian was not changed: it would not save.", 10000);
      return;
    }
    const rest = (await this.loadData()) || {};
    delete rest.lastSync;
    await this.saveData(rest);
    new obsidian.Notice("Claudian put back as it was before the sync.");
  }

  onunload() { this.app.workspace.detachLeavesOfType(VIEW); }

  async loadMenu() {
    try {
      return cleanMenu(await this.app.vault.adapter.read(this.manifest.dir + "/menu.json"));
    } catch (e) { return []; }
  }

  async show() {
    const ws = this.app.workspace;
    let leaf = ws.getLeavesOfType(VIEW)[0];
    if (!leaf) {
      leaf = ws.getRightLeaf(false);
      await leaf.setViewState({ type: VIEW, active: true });
    }
    ws.revealLeaf(leaf);
  }

  async copy(b) {
    try {
      await navigator.clipboard.writeText(b.prompt);
      new obsidian.Notice("Copied: " + b.label + ". Paste it into Claude.");
    } catch (e) {
      new obsidian.Notice("Could not copy. The prompt is: " + b.prompt, 15000);
    }
  }
}

GtPanelPlugin.pure = { allowanceLine, planSync, cleanMenu, probeRoute, stripLines, wantsSubmit, oneLine, TESTED };    // for the tests, which run without Obsidian
module.exports = GtPanelPlugin;
