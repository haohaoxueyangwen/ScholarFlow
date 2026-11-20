/** Loopback adapter for the existing Worker build; no application logic lives here. */
import { createServer } from "node:http";
import { readFile, realpath, stat, cp, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, extname, resolve, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";

const appRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const mime = { ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".html": "text/html", ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".jpg": "image/jpeg", ".webp": "image/webp", ".woff2": "font/woff2", ".woff": "font/woff", ".pdf": "application/pdf", ".wasm": "application/wasm" };

export async function staticResponse(request, root = resolve(appRoot, "dist/client")) {
  let pathname;
  try { pathname = decodeURIComponent(new URL(request.url).pathname); } catch { return new Response("Bad path", { status: 400 }); }
  // This is the framework's root RSC route, not a hidden static file.
  if (pathname === "/.rsc") return null;
  if (pathname.includes("\\") || pathname.includes("\0") || pathname.split("/").some((part) => part === ".." || part.startsWith("."))) return new Response("Forbidden", { status: 403 });
  const target = resolve(root, "." + pathname);
  if (!target.startsWith(resolve(root) + sep)) return null;
  try {
    const actual = await realpath(target);
    if (!actual.startsWith(await realpath(root) + sep)) return new Response("Forbidden", { status: 403 });
    const info = await stat(actual);
    if (!info.isFile()) return null;
    const headers = { "Content-Type": mime[extname(actual)] || "application/octet-stream", "Content-Length": String(info.size), "X-Content-Type-Options": "nosniff", "Cache-Control": pathname.startsWith("/assets/") ? "public, max-age=31536000, immutable" : "public, max-age=60" };
    return new Response(request.method === "HEAD" ? null : await readFile(actual), { headers });
  } catch (error) {
    if (["ENOENT", "ENOTDIR"].includes(error.code)) return null;
    throw error;
  }
}

export async function createLocalServer({ root = appRoot, instanceId = "", buildId = "" } = {}) {
  // Pin server modules and browser assets to the same immutable runtime copy.
  // Rebuilding dist must never invalidate assets referenced by a running server.
  const runtime = await mkdtemp(resolve(tmpdir(), "scholarflow-web-"));
  try {
    await cp(resolve(root, "dist"), runtime, { recursive: true });
  } catch (error) {
    await rm(runtime, { recursive: true, force: true });
    throw error;
  }
  const { default: worker } = await import(pathToFileURL(resolve(runtime, "server/index.js")).href);
  if (typeof worker?.fetch !== "function") throw new Error("Workbench Worker build is missing fetch(); rebuild before starting.");
  const server = createServer(async (incoming, outgoing) => {
    try {
      const host = incoming.headers.host || "";
      const hostMatch = /^(?:localhost|127\.0\.0\.1)(?::(\d{1,5}))?$/.exec(host);
      if (!hostMatch) { outgoing.writeHead(403); outgoing.end("Loopback only"); return; }
      const origin = new URL(`http://${host}`);
      if (hostMatch[1] && Number(hostMatch[1]) !== server.address().port) { outgoing.writeHead(403); outgoing.end("Invalid port"); return; }
      if (!["GET", "HEAD"].includes(incoming.method)) { outgoing.writeHead(405, { Allow: "GET, HEAD" }); outgoing.end("Use the governed application API for writes"); return; }
      if (new URL(incoming.url, origin).pathname === "/__scholarflow_health") {
        const body = JSON.stringify({ status: "ok", service: "scholarflow-research-web", instance_id: instanceId, build_id: buildId });
        outgoing.writeHead(200, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", "Content-Length": Buffer.byteLength(body) });
        outgoing.end(incoming.method === "HEAD" ? undefined : body);
        return;
      }
      const request = new Request(new URL(incoming.url, origin), { method: incoming.method, headers: incoming.headers });
      const assets = (request) => staticResponse(request, resolve(runtime, "client"));
      const response = await assets(request) || await worker.fetch(request, { ASSETS: { fetch: async (request) => await assets(request) || new Response("Not found", { status: 404 }) } }, { waitUntil: (promise) => void promise.catch(() => {}), passThroughOnException() {} });
      outgoing.statusCode = response.status;
      response.headers.forEach((value, name) => { if (name !== "set-cookie") outgoing.setHeader(name, value); });
      const cookies = response.headers.getSetCookie();
      if (cookies.length) outgoing.setHeader("set-cookie", cookies);
      outgoing.setHeader("X-Content-Type-Options", "nosniff");
      if (incoming.method === "HEAD" || !response.body) outgoing.end();
      else await pipeline(Readable.fromWeb(response.body), outgoing);
    } catch (error) {
      if (!outgoing.headersSent) outgoing.writeHead(500, { "Content-Type": "text/plain" });
      if (!outgoing.writableEnded) outgoing.end("Workbench rendering failed; inspect the local startup log.");
      console.error(JSON.stringify({ event: "workbench.render_error", type: error.name }));
    }
  });
  server.on("close", () => { void rm(runtime, { recursive: true, force: true }); });
  return server;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const portIndex = process.argv.indexOf("--port");
  const port = portIndex < 0 ? 3000 : Number(process.argv[portIndex + 1]);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error("Invalid local port");
  const instanceIndex = process.argv.indexOf("--instance-id");
  const buildIndex = process.argv.indexOf("--build-id");
  const instanceId = instanceIndex < 0 ? "" : process.argv[instanceIndex + 1];
  const buildId = buildIndex < 0 ? "" : process.argv[buildIndex + 1];
  if (!/^[a-zA-Z0-9-]{0,128}$/.test(instanceId || "") || !/^[a-zA-Z0-9-]{0,128}$/.test(buildId || "")) throw new Error("Invalid instance identity");
  const server = await createLocalServer({ instanceId, buildId });
  server.listen(port, "127.0.0.1", () => console.log(`ScholarFlow production: http://127.0.0.1:${port}`));
}
