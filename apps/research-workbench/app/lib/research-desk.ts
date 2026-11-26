/** Small transport and state contracts shared by the Pi-native reading desk. */
export type DeskScope = { stage: string; id: string; generation: number };

export type DeskPane = "projects" | "document" | "pi";
export type DeskLayout = "wide" | "split" | "stack";
export const DESK_WIDE = "(min-width: 1160px)";
export const DESK_SPLIT = "(min-width: 780px)";

type PollClock = { schedule: (callback: () => void, delay: number) => unknown; cancel: (handle: unknown) => void };

/** Wait for each request to settle before scheduling the next one. */
export function startSettledPoll(run: () => Promise<void>, onError: (error: unknown) => void, delay = 2000, clock: PollClock = {
  schedule: (callback, milliseconds) => globalThis.setTimeout(callback, milliseconds),
  cancel: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
}) {
  let stopped = false;
  let handle: unknown;
  const tick = async () => {
    if (stopped) return;
    try { await run(); }
    catch (error) { if (!stopped) onError(error); }
    finally { if (!stopped) handle = clock.schedule(() => void tick(), delay); }
  };
  handle = clock.schedule(() => void tick(), delay);
  return () => { stopped = true; clock.cancel(handle); };
}

export function deskPanels(layout: DeskLayout, pane: DeskPane, reading: boolean, projectsOpen: boolean, piOpen: boolean) {
  const active = reading && pane === "projects" ? "document" : pane;
  if (layout === "stack") return { active, projects: active === "projects", pi: active === "pi", document: active === "document" };
  const projects = !reading && projectsOpen;
  const both = layout === "split" && projects && piOpen;
  return { active, projects: projects && (!both || active !== "pi"), pi: piOpen && (!both || active === "pi"), document: true };
}

export function fitPaperWidth(width: number, height: number, aspect: number, fit: "width" | "page", zoom: number) {
  const base = fit === "page" ? Math.min(width, height * aspect) : width;
  return Math.max(1, Math.floor(base * zoom));
}

export function isSameScope(current: DeskScope, captured: DeskScope): boolean {
  return current.stage === captured.stage && current.id === captured.id && current.generation === captured.generation;
}

export function deskLocation(search: string, projects: readonly { stage_id: string }[]): { stage: string; id: string } {
  const params = new URLSearchParams(search);
  const stage = params.get("project") || "";
  const id = params.get("workspace") || "";
  if (!projects.some(project => project.stage_id === stage)) return { stage: "", id: "" };
  return { stage, id: /^workspace-[a-f0-9]{32}$/.test(id) ? id : "" };
}

export function replaceDeskLocation(stage: string, id = "") {
  const url = new URL(window.location.href);
  if (stage) url.searchParams.set("project", stage); else url.searchParams.delete("project");
  if (stage && id) url.searchParams.set("workspace", id); else url.searchParams.delete("workspace");
  window.history.replaceState(window.history.state, "", url);
}

export function draftKey(project: string, workspace: string): string {
  return `scholarflow:reading-desk:draft:${encodeURIComponent(project)}:${encodeURIComponent(workspace)}`;
}

export function readDraft(storage: Storage | null, project: string, workspace: string): { value: string; available: boolean } {
  try {
    if (!storage) throw new Error("storage unavailable");
    return { value: storage.getItem(draftKey(project, workspace)) || "", available: true };
  } catch { return { value: "", available: false }; }
}

export function writeDraft(storage: Storage | null, project: string, workspace: string, value: string): boolean {
  try {
    if (!storage) throw new Error("storage unavailable");
    if (value) storage.setItem(draftKey(project, workspace), value);
    else storage.removeItem(draftKey(project, workspace));
    return true;
  } catch { return false; }
}

export function browserStorage(): Storage | null {
  try { return window.localStorage; } catch { return null; }
}

export type ReviewDraft = { proposalId: string; proposalHash: string; title: string; content: string; requestId: string; submitted: boolean };

export function reviewDraftKey(project: string, workspace: string, proposalId: string, proposalHash: string): string {
  return `scholarflow:reading-desk:review:${[project, workspace, proposalId, proposalHash].map(encodeURIComponent).join(":")}`;
}

