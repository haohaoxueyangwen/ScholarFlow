import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

test("release verification rejects changed assets as well as stale source", () => {
  const root = mkdtempSync(join(tmpdir(), "sf-stamp-integrity-"));
  try {
    for (const dir of ["scripts", "dist/server", "dist/client/assets", ".openai"]) mkdirSync(join(root, dir), { recursive: true });
    copyFileSync(new URL("../scripts/build-stamp.mjs", import.meta.url), join(root, "scripts/build-stamp.mjs"));
    for (const file of ["package.json", "package-lock.json", "vite.config.ts", ".openai/hosting.json"]) writeFileSync(join(root, file), "{}");
    writeFileSync(join(root, "dist/server/index.js"), "server-original");
    writeFileSync(join(root, "dist/client/assets/app.js"), "client-original");
    const run = (...args) => spawnSync(process.execPath, ["scripts/build-stamp.mjs", ...args], { cwd: root, encoding: "utf8" });
    assert.equal(run().status, 0);
    const record = JSON.parse(readFileSync(join(root, "dist/workbench-build.json"), "utf8"));
    assert.match(record.output_fingerprint, /^[a-f0-9]{64}$/);
    assert.equal(run("--check").status, 0);
    writeFileSync(join(root, "dist/client/assets/app.js"), "changed-or-truncated");
    assert.notEqual(run("--check").status, 0);
    writeFileSync(join(root, "dist/client/assets/app.js"), "client-original");
    assert.equal(run("--check").status, 0);
    writeFileSync(join(root, "package.json"), '{"changed":true}');
    assert.notEqual(run("--check").status, 0);
  } finally { rmSync(root, { recursive: true, force: true }); }
});
