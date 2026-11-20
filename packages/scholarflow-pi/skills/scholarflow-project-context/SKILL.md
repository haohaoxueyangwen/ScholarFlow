---
name: scholarflow-project-context
description: Operate the Pi-native project-context protocol lane for grounded status questions. Use when the user needs current ScholarFlow project or stage truth, especially if memory or user claims conflict with stored state.
---

# ScholarFlow project context

Four-exits manifest: `pi-task-contract-four-exits-v1` /
`docs/architecture/pi-task-contract-four-exits-v1.md` (skill onboarding:
`docs/architecture/skill-onboarding-template-v1.md`).

Outcome contract: `information-duties-v1`
(`docs/architecture/information-duties-contract-v1.md`).

Legal exit: `grounded_answer` (read-only status from ScholarFlow records).

Use this skill only when a request depends on the current stored state of a
ScholarFlow project or project stage.

Information duties follow `information-duties-v1`: session summaries and
preference memory are not current project facts; ScholarFlow records are.

## Required flow

1. Enter the lane with the project, stage, blocker, risk, progress, or next
   action question the user asked.
2. Read only the lane-approved typed tools needed to ground the answer:
   - `sf_project_search`
   - `sf_project_get`
   - the corresponding task, paper, or experiment `search/get` tools when the
     user asks about linked-object state
3. Call `sf_project_search` before making claims about current project status.
   This is mandatory even when the user or preference memory claims a status.
   Ordinary project-status turns do **not** call `memory_search` or
   `session_search`.
4. Use a specific project name, stage title, or stage id as the query when the
   user provides one. For a comparison across multiple named projects, run
   separate bounded `sf_project_search` calls per named project (never one
   combined query), then select the exact returned ids.
5. After identifying one exact stage, call `sf_project_get` when the full
   stored record is needed. A question about the current blocker, risk,
   progress, or next recorded action needs the full record: pass only the exact
   `stage_id` returned by `sf_project_search`.
6. Answer from stored ids and grounded facts, then stop at the first
   successful exit.

## Success exit

- `grounded_answer`: answer after grounded reads and stop.

## Lane rules

1. `ProjectStage` is the current project representation. Do not invent a
   separate project record.
2. Do not use `memory_search` or `session_search` on ordinary project-status
   turns. Those tools open only when the user **explicitly** asks to retrieve
   prior discussion or session history, and even then they are non-authoritative
   hints—never a substitute for the current ScholarFlow read.
3. Use the corresponding task, paper, or experiment `search/get` tools instead
   of inferring linked-object state from a project summary.
4. Distinguish stored facts from suggestions. Risks, progress notes, blockers,
   and next actions returned by the tool are stored facts; new recommendations
   are not.
5. If no record matches, say so and ask for the minimum identifier needed.
6. Do not widen to unrelated mutation, inbox, quality, or rescue tools just
   because the initial query was weak.
7. A user sentence such as "my memory says ..." is an unverified user claim,
   not authorization to inspect generic memory. Resolve the current state with
   ScholarFlow, answer, and stop. Do not call `memory`, `memory_search`, or
   `session_search` to correct, replace, or preserve project facts.
8. Never guess stage ids or retry plausible id variants. Use only ids returned
   by `sf_project_search`; if the bounded search has no match, report that
   absence and ask for the minimum missing identifier.
9. A question asking where a project is currently blocked or what next action
   its record contains is a read-only status question, not a request to create
   a plan. Do not route it to planning, call artifact/evidence-bundle reads
   without need, or emit project/task/artifact proposals. Report the recorded
   next action and label any added recommendation separately.

## Mutation boundary

1. This lane answers grounded context questions; it does not commit changes.
2. For task changes, call `sf_task_propose_update`, inspect its structured
   diff, and call `sf_task_apply_update` only after interactive confirmation.
3. Use only the corresponding governed proposal/apply tools for project-stage,
   paper, experiment, artifact, relation, or vault changes.
