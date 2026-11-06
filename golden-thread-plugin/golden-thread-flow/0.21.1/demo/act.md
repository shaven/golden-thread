## See the flow

narration: Every promotion in this tour was recorded as an event, not a line of prose. The flow view draws those events: one lane per project, time left to right, and an arrow each time a piece of knowledge climbed a level.
do: Invoke the gt-flow skill to render the demo vault's flow: run `python3 <module:flow>/gt_flow.py render --vault $GT_VAULT --redact`, open the file it prints, and point at one arrow climbing from research to Knowledge. If it exits 2 (no events yet), read its message to the audience instead: it says how events are produced.
point: The page is one offline file drawn from the event stream, and because it was rendered with --redact it names no file and no project, so it can be shared as it is.
