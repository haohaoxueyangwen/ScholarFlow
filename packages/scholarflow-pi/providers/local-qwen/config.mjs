export const PROVIDER_ID = "scholarflow-local";
export const MODEL_ID = "qwen38-vision";
export const DEFAULT_BASE_URL = "http://127.0.0.1:18080/v1";
export const TIMEOUT_MS = 600000;
// Qualification is scoped to the reviewed qwen38-vision deployment, not every compatible endpoint.
export const TOOL_CALLING_QUALIFIED = true;
export function toolsEnabled(env = process.env) {
  const value = env.SCHOLARFLOW_QWEN_TOOL_CALLING;
  if (value !== undefined && value !== "" && value !== "0" && value !== "1") throw new Error("SCHOLARFLOW_QWEN_TOOL_CALLING must be 0 or 1.");
  return value === "0" ? false : value === "1" ? true : TOOL_CALLING_QUALIFIED;
}

export function localBaseUrl(value = DEFAULT_BASE_URL) {
  const url = new URL(value);
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
      || url.username || url.password || url.search || url.hash || url.pathname.replace(/\/$/, "") !== "/v1") {
    throw new Error("Local Qwen requires a credential-free loopback HTTP /v1 URL; use the SSH tunnel.");
  }
  return url.href.replace(/\/$/, "");
}

export function modelConfig() {
  return {
    id: MODEL_ID, name: "ScholarFlow local Qwen vision", reasoning: true, input: ["text", "image"],
    contextWindow: 262144, maxTokens: 4096,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    thinkingLevelMap: { minimal: "low", low: "low", medium: "medium", high: "xhigh", xhigh: "xhigh", max: "xhigh" },
    compat: {
      supportsStore: false, supportsDeveloperRole: false, supportsReasoningEffort: false,
      supportsStrictMode: false, maxTokensField: "max_tokens", thinkingFormat: "chat-template",
      chatTemplateKwargs: { enable_thinking: { $var: "thinking.enabled" }, reasoning_effort: { $var: "thinking.effort", omitWhenOff: true } },
    },
  };
}

export function assertSingleImage(payload) {
  if (!payload || typeof payload !== "object" || !Array.isArray(payload.messages)) throw new Error("Invalid local Qwen Chat Completions payload.");
  const count = payload.messages.reduce((total, message) => total + (Array.isArray(message.content)
    ? message.content.filter(part => part.type === "image_url").length : 0), 0);
  if (count > 1) throw new Error("Local Qwen accepts at most one image per request, including conversation history. Start a new session for another image.");
  return payload;
}

// Pi transport decorator only; not another agent/model loop.
export function boundedStream(nativeStream, {allowTools = false} = {}) {
  return (model, context, options = {}) => nativeStream(model, context, {
    ...options, apiKey: options.apiKey || "local", maxRetries: 0,
    timeoutMs: options.timeoutMs ?? TIMEOUT_MS,
    maxTokens: Math.min(options.maxTokens ?? model.maxTokens, model.maxTokens),
    temperature: options.temperature ?? 1,
    samplingParams: { top_p: 0.95, ...options.samplingParams },
    onPayload: async (payload, currentModel) => {
      const replacement = await options.onPayload?.(payload, currentModel);
      const finalPayload = assertSingleImage(replacement === undefined ? payload : replacement);
      if (!allowTools && finalPayload.tools?.length) throw new Error("Local Qwen tool calling is disabled for this run. Use scholarflow-local.ps1 -ChatOnly or the reviewed tool-enabled configuration.");
      return finalPayload;
    },
  });
}
