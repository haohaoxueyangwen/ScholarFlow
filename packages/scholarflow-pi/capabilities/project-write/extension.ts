import type {ExtensionAPI} from "@earendil-works/pi-coding-agent";
import {projectWrite} from "./tools.ts";
import {installReadonly} from "../../harness/readonly.ts";
import {resolveComposition} from "../../harness/composition.mjs";
export default function projectWriteExtension(pi: ExtensionAPI) {
  const composition = resolveComposition({ids: ["project-write"]});
  const item = composition.enabled[0];
  installReadonly(pi, item ? [{...projectWrite(pi), skillPaths: item.skillPaths}] : [], composition.diagnostics);
}
