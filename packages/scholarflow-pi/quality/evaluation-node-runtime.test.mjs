import assert from "node:assert/strict";
import test from "node:test";
import { assertNodeRuntimeAtLeast } from "../scripts/assert-node-runtime.mjs";

test("evaluation rejects the observed unsupported Node 22.17 runtime", () => {
  assert.throws(() => assertNodeRuntimeAtLeast("v22.17.0", "22.19.0", "controller"), /controller requires Node.js >= 22.19.0/);
  assert.throws(() => assertNodeRuntimeAtLeast("22.18.9", "22.19.0", "Pi child"), /Pi child requires/);
});

test("evaluation accepts the policy minimum and newer stable runtimes", () => {
  for (const version of ["22.19.0", "v22.19.1", "24.0.0"]) {
    assert.doesNotThrow(() => assertNodeRuntimeAtLeast(version, "22.19.0"));
  }
});

test("evaluation rejects unknown version output without echoing it", () => {
  assert.throws(() => assertNodeRuntimeAtLeast("untrusted-output-value", "22.19.0"), (error) => !error.message.includes("untrusted-output-value"));
});
