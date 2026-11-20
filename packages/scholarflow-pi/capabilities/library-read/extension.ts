import {dirname,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {installReadonly} from '../../harness/readonly.ts';
import {libraryRead} from './tools.ts';
export default function extension(pi:any){installReadonly(pi,[{...libraryRead(pi),skillPaths:[resolve(dirname(fileURLToPath(import.meta.url)),'skills/library-read/SKILL.md')]}]);}
