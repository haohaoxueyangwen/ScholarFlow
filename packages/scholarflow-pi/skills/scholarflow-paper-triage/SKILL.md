---
name: scholarflow-paper-triage
description: Operate the Pi-native paper-triage protocol lane for a new paper. Use when the user wants grounded project association and the minimum governed paper proposal without committing.
---

# ScholarFlow paper triage

Use this skill only for the Pi-native lane that turns a paper mention,
citation, DOI, URL, or title into the smallest governed paper intake proposal.
Ground the paper against active ScholarFlow work before proposing anything.

## Required flow

1. Before any other ScholarFlow tool, call `sf_lane_enter` with
   `lane: "paper"`. Enter the lane with the paper source the user supplied and
   identify the known metadata, unknown metadata, concise claims, relevance,
   and uncertainty.
2. Read only the lane-approved typed tools needed to ground the paper:
   - `sf_project_search`
   - `sf_task_search`
   - `sf_experiment_search`
   - `sf_project_get`
   - `sf_task_get`
   - `sf_experiment_get`
   - `sf_paper_search`
   - `sf_relation_search` (only when inspecting confirmed links for an already
     stored paper)
   - `sf_paper_propose_intake`
   - `sf_kb_propose_write`
3. Use the minimum grounded reads needed to find an evidence-backed
   association. Never invent ids.
4. If identity or essential metadata remains too ambiguous, ask one bounded
   clarification question and stop.
5. Otherwise call `sf_paper_propose_intake`; place paper claims in `summary`
   and project-specific interpretation in `relevance`.
6. If the user explicitly wants a durable reference note, call
   `sf_kb_propose_write` for the stored paper with `content_kind: reference`.
7. Produce the minimum governed result and stop at the first successful exit.

## Success exits

- `clarify_required`: ask one bounded clarification question and stop.
- `paper_intake_proposal`: present the paper intake proposal and stop.

## Lane rules

1. Prefer the smallest read set that can identify the project, task, or
   experiment association. Do not widen to unrelated searches for rescue.
   Once you enter the `paper` lane, stay there; do not switch to `intake`,
   `literature`, or `all`.
2. Use `sf_project_search`, `sf_task_search`, `sf_experiment_search`, and
   follow-up `get` calls only when they are needed to resolve one exact record.
3. Use `sf_paper_search` only when checking whether the paper already exists or
   when the user provides a strong identifier such as a DOI or URL.
   For a title-only request, do not spend a call searching the same unknown
   title unless duplicate detection is essential. Search results already
   contain enough fields for association; call a matching `get` only when a
   required field is absent or the search is ambiguous. Keep the ordinary
   title-to-proposal path within eight tool calls, including lane entry.
   When the request is specifically about an existing paper's evidence links,
   call `sf_relation_search` with the exact `paper:<id>` as `subject_ref` and,
   if needed, once as `object_ref`; do not broaden it to an unbounded query.
4. Do not call `memory_search`, `session_search`, `sf_quality_status`,
   `sf_inbox_search`, or `skill_manage` to compensate for missing grounding.
5. Do not call `sf_zotero_*` merely to inspect availability. Use Zotero only
   when the user explicitly requests Zotero import or sync, or supplies a real
   Zotero item key. A title, DOI, URL, or ordinary paper metadata is not a
   Zotero item key and must not trigger `sf_zotero_describe`.
6. Do not invent ids, authors, year, claims, or relevance. Surface uncertainty
   explicitly in the proposal when the source is incomplete.

## Proposal rules

1. The default output is one `sf_paper_propose_intake` proposal.
2. A `sf_kb_propose_write` reference note is optional and should be added only
   when the user explicitly asks for a durable note.
3. Do not call `sf_paper_apply_intake` or `sf_kb_apply_write` in the same
   turn. Keep the result at proposal stage and preserve interactive
   confirmation.
4. Never pass a raw note body that bypasses the Constitution decision or target
   path review.
5. After the minimum proposal set is ready, stop immediately. Pi handles
   confirmation later.
