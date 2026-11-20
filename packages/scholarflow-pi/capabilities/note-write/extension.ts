import type {ExtensionAPI} from "@earendil-works/pi-coding-agent";
import {noteWrite} from "./tools.ts";
import {installReadonly} from "../../harness/readonly.ts";
import {resolveComposition} from "../../harness/composition.mjs";
export default function noteWriteExtension(pi: ExtensionAPI) {
  const composition = resolveComposition({ids: ["note-write"]});
  const item = composition.enabled[0];
  installReadonly(pi, item ? [{...noteWrite(pi), skillPaths: item.skillPaths}] : [], composition.diagnostics);
}
