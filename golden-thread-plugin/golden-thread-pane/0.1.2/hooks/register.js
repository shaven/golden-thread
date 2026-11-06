// gt pane: twelve plain-words buttons in a side pane. Press one and its prompt is sent as if you
// had typed it. The list is the user's own file, ~/.claude/golden-thread/pane/buttons.json,
// edited with /gt-pane:gt-pane (plain words), the CLI it calls, or Arrange mode in the pane.
//
// What this file may reach (tests/test_pane_module.py pins the list): the pane and band, the
// /gt-pane-buttons command, prompt.submit, one command.run (the button editor, /gt-pane:gt-pane),
// a read of six small files under ~/.claude, one write (the button list, on Arrange > Done) and
// HOME. No shell, no network, no process, no model.
import {
  hotkeyFor, parseButtons, serializeButtons, parseSetting, parseStatusSetting, parseButtonCount, visibleButtons,
  parsePersonas, personaSeed, fitLabel, arrangeStart, arrangeReduce, bandLine, newestReading, latestReading,
  usageFooter, sessionLines, footerPlan, CLOSE_TIP, shouldShowTip, serializeTipState, placementPlan, FULLSCREEN_HINT,
} from './pane_core.js'

const PANE = 'gt'
const WIDTH = 28                     // columns asked for when docked beside the transcript
const FOOTER_EVERY_MS = 5000         // never read the usage file twice inside this
const EDITOR = 'gt-pane:gt-pane'     // Update Actions runs this: the skill that changes the buttons
const EDITOR_WORDS = 'Help me update the actions of my gt pane buttons: show my buttons and ask which one to change.'
const HELP = 'gt:gt-help'           // Open Help runs this: the offline help page for your role
const HELP_WORDS = 'Open the gt help page for me.'

let setting = 'open'                 // pane_at_start: open | closed | off
let statusSetting = 'on'             // pane_status: on | off
let countSetting = 12               // pane_buttons: 3 to 12, how many buttons are drawn
let buttons = []
let problem = ''
let isOpen = false
// Where the pane really is (0.21.4). ui.open answers isPlaced:true at session start, before Claude Code has
// measured the terminal, and the side column docks only in the fullscreen renderer; so what ui.open said is
// provisional. Once a draw carries a viewport the engine's record is read again, and the band decides
// (placementPlan) whether it must carry the buttons itself and say that the pane docks only in fullscreen.
let placed = true                    // ui.open's answer until the viewport is known, then the engine's record
let viewport = null                  // e.viewport of the last draw that had one: {columns, rows, isFullscreen?}
let surface = ''                     // e.surface of that draw
let checkedFor = ''                  // the window the engine's record was last read for
let arrange = null                   // arrangeStart(...) while arranging, else null
let footer = null                    // {lines, text, stale} or null
let footerAt = 0
let live = []                        // sessionLines(): this session's context and cache
let lastTurn = null                  // the main loop's last turn.complete usage
let tipThisSession = false          // the Close tip, shown once ever (state.json) and once per session

function dirOf(home) {
  return home + '/.claude/golden-thread'
}
function buttonsPath(home) {
  return dirOf(home) + '/pane/buttons.json'
}
function statePath(home) {
  return dirOf(home) + '/pane/state.json'
}

async function homeDir($) {
  const h = (await $.env.get('HOME')) || (await $.env.get('USERPROFILE')) || ''
  return h
}

async function readText($, path) {
  try {
    return await $.fs.read(path)
  } catch (err) {
    return ''
  }
}

async function readConfig($, home) {
  const text = await readText($, home + '/.claude/vault-config.json')
  setting = parseSetting(text)
  statusSetting = parseStatusSetting(text)
  countSetting = parseButtonCount(text)
  return text
}

// The first-run fallback: the install's session hook seeds the list, but if it has not, build
// the same list here from the installed persona packs. Never overwrites a file that exists.
async function seedList($, home, configText) {
  const packs = {}
  for (const p of parsePersonas(configText)) {
    const t = await readText($, dirOf(home) + '/packs/personas/' + p + '.json')
    try {
      packs[p] = JSON.parse(t)
    } catch (err) {
      packs[p] = {}
    }
  }
  let generic = null
  try {
    generic = JSON.parse(await readText($, dirOf(home) + '/packs/pane/generic.json')).pane
  } catch (err) {
    generic = null
  }
  const list = personaSeed(parsePersonas(configText), packs, generic)
  if (list.length > 0) await $.fs.write(buttonsPath(home), serializeButtons(list))
  return list
}

