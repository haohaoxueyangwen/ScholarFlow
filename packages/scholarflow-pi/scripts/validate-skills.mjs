import { createHash } from "node:crypto";
import { readFile, readdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const defaultPackageRoot = resolve(scriptDirectory, "..");
const allowedFrontmatterFields = new Set([
  "name",
  "description",
  "license",
  "compatibility",
  "metadata",
  "allowed-tools",
]);
const skillNamePattern = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

function sha256(value) {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function frontmatter(source, path, errors) {
  const match = source.match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/);
  if (!match) {
    errors.push(`${path}: SKILL.md must begin with YAML frontmatter`);
    return {};
  }
  const value = {};
  const lines = match[1].split(/\r?\n/);
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (!line.trim() || line.trimStart().startsWith("#") || /^\s/.test(line)) continue;
    const field = line.match(/^([A-Za-z][A-Za-z0-9-]*):(?:\s*(.*))?$/);
    if (!field) {
      errors.push(`${path}: unsupported YAML frontmatter line '${line}'`);
      continue;
    }
    const [, key, rawValue = ""] = field;
    if (Object.hasOwn(value, key)) {
      errors.push(`${path}: duplicate frontmatter field '${key}'`);
      continue;
    }
    if (rawValue === ">" || rawValue === "|") {
      const block = [];
      while (index + 1 < lines.length && /^\s/.test(lines[index + 1])) {
        index += 1;
        block.push(lines[index].trim());
      }
      value[key] = block.join(rawValue === ">" ? " " : "\n");
    } else if (rawValue.startsWith('"') && rawValue.endsWith('"')) {
      try {
        value[key] = JSON.parse(rawValue);
      } catch {
        errors.push(`${path}: invalid quoted value for '${key}'`);
        value[key] = rawValue;
      }
    } else if (rawValue.startsWith("'") && rawValue.endsWith("'")) {
      value[key] = rawValue.slice(1, -1).replaceAll("''", "'");
    } else {
      value[key] = rawValue;
    }
  }
  return value;
}

