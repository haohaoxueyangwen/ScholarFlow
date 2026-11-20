// Capability-local resource resolution, not an intent/lane classifier.
export function projectScope() {
  let explicit = new Set();
  let candidates = new Set();
  return {
    beginTurn(prompt = "") {
      explicit = new Set(String(prompt).match(/\bstage-[A-Za-z0-9][A-Za-z0-9_-]*\b/g) || []);
      candidates = new Set();
    },
    searched(result) {
      // A paginated single visible result is not a unique match.
      candidates = Number(result.total_matches ?? result.total) === 1 && result.has_more !== true && result.items?.length === 1
        ? new Set([result.items[0].stage_id]) : new Set();
    },
    assertRead(stageId) {
      if (explicit.has(stageId) || candidates.has(stageId)) return;
      throw new Error("Project scope unresolved: use a user-supplied exact stage ID or a unique project search result. Multiple stages require user clarification before any stage-specific read.");
    },
    authorizedIds() { return [...new Set([...explicit, ...candidates])]; },
  };
}
