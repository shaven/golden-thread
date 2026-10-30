// gt expiry guard, the in-session half (0.21.2). Claude Code runs this inside the open session (gt
// supports Claude Code 2.1.292 and later); the guard's background copy is the fallback when it cannot act.
//
// The guard's Python half cannot compact an open session: the running Claude Code holds the
// conversation in its own memory. This mod can ($.session.compact), so for an open session the
// cut happens in place, while the prompt cache is still warm, and the user comes back to a small
// session with nothing to press.
//
// A mod is never told its session id, so the two halves pair up through files in
// ~/.claude/golden-thread/expiry-guard/mod/:
//   turn-<turnId>.json  this mod, at the end of each main-loop turn: the folder and the time
//   plan-<turnId>.json  the guard's idle hook, when it pairs exactly one turn with its session:
//                       what to do (compact | ask), when, and where to save the handoff
//   done-<turnId>.json  this mod: what it did, so the background job stands down
// No pairing, an ambiguous one, or a failure here leaves the background job to act as before.
//
// LAST RESORT, NO HOOK AT ALL. Both hooks that can write a plan (Stop and the idle notification)
// may fail to run: Claude Code's idle notifier is one 60 s timer that gives up for the whole idle
// period when the terminal was touched. If no plan has arrived by the last useful poll, this mod
// plans for itself: it reads expiry_guard.* from ~/.claude/vault-config.json (mode must be auto,
// open_sessions compact or ask, min_tokens respected, defaults as gt_settings registers them),
// asks Claude Code for the context size ($.session.usage; no answer, no plan), takes the cache
// lifetime from CLAUDE_CODE_PROMPT_CACHE_TTL (else 1 hour) and acts at turn end + lifetime - margin
// - EARLY_S, the margin rule the Python half uses. It never acts when a plan exists (the plan wins)
// or when a done record for the turn exists, and the next prompt cancels it like any plan. Its done
// record carries "source": "self".
//
// AUTO-COMPACT AND THE COMMAND (0.21.4). The same capture-then-cut path also runs on a schedule the
// owner sets, not only on idleness: auto_compact (warn | off | auto | 30..90 percent of the window | 100000..400000 tokens; 0.22.0, default warn) in
// vault-config.json. At the end of a main-loop turn, when the context has passed that percent, the
// session is known to have no background work running, no done record exists for the turn and at
// least AUTO_MIN_TURNS turns have passed since the last cut, it sends the capture turn (strict: if
// that turn cannot be sent, or ends in an error, nothing is cut) and then compacts in place. This is
// the ONE owner of "compact now" at a turn boundary: the idle plan above only ever acts after the
// idle period, and the turn marker + done record written here make the Python half stand down.
//
// WARN, THE DEFAULT (0.22.0, owner 2026-10-26: cutting is opt-in). `warn` never cuts and never touches
// the prompt box: at the end of a main-loop turn where the context has passed the line `auto` would cut
// at (AUTO_TOKENS, held to 75% of a known window) it says so once, as one $.ui.log line naming
// /gt:gt-minimize compact and /compact. Said again only after the context has fallen back under the
// line (a /compact) and climbed past it again. It drops warned-<session>.json here so gt_state.py's
// context nudge, on the very next prompt, does not say the same thing a second time.
//
// BUSY OR NOT. A cut while a background agent or shell runs would lose what it reports back, so the
// automatic cut needs a positive "nothing running". The source is bg-<session>.json, written by
// gt_expiry_guard.py's Stop hook from Claude Code's own `background_tasks` list at the end of every
// reply. It counts only when it is this session's (its id), written during this turn, and says
// known: true. Anything else -- no marker yet (looked for again for a few seconds: the Stop hook and
// this hook race), another session's, an old one, one that cannot say, or a Claude Code that does not
// tell the mod its session id -- is "cannot tell", and cannot tell never cuts: it says so once per
// session and leaves the idle plan to work as usual.
//
// The command /gt:gt-minimize compact has the model capture and write the handoff itself, then drops
// request-<session>.json (gt_compact.py request), in this folder or, when sandbox mode keeps it out of
// ~/.claude/golden-thread, in ~/.gt-scratch/compact. Keyed by SESSION, not folder: several sessions
// often share a folder, and only the session that asked may take it, so a mod that cannot learn its
// session id takes no request. The mod cuts at that turn's end, with no second capture, and puts
// "Read the handoff at <the vault handoff>" in the prompt box.
//
// WHAT A CUT CARRIES AND WHAT KIND IT IS (0.22.0, owner 2026-10-26: two separate choices). Both the
// automatic cut and a request read them; the expiry guard's idle plan does not (its own capture_first).
//   cut_handoff  none     a plain cut: no capture turn, no handoff file, no pickup line
//                handoff  the capture turn writes a handoff only; nothing is saved to the vault
//                facts    (default) the capture turn saves new findings through the write queue and writes
//                         a handoff with its key facts -- the cut as it was before 0.22.0
//   cut_kind     compact  (default) compact in place, as above
//                clear    the capture reply is saved as the handoff, the session is cleared with
//                         $.command.run({command: 'clear'}) (the documented way to run a slash command as the
//                         person would), and the pickup line goes in the fresh session's prompt box. The types
//                         say a /clear ends the conversation, raises session.end {reason: 'clear'}, and goes on
//                         in the same process under a new session id with no session.start, so this module and
//                         its variables live on. Not seen in a live Claude Code. If the command API is missing
//                         or refuses, the notice says to type /clear, and the pickup is put in the box when that
//                         clear's session.end arrives.
// clear with none would discard the session: never cut (said once a session); gt_settings refuses it too.
//
// What this file may reach (tests/test_expiry_guard_inplace.py pins the list): HOME and the cache
// lifetime variable, files under ~/.claude/golden-thread/expiry-guard and ~/.gt-scratch/compact,
// ~/.claude/vault-config.json (read only), the session's id, context size and compaction, the /clear
// command, the prompt box (read, so a fill keeps the draft already typed there), a log line.
// No shell, no network, no process, no model call of its own.

