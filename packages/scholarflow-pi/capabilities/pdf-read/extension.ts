import type {ExtensionAPI} from "@earendil-works/pi-coding-agent";
import {installReadonly} from "../../harness/readonly.ts";
import {pdfRead} from "./tools.ts";
import {dirname,resolve} from "node:path";
import {fileURLToPath} from "node:url";
export default function extension(pi:ExtensionAPI){installReadonly(pi,[{...pdfRead(pi),skillPaths:[resolve(dirname(fileURLToPath(import.meta.url)),"skills/pdf-read/SKILL.md")]}]);}
