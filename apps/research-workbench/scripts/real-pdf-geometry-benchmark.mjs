import { createHash } from "node:crypto";
import { readFile, mkdir, writeFile } from "node:fs/promises";
import { basename, dirname, resolve } from "node:path";
import { performance } from "node:perf_hooks";
import { getDocument, Util, version } from "pdfjs-dist/legacy/build/pdf.mjs";
import { normalizeRects } from "../app/lib/pdf-anchors.ts";

const MAX_INPUTS = 8;
const MAX_PAGES = 100;
const MAX_REPEATS = 5;
const MAX_WARMUPS = 2;
const DEFAULT_MAX_BYTES = 50 * 1024 * 1024;

function usage() {
  return [
    "Usage: node --experimental-strip-types real-pdf-geometry-benchmark.mjs --input <pdf> [--input <pdf> ...] [options]",
    "Options:",
    `  --max-pages <1-${MAX_PAGES}>   Process all pages up to this bound; larger files are evenly sampled (default 100).`,
    `  --repeats <1-${MAX_REPEATS}>       Measured passes per PDF (default 3).`,
    `  --warmups <0-${MAX_WARMUPS}>       Unreported warm-up passes per PDF (default 1).`,
    "  --max-bytes <n>            Reject inputs larger than this size (default 52428800).",
    "  --require-text             Fail when a sampled PDF has no extractable text.",
    "  --output <json-path>       Write the metadata-only result JSON as well as printing it.",
    "  --help                     Show this message.",
  ].join("\n");
}

function positiveInteger(value, name, maximum = Number.MAX_SAFE_INTEGER, allowZero = false) {
  const number = Number(value);
  if (!Number.isInteger(number) || number < (allowZero ? 0 : 1) || number > maximum) {
    throw new Error(`${name} is outside its permitted integer range`);
  }
  return number;
}

function parseArgs(argv) {
  const options = {
    inputs: [],
    maxPages: MAX_PAGES,
    repeats: 3,
    warmups: 1,
    maxBytes: DEFAULT_MAX_BYTES,
    requireText: false,
    output: null,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--help") {
      options.help = true;
      continue;
    }
    if (argument === "--require-text") {
      options.requireText = true;
      continue;
    }
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) throw new Error(`${argument} requires a value`);
    index += 1;
    if (argument === "--input") options.inputs.push(value);
    else if (argument === "--max-pages") options.maxPages = positiveInteger(value, argument, MAX_PAGES);
    else if (argument === "--repeats") options.repeats = positiveInteger(value, argument, MAX_REPEATS);
    else if (argument === "--warmups") options.warmups = positiveInteger(value, argument, MAX_WARMUPS, true);
    else if (argument === "--max-bytes") options.maxBytes = positiveInteger(value, argument);
    else if (argument === "--output") options.output = resolve(value);
    else throw new Error(`unknown option: ${argument}`);
  }
  if (options.help) return options;
  if (options.inputs.length === 0) throw new Error("at least one --input is required");
  if (options.inputs.length > MAX_INPUTS) throw new Error(`at most ${MAX_INPUTS} inputs are permitted`);
  return options;
}

function samplePages(pageCount, maximum) {
  if (pageCount <= maximum) return Array.from({ length: pageCount }, (_, index) => index + 1);
  if (maximum === 1) return [1];
  return Array.from(
    { length: maximum },
    (_, index) => Math.round((index * (pageCount - 1)) / (maximum - 1)) + 1,
  );
}

function round(value, digits = 3) {
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}