const POLL_MS = [75000, 150000, 300000]   // when to look for the plan after a turn ends
const STALE_S = 900                        // a plan or claim older than this is ignored
const EARLY_S = 120                        // a self-made plan acts this long before the Python job would
const MIN_AFTER_IDLE_S = 150               // the margin never reaches closer than this to the turn's end
const INSTRUCTIONS = 'Summarise in at most 6,000 tokens. Keep: the task in progress and its next step, decisions made and why, file paths and names touched, commands and settings that worked, open items and questions, and where the handoff is saved. Drop: tool output already acted on, repeated file dumps, dead ends.'
// cut_handoff none: no handoff is kept, so the summary is not asked to say where one is saved
const INSTRUCTIONS_PLAIN = 'Summarise in at most 6,000 tokens. Keep: the task in progress and its next step, decisions made and why, file paths and names touched, commands and settings that worked, open items and questions. Drop: tool output already acted on, repeated file dumps, dead ends.'
const SELF_DEFAULTS = { mode: 'warn-only', open: 'compact', minTokens: 150000, margin: 'auto', capture: 'on', auto: null }
const CAPTURE_WAIT_S = 600                 // a capture turn that has not ended by then is given up on
// Capture first (owner, 2026-10-13: "We only want it to do a handoff"): before the cut, one warm turn
// saves what is not yet in the vault and writes the handoff, so the compaction keeps a handoff, not
// whatever the summary happens to keep. It runs while the cache is warm, so it is mostly cache reads.
const AUTO_MIN_TURNS = 6                    // at most one automatic cut per this many main-loop turns
const AUTO_DEFAULT_WINDOW = 200000         // when Claude Code reports tokens but no window size
const BUSY_FRESH_S = 60                     // a busy marker older than this is not this turn's
const BUSY_RETRY_MS = [1000, 1500, 2500]    // look again for the marker after these waits (about 5 s in all)
const DECISION_RETRY_MS = [700, 700, 700]   // look again for this turn's auto-clear decision (about 2 s in all)
const HANDOFF_ITEMS = 'the task in progress and its next step, decisions made and why, files touched, commands that worked, open questions'
const PROJECT_POINTER = "the open project's slug with the paths of its memory/MEMORY.md and source.md, to be read only " +
  'when the work needs them (do not read them now)'
// cut_handoff facts (the default) or handoff, for a compact or a clear.
function saveSteps(mode, kind) {
  const end = ' Start no other work: the session is ' + (kind === 'clear' ? 'cleared' : 'compacted') + ' when this reply ends.'
  if (mode === 'handoff') {
    return 'save nothing to the vault this time (cut_handoff is handoff). Reply with a handoff of at most 40 lines: ' +
      HANDOFF_ITEMS + ', and ' + PROJECT_POINTER + '.' + end
  }
  return "(1) if this session produced findings, decisions or facts that are not yet in the vault, " +
    "queue them to the open project's research.md (decisions to decisions.md) through gt_write_queue.py and apply them " +
    'with gt_broker.py drain, naming --vault, as the core rules say; skip this if nothing is new. (2) Then reply with a ' +
    'handoff of at most 40 lines: ' + HANDOFF_ITEMS + ', the key facts the next session needs (figures, names, settings, ' +
    'findings), what step 1 saved, and ' + PROJECT_POINTER + '.' + end
}
const SAVE_STEPS = saveSteps('facts', 'compact')
const CUT_HANDOFFS = ['none', 'handoff', 'facts']
// What a cut with these settings is, in the words the notices use (gt_settings.cut_words says the same).
function cutWords(kind, mode) {
  if (kind === 'clear' && mode === 'none') return 'refused (cut_kind clear with cut_handoff none would carry nothing over)'
  if (mode === 'none') return 'a plain compact'
  return (kind === 'clear' ? 'a clear' : 'a compact') + (mode === 'handoff' ? ' with a handoff' : ' with a handoff and facts')
}
const AFTER_CAPTURE = ' The last reply is the handoff written for this compaction: keep it in full, with its project pointer.' +
  ' After the cut, read the handoff file first (its path is in the prompt box) before doing anything else.'
// auto_compact (0.22.0): warn (the default; a notice, never a cut), off, a percent 30-90, a token count
// 100000-400000, or auto. gt_compact.py carries
// the same rules; tests/test_auto_compact_tokens.py runs one table of cases through both.
const AUTO_TOKENS = 150000
const AUTO_HELD = 0.75
// Only windows that were measured; Opus 4.8 and Haiku 4.5 are unknown on purpose.
const WINDOWS = [['sonnet-5-5', 1000000], ['opus-5-5', 1000000], ['sonnet-4-6', 200000]]
function windowFor(model) {
  const m = String(model == null ? '' : model).trim().toLowerCase().replace(/[ ._]+/g, '-')
  for (const [frag, win] of WINDOWS) if (m.includes(frag)) return win
  return 0
}
function parseAuto(raw) {
  const v = String(raw == null ? 'null' : raw).trim().toLowerCase()
  if (v === 'off' || v === 'warn') return null              // warn says, never cuts (see WARN above)
  if (v === 'auto') return { kind: 'tokens', tokens: AUTO_TOKENS }
  const m = /^([0-9]+)(k?)$/.exec(v)
  if (!m) return null
  const n = Number(m[1]) * (m[2] ? 1000 : 1)
  if (!m[2] && n >= 30 && n <= 90) return { kind: 'pct', pct: n }
  return n >= 100000 && n <= 400000 ? { kind: 'tokens', tokens: n } : null
}
function capturePrompt(plan) {
  if (plan && plan.auto) {
    const passed = plan.threshold ? 'about ' + Math.round(Number(plan.context) / 1000) + 'K tokens of context'
      : Math.round(Number(plan.pct) || 0) + '% of its context window'
    const clear = plan.kind === 'clear'
    return 'gt auto-' + (clear ? 'clear' : 'compact') + ': this session passed ' + passed + ' (the auto_compact ' +
      'setting) and is about to be ' + (clear ? 'cleared; the fresh session starts from your handoff' : 'compacted') +
      '. Before that: ' + saveSteps(plan.handoffMode, plan.kind)
  }
  if (plan && plan.kind === 'clear') {
    return 'gt expiry guard: this session has been idle and is about to be cleared while its prompt cache is still ' +
      'warm; the fresh session starts from your handoff. Before that: ' + saveSteps('facts', 'clear')
  }
  return 'gt expiry guard: this session has been idle and is about to be compacted while its prompt cache ' +
    'is still warm. Before that: ' + SAVE_STEPS
}
function srcOf(plan) {
  const cut = plan.kind ? { kind: plan.kind, handoff_mode: plan.handoffMode || 'facts' } : {}
  return Object.assign(plan.auto ? { source: 'auto', auto: true, context: plan.context, threshold: plan.threshold, window: plan.window }
    : plan.manual ? { source: 'plan', manual: true } : plan.self ? { source: 'self', self: true } : { source: 'plan' }, cut)
}

