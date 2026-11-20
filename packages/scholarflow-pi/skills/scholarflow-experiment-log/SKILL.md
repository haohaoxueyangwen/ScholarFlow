---
name: scholarflow-experiment-log
description: Operate the Pi-native experiment-log protocol lane for planned, running, completed, failed, or blocked experiments. Use when the user wants a governed experiment proposal without committing.
---

# ScholarFlow experiment log

Use this skill only for the Pi-native lane that preserves experiment facts as
ScholarFlow domain state. A failed experiment is research data, not Agent
failure memory.

## Required flow

1. First call `sf_lane_enter` with `lane: "experiment"`; this is a safety
   boundary that removes generic memory and unrelated workflow tools.
2. Then enter the lane with the user-reported experiment event, result,
   failure, or next-step request. Read only the lane-approved typed tools needed to identify the stored
   experiment and its project context:
   - `sf_experiment_search`
   - `sf_experiment_get`
   - `sf_project_get`
   - `sf_experiment_propose_log`
3. Use the minimum grounded reads needed to decide whether the log is updating
   an existing experiment or proposing a new one.
4. If the target experiment remains ambiguous, ask one bounded clarification
   question and stop.
5. Otherwise call `sf_experiment_propose_log` and preserve commands, paths,
   variables, metrics, results, failures, and next actions as distinct fields.
   A successful proposal is the terminal exit; the Pi extension stops the
   current agent turn immediately after it, so never plan or issue a second
   experiment proposal in the same turn.
6. Produce the minimum governed result and stop at the first successful exit.

## Bounded execution contract

Use one broad `sf_experiment_search` for the user-supplied experiment terms;
do not retry the same search with guessed status variants. When it returns one
exact stored experiment, do not fan out to linked task/project reads merely to
restate context. Use `sf_experiment_get` or `sf_experiment_observe` only when
the proposal needs a field that the search result does not contain. The normal
route is therefore at most one search, two optional reads, and one
`sf_experiment_propose_log` call.

For a failure-preservation request, the first successful
`sf_experiment_propose_log` is the complete exit. Do not create a second
diagnostic experiment, update a task, or continue gathering context in the
same turn. The hard ceiling is six experiment-lane tool calls.

## Success exits

- `clarify_required`: ask one bounded clarification question and stop.
- `diagnostic_proposal`: present the `sf_experiment_propose_log` proposal and
  stop.

## Domain boundary

An experiment failure is a research-domain outcome, not an Agent or tool
failure. Preserve it through the typed experiment proposal. Generic context
stores and unrelated workflow lanes are outside this request's active tool
surface. Agent-procedure failures remain a separate runtime concern.

## Lane rules

1. Do not infer a successful status from an incomplete run or hide a failure
   reason.
2. Use `sf_experiment_get` and `sf_project_get` only after a grounded
   `sf_experiment_search` has identified one exact stored record, and only
   when the search result lacks a field required by the proposal. Do not read
   the linked task or project merely to restate context already returned by
   the experiment. Use `sf_experiment_observe` only when permitted logs,
   checkpoints, or result files are needed to establish the result. The normal update path is search,
   optional get/observe, then proposal, with no more than six tool calls.
3. Remain in the experiment lane for the entire turn. Do not reopen the
   general lane or widen to an unrelated workflow for rescue.
4. Do not store experiment facts in generic memory. Experiments, failures,
   metrics, and next actions belong only in ScholarFlow domain records.
5. Update linked task or project status only through separate proposals so each
   side effect remains reviewable.
6. Use a governed KB write only for durable reference or synthesis content, not
   for every transient log line.

## Proposal rules

1. Keep the default result to one `sf_experiment_propose_log` proposal.
2. Inspect the structured diff before any commit path.
3. Do not call `sf_experiment_apply_log` in the same turn. Keep the result at
   proposal stage and preserve interactive confirmation.
4. After the minimum diagnostic proposal is ready, stop immediately. Pi handles
   confirmation later.
