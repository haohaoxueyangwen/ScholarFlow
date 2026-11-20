import {dirname,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {installReadonly} from '../../harness/readonly.ts';
import {researchContinuity} from './tools.ts';
export default function extension(pi:any){installReadonly(pi,[{...researchContinuity(pi),skillPaths:[resolve(dirname(fileURLToPath(import.meta.url)),'skills/research-continuity/SKILL.md')]}]);}
