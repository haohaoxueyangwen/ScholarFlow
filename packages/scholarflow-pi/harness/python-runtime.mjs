import { existsSync } from "node:fs";
import { delimiter, join, resolve } from "node:path";
import { spawnSync } from "node:child_process";

function probePython(candidate) {
  const result = spawnSync(candidate, ["-s", "-B", "-c", "import platform; print(platform.python_version())"], {
    encoding: "utf8", timeout: 10000,
  });
  return result.status === 0 ? String(result.stdout).trim() : "";
}

function compatible(version) {
  const parts = /^(\d+)\.(\d+)\.(\d+)$/.exec(String(version))?.slice(1).map(Number);
  return Boolean(parts && (parts[0] > 3 || (parts[0] === 3 && parts[1] >= 11)));
}

export function resolveScholarPython({ env = process.env, exists = existsSync, probe = probePython } = {}) {
  const explicit = env.SCHOLARFLOW_PYTHON?.trim();
  const names = process.platform === "win32" ? ["python.exe", "python3.exe"] : ["python3", "python"];
  const fromPath = String(env.PATH || env.Path || "").split(delimiter)
    .map((entry) => entry.trim().replace(/^"|"$/g, ""))
    .filter(Boolean)
    .flatMap((directory) => names.map((name) => join(directory, name)));
  // A configured executable is a deliberate boundary: do not silently switch
  // Python environments when it becomes unavailable or too old.
  const candidates = explicit ? [explicit] : fromPath;
  for (const value of [...new Set(candidates)]) {
    const candidate = resolve(value);
    try {
      if (exists(candidate) && compatible(probe(candidate))) return candidate;
    } catch { /* Probe the next PATH candidate. */ }
  }
  throw new Error("Python 3.11 or newer is required; set SCHOLARFLOW_PYTHON to a compatible executable.");
}
