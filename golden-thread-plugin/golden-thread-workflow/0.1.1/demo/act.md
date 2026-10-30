## Save it as a workflow

narration: Some chains of steps you do every morning. Save one as a workflow and run it by name: the gt steps run on their own, and it stops where it needs you or Claude.
do: Run `bash <scripts>/gt_demo.sh show workflow` and show its output as printed: the starters, the draft Claude wrote for "Build a workflow" (shown before anything is saved), the saved and checked workflow, and the run that did two gt steps and is waiting at the third. Then answer that waiting step for the audience in one sentence from the prompt it printed. Its state lives in the demo vault's ignored .demo/workflow folder, never in your own workflows.
point: The workflow ran real gt tools, needed no network, and waited for Claude exactly where it said it would.
