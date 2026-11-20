import assert from "node:assert/strict";
import { delimiter, join, resolve } from "node:path";
import test from "node:test";
import { resolveScholarPython } from "../harness/python-runtime.mjs";

const exe = process.platform === "win32" ? "python.exe" : "python3";
const first = resolve("runtime-test-old", exe);
const second = resolve("runtime-test-new", exe);
const pathValue = [resolve("runtime-test-old"), resolve("runtime-test-new")].join(delimiter);
const exists = (candidate) => candidate === first || candidate === second;
const probe = (candidate) => candidate === first ? "3.9.12" : "3.11.8";

test("configured compatible Python takes priority", () => {
  assert.equal(resolveScholarPython({ env: { SCHOLARFLOW_PYTHON: second, PATH: pathValue }, exists, probe }), second);
});

test("PATH lookup skips an old Python and picks a compatible executable", () => {
  assert.equal(resolveScholarPython({ env: { PATH: pathValue }, exists, probe }), second);
});

test("an invalid explicit override does not silently select a different Python", () => {
  assert.throws(() => resolveScholarPython({ env: { SCHOLARFLOW_PYTHON: first, PATH: pathValue }, exists, probe }), /Python 3\.11/);
});

test("missing and too-old executables fail clearly", () => {
  assert.throws(() => resolveScholarPython({ env: { PATH: join("no", "python") }, exists: () => false, probe }), /Python 3\.11/);
});
