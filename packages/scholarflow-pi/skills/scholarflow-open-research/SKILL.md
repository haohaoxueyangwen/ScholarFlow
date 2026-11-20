---
name: scholarflow-open-research
description: Investigate a broad or current research question with several independent evidence surfaces by coordinating bounded read-only Pi workers. Use for research-frontier scans, architecture comparisons, contested claims, or open questions where one sequential search would likely miss important evidence; do not use for stored-only project status, a single source lookup, or a task whose steps depend on each other.
---

# ScholarFlow open research

Use `sf_research_delegate` only when independent source coverage is worth extra
model calls. The persistent Pi session remains the controller; workers are
temporary evidence collectors, not new project owners.

1. Call `sf_lane_enter` for the `literature` lane. Ground the scope with the smallest necessary
   ScholarFlow read, but do not copy the parent conversation into worker tasks.
2. Split the question into non-overlapping evidence facets. Prefer two workers,
   use three only for a clearly broad question, and use four only when the user
   explicitly supplied four independent scopes. Good
   facets can be searched independently, such as primary research, production
   implementations, failure evidence, and standards or official guidance.
   Do not split one sequential reasoning chain into parallel workers.
3. Call `sf_research_delegate` exactly once. Prefer two workers. Give every facet a precise focus
   and, when useful, up to four distinct queries. Use three workers only when the user named three
   independent scopes, and four only when they explicitly supplied four independent scopes. After
   the delegate returns, stop: do not call knowledge, paper, or artifact tools. Use the fast configured
   worker model unless the host explicitly overrides it. Each worker starts
   with the private read-only `sf_research_search` tool, which returns bounded
   OpenAlex metadata and stable DOI/OpenAlex refs before optional Web or local
   knowledge validation. `OPENALEX_API_KEY` is optional for casual use and can
   be configured later for a larger API budget.
4. Treat each returned bundle as candidate evidence. Synthesize across bundles,
   cite their exact source refs, distinguish primary from secondary evidence,
   report conflicts and gaps, and include the aggregate worker token/cost data.
   Never invent a source omitted from the bundles.
5. If every worker fails, state that the parallel research attempt failed and
   continue only with already available verified evidence. If some fail, label
   the missing facet instead of hiding the partial result.
6. Workers are process-enforced read-only. They must never receive proposal,
   apply, write, acknowledge, generic MCP, shell, filesystem, browser-control,
   or memory-write tools. Do not ask a worker to bypass this boundary.
7. Do not persist the synthesis unless the user explicitly asks. When they do,
   the persistent parent Pi may emit one governed
   `sf_artifact_propose_publish` after synthesis; workers cannot propose or
   apply any state change.

For a narrow lookup, use one direct read/search tool instead. For stored
literature synthesis, use the normal bounded evidence-bundle path without
starting workers.