async function loadButtons($, home, configText) {
  try {
    if (!(await $.fs.exists(buttonsPath(home)))) {
      const seeded = await seedList($, home, configText)
      if (seeded.length === 0) throw new Error('no button list yet; ask me to reset the gt pane')
      buttons = seeded
      problem = ''
      return
    }
    buttons = parseButtons(await $.fs.read(buttonsPath(home)))
    problem = ''
  } catch (err) {
    buttons = []
    problem = 'buttons.json: ' + (err && err.message ? err.message : 'unreadable') + ' (ask me to reset the gt pane)'
  }
}

// The allowance footer. With pane_status off the usage file is not read at all.
async function loadFooter($, home, force) {
  const plan = footerPlan(statusSetting)
  if (!plan.read) {
    footer = null
    live = []
    return
  }
  const now = Date.now()
  if (!force && now - footerAt < FOOTER_EVERY_MS) return
  footerAt = now
  // the newest reading alone first (the ledger grows without bound); the ledger only when that is missing
  let reading = latestReading(await readText($, dirOf(home) + '/usage/latest.json'))
  if (!reading) reading = newestReading(await readText($, dirOf(home) + '/usage/readings.jsonl'))
  footer = usageFooter(reading, now)
  let pct = null
  try {
    pct = (await $.session.usage()).context.percent
  } catch (err) {
    // no session bound yet: the context line waits for the next read
  }
  live = sessionLines(pct, lastTurn)
}

async function reloadAll($, force) {
  const home = await homeDir($)
  const configText = await readConfig($, home)
  if (setting === 'off') return
  await loadButtons($, home, configText)
  await loadFooter($, home, force)
}

async function openPane($, byPerson) {
  const spec = { id: PANE, title: 'gt', columns: WIDTH, closeOnEscape: true }
  const r = await $.ui.open(byPerson ? { ...spec, focus: true } : spec)
  isOpen = true
  placed = !!r.isPlaced              // provisional: settlePlacement() checks it once the window is measured
  checkedFor = ''
  $.ui.invalidate('ui.render')
}

// Read the engine's own record of the pane once a draw carries a viewport, and again when the window
// changes size (an unasked pane waiting undrawn is seated when the terminal widens). Not on every draw.
async function settlePlacement($, e) {
  const v = e && e.viewport
  if (!v || typeof v.columns !== 'number') return
  viewport = v
  surface = e.surface
  if (!isOpen) return
  const key = surface + ':' + v.columns + ':' + v.isFullscreen
  if (key === checkedFor) return
  checkedFor = key
  try {
    const mine = (await $.ui.panes()).find((p) => p.id === PANE)
    if (mine && typeof mine.isPlaced === 'boolean') placed = mine.isPlaced
  } catch (err) {
    // no record to read: ui.open's answer stands
  }
}

async function closePane($) {
  await $.ui.close({ id: PANE })
  isOpen = false
  placed = true
  arrange = null
  $.ui.invalidate('ui.render')
}

