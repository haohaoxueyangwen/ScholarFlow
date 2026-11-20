import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import Markdown from "react-markdown";
import { markdownOptions } from "../app/lib/markdown.ts";
import { conversationItems, mergeMessages } from "../app/lib/conversation.ts";
import { apiGet } from "../app/lib/api.ts";
import { normalizeRects } from "../app/lib/pdf-anchors.ts";
import { readerPageWindow } from "../app/lib/reader-window.ts";
import { statusLabel } from "../app/lib/presentation.ts";
import { PROSE_DRAFT_KEY, readProseDrafts, saveProseDraft } from "../app/lib/draft-storage.ts";

test("draft writes detect another window and preserve unrelated chapters", () => {
  const data = new Map();
  const storage = { getItem: (key) => data.get(key) ?? null, setItem: (key, value) => data.set(key, value) };
  assert.equal(saveProseDraft(storage, "a", "first", "").saved, true);
  assert.equal(saveProseDraft(storage, "b", "other chapter", "").saved, true);
  assert.deepEqual(saveProseDraft(storage, "a", "stale window", ""), { saved: false, current: "first" });
  assert.deepEqual(readProseDrafts(storage.getItem(PROSE_DRAFT_KEY)), { a: "first", b: "other chapter" });
  assert.equal(saveProseDraft(storage, "a", "resolved", "first").saved, true);
});

test("reader handoff explicitly preserves its target session", () => {
  const handoff = buildReaderHandoff({ title: "Paper", sourceRef: "paper:qa", page: 1, quote: "text", note: "", projectIds: ["p1"], sessionId: "session-target" });
  assert.equal(takeReaderHandoffs(JSON.stringify([handoff])).items[0].sessionId, "session-target");
});

test("expired proposals have a distinct historical label", () => {
  assert.equal(statusLabel("expired"), "已过期");
  assert.notEqual(statusLabel("expired"), statusLabel("rejected"));
});
test("reader handoff carries project scope without guessing a target session", () => {
  const handoff = buildReaderHandoff({ title: "Fixture", sourceRef: "paper:qa", page: 2, quote: "text", note: "", projectIds: ["stage-a"] });
  assert.deepEqual(handoff.projectIds, ["stage-a"]);
  assert.equal(handoff.sessionId, undefined);
  assert.deepEqual(buildReaderHandoff({ title: "Unknown", sourceRef: "paper:qa", page: 1, quote: "", note: "" }).projectIds, []);
});
import { buildReaderHandoff, takeReaderHandoffs } from "../app/lib/reader-handoff.ts";

test("reader handoff preserves selected provenance without invoking Pi", () => {
  const request = buildReaderHandoff({ title: "Paper", sourceRef: "local_pdf:pdf_abcdef", page: 4, quote: "Evidence excerpt", note: "My question" });
  const parsed = takeReaderHandoffs(JSON.stringify([request]));
  assert.equal(parsed.items[0].sourceRef, "local_pdf:pdf_abcdef");
  assert.match(parsed.items[0].prompt, /page 4|第 4 页/);
  assert.match(parsed.items[0].prompt, /把个人批注视为我的笔记/);
  assert.deepEqual(takeReaderHandoffs("not-json"), { items: [], malformed: true });
});

test("long PDF canvas budget stays bounded and follows page navigation", () => {
  assert.deepEqual(readerPageWindow(1, 700), [1, 2, 3]);
  assert.deepEqual(readerPageWindow(350, 700), [348, 349, 350, 351, 352]);
  assert.deepEqual(readerPageWindow(700, 700), [698, 699, 700]);
  assert.equal(readerPageWindow(350, 700).length < 10, true);
});

test("PDF selection geometry survives scale and deduplicates nested spans", () => {
  const rectangles = [{ left: 20, top: 40, width: 80, height: 12 }, { left: 20, top: 40, width: 80, height: 12 }, { left: 20, top: 55, width: 60, height: 12 }];
  const normalized = normalizeRects(rectangles, { left: 0, top: 0, width: 200, height: 400 });
  const scaled = normalizeRects(rectangles.map((rect) => Object.fromEntries(Object.entries(rect).map(([key, value]) => [key, value * 2]))), { left: 0, top: 0, width: 400, height: 800 });
  assert.equal(normalized.length, 2);
  assert.deepEqual(normalized, scaled);
});

test("streaming whitespace and messages are lossless beyond 1000 deltas", () => {
  const chunks = ["Hello", " ", "world\n", ...Array(1100).fill("a b "), "\nEnd"];
  const events = chunks.map((delta, index) => ({ type: "message_update", sequence: index + 1, assistantMessageEvent: { type: "text_delta", delta } }));
  const messages = conversationItems([...events, events[0], { type: "agent_settled", sequence: 2000 }]);
  assert.equal(messages.length, 1);
  assert.equal(messages[0].text, chunks.join(""));
  assert.equal(messages[0].pending, false);
});

test("complete Pi message replaces partial text without duplication", () => {
  const messages = conversationItems([{ type: "message_update", sequence: 1, assistantMessageEvent: { type: "text_delta", delta: "Half" } }, { type: "message_end", sequence: 2, message: { role: "assistant", content: [{ type: "text", text: "Complete answer" }] } }]);
  assert.equal(messages.length, 1);
  assert.equal(messages[0].text, "Complete answer");
});

test("history pages deduplicate stable IDs and keep chronological order", () => {
  const result = mergeMessages([{ id: "a", sequence: 5, text: "partial" }], [{ id: "a", sequence: 5, text: "complete" }, { id: "u", sequence: 1, text: "question" }]);
  assert.deepEqual(result.map((item) => item.text), ["question", "complete"]);
});

test("Pi message identity stays isolated across sessions with overlapping sequences", () => {
  const event = (session_id, message) => ({ session_id, sequence: 1, type: "workbench_user_message", message });
  const globalMessage = conversationItems([event("global-session", "Global question")])[0];
  const taskMessage = conversationItems([event("task-session", "Task question")])[0];
  const merged = mergeMessages([globalMessage], [taskMessage]);
  assert.deepEqual(merged.map((item) => item.text), ["Global question", "Task question"]);
  assert.notEqual(merged[0].id, merged[1].id);
});

test("safe markdown supports tables/code and blocks active content", () => {
  const source = "| Field | Value |\n| --- | --- |\n| DOI | 10.1/test |\n\n```js\nconst x = 1;\n```\n\n[unsafe](javascript:alert%281%29)\n\n<script>alert(1)</script>\n\n![track](https://example.com/track.png)";
  const html = renderToStaticMarkup(createElement(Markdown, markdownOptions, source));
  assert.match(html, /<table>/);
  assert.match(html, /<pre><code/);
  assert.doesNotMatch(html, /javascript:|<script|<img|track\.png/);
});

test("API requests are bounded and preserve caller cancellation", async (t) => {
  t.mock.method(globalThis, "fetch", (_url, init) => new Promise((_resolve, reject) => {
    if (init.signal.aborted) reject(init.signal.reason);
    else init.signal.addEventListener("abort", () => reject(init.signal.reason), { once: true });
  }));
  await assert.rejects(apiGet("/slow", { timeoutMs: 10 }), /请求超时/);
  const controller = new AbortController();
  controller.abort(new Error("superseded"));
  await assert.rejects(apiGet("/cancelled", { signal: controller.signal }), /superseded/);
});