function normalizedTextGeometry(items, viewport) {
  const pageRect = { left: 0, top: 0, width: viewport.width, height: viewport.height };
  const sourceRects = [];
  let roundTripChecks = 0;
  let roundTripFailures = 0;
  let invalidItems = 0;
  let maxRoundTripErrorPt = 0;
  const exclusions = {
    malformedTransform: 0,
    nonPositiveDimensions: 0,
    nonFiniteTransformedMatrix: 0,
    degenerateAxes: 0,
    invalidEnvelope: 0,
  };

  for (const item of items) {
    const transform = Array.from(item.transform ?? []);
    if (transform.length !== 6 || !transform.every(Number.isFinite)) {
      invalidItems += 1;
      exclusions.malformedTransform += 1;
      continue;
    }
    if (!Number.isFinite(item.width) || !Number.isFinite(item.height) || item.width <= 0 || item.height <= 0) {
      invalidItems += 1;
      exclusions.nonPositiveDimensions += 1;
      continue;
    }

    const roundTrip = viewport.convertToViewportPoint(transform[4], transform[5]);
    const recovered = viewport.convertToPdfPoint(roundTrip[0], roundTrip[1]);
    const errorPt = Math.hypot(recovered[0] - transform[4], recovered[1] - transform[5]);
    roundTripChecks += 1;
    maxRoundTripErrorPt = Math.max(maxRoundTripErrorPt, errorPt);
    if (!Number.isFinite(errorPt) || errorPt > 1e-4) roundTripFailures += 1;

    const matrix = Util.transform(viewport.transform, transform);
    if (!Array.from(matrix).every(Number.isFinite)) {
      invalidItems += 1;
      exclusions.nonFiniteTransformedMatrix += 1;
      continue;
    }
    const directionLength = Math.hypot(matrix[0], matrix[1]);
    const fontAxisLength = Math.hypot(matrix[2], matrix[3]);
    if (directionLength <= 0 || fontAxisLength <= 0) {
      invalidItems += 1;
      exclusions.degenerateAxes += 1;
      continue;
    }

    // This is a text-item envelope, not a character-accurate DOM selection box.
    const advanceLength = item.width * viewport.scale;
    const fontHeight = item.height * viewport.scale;
    const advance = [
      (matrix[0] / directionLength) * advanceLength,
      (matrix[1] / directionLength) * advanceLength,
    ];
    const fontAxis = [
      (matrix[2] / fontAxisLength) * fontHeight,
      (matrix[3] / fontAxisLength) * fontHeight,
    ];
    const origin = [matrix[4], matrix[5]];
    const corners = [
      origin,
      [origin[0] + advance[0], origin[1] + advance[1]],
      [origin[0] + advance[0] + fontAxis[0], origin[1] + advance[1] + fontAxis[1]],
      [origin[0] + fontAxis[0], origin[1] + fontAxis[1]],
    ];
    const xs = corners.map(([x]) => x);
    const ys = corners.map(([, y]) => y);
    const rect = {
      left: Math.min(...xs),
      top: Math.min(...ys),
      width: Math.max(...xs) - Math.min(...xs),
      height: Math.max(...ys) - Math.min(...ys),
    };
    if (
      Object.values(rect).every(Number.isFinite) && rect.width > 0 && rect.height > 0
    ) sourceRects.push(rect);
    else {
      invalidItems += 1;
      exclusions.invalidEnvelope += 1;
    }
  }

  return {
    normalized: normalizeRects(sourceRects.slice(0, 300), pageRect),
    sourceRectCount: sourceRects.length,
    invalidItems,
    exclusions,
    roundTripChecks,
    roundTripFailures,
    maxRoundTripErrorPt,
  };
}

function sameNormalizedGeometry(left, right) {
  if (left.length !== right.length) return false;
  const fields = ["x", "y", "width", "height"];
  return left.every((rect, index) =>
    fields.every((field) => Math.abs(rect[field] - right[index][field]) <= 1e-5),
  );
}