let cwd = ''
let interactive = false
let timers = []
let pending = ''                           // the turn this mod is waiting on, if any
let turnAt = 0                             // when that turn ended, in seconds
let turnsSince = AUTO_MIN_TURNS            // main-loop turns since the last automatic or requested cut
let lastRequestAt = 0                      // the newest compact request already acted on
let lastAutoClearAt = 0                    // the newest auto-clear decision (auto_clear on) already acted on
let capture = null                         // {home, turn, plan, at} while the capture turn runs
let turnStartedAt = 0                      // when the current main-loop turn started, in seconds
let toldCannotTell = false                 // the "cannot tell" notice is said once per session
let warned = false                         // warn: said for this climb past the line (re-armed below it)
let toldRefused = false                    // clear with cut_handoff none: said once per session
let pendingPickup = null                   // {line, home, turn, plan} while a clear is under way or asked for

function dirOf(home) {
  return home + '/.claude/golden-thread/expiry-guard/mod'
}
// gt_compact.py's fallback when sandbox mode denies writes under ~/.claude/golden-thread.
function scratchOf(home) {
  return home + '/.gt-scratch/compact'
}
function safe(id) {
  return String(id || '').replace(/[^A-Za-z0-9._-]/g, '_').slice(0, 80)
}
function nowS() {
  return Date.now() / 1000
}
function stopAll() {
  for (const t of timers) clearTimeout(t)
  timers = []
  pending = ''
}
async function cacheTtlEnv($) {
  try {
    return String((await $.env.get('CLAUDE_CODE_PROMPT_CACHE_TTL')) || '')
  } catch (err) {
    return ''
  }
}
async function homeDir($) {
  try {
    return (await $.env.get('HOME')) || ''
  } catch (err) {
    return ''
  }
}
async function readJson($, path) {
  try {
    if (!(await $.fs.exists(path))) return null
    const v = JSON.parse(await $.fs.read(path))
    return v && typeof v === 'object' ? v : null
  } catch (err) {
    return null
  }
}
async function writeJson($, path, obj) {
  try {
    await $.fs.write(path, JSON.stringify(obj) + '\n')
    return true
  } catch (err) {
    return false
  }
}
function baseOf(home) {
  return home + '/.claude/golden-thread/expiry-guard'
}
// Claude Code 2.1.296 tells a mod its session id ($.session.id), so the guard pairs a turn with
// its session exactly. 2.1.292 to 2.1.295 do not: the marker then carries no id and the guard falls
// back to folder and time, and this mod takes no compact request and makes no automatic cut.
async function sessionId($) {
  try {
    const id = await $.session.id()          // no id from this Claude Code: caught below
    return typeof id === 'string' ? id : ''
  } catch (err) {
    return ''
  }
}

function captureFailed(e) {
  if (!e) return false
  const st = String(e.status || e.outcome || e.reason || '').toLowerCase()
  return !!(e.error || e.aborted || e.interrupted || ['error', 'failed', 'aborted', 'cancelled', 'canceled'].includes(st))
}

function kTokens(n) {
  return Math.round((Number(n) || 0) / 1000) + 'K'
}

// Owner, 2026-10-22: a fill replaces the draft, so text already typed in the box was lost. Read the box
// first: an empty one gets the line; one with text keeps it, pushed down below the line as its first line.
// Owner, 2026-10-26: a box that cannot be read may hold a draft, so it is not filled at all; the caller's
// notice carries the line to type or paste instead.
async function fillAbove($, line) {
  let draft = ''
  try {
    const box = await $.prompt.read()
    draft = box && typeof box.text === 'string' ? box.text : ''
  } catch (err) {
    return { isFilled: false }             // no box to read: leave it alone
  }
  if (draft.trim() === '') return $.prompt.fill({ text: line })
  if (draft.split('\n')[0] === line) return { isFilled: true }   // already first: nothing to push down
  return $.prompt.fill({ text: line + '\n' + draft })
}

// The plan says compact: do it now, in place, and leave the summary where the guard said.
async function cutNow($, home, turn, plan) {
  const done = dirOf(home) + '/done-' + safe(turn) + '.json'
  const src = srcOf(plan)
  await writeJson($, done, Object.assign({ turn, status: 'compacting', at: nowS() }, src))
  let r
  try {
    r = await $.session.compact({ instructions: String(plan.instructions || '') })
  } catch (err) {
    await writeJson($, done, Object.assign({ turn, status: 'failed', at: nowS(), why: String((err && err.message) || err).slice(0, 200) }, src))
    return
  }
  const first = r && Array.isArray(r.messages) && r.messages[0]
  const summary = first && typeof first.text === 'string' ? first.text : ''
  if (summary && plan.handoff) {
    const head = '# Handoff: ' + (plan.name || 'session') + ', compacted in place\n\n' +
      'Written ' + new Date().toISOString() + ' by the gt expiry guard while the prompt cache was warm.\n' +
      'The open session continues from this summary; it is kept here so a later session can too.\n\n'
    try {
      await $.fs.write(plan.handoff, head + summary + '\n')
    } catch (err) {
      // the session is compacted either way; only the copy on disk is missing
    }
  }
  await writeJson($, done, Object.assign({
    turn, status: 'compacted', at: nowS(),
    before: r && r.tokensBefore, after: r && r.tokensAfter, handoff: summary ? plan.handoff || '' : '',
  }, src))
  // Owner, 2026-10-14: the handoff was written but nothing offered to load it. Put the pickup in the
  // prompt box so Enter reads it; the user can clear it and type something else instead.
  let filled = false
  const pickup = plan.pickup || plan.handoff
  const pickupLine = 'Read the handoff at ' + pickup + ' and pick up where we left off.'
  if (summary && pickup) {
    try {
      const f = await fillAbove($, pickupLine)
      filled = !!(f && f.isFilled)
    } catch (err) {
      filled = false
    }
  }
  try {
    await $.ui.log((plan.auto ? 'Auto-compacted this session at ' + (plan.threshold ? kTokens(plan.context) + ' tokens' : Math.round(Number(plan.pct) || 0) + '% context') + ' (' :
      plan.manual ? 'Compacted this session on request (' : 'Compacted this session while its cache was warm (') + kTokens(r && r.tokensBefore) +
      ' to ' + kTokens(r && r.tokensAfter) + ' tokens).' +
      (summary && pickup ? ' Handoff saved: ' + pickup +
        (filled ? '. Press Enter to load it.' : '. The prompt box was left as it was; type or paste: ' + pickupLine) : ' Carry on as usual.'))
  } catch (err) {
    // the notice is a courtesy
  }
}

