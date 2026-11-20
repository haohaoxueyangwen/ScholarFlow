function defaultApiBase() {
  if (typeof window === "undefined") return "http://localhost:8765/api";
  const host = window.location.hostname;
  const loopbackHost = host === "127.0.0.1" || host === "localhost" || host === "[::1]"
    ? host
    : "localhost";
  return `http://${loopbackHost}:8765/api`;
}

export const API_BASE =
  process.env.NEXT_PUBLIC_SCHOLARFLOW_API_URL || defaultApiBase();

export class WorkbenchApiError extends Error {
  readonly status: number;
  readonly requestId: string;

  constructor(message: string, status: number, requestId = "") {
    super(requestId ? `${message}（请求 ${requestId}）` : message);
    this.name = "WorkbenchApiError";
    this.status = status;
    this.requestId = requestId;
  }
}

async function decode<T>(response: Response): Promise<T> {
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const body = payload as { error?: { message?: string } } | null;
    throw new WorkbenchApiError(
      body?.error?.message || `Workbench request failed (${response.status})`,
      response.status,
      response.headers.get("X-ScholarFlow-Request-Id") || "",
    );
  }
  return payload as T;
}

type RequestOptions = { signal?: AbortSignal; timeoutMs?: number };

async function request<T>(path: string, init: RequestInit, options: RequestOptions = {}): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort(options.signal?.reason);
  options.signal?.addEventListener("abort", abort, { once: true });
  if (options.signal?.aborted) abort();
  const timer = setTimeout(() => controller.abort(new Error("请求超时，请检查连接；写入操作请先查询回执再重试。")), options.timeoutMs ?? 20000);
  try {
    return await decode<T>(await fetch(`${API_BASE}${path}`, { credentials: "include", cache: "no-store", ...init, signal: controller.signal }));
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener("abort", abort);
  }
}

export async function apiGet<T>(path: string, options?: RequestOptions): Promise<T> {
  return request<T>(path, {}, options);
}

export async function apiDownload(path: string, fallbackName: string): Promise<void> {
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    cache: "no-store",
  });
  if (!response.ok) {
    await decode<never>(response);
    return;
  }
  const disposition = response.headers.get("Content-Disposition") || "";
  const match = /filename="([^"]+)"/.exec(disposition);
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = match?.[1] || fallbackName;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export async function apiPost<T>(
  path: string,
  body: Record<string, unknown>,
  options?: RequestOptions,
): Promise<T> {
  return request<T>(path, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-ScholarFlow-Intent": "confirm",
    },
    body: JSON.stringify(body),
  }, { timeoutMs: 120000, ...options });
}

export async function apiUploadPdf<T>(file: File, projectId?: string): Promise<T> {
  const response = await fetch(`${API_BASE}/reader/imports`, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/pdf",
      "X-ScholarFlow-Intent": "confirm",
      "X-ScholarFlow-Filename": encodeURIComponent(file.name),
      ...(projectId ? { "X-ScholarFlow-Project": encodeURIComponent(projectId) } : {}),
    },
    body: file,
  });
  return decode<T>(response);
}

export async function apiImportPdfFromUrl<T>(
  pdfUrl: string,
  options: { title?: string; authors?: string[]; year?: number | null; doi?: string; projectId?: string } = {},
): Promise<T> {
  return request<T>("/reader/imports/from-url", {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-ScholarFlow-Intent": "confirm",
    },
    body: JSON.stringify({
      pdf_url: pdfUrl,
      title: options.title || "",
      authors: options.authors || [],
      year: options.year ?? null,
      doi: options.doi || "",
      project_stage_ids: options.projectId ? [options.projectId] : [],
    }),
  }, { timeoutMs: 120000 });
}

export async function establishWorkbenchSession(): Promise<void> {
  await apiGet("/session");
}

export type ReaderTranslationJob = {
  task_id: string;
  status: "queued" | "running" | "cancelling" | "completed" | "failed" | "cancelled" | "interrupted";
  translation?: string;
  error_code?: string;
  source_sha256?: string;
};

export class ReaderTranslationTerminalError extends Error {}

async function waitForReaderTranslation(taskId: string, signal: AbortSignal, onStatus?: (job: ReaderTranslationJob) => void): Promise<ReaderTranslationJob> {
  const path = `/reader/translations/${encodeURIComponent(taskId)}`;
  const cancelServerTask = () => { void apiPost<ReaderTranslationJob>(`${path}/cancel`, {}, { timeoutMs: 12000 }).catch(() => {}); };
  const cancelListener = () => cancelServerTask();
  signal.addEventListener("abort", cancelListener, { once: true });
  try {
    const deadline = Date.now() + 210000;
    while (!signal.aborted && Date.now() < deadline) {
      const job = await apiGet<ReaderTranslationJob>(path, { signal, timeoutMs: 20000 });
      onStatus?.(job);
      if (["completed", "failed", "cancelled", "interrupted"].includes(job.status)) {
        if (job.status !== "completed") throw new ReaderTranslationTerminalError(`翻译任务${job.status}：${job.error_code || "请重新选择原文后再试"}`);
        return job;
      }
      await new Promise<void>((resolve, reject) => {
        const timer = setTimeout(resolve, 750);
        signal.addEventListener("abort", () => { clearTimeout(timer); reject(signal.reason || new Error("翻译已取消")); }, { once: true });
      });
    }
    if (!signal.aborted) cancelServerTask();
    throw new Error(signal.aborted ? "翻译已取消。" : "翻译状态查询超时；可在阅读器中恢复查看。");
  } finally { signal.removeEventListener("abort", cancelListener); }
}

export async function apiRunReaderTranslation(source: string, taskId: string, signal: AbortSignal, onStatus?: (job: ReaderTranslationJob) => void): Promise<ReaderTranslationJob> {
  let created: ReaderTranslationJob;
  try {
    created = await apiGet<ReaderTranslationJob>(`/reader/translations/${encodeURIComponent(taskId)}`, { signal, timeoutMs: 12000 });
  } catch (caught) {
    if (!(caught instanceof WorkbenchApiError) || caught.status !== 404) throw caught;
    created = await apiPost<ReaderTranslationJob>("/reader/translations", { source, task_id: taskId }, { signal, timeoutMs: 20000 });
  }
  onStatus?.(created);
  return waitForReaderTranslation(created.task_id, signal, onStatus);
}

export function apiResumeReaderTranslation(taskId: string, signal: AbortSignal, onStatus?: (job: ReaderTranslationJob) => void): Promise<ReaderTranslationJob> {
  return waitForReaderTranslation(taskId, signal, onStatus);
}

export async function apiRunKnowledgeSync<T>(planId: string, allowDeletes: boolean, onStatus?: (job: Record<string, unknown>) => void): Promise<T> {
  const path = `/knowledge/sync-plans/${encodeURIComponent(planId)}`;
  let job = await apiPost<Record<string, unknown>>(`${path}/apply`, { allow_deletes: allowDeletes, wait: false });
  const deadline = Date.now() + 900000;
  while (job.status !== "applied") {
    onStatus?.(job);
    if (!["queued", "running"].includes(String(job.status))) throw new Error(`知识同步${job.status}：${job.error_code || "请在知识库任务列表查看并重试"}`);
    if (Date.now() > deadline) throw new Error("同步仍在后台执行，可在知识库任务列表继续查看；不要重复创建计划。");
    await new Promise((resolve) => setTimeout(resolve, 1500));
    job = await apiGet<Record<string, unknown>>(path);
  }
  return (job.receipt || job) as T;
}
