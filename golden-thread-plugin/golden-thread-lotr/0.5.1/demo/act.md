## Bring your MCP servers into LOTR

narration: You may already have MCP servers set up in Claude Code. LOTR finds them, says what each needs, and puts all of them behind one small gateway whose index finds an operation by what it does.
do: Run `bash <scripts>/gt_demo.sh show lotr` and show its output as printed. Say that the MCP servers come from a fixture config in the demo's own home, that --no-probe and --dry-run contact nothing, and that the tracker connection points at an .invalid address that is never called: find and describe answer from the capability index alone.
point: Three existing servers were found without a single connection, and the index found "my open issues" by meaning, not by name.
