// Host authority only; a task ID is always bound to one exact stage.
export function validateTaskGrant(grant) {
  const text = value => typeof value === "string" && value.length > 0 && value.trim() === value && value.length <= 200;
  const map = value => !!value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).length <= 100
    && Object.entries(value).every(([task, stage]) => text(task) && text(stage));
  return map(grant.create_tasks) && map(grant.update_tasks)
    && Object.entries(grant.create_tasks).every(([task, stage]) => !(task in grant.update_tasks) || grant.update_tasks[task] === stage);
}
