import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { openAICompletionsApi } from "@earendil-works/pi-ai/compat";
import { PROVIDER_ID, boundedStream, localBaseUrl, modelConfig, toolsEnabled } from "./config.mjs";

export default function localQwen(pi: ExtensionAPI) {
  pi.registerProvider(PROVIDER_ID, {
    name: "ScholarFlow local Qwen (SSH)", baseUrl: localBaseUrl(process.env.SCHOLARFLOW_QWEN_BASE_URL),
    api: "openai-completions", apiKey: "local", models: [modelConfig()],
    streamSimple: boundedStream(openAICompletionsApi().streamSimple, {allowTools: toolsEnabled()}),
  });
}
