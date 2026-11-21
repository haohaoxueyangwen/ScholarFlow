import test from "node:test";
import assert from "node:assert/strict";
import { natureSkillPaths } from "../harness/nature-skill.mjs";

test("Nature is opt-in and does not probe Python when absent", () => {
  assert.deepEqual(natureSkillPaths({env: {}, python: () => { throw Error("not called"); }}), []);
});

test("only a verified and activated skill is loaded", () => {
  const opts = {env: {SCHOLARFLOW_NATURE_HOME: "/tmp/agent"}, root: "/repo", python: () => "python"};
  assert.deepEqual(natureSkillPaths({...opts, spawn: () => ({status: 0, stdout: JSON.stringify({active: "v-test", skill_paths: ["/safe/SKILL.md"], resources: [{path: "/safe/SKILL.md", sha256: "a".repeat(64)}]})})}), ["/safe/SKILL.md"]);
  assert.throws(() => natureSkillPaths({...opts, spawn: () => ({status: 0, stdout: JSON.stringify({active: null, skill_paths: []})})}), /not installed/);
  assert.throws(() => natureSkillPaths({...opts, spawn: () => ({status: 1, stderr: "changed"})}), /validation failed/);
});