export async function validateSkillPackage(packageRoot = defaultPackageRoot) {
  const root = resolve(packageRoot);
  const skillsRoot = resolve(root, "skills");
  const lockPath = resolve(root, "quality", "skill-lock.json");
  const errors = [];
  const warnings = [];
  const packageDocument = JSON.parse(await readFile(resolve(root, "package.json"), "utf8"));
  const lockDocument = JSON.parse(await readFile(lockPath, "utf8"));
  const ecosystemDocument = JSON.parse(
    await readFile(resolve(root, "quality", "ecosystem-manifest.json"), "utf8"),
  );
  const natureSource = JSON.parse(
    await readFile(resolve(root, "vendor", "nature-skills", "SOURCE.json"), "utf8"),
  );
  if (lockDocument.schema_version !== 1) {
    errors.push(`${lockPath}: unsupported schema_version`);
  }
  if (lockDocument.spec_url !== "https://agentskills.io/specification") {
    errors.push(`${lockPath}: spec_url must identify the Agent Skills specification`);
  }
  if (lockDocument.package_version !== packageDocument.version) {
    errors.push(`${lockPath}: package_version must match package.json`);
  }

  const lockEntries = Array.isArray(lockDocument.skills) ? lockDocument.skills : [];
  const locksByName = new Map();
  for (const entry of lockEntries) {
    const name = String(entry?.name || "");
    if (!name || locksByName.has(name)) {
      errors.push(`${lockPath}: every lock entry needs a unique name`);
      continue;
    }
    locksByName.set(name, entry);
  }

  const directories = (await readdir(skillsRoot, { withFileTypes: true }))
    .filter((entry) => entry.isDirectory())
    .sort((left, right) => left.name.localeCompare(right.name));
  const results = [];
  for (const directory of directories) {
    const path = resolve(skillsRoot, directory.name, "SKILL.md");
    let source;
    try {
      source = await readFile(path, "utf8");
    } catch {
      errors.push(`${path}: missing SKILL.md`);
      continue;
    }
    const metadata = frontmatter(source, path, errors);
    const fields = Object.keys(metadata);
    for (const field of fields) {
      if (!allowedFrontmatterFields.has(field)) {
        errors.push(`${path}: unsupported frontmatter field '${field}'`);
      }
    }
    const name = String(metadata.name || "");
    const description = String(metadata.description || "").trim();
    if (!name || name.length > 64 || !skillNamePattern.test(name)) {
      errors.push(`${path}: name must be 1-64 lowercase letters, digits, and single hyphens`);
    }
    if (name !== directory.name) {
      errors.push(`${path}: name must match its directory`);
    }
    if (!description || description.length > 1024) {
      errors.push(`${path}: description must contain 1-1024 characters`);
    }
    const lineCount = source.split(/\r?\n/).length;
    if (lineCount > 500) {
      errors.push(`${path}: SKILL.md exceeds the 500-line progressive-loading limit`);
    }

    const lock = locksByName.get(name);
    if (!lock) {
      errors.push(`${path}: missing quality/skill-lock.json entry`);
    } else {
      if (lock.content_sha256 !== sha256(source)) {
        errors.push(`${path}: content hash differs from the reviewed skill lock`);
      }
      if (!String(lock.source || "").startsWith("workspace://")) {
        errors.push(`${path}: source must be an explicit workspace:// URI`);
      }
      if (!String(lock.source_revision || "").trim()) {
        errors.push(`${path}: source_revision is required`);
      }
      if (!String(lock.update_policy || "").trim()) {
        errors.push(`${path}: update_policy is required`);
      }
      if (!/^\d{4}-\d{2}-\d{2}$/.test(String(lock.reviewed_at || ""))) {
        errors.push(`${path}: reviewed_at must be YYYY-MM-DD`);
      }
      if (lock.source_type === "upstream_adapter") {
        const upstream = lock.upstream || {};
        if (!/^https:\/\//.test(String(upstream.source || ""))) {
          errors.push(`${path}: upstream adapter requires an HTTPS source`);
        }
        if (!/^[0-9a-f]{40}$/.test(String(upstream.revision || ""))) {
          errors.push(`${path}: upstream adapter requires a pinned Git commit`);
        }
        const ecosystemSource = (ecosystemDocument.vendored || []).find(
          (item) => item.package === upstream.package,
        );
        if (
          !ecosystemSource
          || upstream.source !== natureSource.source
          || upstream.revision !== natureSource.commit
          || upstream.source !== ecosystemSource?.source
          || upstream.revision !== ecosystemSource?.version
        ) {
          errors.push(`${path}: upstream lock differs from SOURCE.json or ecosystem-manifest.json`);
        }
      } else if (lock.source_type !== "first_party") {
        errors.push(`${path}: source_type must be first_party or upstream_adapter`);
      }
    }
    locksByName.delete(name);
    results.push({ name, path, line_count: lineCount, content_sha256: sha256(source) });
  }
  for (const name of locksByName.keys()) {
    errors.push(`${lockPath}: lock entry '${name}' has no installed Skill`);
  }
  return {
    schema_version: 1,
    status: errors.length ? "fail" : "pass",
    skill_count: results.length,
    skills: results,
    warnings,
    errors,
  };
}

export async function refreshSkillLock(skillName, packageRoot = defaultPackageRoot) {
  const root = resolve(packageRoot);
  const lockPath = resolve(root, "quality", "skill-lock.json");
  const skillPath = resolve(root, "skills", skillName, "SKILL.md");
  const packageDocument = JSON.parse(await readFile(resolve(root, "package.json"), "utf8"));
  const lockDocument = JSON.parse(await readFile(lockPath, "utf8"));
  const entry = (lockDocument.skills || []).find((item) => item.name === skillName);
  if (!entry) throw new Error(`Skill '${skillName}' has no existing reviewed lock`);
  const source = await readFile(skillPath, "utf8");
  entry.content_sha256 = sha256(source);
  entry.reviewed_at = new Date().toISOString().slice(0, 10);
  lockDocument.package_version = packageDocument.version;
  if (entry.source_type === "first_party") {
    entry.source_revision = `@scholarflow/pi@${packageDocument.version}`;
  }
  await writeFile(lockPath, `${JSON.stringify(lockDocument, null, 2)}\n`, "utf8");
  return {
    name: skillName,
    content_sha256: entry.content_sha256,
    reviewed_at: entry.reviewed_at,
  };
}

async function main() {
  const arguments_ = process.argv.slice(2);
  const packageRoot = arguments_.find((item) => !item.startsWith("--")) || defaultPackageRoot;
  const skillArgument = arguments_.find((item) => item.startsWith("--skill="));
  if (arguments_.includes("--update-lock")) {
    if (!skillArgument) throw new Error("--update-lock requires --skill=<name>");
    const updated = await refreshSkillLock(
      skillArgument.slice("--skill=".length),
      packageRoot,
    );
    process.stdout.write(`${JSON.stringify({ status: "updated", ...updated }, null, 2)}\n`);
  }
  const report = await validateSkillPackage(packageRoot);
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  if (report.status !== "pass") process.exitCode = 1;
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  await main();
}