// The first time ever the pane is closed (the x Claude Code draws on its border, Esc, /gt-pane-buttons),
// say how to turn it off. It changes no setting; /gt-pane-buttons brings the pane back. Until 0.21.0 the
// pane drew a second x of its own for this, beside the border's (owner, 2026-10-10: "Why are
// there two close buttons on the panel?").
async function closeTip($) {
  const home = await homeDir($)
  if (tipThisSession || !shouldShowTip(await readText($, statePath(home)))) return
  tipThisSession = true
  try {
    await $.fs.write(statePath(home), serializeTipState())
  } catch (err) {
    // cannot record it: it shows once this session and may show again next time
  }
  await $.ui.log(CLOSE_TIP)
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    try {
      await reloadAll($, true)
      if (setting === 'open') {
        try {
          await openPane($, false)
        } catch (err) {
          placed = false
        }
      }
    } catch (err) {
      problem = 'gt pane: could not start'
    }
    try {
      await $.command.register({ name: 'gt-pane-buttons', description: 'Show or hide the gt button pane' })
    } catch (err) {
      // a name another mod holds: the pane still works from the band
    }
    return next(e)
  })

  on('command.run', { command: 'gt-pane-buttons' }, async ($) => {
    await reloadAll($, true)
    if (setting === 'off') {
      return { text: 'The gt pane is switched off. To use it, ask Claude to set pane_at_start to open.' }
    }
    const open = (await $.ui.panes()).some((p) => p.id === PANE)
    if (open) {
      await closePane($)
      return {}
    }
    await openPane($, true)
    return {}
  })

  // The cache line: the main loop's last turn (a subagent's turn carries agentId and is skipped).
  on('turn.complete', async ($, e, next) => {
    const r = await next(e)
    try {
      const u = (r && r.usage) || e.usage
      if (!e.agentId && u && statusSetting !== 'off') {
        lastTurn = u
        footerAt = 0
        $.ui.invalidate('ui.render')
      }
    } catch (err) {
      // a figure in the pane never costs a turn its result
    }
    return r
  })

  // The user closed it (Esc, the x on its border, Ctrl+X X): forget that it was open.
  on('ui.close', async ($, e, next) => {
    if (e.id !== PANE) return next(e)
    isOpen = false
    placed = true
    arrange = null
    const r = await next(e)
    try {
      await closeTip($)
    } catch (err) {
      // the tip is a courtesy: a close never fails over it
    }
    return r
  }).catch(() => {})

  on('ui.render', { component: 'Pane' }, async ($, e, next) => {
    if (e.requestId !== PANE || setting === 'off') return next(e)
    if (!placed) {
      // Claude Code raises this draw only for a pane it has seated: if the record said otherwise a moment
      // ago, the record was stale. Once, so the band stops carrying the buttons.
      placed = true
      $.ui.invalidate('ui.render')
    }
    const { Box, Text, Button } = $.ui.resolve(e)
    if (problem) return Text({ color: 'red', children: [problem] })
    try {
      await loadFooter($, await homeDir($), false)
    } catch (err) {
      footer = null
    }
    const cols = (e.props && e.props.bodyColumns) || WIDTH - 2
    const redraw = () => $.ui.invalidate('ui.render')
    const rows = arrange ? arrangeRows({ Box, Text, Button }, cols, redraw) : visibleButtons(buttons, countSetting).map((b, i) => card({ Box, Button }, $, b, i, cols))
    const foot = footerRow({ Text })
    const tail = arrange ? arrangeControls({ Box, Button }, $, redraw)
      : [openArrange({ Button }, redraw), updateActions({ Button }, $), openHelp({ Button }, $)]
    return Box({ flexDirection: 'column', children: [...rows, ...tail, ...foot] })
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (setting === 'off') return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    const theirs = await next(e)
    try {
      await settlePlacement($, e)
    } catch (err) {
      // the band still draws from what it knew
    }
    const plan = placementPlan({ open: isOpen, surface, viewport, placed })
    const note = Text({
      dimColor: true,
      children: [bandLine({ problem: isOpen && !plan.narrow ? '' : problem, open: isOpen, arranging: !!arrange })],
    })
    // the pane's own red line is not on screen when the buttons ride in the band, so the band names the problem
    const hint = plan.hint ? [Text({ dimColor: true, wrap: 'truncate-end', children: [FULLSCREEN_HINT] })] : []
    const row = plan.narrow && isOpen && !problem && buttons.length > 0
      ? [Box({ flexDirection: 'row', columnGap: 2, flexWrap: 'wrap', children: visibleButtons(buttons, countSetting).map((b, i) => press({ Button }, $, b, i, false)) })]
      : []
    return Box({ flexDirection: 'column', children: [note, ...hint, ...row, theirs] })
  })
}

// A Button that submits the prompt as the person's own words.
function press({ Button }, $, b, i, withKey) {
  return Button({
    key: 'gt-' + i,
    label: b.label,
    hotkey: withKey ? hotkeyFor(i) : undefined,
    plain: true,
    onPress: async () => {
      await $.prompt.submit({ text: b.prompt, asUser: true })
    },
  })
}

// A bordered round card around the button; the border lights on hover.
function card({ Box, Button }, $, b, i, cols) {
  const shown = { label: fitLabel(b.label, Math.max(cols - 7, 4)), prompt: b.prompt }
  return Box({
    key: 'card-' + i,
    borderStyle: 'round',
    paddingX: 1,
    width: cols,
    hover: { scope: 'gt-' + i, borderColor: 'cyan' },
    children: [press({ Button }, $, shown, i, true)],
  })
}

// One figure to a line: each is under 26 characters, the pane's width, so none wraps.
function footerRow({ Text }) {
  const allowance = footer ? footer.lines.map((line, i) =>
    Text({ key: 'gt-footer-' + i, dimColor: footer.stale, wrap: 'wrap', children: [line] })) : []
  return allowance.concat(live.map((line, i) =>
    Text({ key: 'gt-live-' + i, wrap: 'wrap', children: [line] })))
}

