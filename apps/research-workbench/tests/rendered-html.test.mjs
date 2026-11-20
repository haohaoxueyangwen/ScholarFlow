import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", {
      headers: { accept: "text/html" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}

test("server-renders the blank ScholarFlow page", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>ScholarFlow Research Workbench<\/title>/i);
  assert.match(html, /正在连接研究服务/);
  assert.doesNotMatch(html, /研究项目与执行|新建研究项目|Proposal Center/);
  assert.doesNotMatch(html, /Your site is taking shape|react-loading-skeleton/);
});

test("keeps the local-first API boundary explicit", async () => {
  const [page, api, layout, packageJson] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/lib/api.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
  ]);

  assert.match(api, /http:\/\/localhost:8765\/api/);
  assert.match(api, /NEXT_PUBLIC_SCHOLARFLOW_API_URL/);
  assert.match(api, /window\.location\.hostname/);
  assert.match(api, /X-ScholarFlow-Intent/);
  assert.match(api, /credentials:\s*"include"/);
  assert.match(page, /ReadingDesk/);
  assert.doesNotMatch(page, /CapabilityWorkspace|WritingWorkspace|LiteratureWorkspace|ProposalReviewCard/);
  assert.match(layout, /lang="zh-CN"/);
  assert.match(packageJson, /@scholarflow\/research-workbench/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);
});
