import type {ExtensionAPI} from "@earendil-works/pi-coding-agent";
import {taskWrite} from "./tools.ts";
import {installReadonly} from "../../harness/readonly.ts";
import {resolveComposition} from "../../harness/composition.mjs";
export default function taskWriteExtension(pi: ExtensionAPI) {
  const composition = resolveComposition({ids: ["task-write"]});
  const item = composition.enabled[0];
  installReadonly(pi, item ? [{...taskWrite(pi), skillPaths: item.skillPaths}] : [], composition.diagnostics);
}
