import type {ExtensionAPI} from "@earendil-works/pi-coding-agent";
import {noteProject} from "./tools.ts";
import {installReadonly} from "../../harness/readonly.ts";
import {resolveComposition} from "../../harness/composition.mjs";
export default function noteProjectExtension(pi: ExtensionAPI) {
  const composition=resolveComposition({ids:["note-project"]});
  const item=composition.enabled[0];
  installReadonly(pi,item?[{...noteProject(pi),skillPaths:item.skillPaths}]:[],composition.diagnostics);
}
