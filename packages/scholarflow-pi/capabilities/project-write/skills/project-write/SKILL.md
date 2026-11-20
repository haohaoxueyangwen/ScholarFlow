---
name: project-write
description: Propose creating an authorized project stage from a user goal or stored paper evidence, or update recorded progress and inspect proposal status. Use for requested project changes, not read-only project questions.
---

Use the exact stage and action permitted by the operator's project-write grant. Call `sf_project_write_context` to obtain the current record or absence and expected_fingerprint. Do not guess a version or use an old chat summary; if current data is unavailable, stop and explain the dependency/scope failure.

Choose create only for an explicitly requested new stage; inspecting an existing stage or its committed update uses update. A successful context/status read is not a reason to probe the other mode. Treat stage/proposal IDs as opaque: preserve the exact supplied or returned ID, including any sha256: prefix. Do not test stripped prefixes or alternate IDs. If the exact ID is not found, ask for correction instead of guessing variants.

Call `sf_project_propose_write` with create/update, changes, expected_fingerprint and a stable idempotency key. Reuse the key only for the same payload. Updates currently allow progress_note, status, next_actions and risks. For creation, use context.creation_basis: user_goal permits an evidence-free starting stage only with the exact authorized_goal returned by the host; this is a research intention, not paper evidence. Supply project_name, title and that goal; never supply creation_origin or fake references. Otherwise paper_evidence requires authorized, stored paper evidence_refs. If neither authorized basis is available, stop rather than inventing one.

Show the returned proposal ID, exact review hash and diff. A pending proposal is durable control metadata, not committed project progress. Ask the human operator to review and approve/reject through the separate CLI; no model tool can approve or apply. A natural-language "confirmed" is not a substitute for that channel.

Use `sf_project_write_status` to inspect the same owned stage/proposal after operator action, restart, timeout or response loss. Only an actual committed domain receipt proves that write occurred. It does not prove the record is still current after later edits; read current project facts separately. On expired/stale/rejected/permission errors stop, explain the state, and obtain fresh data/authority rather than retrying with fabricated approval. Cancellation does not undo a commit.

Ground time and history claims in returned evidence. Use the server's expires_at_has_passed and expiry_blocks_commit fields, never the chat date or a guessed UTC comparison. A committed receipt remains historical evidence after expiry. edit_history_coverage is not a full edit log: matching current values/updated_at do not prove "no later edits". State only that the observed current value matches (or differs from) the committed value. If these fields are absent, do not infer expiry or complete edit history.