// cut_kind clear: the session is cleared and the fresh one starts from the handoff. `reply` is the capture
// turn's answer (the automatic cut), saved here as the handoff; a request already names its handoff
// (plan.pickup), written by the model. No handoff text, no clear: a clear without one discards the session.
async function clearNow($, home, turn, plan, reply) {
  const done = dirOf(home) + '/done-' + safe(turn) + '.json'
  const src = srcOf(plan)
  let pickup = plan.pickup || ''
  if (!pickup) {
    const text = String(reply || '').trim()
    let saved = false
    if (text && plan.handoff) {
      const head = '# Handoff: ' + (plan.name || 'session') + ', before a clear\n\n' +
        'Written ' + new Date().toISOString() + ' by gt (auto_compact, cut_kind clear) from the session\'s last reply.\n\n'
      try {
        await $.fs.write(plan.handoff, head + text + '\n')
        saved = true
      } catch (err) {
        saved = false
      }
    }
    if (!saved) {
      await writeJson($, done, Object.assign({ turn, status: 'capture-failed', at: nowS(), why: 'no handoff saved' }, src))
      try {
        await $.ui.log('Auto-clear skipped: no handoff could be saved, so nothing was cleared.')
      } catch (err) {
        // the notice is a courtesy
      }
      return
    }
    pickup = plan.handoff
  }
  const line = 'Read the handoff at ' + pickup + ' and pick up where we left off.'
  await writeJson($, done, Object.assign({ turn, status: 'clearing', at: nowS(), handoff: pickup }, src))
  // The guard named a pickup file (open_sessions clear): arm it now, so the SessionStart the clear causes loads the
  // handoff into the fresh session itself and the prompt box is left alone.
  let loaded = false
  if (plan.pickup_file) {
    loaded = await writeJson($, plan.pickup_file, { handoff: pickup, session_id: plan.sid || '', at: nowS() })
  }
  pendingPickup = { line, home, turn, plan, pickup, loaded }
  let ran = false
  try {
    await $.command.run({ command: 'clear' })
    ran = true
  } catch (err) {
    ran = false
  }
  if (ran) {
    setTimeout(() => { deliverPickup($).catch(() => {}) }, 1000)   // session.end may have delivered it already
    return
  }
  await writeJson($, done, Object.assign({ turn, status: 'clear-asked', at: nowS(), handoff: pickup }, src))
  try {
    await $.ui.log('Handoff saved: ' + pickup + '. gt could not clear this session by itself. Type /clear; gt then ' +
      'puts the pickup in the prompt box (or type or paste: ' + line + ')')
  } catch (err) {
    // the notice is a courtesy
  }
}

// After a clear (the mod's own, or one the person typed when asked): the pickup goes in the box, above any
// draft (fillAbove), once.
async function deliverPickup($) {
  if (!pendingPickup) return
  const p = pendingPickup
  pendingPickup = null
  let filled = false
  if (!p.loaded) {
    try {
      const f = await fillAbove($, p.line)
      filled = !!(f && f.isFilled)
    } catch (err) {
      filled = false
    }
  }
  await writeJson($, dirOf(p.home) + '/done-' + safe(p.turn) + '.json',
    Object.assign({ turn: p.turn, status: 'cleared', at: nowS(), handoff: p.pickup, filled, loaded: !!p.loaded }, srcOf(p.plan)))
  if (p.loaded) {
    try {
      await $.ui.log('Cleared this idle session; the fresh session was given the handoff: ' + p.pickup)
    } catch (err) {
      // the notice is a courtesy
    }
    return
  }
  try {
    await $.ui.log((p.plan.auto ? 'Auto-cleared this session at ' + (p.plan.threshold ? kTokens(p.plan.context) + ' tokens'
      : Math.round(Number(p.plan.pct) || 0) + '% context') : 'Cleared this session on request') +
      '; the fresh session starts from the handoff: ' + p.pickup +
      (filled ? '. Press Enter to load it.' : '. The prompt box was left as it was; type or paste: ' + p.line))
  } catch (err) {
    // the notice is a courtesy
  }
}

// The plan says compact: capture first (one warm turn that saves and writes the handoff), then cut
// when that turn ends. capture_first off, or a prompt that cannot be sent, cuts at once as before.
// cut_handoff none (the automatic cut and a request): no capture, a plain cut.
async function compactNow($, home, turn, plan) {
  if (plan.handoffMode === 'none') return cutNow($, home, turn, plan)
  const cfg = await selfSettings($, home)
  if (!plan.strict && (cfg.capture === 'off' || plan.capture === false)) return cutNow($, home, turn, plan)
  const src = srcOf(plan)
  capture = { home, turn, plan: Object.assign({}, plan, { instructions: String(plan.instructions || INSTRUCTIONS) + (plan.kind === 'clear' ? '' : AFTER_CAPTURE) }), at: nowS() }
  await writeJson($, dirOf(home) + '/done-' + safe(turn) + '.json', Object.assign({ turn, status: 'capturing', at: nowS() }, src))
  try {
    await $.prompt.submit({ text: capturePrompt(plan) })
  } catch (err) {
    capture = null
    if (plan.strict) return abortCapture($, home, turn, plan)   // capture before cut: no save, no cut
    return cutNow($, home, turn, plan)
  }
  timers.push(setTimeout(() => { giveUpCapture($, turn).catch(() => {}) }, CAPTURE_WAIT_S * 1000))
}

// The capture turn ended `endedAt`; the cut runs a second later, outside the hook (see turn.complete). Never inside the
// person's next turn (one that started since: stopAll cancels this timer, and this check stands behind it), and for an
// automatic cut never while a background task runs: busy is read again here, since a task may have started during the
// capture turn. Cannot tell, after the retries, cuts nothing (BUSY OR NOT).
async function afterCapture($, c, reply, endedAt, idx) {
  if (turnStartedAt > endedAt) return skipCut($, c, 'turn-started', 'you started a new turn')
  if (c.plan.strict && c.plan.sid) {
    const st = await busyState($, c.home, c.plan.sid)
    if (st === 'busy') return skipCut($, c, 'busy', 'a background task is running')
    if (st === 'unknown') {
      if (idx < BUSY_RETRY_MS.length - 1) {
        timers.push(setTimeout(() => { afterCapture($, c, reply, endedAt, idx + 1).catch(() => {}) }, BUSY_RETRY_MS[idx + 1]))
        return
      }
      return skipCut($, c, 'cannot-tell', 'gt cannot tell whether background work is running')
    }
  }
  return c.plan.kind === 'clear' ? clearNow($, c.home, c.turn, c.plan, reply) : cutNow($, c.home, c.turn, c.plan)
}

