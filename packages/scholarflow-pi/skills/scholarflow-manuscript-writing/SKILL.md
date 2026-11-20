---
name: scholarflow-manuscript-writing
description: Draft, restructure, or plan an evidence-grounded scientific manuscript or section with Nature-style argument discipline. Use for abstracts, introductions, methods, results, discussions, conclusions, titles, Chinese-to-English scientific drafting, or initial-submission prose tied to a ScholarFlow project. Do not use for literature reviews, reviewer responses, final polishing alone, or direct Overleaf synchronization.
---

# ScholarFlow manuscript writing

Use the reviewed Apache-2.0 `nature-writing` method through ScholarFlow's
typed evidence and proposal boundary. Upstream provenance is pinned under
`../../vendor/nature-skills`; this adapter, not the vendor Skill, owns Pi tool
selection and write governance.

## Bounded workflow

1. Enter the `writing` lane with `sf_lane_enter`.
2. Resolve the exact project once with `sf_project_search`. Do not guess a
   stage id.
3. Call one project-scoped `sf_evidence_bundle`, then
   `sf_manuscript_search` and `sf_artifact_search` for existing local or
   conceptual drafts. Scope artifact lookup with
   `kind="manuscript_draft"` and the exact returned `project_stage_id`.
   Reuse returned
   records; do not widen into generic memory, web search, or direct files.
4. State the writing axes briefly: task=`manuscript`, section, language, and
   journal (`nature`, `nature-family`, `nat-comms`, or `generic`). Default to
   research paper, the requested section, detected language, and `generic`.
5. Build a claim/evidence/boundary map before prose. Preserve uncertainty,
   negative results, limitations, terminology, units, and exact citation
   placeholders. If evidence is missing, insert `[EVIDENCE NEEDED: ...]` and
   list it after the draft; never invent a result, citation, author, or venue.
6. Draft with one function per paragraph: context, gap, claim, evidence,
   boundary, implication. Prefer a clear argument over imitation of Nature's
   printed layout. For Chinese-to-English work, stabilize terminology before
   translating sentence structure.
7. A user-facing draft request does not by itself authorize a durable proposal. When the user explicitly requests a pending typed proposal or asks to save the draft, produce a typed artifact proposal first after the evidence reads by calling `sf_artifact_propose_publish` exactly once. The user-facing explanation should explain this proposal, not create a second untracked draft. Use `kind="manuscript_draft"`,
   `status="draft"`, the exact `project_stage_id`, and non-empty
   `evidence_refs` containing the stage id plus at least one typed stored ref.
   The content must include the axes, claim/evidence map, draft, and missing
   inputs. Do not produce unsupported claims. “Do not apply/write files” does
   not cancel an explicit pending-proposal request; the proposal is not a
   formal apply/publish. Without that request, provide the draft in the answer
   and do not create a proposal.
8. For an explicitly requested edit to an existing governed local manuscript,
   call `sf_manuscript_get`, then
   `sf_manuscript_read_section` for the exact target. Convert the reviewed
   prose into LaTeX for that section only and call
   `sf_manuscript_propose_patch` with its exact file hash and evidence refs.
   Run `sf_manuscript_validate` only against the current accepted revision.
   `sf_manuscript_compile` may render that accepted revision to derived PDF,
   but do not claim the proposed replacement compiled.
9. The supported `sf_artifact_propose_publish` fields are `artifact_id`,
   `kind`, `title`, `content`, `status`, `project_stage_id`, `evidence_refs`,
   `related_papers`, `related_tasks`, `related_experiments`, `period_start`,
   `period_end`, and `obsidian_note`. There is no `summary` field; put any
   summary in `content`. Valid artifact statuses are `draft`, `reviewed`,
   `final`, and `archived`; use `draft` for a pending proposal (`pending` is
   proposal lifecycle state, not an artifact status). Stop after the requested
   proposal. Never call
   `sf_artifact_apply_publish`, `sf_manuscript_apply_patch`,
   `sf_kb_apply_write`, filesystem tools, Git, or Overleaf in this turn.
   Applying a reviewed manuscript and synchronizing Overleaf are separate,
   explicitly confirmed operations. A proposal requires separate user
   confirmation before any apply path becomes eligible.

For an artifact proposal, repeat the exact proposal status,
`project_stage_id`, `kind`, and at least one evidence reference. For a local
patch, repeat the exact manuscript id, section, proposal id, and evidence ref.
