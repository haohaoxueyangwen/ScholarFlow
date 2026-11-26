import assert from "node:assert/strict";
import test from "node:test";
import { FIXTURE, fixturePreservationChecks } from "../../../scripts/pi_native_compaction_smoke.mjs";

const preserved = [FIXTURE.evidenceId, FIXTURE.evidenceRef,
  "SYN-DECISION-021 (report only the stated fixture observation; never claim external validation)",
  `${FIXTURE.pendingTaskId} (${FIXTURE.pendingTaskText})`].join("\n");

test("native compaction fixture accepts punctuation changes in preserved records", () => {
  assert.ok(Object.values(fixturePreservationChecks(preserved)).every(Boolean));
});
test("native compaction fixture rejects lost decision and no-action boundaries", () => {
  assert.equal(fixturePreservationChecks(preserved.replace("never claim external validation", "claim external validation")).decisionBoundary, false);
  assert.equal(fixturePreservationChecks(preserved.replace("SYN-DECISION-021", "WRONG-DECISION")).decisionBoundary, false);
  assert.equal(fixturePreservationChecks(preserved.replace("do not run tools or take external action", "run external tools")).pendingTaskNoActionBoundary, false);
});
test("native compaction fixture still requires the exact evidence reference", () => {
  assert.equal(fixturePreservationChecks(preserved.replace(FIXTURE.evidenceRef, "synthetic://different-source")).evidenceRef, false);
});