function openArrange({ Button }, redraw) {
  return Button({
    key: 'gt-arrange',
    label: 'Arrange',
    hotkey: 'a',
    onPress: () => {
      arrange = arrangeStart(buttons)
      redraw()
    },
  })
}

// Update Actions (owner, 2026-10-13): below Arrange, it opens the button editor -- the
// /gt-pane:gt-pane skill that renames a button or changes the words it sends. Arrange only
// reorders. prompt.submit refuses a text that starts with a slash, so the editor is run as a
// command; if this session has no command of that name, the same request goes as plain words.
function updateActions({ Button }, $) {
  return Button({
    key: 'gt-update-actions',
    label: 'Update Actions',
    hotkey: 'e',
    onPress: async () => {
      try {
        await $.command.run({ command: EDITOR })
      } catch (err) {
        await $.prompt.submit({ text: EDITOR_WORDS, asUser: true })
      }
    },
  })
}

// Open Help (owner, 2026-10-16): below Update Actions, it opens /gt:gt-help, the offline help page
// for the user's role. Fixed like Arrange and Update Actions, so it never takes one of the 12
// buttons; as there, a session with no command of that name gets the same request as plain words.
function openHelp({ Button }, $) {
  return Button({
    key: 'gt-open-help',
    label: 'Open Help',
    hotkey: 'h',
    onPress: async () => {
      try {
        await $.command.run({ command: HELP })
      } catch (err) {
        await $.prompt.submit({ text: HELP_WORDS, asUser: true })
      }
    },
  })
}

// Arrange mode: pick a button (its key or a press), move it with the up and down marks. Mods
// cannot bind the arrow keys (Claude Code keeps Tab and arrows for moving between controls), so
// the marks are buttons: Tab to one and press Enter, or use their letter keys.
function arrangeRows({ Box, Text, Button }, cols, redraw) {
  const act = (action) => () => {
    arrange = arrangeReduce(arrange, action)
    redraw()
  }
  // A mark selects its row first, then moves it, so one press is enough.
  const nudge = (i, type) => () => {
    let s = arrange
    if (s.selected !== i) s = arrangeReduce(s, { type: 'select', index: i })
    arrange = arrangeReduce(s, { type })
    redraw()
  }
  return arrange.list.map((b, i) => {
    const picked = arrange.selected === i
    return Box({
      key: 'row-' + i,
      flexDirection: 'row',
      columnGap: 1,
      borderStyle: 'round',
      borderColor: picked ? 'cyan' : undefined,
      paddingX: 1,
      width: cols,
      children: [
        Button({ key: 'gt-' + i, label: (picked ? '> ' : '') + fitLabel(b.label, Math.max(cols - 14, 4)), hotkey: hotkeyFor(i), plain: true, onPress: act({ type: 'select', index: i }) }),
        Button({ key: 'up-' + i, label: '\u2191', plain: true, onPress: nudge(i, 'up') }),
        Button({ key: 'down-' + i, label: '\u2193', plain: true, onPress: nudge(i, 'down') }),
      ],
    })
  })
}

function arrangeControls({ Box, Button }, $, redraw) {
  const act = (action) => () => {
    arrange = arrangeReduce(arrange, action)
    redraw()
  }
  return [
    Box({
      key: 'arrange-controls',
      flexDirection: 'row',
      columnGap: 2,
      flexWrap: 'wrap',
      children: [
        Button({ key: 'gt-move-up', label: 'Up', hotkey: 'u', plain: true, onPress: act({ type: 'up' }) }),
        Button({ key: 'gt-move-down', label: 'Down', hotkey: 'd', plain: true, onPress: act({ type: 'down' }) }),
        Button({
          key: 'gt-done',
          label: 'Done',
          hotkey: 's',
          plain: true,
          onPress: async () => {
            if (arrange.dirty) {
              try {
                const home = await homeDir($)
                await $.fs.write(buttonsPath(home), serializeButtons(arrange.list))
                buttons = arrange.list
                problem = ''
              } catch (err) {
                problem = 'could not save the new order'
              }
            }
            arrange = null
            redraw()
          },
        }),
        Button({ key: 'gt-cancel', label: 'Cancel', hotkey: 'c', plain: true, onPress: () => { arrange = null; redraw() } }),
      ],
    }),
  ]
}
