import { existsSync } from "node:fs";
import { resolve } from "node:path";

// A portable checkout intentionally does not ship a user's Pi settings. For
// offline contract evaluation only, explicitly load the project resources.
export function offlineContractResourceArgs({ live, evaluationCwd, packageRoot, hasSettings = existsSync }) {
  if (live || hasSettings(resolve(evaluationCwd, ".pi", "settings.json"))) return [];
  return [
    "--no-extensions", "--no-context-files", "--no-prompt-templates", "--no-themes",
    "-e", resolve(packageRoot, "extensions", "index.ts"),
    "--skill", resolve(packageRoot, "skills"),
  ];
}
