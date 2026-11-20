import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {callReadOperation, localGrant} from "../../harness/operation-client.ts";
import { projectScope } from "./project-scope.js";

export function projectEvidence(pi: ExtensionAPI) {
  const scope = projectScope();
  pi.on("session_start", () => scope.beginTurn());
  pi.on("before_agent_start", (event) => scope.beginTurn(event.prompt));
  return { id: "project-evidence", scopePolicy: "Each turn requires an exact stage ID supplied by the user or a unique non-paginated search result. Multiple matches: show candidates and ask, do not call stage-specific reads. Scope resets every turn.", tools: [
    {
      name: "sf_project_search", label: "Search projects",
      description: "Read current project records. Returns exact stage IDs; no writes.",
      parameters: Type.Object({
        query: Type.Optional(Type.String()),
        limit: Type.Optional(Type.Integer({minimum: 1, maximum: 100})),
        offset: Type.Optional(Type.Integer({minimum: 0})),
      }, {additionalProperties: false}),
      async execute(id: string, params: {query?: string; limit?: number; offset?: number}, signal?: AbortSignal, _update?: any, ctx?: any) {
        const result = await callReadOperation(pi, "project.search", params, {kind: "project_catalog"}, localGrant("project-evidence"), id, signal, 15000, ctx);
        scope.searched(result.details.data);
        return result;
      },
    },
    {
      name: "sf_project_get", label: "Read project",
      description: "Read one exact project stage ID authorized in this turn by the user or a unique search match. Multiple matches require clarification, not attempted reads.",
      parameters: Type.Object({stage_id: Type.String({minLength: 1})}, {additionalProperties: false}),
      async execute(id: string, params: {stage_id: string}, signal?: AbortSignal, _update?: any, ctx?: any) {
        scope.assertRead(params.stage_id.trim());
        return callReadOperation(pi, "project.get", params, {kind: "project_stage", stage_id: params.stage_id.trim()}, localGrant("project-evidence", scope.authorizedIds()), id, signal, 15000, ctx);
      },
    },
    {
      name: "sf_evidence_bundle", label: "Read project evidence",
      description: "Read stored evidence and provenance for an exact project. Not semantic retrieval or a complete literature search.",
      parameters: Type.Object({
        project_stage_id: Type.String({minLength: 1}),
        query: Type.Optional(Type.String()),
        limit: Type.Optional(Type.Integer({minimum: 1, maximum: 20})),
      }, {additionalProperties: false}),
      async execute(id: string, params: {project_stage_id: string; query?: string; limit?: number}, signal?: AbortSignal, _update?: any, ctx?: any) {
        scope.assertRead(params.project_stage_id.trim());
        return callReadOperation(pi, "evidence.bundle", params, {kind: "project_stage", stage_id: params.project_stage_id.trim()}, localGrant("project-evidence", scope.authorizedIds()), id, signal, 15000, ctx);
      },
    },
  ] };
}
