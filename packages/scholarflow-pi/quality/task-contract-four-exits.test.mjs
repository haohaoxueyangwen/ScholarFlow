import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import {
  assertActiveToolsMatchSkillSurface,
  assertFixtureMatchesContract,
  executionGuardsForSkill,
  legalExitsForSkill,
  loadBootstrapOutcomeContract,
  loadFourExitsManifest,
  loadInformationDutiesContract,
  routeDirectiveForSkill,
  toolSurfaceForSkill,
} from "../extensions/task-contract.js";

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const fixtures = JSON.parse(
  readFileSync(resolve(packageRoot, "quality/skill-fixtures.json"), "utf8"),
).fixtures;

test("T4.5.1: bootstrap route text is generated from project-create-outcomes-v4", () => {
  const contract = loadBootstrapOutcomeContract();
  const text = routeDirectiveForSkill("project_bootstrap");
  for (const outcome of contract.outcomes) {
    assert.match(text, new RegExp(outcome.code));
  }
  assert.match(text, /project-create-outcomes-v4/);
  assert.match(text, /never an empty overview query/i);
  assert.match(text, /sf_project_get with one exact listed stage_id/i);
  assert.match(text, /evidence_refs must be only stored paper refs/i);
  assert.match(text, /unless they explicitly asked to reuse/i);
});

test("T4.5.1: project-context route text cites information-duties allowlist", () => {
  const duties = loadInformationDutiesContract();
  const text = routeDirectiveForSkill("project_context", { readOnly: true });
  assert.match(text, new RegExp(duties.contract_id));
  for (const tool of duties.project_status_allowlist) {
    assert.match(text, new RegExp(tool));
  }
});

test("T4.5.1b: project-context directives follow resolution mode", () => {
  const explicit = routeDirectiveForSkill("project_context", {
    readOnly: true,
    resolutionMode: "explicit_id",
  });
  assert.match(explicit, /sf_project_get/);
  assert.match(explicit, /do not run a fuzzy sf_project_search/i);

  const dual = routeDirectiveForSkill("project_context", {
    readOnly: false,
    resolutionMode: "dual_named",
  });
  assert.match(dual, /separate bounded sf_project_search/i);
  assert.doesNotMatch(dual, /Call sf_project_search exactly once with a broad query/);
});

test("T4.5.2: tool surfaces match manifest reads", () => {
  const manifest = loadFourExitsManifest();
  for (const skillId of Object.keys(manifest.skills)) {
    const reads = manifest.skills[skillId].tool_surface.reads;
    assert.deepEqual(toolSurfaceForSkill(skillId), reads);
  }
  assert.deepEqual(
    toolSurfaceForSkill("scholarflow-project-bootstrap", { includeProposals: true }).sort(),
    [
      "sf_lane_enter",
      "sf_paper_get",
      "sf_paper_search",
      "sf_project_get",
      "sf_project_propose_create",
      "sf_project_search",
      "sf_task_propose_create",
      "sf_task_search",
    ].sort(),
  );
});

test("T4.5.3: execution guard names are declared per skill", () => {
  assert.ok(executionGuardsForSkill("scholarflow-project-context").includes("guardProjectSearchCall"));
  assert.ok(executionGuardsForSkill("scholarflow-project-bootstrap").includes("allowedInLane"));
});

test("T4.5.4: v4 fixtures and legal exits align", () => {
  for (const fixture of fixtures.filter((item) => item.fixture_suite === "project-create-outcomes-v4")) {
    const check = assertFixtureMatchesContract(fixture);
    assert.equal(check.ok, true, check.reason);
    assert.ok(legalExitsForSkill("scholarflow-project-bootstrap").includes(fixture.contract_outcome));
  }
});

test("T4.5.4: project-context release fixtures use grounded_answer exit", () => {
  const followup = fixtures.find((item) => item.id === "multi-project-followup");
  assert.ok(followup);
  assert.equal(followup.contract_outcome, "grounded_answer");
  const check = assertFixtureMatchesContract(followup);
  assert.equal(check.ok, true, check.reason);
});

test("T4.5.2: read-only project-context bootstrap tools stay on contract surface", () => {
  const check = assertActiveToolsMatchSkillSurface(
    "scholarflow-project-context",
    ["sf_lane_enter", "sf_project_search", "sf_project_get"],
  );
  assert.equal(check.ok, true, check.reason);
});
