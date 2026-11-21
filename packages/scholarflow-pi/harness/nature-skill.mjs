// Optional pinned upstream skill; Pi remains the sole execution runtime.
import { spawnSync } from "node:child_process";
import { resolve } from "node:path";
import { resolveScholarPython } from "./python-runtime.mjs";

export function natureSkillBundle({env = process.env, spawn = spawnSync, python = resolveScholarPython, root} = {}) {
  const home = env.SCHOLARFLOW_NATURE_HOME?.trim();
  if (!home) return {skillPaths: [], resources: []};
  if (!root) throw new Error("ScholarFlow package root is required");
  const result = spawn(python({env}), ["-s", "-B", "-m", "scholarflow.nature_skill_package", "status", "--home", resolve(home), "--resources"], {
    cwd: root, encoding: "utf8", timeout: 30000, env: {...env, PYTHONUTF8: "1", PYTHONNOUSERSITE: "1"},
  });
  if (result.status !== 0) throw new Error("Nature Skill validation failed: " + String(result.stderr || "unavailable").trim());
  let data;
  try { data = JSON.parse(result.stdout); } catch { throw new Error("Invalid Nature Skill validation result"); }
  if (!data.active || !Array.isArray(data.skill_paths) || data.skill_paths.length !== 1 || typeof data.skill_paths[0] !== "string"
      || !Array.isArray(data.resources) || !data.resources.length || data.resources.some(item => typeof item.path !== "string" || !/^[a-f0-9]{64}$/.test(item.sha256))) {
    throw new Error("Nature Skill is not installed and explicitly activated");
  }
  return {skillPaths: data.skill_paths, resources: data.resources};
}

export function natureSkillPaths(options) { return natureSkillBundle(options).skillPaths; }
