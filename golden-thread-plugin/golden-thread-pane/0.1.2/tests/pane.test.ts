// claude plugin test golden-thread-pane/0.1.2   (Claude Code 2.1.287 or later; no session, no network)
import { expect, test } from 'claude-code/testing'

const HOME = '/home/t'
const LIST = HOME + '/.claude/golden-thread/pane/buttons.json'
const CONFIG = HOME + '/.claude/vault-config.json'
const USAGE = HOME + '/.claude/golden-thread/usage/readings.jsonl'
const LATEST = HOME + '/.claude/golden-thread/usage/latest.json'

const twelve = Array.from({ length: 12 }, (_, i) => ({ label: 'Button ' + (i + 1), prompt: 'Do thing ' + (i + 1) }))
const listText = (b = twelve) => JSON.stringify({ version: 1, buttons: b })

function PANE(bodyColumns = 26, surface: 'terminal' | 'desktop' = 'terminal') {
  return {
    plugin: 'gt-pane', component: 'Pane', requestId: 'gt', surface,
    viewport: { columns: 160, rows: 40 },
    props: { title: 'gt', isFocused: true, bodyColumns, placement: 'dock', scroll: { offset: 0, bodyRows: 30 }, view: {} },
  } as const
}
const BAND = {
  plugin: 'gt-pane', component: 'AbovePrompt', requestId: 'band', surface: 'terminal',
  viewport: { columns: 100, rows: 40 },
  props: { hasSurvey: false, isWorking: false, maxRows: 6, bodyColumns: 100, scroll: { offset: 0, bodyRows: 6 }, view: {} },
} as const

// Stubs for everything the mod reaches. files: path -> text; returns what was written/submitted/read.
// opts.placed: what ui.open answers at session start. opts.recordPlaced: what $.ui.panes() says of the open pane
// (the engine's record; read at call time, so a test may change it between draws). Left out, the record carries no isPlaced.
function world(on: any, files: Record<string, string>, opts: { placed?: boolean; panes?: string[]; noEditor?: boolean; recordPlaced?: boolean } = {}) {
  const seen = { writes: [] as Array<[string, string]>, submits: [] as string[], reads: [] as string[], opened: [] as any[], closes: 0, logs: [] as string[], runs: [] as string[], paneReads: 0 }
  on('env.get', ($: any, e: any) => ({ value: e.name === 'HOME' ? HOME : undefined }))
  on('fs.exists', ($: any, e: any) => ({ value: e.path in files }))
  on('fs.read', ($: any, e: any) => {
    seen.reads.push(e.path)
    if (!(e.path in files)) return { deny: 'ENOENT' }
    return { value: files[e.path] }
  })
  on('fs.write', ($: any, e: any) => { seen.writes.push([e.path, e.text]); files[e.path] = e.text; return { value: undefined } })
  on('ui.open', ($: any, e: any) => { seen.opened.push(e); return { value: { isPlaced: opts.placed !== false } } })
  on('ui.close', () => { seen.closes += 1; return { value: undefined } })
  on('ui.log', ($: any, e: any) => { seen.logs.push(e.text); return { value: undefined } })
  on('ui.panes', () => {
    seen.paneReads += 1
    return { value: (opts.panes || []).map((id) => (typeof opts.recordPlaced === 'boolean' ? { id, isPlaced: opts.recordPlaced } : { id })) }
  })
  on('command.register', () => ({ value: undefined }))
  on('command.run', ($: any, e: any) => {
    if (e.command === 'gt-pane-buttons') return {}
    if (opts.noEditor) throw new Error('no command named /' + e.command + ' in this session')
    seen.runs.push(e.command)
    return { text: '' }
  })
  on('prompt.submit', ($: any, e: any) => { seen.submits.push(e.text); return { text: e.text } })
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['drawn by Claude Code'] }))
  on('session.start', () => ({ cwd: '/work' }))
  on('turn.complete', ($: any, e: any) => ({ text: '', usage: e.usage }))
  return seen
}