async function runPass(data, options) {
  // PDF.js may transfer ownership of the typed-array buffer to its worker.
  const passData = data.slice();
  const startedAt = performance.now();
  const rssStart = process.memoryUsage().rss;
  let rssPeak = rssStart;
  let loadingTask;
  let document;
  let openMs = 0;
  let getPageMs = 0;
  let getTextContentMs = 0;
  let geometryMs = 0;
  let textItemCount = 0;
  let textCharacterCount = 0;
  let nonEmptyPages = 0;
  let pagesWithoutText = 0;
  let normalizedRectCount = 0;
  let roundTripChecks = 0;
  let roundTripFailures = 0;
  let invalidGeometryItems = 0;
  const geometryExclusions = {};
  let scaleInvariantPages = 0;
  let scaleInvariantFailures = 0;
  let maxRoundTripErrorPt = 0;
  let geometries = [];
  let selectedPages = [];
  let totalPages = 0;

  try {
    const openStartedAt = performance.now();
    loadingTask = getDocument({
      data: passData,
      disableAutoFetch: true,
      disableRange: true,
      disableStream: true,
      isEvalSupported: false,
      useSystemFonts: true,
    });
    document = await loadingTask.promise;
    openMs = performance.now() - openStartedAt;
    totalPages = document.numPages;
    selectedPages = samplePages(totalPages, options.maxPages);

    for (const pageNumber of selectedPages) {
      const pageStartedAt = performance.now();
      const page = await document.getPage(pageNumber);
      getPageMs += performance.now() - pageStartedAt;
      const viewport1 = page.getViewport({ scale: 1 });
      const viewport2 = page.getViewport({ scale: 2 });
      geometries.push({
        page: pageNumber,
        rotation: page.rotate,
        widthPt: round(viewport1.width),
        heightPt: round(viewport1.height),
      });

      const textStartedAt = performance.now();
      const textContent = await page.getTextContent();
      getTextContentMs += performance.now() - textStartedAt;
      const textItems = textContent.items.filter(
        (item) => typeof item.str === "string" && item.str.trim().length > 0,
      );
      textItemCount += textItems.length;
      textCharacterCount += textItems.reduce((sum, item) => sum + item.str.length, 0);

      if (textItems.length === 0) {
        pagesWithoutText += 1;
      } else {
        nonEmptyPages += 1;
        const geometryStartedAt = performance.now();
        const firstScale = normalizedTextGeometry(textItems, viewport1);
        const secondScale = normalizedTextGeometry(textItems, viewport2);
        geometryMs += performance.now() - geometryStartedAt;
        normalizedRectCount += firstScale.normalized.length;
        roundTripChecks += firstScale.roundTripChecks + secondScale.roundTripChecks;
        roundTripFailures += firstScale.roundTripFailures + secondScale.roundTripFailures;
        invalidGeometryItems += firstScale.invalidItems + secondScale.invalidItems;
        for (const [reason, count] of Object.entries(firstScale.exclusions)) {
          geometryExclusions[reason] = (geometryExclusions[reason] ?? 0) + count;
        }
        for (const [reason, count] of Object.entries(secondScale.exclusions)) {
          geometryExclusions[reason] = (geometryExclusions[reason] ?? 0) + count;
        }
        maxRoundTripErrorPt = Math.max(
          maxRoundTripErrorPt,
          firstScale.maxRoundTripErrorPt,
          secondScale.maxRoundTripErrorPt,
        );
        scaleInvariantPages += 1;
        if (!sameNormalizedGeometry(firstScale.normalized, secondScale.normalized)) {
          scaleInvariantFailures += 1;
        }
      }
      page.cleanup();
      rssPeak = Math.max(rssPeak, process.memoryUsage().rss);
    }
  } finally {
    if (document) await document.destroy();
    else if (loadingTask) await loadingTask.destroy();
  }

  const elapsedMs = performance.now() - startedAt;
  rssPeak = Math.max(rssPeak, process.memoryUsage().rss);
  return {
    sampledPages: selectedPages.length,
    selectedPages,
    totalPages,
    pageGeometries: geometries,
    textItemCount,
    textCharacterCount,
    pagesWithText: nonEmptyPages,
    pagesWithoutText,
    normalizedRectCount,
    roundTripChecks,
    roundTripFailures,
    invalidGeometryItems,
    geometryExclusions,
    maxRoundTripErrorPt: round(maxRoundTripErrorPt, 8),
    scaleInvariantPages,
    scaleInvariantFailures,
    timingMs: {
      open: round(openMs),
      getPageTotal: round(getPageMs),
      textContentTotal: round(getTextContentMs),
      geometryTotal: round(geometryMs),
      total: round(elapsedMs),
      sampledPagesPerSecond: elapsedMs > 0 ? round((selectedPages.length * 1000) / elapsedMs, 2) : null,
    },
    memoryBytes: {
      rssStart: rssStart,
      rssEnd: process.memoryUsage().rss,
      rssPeakObserved: rssPeak,
    },
  };
}

function median(values) {
  if (!values.length) return null;
  const ordered = [...values].sort((left, right) => left - right);
  const middle = Math.floor(ordered.length / 2);
  return ordered.length % 2 ? ordered[middle] : (ordered[middle - 1] + ordered[middle]) / 2;
}

function summarizeRuns(runs) {
  const fields = ["open", "getPageTotal", "textContentTotal", "geometryTotal", "total", "sampledPagesPerSecond"];
  return Object.fromEntries(fields.map((field) => {
    const values = runs.map((run) => run.timingMs[field]).filter(Number.isFinite);
    return [field, {
      min: values.length ? round(Math.min(...values), 3) : null,
      median: values.length ? round(median(values), 3) : null,
      max: values.length ? round(Math.max(...values), 3) : null,
    }];
  }));
}

