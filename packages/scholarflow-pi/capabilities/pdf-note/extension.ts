import {dirname,resolve} from "node:path";
import {fileURLToPath} from "node:url";
import {installReadonly} from "../../harness/readonly.ts";
import {pdfNote} from "./tools.ts";
export default function extension(pi:any){installReadonly(pi,[{...pdfNote(pi),skillPaths:[resolve(dirname(fileURLToPath(import.meta.url)),"skills/pdf-note/SKILL.md")]}]);}
