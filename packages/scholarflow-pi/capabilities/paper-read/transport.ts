import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {callReadOperation, localGrant} from "../../harness/operation-client.ts";

export async function callPaper(pi: ExtensionAPI, operation: string, params: unknown, id: string, signal?: AbortSignal, timeout = 15000, ctx?: any) {
  return callReadOperation(pi, `paper.${operation}`, params, {kind: "paper_catalog"}, localGrant("paper-read"), id, signal, timeout, ctx);
}
