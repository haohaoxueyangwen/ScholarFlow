import { pathToFileURL } from "node:url";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { resolveComposition } from "../../harness/composition.mjs";
import { installReadonly } from "../../harness/readonly.ts";

// Pilot composition root: add a capability here, not in the legacy lane router.
export default async function minimalHarness(pi: ExtensionAPI) {
  const composition = resolveComposition();
  const capabilities = [];
  for (const item of composition.enabled) {
    const module = await import(pathToFileURL(item.modulePath).href);
    capabilities.push({...module[item.factory](pi), skillPaths: item.skillPaths});
  }
  installReadonly(pi, capabilities, composition.diagnostics, composition.natureResources);
}
