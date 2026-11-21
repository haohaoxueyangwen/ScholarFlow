import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { deepseekApiKey } from "../harness/provider-auth.mjs";

test("DeepSeek supports environment, explicit Pi file and existing Pi login", () => {
  const temp = mkdtempSync(resolve(tmpdir(), "sf-provider-test-"));
  try {
    const legacy = resolve(temp, ".pi/agent"); mkdirSync(legacy, {recursive: true});
    writeFileSync(resolve(legacy, "auth.json"), JSON.stringify({deepseek: {type: "api_key", key: "synthetic-legacy"}}));
    const explicit = resolve(temp, "local.json"); writeFileSync(explicit, JSON.stringify({deepseek: {type: "api_key", key: "synthetic-local"}}));
    assert.equal(deepseekApiKey({env: {}, home: temp}), "synthetic-legacy");
    assert.equal(deepseekApiKey({env: {SCHOLARFLOW_PI_AUTH_FILE: explicit}, home: temp}), "synthetic-local");
    assert.equal(deepseekApiKey({env: {DEEPSEEK_API_KEY: "synthetic-env", SCHOLARFLOW_PI_AUTH_FILE: explicit}, home: temp}), "synthetic-env");
    assert.throws(() => deepseekApiKey({env: {SCHOLARFLOW_PI_AUTH_FILE: resolve(temp, "missing")}, home: temp}), /not configured/);
  } finally { rmSync(temp, {recursive: true, force: true}); }
});
