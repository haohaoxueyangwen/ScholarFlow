import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { copyFileSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { canAskKnowledge, draftKey, isSameScope, previewMatches, readDraft, readReviewDraft, researchRequest, turnStateText, uniquePdfSources, writeDraft, writeReviewDraft } from "../app/lib/research-desk.ts";

test("drafts are isolated by both project and workspace and survive remounts", () => {
  const values = new Map();
  const storage = { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key) };
  assert.notEqual(draftKey("project-a", "workspace-a"), draftKey("project-b", "workspace-a"));
  assert.notEqual(draftKey("project-a", "workspace-a"), draftKey("project-a", "workspace-b"));
  assert.equal(writeDraft(storage, "project-a", "workspace-a", "unfinished"), true);
  assert.deepEqual(readDraft(storage, "project-a", "workspace-a"), { value: "unfinished", available: true });
  assert.equal(readDraft(storage, "project-b", "workspace-a").value, "");
  assert.equal(writeDraft(storage, "project-a", "workspace-a", ""), true);
  assert.equal(readDraft(storage, "project-a", "workspace-a").value, "");
});

test("review draft recovery is bound to project, workspace and exact proposal version", () => {
  const values = new Map();
  const storage = { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key) };
  const draft = { proposalId: "p1", proposalHash: "hash1", title: "Edited", content: "Narrow claim", requestId: "revision-" + "a".repeat(32), submitted: true };
  assert.equal(writeReviewDraft(storage, "a", "w", draft, "p1", "hash1"), true);
  assert.deepEqual(readReviewDraft(storage, "a", "w", "p1", "hash1"), draft);
  assert.equal(readReviewDraft(storage, "b", "w", "p1", "hash1"), null);
  assert.equal(readReviewDraft(storage, "a", "other", "p1", "hash1"), null);
  assert.equal(readReviewDraft(storage, "a", "w", "p1", "hash2"), null);
});

test("project preview becomes invalid when any reviewed field changes", () => {
  const preview = { changes: { project_name: "a", title: "b", goal: "c" } };
  assert.equal(previewMatches(preview, "a", "b", "c"), true);
  assert.equal(previewMatches(preview, "a", "b", "changed"), false);
  assert.equal(previewMatches(null, "a", "b", "c"), false);
});

test("unavailable browser storage is reported instead of pretending to save", () => {
  const storage = { getItem() { throw Error("blocked"); }, setItem() { throw Error("blocked"); }, removeItem() { throw Error("blocked"); } };
  assert.deepEqual(readDraft(storage, "a", "b"), { value: "", available: false });
  assert.equal(writeDraft(storage, "a", "b", "unsaved"), false);
});

test("late responses cannot replace a different project or workspace", () => {
  assert.equal(isSameScope({ stage: "a", id: "one", generation: 2 }, { stage: "a", id: "one", generation: 2 }), true);
  assert.equal(isSameScope({ stage: "b", id: "one", generation: 3 }, { stage: "a", id: "one", generation: 2 }), false);
  assert.equal(isSameScope({ stage: "a", id: "one", generation: 3 }, { stage: "a", id: "one", generation: 2 }), false);
});

test("project knowledge is usable independently of the current note and interruption is explicit", () => {
  assert.equal(canAskKnowledge({ knowledge_available: true, knowledge: { status: "not_queued" } }), true);
  assert.equal(canAskKnowledge({ knowledge_available: false, knowledge: { status: "indexed" } }), false);
  assert.match(turnStateText("interrupted"), /中断/);
  assert.notEqual(turnStateText("interrupted"), turnStateText("running"));
});

test("source links show each valid PDF page once", () => {
  const id = "pdf_" + "a".repeat(24);
  const page = (value) => ({ import_id: id, anchor: { page_start: value } });
  assert.deepEqual(uniquePdfSources([page(1), page(1), page(2), { import_id: "invalid", anchor: { page_start: 1 } }, page(0)]), [page(1), page(2)]);
});

test("requests establish a credentialed local session and require intent for writes", async () => {
  const calls = [];
  const fakeFetch = async (url, options) => { calls.push({ url, options }); return new Response("{}", { status: 200 }); };
  await researchRequest("http://127.0.0.1:8787", "/projects/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }, fakeFetch);
  assert.equal(calls[0].url, "http://127.0.0.1:8787/session");
  assert.equal(calls[0].options.credentials, "include");
  assert.equal(calls[1].options.credentials, "include");
  assert.equal(new Headers(calls[1].options.headers).get("X-ScholarFlow-Intent"), "confirm");
});

test("research requests cannot be pointed at an external host", async () => {
  await assert.rejects(() => researchRequest("http://localhost.attacker.example:8787", "/projects", {}, async () => new Response()), /loopback/i);
  await assert.rejects(() => researchRequest("http://127.0.0.1:8787", "//attacker.example/", {}, async () => new Response()), /path/i);
});

test("build fingerprint changes when research API endpoint changes", () => {
  const root = mkdtempSync(join(tmpdir(), "sf-build-stamp-test-"));
  try {
    for (const dir of ["scripts", "dist", ".openai"]) mkdirSync(join(root, dir));
    copyFileSync(new URL("../scripts/build-stamp.mjs", import.meta.url), join(root, "scripts/build-stamp.mjs"));
    for (const file of ["package.json", "package-lock.json", "vite.config.ts", ".openai/hosting.json"]) writeFileSync(join(root, file), "{}");
    const run = (port) => spawnSync(process.execPath, ["scripts/build-stamp.mjs", "--fingerprint"], {
      cwd: root, encoding: "utf8", env: { ...process.env, NEXT_PUBLIC_SCHOLARFLOW_RESEARCH_URL: `http://127.0.0.1:${port}` },
    });
    const first = run(18787), second = run(18788);
    assert.equal(first.status, 0, first.stderr);
    assert.equal(second.status, 0, second.stderr);
    assert.match(first.stdout.trim(), /^[a-f0-9]{64}$/);
    assert.notEqual(first.stdout.trim(), second.stdout.trim());
  } finally { rmSync(root, { recursive: true, force: true }); }
});
