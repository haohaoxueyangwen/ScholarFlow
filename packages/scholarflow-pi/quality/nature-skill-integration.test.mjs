import test from "node:test";
import assert from "node:assert/strict";
import { cpSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { DefaultResourceLoader, SettingsManager } from "@earendil-works/pi-coding-agent";
import { loadExtensions } from "../node_modules/@earendil-works/pi-coding-agent/dist/core/extensions/loader.js";
import { resolveComposition } from "../harness/composition.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const profile = resolve(root, "packages/scholarflow-pi/profiles/minimal");
const configuredHome = process.env.SCHOLARFLOW_NATURE_HOME;

test("native Pi loads pinned Nature and only its unchanged verified resources", {skip: !configuredHome}, async () => {
  const temp = mkdtempSync(resolve(tmpdir(), "scholarflow-nature-test-"));
  const previous = process.env.SCHOLARFLOW_NATURE_HOME;
  try {
    cpSync(configuredHome, temp, {recursive: true});
    process.env.SCHOLARFLOW_NATURE_HOME = temp;
    const composition = resolveComposition();
    const loader = new DefaultResourceLoader({cwd: profile, agentDir: temp, settingsManager: SettingsManager.inMemory({packages: []}),
      noExtensions: true, noSkills: true, noContextFiles: true, noPromptTemplates: true, noThemes: true,
      additionalSkillPaths: composition.skillPaths});
    await loader.reload();
    assert.ok(loader.getSkills().skills.some((item) => item.name === "nature-writing"));
    assert.deepEqual(loader.getSkills().diagnostics.filter((item) => item.type === "error"), []);
    const loaded = await loadExtensions([resolve(profile, "extension.ts")], profile);
    assert.deepEqual(loaded.errors, []);
    const guard = loaded.extensions[0].handlers.get("tool_call")[0];
    const resource = composition.natureResources.find((item) => item.path.endsWith("manifest.yaml") && item.path.includes("nature-writing"));
    assert.ok(resource);
    const call = {toolName: "read", input: {path: resource.path}};
    assert.equal(await guard(call, {cwd: profile}), undefined);
    assert.equal((await guard({toolName: "read", input: {path: resolve(root, ".pi/settings.json")}}, {cwd: profile})).block, true);
    writeFileSync(resource.path, "tampered test resource");
    assert.equal((await guard(call, {cwd: profile})).block, true);
  } finally {
    if (previous === undefined) delete process.env.SCHOLARFLOW_NATURE_HOME; else process.env.SCHOLARFLOW_NATURE_HOME = previous;
    rmSync(temp, {recursive: true, force: true});
  }
});
