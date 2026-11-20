// Host authority validation; no model-created permission or fuzzy project match.
export function validateNoteGrant(grant) {
  const id = (value, prefix) => typeof value === "string" && value.trim() === value && value.length <= 200
    && value.startsWith(prefix) && value.length > prefix.length;
  const map = value => !!value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).length <= 100
    && Object.entries(value).every(([note, stage]) => id(note, "artifact-") && id(stage, "stage-"));
  const sources = value => !!value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).length <= 100
    && Object.entries(value).every(([stage, papers]) => id(stage, "stage-") && Array.isArray(papers) && papers.length >= 1 && papers.length <= 8
      && new Set(papers).size === papers.length && papers.every(p => id(p, "paper-")));
  return map(grant.create_notes) && map(grant.update_notes) && sources(grant.source_papers)
    && Object.entries(grant.create_notes).every(([note, stage]) => !(note in grant.update_notes) || grant.update_notes[note] === stage);
}
