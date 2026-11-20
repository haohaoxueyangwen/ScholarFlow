import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import {
  canApplyManuscriptPatch,
  isCurrentManuscriptRequest,
  matchingManuscriptPatchProposals,
} from "../app/lib/manuscript-workspace.ts";

const patchProposal = (overrides = {}) => ({
  proposal_id: "sha256:patch-1",
  operation: "manuscript.patch",
  status: "pending",
  target: { manuscript_id: "manuscript-alpha", section: "results" },
  diff: {
    section: { before: "Old result.", after: "New result." },
    evidence_refs: { before: [], after: ["paper:paper-1"] },
  },
  review: {
    evidence: [{ ref: "paper:paper-1", label: "Fixture paper", status: "active", resolved: true }],
  },
  ...overrides,
});

test("manuscript patch selection is scoped to pending proposal, manuscript, and section", () => {
  const proposals = [
    patchProposal(),
    patchProposal({ proposal_id: "sha256:other-section", target: { manuscript_id: "manuscript-alpha", section: "abstract" } }),
    patchProposal({ proposal_id: "sha256:other-manuscript", target: { manuscript_id: "manuscript-beta", section: "results" } }),
    patchProposal({ proposal_id: "sha256:applied", status: "applied" }),
    patchProposal({ proposal_id: "sha256:other-operation", operation: "research_artifact.publish" }),
  ];
  const selected = matchingManuscriptPatchProposals({ items: proposals }, "manuscript-alpha", "results");

  assert.equal(selected.length, 1);
  assert.equal(selected[0].proposalId, "sha256:patch-1");
  assert.equal(selected[0].beforeLatex, "Old result.");
  assert.equal(selected[0].afterLatex, "New result.");
  assert.deepEqual(selected[0].evidenceRefs, ["paper:paper-1"]);
  assert.equal(selected[0].reviewEvidence[0].resolved, true);
});

test("inline confirmation requires the reviewed section snapshot and typed evidence", () => {
  const [proposal] = matchingManuscriptPatchProposals({ items: [patchProposal()] }, "manuscript-alpha", "results");
  assert.equal(canApplyManuscriptPatch(proposal, "Old result."), true);
  assert.equal(canApplyManuscriptPatch(proposal, "Collaborator changed this."), false);
  assert.equal(canApplyManuscriptPatch({ ...proposal, evidenceRefs: [] }, "Old result."), false);
});

test("older section responses are rejected after a newer request or project switch", () => {
  assert.equal(isCurrentManuscriptRequest(4, 5, "stage-a", "stage-a"), false);
  assert.equal(isCurrentManuscriptRequest(5, 5, "stage-a", "stage-b"), false);
  assert.equal(isCurrentManuscriptRequest(5, 5, "stage-b", "stage-b"), true);
});

test("browser gate request callback runs in the CLI VM without URL or timers", async () => {
  const source = readFileSync(new URL("../../../scripts/workbench_browser_checks.js", import.meta.url), "utf8");
  const runChecks = vm.runInNewContext(source, { URL: undefined, setTimeout: undefined });
  const callbacks = new Map();
  const page = {
    on: (event, callback) => callbacks.set(event, callback),
    reload: async () => {
      callbacks.get("request")({ url: () => "http://127.0.0.1:13000/?next=/api/health" });
      callbacks.get("request")({ url: () => "http://127.0.0.1:18765/api/health" });
    },
    getByText: () => ({ waitFor: async () => {} }),
    getByRole: () => ({ count: async () => 1 }),
    locator: () => ({ evaluate: async () => { throw new Error("startup callback and API discovery passed"); } }),
  };
  await assert.rejects(runChecks(page), /startup callback and API discovery passed/);
});