test('every one of the twelve buttons draws, with its hotkey, in both apps', async ($, on) => {
  world(on, { [LIST]: listText() })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount(PANE(26, surface))
    for (let i = 0; i < 12; i++) expect(await ui.find({ key: 'gt-' + i })).toBeDefined()
    expect(await ui.find({ key: 'gt-12' })).toBeUndefined()
    const last = await ui.find({ key: 'gt-11' })
    expect(last.props.hotkey).toBe('w')
    expect((await ui.find({ key: 'gt-10' })).props.hotkey).toBe('q')
    expect((await ui.find({ key: 'gt-9' })).props.hotkey).toBe('0')
    expect(last.props.plain).toBe(true)
    await ui.unmount()
  }
})

test('pane_buttons 4: only four buttons draw, the fixed rows and the footer still do, Arrange lists all', async ($, on) => {
  world(on, { [LIST]: listText(), [USAGE]: reading(60000), [CONFIG]: JSON.stringify({ pane_buttons: '4' }) })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const ui = await $.ui.mount(PANE())
  for (let i = 0; i < 4; i++) expect(await ui.find({ key: 'gt-' + i })).toBeDefined()
  expect(await ui.find({ key: 'gt-4' })).toBeUndefined()
  expect(await ui.find({ key: 'gt-arrange' })).toBeDefined()
  expect(await ui.find({ key: 'gt-update-actions' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: /^5-hour/ })).toBeDefined()
  await ui.press({ key: 'gt-arrange' })
  for (let i = 0; i < 12; i++) expect(await ui.find({ key: 'gt-' + i })).toBeDefined()
})

test('pressing a button sends its prompt as the user', async ($, on) => {
  const seen = world(on, { [LIST]: listText() })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const ui = await $.ui.mount(PANE())
  await ui.press({ key: 'gt-2' })
  expect(seen.submits).toEqual(['Do thing 3'])
})

test('the pane opens at start when pane_at_start is open, docked at 28 columns', async ($, on) => {
  const seen = world(on, { [LIST]: listText(), [CONFIG]: JSON.stringify({ pane_at_start: 'open' }) })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  expect(seen.opened.length).toBe(1)
  expect(seen.opened[0].columns).toBe(28)
  expect(seen.opened[0].title).toBe('gt')
})

test('closed opens nothing at start, and off draws nothing at all', async ($, on) => {
  const seen = world(on, { [LIST]: listText(), [CONFIG]: JSON.stringify({ pane_at_start: 'closed' }) })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  expect(seen.opened.length).toBe(0)
})

test('off: the pane and the band are left to Claude Code', async ($, on) => {
  const seen = world(on, { [LIST]: listText(), [CONFIG]: JSON.stringify({ pane_at_start: 'off' }) })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  expect(seen.opened.length).toBe(0)
  const ui = await $.ui.mount(PANE())
  expect(await ui.find({ type: 'Text', text: 'drawn by Claude Code' })).toBeDefined()
  expect(await ui.find({ key: 'gt-0' })).toBeUndefined()
  const band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Text', text: /^gt/ })).toBeUndefined()
})

test('a damaged list shows one red line, not a crash', async ($, on) => {
  world(on, { [LIST]: '{ broken' })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const ui = await $.ui.mount(PANE())
  const line = await ui.find({ type: 'Text', text: /^buttons\.json:/ })
  expect(line).toBeDefined()
  expect(line.props.color).toBe('red')
  expect(await ui.find({ key: 'gt-0' })).toBeUndefined()
})

test('a missing list is built from the persona pack, and an existing one is never overwritten', async ($, on) => {
  const pack = { pane: [{ label: 'Mine', prompt: 'My words' }] }
  const seen = world(on, {
    [CONFIG]: JSON.stringify({ persona: 'executive' }),
    [HOME + '/.claude/golden-thread/packs/personas/executive.json']: JSON.stringify(pack),
  })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  expect(seen.writes.length).toBe(1)
  expect(seen.writes[0][0]).toBe(LIST)
  expect(JSON.parse(seen.writes[0][1]).buttons).toEqual(pack.pane)
})

