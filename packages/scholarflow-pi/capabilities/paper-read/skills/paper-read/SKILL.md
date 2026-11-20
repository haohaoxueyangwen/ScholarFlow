---
name: paper-read
description: Read existing local ScholarFlow paper metadata and stored summaries. Use for catalog search, DOI/status checks, and source-grounded summary review; not PDF full-text reading, web literature search, translation, or writing records.
---

# Paper records (read-only)

The operator must enable local-catalog-read permission. This authorizes reads of the local paper catalog, not automatic membership in the current project. Never infer project evidence links from a catalog match.

1. Use sf_paper_search with a title/author/year filter; use its exact paper_id in sf_paper_get. For an exact user-supplied ID, get it directly.
2. Report paper ID, provenance fields and only fields actually returned. Distinguish stored summary from the original article's full text.
3. If summary or full text is absent, say it is unavailable; do not invent methods/results or claim to have read the PDF.
4. A not_found or dependency error is not an empty successful result. Explain the error; do not retry another project's records as a substitute.
5. No writes, imports, project attachment, Zotero access or external search are provided. The harness permits native read only for enabled Skill files, not arbitrary PDFs or host files.

Pi owns model execution, sessions and compression. Use the harness scope diagnostic; do not introduce a second loop or a central lane.
