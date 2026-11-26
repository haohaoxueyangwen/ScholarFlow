import test from "node:test";
import assert from "node:assert/strict";
import {projectScope} from "../profiles/minimal/project-scope.js";

test("multiple or paginated matches cannot authorize stage-specific reads", () => {
  const scope = projectScope();
  scope.beginTurn("看项目");
  for (const result of [{total: 2, items: [{stage_id: "stage-a"}, {stage_id: "stage-b"}]}, {total: 2, items: [{stage_id: "stage-a"}]}, {total_matches: 2, count: 1, has_more: true, items: [{stage_id: "stage-a"}]}, {count: 1, items: [{stage_id: "stage-a"}]}]) {
    scope.searched(result);
    assert.throws(() => scope.assertRead("stage-a"), /Project scope unresolved/);
  }
});
test("unique result and user exact IDs work, but scope resets each turn", () => {
  const scope = projectScope();
  scope.beginTurn("看 stage-a，比较 stage-b。");
  scope.assertRead("stage-a");
  scope.assertRead("stage-b");
  assert.throws(() => scope.assertRead("stage-c"));
  scope.beginTurn("新任务");
  assert.throws(() => scope.assertRead("stage-a"));
  scope.searched({total_matches: 1, count: 1, has_more: false, items: [{stage_id: "stage-c"}]});
  scope.assertRead("stage-c");
  scope.beginTurn();
  assert.throws(() => scope.assertRead("stage-c"));
});
