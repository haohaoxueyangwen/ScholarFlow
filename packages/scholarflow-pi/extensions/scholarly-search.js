const OPENALEX_WORKS_ENDPOINT = "https://api.openalex.org/works";
const MAX_QUERY_COUNT = 4;
const MAX_RESULTS_PER_QUERY = 4;
const MAX_REQUEST_ATTEMPTS = 3;

export async function searchScholarlyWorks({ queries, maxResultsPerQuery = 2, signal, fetchImpl = fetch }) {
  const normalizedQueries = normalizeQueries(queries);
  const perPage = boundedInteger(maxResultsPerQuery, 1, MAX_RESULTS_PER_QUERY, 2);
  const batches = [];
  // Workers already run in parallel. Keep queries within one worker
  // sequential so 2-4 facets do not multiply into an anonymous API burst.
  for (const query of normalizedQueries) {
    try {
      const url = openAlexSearchUrl(query, perPage);
      const response = await fetchOpenAlex(url, { query, signal, fetchImpl });
      const payload = await response.json();
      const items = Array.isArray(payload?.results)
        ? payload.results.map(normalizeOpenAlexWork).filter(Boolean)
        : [];
      batches.push({ query, items, error: null });
    } catch (error) {
      if (signal?.aborted) throw error;
      batches.push({
        query,
        items: [],
        error: boundedString(error instanceof Error ? error.message : String(error), 500),
      });
    }
  }
  const merged = new Map();
  for (const batch of batches) {
    for (const item of batch.items) {
      const existing = merged.get(item.ref);
      if (existing) existing.matched_queries = [...new Set([...existing.matched_queries, batch.query])];
      else merged.set(item.ref, { ...item, matched_queries: [batch.query] });
    }
  }
  const items = [...merged.values()];
  return {
    schema_version: 1,
    provider: "openalex",
    read_only: true,
    query_count: normalizedQueries.length,
    result_count: items.length,
    query_errors: batches.filter((batch) => batch.error).map((batch) => ({ query: batch.query, error: batch.error })),
    items,
  };
}

async function fetchOpenAlex(url, { query, signal, fetchImpl }) {
  for (let attempt = 0; attempt < MAX_REQUEST_ATTEMPTS; attempt += 1) {
    const response = await fetchImpl(url, {
      signal,
      headers: { Accept: "application/json", "User-Agent": "ScholarFlow/0.1 research-worker" },
    });
    if (response.ok) return response;
    const retryable = response.status === 429 || response.status >= 500;
    if (!retryable || attempt === MAX_REQUEST_ATTEMPTS - 1) {
      throw new Error(`OpenAlex returned HTTP ${response.status}`);
    }
    await abortableDelay(retryDelayMs(response, query, attempt), signal);
  }
  throw new Error("OpenAlex retry budget exhausted");
}

function retryDelayMs(response, query, attempt) {
  const retryAfter = Number.parseFloat(response.headers?.get?.("retry-after") ?? "");
  if (Number.isFinite(retryAfter) && retryAfter >= 0) return Math.min(5_000, retryAfter * 1_000);
  const jitter = [...String(query)].reduce((total, character) => total + character.charCodeAt(0), 0) % 251;
  return Math.min(5_000, 500 * (2 ** attempt) + jitter);
}

function abortableDelay(milliseconds, signal) {
  if (signal?.aborted) return Promise.reject(signal.reason ?? new Error("OpenAlex request aborted"));
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      clearTimeout(timeout);
      signal?.removeEventListener("abort", onAbort);
      reject(signal.reason ?? new Error("OpenAlex request aborted"));
    };
    const timeout = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, milliseconds);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export function openAlexSearchUrl(query, perPage = 2) {
  const url = new URL(OPENALEX_WORKS_ENDPOINT);
  url.searchParams.set("search", boundedString(query, 400));
  url.searchParams.set("per_page", String(boundedInteger(perPage, 1, MAX_RESULTS_PER_QUERY, 2)));
  url.searchParams.set(
    "select",
    "id,doi,title,publication_year,type,primary_location,authorships,abstract_inverted_index,cited_by_count",
  );
  const apiKey = String(process.env.OPENALEX_API_KEY ?? "").trim();
  if (apiKey) url.searchParams.set("api_key", apiKey);
  return url;
}

export function normalizeOpenAlexWork(value) {
  if (!value || typeof value !== "object") return null;
  const openAlexId = boundedString(value.id, 300);
  const doi = boundedString(value.doi, 300).replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, "");
  const ref = doi ? `doi:${doi}` : openAlexId;
  const title = boundedString(value.title, 600);
  if (!ref || !title) return null;
  const landingPage = boundedString(value.primary_location?.landing_page_url, 1_000);
  const authors = Array.isArray(value.authorships)
    ? value.authorships.map((item) => boundedString(item?.author?.display_name, 160)).filter(Boolean).slice(0, 8)
    : [];
  return {
    ref,
    title,
    url: doi ? `https://doi.org/${doi}` : landingPage || openAlexId,
    ...(doi ? { doi } : {}),
    openalex_id: openAlexId,
    year: Number.isInteger(value.publication_year) ? value.publication_year : null,
    work_type: boundedString(value.type, 80) || "unknown",
    venue: boundedString(value.primary_location?.source?.display_name, 240),
    authors,
    cited_by_count: Number.isFinite(Number(value.cited_by_count)) ? Number(value.cited_by_count) : 0,
    abstract: reconstructAbstract(value.abstract_inverted_index),
    // Bibliographic metadata alone cannot distinguish original research from
    // reviews or other secondary evidence. A worker may classify it only
    // after inspecting the title/abstract or a fetched source.
    source_type: "unknown",
  };
}

export function reconstructAbstract(invertedIndex) {
  if (!invertedIndex || typeof invertedIndex !== "object" || Array.isArray(invertedIndex)) return "";
  const positioned = [];
  for (const [word, positions] of Object.entries(invertedIndex)) {
    if (!Array.isArray(positions)) continue;
    for (const position of positions) {
      if (Number.isInteger(position) && position >= 0 && position < 20_000) positioned.push([position, word]);
    }
  }
  positioned.sort((left, right) => left[0] - right[0]);
  return boundedString(positioned.map(([, word]) => word).join(" "), 800);
}

function normalizeQueries(queries) {
  if (!Array.isArray(queries)) throw new Error("queries must be an array");
  const normalized = [...new Set(queries.map((query) => boundedString(query, 400)).filter(Boolean))].slice(0, MAX_QUERY_COUNT);
  if (!normalized.length) throw new Error("at least one scholarly query is required");
  return normalized;
}

function boundedInteger(value, minimum, maximum, fallback) {
  const parsed = Number.parseInt(String(value ?? ""), 10);
  return Number.isFinite(parsed) ? Math.max(minimum, Math.min(maximum, parsed)) : fallback;
}

function boundedString(value, limit) {
  const text = typeof value === "string" ? value.trim() : "";
  return text.length > limit ? text.slice(0, limit) : text;
}
