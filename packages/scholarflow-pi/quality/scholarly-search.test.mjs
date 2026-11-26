import assert from "node:assert/strict";
import test from "node:test";

import {
  normalizeOpenAlexWork,
  openAlexSearchUrl,
  reconstructAbstract,
  searchScholarlyWorks,
} from "../extensions/scholarly-search.js";

test("OpenAlex search URL is bounded to selected scholarly metadata", () => {
  const url = openAlexSearchUrl("agent memory", 99);
  assert.equal(url.origin, "https://api.openalex.org");
  assert.equal(url.pathname, "/works");
  assert.equal(url.searchParams.get("search"), "agent memory");
  assert.equal(url.searchParams.get("per_page"), "4");
  assert.match(url.searchParams.get("select"), /abstract_inverted_index/);
});

test("OpenAlex work normalization emits stable refs and reconstructed abstracts", () => {
  const item = normalizeOpenAlexWork({
    id: "https://openalex.org/W1",
    doi: "https://doi.org/10.1000/Example",
    title: "Durable Agent Memory",
    publication_year: 2026,
    type: "article",
    cited_by_count: 7,
    authorships: [{ author: { display_name: "A. Researcher" } }],
    primary_location: { landing_page_url: "https://example.test/paper", source: { display_name: "Journal" } },
    abstract_inverted_index: { Durable: [0], memory: [2], agent: [1] },
  });
  assert.equal(item.ref, "doi:10.1000/Example");
  assert.equal(item.abstract, "Durable agent memory");
  assert.deepEqual(item.authors, ["A. Researcher"]);
  assert.equal(item.source_type, "unknown");
  assert.equal(reconstructAbstract(null), "");
});

test("scholarly search deduplicates records while preserving matched queries", async () => {
  const calls = [];
  const fetchImpl = async (url) => {
    calls.push(String(url));
    return new Response(JSON.stringify({
      results: [{
        id: "https://openalex.org/W1",
        doi: "https://doi.org/10.1000/one",
        title: "One",
        authorships: [],
      }],
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  const result = await searchScholarlyWorks({ queries: ["memory", "context"], fetchImpl });
  assert.equal(calls.length, 2);
  assert.equal(result.result_count, 1);
  assert.deepEqual(result.items[0].matched_queries, ["memory", "context"]);
  assert.equal(result.read_only, true);
});

test("scholarly search retries a bounded OpenAlex 429 without parallel query bursts", async () => {
  let active = 0;
  let maxActive = 0;
  let calls = 0;
  const fetchImpl = async () => {
    calls += 1;
    active += 1;
    maxActive = Math.max(maxActive, active);
    active -= 1;
    if (calls === 1) {
      return new Response("rate limited", { status: 429, headers: { "Retry-After": "0" } });
    }
    return new Response(JSON.stringify({ results: [] }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  const result = await searchScholarlyWorks({ queries: ["one", "two"], fetchImpl });
  assert.equal(calls, 3);
  assert.equal(maxActive, 1);
  assert.equal(result.query_errors.length, 0);
});