async function benchmarkInput(input, options) {
  const path = resolve(input);
  const dataBuffer = await readFile(path);
  if (dataBuffer.length > options.maxBytes) throw new Error(`${basename(path)} exceeds --max-bytes`);
  if (dataBuffer.subarray(0, 5).toString("ascii") !== "%PDF-") {
    throw new Error(`${basename(path)} does not have a PDF signature`);
  }
  const data = new Uint8Array(dataBuffer);
  const sha256 = createHash("sha256").update(dataBuffer).digest("hex");
  const warmupRuns = [];
  for (let index = 0; index < options.warmups; index += 1) {
    warmupRuns.push(await runPass(data, options));
  }
  const runs = [];
  for (let index = 0; index < options.repeats; index += 1) {
    runs.push(await runPass(data, options));
  }

  const invariantFailures = runs.reduce((sum, run) => sum + run.roundTripFailures + run.scaleInvariantFailures, 0);
  const roundTripFailures = runs.reduce((sum, run) => sum + run.roundTripFailures, 0);
  const scaleInvariantFailures = runs.reduce((sum, run) => sum + run.scaleInvariantFailures, 0);
  const invalidGeometryItems = runs.reduce((sum, run) => sum + run.invalidGeometryItems, 0);
  const noText = runs.some((run) => run.textItemCount === 0);
  const failedForNoText = options.requireText && noText;
  const hasTextWithoutRects = runs.some((run) => run.textItemCount > 0 && run.normalizedRectCount === 0);
  const status = invariantFailures || failedForNoText || hasTextWithoutRects
    ? "fail"
    : noText
      ? "warn"
      : invalidGeometryItems
        ? "warn"
        : "pass";
  const representative = runs[0];

  return {
    fixture: basename(path),
    sha256,
    bytes: dataBuffer.length,
    status,
    textStatus: representative.textItemCount > 0 ? "text_extracted" : "no_text_extracted",
    totalPages: representative.totalPages,
    sampledPages: representative.selectedPages,
    pageGeometries: representative.pageGeometries,
    textItemCountPerRun: representative.textItemCount,
    textCharacterCountPerRun: representative.textCharacterCount,
    pagesWithTextPerRun: representative.pagesWithText,
    pagesWithoutTextPerRun: representative.pagesWithoutText,
    normalizedRectCountPerRun: representative.normalizedRectCount,
    roundTripChecksPerRun: representative.roundTripChecks,
    roundTripFailures,
    scaleInvariantPagesPerRun: representative.scaleInvariantPages,
    scaleInvariantFailures,
    invalidGeometryItems,
    geometryExclusionsPerRun: representative.geometryExclusions,
    maxRoundTripErrorPtPerRun: representative.maxRoundTripErrorPt,
    warmups: warmupRuns.length,
    repeats: runs,
    timingSummaryMs: summarizeRuns(runs),
    peakObservedRssBytes: Math.max(...[...warmupRuns, ...runs].map((run) => run.memoryBytes.rssPeakObserved)),
    caveat: "PDF.js parsing/text geometry only; text-item envelopes approximate DOM selections and do not measure browser rendering, OCR quality, or semantic correctness.",
  };
}

async function main() {
  let options;
  try {
    options = parseArgs(process.argv.slice(2));
  } catch (error) {
    process.stderr.write(`${error.message}\n${usage()}\n`);
    process.exitCode = 2;
    return;
  }
  if (options.help) {
    process.stdout.write(`${usage()}\n`);
    return;
  }

  const results = [];
  let failed = false;
  let warned = false;
  for (const input of options.inputs) {
    try {
      const result = await benchmarkInput(input, options);
      failed ||= result.status === "fail";
      warned ||= result.status === "warn";
      results.push(result);
    } catch (error) {
      failed = true;
      results.push({
        fixture: basename(resolve(input)),
        status: "fail",
        errorType: error?.name ?? "Error",
      });
    }
  }

  const report = {
    schemaVersion: 1,
    benchmark: "offline_real_pdfjs_text_geometry",
    generatedAtUtc: new Date().toISOString(),
    environment: {
      node: process.version,
      pdfjs: version,
      networkRequests: 0,
      modelCalls: 0,
      extractedTextEmitted: false,
    },
    bounds: {
      maxInputs: MAX_INPUTS,
      maxPagesPerInput: options.maxPages,
      maxBytesPerInput: options.maxBytes,
      warmups: options.warmups,
      repeats: options.repeats,
      requireText: options.requireText,
    },
    results,
    status: failed ? "fail" : warned ? "warn" : "pass",
  };
  const rendered = `${JSON.stringify(report, null, 2)}\n`;
  process.stdout.write(rendered);
  if (options.output) {
    await mkdir(dirname(options.output), { recursive: true });
    await writeFile(options.output, rendered, "utf8");
  }
  if (failed) process.exitCode = 1;
}

main().catch((error) => {
  process.stderr.write(`benchmark failed: ${error?.name ?? "Error"}\n`);
  process.exitCode = 1;
});
