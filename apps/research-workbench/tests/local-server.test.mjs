import assert from "node:assert/strict";
import test from "node:test";
import { readdir, mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { request as httpRequest } from "node:http";
import { createLocalServer, staticResponse } from "../scripts/serve-local.mjs";

test("running server pins HTML and assets even when dist is replaced", async () => {
  const root = await mkdtemp(join(tmpdir(), "sf-release-test-"));
  let server;
  try {
    await mkdir(join(root, "dist/server"), { recursive: true });
    await mkdir(join(root, "dist/client/assets"), { recursive: true });
    await writeFile(join(root, "dist/server/index.js"), "export default {fetch(){return new Response('<script src=/assets/old.js></script>')}}");
    await writeFile(join(root, "dist/client/assets/old.js"), "old build");
    server = await createLocalServer({ root });
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    await rm(join(root, "dist"), { recursive: true });
    const base = `http://127.0.0.1:${server.address().port}`;
    assert.match(await (await fetch(base)).text(), /old.js/);
    assert.equal(await (await fetch(base + "/assets/old.js")).text(), "old build");
  } finally {
    if (server) await new Promise(resolve => server.close(resolve));
    await rm(root, { recursive: true, force: true });
  }
});

test("local server exposes the exact candidate identity without accepting a forged host", async () => {
  const root = await mkdtemp(join(tmpdir(), "sf-health-test-"));
  let server;
  try {
    await mkdir(join(root, "dist/server"), { recursive: true });
    await mkdir(join(root, "dist/client"), { recursive: true });
    await writeFile(join(root, "dist/server/index.js"), "export default {fetch(){return new Response('candidate')}}");
    server = await createLocalServer({ root, instanceId: "instance-123", buildId: "build-456" });
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    const base = `http://127.0.0.1:${server.address().port}`;
    const response = await fetch(base + "/__scholarflow_health");
    assert.deepEqual(await response.json(), { status: "ok", service: "scholarflow-research-web", instance_id: "instance-123", build_id: "build-456" });
    const forgedStatus = await new Promise((resolve, reject) => {
      const request = httpRequest({ host: "127.0.0.1", port: server.address().port, path: "/__scholarflow_health", headers: { Host: "attacker.example" } }, (response) => { response.resume(); resolve(response.statusCode); });
      request.on("error", reject); request.end();
    });
    assert.equal(forgedStatus, 403);
  } finally {
    if (server) await new Promise(resolve => server.close(resolve));
    await rm(root, { recursive: true, force: true });
  }
});

test("production adapter serves HTML, CSS, JS and PDF worker on Windows", async () => {
  const server = await createLocalServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const response = await fetch(base);
    assert.equal(response.status, 200);
    const html = await response.text();
    assert.match(html, /ScholarFlow/);
    const urls = [...html.matchAll(/(?:src|href)="([^"]+\.(?:css|js)(?:\?[^"]*)?)"/g)].map((match) => match[1]).filter((path) => path.startsWith("/"));
    assert.ok(urls.length > 0);
    for (const path of urls) assert.equal((await fetch(new URL(path, base))).status, 200, path);
    const files = await readdir(new URL("../dist/client/assets/", import.meta.url));
    const worker = files.find((path) => path.startsWith("pdf.worker") && path.endsWith(".mjs"));
    assert.ok(worker, "PDF worker is emitted");
    const workerResponse = await fetch(`${base}/assets/${worker}`);
    assert.equal(workerResponse.status, 200);
    assert.match(workerResponse.headers.get("content-type"), /javascript/);
    assert.equal((await fetch(base, { method: "POST" })).status, 405);
  } finally {
    await new Promise((resolve) => server.close(resolve));
    server.closeAllConnections();
  }
});

test("static serving rejects hidden and Windows traversal paths", async () => {
  assert.equal(await staticResponse(new Request("http://localhost/.rsc")), null);
  for (const path of ["/.env", "/assets/%5c..%5cpackage.json"]) {
    assert.equal((await staticResponse(new Request(`http://localhost${path}`))).status, 403);
  }
});