test('narrow: when the pane cannot be placed the band shows the buttons as a wrapped row', async ($, on) => {
  world(on, { [LIST]: listText() }, { placed: false })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const band = await $.ui.mount(BAND)
  expect(await band.find({ type: 'Text', text: /^gt/ })).toBeDefined()
  expect(await band.find({ key: 'gt-0' })).toBeDefined()
  expect(await band.find({ key: 'gt-11' })).toBeDefined()
})

test('placed: the band is one dim line and no buttons', async ($, on) => {
  world(on, { [LIST]: listText() })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const band = await $.ui.mount(BAND)
  const line = await band.find({ type: 'Text', text: /^gt/ })
  expect(line.props.dimColor).toBe(true)
  expect(await band.find({ key: 'gt-0' })).toBeUndefined()
})

test('Arrange: pick a button, move it down with the mark, Done saves the new order', async ($, on) => {
  const seen = world(on, { [LIST]: listText() })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const ui = await $.ui.mount(PANE())
  await ui.press({ key: 'gt-arrange' })
  await ui.press({ key: 'gt-0' })
  await ui.press({ key: 'gt-move-down' })
  await ui.press({ key: 'down-1' })
  await ui.press({ key: 'gt-done' })
  expect(seen.writes.length).toBe(1)
  const saved = JSON.parse(seen.writes[0][1]).buttons.map((b: any) => b.label)
  expect(saved.slice(0, 3)).toEqual(['Button 2', 'Button 3', 'Button 1'])
  expect(seen.submits).toEqual([])
})

test('Update Actions sits below Arrange and opens the button editor', async ($, on) => {
  const seen = world(on, { [LIST]: listText() })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const ui = await $.ui.mount(PANE())
  const arrange = await ui.find({ key: 'gt-arrange' })
  const update = await ui.find({ key: 'gt-update-actions' })
  expect(update).toBeDefined()
  expect(update.props.label).toBe('Update Actions')
  expect(update.props.hotkey).toBe('e')
  expect(arrange).toBeDefined()
  await ui.press({ key: 'gt-update-actions' })
  expect(seen.runs).toEqual(['gt-pane:gt-pane'])
  expect(seen.submits).toEqual([])
  expect(seen.writes).toEqual([])
})

test('Update Actions falls back to plain words when the editor command is missing', async ($, on) => {
  const seen = world(on, { [LIST]: listText() }, { noEditor: true })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const ui = await $.ui.mount(PANE())
  await ui.press({ key: 'gt-update-actions' })
  expect(seen.submits.length).toBe(1)
  expect(seen.submits[0]).toMatch(/^Help me update the actions of my gt pane buttons/)
})

test('Open Help sits below Update Actions and opens the help page', async ($, on) => {
  const seen = world(on, { [LIST]: listText() })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const ui = await $.ui.mount(PANE())
  const help = await ui.find({ key: 'gt-open-help' })
  expect(help).toBeDefined()
  expect(help.props.label).toBe('Open Help')
  expect(help.props.hotkey).toBe('h')
  await ui.press({ key: 'gt-open-help' })
  expect(seen.runs).toEqual(['gt:gt-help'])
  expect(seen.submits).toEqual([])
  expect(seen.writes).toEqual([])
})

test('Open Help falls back to plain words when the help command is missing', async ($, on) => {
  const seen = world(on, { [LIST]: listText() }, { noEditor: true })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const ui = await $.ui.mount(PANE())
  await ui.press({ key: 'gt-open-help' })
  expect(seen.submits).toEqual(['Open the gt help page for me.'])
})

test('Open Help hides while arranging and comes back after', async ($, on) => {
  world(on, { [LIST]: listText() })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const ui = await $.ui.mount(PANE())
  await ui.press({ key: 'gt-arrange' })
  expect(await ui.find({ key: 'gt-open-help' })).toBeUndefined()
  await ui.press({ key: 'gt-cancel' })
  expect(await ui.find({ key: 'gt-open-help' })).toBeDefined()
})

test('Update Actions hides while arranging and comes back after', async ($, on) => {
  world(on, { [LIST]: listText() })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const ui = await $.ui.mount(PANE())
  await ui.press({ key: 'gt-arrange' })
  expect(await ui.find({ key: 'gt-update-actions' })).toBeUndefined()
  await ui.press({ key: 'gt-cancel' })
  expect(await ui.find({ key: 'gt-update-actions' })).toBeDefined()
})

