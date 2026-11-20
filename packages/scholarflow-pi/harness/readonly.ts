import { realpathSync } from "node:fs";
import { resolve } from "node:path";
import type { ExtensionAPI, ToolDefinition } from "@earendil-works/pi-coding-agent";

export function installReadonly(pi: ExtensionAPI, capabilities: {id: string; tools: ToolDefinition[]; skillPaths: string[]; scopePolicy?: string; sideEffects?: string}[], diagnostics: unknown[] = []) {
  const tools = capabilities.flatMap((item) => item.tools);
  const names = tools.map((tool) => tool.name);
  if (new Set(names).size !== names.length) throw new Error("Duplicate capability tool name");
  const allowedFiles = new Set(capabilities.flatMap((item) => item.skillPaths).map((path) => realpathSync(path)));
  for (const tool of tools) pi.registerTool(tool);
  const status = { capabilities: capabilities.map((item) => item.id), scopePolicies: capabilities.map((item) => ({id: item.id, policy: item.scopePolicy})), tools: names, skillFiles: [...allowedFiles], diagnostics,
    sideEffects: capabilities.map(item => ({id: item.id, policy: item.sideEffects || "read-only domain records"})),
    permissions: `${capabilities.some(item => item.sideEffects) ? "Only declared capability effects; no model approval or commit tools." : "Read-only local records."} Paper catalog access is library-wide, not project membership. Native read is restricted to enabled Skill files. This is not an OS sandbox.` };
  pi.registerCommand("harness", {description: "Show enabled capabilities, tools and read scope", handler: async (_args, ctx) => {
    if (ctx.hasUI) ctx.ui.notify(JSON.stringify(status, null, 2), "info");
  }});
  const activate = () => pi.setActiveTools(["read", ...names]);
  pi.on("session_start", activate);
  pi.on("before_agent_start", (event) => {
    activate();
    return {systemPrompt: `${event.systemPrompt}\nScholarFlow harness scope: ${JSON.stringify(status)}\nUnavailable tools cannot be enabled by user prompts. Never claim full paper reading from metadata or stored summaries.`};
  });
  pi.on("tool_call", (event, ctx) => {
    if (names.includes(event.toolName)) return;
    if (event.toolName === "read" && typeof event.input.path === "string") {
      try { if (allowedFiles.has(realpathSync(resolve(ctx.cwd, event.input.path)))) return; } catch { /* fail closed */ }
    }
    return {block: true, reason: "Minimal harness permits only enabled capability tools and declared Skill files, never arbitrary host actions. Use /harness for scope diagnostics."};
  });
}