async function skipCut($, c, status, why) {
  await writeJson($, dirOf(c.home) + '/done-' + safe(c.turn) + '.json', Object.assign({ turn: c.turn, status: 'skipped', why: status, at: nowS() }, srcOf(c.plan)))
  try {
    await $.ui.log((c.plan.kind === 'clear' ? 'Auto-clear' : 'Auto-compact') + ' skipped after the save step: ' + why +
      ', so nothing was ' + (c.plan.kind === 'clear' ? 'cleared' : 'cut') + '.')
  } catch (err) {
    // the notice is a courtesy
  }
}

async function abortCapture($, home, turn, plan) {
  await writeJson($, dirOf(home) + '/done-' + safe(turn) + '.json', Object.assign({ turn, status: 'capture-failed', at: nowS() }, srcOf(plan)))
  try {
    await $.ui.log(plan.kind === 'clear' ? 'Auto-clear skipped: the save step could not run, so nothing was cleared.'
      : 'Auto-compact skipped: the save step could not run, so nothing was cut.')
  } catch (err) {
    // the notice is a courtesy
  }
}

async function giveUpCapture($, turn) {
  if (!capture || capture.turn !== turn) return
  const c = capture
  capture = null
  await writeJson($, dirOf(c.home) + '/done-' + safe(turn) + '.json', { turn, status: 'capture-timeout', at: nowS() })
}

// The plan says ask: say so, and put /compact in the prompt box so Enter does it -- only when the box is
// EMPTY (owner, 2026-10-26: nothing gt does may overwrite typed text). A slash command runs only as the
// first thing in the box, and /compact takes what follows it as its summary instructions, so neither
// "/compact" over a draft nor "/compact" above one is safe: with text in the box, or a box that cannot be
// read, the box is left as it is and the notice alone is said.
async function askNow($, home, turn, plan) {
  let filled = false
  let draft = null                         // null: the box could not be read
  try {
    const box = await $.prompt.read()
    draft = box && typeof box.text === 'string' ? box.text : ''
  } catch (err) {
    draft = null
  }
  if (draft !== null && draft.trim() === '') {
    try {
      const r = await $.prompt.fill({ text: '/compact' })
      filled = !!(r && r.isFilled)
    } catch (err) {
      filled = false                       // no prompt box to fill: the notice alone
    }
  }
  const kept = draft !== null && draft.trim() !== ''
  try {
    await $.ui.log('This session (' + kTokens(plan.context) + ' tokens) will lose its prompt cache in about ' +
      Math.max(1, Math.round((Number(plan.expires_at) - nowS()) / 60)) + ' min. ' +
      (filled ? 'Press Enter to compact it now, while that is cheap.'
        : kept ? 'Your typed text was left in the box; send or clear it, then type /compact while that is cheap.'
          : 'Type /compact now, while that is cheap.'))
  } catch (err) {
    // nothing else to do
  }
  await writeJson($, dirOf(home) + '/done-' + safe(turn) + '.json',
    Object.assign({ turn, status: 'asked', at: nowS(), filled }, srcOf(plan)))
}

async function act($, turn, own) {
  if (pending !== turn) return
  const home = await homeDir($)
  if (!home) return
  if (await readJson($, dirOf(home) + '/done-' + safe(turn) + '.json')) return   // already acted: never twice
  let plan = await readJson($, dirOf(home) + '/plan-' + safe(turn) + '.json')
  if (plan && plan.turn !== turn) plan = null
  if (!plan && own) plan = own                                // no plan from the guard: the one made here
  if (!plan) return                                           // the guard withdrew it: a prompt came
  if (nowS() >= Number(plan.expires_at || 0)) return          // too late: a cut now would be cold
  pending = ''
  if (plan.action === 'compact') await compactNow($, home, turn, plan)
  else if (plan.action === 'ask') await askNow($, home, turn, plan)
}

// The guard's margin rule (gt_expiry_guard.margin_seconds) for a cache lifetime in seconds.
function marginS(margin, ttl) {
  const mins = margin === 'auto' ? (ttl === 300 ? 5 : 10) : Number(margin)
  return Math.max(0, Math.min((Number.isFinite(mins) ? mins : 10) * 60, ttl - MIN_AFTER_IDLE_S))
}

// expiry_guard.* from vault-config.json, anything unreadable or invalid falling back to the default.
async function selfSettings($, home) {
  const cfg = (await readJson($, home + '/.claude/vault-config.json')) || {}
  const get = (k) => (typeof cfg[k] === 'string' ? cfg[k].trim().toLowerCase() : '')
  const out = Object.assign({}, SELF_DEFAULTS)
  if (['auto', 'warn-only', 'off'].includes(get('expiry_guard'))) out.mode = get('expiry_guard')
  if (['compact', 'ask', 'clear', 'fork', 'notify', 'off'].includes(get('expiry_guard.open_sessions'))) out.open = get('expiry_guard.open_sessions')
  const mt = get('expiry_guard.min_tokens')
  if (/^[0-9]+$/.test(mt) && Number(mt) >= 1) out.minTokens = Number(mt)
  // a missing key (or no config file) is warn; an explicit value is kept; junk is off, never on
  const ac = Object.prototype.hasOwnProperty.call(cfg, 'auto_compact') ? String(cfg.auto_compact).trim().toLowerCase() : 'warn'
  out.auto = parseAuto(ac)
  out.warn = ac === 'warn'
  // auto_clear (0.22.0, phase 143): only the exact value `on` acts; the Python half has already checked the shadow gate
  out.autoClear = get('auto_clear') === 'on' ? 'on' : 'off'
  // cut_handoff / cut_kind: junk is the safe default (facts; compact -- a typo never clears)
  out.cutHandoff = CUT_HANDOFFS.includes(get('cut_handoff')) ? get('cut_handoff') : 'facts'
  out.cutKind = get('cut_kind') === 'clear' ? 'clear' : 'compact'
  if (['on', 'off'].includes(get('expiry_guard.capture_first'))) out.capture = get('expiry_guard.capture_first')
  const mg = get('expiry_guard.margin_min')
  if (mg === 'auto' || /^[0-9]+$/.test(mg)) out.margin = mg
  return out
}

async function contextTokens($) {
  try {
    const u = await $.session.usage()        // no answer throws: caught below
    const n = Number(u && u.context && u.context.tokens)
    return Number.isFinite(n) && n > 0 ? n : 0
  } catch (err) {
    return 0                               // no answer: no plan
  }
}

