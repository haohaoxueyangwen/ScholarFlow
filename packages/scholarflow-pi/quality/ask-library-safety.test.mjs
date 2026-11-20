import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { parseLibraryRequest } from "../harness/library-request.mjs";

const script = resolve(dirname(fileURLToPath(import.meta.url)), "../scripts/ask-library.mjs");

test("library CLI delegates to the explicit request validator", () => {
  const source = readFileSync(script, "utf8");
  assert.match(source, /parseLibraryRequest\(args\)/);
});

test("missing question or project is rejected without launching a model", () => {
  assert.throws(() => parseLibraryRequest([]), /Bounded project\/question required/);
  assert.throws(() => parseLibraryRequest(["--stage", "stage-A"]), /Bounded project\/question required/);
  assert.throws(() => parseLibraryRequest(["--stage", "stage-A", "--question", "   "]), /Bounded project\/question required/);
  assert.throws(() => parseLibraryRequest(["--stage", "stage-A", "--stage", "stage-B", "--question", "test"]), /Bounded project\/question required/);
  assert.deepEqual(parseLibraryRequest(["--stage", "stage-A", "--question", "Synthetic question"]), {
    qualification: false, stage: "stage-A", question: "Synthetic question",
  });
});
