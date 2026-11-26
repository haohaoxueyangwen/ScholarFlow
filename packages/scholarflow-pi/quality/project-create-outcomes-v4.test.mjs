import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { classifyScholarFlowPrompt } from "../extensions/routing.js";

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const contract = JSON.parse(
  readFileSync(resolve(packageRoot, "quality/project-create-outcomes-v4.json"), "utf8"),
);
const fixtures = JSON.parse(
  readFileSync(resolve(packageRoot, "quality/skill-fixtures.json"), "utf8"),
).fixtures;

test("project-create-outcomes-v4 contract enumerates four legal results", () => {
  assert.equal(contract.contract_id, "project-create-outcomes-v4");
  assert.equal(contract.fixture_suite, "project-create-outcomes-v4");
  assert.deepEqual(
    contract.outcomes.map((item) => item.code),
    ["create_proposal", "reuse_existing", "clarify", "evidence_gap"],
  );
});

test("v4 bootstrap fixtures are suite-tagged and isolated from the default set", () => {
  const suite = fixtures.filter((item) => item.fixture_suite === "project-create-outcomes-v4");
  assert.equal(suite.length, 5);
  assert.ok(fixtures.some((item) => item.id === "project-bootstrap" && !item.fixture_suite));
  for (const fixture of suite) {
    assert.equal(fixture.skill, "scholarflow-project-bootstrap");
    assert.ok(fixture.contract_outcome);
    assert.ok(contract.outcomes.some((item) => item.code === fixture.contract_outcome));
  }
});

test("v4 bootstrap prompts route into project bootstrap", () => {
  for (const fixture of fixtures.filter((item) => item.fixture_suite === "project-create-outcomes-v4")) {
    const route = classifyScholarFlowPrompt(fixture.prompt);
    assert.equal(route.projectBootstrapPrompt, true, fixture.id);
  }
});

test("create_proposal fixtures require propose_create; refusal fixtures forbid it", () => {
  for (const fixture of fixtures.filter((item) => item.fixture_suite === "project-create-outcomes-v4")) {
    const outcome = contract.outcomes.find((item) => item.code === fixture.contract_outcome);
    assert.ok(outcome, fixture.id);
    const forbidden = fixture.forbidden_tools || [];
    const required = fixture.live_required_all || [];
    if (outcome.propose_create === "required") {
      assert.ok(required.includes("sf_project_propose_create"), fixture.id);
      assert.equal(forbidden.includes("sf_project_propose_create"), false, fixture.id);
    } else {
      assert.ok(forbidden.includes("sf_project_propose_create"), fixture.id);
      assert.equal(required.includes("sf_project_propose_create"), false, fixture.id);
    }
  }
});
