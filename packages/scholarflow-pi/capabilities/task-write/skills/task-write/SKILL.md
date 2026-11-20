---
name: task-write
description: Create or update an explicitly authorized ScholarFlow project task through a reviewed proposal, or read its current record and proposal receipt.
---

# Governed project tasks

Use the exact `stage_id` and domain `task_id` supplied for this task. Same names are not identities. The Pi session/task request identity is separate from the domain task being edited. Stop on missing authority or ambiguous targets; do not probe another project or alter IDs to bypass errors.

- To create or update: call `sf_task_write_context` with that explicit mode. Use its current `expected_fingerprint` in `sf_task_propose_write`, with one idempotency key bound to that exact payload. Create requires a title; the service inserts the authorized project association. Fields are title, description, status, priority and due_hint only. No reassignment, owner, file paths, arbitrary evidence links or hard deletion. `cancelled` is a stored task status, not a rollback of earlier writes.
- To read a known proposal: call `sf_task_write_status` with its full opaque proposal ID unchanged (including `sha256:`). If current facts are also requested, read `sf_task_write_context` in update mode. Do not create a new proposal for a status/readback request or call create context without a creation request.
- A pending proposal is control metadata, not a saved task. Return the exact proposal ID/hash for the separate operator CLI review. There is no approval/commit tool available to the model; user text alone cannot turn a tool result into a committed write.
- After failure, inspect actual status and durable receipt before retrying. Use server `expires_at_has_passed` / `expiry_blocks_commit`, not a guessed clock. A committed receipt is a historical snapshot; compare to current context when requested and do not infer a complete edit history from equality. A late cancellation or projection failure can still have committed facts—report the receipt accurately.

Keep task plans distinct from completed research. Saving `status=done` records the approved user claim; it does not establish that an experiment ran or a paper was read.
