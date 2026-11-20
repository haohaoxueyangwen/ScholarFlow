---
name: scholarflow-research-intake
description: Operate the Pi-native intake protocol lane for a new research idea or request. Use when the user wants ScholarFlow alignment and the smallest governed proposal set without committing.
---

# ScholarFlow research intake

Use this skill only for the Pi-native entry lane that turns a new idea,
request, or source into a deterministic ScholarFlow intake item and then the
smallest governed proposal set. Do not free-search the whole research system
before opening the lane.

## Required flow

1. Enter the lane by calling `sf_lane_enter` with `lane="intake"`. This is
   mandatory because the Pi runtime starts in a bootstrap surface that exposes
   only the lane loader; do not batch any other ScholarFlow tool in the same
   assistant message as lane entry.
2. Call `sf_intake_create` with the raw user request.
3. Read only the lane-approved typed tools in order:
   - `sf_intake_create`
   - `sf_intake_anchor`
   - `sf_intake_prepare`
   - `sf_intake_propose`
4. Call `sf_intake_anchor` on the returned `intake_id`.
5. Treat the anchor response as candidates, not a routing decision. Select the
   smallest route that satisfies the user's request and select exact ids only
   from the returned candidates. When the user explicitly names a project,
   match it against each structured `stage_candidates[].project_name` and use
   that candidate's exact `stage_id`; do not select a different related
   project merely because it also ranked.
6. If there is no defensible stage candidate, ask one clarification question
   based on `missing_fields` and stop. Otherwise call `sf_intake_prepare` with
   an explicit `route`, `stage_id`, and only the related ids Pi actually chose.
7. In that same turn, immediately call `sf_intake_propose` for the same
   `intake_id`. This is mandatory: `sf_intake_prepare` is never a success exit.
   “Do not commit” means do not call an apply tool; it never means skip the
   uncommitted proposal-emission step.
8. Produce only the minimum governed result returned by the lane and stop at
   the first successful exit.

## Success exits

- `clarify_required`: ask one bounded clarification question and stop.
- `anchored`: choose a route and candidate ids, then prepare and propose.
- `proposal_ready`: call `sf_intake_propose`, present the emitted governed
  proposal objects, and stop.

## Source of truth

Use the deterministic intake payloads as the source of truth for candidate
records and proposal scaffolds. Pi, not Python, owns the semantic decision for:

- the selected `project_stage_id`
- the minimum route needed for the request
- which candidate governed records are actually related

Pass those choices explicitly to `sf_intake_prepare`. Never assume that the
first candidate is automatically correct.

Use the prepared payload as the source of truth for:

- the proposal scaffolds
- the emitted governed proposal objects

Allowed proposal routes are:

- `task_only`
- `task_and_experiment`
- `paper_intake`

Pi selects one route. The deterministic intake subsystem validates the choice
and turns it into governed proposal objects.

## Lane rules

1. In the default intake lane, do not call:
   - `sf_project_search`
   - `sf_project_get`
   - `sf_task_search`
   - `sf_task_get`
   - `sf_experiment_search`
   - `sf_experiment_get`
   - `sf_paper_search`
   - `sf_paper_get`
   - `sf_artifact_search`
   - `sf_relation_search`
   - `sf_quality_status`
   - `sf_inbox_search`
   - `memory_search`
   - `skill_manage`
   - `sf_project_propose_update`
   - `sf_relation_propose_upsert`
   - `sf_research_mind_map`
   - `sf_experiment_observe` (for approved experiment evidence files)
2. After `sf_intake_anchor`, stay inside the intake lane. Do not open
   additional ScholarFlow read tools to re-check stage, task, experiment, or
   paper context. `sf_intake_prepare` is the only follow-up read step in the
   default lane.
3. Do not infer ids that were not returned by the anchor response. If the
   candidates are insufficient, ask one clarification question.
4. After `sf_intake_prepare` has returned a sufficient anchor and route, do not
   inspect more evidence or widen to rescue tools.
5. Do not invent ids. Use the exact ids and proposal scaffolds returned by the
   intake subsystem.
6. Do not preflight-check a new task or experiment id with `sf_task_get` or
   `sf_experiment_get`. If the intake subsystem prepared a new object, propose
   it directly.
7. Do not widen intake into cleanup of existing stages, experiments, inbox
   items, relation graphs, artifacts, or summaries.

## Proposal rules

1. Keep the default output to the smallest governed set:
   - at most one Task proposal
   - optionally one Experiment proposal
   - or one Paper intake proposal
2. Do not manually reconstruct proposal payloads if `sf_intake_propose` already
   returned them.
3. Do not create batches of proposals that cross-reference other uncommitted
   new objects.
4. Do not end a successful default intake turn after `sf_intake_prepare` alone.
   If the route is proposal-ready, `sf_intake_propose` is required in the same
   turn.
5. After `sf_intake_propose` has emitted the minimum proposal set, stop and
   present it for review. Reproduce each returned operation name exactly in
   the final summary (for example `task.create`, `experiment.log`, or
   `paper.intake`) so the governed result is machine-auditable. Pi handles
   confirmation later.