// Context fill, from what Claude Code reports: a percent when it gives one, else tokens over a
// window when it gives one, else tokens over AUTO_DEFAULT_WINDOW.
async function contextInfo($) {
  let u = null
  try {
    u = await $.session.usage()
  } catch (err) {
    return { pct: null, tokens: 0, window: 0 }
  }
  const c = (u && u.context) || {}
  const tokens = Number(c.tokens)
  let pct = null
  for (const k of ['percent', 'pct', 'percentage', 'usedPercent']) {
    if (pct === null && Number.isFinite(Number(c[k])) && c[k] !== null && c[k] !== '') pct = Number(c[k])
  }
  let known = 0                              // the window when Claude Code reports it, else the model's, else 0
  for (const k of ['max', 'limit', 'window', 'size', 'maxTokens', 'contextWindow']) {
    if (Number(c[k]) > 0) { known = Number(c[k]); break }
  }
  if (!known) known = windowFor((u && u.model) || c.model)
  if (pct === null && Number.isFinite(tokens) && tokens > 0) pct = (tokens / (known || AUTO_DEFAULT_WINDOW)) * 100
  return { pct, tokens: Number.isFinite(tokens) ? tokens : 0, window: known }
}

// 'idle' | 'busy' | 'unknown', from the Stop hook's bg-<session>.json (see BUSY OR NOT above).
async function busyState($, home, sid) {
  if (!sid) return 'unknown'
  const m = await readJson($, dirOf(home) + '/bg-' + safe(sid) + '.json')
  if (!m || m.session !== sid || m.known !== true) return 'unknown'
  const at = Number(m.at || 0)
  if (!(at > 0) || nowS() - at > BUSY_FRESH_S || (turnStartedAt > 0 && at < turnStartedAt - 1)) return 'unknown'
  const n = Number(m.running)
  if (!Number.isFinite(n)) return 'unknown'
  return n > 0 ? 'busy' : 'idle'
}

async function tellCannotTell($, pct) {
  if (toldCannotTell) return
  toldCannotTell = true
  try {
    await $.ui.log('Auto-compact did not cut at ' + Math.round(Number(pct) || 0) + '% context: gt cannot tell ' +
      'whether background work is running in this session, and it never cuts on a guess. ' +
      '/gt:gt-minimize compact still works; this is said once per session.')
  } catch (err) {
    // the notice is a courtesy
  }
}

// cut_kind clear with cut_handoff none: no cut. Said once a session for the automatic cut; every time for a
// request (the person asked, and is told why nothing happened).
async function refuseClear($, home, turn, plan) {
  if (plan.auto) toldRefused = true          // no done record: the turn's idle plan still works as usual
  else await writeJson($, dirOf(home) + '/done-' + safe(turn) + '.json', Object.assign({ turn, status: 'refused', at: nowS() }, srcOf(plan)))
  try {
    await $.ui.log('Nothing was cut: cut_kind clear with cut_handoff none would clear the session and carry nothing ' +
      'over. Set cut_handoff to handoff or facts, or cut_kind to compact (/gt:gt-settings).' +
      (plan.auto ? ' Said once per session.' : ''))
  } catch (err) {
    // the notice is a courtesy
  }
}

// auto_compact warn: one log line when the context passes the line `auto` would cut at. Never fills,
// submits or compacts. The marker tells gt_state.py's nudge to stay quiet on the next prompt.
async function warnOnce($, home, sid, cfg) {
  const info = await contextInfo($)
  if (!(info.tokens > 0)) return                                    // no reading: say nothing
  const line = info.window ? Math.min(AUTO_TOKENS, Math.floor(info.window * AUTO_HELD)) : AUTO_TOKENS
  if (info.tokens < line) {
    warned = false                                                  // back under it (a /compact): re-arm
    return
  }
  if (warned) return
  warned = true
  try {
    await $.ui.log('This session is about ' + Math.round(info.tokens / 1000) + 'K tokens of context, past the ' +
      Math.round(line / 1000) + 'K line; every reply re-sends all of it. At a break: /gt:gt-minimize compact ' +
      '(' + cutWords(cfg.cutKind, cfg.cutHandoff) + ', from cut_kind and cut_handoff) or /compact. A cut saves most on a 1M-window model; on a ' +
      '200K window Claude Code compacts near 168K anyway. Said once. /gt:gt-settings auto_compact auto has gt do ' +
      'it by itself; off silences this.')
  } catch (err) {
    // the notice is a courtesy
  }
  if (sid) await writeJson($, dirOf(home) + '/warned-' + safe(sid) + '.json', { session: sid, at: nowS(), context: info.tokens, line })
}

// The idle plan's polls, from the turn's end, for a turn the automatic cut did not take after all.
function armIdle($, turn, endedAt) {
  pending = turn
  turnAt = endedAt
  const gone = Math.max(0, (nowS() - endedAt) * 1000)
  timers.push(...POLL_MS.map((ms, i) => setTimeout(() => { lookForPlan($, turn, i).catch(() => {}) }, Math.max(1000, ms - gone))))
}

// The marker was not there at the turn's end: look again a few times, then cut, defer or stand down.
async function recheckBusy($, home, turn, sid, plan, idx, endedAt) {
  const st = await busyState($, home, sid)
  if (st === 'idle') {
    turnsSince = 0
    return compactNow($, home, turn, plan)
  }
  if (st === 'unknown' && sid && idx < BUSY_RETRY_MS.length - 1) {
    timers.push(setTimeout(() => {
      recheckBusy($, home, turn, sid, plan, idx + 1, endedAt).catch(() => {})
    }, BUSY_RETRY_MS[idx + 1]))
    return
  }
  if (st === 'unknown') await tellCannotTell($, plan.pct)
  armIdle($, turn, endedAt)                                   // busy: the next turn boundary tries again
}

async function findRequest($, home, sid) {
  if (!sid) return null                                       // no id: never take another session's request
  for (const path of [dirOf(home) + '/request-' + safe(sid) + '.json', scratchOf(home) + '/request-' + safe(sid) + '.json']) {
    const req = await readJson($, path)
    if (req && req.session === sid) return { req, path }
  }
  return null
}

