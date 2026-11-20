---
name: scholarflow-weekly-review
description: Build an evidence-grounded weekly research review from ScholarFlow projects, tasks, papers, and experiments.
---

# ScholarFlow weekly review

1. Use `sf_project_search` to identify the exact stage, then call
   `sf_evidence_bundle` with that `project_stage_id` as the batch source for its tasks, experiments, papers,
   and evidence in the review period. Call `sf_artifact_search` for an existing
   review. Use `sf_task_search`, `sf_experiment_search`, or `sf_paper_search`
   only as a fallback when the bundle lacks a required record; do not repeat
   bundle results with individual `get` calls.
2. A weekly review is read-only by default. If the user explicitly requests a
   typed/pending artifact proposal or asks to save the report, produce a typed
   artifact proposal first by calling `sf_artifact_propose_publish` once after
   the evidence reads. The user-facing explanation should explain that pending
   proposal. Otherwise return the review in the answer without proposing or
   persisting a formal artifact. “Do not publish” does not cancel an explicit
   pending-proposal request; it does prohibit applying/publishing the formal
   artifact.
3. Structure the review as: period scope, completed work, evidence gained,
   blockers, risks, unresolved decisions, recommendations, and next actions.
4. Summarize only evidence-backed facts in the factual sections. Cite record
   ids, artifact ids, evidence bundle refs, or source links beside claims, and
   clearly label recommendations that are not yet stored facts. Do not produce unsupported claims.
5. Report project progress, risks, and next actions as stored facts or clearly
   labeled recommendations. Propose project/task changes separately with
   `sf_project_propose_update` or the matching task tool only when the user
   explicitly requests that governed change.
6. For an explicitly requested pending report proposal, call
   `sf_artifact_propose_publish` with `kind: weekly_report`, `status: draft`,
   period bounds,
   and evidence links. Its supported fields are `artifact_id`, `kind`,
   `title`, `content`, `status`, `project_stage_id`, `evidence_refs`,
   `related_papers`, `related_tasks`, `related_experiments`, `period_start`,
   `period_end`, and `obsidian_note`. Do not send a `summary` field; include
   any short summary in `content`. The proposal's lifecycle may be pending,
   but `pending` is not a valid artifact status; allowed values are `draft`,
   `reviewed`, `final`, and `archived`. `evidence_refs` is mandatory in practice:
   pass a non-empty array copied from the evidence bundle, including the exact
   stage id returned by `sf_project_search` and at least one typed ref. Do not
   encode the stage as `project_stage:<id>` or substitute relationship
   fields for `evidence_refs`. Apply only after separate confirmation.
7. If the committed review should enter Obsidian, and the user requests it,
   call `sf_kb_propose_write` for the research artifact with
   `content_kind: synthesis`.
8. Do not treat session memory as research truth or perform raw vault writes.
9. Keep the normal review path within seven tool calls. Stop after the pending
   artifact proposal when requested, or after the grounded answer otherwise.
   Do not add relation, mind-map, or experiment-observation calls unless the
   user explicitly asks for those analyses. Never apply/publish a proposal in
   the same turn.

Before stopping after a proposal, verify that its payload contains the exact
stage id, `kind: weekly_report`, and the non-empty `evidence_refs` array.
Repeat the exact stage id and at least one typed evidence reference in the
final answer. Do not invent proposal metadata when no proposal was requested
or emitted. Applying/publishing is separate and requires the normal
confirmation path.
