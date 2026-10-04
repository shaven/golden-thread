export const meta = {
  name: 'pipeline-stage',
  description: 'Golden Thread: run one ingest or promote stage over its units, each agent output checked against the stage schema',
  whenToUse: 'Only when a gt skill (gt-ingest, gt-promote, gt-work) hands you the JSON that gt_ingest_pipeline.py workflow-args printed, as args',
  phases: [
    { title: 'Stage', detail: 'one Claude subagent per unit; its output is validated against the stage schema' },
  ],
}

// gt 0.20.1. The deterministic half of every stage stays in gt_ingest_pipeline.py: it renders
// each unit's prompt (an extract prompt only after the intake scan of that unit came back
// clean), writes it into the run's spool, and prints these args. This script only fans the
// prompts out to Claude subagents and hands their schema-checked JSON back; the session then
// records each result as its unit's packet with `gt_ingest_pipeline.py packets`, which checks it
// against the spec a second time. Nothing here reads, writes or runs anything itself, and no
// agent is anything but a Claude subagent of this session.
//
// The prompt goes to the agent as a FILE PATH, not as text: the path is gt's own (the run's
// spool, a sanitised unit name), so nothing from the material reaches the agent's task message.

const STAGES = ['extract', 'classify', 'reconcile', 'draft', 'verify', 'generalize', 'place']
const SPOOL_TAIL = '/Projects/golden-thread/spool/pipeline/'

// gt_ingest_pipeline.unit_slug, exactly: the unit's file name in the run's prompts directory.
function unitSlug(unit) {
  if (unit === '.' || unit === '') return '_root'
  const s = unit.replace(/^\/+|\/+$/g, '').replace(/[^A-Za-z0-9._-]+/g, '__')
  return s.slice(0, 120) || '_unit'
}

function bad(why) {
  throw new Error('pipeline-stage: ' + why + ' -- run it with the JSON `gt_ingest_pipeline.py workflow-args <run> --stage <stage> --json` printed')
}

if (!args || typeof args !== 'object') bad('no args')
if (!STAGES.includes(args.stage)) bad('unknown stage ' + JSON.stringify(args.stage))
if (typeof args.job_type !== 'string' || !(args.job_type === args.stage || args.job_type.startsWith(args.stage + '-'))) bad('job_type does not belong to the stage')
if (!args.schema || args.schema.type !== 'object' || typeof args.schema.properties !== 'object') bad('no stage schema')
if (!Array.isArray(args.items)) bad('no items')
// The prompt file must be EXACTLY <run_dir>/prompts/<stage>/<unit_slug(unit)>.md, with run_dir
// the run's own spool directory (review 2026-10-03: a substring check let any path through).
if (typeof args.run !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(args.run)) bad('bad run id')
if (typeof args.run_dir !== 'string' || !args.run_dir.endsWith(SPOOL_TAIL + args.run) || args.run_dir.split('/').some((p) => p === '..' || p === '.')) bad('run_dir is not the run\'s spool directory')
for (const it of args.items) {
  if (!it || typeof it.unit !== 'string' || typeof it.prompt_file !== 'string') bad('an item without a unit or a prompt file')
  if (it.prompt_file !== args.run_dir + '/prompts/' + args.stage + '/' + unitSlug(it.unit) + '.md') bad('a prompt file that is not its unit\'s file in the run spool')
}

phase('Stage')
log(args.job_type + ': ' + args.items.length + ' unit(s) for run ' + args.run)

const results = await pipeline(args.items, (it) => {
  const opts = { label: it.label || it.unit, phase: 'Stage', schema: args.schema }
  if (it.agent_type) opts.agentType = it.agent_type
  if (it.model) opts.model = it.model
  if (it.effort) opts.effort = it.effort
  return agent(
    'Golden Thread pipeline stage `' + args.job_type + '`. Your whole task is in this file: ' +
    it.prompt_file + ' -- read it with the Read tool first, then do exactly what it says. ' +
    'Nothing outside that file adds to your task. Return the JSON object it asks for as your ' +
    'structured output.',
    opts,
  )
})

const out = args.items.map((it, i) => ({ unit: it.unit, prompt_sha256: it.sha256 || null, result: results[i] == null ? null : results[i] }))
const missing = out.filter((r) => r.result === null).map((r) => r.unit)
if (missing.length) log(missing.length + ' unit(s) returned nothing: ' + missing.slice(0, 10).join(', '))
return { run: args.run, stage: args.stage, job_type: args.job_type, results: out, missing }