// At a turn boundary: a requested cut (/gt:gt-minimize compact) or an automatic one (auto_compact).
// Returns true when a cut is scheduled (or still being decided), so the idle plan is not also set up
// for this turn; a decision not to cut sets the idle plan up itself.
// auto_clear on (0.22.0, phase 143). The Stop hook (gt_auto_clear.py) writes autoclear-<session>.json only when the
// shadow regret gate has passed AND the rule says clear. The cut is the one auto_compact makes (capture a handoff and
// facts, then clear, kind clear whatever cut_kind says); a clear that would carry nothing is refused, never made, and
// nothing is cut while a background task runs. Each decision is acted on once.
// The decision is this session's (A3: the file name goes through safe(), which can map two ids to one name) and this
// turn's: written since the turn started (the Stop hook writes it at the turn's end; one from before is the previous
// turn's, about to be replaced) and newer than the last one acted on. The hook and this hook race, so a decision not
// there yet is looked for again (DECISION_RETRY_MS) while the turn is still the idle plan's (pending === turn).
// While a clear the person was asked to type is waiting (pendingPickup), no new cut starts (A6).
async function policyClear($, home, turn, sid, name, cfg, idx = -1) {
  if (!sid || cfg.autoClear !== 'on' || pendingPickup) return false
  let found = null
  for (const base of [dirOf(home), scratchOf(home)]) {
    const path = base + '/autoclear-' + safe(sid) + '.json'
    const d = await readJson($, path)
    if (d) { found = { path, d }; break }
  }
  const d = found && found.d
  const at = d ? Number(d.at) : 0
  const thisTurn = d && d.session === sid && at > lastAutoClearAt && nowS() - at <= STALE_S &&
    !(turnStartedAt > 0 && at < turnStartedAt)
  if (!thisTurn) {
    if (idx < DECISION_RETRY_MS.length - 1) {
      const endedAt = nowS()
      timers.push(setTimeout(() => {
        retryDecision($, home, turn, sid, name, idx + 1, endedAt).catch(() => {})
      }, DECISION_RETRY_MS[idx + 1]))
    }
    return false
  }
  if (d.clear !== true || d.consumed) return false
  if (await readJson($, dirOf(home) + '/done-' + safe(turn) + '.json')) return false
  if (cfg.cutHandoff === 'none') {                                  // a clear that carries nothing: never
    if (!toldRefused) await refuseClear($, home, turn, { auto: true, kind: 'clear', handoffMode: 'none' })
    return false
  }
  const st = await busyState($, home, sid)
  if (st === 'busy') return false                                   // a background task is running: try the next turn
  const info = await contextInfo($)
  const plan = { turn, action: 'compact', auto: true, strict: true, policy: 'auto_clear', pct: info.pct, context: info.tokens || d.context,
    threshold: 0, window: info.window, name, kind: 'clear', handoffMode: cfg.cutHandoff, sid,
    written_at: nowS(), expires_at: nowS() + 3600, instructions: INSTRUCTIONS,
    handoff: baseOf(home) + '/handoff-auto-' + safe(turn) + '.md' }
  lastAutoClearAt = at                                              // acted on once, whatever the file says next
  const again = await readJson($, found.path)                       // A2: the hook may have written a newer one since
  if (again && Number(again.at) === at) await writeJson($, found.path, Object.assign({}, d, { consumed: turn }))
  stopAll()                                                         // a retry: the idle plan's polls give way
  const endedAt = nowS()
  if (st === 'idle') {
    timers.push(setTimeout(() => {
      turnsSince = 0
      compactNow($, home, turn, plan).catch(() => {})
    }, 1000))
    return true
  }
  timers.push(setTimeout(() => { recheckBusy($, home, turn, sid, plan, 0, endedAt).catch(() => {}) }, BUSY_RETRY_MS[0]))
  return true
}

// A later look for this turn's decision: only while nothing else took the turn and the person has not started another.
async function retryDecision($, home, turn, sid, name, idx, endedAt) {
  if (pending !== turn || turnStartedAt > endedAt || capture) return
  const cfg = await selfSettings($, home)
  await policyClear($, home, turn, sid, name, cfg, idx)
}

async function wantedCut($, home, turn, sid) {
  turnsSince += 1
  const found = await findRequest($, home, sid)
  const req = found && found.req
  const name = (cwd.split('/').filter(Boolean).pop()) || 'session'
  const cfg = await selfSettings($, home)
  // A request (v3, 0.22.0) carries its kind and handoff state; an older one has a handoff and follows cut_kind.
  const reqMode = req && CUT_HANDOFFS.includes(req.handoff_mode) ? req.handoff_mode : 'facts'
  const reqHandoff = req && typeof req.handoff === 'string' ? req.handoff : ''
  if (req && !req.consumed && (reqHandoff || reqMode === 'none') &&
      Number(req.at) > lastRequestAt && nowS() - Number(req.at) <= STALE_S) {
    // The model already captured and wrote the handoff this turn (gt_compact.py refuses without it,
    // unless cut_handoff is none).
    const kind = req.kind === 'clear' || req.kind === 'compact' ? req.kind : cfg.cutKind
    const none = reqMode === 'none' || !reqHandoff
    const plan = { turn, action: 'compact', manual: true, name, kind, handoffMode: reqMode, expires_at: nowS() + 3600,
      instructions: none ? INSTRUCTIONS_PLAIN : INSTRUCTIONS,
      handoff: none ? '' : baseOf(home) + '/handoff-manual-' + safe(turn) + '.md', pickup: none ? '' : reqHandoff }
    timers.push(setTimeout(() => {
      (async () => {
        lastRequestAt = Number(req.at)
        turnsSince = 0
        await writeJson($, found.path, Object.assign({}, req, { consumed: turn }))
        if (kind === 'clear' && none) return refuseClear($, home, turn, plan)
        if (kind === 'clear') return clearNow($, home, turn, plan, '')
        await cutNow($, home, turn, plan)
      })().catch(() => {})
    }, 1000))
    return true
  }
  if (await policyClear($, home, turn, sid, name, cfg)) return true
  if (cfg.warn) {
    await warnOnce($, home, sid, cfg)
    return false                                                    // warn never cuts: the idle plan as usual
  }
  if (!cfg.auto || turnsSince < AUTO_MIN_TURNS || pendingPickup) return false   // a clear the person was asked to type waits
  if (await readJson($, dirOf(home) + '/done-' + safe(turn) + '.json')) return false
  const info = await contextInfo($)
  let threshold = 0
  if (cfg.auto.kind === 'pct') {
    if (info.pct === null || info.pct < cfg.auto.pct) return false
  } else {
    threshold = info.window ? Math.min(cfg.auto.tokens, Math.floor(info.window * AUTO_HELD)) : cfg.auto.tokens
    if (!(info.tokens > 0) || info.tokens < threshold) return false
  }
  if (cfg.cutKind === 'clear' && cfg.cutHandoff === 'none') {      // a clear that carries nothing: never
    if (!toldRefused) await refuseClear($, home, turn, { auto: true, kind: 'clear', handoffMode: 'none' })
    return false
  }
  const st = await busyState($, home, sid)
  if (st === 'busy') return false                                   // a background task is running: try the next turn
  const none = cfg.cutHandoff === 'none'
  const plan = { turn, action: 'compact', auto: true, strict: true, pct: info.pct, context: info.tokens, threshold, window: info.window, name,
    kind: cfg.cutKind, handoffMode: cfg.cutHandoff, sid,
    written_at: nowS(), expires_at: nowS() + 3600, instructions: none ? INSTRUCTIONS_PLAIN : INSTRUCTIONS,
    handoff: none ? '' : baseOf(home) + '/handoff-auto-' + safe(turn) + '.md' }
  const endedAt = nowS()
  if (st === 'idle') {
    timers.push(setTimeout(() => {
      turnsSince = 0
      compactNow($, home, turn, plan).catch(() => {})
    }, 1000))
    return true
  }
  if (!sid) {                                                       // nothing to look again for
    await tellCannotTell($, info.pct)
    return false
  }
  timers.push(setTimeout(() => { recheckBusy($, home, turn, sid, plan, 0, endedAt).catch(() => {}) }, BUSY_RETRY_MS[0]))
  return true
}

