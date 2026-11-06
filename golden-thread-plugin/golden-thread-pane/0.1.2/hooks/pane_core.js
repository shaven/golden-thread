// gt pane: the pure functions register.js imports. Nothing here touches Claude Code: no `$`,
// no files, no clock. Everything takes its input as an argument, so `node --test` can check it.

export const MAX_BUTTONS = 12
export const MAX_LABEL = 22
export const MAX_PROMPT = 500
// Hotkeys for buttons 1 to 12: the digit row, then q and w. Claude Code accepts only one digit or
// one lowercase letter as a Button hotkey (a '-' or '=' makes the whole pane fail to draw, found
// with `claude plugin test` on 2.1.295), so the 11th and 12th buttons take q and w.
export const HOTKEYS = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0', 'q', 'w']
// The personas whose installs ask "Add the gt button pane?" with yes already chosen.
export const PLAIN_PERSONAS = ['project-manager', 'product-manager', 'executive', 'leadership']
export const STALE_MS = 60 * 60 * 1000

export function hotkeyFor(i) {
  return Number.isInteger(i) && i >= 0 && i < HOTKEYS.length ? HOTKEYS[i] : undefined
}

export function indexForHotkey(key) {
  return HOTKEYS.indexOf(key)
}

// ---- the button list ------------------------------------------------------------------------
// A control character, a line or paragraph separator, or a bidi override: none may be in a
// label or a prompt. A prompt is one line of plain words.
// eslint-disable-next-line no-control-regex
const BAD_CHARS = /[\u0000-\u001f\u007f-\u009f\u2028\u2029\u202a-\u202e\u2066-\u2069]/

function problemWith(b, n) {
  if (!b || typeof b !== 'object' || Array.isArray(b)) return 'button ' + n + ' is not an object'
  if (typeof b.label !== 'string' || !b.label.trim()) return 'button ' + n + ' needs a label'
  if (typeof b.prompt !== 'string' || !b.prompt.trim()) return 'button ' + n + ' needs a prompt'
  if (BAD_CHARS.test(b.label) || BAD_CHARS.test(b.prompt)) {
    return 'button ' + n + ' has a control character in its label or prompt'
  }
  if ([...b.label].length > MAX_LABEL) return 'button ' + n + ' label is longer than ' + MAX_LABEL + ' characters'
  if ([...b.prompt].length > MAX_PROMPT) return 'button ' + n + ' prompt is longer than ' + MAX_PROMPT + ' characters'
  return ''
}