test('Arrange > Cancel, or Done with no change, writes nothing', async ($, on) => {
  const seen = world(on, { [LIST]: listText() })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const ui = await $.ui.mount(PANE())
  await ui.press({ key: 'gt-arrange' })
  await ui.press({ key: 'gt-0' })
  await ui.press({ key: 'gt-move-down' })
  await ui.press({ key: 'gt-cancel' })
  await ui.press({ key: 'gt-arrange' })
  await ui.press({ key: 'gt-done' })
  expect(seen.writes).toEqual([])
})

const NOW = Date.now()
const reading = (ageMs: number) => JSON.stringify({ ts: Math.floor((NOW - ageMs) / 1000), five_hour: 12, seven_day: 61 }) + '\n'

test('footer: a fresh reading shows both figures, undimmed', async ($, on) => {
  world(on, { [LIST]: listText(), [USAGE]: reading(5 * 60000) })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const ui = await $.ui.mount(PANE())
  const f = await ui.find({ type: 'Text', text: /^5-hour/ })
  expect(f.children.join('')).toBe('5-hour 12%')
  expect((await ui.find({ type: 'Text', text: /^weekly/ })).children.join('')).toBe('weekly 61%')
  expect((await ui.find({ type: 'Text', text: /^as of/ })).children.join('')).toMatch(/^as of \d\d:\d\d$/)
  expect(f.props.dimColor).toBe(false)
})

test('footer: an old reading is dimmed and says its age; none at all shows nothing', async ($, on) => {
  world(on, { [LIST]: listText(), [USAGE]: reading(3 * 3600000 + 600000) })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const ui = await $.ui.mount(PANE())
  const f = await ui.find({ type: 'Text', text: /^5-hour/ })
  expect(f.props.dimColor).toBe(true)
  expect((await ui.find({ type: 'Text', text: /^as of/ })).children.join('')).toMatch(/\(3h ago\)$/)
})

test('footer: latest.json is read first and wins over the ledger', async ($, on) => {
  const newest = JSON.stringify({ ts: Math.floor((NOW - 60000) / 1000), five_hour: 33, seven_day: 44 }) + '\n'
  world(on, { [LIST]: listText(), [LATEST]: newest, [USAGE]: reading(5 * 60000) })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const ui = await $.ui.mount(PANE())
  const f = await ui.find({ type: 'Text', text: /^5-hour/ })
  expect(f.children.join('')).toBe('5-hour 33%')
  expect((await ui.find({ type: 'Text', text: /^weekly/ })).children.join('')).toBe('weekly 44%')
})

test('footer: a broken latest.json falls back to the ledger', async ($, on) => {
  world(on, { [LIST]: listText(), [LATEST]: '{not json', [USAGE]: reading(5 * 60000) })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const ui = await $.ui.mount(PANE())
  const f = await ui.find({ type: 'Text', text: /^5-hour/ })
  expect(f.children.join('')).toBe('5-hour 12%')
})

test('footer: no usage file, nothing drawn', async ($, on) => {
  world(on, { [LIST]: listText() })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const ui = await $.ui.mount(PANE())
  expect(await ui.find({ type: 'Text', text: /^5-hour/ })).toBeUndefined()
})

test('pane_status off: no footer, and the usage file is not even read', async ($, on) => {
  const seen = world(on, { [LIST]: listText(), [USAGE]: reading(60000), [CONFIG]: JSON.stringify({ pane_status: 'off' }) })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const ui = await $.ui.mount(PANE())
  expect(await ui.find({ type: 'Text', text: /^5-hour/ })).toBeUndefined()
  expect(seen.reads.filter((p) => p === USAGE || p === LATEST)).toEqual([])
})

// /gt-pane-buttons while the pane is open closes it: the same ui.close hook the border x raises (the
// test kit can run a command but cannot raise a person's close of a pane)
async function toggle($: any) {
  await $.command.run({ command: 'gt-pane-buttons', args: '', origin: { kind: 'user' } })
}

