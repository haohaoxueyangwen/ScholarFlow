import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { callPaper } from "./transport.ts";

export function paperRead(pi: ExtensionAPI) {
  return {id: "paper-read", scopePolicy: "Explicitly authorized local-catalog-read, library-wide. No automatic project ownership, full-text reading or writes.", tools: [
    {name: "sf_paper_search", label: "Search local paper catalog",
      description: "Read-only search of the authorized local paper catalog, not project-filtered, web search or PDF full-text reading.",
      parameters: Type.Object({
        query: Type.Optional(Type.String()), status: Type.Optional(Type.String()), author: Type.Optional(Type.String()),
        limit: Type.Optional(Type.Integer({minimum: 1, maximum: 100})), offset: Type.Optional(Type.Integer({minimum: 0})),
        year_from: Type.Optional(Type.Integer({minimum: 1000, maximum: 2100})), year_to: Type.Optional(Type.Integer({minimum: 1000, maximum: 2100})),
      }, {additionalProperties: false}),
      async execute(id: string, params: Record<string, unknown>, signal?: AbortSignal, _update?: any, ctx?: any) { return callPaper(pi, "search", params, id, signal, 15000, ctx); }},
    {name: "sf_paper_get", label: "Read stored paper record",
      description: "Read one exact paper ID: metadata and stored summary only. Does not read PDFs or attach papers to projects.",
      parameters: Type.Object({paper_id: Type.String({minLength: 1})}, {additionalProperties: false}),
      async execute(id: string, params: {paper_id: string}, signal?: AbortSignal, _update?: any, ctx?: any) { return callPaper(pi, "get", params, id, signal, 15000, ctx); }},
  ]};
}
