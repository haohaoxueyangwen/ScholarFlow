import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFile } from "node:fs/promises";
import { resolve, sep } from "node:path";
import test from "node:test";
import { createLocalServer } from "../scripts/serve-local.mjs";

const candidate = process.env.SCHOLARFLOW_CANDIDATE_BUILD_ROOT;
const repoRoot = resolve(import.meta.dirname, "../../..");

test("isolated candidate matches current source and serves every referenced JS/CSS", { skip: !candidate }, async () => {
  const root = resolve(candidate);
  assert.ok(root.startsWith(resolve(repoRoot, ".tmp/research-desk-builds") + sep), "Candidate must be an isolated build directory");
  const record = JSON.parse(await readFile(resolve(root, "dist/workbench-build.json"), "utf8"));
  const stamp = spawnSync(process.execPath, [resolve(import.meta.dirname, "../scripts/build-stamp.mjs"), "--fingerprint"], { encoding: "utf8", env: process.env });
  assert.equal(stamp.status, 0, stamp.stderr);
  assert.equal(record.fingerprint, stamp.stdout.trim(), "Candidate is stale");
  const integrity = spawnSync(process.execPath, [resolve(root, "scripts/build-stamp.mjs"), "--check"], { encoding: "utf8", env: process.env });
  assert.equal(integrity.status, 0, "Candidate outputs no longer match their build receipt");

  const server = await createLocalServer({ root, instanceId: "release-test", buildId: record.fingerprint });
  await new Promise((done) => server.listen(0, "127.0.0.1", done));
  try {
    const base = `http://127.0.0.1:${server.address().port}`;
    const health = await (await fetch(`${base}/__scholarflow_health`)).json();
    assert.equal(health.instance_id, "release-test");
    assert.equal(health.build_id, record.fingerprint);
    for (const path of ["/", "/desk"]) {
      const response = await fetch(base + path);
      assert.equal(response.status, 200, path);
      const html = await response.text();
      assert.match(html, /正在连接研究服务/);
      const urls = [...html.matchAll(/(?:src|href)=["']([^"']+\.(?:css|js)(?:\?[^"']*)?)["']/g)].map((item) => item[1]).filter((item) => item.startsWith("/"));
      assert.ok(urls.length > 0, `${path} has no JS/CSS assets`);
      for (const url of urls) {
        const asset = await fetch(new URL(url, base));
        assert.equal(asset.status, 200, url);
        assert.match(asset.headers.get("content-type") || "", /javascript|css/, url);
      }
    }
  } finally {
    await new Promise((done) => server.close(done));
    server.closeAllConnections();
  }
});
