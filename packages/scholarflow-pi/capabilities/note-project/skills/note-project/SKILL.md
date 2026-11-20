---
name: note-project
description: Propose and inspect frozen local Markdown or host-reviewed Obsidian multi-file projections of an existing project source note, with separate operator confirmation.
---

# Controlled note projection

Use sf_note_project_context, sf_note_propose_projection and sf_note_projection_status with exact stage_id/artifact_id/target_id. The source is an existing human research note; mode is update because this capability never creates or edits the source note.

Read context and copy expected_fingerprint and expected_target_hash unchanged (the latter binds the entire multi-file plan in Obsidian mode). The service freezes the complete rendered bytes; do not supply paths, new text or approval. Return the full proposal_id including sha256: and proposal_hash, every exact file change and pending status. Only a separate operator CLI can write the projection.

The context policy chooses the host-granted mode, not the model. local-markdown-v1 writes one file and forbids vaults. obsidian-project-v1 requires a host-reviewed, hash-pinned CLAUDE.md and repository policy; its complete plan includes the note, wiki/log.md, 10_Projects/example_vault.md and an explicitly granted UTC journal when required. Show all side effects and needed directories. This enforces fixed repository checks reviewed against the policy document, not arbitrary natural-language rules. Unknown or changed policy stops execution. Neither mode updates AI knowledge. Stored-summary provenance is not full-PDF verification.

obsidian-example_vault-v1 additionally pins AGENTS.md, requires date/updated/type/status/tags/related metadata and resolving full wiki-path backlinks, and includes wiki/index.md. Every affected file is explicit. Use before_exists and before_hash_kind: a missing-file fingerprint is not the SHA-256 of an empty file. trace_marker is per-file; do not assume a marker is present in every side effect.

Status separates a historical export step receipt from current source/target matches. After a response loss, inspect status before retry; a completed receipt can be replayed without rewriting. Recovery observation of expected bytes is not proof of an exclusive writer or a multi-file transaction.

In Obsidian mode, partial means some steps have durable intent/completion while others are unfinished. Report each completed, uncertain intent and not_started file; check policy/source/target match flags and expiry separately. Never say all files committed from a partial receipt, automatically roll back, or overwrite manual drift. After source/policy/grant/target changes, remaining writes stop and require a newly reviewed recovery plan. Historical completed sync does not prove current files remain unchanged.

The host may grant recovery_of with exactly unfinished paths for a separately reviewed same-source/same-policy recovery. It leaves completed targets and the original partial history untouched. Changed source or policy requires human reconciliation, not automatic continuation of the old bytes.
