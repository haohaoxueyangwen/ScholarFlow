type JsonObject = Record<string, unknown>;

export type ManuscriptPatchEvidence = {
  ref: string;
  label: string;
  status: string;
  resolved: boolean;
};

export type ManuscriptPatchProposal = {
  proposalId: string;
  status: string;
  section: string;
  beforeLatex: string;
  afterLatex: string;
  evidenceRefs: string[];
  reviewEvidence: ManuscriptPatchEvidence[];
  createdAt: string;
  expiresAt: string;
};

function object(value: unknown): JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as JsonObject
    : {};
}

function string(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export function matchingManuscriptPatchProposals(
  payload: unknown,
  manuscriptId: string,
  section: string,
): ManuscriptPatchProposal[] {
  const items = object(payload).items;
  if (!Array.isArray(items)) return [];

  return items.flatMap((value) => {
    const proposal = object(value);
    const target = object(proposal.target);
    if (
      string(proposal.operation) !== "manuscript.patch"
      || string(proposal.status) !== "pending"
      || string(target.manuscript_id) !== manuscriptId
      || string(target.section) !== section
    ) return [];

    const diff = object(proposal.diff);
    const sectionChange = object(diff.section);
    const beforeLatex = sectionChange.before;
    const afterLatex = sectionChange.after;
    if (typeof beforeLatex !== "string" || typeof afterLatex !== "string") return [];

    const evidenceChange = object(diff.evidence_refs);
    const evidenceRefs = Array.isArray(evidenceChange.after)
      ? evidenceChange.after.filter((ref): ref is string => typeof ref === "string" && ref.trim().length > 0)
      : [];
    const review = object(proposal.review);
    const reviewEvidence = Array.isArray(review.evidence)
      ? review.evidence.flatMap((item) => {
        const evidence = object(item);
        const ref = string(evidence.ref);
        if (!ref) return [];
        return [{
          ref,
          label: string(evidence.label) || ref,
          status: string(evidence.status),
          resolved: evidence.resolved === true,
        }];
      })
      : [];

    return [{
      proposalId: string(proposal.proposal_id),
      status: string(proposal.status),
      section,
      beforeLatex,
      afterLatex,
      evidenceRefs,
      reviewEvidence,
      createdAt: string(proposal.created_at),
      expiresAt: string(proposal.expires_at),
    }];
  }).filter((proposal) => proposal.proposalId.length > 0);
}

export function canApplyManuscriptPatch(
  proposal: ManuscriptPatchProposal,
  currentLatex: string | undefined,
): boolean {
  return proposal.status === "pending"
    && typeof currentLatex === "string"
    && proposal.beforeLatex === currentLatex
    && proposal.afterLatex.trim() !== proposal.beforeLatex.trim()
    && proposal.evidenceRefs.length > 0;
}

export function isCurrentManuscriptRequest(
  requestId: number,
  latestRequestId: number,
  requestedStageId: string,
  currentStageId: string,
): boolean {
  return requestId === latestRequestId && requestedStageId === currentStageId;
}
