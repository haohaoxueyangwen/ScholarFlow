---
name: project-evidence
description: Answer questions about a ScholarFlow project's current state and stored evidence. Use for project orientation and evidence-grounded next-step discussion, not for modifying records or searching new literature.
---

# Project evidence

Use `sf_project_search` when the project is identified by name; use the exact returned stage ID with `sf_project_get`. If the user supplies an exact ID, it can be read directly. Clarify ambiguous matches rather than inventing an ID.

Use `sf_evidence_bundle` when the question needs the project's linked evidence. Distinguish stored records, source excerpts, and your suggestions. Cite returned record IDs and say when the stored material does not answer the question.

The profile is read-only: discuss proposed next steps in the answer, but do not claim that tasks, notes, or manuscripts were saved. Native Pi owns conversation and compaction; no lane entry or secondary agent loop is required.
