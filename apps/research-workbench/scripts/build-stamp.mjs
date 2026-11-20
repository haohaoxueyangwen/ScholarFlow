import { createHash } from "node:crypto";
import { readdir, readFile, writeFile, access } from "node:fs/promises";
import { resolve, relative, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
async function files(directory) {
  const entries = await readdir(directory, { withFileTypes: true }).catch(() => []);
  const result = [];
  for (const entry of entries) {
    if (entry.isSymbolicLink()) continue;
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) result.push(...await files(path));
    else result.push(path);
  }
  return result;
}
export async function sourceFingerprint() {
  const paths = [...await files(resolve(root, "app")), ...await files(resolve(root, "worker")), ...await files(resolve(root, "public")), ...await files(resolve(root, "tooling")), ...await files(resolve(root, "scripts")), ...["package.json", "package-lock.json", "vite.config.ts", ".openai/hosting.json"].map((path) => resolve(root, path))];
  for (const optional of ["next.config.ts", "tsconfig.json", "postcss.config.mjs"]) {
    const path = resolve(root, optional);
    try { await access(path); paths.push(path); } catch { /* optional config */ }
  }
  const hash = createHash("sha256");
  for (const path of paths.sort()) hash.update(relative(root, path)).update(await readFile(path));
  hash.update(process.env.NEXT_PUBLIC_SCHOLARFLOW_API_URL || "default");
  hash.update(JSON.stringify(process.env.NEXT_PUBLIC_SCHOLARFLOW_RESEARCH_URL || "default"));
  return hash.digest("hex");
}
export async function outputFingerprint() {
  const paths = [...await files(resolve(root, "dist/server")), ...await files(resolve(root, "dist/client"))];
  if (!paths.length) throw new Error("Build outputs are missing");
  const hash = createHash("sha256");
  for (const path of paths.sort()) {
    const content = await readFile(path);
    hash.update(relative(root, path)).update("\0").update(String(content.length)).update("\0").update(content);
  }
  return hash.digest("hex");
}
const target = resolve(root, "dist/workbench-build.json");
if (process.argv.includes("--fingerprint")) {
  console.log(await sourceFingerprint());
} else if (process.argv.includes("--check")) {
  try {
    await access(resolve(root, "dist/server/index.js"));
    await access(resolve(root, "dist/client"));
    const record = JSON.parse(await readFile(target, "utf8"));
    process.exitCode = record.fingerprint === await sourceFingerprint() && record.output_fingerprint === await outputFingerprint() ? 0 : 1;
  } catch { process.exitCode = 1; }
} else {
  await writeFile(target, JSON.stringify({ fingerprint: await sourceFingerprint(), output_fingerprint: await outputFingerprint(), built_at: new Date().toISOString() }) + "\n");
}
