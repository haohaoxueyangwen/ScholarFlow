#!/usr/bin/env node
/**
 * Re-exec under a pinned ScholarFlow Node (>=22.19.0) when PATH node is older.
 * Sets SCHOLARFLOW_NODE so evaluation children share the same binary.
 *
 * Usage: node ./scripts/run-with-pinned-node.mjs [--] <args for node...>
 * Example: node ./scripts/run-with-pinned-node.mjs --test ./quality/*.test.mjs
 */
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { delimiter, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { assertNodeRuntimeAtLeast } from "./assert-node-runtime.mjs";

const MINIMUM = "22.19.0";

function parseVersion(value) {
  return /^v?(\d+)\.(\d+)\.(\d+)$/.exec(String(value).trim())?.slice(1).map(Number) ?? null;
}

function isAtLeast(actual, minimum) {
  const observed = parseVersion(actual);
  const required = parseVersion(minimum);
  if (!observed || !required) return false;
  for (let i = 0; i < 3; i += 1) {
    if (observed[i] > required[i]) return true;
    if (observed[i] < required[i]) return false;
  }
  return true;
}

function probe(candidate) {
  if (!candidate || !existsSync(candidate)) return null;
  const probed = spawnSync(candidate, ["--version"], { encoding: "utf8", timeout: 5000 });
  if (probed.status !== 0) return null;
  const version = String(probed.stdout || "").trim();
  return isAtLeast(version, MINIMUM) ? candidate : null;
}

function resolvePinnedNode() {
  const fromEnv = process.env.SCHOLARFLOW_NODE?.trim();
  const executable = process.platform === "win32" ? "node.exe" : "node";
  const fromPath = (process.env.PATH || "")
    .split(delimiter)
    .map((directory) => directory.trim().replace(/^"|"$/g, ""))
    .filter(Boolean)
    .map((directory) => join(directory, executable));
  for (const candidate of [fromEnv, process.execPath, ...fromPath]) {
    const ok = probe(candidate);
    if (ok) return ok;
  }
  return null;
}

const argv = process.argv.slice(2).filter((item, index, all) => !(item === "--" && index === 0));
const pinned = resolvePinnedNode();
if (!pinned) {
  console.error(
    `ScholarFlow requires Node.js >= ${MINIMUM}. Set SCHOLARFLOW_NODE to a compatible node.exe.`,
  );
  process.exit(1);
}

assertNodeRuntimeAtLeast(
  spawnSync(pinned, ["--version"], { encoding: "utf8" }).stdout.trim(),
  MINIMUM,
  "pinned runner",
);

const alreadyPinned =
  process.execPath.replace(/\\/g, "/").toLowerCase() === pinned.replace(/\\/g, "/").toLowerCase();
if (alreadyPinned && isAtLeast(process.versions.node, MINIMUM)) {
  process.env.SCHOLARFLOW_NODE = pinned;
  const result = spawnSync(process.execPath, argv, {
    stdio: "inherit",
    env: process.env,
    shell: false,
  });
  process.exit(result.status ?? 1);
}

process.env.SCHOLARFLOW_NODE = pinned;
const result = spawnSync(pinned, argv, {
  stdio: "inherit",
  env: process.env,
  shell: false,
  cwd: join(dirname(fileURLToPath(import.meta.url)), ".."),
});
process.exit(result.status ?? 1);