const TIP = "To turn off the panel, make the change in settings using this command: /gt:gt-settings pane_at_start off  (or 'closed' to keep it loaded but closed; /gt-pane-buttons reopens it)"
const STATE = HOME + '/.claude/golden-thread/pane/state.json'

test('Close: only Claude Code draws an x; the pane draws none of its own', async ($, on) => {
  world(on, { [LIST]: listText(), [CONFIG]: JSON.stringify({ pane_at_start: 'open' }) })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const ui = await $.ui.mount(PANE())
  expect(await ui.find({ key: 'gt-close-box' })).toBeUndefined()
  expect(await ui.find({ key: 'gt-close' })).toBeUndefined()
  expect(await ui.find({ key: 'gt-0' })).toBeDefined()
})

test('Close: the first close shows the tip once', async ($, on) => {
  const seen = world(on, { [LIST]: listText(), [CONFIG]: JSON.stringify({ pane_at_start: 'open' }) }, { panes: ['gt'] })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  await toggle($)
  expect(seen.closes).toBe(1)
  expect(seen.logs).toEqual([TIP])
  expect(JSON.parse(seen.writes.find((w) => w[0] === STATE)![1])).toEqual({ close_tip_shown: true })
  // the setting is untouched: nothing but the tip's own state file was written
  expect(seen.writes.map((w) => w[0])).toEqual([STATE])
})

test('Close: the second close, in this session or a later one, shows no tip', async ($, on) => {
  const files: Record<string, string> = { [LIST]: listText() }
  const seen = world(on, files, { panes: ['gt'] })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  await toggle($)
  await toggle($)
  expect(seen.closes).toBe(2)
  expect(seen.logs).toEqual([TIP])
  expect(STATE in files).toBe(true)
})

test('Close: with the state file already written the tip never shows', async ($, on) => {
  const seen = world(on, { [LIST]: listText(), [STATE]: JSON.stringify({ close_tip_shown: true }) }, { panes: ['gt'] })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  await toggle($)
  expect(seen.closes).toBe(1)
  expect(seen.logs).toEqual([])
  expect(seen.writes).toEqual([])
})

test('Close: /gt-pane-buttons that opens the pane shows no tip', async ($, on) => {
  const seen = world(on, { [LIST]: listText(), [CONFIG]: JSON.stringify({ pane_at_start: 'closed' }) })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  await toggle($)
  expect(seen.closes).toBe(0)
  expect(seen.logs).toEqual([])
})