export function readReviewDraft(storage: Storage | null, project: string, workspace: string, proposalId: string, proposalHash: string): ReviewDraft | null {
  try {
    const raw = storage?.getItem(reviewDraftKey(project, workspace, proposalId, proposalHash));
    if (!raw) return null;
    const value = JSON.parse(raw) as ReviewDraft;
    return value.proposalId === proposalId && value.proposalHash === proposalHash && typeof value.title === "string" && typeof value.content === "string"
      && /^revision-[a-f0-9]{32}$/.test(value.requestId) && typeof value.submitted === "boolean" ? value : null;
  } catch { return null; }
}

export function writeReviewDraft(storage: Storage | null, project: string, workspace: string, draft: ReviewDraft | null, proposalId: string, proposalHash: string): boolean {
  try {
    if (!storage) throw new Error("storage unavailable");
    const key = reviewDraftKey(project, workspace, proposalId, proposalHash);
    if (draft) storage.setItem(key, JSON.stringify(draft)); else storage.removeItem(key);
    return true;
  } catch { return false; }
}

export function previewMatches(preview: { changes: { project_name?: string; title?: string; goal?: string } } | null, name: string, title: string, goal: string): boolean {
  return Boolean(preview && preview.changes.project_name === name && preview.changes.title === title && preview.changes.goal === goal);
}

export function canAskKnowledge(workspace: { knowledge_available: boolean }): boolean {
  return workspace.knowledge_available === true;
}

export function uniquePdfSources<T extends { import_id?: string; anchor?: { page_start?: number } }>(sources: T[]): T[] {
  const seen = new Set<string>();
  return sources.filter((source) => {
    const page = source.anchor?.page_start;
    if (!source.import_id || !/^pdf_[a-f0-9]{24}$/.test(source.import_id) || !Number.isSafeInteger(page) || (page || 0) < 1) return false;
    const key = `${source.import_id}:${page}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export function turnStateText(status: string, error?: string): string {
  if (status === "interrupted") return error || "任务已中断；请检查连接后重新发送。";
  if (status === "failed") return error || "这一轮失败了；请检查连接后重试。";
  if (status === "queued") return "正在等待 Pi 处理…";
  if (status === "running") return "Pi 正在阅读…";
  return "状态未知，请刷新后重试。";
}

export function researchApiBase(): string {
  const configured = process.env.NEXT_PUBLIC_SCHOLARFLOW_RESEARCH_URL || "http://127.0.0.1:8787";
  const url = new URL(configured);
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(url.hostname) || url.username || url.password || url.pathname !== "/" || url.search || url.hash) {
    throw new Error("Research API must be a loopback HTTP origin");
  }
  // Cookies are site-bound. Use the same loopback hostname as the web page.
  if (typeof window !== "undefined" && ["127.0.0.1", "localhost"].includes(window.location.hostname)) url.hostname = window.location.hostname;
  return url.origin;
}

export async function researchRequest(
  base: string,
  path: string,
  init: RequestInit = {},
  fetcher: typeof fetch = fetch,
): Promise<Response> {
  const url = new URL(base);
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(url.hostname) || url.username || url.password || url.pathname !== "/" || url.search || url.hash) {
    throw new Error("Research API must be a loopback HTTP origin");
  }
  if (!path.startsWith("/") || path.startsWith("//")) throw new Error("Invalid research API path");
  const session = await fetcher(`${url.origin}/session`, { credentials: "include", cache: "no-store" });
  if (!session.ok) throw new Error("本地研究会话无法连接；请检查启动器和网页地址。");
  const headers = new Headers(init.headers);
  if ((init.method || "GET").toUpperCase() === "POST") headers.set("X-ScholarFlow-Intent", "confirm");
  return fetcher(`${url.origin}${path}`, { ...init, headers, credentials: "include", cache: "no-store" });
}
export function readerPageAuthorized(ranges: number[][], page: number, pageCount: number) {
  return Number.isInteger(page) && page > 0 && page <= pageCount && ranges.some(([first, last]) => first <= page && page <= last);
}

export function buildDocumentDraft(
  existing: string, excerpt: string, page: number, ranges: number[][], pageCount: number,
) {
  const quote = excerpt.trim();
  if (!readerPageAuthorized(ranges, page, pageCount) || !quote || quote.length > 1500) return null;
  const combined = `${existing}${existing ? "\n\n" : ""}摘录，供接下来讨论：\n【当前 PDF · 第 ${page} 页】\n${quote}`;
  return combined.length <= 6000 ? combined : null;
}

