import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { resolveComposition } from "../../harness/composition.mjs";
import { installReadonly } from "../../harness/readonly.ts";
import { paperRead } from "./tools.ts";

export default function paperReadPackage(pi: ExtensionAPI) {
  const composition = resolveComposition({ids: ["paper-read"]});
  installReadonly(pi, composition.enabled.map((item) => ({...paperRead(pi), skillPaths: item.skillPaths})), composition.diagnostics);
}
