import { existsSync, readFileSync, realpathSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { allToolNames } from "../node_modules/@earendil-works/pi-coding-agent/dist/core/tools/index.js";
import { validateGoalRequests } from "../capabilities/project-write/grant.mjs";
import { validateTaskGrant } from "../capabilities/task-write/grant.mjs";
import { validateNoteGrant } from "../capabilities/note-write/grant.mjs";
import { validateProjectionGrant } from "../capabilities/note-project/grant.mjs";
import { validatePdfGrant } from "../capabilities/pdf-read/grant.mjs";
import { validatePdfNoteGrant } from "../capabilities/pdf-note/grant.mjs";
import { validateLibraryGrant } from "../capabilities/library-read/grant.mjs";
import { validateContinuityGrant } from "../capabilities/research-continuity/grant.mjs";
import { validateWorkflow } from "../capabilities/research-workflow/grant.mjs";

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
// Composition only: adding a capability does not add a domain command switch.
export const catalog = {
  "research-workflow": {root:resolve(packageRoot,"capabilities/research-workflow"),module:"tools.ts",factory:"researchWorkflow",dependencies:["grant.mjs"],grantFileEnv:"SCHOLARFLOW_WORKFLOW_GRANT_FILE",grantFields:["principal","stage_id","document_id","artifact_id","knowledge_configured"],grantArrayFields:[],validateGrant:validateWorkflow},
  "research-continuity": {root:resolve(packageRoot,"capabilities/research-continuity"),module:"tools.ts",factory:"researchContinuity",needsPython:true,dependencies:["operations.py","adapter.py","grant.mjs","../../../../scholarflow/research_continuity.py"],grantFileEnv:"SCHOLARFLOW_CONTINUITY_GRANT_FILE",grantFields:["principal","stage_id","workflow_id","objective","library_grant_file","source_ids"],grantArrayFields:["source_ids"],validateGrant:validateContinuityGrant},
  "library-read": {root:resolve(packageRoot,"capabilities/library-read"),module:"tools.ts",factory:"libraryRead",needsPython:true,dependencies:["operations.py","adapter.py","grant.mjs","../../../../scripts/library_knowledge.py"],grantFileEnv:"SCHOLARFLOW_LIBRARY_GRANT_FILE",grantFields:["principal","stage_ids"],validateGrant:validateLibraryGrant},
  "pdf-note": {root:resolve(packageRoot,"capabilities/pdf-note"),module:"tools.ts",factory:"pdfNote",needsPython:true,dependencies:["operations.py","adapter.py","grant.mjs","../../../../scholarflow/pdf_notes.py"],grantFileEnv:"SCHOLARFLOW_PDF_NOTE_GRANT_FILE",grantFields:["principal","create_notes","update_notes","pdf_grant_file","pdf_sources"],grantArrayFields:[],validateGrant:validatePdfNoteGrant},
  "pdf-read": {root:resolve(packageRoot,"capabilities/pdf-read"),module:"tools.ts",factory:"pdfRead",needsPython:true,
    dependencies:["operations.py","adapter.py","grant.mjs","../../../../scholarflow/pdf_reading.py","../../harness/operation-client.ts"],
    grantFileEnv:"SCHOLARFLOW_PDF_READ_GRANT_FILE",grantFields:["principal","documents"],grantArrayFields:[],validateGrant:validatePdfGrant},
  "note-project": { root: resolve(packageRoot, "capabilities/note-project"), module: "tools.ts", factory: "noteProject", needsPython: true,
    dependencies: ["operations.py", "adapter.py", "grant.mjs", "../../../../scholarflow/note_projection.py", "../../../../scholarflow/governed_writes.py", "../../harness/write-client.ts"],
    grantFileEnv: "SCHOLARFLOW_NOTE_PROJECT_GRANT_FILE", grantFields: ["principal", "targets"], grantArrayFields: [], validateGrant: validateProjectionGrant },
  "project-evidence": { root: resolve(packageRoot, "profiles/minimal"), module: "project-evidence.ts", factory: "projectEvidence", needsPython: true, dependencies: ["operations.py", "../../harness/operation_runner.py"] },
  "paper-read": { root: resolve(packageRoot, "capabilities/paper-read"), module: "tools.ts", factory: "paperRead", needsPython: true, dependencies: ["operations.py", "../../harness/operation_runner.py"], permission: "local-catalog-read" },
  "project-write": { root: resolve(packageRoot, "capabilities/project-write"), module: "tools.ts", factory: "projectWrite", needsPython: true,
    dependencies: ["operations.py", "adapter.py", "grant.mjs", "../../../../scholarflow/governed_writes.py", "../../harness/write-client.ts"], grantFileEnv: "SCHOLARFLOW_PROJECT_WRITE_GRANT_FILE", grantFields: ["principal", "create_stage_ids", "update_stage_ids", "paper_ids"],
    grantOptionalFields: ["goal_requests"], validateGrant: validateGoalRequests },
  "task-write": { root: resolve(packageRoot, "capabilities/task-write"), module: "tools.ts", factory: "taskWrite", needsPython: true,
    dependencies: ["operations.py", "adapter.py", "grant.mjs", "../../../../scholarflow/governed_writes.py", "../../harness/write-client.ts"],
    grantFileEnv: "SCHOLARFLOW_TASK_WRITE_GRANT_FILE", grantFields: ["principal", "create_tasks", "update_tasks"], grantArrayFields: [], validateGrant: validateTaskGrant },
  "note-write": { root: resolve(packageRoot, "capabilities/note-write"), module: "tools.ts", factory: "noteWrite", needsPython: true,
    dependencies: ["operations.py", "adapter.py", "grant.mjs", "../../../../scholarflow/source_notes.py", "../../../../scholarflow/governed_writes.py", "../../harness/write-client.ts"],
    grantFileEnv: "SCHOLARFLOW_NOTE_WRITE_GRANT_FILE", grantFields: ["principal", "create_notes", "update_notes", "source_papers"], grantArrayFields: [], validateGrant: validateNoteGrant },
};

export function resolveComposition({ ids, libraryRead, entries = catalog } = {}) {
  ids ??= (process.env.SCHOLARFLOW_CAPABILITIES ?? "project-evidence").split(",").filter(Boolean);
  libraryRead ??= process.env.SCHOLARFLOW_PAPER_SCOPE === "local-catalog-read";
  if (new Set(ids).size !== ids.length) throw new Error("Duplicate capability ID");
  const enabled = [], diagnostics = [];
  for (const id of ids) {
    const item = entries[id];
    if (!item) throw new Error(`Unknown capability: ${id}`);
    try {
      if (item.permission && !libraryRead) throw new Error(`Explicit ${item.permission} permission required`);
      if (item.grantFileEnv) {
        const path = process.env[item.grantFileEnv];
        if (!path || !existsSync(path)) throw new Error("Explicit trusted capability grant file required");
        const grant = JSON.parse(readFileSync(path, "utf8").replace(/^\uFEFF/, ""));
        if (!grant || typeof grant !== "object" || Array.isArray(grant)
            || item.grantFields.some(k => !(k in grant))
            || Object.keys(grant).some(k => ![...item.grantFields, ...(item.grantOptionalFields ?? [])].includes(k))
            || typeof grant.principal !== "string" || !grant.principal.trim() || grant.principal !== grant.principal.trim() || grant.principal.length > 160
            || (item.grantArrayFields ?? item.grantFields.filter(k => k !== "principal")).some(k => !Array.isArray(grant[k]) || grant[k].length > 100
              || grant[k].some(x => typeof x !== "string" || !x.trim() || x !== x.trim() || x.length > 200))
            || item.validateGrant && !item.validateGrant(grant)) throw new Error("Invalid trusted capability grant file");
      }
      if (item.dependency && !existsSync(resolve(item.root, item.dependency))) throw new Error(`Missing dependency: ${item.dependency}`);
      for (const path of item.dependencies ?? []) if (!existsSync(resolve(item.root, path))) throw new Error(`Missing dependency: ${path}`);
      if (item.needsPython && process.env.SCHOLARFLOW_PYTHON && !existsSync(process.env.SCHOLARFLOW_PYTHON)) throw new Error("Configured shared Python executable is unavailable");
      const manifest = JSON.parse(readFileSync(resolve(item.root, "package.json"), "utf8"));
      const skillPaths = manifest.pi.skills.map((path) => realpathSync(resolve(item.root, path, "SKILL.md")));
      const modulePath = realpathSync(resolve(item.root, item.module));
      enabled.push({ id, ...item, modulePath, skillPaths });
    } catch (error) { diagnostics.push({ id, status: "disabled", reason: error.message }); }
  }
  return { enabled, diagnostics, skillPaths: enabled.flatMap((item) => item.skillPaths) };
}

export function runtimeArgs(composition, {includeLocalQwenProvider = true} = {}) {
  return ["--offline", "--no-extensions", "--no-skills", "--no-context-files", "--no-prompt-templates", "--no-themes",
    "-e", resolve(packageRoot, "profiles/minimal/extension.ts"),
    ...(includeLocalQwenProvider ? ["-e", resolve(packageRoot, "providers/local-qwen/extension.ts")] : []),
    ...composition.skillPaths.flatMap((path) => ["--skill", path]),
    "--exclude-tools", [...allToolNames].filter((name) => name !== "read").join(",")];
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const composition = resolveComposition();
  for (const item of composition.diagnostics) process.stderr.write(`${item.id}: ${item.reason}\n`);
  process.stdout.write(JSON.stringify(process.argv.includes("--status")
    ? {capabilities: composition.enabled.map((item) => item.id), skillPaths: composition.skillPaths, diagnostics: composition.diagnostics}
    : runtimeArgs(composition)));
}