// No plan arrived. At the last poll that still leaves time, decide whether to cut by ourselves.
async function considerSelfPlan($, home, turn, idx) {
  const cfg = await selfSettings($, home)
  if (cfg.mode !== 'auto' || (cfg.open !== 'compact' && cfg.open !== 'ask' && cfg.open !== 'clear')) return
  const raw = (await cacheTtlEnv($)).trim().toLowerCase()
  const ttl = raw === '5m' || raw === '300' || raw === '5' ? 300 : 3600
  const base = turnAt + ttl - marginS(cfg.margin, ttl)         // when the Python job would act
  const next = POLL_MS[idx + 1]
  if (next !== undefined && turnAt + next / 1000 < base - 20) return   // a later poll still has time
  const fireAt = Math.max(nowS() + 2, base - EARLY_S)
  if (fireAt >= turnAt + ttl - 30) return                      // nothing warm left to protect
  if (await readJson($, dirOf(home) + '/done-' + safe(turn) + '.json')) return
  const tokens = await contextTokens($)
  if (tokens < cfg.minTokens) return
  const own = {
    turn, action: cfg.open === 'clear' ? 'compact' : cfg.open, written_at: nowS(), fire_at: fireAt, expires_at: turnAt + ttl,
    context: tokens, name: (cwd.split('/').filter(Boolean).pop()) || 'session',
    handoff: baseOf(home) + '/handoff-self-' + safe(turn) + '.md', instructions: INSTRUCTIONS, self: true,
  }
  if (cfg.open === 'clear') Object.assign(own, { kind: 'clear', handoffMode: 'facts', strict: true })   // no pickup file: the line goes in the box
  for (const t of timers) clearTimeout(t)
  timers = [setTimeout(() => { act($, turn, own).catch(() => {}) }, Math.max(1000, (fireAt - nowS()) * 1000))]
}

async function lookForPlan($, turn, idx) {
  if (pending !== turn) return
  const home = await homeDir($)
  if (!home) return
  const plan = await readJson($, dirOf(home) + '/plan-' + safe(turn) + '.json')
  if (!plan || plan.turn !== turn || nowS() - Number(plan.written_at || 0) > STALE_S) {
    await considerSelfPlan($, home, turn, idx)
    return
  }
  for (const t of timers) clearTimeout(t)
  timers = [setTimeout(() => { act($, turn).catch(() => {}) },
    Math.max(1000, (Number(plan.fire_at) - nowS()) * 1000))]
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    cwd = (e && e.cwd) || ''
    interactive = !!(e && e.isInteractive)
    return next(e)
  })

  // A /clear (the mod's own, or one the person typed when asked to): the process goes on under a new session
  // id with no session.start, so this module's per-session state starts over here, and a pickup waiting for
  // the clear goes in the fresh session's prompt box.
  on('session.end', async ($, e, next) => {
    const r = await next(e)
    try {
      if (e && e.reason === 'clear') {
        stopAll()
        capture = null
        warned = false
        toldCannotTell = false
        toldRefused = false
        turnsSince = 0
        if (pendingPickup) setTimeout(() => { deliverPickup($).catch(() => {}) }, 1000)
      }
    } catch (err) {
      // a clear never fails over this
    }
    return r
  })

  // Anything the person starts cancels what was waiting: the idle period is over.
  on('turn.start', async ($, e, next) => {
    if (!(e && e.agentId) && !capture) {                // the capture turn is this mod's own
      stopAll()
      turnStartedAt = nowS()
    }
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const r = await next(e)
    try {
      if (!interactive || (e && e.agentId) || !(e && e.turnId)) return r
      if (capture) {
        // The capture turn ended: cut now, outside this hook (a compaction from inside it would
        // wait on the turn the hook is holding).
        const c = capture
        capture = null
        stopAll()
        if (c.plan.strict && captureFailed(e)) {            // capture before cut: a failed save cuts nothing
          await abortCapture($, c.home, c.turn, c.plan)
          return r
        }
        const reply = r && typeof r.text === 'string' ? r.text : ''
        const endedAt = nowS()
        // in `timers`, so the person's next turn.start cancels it (A1)
        timers.push(setTimeout(() => { afterCapture($, c, reply, endedAt, 0).catch(() => {}) }, 1000))
        return r
      }
      stopAll()
      const home = await homeDir($)
      if (!home) return r
      const turn = String(e.turnId)
      const sid = await sessionId($)
      const mark = sid ? { turn, cwd, at: nowS(), session: sid, v: 2 } : { turn, cwd, at: nowS(), v: 1 }
      const ok = await writeJson($, dirOf(home) + '/turn-' + safe(turn) + '.json', mark)
      if (!ok) return r
      if (await wantedCut($, home, turn, sid)) return r
      pending = turn
      turnAt = nowS()
      timers.push(...POLL_MS.map((ms, i) => setTimeout(() => { lookForPlan($, turn, i).catch(() => {}) }, ms)))
    } catch (err) {
      // the guard's background job is the fallback; a turn never fails over this
    }
    return r
  })
}
