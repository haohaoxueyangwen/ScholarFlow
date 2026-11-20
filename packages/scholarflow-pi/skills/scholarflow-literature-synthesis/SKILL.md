---
name: scholarflow-literature-synthesis
description: Search, triage, compare, and persist an evidence-linked literature synthesis for a research question. Use for literature reviews, related-work maps, paper comparisons, evidence tables, research gaps, citation-grounded claims, or updating a review from new papers.
---

# ScholarFlow literature synthesis

## Hard execution order

For a project-scoped stored-literature request, execute this bounded read path
in order: `sf_project_search` -> one `sf_evidence_bundle` with the returned
`project_stage_id` -> `sf_artifact_search` -> optional `sf_paper_search` only
if the bundle does not establish the corpus. A synthesis request alone does
not authorize creating a pending artifact proposal. Do not perform any
project/task/experiment/paper `get`, relation search, experiment observation,
or additional broad search before the scoped bundle.

1. Use `sf_paper_search`, `sf_artifact_search`, and one
   `sf_evidence_bundle`, passing the exact `project_stage_id`, before using
   Web/PDF or scholarly MCP retrieval. Treat
   the bundle as the batch read for linked project, task, experiment, and
   paper records; do not repeat its records with individual `get` calls.
2. Only when the user explicitly requests a typed/pending artifact proposal or asks to save the output, produce a typed artifact proposal first by calling `sf_artifact_propose_publish` exactly once after the bounded reads. The user-facing explanation should explain that pending proposal. Otherwise provide the synthesis in the answer and make no proposal. A request to propose while saying “do not publish” still authorizes the pending proposal; it does not authorize applying or publishing the formal artifact.
3. Define the question, inclusion boundary, and evidence dimensions. Treat search results as candidates until their metadata and relevance are checked.
4. Intake important sources with `sf_paper_propose_intake`; do not store search snippets as paper conclusions.
5. Treat `sf_evidence_bundle` as read-only: it may retrieve, summarize, and cite, but it must not be used as a shortcut for proposals, commits, or other state transitions.
6. Structure the synthesis as: question and scope, evidence inventory, comparison axes, supported conclusions, disagreements, evidence gaps, and follow-up searches.
7. Every synthesis claim must cite stored paper ids, artifact ids, evidence bundle refs, or source links. Do not produce unsupported claims; move ungrounded ideas into the evidence-gap or follow-up section.
8. For an explicitly requested proposal, use `sf_artifact_propose_publish`. Its
   supported fields are `artifact_id`, `kind`, `title`, `content`, `status`,
   `project_stage_id`, `evidence_refs`, `related_papers`, `related_tasks`,
   `related_experiments`, `period_start`, `period_end`, and `obsidian_note`.
   Do not send a `summary` field; put any short summary in `content`. The `kind` must
   be exactly `literature_review` (not `synthesis`), with `status: draft`,
   related paper ids, `project_stage_id` when known, and evidence references.
   The proposal lifecycle is pending; `pending` is not a valid artifact
   status. Apply only after confirmation.
   The `evidence_refs` array must be non-empty and must include the exact stage
   id returned by `sf_project_search` plus at least one typed paper/task/
   experiment ref copied from the bundle (for example, `paper:<returned_id>`).
   Replace placeholders with actual returned values; never teach or guess ids.
   Do not encode the stage as
   `project_stage:<id>` inside `evidence_refs`, and do not rely on
   `related_papers` alone. In the final answer, repeat the exact
   `project_stage_id`, proposal kind, proposal status, and at least one typed
   evidence reference; do not rely on the tool payload alone. The next
   assistant message MUST include a metadata block with literal copied values,
   for example `project_stage_id: <returned_stage_id>`,
   `kind: literature_review`, `status: draft`, and
   `evidence_ref: <typed_ref_from_bundle>`. Replace placeholders with actual
   returned values.
9. Write it to Obsidian only through a separately requested and confirmed
   `sf_kb_propose_write` on the committed artifact.
10. Persist only high-value support or contradiction edges with the governed relation proposal/apply tools.
11. For a stored-literature-only request, do not call `sf_research_mind_map`,
    `sf_relation_search`, extra broad searches, or external retrieval unless
    the user asks for them. Keep the normal proposal path within six calls.

`literature_review` is the semantic contract for every literature review,
survey, related-work synthesis, or paper comparison. The separate
`synthesis` kind is reserved for a general cross-domain synthesis and must not
be used for this skill, even when the user says “synthesize.” A successful
proposal creates a pending governed proposal only; never call
`sf_artifact_apply_publish` unless the user separately asks to apply an exact
existing proposal and the normal interactive confirmation succeeds.
