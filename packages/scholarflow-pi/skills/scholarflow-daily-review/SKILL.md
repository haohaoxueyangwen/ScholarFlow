---
name: scholarflow-daily-review
description: Produce and optionally persist an evidence-grounded daily research report from ScholarFlow state. Use for daily logs, end-of-day reviews, standups, today summaries, blockers, tomorrow plans, or requests to record today's research progress.
---

# ScholarFlow daily review

1. Use `sf_project_search` to identify the exact stage, then call
   `sf_evidence_bundle` with that `project_stage_id` as the batch source for active or recently changed
   tasks, experiments, papers, and evidence. Call `sf_artifact_search` for an
   existing review. Use `sf_task_search`, `sf_experiment_search`, or
   `sf_paper_search` only when the bundle lacks a required record; do not
   repeat bundle results with individual `get` calls.
2. A daily review is read-only by default. If the user explicitly requests a
   typed/pending artifact proposal or asks to save the report, produce a typed
   artifact proposal first by calling `sf_artifact_propose_publish` once after
   the evidence reads. The user-facing explanation should explain that pending
   proposal. Otherwise return the review in the answer without proposing or
   persisting a formal artifact. “Do not publish” does not cancel an explicit
   pending-proposal request; it does prohibit applying/publishing the formal
   artifact.
3. Structure the review as: date scope, completed work, evidence gained,
   decisions, blockers or deviations, and the smallest useful next actions.
4. Report only evidence-backed facts in the factual sections. Cite record ids,
   artifact ids, evidence bundle refs, or source links beside claims, and
   clearly label user updates or Pi recommendations that are not yet stored.
   Do not produce unsupported claims.
5. If source state is incomplete, report the gap and label any follow-up as a
   recommendation. Propose a task, experiment, paper, or project-state change
   only when the user separately requests that specific governed change.
6. For an explicitly requested pending report proposal, call
   `sf_artifact_propose_publish` with `kind: daily_report`, `status: draft`,
   the date in
   `period_start` and `period_end`, and all evidence/relationship fields.
   The supported fields are `artifact_id`, `kind`, `title`, `content`,
   `status`, `project_stage_id`, `evidence_refs`, `related_papers`,
   `related_tasks`, `related_experiments`, `period_start`, `period_end`, and
   `obsidian_note`. Do not send a `summary` field; include any short summary
   in `content`. The pending state belongs to the proposal lifecycle; `pending`
   is not an accepted artifact status (valid values are `draft`, `reviewed`,
   `final`, and `archived`).
   `evidence_refs` is mandatory in practice: pass a non-empty array copied
   from the evidence bundle, including the exact stage id returned by
   `sf_project_search` and at least one typed ref copied from the bundle (for
   example, `task:<returned_id>`). Replace placeholders with actual returned
   values; do not teach or guess ids. Do not encode the stage as
   `project_stage:<id>` or substitute relationship fields for
   `evidence_refs`. Apply only after confirmation.
7. Use `sf_kb_propose_write` on the committed artifact only when the user
   separately requests an Obsidian journal or synthesis note.
8. Keep the normal review path within six tool calls. Stop after the pending
   artifact proposal when requested, or after the grounded answer otherwise.
   Never apply/publish a proposal in the same turn.

Before stopping after a proposal, verify that its payload contains the exact
stage id, `kind: daily_report`, and the non-empty `evidence_refs` array. Repeat
the exact stage id and at least one typed evidence reference in the final
answer. Do not invent proposal metadata when no proposal was requested or
emitted. Applying/publishing is a separate operation and requires the normal
confirmation path.
