import assert from "node:assert/strict";
import test from "node:test";
import {createRequire} from "node:module";
import {dirname, resolve} from "node:path";
import {fileURLToPath} from "node:url";
const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const requirePi = createRequire(resolve(packageRoot, "node_modules/@earendil-works/pi-coding-agent/package.json"));
const {createJiti} = requirePi("jiti");
const {callReadOperation, localGrant, ReadOperationFailure} = await createJiti(import.meta.url).import(resolve(packageRoot, "harness/operation-client.ts"));
const call = (pi, signal) => callReadOperation(pi, "paper.search", {}, {kind: "paper_catalog"}, localGrant("paper-read"), "h003-test", signal, 1000, {sessionManager: {getSessionId: () => "h003-session"}});

test("client rejects corrupt, null or mismatched receipts instead of treating them as success", async () => {
  for (const stdout of ["not-json", "null", "{}", JSON.stringify({protocol_version: 1, request_id: "wrong", task_id: "h003-session", operation: "paper.search", ok: true})]) {
    await assert.rejects(call({exec: async () => ({stdout, code: 0, killed: false})}), error => {
      assert.ok(error instanceof ReadOperationFailure);
      assert.ok(["protocol_error", "dependency_unavailable"].includes(error.response.error.code));
      assert.equal(error.response.receipt.execution, "unknown");
      return true;
    });
  }
});

test("pre-launch cancellation is not_started; transport kill is unknown even with exit zero", async () => {
  const controller = new AbortController(); controller.abort();
  await assert.rejects(call({exec: () => {throw new Error("must not launch");}}, controller.signal), error => {
    assert.equal(error.response.status, "cancelled"); assert.equal(error.response.receipt.execution, "not_started"); return true;
  });
  await assert.rejects(call({exec: async () => ({stdout: "", code: 0, killed: true})}), error => {
    assert.equal(error.response.status, "timeout"); assert.equal(error.response.receipt.execution, "unknown"); return true;
  });
});

test("request/session identity and trusted grants remain outside model params", async () => {
  const previous = process.env.SCHOLARFLOW_TASK_ID; delete process.env.SCHOLARFLOW_TASK_ID;
  try {
    const pi = {exec: async (_command, args) => {
      const request = JSON.parse(args[args.indexOf("--request-json") + 1]);
      const grant = JSON.parse(args[args.indexOf("--grant-json") + 1]);
      assert.equal(request.task_id, "h003-session");
      assert.equal("grant" in request, false);
      assert.equal(grant.principal, "pi-local");
      return {code: 0, killed: false, stdout: JSON.stringify({...request, ok: true, status: "ok", data: {items: []}, source_versions: [],
        receipt: {request_id: request.request_id, task_id: request.task_id, operation: request.operation, read_only: true, execution: "completed", audit: {persisted: false}}})};
    }};
    assert.deepEqual((await call(pi)).details.data, {items: []});
  } finally { if (previous === undefined) delete process.env.SCHOLARFLOW_TASK_ID; else process.env.SCHOLARFLOW_TASK_ID = previous; }
});
