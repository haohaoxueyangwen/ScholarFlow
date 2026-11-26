import assert from "node:assert/strict";
import { resolve } from "node:path";
import test from "node:test";
import { offlineContractResourceArgs } from "../scripts/evaluation-resources.mjs";

test("offline portable contract evaluation explicitly loads project resources", () => {
  const evaluationCwd = resolve("synthetic-portable-checkout");
  const packageRoot = resolve(evaluationCwd, "packages/scholarflow-pi");
  let settingsPath;
  const args = offlineContractResourceArgs({ live: false, evaluationCwd, packageRoot, hasSettings: (path) => { settingsPath = path; return false; } });
  assert.equal(settingsPath, resolve(evaluationCwd, ".pi", "settings.json"));
  assert.ok(args.includes("--no-extensions"));
  assert.deepEqual(args.slice(-4), ["-e", resolve(packageRoot, "extensions/index.ts"), "--skill", resolve(packageRoot, "skills")]);
});

test("configured and live evaluations preserve their existing resource selection", () => {
  const common = { evaluationCwd: resolve("synthetic-checkout"), packageRoot: resolve("synthetic-package") };
  assert.deepEqual(offlineContractResourceArgs({ ...common, live: false, hasSettings: () => true }), []);
  assert.deepEqual(offlineContractResourceArgs({ ...common, live: true, hasSettings: () => { throw new Error("must not probe"); } }), []);
});