// Text of buttons.json -> [{label, prompt}], or throws an Error whose message is one line.
export function parseButtons(text) {
  let data
  try {
    data = JSON.parse(text)
  } catch (e) {
    throw new Error(String(text || '').trim() === '' ? 'file is empty (not JSON)' : 'not valid JSON')
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('expected {version, buttons}')
  if (data.version !== 1) throw new Error('version must be 1')
  if (!Array.isArray(data.buttons)) throw new Error('buttons must be a list')
  if (data.buttons.length === 0) throw new Error('needs at least one button')
  if (data.buttons.length > MAX_BUTTONS) throw new Error('at most ' + MAX_BUTTONS + ' buttons')
  const out = []
  for (let i = 0; i < data.buttons.length; i++) {
    const why = problemWith(data.buttons[i], i + 1)
    if (why) throw new Error(why)
    out.push({ label: data.buttons[i].label, prompt: data.buttons[i].prompt })
  }
  return out
}

export function serializeButtons(buttons) {
  return JSON.stringify({ version: 1, buttons: buttons.map((b) => ({ label: b.label, prompt: b.prompt })) }, null, 2) + '\n'
}

// ---- settings and personas, read from the text of ~/.claude/vault-config.json ---------------
function configObject(text) {
  try {
    const d = JSON.parse(text)
    return d && typeof d === 'object' && !Array.isArray(d) ? d : {}
  } catch (e) {
    return {}
  }
}

export function parseSetting(text) {
  const v = configObject(text).pane_at_start
  return v === 'closed' || v === 'off' || v === 'open' ? v : 'open'
}

export function parseStatusSetting(text) {
  return configObject(text).pane_status === 'off' ? 'off' : 'on'
}

// pane_buttons (0.22.0): how many of the buttons the pane draws, 3 to 12. A missing or unusable value means
// all twelve, so nothing changes until someone sets it.
export function parseButtonCount(text) {
  const raw = configObject(text).pane_buttons
  const n = typeof raw === 'number' ? raw : typeof raw === 'string' && /^[0-9]+$/.test(raw.trim()) ? Number(raw.trim()) : NaN
  return Number.isInteger(n) && n >= 3 && n <= MAX_BUTTONS ? n : MAX_BUTTONS
}

// The buttons the pane draws: the first n. The list itself is never changed, and Arrange still works on all of it.
export function visibleButtons(list, n) {
  const k = Number.isInteger(n) && n >= 1 ? n : MAX_BUTTONS
  return list.slice(0, Math.min(k, MAX_BUTTONS))
}

export function parsePersonas(text) {
  const v = configObject(text).persona
  const raw = Array.isArray(v) ? v : typeof v === 'string' ? v.split(',') : []
  const out = []
  for (const x of raw) {
    const n = String(x).replace(/[[\]'"]/g, '').trim().toLowerCase()
    if (n && n !== 'none' && !out.includes(n)) out.push(n)
  }
  return out
}

function usablePane(pane) {
  if (!Array.isArray(pane) || pane.length === 0 || pane.length > MAX_BUTTONS) return null
  for (let i = 0; i < pane.length; i++) if (problemWith(pane[i], i + 1)) return null
  return pane.map((b) => ({ label: b.label, prompt: b.prompt }))
}

// The first persona with a usable `pane` in its pack wins; otherwise the generic list; otherwise [].
// packs: {personaName: parsedPackObject}. generic: a list of buttons, or null.
export function personaSeed(personas, packs, generic) {
  for (const p of personas || []) {
    const got = usablePane(packs && packs[p] && packs[p].pane)
    if (got) return got
  }
  return usablePane(generic) || []
}

// ---- order ----------------------------------------------------------------------------------
function inRange(list, i) {
  if (!Number.isInteger(i) || i < 0 || i >= list.length) throw new Error('position out of range')
}

export function moveButton(list, from, to) {
  inRange(list, from)
  inRange(list, to)
  const out = list.slice()
  const [one] = out.splice(from, 1)
  out.splice(to, 0, one)
  return out
}

export function swapButtons(list, a, b) {
  inRange(list, a)
  inRange(list, b)
  const out = list.slice()
  out[a] = list[b]
  out[b] = list[a]
  return out
}

// Shorten a label to `width` characters with an ellipsis, only when it must.
export function fitLabel(label, width) {
  const chars = [...label]
  if (width <= 0) return ''
  if (chars.length <= width) return label
  return chars.slice(0, width - 1).join('') + '\u2026'
}

// ---- arrange mode: a reducer, so the moves are checkable without a screen ------------------
export function arrangeStart(list) {
  return { list: list.slice(), selected: null, dirty: false }
}

export function arrangeReduce(state, action) {
  const { list, selected } = state
  switch (action.type) {
    case 'select':
      if (!Number.isInteger(action.index) || action.index < 0 || action.index >= list.length) return state
      return { ...state, selected: selected === action.index ? null : action.index }
    case 'up':
    case 'down':
    case 'top':
    case 'bottom': {
      if (selected === null) return state
      const to = action.type === 'up' ? selected - 1 : action.type === 'down' ? selected + 1
        : action.type === 'top' ? 0 : list.length - 1
      if (to < 0 || to >= list.length || to === selected) return state
      return { list: moveButton(list, selected, to), selected: to, dirty: true }
    }
    default:
      return state
  }
}

// ---- the band: one short dim line -----------------------------------------------------------
export function bandLine(st) {
  if (st && st.problem) return 'gt \u00b7 ' + st.problem + ' \u00b7 /gt-pane-buttons'
  if (st && st.arranging) return 'gt \u00b7 arranging: pick a button, move it with \u2191 \u2193, then Done'
  if (st && st.open) return 'gt \u00b7 press a button or its key \u00b7 /gt-pane-buttons closes the pane'
  return 'gt \u00b7 /gt-pane-buttons opens the button pane'
}

// ---- where the pane really is (0.21.4) -------------------------------------------------------
// `ui.open` answers { isPlaced: true } at session start BEFORE Claude Code has measured the terminal,
// so that answer only means "registered", not "drawn"; and the side column docks only in the
// fullscreen renderer. Under the classic (main-screen) renderer it does not draw, and the owner saw
// the one-line band alone (2026-10-13: it docked the moment `/tui fullscreen` was run).
export const FULLSCREEN_HINT = 'gt · pane docks only in fullscreen — run /tui fullscreen'

// Whether the buttons must ride in the band, and whether to tell the person why the pane is not docked.
//   st.open       the pane is open
//   st.surface    e.surface of a render event
//   st.viewport   e.viewport of a render event ({columns, rows, isFullscreen?}); absent until the terminal is
//                 measured, and then the pane's placement is only provisional
//   st.placed     the engine's record (ui.panes) once a viewport is known, else ui.open's answer
// The renderer is the terminal's own `isFullscreen` (true fullscreen, false classic, fixed per session);
// absent means unknown, and nothing is claimed. Only the terminal is told to run /tui.
// -> { narrow, hint }
export function placementPlan(st) {
  if (!st || !st.open) return { narrow: false, hint: false }
  const v = st.viewport
  const measured = !!v && typeof v.columns === 'number'
  const classic = measured && st.surface === 'terminal' && v.isFullscreen === false
  return { narrow: classic || st.placed === false, hint: classic }
}

// ---- the allowance footer: read-only figures from the usage module's saved readings ---------
// Text of readings.jsonl -> the newest line that has a plan figure, or null. Never throws.
export function newestReading(text) {
  if (typeof text !== 'string') return null
  const lines = text.split('\n')
  for (let i = lines.length - 1; i >= 0; i--) {
    if (!lines[i].trim()) continue
    let r
    try {
      r = JSON.parse(lines[i])
    } catch (e) {
      continue
    }
    if (!r || typeof r !== 'object' || typeof r.ts !== 'number') continue
    const f = typeof r.five_hour === 'number'
    const w = typeof r.seven_day === 'number'
    if (f || w) return r
  }
  return null
}

// Text of the usage module's latest.json (the newest reading alone, written whole each time a reading is
// recorded) -> that reading, or null when it is not one. Never throws. The ledger is the fallback.
export function latestReading(text) {
  if (typeof text !== 'string') return null
  let r
  try {
    r = JSON.parse(text)
  } catch (e) {
    return null
  }
  if (!r || typeof r !== 'object' || Array.isArray(r) || typeof r.ts !== 'number') return null
  if (typeof r.five_hour === 'number' || typeof r.seven_day === 'number') return r
  return null
}

function pad2(n) {
  return (n < 10 ? '0' : '') + n
}

function ageWords(ms) {
  const h = Math.floor(ms / 3600000)
  if (h >= 24) return Math.floor(h / 24) + 'd ago'
  return Math.max(h, 1) + 'h ago'
}

// reading {ts (seconds), five_hour, seven_day}, nowMs, tzOffsetMinutes (minutes EAST of UTC; omit
// for this machine's zone) -> {lines, text, stale} or null when there is nothing to show. One figure
// to a line, so the narrow pane never wraps one mid-figure (owner, 2026-10-10: "on top of each
// other ... rather than all in a row and have it wrap weird"); `text` is the lines joined by \n.
export function usageFooter(reading, nowMs, tzOffsetMinutes) {
  if (!reading || typeof reading.ts !== 'number') return null
  const parts = []
  if (typeof reading.five_hour === 'number') parts.push('5-hour ' + Math.round(reading.five_hour) + '%')
  if (typeof reading.seven_day === 'number') parts.push('weekly ' + Math.round(reading.seven_day) + '%')
  if (parts.length === 0) return null
  const at = new Date(reading.ts * 1000)
  const east = typeof tzOffsetMinutes === 'number' ? tzOffsetMinutes : -at.getTimezoneOffset()
  const local = new Date(at.getTime() + east * 60000)
  const age = Math.max(0, nowMs - reading.ts * 1000)
  const stale = age > STALE_MS
  parts.push('as of ' + pad2(local.getUTCHours()) + ':' + pad2(local.getUTCMinutes()) +
             (stale ? ' (' + ageWords(age) + ')' : ''))
  return { lines: parts, text: parts.join('\n'), stale }
}

// This session's own figures, below the allowance (owner, 2026-10-11: "why is it not showing the
// context ... nor the cache?"): contextPct from the session usage call, turnUsage the main loop's last
// turn.complete usage. The cache figure is the share of that turn's input the prompt cache served.
// -> lines (possibly empty); a figure not known yet is left out, never shown as 0.
export function sessionLines(contextPct, turnUsage) {
  const lines = []
  if (typeof contextPct === 'number') lines.push('context ' + Math.round(contextPct) + '%')
  if (turnUsage && typeof turnUsage === 'object') {
    const read = turnUsage.cache_read_input_tokens || 0
    const total = read + (turnUsage.cache_creation_input_tokens || 0) + (turnUsage.input_tokens || 0)
    if (total > 0) lines.push('cache ' + Math.round((100 * read) / total) + '%')
  }
  return lines
}

// pane_status off: nothing is drawn and the usage file is not even read.
export function footerPlan(status) {
  return status === 'off' ? { read: false, draw: false } : { read: true, draw: true }
}

// ---- the one-time tip when the pane is first closed ----------------------------------------------
export const CLOSE_TIP = "To turn off the panel, make the change in settings using this command: /gt:gt-settings pane_at_start off  (or 'closed' to keep it loaded but closed; /gt-pane-buttons reopens it)"

// Text of the small state file beside the button list -> true when the tip has not been shown yet.
// A missing, empty or unreadable file means not shown.
export function shouldShowTip(stateText) {
  try {
    const d = JSON.parse(stateText)
    return !(d && typeof d === 'object' && d.close_tip_shown === true)
  } catch (e) {
    return true
  }
}

export function serializeTipState() {
  return JSON.stringify({ close_tip_shown: true }) + '\n'
}
