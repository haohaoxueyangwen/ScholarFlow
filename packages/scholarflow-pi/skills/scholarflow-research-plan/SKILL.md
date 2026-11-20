---
name: scholarflow-research-plan
description: Build or revise an evidence-linked research plan from ScholarFlow projects, papers, tasks, experiments, and prior artifacts. Use for research roadmaps, thesis plans, milestone planning, evidence-gap analysis, experiment sequencing, or deciding the next research stage.
---

# ScholarFlow research plan

1. Use `sf_project_search` to identify the exact stage, then call
   `sf_evidence_bundle` with that `project_stage_id` as the batch source for its linked tasks, papers,
   experiments, and evidence. Call `sf_artifact_search` to find a reusable
   plan. Use `sf_task_search`, `sf_paper_search`, or `sf_experiment_search`
   only as a fallback when the bundle is missing a required object; do not
   repeat bundle results with individual `get` calls.
2. Produce a typed artifact proposal first with `sf_artifact_propose_publish`.
   Render markdown only as the user-facing explanation of that proposed artifact.
3. Call `sf_research_perspectives` and use only perspectives relevant to the decision.
4. Treat `sf_evidence_bundle` as read-only: it may retrieve, summarize, and cite,
   but it must not emit proposals, commits, or any other ScholarFlow state transition.
5. Separate stored evidence, hypotheses, recommendations, and unresolved questions. Put ScholarFlow ids, evidence bundle refs, or external source links beside every evidence claim.
6. Structure the plan around objective, current evidence, evidence gaps, hypotheses, experiment sequence, milestones, risks, decision gates, and next actions.
   Do not invent quantitative gate thresholds or parameter directions. Derive
   them from stored evidence or an external source, otherwise label them as
   proposed values that require domain confirmation. Prefer qualitative gates
   when no threshold is stored; never present an invented number as an
   acceptance criterion.
7. Every plan section and decision gate must link back to stored records or evidence bundle references. Do not produce unsupported claims.
8. Reuse an existing artifact when it represents the same plan; otherwise create a stable `artifact_id`.
9. Propose the result with `sf_artifact_propose_publish`, `kind: research_plan`, and complete evidence/relationship fields. `evidence_refs` is mandatory in practice: pass a non-empty array copied from the evidence bundle, including the exact stage id returned by `sf_project_search` and at least one typed linked ref copied from the bundle (for example, `task:<returned_id>`, `paper:<returned_id>`, or `experiment:<returned_id>`). Replace placeholders with actual returned values; never teach or guess ids. Do not replace the exact stage id with `project_stage:<id>` and do not rely on `related_*` fields instead of `evidence_refs`. Apply only after Pi's interactive confirmation.
10. Propose task, experiment, or project changes separately. Never hide operational mutations inside the plan artifact.
11. When a support, contradiction, dependency, or derivation is important beyond this plan, propose a typed edge with `sf_relation_propose_upsert`.
12. Do not call `sf_kb_propose_write` for a project stage as a shortcut for
   persisting a plan. A plan must first become a confirmed research artifact;
   propose a KB write only for that committed artifact and only when the user
   requests an Obsidian copy.
13. Keep the normal plan path within six tool calls: project search, artifact
   search, one evidence bundle, relevant perspectives, and one artifact
   proposal. Do not call mind-map or relation-search tools unless the user
   explicitly requests those outputs.

## Proposal and answer contract

Before stopping, verify that the proposal payload visibly contains the exact
`project_stage_id`, `kind: research_plan`, and the non-empty `evidence_refs`
array. Your next assistant message MUST include a small metadata block with
the literal values copied from the proposal, for example:

`project_stage_id: <returned_stage_id>`
`kind: research_plan`
`status: draft`
`evidence_ref: <typed_ref_from_bundle>`

Replace placeholders with actual values returned by the tools.

In the final answer repeat the exact stage id, proposal kind, proposal status,
and at least one typed evidence reference; do not rely on the tool payload
alone.