test('footer: this session\'s context, and the cache share of the last main-loop turn', async ($, on) => {
  world(on, { [LIST]: listText() })
  on('session.usage', () => ({ value: { startedAt: 0, context: { window: 200000, tokens: 18000, percent: 9 }, rateLimits: [] } }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  await $.turn.complete({ turnId: 't1', isAborted: false, reason: 'answer', answer: 'ok', durationMs: 1,
    usage: { model: 'm', input_tokens: 10, cache_read_input_tokens: 980, cache_creation_input_tokens: 10, output_tokens: 5 } } as any)
  const ui = await $.ui.mount(PANE())
  expect((await ui.find({ type: 'Text', text: /^context/ })).children.join('')).toBe('context 9%')
  expect((await ui.find({ type: 'Text', text: /^cache/ })).children.join('')).toBe('cache 98%')
})

test('footer: a subagent\'s turn does not set the cache line', async ($, on) => {
  world(on, { [LIST]: listText() })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  await $.turn.complete({ turnId: 't2', agentId: 'a1', isAborted: false, reason: 'answer', answer: 'ok', durationMs: 1,
    usage: { model: 'm', input_tokens: 10, cache_read_input_tokens: 980, cache_creation_input_tokens: 10, output_tokens: 5 } } as any)
  const ui = await $.ui.mount(PANE())
  expect(await ui.find({ type: 'Text', text: /^cache/ })).toBeUndefined()
})

// ---- where the pane really is (0.21.4) -------------------------------------------------------------
// ui.open answers isPlaced:true at session start, before the terminal is measured, and the side column docks only
// in the fullscreen renderer. So the answer is provisional; the band decides again once a draw carries a viewport.
const HINT = /pane docks only in fullscreen \u2014 run \/tui fullscreen/

function BANDV(columns: number, isFullscreen?: boolean) {
  const viewport: any = { columns, rows: 40 }
  if (isFullscreen !== undefined) viewport.isFullscreen = isFullscreen
  return { ...BAND, viewport } as const
}

test('placement: isPlaced from session.start is provisional, the engine\'s record decides once the window is measured', async ($, on) => {
  world(on, { [LIST]: listText() }, { placed: true, panes: ['gt'], recordPlaced: false })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const band = await $.ui.mount(BANDV(100, true))
  expect(await band.find({ key: 'gt-0' })).toBeDefined()
  expect(await band.find({ key: 'gt-11' })).toBeDefined()
  expect(await band.find({ type: 'Text', text: HINT })).toBeUndefined()
})

test('placement: a pane that waited undrawn at start is picked up when the engine says it is placed', async ($, on) => {
  world(on, { [LIST]: listText() }, { placed: false, panes: ['gt'], recordPlaced: true })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const band = await $.ui.mount(BANDV(160, true))
  expect(await band.find({ key: 'gt-0' })).toBeUndefined()
  expect(await band.find({ type: 'Text', text: /^gt/ })).toBeDefined()
})

test('placement: widening the window re-checks, so the row goes when the pane docks', async ($, on) => {
  const opts = { placed: true, panes: ['gt'], recordPlaced: false }
  world(on, { [LIST]: listText() }, opts)
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const narrow = await $.ui.mount(BANDV(100, true))
  expect(await narrow.find({ key: 'gt-0' })).toBeDefined()
  await narrow.unmount()
  opts.recordPlaced = true
  const wide = await $.ui.mount(BANDV(160, true))
  expect(await wide.find({ key: 'gt-0' })).toBeUndefined()
})

test('placement: the engine\'s record is read once per window size, not on every draw', async ($, on) => {
  const seen = world(on, { [LIST]: listText() }, { panes: ['gt'], recordPlaced: true })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  for (let i = 0; i < 3; i++) {
    const band = await $.ui.mount(BANDV(160, true))
    await band.unmount()
  }
  expect(seen.paneReads).toBe(1)
})

test('placement: a draw of the pane itself proves it is placed, whatever the record said a moment ago', async ($, on) => {
  world(on, { [LIST]: listText() }, { placed: true, panes: ['gt'], recordPlaced: false })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const before = await $.ui.mount(BANDV(100, true))
  expect(await before.find({ key: 'gt-0' })).toBeDefined()
  await before.unmount()
  const pane = await $.ui.mount(PANE())
  expect(await pane.find({ key: 'gt-0' })).toBeDefined()
  const after = await $.ui.mount(BANDV(100, true))
  expect(await after.find({ key: 'gt-0' })).toBeUndefined()
})

test('placement: before the window is measured nothing is claimed, the open answer stands', async ($, on) => {
  const seen = world(on, { [LIST]: listText() }, { placed: true, panes: ['gt'], recordPlaced: false })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const { viewport, ...unmeasured } = BAND as any
  const band = await $.ui.mount(unmeasured)
  expect(await band.find({ key: 'gt-0' })).toBeUndefined()
  expect(await band.find({ type: 'Text', text: HINT })).toBeUndefined()
  expect(seen.paneReads).toBe(0)
})

test('classic renderer: the band says the pane docks only in fullscreen, and carries the buttons itself', async ($, on) => {
  // ui.open said isPlaced:true and so does the engine's record: neither means a dock was drawn
  world(on, { [LIST]: listText() }, { placed: true, panes: ['gt'], recordPlaced: true })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const band = await $.ui.mount(BANDV(160, false))
  const hint = await band.find({ type: 'Text', text: HINT })
  expect(hint).toBeDefined()
  expect(hint.props.dimColor).toBe(true)
  expect(hint.children.join('')).toBe('gt \u00b7 pane docks only in fullscreen \u2014 run /tui fullscreen')
  expect(await band.find({ key: 'gt-0' })).toBeDefined()
  expect(await band.find({ key: 'gt-11' })).toBeDefined()
})

test('classic renderer: the hint is a line of its own, so the first band line is the one it always was', async ($, on) => {
  world(on, { [LIST]: listText() }, { panes: ['gt'], recordPlaced: true })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const band = await $.ui.mount(BANDV(100, false))
  const first = await band.find({ type: 'Text', text: /^gt \u00b7 press a button/ })
  expect(first).toBeDefined()
  expect(first.children.join('')).not.toMatch(/fullscreen/)
})

test('fullscreen renderer: docked and placed, no hint and no row', async ($, on) => {
  world(on, { [LIST]: listText() }, { panes: ['gt'], recordPlaced: true })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const band = await $.ui.mount(BANDV(160, true))
  expect(await band.find({ type: 'Text', text: HINT })).toBeUndefined()
  expect(await band.find({ key: 'gt-0' })).toBeUndefined()
})

test('renderer not reported: no hint is given, only the placement decides', async ($, on) => {
  world(on, { [LIST]: listText() }, { panes: ['gt'], recordPlaced: true })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const band = await $.ui.mount(BANDV(160))
  expect(await band.find({ type: 'Text', text: HINT })).toBeUndefined()
  expect(await band.find({ key: 'gt-0' })).toBeUndefined()
})

test('classic renderer, pane_at_start closed: no pane is open, so no hint', async ($, on) => {
  world(on, { [LIST]: listText(), [CONFIG]: JSON.stringify({ pane_at_start: 'closed' }) })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const band = await $.ui.mount(BANDV(160, false))
  expect(await band.find({ type: 'Text', text: HINT })).toBeUndefined()
  expect(await band.find({ key: 'gt-0' })).toBeUndefined()
})

test('classic renderer, pane_at_start off: the band is left to Claude Code, no hint', async ($, on) => {
  world(on, { [LIST]: listText(), [CONFIG]: JSON.stringify({ pane_at_start: 'off' }) })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const band = await $.ui.mount(BANDV(160, false))
  expect(await band.find({ type: 'Text', text: HINT })).toBeUndefined()
})

test('classic renderer, /gt-pane-buttons opens the pane by hand: the hint shows then, and goes when it is closed', async ($, on) => {
  const opts = { panes: [] as string[], recordPlaced: true }
  world(on, { [LIST]: listText(), [CONFIG]: JSON.stringify({ pane_at_start: 'closed' }) }, opts)
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  await toggle($)
  opts.panes = ['gt']
  const open = await $.ui.mount(BANDV(160, false))
  expect(await open.find({ type: 'Text', text: HINT })).toBeDefined()
  await open.unmount()
  await toggle($)
  const closed = await $.ui.mount(BANDV(160, false))
  expect(await closed.find({ type: 'Text', text: HINT })).toBeUndefined()
  expect(await closed.find({ key: 'gt-0' })).toBeUndefined()
})

test('classic renderer: a damaged list is named in the band, because the pane is not on screen to say it', async ($, on) => {
  world(on, { [LIST]: '{ broken' }, { panes: ['gt'], recordPlaced: true })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const band = await $.ui.mount(BANDV(160, false))
  expect(await band.find({ type: 'Text', text: /^gt \u00b7 buttons\.json:/ })).toBeDefined()
  expect(await band.find({ type: 'Text', text: HINT })).toBeDefined()
  expect(await band.find({ key: 'gt-0' })).toBeUndefined()
})

test('docked: a damaged list is left to the pane\'s red line, the band does not repeat it', async ($, on) => {
  world(on, { [LIST]: '{ broken' }, { panes: ['gt'], recordPlaced: true })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const band = await $.ui.mount(BANDV(160, true))
  expect(await band.find({ type: 'Text', text: /buttons\.json:/ })).toBeUndefined()
})

test('the desktop app is never told to run /tui fullscreen', async ($, on) => {
  world(on, { [LIST]: listText() }, { panes: ['gt'], recordPlaced: true })
  await $.session.start({ surface: 'desktop', isInteractive: true, cwd: '/work' })
  const band = await $.ui.mount({ ...BANDV(120, false), surface: 'desktop' } as any)
  expect(await band.find({ type: 'Text', text: HINT })).toBeUndefined()
})
