import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

/**
 * ScholarFlow's single compatibility boundary for the upstream Pi API.
 *
 * Pi API changes must be adapted here and qualified by pi_lifecycle.py before
 * domain tools are changed. Keeping the upstream type import in one module
 * makes future source migrations small and auditable.
 */
export type ScholarFlowPiAPI = ExtensionAPI;

const REQUIRED_METHODS = [
  "exec",
  "getAllTools",
  "on",
  "registerCommand",
  "registerTool",
  "setActiveTools",
] as const;

export function requireCompatiblePi(raw: ExtensionAPI): ScholarFlowPiAPI {
  const candidate = raw as ExtensionAPI & Record<string, unknown>;
  const missing = REQUIRED_METHODS.filter((name) => typeof candidate[name] !== "function");
  if (missing.length) {
    throw new Error(
      `Unsupported Pi extension API; missing ${missing.join(", ")}. ` +
      "Run `python scripts/pi_lifecycle.py canary --target latest` and add a reviewed compatibility adapter.",
    );
  }
  return raw;
}
