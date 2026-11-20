// Capability-owned optional grant validation; no model/runtime routing.
export function validateGoalRequests(grant) {
  if (!("goal_requests" in grant)) return true;
  const goals = grant.goal_requests;
  if (!goals || typeof goals !== "object" || Array.isArray(goals) || Object.keys(goals).length > 100) return false;
  const text = (value, maximum) => typeof value === "string" && value.length > 0 && value.trim() === value && value.length <= maximum;
  return Object.entries(goals).every(([stage, item]) => grant.create_stage_ids.includes(stage)
    && item && typeof item === "object" && !Array.isArray(item) && Object.keys(item).sort().join(",") === "goal,request_id"
    && text(item.request_id, 200) && text(item.goal, 4000));
}
