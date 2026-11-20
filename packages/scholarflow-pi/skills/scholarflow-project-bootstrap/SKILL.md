---
name: scholarflow-project-bootstrap
description: Start a new ScholarFlow research project from stored paper evidence and propose its first task after the project is confirmed. Use when the user asks to turn literature into a project, needs an evidence-grounded thesis/project starting point, or asks for the first task or deliverable of an existing project stage.
---

# ScholarFlow project bootstrap

Four-exits manifest: `pi-task-contract-four-exits-v1` /
`docs/architecture/pi-task-contract-four-exits-v1.md` (skill onboarding:
`docs/architecture/skill-onboarding-template-v1.md`).

Outcome contract: `project-create-outcomes-v4`
(`docs/architecture/project-create-outcomes-contract-v4.md`).

Legal exits: `create_proposal`, `reuse_existing`, `clarify`, `evidence_gap`,
`first_task_proposal`.

## Create a project from papers

1. Call `sf_paper_search` once with an empty query to obtain the bounded
   stored-paper set, then select records relevant to the user's topic.
2. Call `sf_paper_get` only when search results lack enough detail to ground the
   goal. Never invent paper ids.
3. Call `sf_project_search` once with the proposed project name or topic to list
   candidate stages for comparison. Never use an empty overview query for this
   duplicate check. If multiple candidates appear, call `sf_project_get` with
   one exact listed `stage_id` before choosing reuse versus create.
4. Choose **exactly one** legal outcome from the contract:

| Outcome | When | Next action |
| --- | --- | --- |
| `create_proposal` | Enough paper evidence and the intended scope is clearly distinct | Call `sf_project_propose_create` once with exact `paper:<paper_id>` refs; stop before apply |
| `reuse_existing` | An existing stage is equivalent (goal, object, deliverables, and scope align) | Cite the exact existing `stage_id`; offer reuse or update; **do not** create |
| `clarify` | Candidates are similar but equivalence cannot be confirmed | State differences and ask one clarifying question; **do not** create |
| `evidence_gap` | No suitable stored paper for the requested scope | Report the gap; route to intake/triage; **do not** invent evidence or create |

5. Dedup clues such as similar names or shared papers are **not** enough alone.
   Compare goal, research object, deliverables, and scope before choosing
   `reuse_existing`. If the user asked to prepare a create proposal for a **new**
   stage, emit `sf_project_propose_create` unless they explicitly asked to reuse
   an equivalent stage.
6. For `create_proposal` only: derive a stable lowercase `stage_id` beginning
   with `stage-`. Build a concise project name, stage title, falsifiable goal,
   known risks, and concrete next actions from selected evidence. Call
   `sf_project_propose_create` with a non-empty `evidence_refs` array.
7. Show the proposal id (or the reused stage id / clarification / evidence gap),
   then stop. Do not apply unless the user interactively confirms a proposal.
8. After a project has been confirmed and stored, create the first task through
   a separate `sf_task_propose_create` call. Never reference an uncommitted
   project proposal from a task.

## Propose the first task

1. Resolve one exact stored stage with `sf_project_search` or `sf_project_get`
   when the user supplied the exact stage id.
2. Call `sf_task_search` with that exact `project_stage_id` to avoid proposing a
   duplicate first task.
3. If the project is absent or still only an uncommitted proposal, stop without
   creating a task.
4. Derive one bounded deliverable from the stored goal, next actions, and
   evidence refs. Keep the title concrete and state a completion definition in
   `description`.
5. Call `sf_task_propose_create` with the exact stored `project_stage_id`, a
   stable `task-` id, `status: pending`, and the paper ids from the project's
   `paper:*` evidence refs in `related_papers`.
6. Show the task proposal id and stop. Do not apply it automatically.

## Boundaries

- Treat stored papers as evidence, not proof that a proposed hypothesis is true.
  Keep inference language explicit.
- Use project creation proposal-first and require interactive confirmation.
- Do not use memory or session history as evidence refs.
- Do not apply project or task proposals automatically.
- Backend rejects exact `stage_id` collisions; do not treat that as semantic
  duplicate detection.
- Never force a create proposal merely because a checklist expects one when the
  correct contract outcome is reuse, clarify, or evidence gap.
