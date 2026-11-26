"use client";

import { FormEvent, useEffect, useRef, useState, useSyncExternalStore } from "react";
import dynamic from "next/dynamic";
import { SafeMarkdown } from "@/app/components/safe-markdown";
import { DeskLiterature } from "@/app/components/desk-literature";
import { browserStorage, buildDocumentDraft, canAskKnowledge, DESK_SPLIT, DESK_WIDE, deskLocation, deskPanels, isSameScope, previewMatches, readDraft, readReviewDraft, readerPageAuthorized, replaceDeskLocation, researchApiBase, researchRequest, startSettledPoll, turnStateText, uniquePdfSources, writeDraft, writeReviewDraft, type DeskLayout, type DeskPane, type DeskScope, type ReviewDraft } from "@/app/lib/research-desk";

const DocumentReader = dynamic(() => import("@/app/components/document-reader").then((module) => module.DocumentReader), {
  ssr: false,
  loading: () => <p className="document-loading" role="status">正在载入论文阅读器…</p>,
});

const deskFetch = (path: string, init?: RequestInit) => researchRequest(researchApiBase(), path, init);

type Project = { stage_id: string; project_name: string; title: string; goal: string };
type Preview = { request_id: string; stage_id: string; proposal_hash: string; created: boolean; changes: { project_name?: string; title?: string; goal?: string } };
type PdfInfo = { import_id: string; filename: string; page_count: number; page_ranges: number[][]; note_blocks: number; total_page_blocks: number };
type PdfSource = { import_id?: string; anchor?: { page_start?: number } };
type Source = { reference_id?: string; title?: string; text?: string; pdf_sources?: PdfSource[] };
type NoteSource = { source_id: string; text?: string; anchor?: { page_start?: number } };
type Proposal = {
  phase: string; proposal_hash: string;
  proposal: { proposal_id: string; normalized_changes?: { title?: string; content?: string; source_snapshot?: { sources?: NoteSource[] } } };
};
type Turn = { turn_id: string; message: string; status: string; answer?: string; error?: string; sources?: Source[] };
type SavedWorkspace = { workspace_id: string; import_id: string | null; filename?: string };
type Workspace = {
  workspace_id: string; stage_id: string; project_title: string; provider: string; knowledge_available: boolean;
  pdf: PdfInfo | null; knowledge: { status: string; error_code?: string }; proposals: Proposal[]; turns: Turn[];
};

const knowledgeText: Record<string, string> = {
  not_queued: "笔记尚未入库", queued: "等待入库", running: "正在入库", indexed: "已入库",
  failed: "入库失败，笔记仍在", stale: "来源已变化", interrupted: "入库中断，可重试",
};
const proposalText: Record<string, string> = { committed: "已保存", rejected: "已拒绝或被修订", pending: "待审阅", approved_pending: "已确认，待保存", expired: "已过期", stale: "来源已变化", superseded: "已被修订" };
const providerLabel = (id: string) => id === "deepseek" ? "DeepSeek · 外部 API" : id === "scholarflow-local" ? "Qwen · 本地" : id;
const isPending = (proposal: Proposal) => ["pending", "approved_pending"].includes(proposal.phase);
function subscribeToLayout(change: () => void) {
  const queries = [window.matchMedia(DESK_WIDE), window.matchMedia(DESK_SPLIT)];
  queries.forEach(query => query.addEventListener("change", change));
  return () => queries.forEach(query => query.removeEventListener("change", change));
}
const currentLayout = (): DeskLayout => window.matchMedia(DESK_WIDE).matches ? "wide" : window.matchMedia(DESK_SPLIT).matches ? "split" : "stack";
const initialLayout = (): DeskLayout => "wide";
type DeskGlyph = "sidebar" | "inspector" | "plus" | "folder" | "file" | "chat" | "external" | "upload" | "send" | "close" | "lock";

const glyphs: Record<DeskGlyph, string> = {
  sidebar: "M3 5h18v14H3z M9 5v14 M5.5 8h1 M5.5 11h1",
  inspector: "M3 5h18v14H3z M15 5v14 M17.5 8h1 M17.5 11h1",
  plus: "M12 5v14 M5 12h14",
  folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  file: "M6 3h8l4 4v14H6z M14 3v5h4 M9 12h6 M9 16h6",
  chat: "M20 11a8 8 0 0 1-8 8H9l-4 2 1-4a8 8 0 1 1 14-6z M8 10h8 M8 13h5",
  external: "M13 4h7v7 M20 4l-9 9 M10 5H5v14h14v-5",
  upload: "M12 16V4 M8 8l4-4 4 4 M5 14v6h14v-6",
  send: "M12 19V5 M6 11l6-6 6 6",
  close: "M6 6l12 12 M18 6 6 18",
  lock: "M5 10h14v10H5z M8 10V7a4 4 0 0 1 8 0v3 M12 14v2",
};

function Glyph({ name }: { name: DeskGlyph }) {
  return <svg className="desk-glyph" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false"><path d={glyphs[name]} /></svg>;
}

async function readJson<T>(response: Response): Promise<T> {
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const message = payload && typeof payload === "object" && "error" in payload ? String((payload as { error?: { message?: string } }).error?.message || "") : "";
    throw new Error(message || `请求失败（${response.status}）`);
  }
  return payload as T;
}

export function ReadingDesk() {
  const [online, setOnline] = useState<boolean | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [stage, setStage] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [review, setReview] = useState<ReviewDraft | null>(null);
  const [reviewStorageAvailable, setReviewStorageAvailable] = useState(true);
  const [name, setName] = useState("");
  const [title, setTitle] = useState("");
  const [goal, setGoal] = useState("");
  const [pages, setPages] = useState("1");
  const [file, setFile] = useState<File | null>(null);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [saved, setSaved] = useState<SavedWorkspace[]>([]);
  const [pdfPage, setPdfPage] = useState(1);
  const [message, setMessage] = useState("");
  const [draftStorageAvailable, setDraftStorageAvailable] = useState(true);
  const [provider, setProvider] = useState("scholarflow-local");
  const [externalConsent, setExternalConsent] = useState(false);
  const [savedError, setSavedError] = useState("");
  const [connectionAttempt, setConnectionAttempt] = useState(0);
  const [pane, setPane] = useState<DeskPane>("projects");
  const layout = useSyncExternalStore(subscribeToLayout, currentLayout, initialLayout);
  const [railOpen, setRailOpen] = useState(true);
  const [assistantOpen, setAssistantOpen] = useState(false);
  const [readerOpen, setReaderOpen] = useState(false);
  const [literatureOpen, setLiteratureOpen] = useState(false);
  const [assistantView, setAssistantView] = useState<"chat" | "notes">("chat");
  const [intake, setIntake] = useState(false);
  const [creating, setCreating] = useState(false);
  const [loadingSaved, setLoadingSaved] = useState(false);
  const [savedAttempt, setSavedAttempt] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const current = useRef<DeskScope>({ stage: "", id: "", generation: 0 });
  const messageRef = useRef("");
  const previewRevision = useRef(0);
  const refreshSerial = useRef(0);
  const projectForm = useRef<HTMLDialogElement>(null);
  const projectRail = useRef<HTMLElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const thread = useRef<HTMLDivElement>(null);

  function confirmLeaveDraft() {
    if ((draftStorageAvailable || !message) && (reviewStorageAvailable || !review)) return true;
    return window.confirm("当前浏览器无法保存草稿。离开后未提交内容会丢失，仍要继续吗？");
  }

  function chooseProject(nextStage: string) {
    current.current = { stage: nextStage, id: "", generation: current.current.generation + 1 };
    setStage(nextStage); setWorkspace(null); setSaved([]); setSavedError(""); setLoadingSaved(Boolean(nextStage)); setFile(null); setPages("1");
    setMessage(""); messageRef.current = ""; setReview(null); setPreview(null); previewRevision.current++; setExternalConsent(false); setError(""); setBusy("");
    setPane(nextStage ? "document" : "projects"); setIntake(false); setAssistantView("chat"); setReaderOpen(false); setAssistantOpen(false);
    setLiteratureOpen(Boolean(nextStage));
    replaceDeskLocation(nextStage);
  }

  function showWorkspace(next: Workspace, expected: DeskScope) {
    if (!isSameScope(current.current, expected) || next.stage_id !== expected.stage) return;
    current.current = { ...expected, id: next.workspace_id };
    setWorkspace(next); setPdfPage(next.pdf?.page_ranges[0]?.[0] ?? 1); setReview(null); setExternalConsent(false);
    setIntake(false); setPane(next.pdf ? "document" : "pi"); setAssistantView("chat"); setFile(null);
    setReaderOpen(Boolean(next.pdf)); setAssistantOpen(!next.pdf);
    setLiteratureOpen(false);
    replaceDeskLocation(next.stage_id, next.workspace_id);
    const draft = readDraft(browserStorage(), next.stage_id, next.workspace_id);
    setMessage(draft.value); messageRef.current = draft.value; setDraftStorageAvailable(draft.available);
  }

  async function refreshWorkspace(scope: DeskScope) {
    const serial = ++refreshSerial.current;
    const next = await readJson<Workspace>(await deskFetch(`/workspaces/${encodeURIComponent(scope.id)}?stage_id=${encodeURIComponent(scope.stage)}`));
    if (serial === refreshSerial.current && isSameScope(current.current, scope) && next.workspace_id === scope.id && next.stage_id === scope.stage) setWorkspace(next);
  }

  async function refreshProjects() {
    const payload = await readJson<{ items: Project[] }>(await deskFetch("/projects"));
    setProjects(payload.items);
  }

  useEffect(() => {
    let alive = true;
    deskFetch("/projects").then((response) => readJson<{ items: Project[] }>(response)).then(async (payload) => {
      if (!alive) return;
      setProjects(payload.items); setOnline(true);
      const location = deskLocation(window.location.search, payload.items);
      if (!location.stage) return;
      const scope = { ...location, generation: current.current.generation + 1 };
      current.current = scope; setStage(scope.stage); setLoadingSaved(true); setPane("document");
      if (!scope.id) { setLiteratureOpen(true); return; }
      setBusy("resume");
      try {
        const next = await readJson<Workspace>(await deskFetch(`/workspaces/${encodeURIComponent(scope.id)}?stage_id=${encodeURIComponent(scope.stage)}`));
        if (alive && next.workspace_id === scope.id) showWorkspace(next, scope);
      } catch {
        if (alive && isSameScope(current.current, scope)) setError("无法恢复链接中的工作区，请从当前项目重新选择。");
      } finally { if (alive && isSameScope(current.current, scope)) setBusy(""); }
    }).catch(() => { if (alive) setOnline(false); });
    return () => { alive = false; };
  }, [connectionAttempt]);

  const pollingActive = Boolean(workspace && (workspace.turns.some((turn) => turn.status === "queued" || turn.status === "running")
    || ["queued", "running"].includes(workspace.knowledge.status)));
  useEffect(() => {
    if (!pollingActive) return;
    const scope = { ...current.current };
    return startSettledPoll(() => refreshWorkspace(scope), (reason) => {
      if (isSameScope(current.current, scope)) setError(reason instanceof Error ? reason.message : "无法刷新工作区");
    });
  }, [pollingActive, workspace?.workspace_id, workspace?.stage_id]);

  const lastTurn = workspace?.turns.at(-1);
  useEffect(() => {
    if (assistantView === "chat" && thread.current) thread.current.scrollTop = thread.current.scrollHeight;
  }, [workspace?.workspace_id, lastTurn?.turn_id, lastTurn?.status, assistantView, assistantOpen, pane]);

  useEffect(() => {
    if (creating && !projectForm.current?.open) projectForm.current?.showModal();
    if (!creating && projectForm.current?.open) projectForm.current.close();
  }, [creating]);

  useEffect(() => {
    if ((draftStorageAvailable || !message) && (reviewStorageAvailable || !review)) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [draftStorageAvailable, message, reviewStorageAvailable, review]);

  async function createPreview(event: FormEvent) {
    event.preventDefault();
    const scope = { ...current.current };
    const revision = previewRevision.current;
    const fields = { project_name: name, title, goal };
    setBusy("preview"); setError("");
    try {
      const result = await readJson<Preview>(await deskFetch("/projects/preview", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(fields),
      }));
      if (isSameScope(current.current, scope) && revision === previewRevision.current) setPreview(result);
    } catch (reason) { if (isSameScope(current.current, scope)) setError(reason instanceof Error ? reason.message : "无法生成预览"); }
    finally { if (isSameScope(current.current, scope)) setBusy(""); }
  }

  async function commitProject() {
    if (!preview || !previewMatches(preview, name, title, goal)) { setError("项目内容已变化，请重新生成预览。"); return; }
    const scope = { ...current.current };
    setBusy("commit"); setError("");
    try {
      const created = await readJson<{ stage_id: string }>(await deskFetch("/projects/commit", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: preview.request_id, reviewed_hash: preview.proposal_hash }),
      }));
      if (!isSameScope(current.current, scope)) return;
      chooseProject(created.stage_id); setCreating(false); setPreview(null); setName(""); setTitle(""); setGoal("");
      await refreshProjects();
    } catch (reason) { if (isSameScope(current.current, scope)) setError(reason instanceof Error ? reason.message : "未创建项目"); }
    finally { if (isSameScope(current.current, scope)) setBusy(""); }
  }

  useEffect(() => {
    if (!stage) return;
    let alive = true;
    deskFetch(`/workspaces?stage_id=${encodeURIComponent(stage)}`)
      .then((response) => readJson<{ items: SavedWorkspace[] }>(response))
      .then((payload) => { if (alive) { setSaved(payload.items); setSavedError(""); } })
      .catch((reason: unknown) => { if (alive) setSavedError(reason instanceof Error ? reason.message : "无法读取已有工作区"); })
      .finally(() => { if (alive) setLoadingSaved(false); });
    return () => { alive = false; };
  }, [stage, workspace?.workspace_id, savedAttempt]);

  async function resume(id: string) {
    if (!confirmLeaveDraft()) return;
    const scope = { stage, id, generation: current.current.generation + 1 };
    current.current = scope;
    setWorkspace(null); setMessage(""); messageRef.current = ""; setExternalConsent(false);
    setBusy("resume"); setError("");
    try {
      const next = await readJson<Workspace>(await deskFetch(`/workspaces/${encodeURIComponent(id)}?stage_id=${encodeURIComponent(scope.stage)}`));
      if (next.workspace_id === scope.id) showWorkspace(next, scope);
    } catch (reason) { if (isSameScope(current.current, scope)) setError(reason instanceof Error ? reason.message : "无法恢复工作区"); }
    finally { if (isSameScope(current.current, scope)) setBusy(""); }
  }

  async function openPdf() {
    if (!stage || !file || !confirmLeaveDraft()) return;
    if (!/\.pdf$/i.test(file.name) || file.size > 80_000_000 || !file.size) { setError("请选择不超过 80 MB 的非空 PDF 文件。"); return; }
    const scope = { ...current.current };
    setBusy("pdf"); setError("");
    try {
      const opened = await readJson<Workspace>(await deskFetch("/workspaces/upload", {
        method: "POST",
        headers: { "Content-Type": "application/pdf", "X-Stage-Id": stage, "X-Pages": pages, "X-Filename": encodeURIComponent(file.name), "X-Provider": provider },
        body: file,
      }));
      showWorkspace(opened, scope);
    } catch (reason) { if (isSameScope(current.current, scope)) setError(reason instanceof Error ? reason.message : "无法打开 PDF"); }
    finally { if (current.current.stage === scope.stage && current.current.generation === scope.generation) setBusy(""); }
  }

  async function openProjectChat() {
    if (!stage || !confirmLeaveDraft()) return;
    const scope = { ...current.current };
    setBusy("project-chat"); setError("");
    try {
      const opened = await readJson<Workspace>(await deskFetch("/workspaces/project", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stage_id: scope.stage, provider }),
      }));
      showWorkspace(opened, scope);
    } catch (reason) { if (isSameScope(current.current, scope)) setError(reason instanceof Error ? reason.message : "无法开启项目对话"); }
    finally { if (current.current.stage === scope.stage && current.current.generation === scope.generation) setBusy(""); }
  }

  async function openLiterature(candidate: string) {
    if (!stage || busy || !confirmLeaveDraft()) return false;
    const scope = { ...current.current };
    setBusy("literature"); setError("");
    try {
      const next = await readJson<Workspace>(await deskFetch("/literature/open", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ stage_id: stage, candidate_id: candidate, pages, provider }),
      }));
      if (!isSameScope(current.current, scope) || next.stage_id !== stage) return false;
      showWorkspace(next, scope);
      setSavedAttempt(value => value + 1);
      return true;
    } catch (reason) {
      if (isSameScope(current.current, scope)) setError(reason instanceof Error ? reason.message : "未能加入这篇论文。");
      return false;
    } finally {
      if (current.current.stage === scope.stage && current.current.generation === scope.generation) setBusy("");
    }
  }

  function setDraftMessage(value: string) {
    setMessage(value); messageRef.current = value;
    if (workspace) setDraftStorageAvailable(writeDraft(browserStorage(), workspace.stage_id, workspace.workspace_id, value));
  }

  async function send(mode: "chat" | "note" | "knowledge") {
    if (!workspace || (mode === "note" && !workspace.pdf)) return;
    const text = mode === "note" ? "请根据当前授权片段整理中文阅读笔记，并提交给我审阅。" : message.trim();
    if (!text) return;
    if (workspace.provider === "deepseek" && !externalConsent) { setError("请先确认允许将授权页和相关项目知识发送至 DeepSeek。"); return; }
    const scope = { ...current.current };
    setBusy("send"); setError("");
    try {
      const requestId = `turn-${crypto.randomUUID().replaceAll("-", "")}`;
      await readJson(await deskFetch(`/workspaces/${encodeURIComponent(scope.id)}/turns`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stage_id: scope.stage, request_id: requestId, mode, message: text, external_consent: externalConsent }),
      }));
      if (!isSameScope(current.current, scope)) return;
      if (mode === "note") setAssistantView("notes");
      if (mode !== "note" && messageRef.current.trim() === text) {
        setDraftStorageAvailable(writeDraft(browserStorage(), scope.stage, scope.id, ""));
        setMessage(""); messageRef.current = "";
      }
      await refreshWorkspace(scope);
    } catch (reason) { if (isSameScope(current.current, scope)) setError(reason instanceof Error ? reason.message : "消息没有发出"); }
    finally { if (isSameScope(current.current, scope)) setBusy(""); }
  }

  async function decide(proposal: Proposal, decision: "approve" | "reject") {
    if (!workspace) return;
    const scope = { ...current.current };
    setBusy("decide"); setError("");
    try {
      await readJson(await deskFetch(`/workspaces/${encodeURIComponent(scope.id)}/approve`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stage_id: workspace.stage_id, proposal_id: proposal.proposal.proposal_id, reviewed_hash: proposal.proposal_hash, decision }),
      }));
      await refreshWorkspace(scope);
    } catch (reason) { if (isSameScope(current.current, scope)) setError(reason instanceof Error ? reason.message : "审阅没有完成"); }
    finally { if (isSameScope(current.current, scope)) setBusy(""); }
  }

  function startReview(proposal: Proposal) {
    if (!workspace) return;
    const id = proposal.proposal.proposal_id;
    const restored = readReviewDraft(browserStorage(), workspace.stage_id, workspace.workspace_id, id, proposal.proposal_hash);
    const next = restored || { proposalId: id, proposalHash: proposal.proposal_hash, title: proposal.proposal.normalized_changes?.title || "", content: proposal.proposal.normalized_changes?.content || "", requestId: `revision-${crypto.randomUUID().replaceAll("-", "")}`, submitted: false };
    setReview(next);
    setReviewStorageAvailable(writeReviewDraft(browserStorage(), workspace.stage_id, workspace.workspace_id, next, id, proposal.proposal_hash));
  }

  function updateReview(next: ReviewDraft) {
    if (!workspace) return;
    setReview(next);
    setReviewStorageAvailable(writeReviewDraft(browserStorage(), workspace.stage_id, workspace.workspace_id, next, next.proposalId, next.proposalHash));
  }

  async function submitReview() {
    if (!workspace || !review || !review.title.trim() || !review.content.trim()) return;
    const original = workspace.proposals.find((item) => item.proposal.proposal_id === review.proposalId)?.proposal.normalized_changes;
    if (!review.submitted && (review.title.trim().length > 1000 || review.content.trim().length > 12000)) { setError("标题或正文超出长度限制。"); return; }
    if (!review.submitted && review.title.trim() === original?.title && review.content.trim() === original?.content) { setError("请先修改标题或正文，再生成修订提案。"); return; }
    const scope = { ...current.current };
    const submitted = { ...review, submitted: true };
    updateReview(submitted); setBusy("revise"); setError("");
    try {
      await readJson(await deskFetch(`/workspaces/${encodeURIComponent(scope.id)}/revise`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stage_id: scope.stage, proposal_id: submitted.proposalId, reviewed_hash: submitted.proposalHash, title: submitted.title, content: submitted.content, request_id: submitted.requestId }),
      }));
      if (!isSameScope(current.current, scope)) return;
      writeReviewDraft(browserStorage(), scope.stage, scope.id, null, submitted.proposalId, submitted.proposalHash);
      setReview(null); await refreshWorkspace(scope);
    } catch (reason) { if (isSameScope(current.current, scope)) setError(reason instanceof Error ? `${reason.message}；修订草稿已保留，可重试。` : "修订未完成，请重试。"); }
    finally { if (isSameScope(current.current, scope)) setBusy(""); }
  }

  async function retryIndex() {
    if (!workspace) return;
    const scope = { ...current.current };
    setBusy("index"); setError("");
    try {
      await readJson(await deskFetch(`/workspaces/${encodeURIComponent(scope.id)}/index`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ stage_id: scope.stage }),
      }));
      await refreshWorkspace(scope);
    } catch (reason) { if (isSameScope(current.current, scope)) setError(reason instanceof Error ? reason.message : "入库重试失败；已保存的笔记仍在。"); }
    finally { if (isSameScope(current.current, scope)) setBusy(""); }
  }

  if (online !== true) return <main className="desk-connect"><div className="desk-connect-card"><Brand /><h1>{online === null ? "正在连接工作台" : "研究服务未连接"}</h1><p>{online === null ? "正在从本机服务加载项目。" : "请启动 ScholarFlow 阅读台，确认本机服务已运行，再重试连接。"}</p>{online === false && <button className="desk-primary" type="button" onClick={() => { setOnline(null); setConnectionAttempt((value) => value + 1); }}>重新连接</button>}</div></main>;

  const project = projects.find((item) => item.stage_id === stage);
  const ranges = workspace?.pdf?.page_ranges.map(([start, end]) => start === end ? String(start) : `${start}-${end}`).join(", ");
  const turnBusy = Boolean(workspace?.turns.some((turn) => ["queued", "running"].includes(turn.status)));
  const waiting = turnBusy || ["queued", "running"].includes(workspace?.knowledge.status || "");
  const blocked = Boolean(busy) || waiting;
  const api = researchApiBase();
  const pending = workspace?.proposals.filter(isPending).length ?? 0;
  const proposals = [...(workspace?.proposals ?? [])].sort((a,b) => Number(isPending(b)) - Number(isPending(a)));
  const pdf = workspace?.pdf;
  const fileUrl = pdf ? `${api}/imports/${encodeURIComponent(pdf.import_id)}/pdf` : "";
  const pdfUrl = pdf ? `${fileUrl}#page=${pdfPage}` : "";
  const reading = Boolean(readerOpen && pdf && !intake && !literatureOpen);
  const visible = deskPanels(layout, pane, reading, railOpen, assistantOpen);
  const goToPage = (page: number) => { if (!pdf || !Number.isInteger(page) || page < 1 || page > pdf.page_count) return; setPdfPage(page); setIntake(false); setLiteratureOpen(false); setReaderOpen(true); setPane("document"); };
  function draftReaderSelection(excerpt: string, page: number) {
    if (!workspace || !pdf || !reading || page !== pdfPage || current.current.stage !== workspace.stage_id || current.current.id !== workspace.workspace_id) return "阅读范围已变化，请重新选择原文。";
    const next = buildDocumentDraft(messageRef.current, excerpt, page, pdf.page_ranges, pdf.page_count);
    if (!next) return "无法加入草稿：请核对授权页，或缩短已有草稿。";
    setDraftMessage(next); setAssistantView("chat"); setAssistantOpen(true);
    setPane("pi");
    window.requestAnimationFrame(() => composer.current?.focus({ preventScroll: true }));
    return null;
  }
  const beginProject = () => { if (confirmLeaveDraft()) { setError(""); setCreating(true); } };
  const showProjects = () => {
    setRailOpen(true); setPane("projects");
    window.requestAnimationFrame(() => projectRail.current?.querySelector<HTMLButtonElement>(".desk-list button:not(:disabled)")?.focus());
  };
  const closeProject = () => { setCreating(false); setPreview(null); previewRevision.current++; setError(""); };

  return (
    <main className="desk-shell" data-reader={reading} data-layout={layout} aria-label="ScholarFlow 科研工作台">
      <header className="desk-top">
        <div className="desk-top-leading">{reading ? <button className="desk-reader-back" type="button" onClick={() => { setReaderOpen(false); setPane("document"); }}>‹ 返回工作台</button> : <button className="desk-tool desk-desktop-tool" type="button" aria-label="项目侧栏" aria-controls="desk-projects" aria-expanded={visible.projects} title={visible.projects ? "收起项目侧栏" : "显示项目侧栏"} onClick={() => { setRailOpen(!visible.projects); if (!visible.projects) setPane("projects"); }}><Glyph name="sidebar" /></button>}<Brand /></div>
        <div className="desk-breadcrumb" title={project?.project_name}>{project ? <>{reading ? "论文阅读" : "工作台"} <span aria-hidden="true">/</span> <strong>{project.project_name || project.title}</strong></> : "科研工作台"}</div>
        <div className="desk-top-trailing"><span className="desk-status" title="本地服务已连接"><i aria-hidden="true" /><span>本地已连接</span></span><button className="desk-ask-pi desk-desktop-tool" type="button" aria-controls="desk-inspector" aria-expanded={visible.pi} title={visible.pi ? "收起 Pi 助手" : "打开 Pi 对话"} onClick={() => { setAssistantOpen(!visible.pi); setPane(visible.pi ? "document" : "pi"); if (!visible.pi) { setAssistantView("chat"); window.requestAnimationFrame(() => composer.current?.focus({ preventScroll: true })); } }}><Glyph name="chat" />问 Pi{!visible.pi && pending > 0 && <span className="desk-count" aria-label={`${pending} 条待审阅笔记`}>{pending}</span>}</button></div>
      </header>
      <nav className="desk-mobile-nav" aria-label="工作区面板">
        {([['projects', '项目', 'folder', 'desk-projects'], ['document', reading ? '论文' : '资料', 'file', 'desk-reader'], ['pi', '问 Pi', 'chat', 'desk-inspector']] as const).filter(([id]) => !reading || id !== "projects").map(([id,label,icon,target]) => <button key={id} type="button" aria-controls={target} aria-pressed={visible.active === id} onClick={() => { setPane(id); if (id === "pi") setAssistantView("chat"); }}><Glyph name={icon} />{label}{id === 'pi' && pending > 0 && <span className="desk-count">{pending}</span>}</button>)}
      </nav>
      {error && !creating && <div className="desk-alert" role="alert"><span>{error}</span><button className="desk-tool" type="button" onClick={() => setError("")} aria-label="关闭提示"><Glyph name="close" /></button></div>}
      <div className="desk-grid" data-pane={visible.active} data-rail={visible.projects ? "open" : "closed"} data-assistant={visible.pi ? "open" : "closed"} data-document={visible.document ? "open" : "closed"}>
        <aside ref={projectRail} className="desk-rail" id="desk-projects" aria-label="项目导航">
          <div className="desk-rail-head"><h2>项目</h2><button className="desk-tool" type="button" onClick={beginProject} disabled={Boolean(busy)} aria-label="新建项目" title="新建项目"><Glyph name="plus" /></button></div>
          {projects.length === 0 ? <p className="desk-muted">还没有项目。新建后即可开始。</p> : <ul className="desk-list">{projects.map((item) => <li key={item.stage_id}><button type="button" aria-current={item.stage_id === stage ? "true" : undefined} onClick={() => { if (item.stage_id === stage) setPane("document"); else if (confirmLeaveDraft()) chooseProject(item.stage_id); }} disabled={Boolean(busy)} title={item.project_name || item.title}><span className="desk-project-mark"><Glyph name="folder" /></span><span><strong>{item.project_name || item.title}</strong>{item.title && item.title !== item.project_name && <small>{item.title}</small>}</span></button></li>)}</ul>}
          {stage && <section className="desk-sessions" aria-label="当前项目的工作区"><div className="desk-rail-head"><h2>工作区</h2><button className="desk-tool" type="button" aria-label="新工作区" title="新工作区" onClick={() => { setLiteratureOpen(true); setIntake(false); setPane("document"); setError(""); }} disabled={Boolean(busy)}><Glyph name="plus" /></button></div>
            {loadingSaved ? <p className="desk-muted" role="status">正在读取工作区…</p> : savedError ? <p className="desk-muted" role="alert">读取工作区失败。 <button title={savedError} type="button" onClick={() => { setLoadingSaved(true); setSavedAttempt(value => value + 1); }}>重试</button></p> : saved.length === 0 ? <p className="desk-muted">此项目还没有工作区。</p> : <ul className="desk-list">{saved.map((item) => <li key={item.workspace_id}><button type="button" aria-current={item.workspace_id === workspace?.workspace_id ? "true" : undefined} onClick={() => { if (item.workspace_id === workspace?.workspace_id) { setLiteratureOpen(false); setIntake(false); setPane(workspace.pdf ? "document" : "pi"); if (workspace.pdf) setReaderOpen(true); else setAssistantOpen(true); } else void resume(item.workspace_id); }} disabled={Boolean(busy)} title={item.filename || "项目对话"}><span className="desk-work-mark"><Glyph name={item.import_id ? "file" : "chat"} /></span><span><strong>{item.filename || "项目对话"}</strong><small>{item.import_id ? "PDF 阅读" : "无 PDF"} · {item.workspace_id.slice(-8)}</small></span></button></li>)}</ul>}
          </section>}
        </aside>
        <section className="desk-document" id="desk-reader" aria-label="资料" onPointerDownCapture={() => { if (reading) setPane("document"); }}>
          {stage && !reading && <nav className="desk-project-tools" aria-label="项目资料入口">
            <button type="button" aria-pressed={literatureOpen} disabled={Boolean(busy)} onClick={() => { setLiteratureOpen(true); setIntake(false); setError(""); }}>找论文</button>
            <button type="button" aria-pressed={!literatureOpen && (intake || !pdf)} disabled={Boolean(busy)} onClick={() => { setLiteratureOpen(false); setIntake(true); setReaderOpen(false); setError(""); }}>本地 PDF</button>
            {pdf && <button type="button" aria-pressed={!literatureOpen && !intake} disabled={Boolean(busy)} onClick={() => { setLiteratureOpen(false); setIntake(false); setReaderOpen(false); setError(""); }}>当前工作区</button>}
          </nav>}
          {stage && <DeskLiterature key={stage} stage={stage} project={project?.project_name || project?.title || "当前项目"} active={literatureOpen && !reading} busy={Boolean(busy)} opening={busy === "literature"} pages={pages} setPages={setPages} provider={provider} setProvider={setProvider} onOpen={openLiterature} />}
          {!literatureOpen && (pdf && !intake ? <>
            <div className="desk-document-bar"><span className="desk-document-icon"><Glyph name="file" /></span><div className="desk-document-title"><h1 title={pdf.filename}>{pdf.filename}</h1><div className="desk-document-meta"><span>{pdf.page_count ?? "—"} 页</span><span><Glyph name="lock" /> Pi 授权页 {ranges}</span></div></div><a className="desk-open-file" href={pdfUrl} target="_blank" rel="noreferrer" aria-label="打开 PDF 原件（新标签页）" title="打开原件"><Glyph name="external" /><span>打开原件</span></a></div>
            {reading ? <DocumentReader key={workspace.workspace_id} workspaceId={workspace.workspace_id} stageId={workspace.stage_id} provider={workspace.provider} fileUrl={fileUrl} filename={pdf.filename} pageCount={pdf.page_count} page={pdfPage} authorized={readerPageAuthorized(pdf.page_ranges, pdfPage, pdf.page_count)} onPage={goToPage} onDraft={draftReaderSelection} /> : <div className="desk-overview desk-welcome"><div className="desk-paper-stack" aria-hidden="true"><div /><div><Glyph name="file" /><i /><i /></div></div><span className="desk-eyebrow">当前工作区</span><h1>继续阅读</h1><p className="desk-intro">第 {pdfPage} 页 · 当前项目里的独立会话</p><button className="desk-primary" type="button" onClick={() => { setReaderOpen(true); setPane("document"); setAssistantOpen(false); }}><Glyph name="file" />进入论文阅读</button></div>}
          </> : <div className={!stage ? "desk-overview desk-welcome" : "desk-overview"}>
            {!stage && <div className="desk-paper-stack" aria-hidden="true"><div /><div><Glyph name="file" /><i /><i /></div></div>}
            <span className="desk-eyebrow">{stage ? "项目工作区" : "ScholarFlow · 科研工作台"}</span>
            <h1>{project ? (project.project_name || project.title) : stage ? "当前项目" : "从一个项目开始"}</h1>
            <p className="desk-intro">{project?.goal || (stage ? "导入文献做有来源的笔记，或直接在项目中与 Pi 对话。" : "把文献、Pi 对话和有来源的笔记放在同一个项目里。")}</p>
            {!stage ? <div className="desk-actions"><button className="desk-primary" type="button" onClick={beginProject}><Glyph name="plus" />新建项目</button>{projects.length > 0 && <button className="desk-quiet" type="button" onClick={showProjects}>查看已有项目</button>}</div> : busy === "resume" ? <p className="desk-muted" role="status">正在恢复工作区…</p> : <div className="desk-intake">
              <div className="desk-intake-head"><div><h2>开启工作区</h2><p>每篇 PDF 使用独立的阅读上下文。</p></div>{intake && workspace && <button type="button" onClick={() => setIntake(false)} disabled={Boolean(busy)}>返回当前工作区</button>}</div>
              <label className="desk-drop" onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); if (!busy) setFile(event.dataTransfer.files[0] ?? null); }}><span className="desk-drop-mark"><Glyph name={file ? "file" : "upload"} /></span><strong>{file?.name || "选择或拖入 PDF"}</strong><small>本地文件 · 最大 80 MB</small><input key={stage} aria-label="选择 PDF" type="file" accept="application/pdf,.pdf" disabled={Boolean(busy)} onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></label>
              <div className="desk-options"><label>Pi 可读页<input aria-label="授权页" value={pages} disabled={Boolean(busy)} onChange={(event) => setPages(event.target.value)} placeholder="例如 1-3,5" /></label><label>新工作区的模型<select aria-label="选择模型" value={provider} disabled={Boolean(busy)} onChange={(event) => setProvider(event.target.value)}><option value="scholarflow-local">本地 Qwen</option><option value="deepseek">DeepSeek（外部 API）</option></select></label></div>
              <p className="desk-muted">{provider === "scholarflow-local" ? "本地模型需先运行，失败不会切换到外部模型。" : "向 DeepSeek 发送内容前，仍需在对话区确认。"}</p>
              <div className="desk-actions"><button className="desk-primary" type="button" onClick={() => void openPdf()} disabled={!file || Boolean(busy)}>{busy === "pdf" ? "正在解析…" : "打开 PDF"}</button><button type="button" onClick={() => void openProjectChat()} disabled={Boolean(busy)}>{busy === "project-chat" ? "正在开启…" : "不传 PDF，开启项目对话"}</button></div>
            </div>}
          </div>)}
        </section>
        <section className="desk-assistant" id="desk-inspector" aria-label="Pi 助手" onFocusCapture={() => setPane("pi")}>
          <div className="desk-assistant-head"><span className="desk-pi-mark" aria-hidden="true">π</span><div><h2>Pi 助手</h2><p>{workspace ? providerLabel(workspace.provider) : "独立研究会话"}</p></div>{turnBusy && <span role="status" className="desk-tag">Pi 处理中</span>}</div>
          <div className="desk-switch" role="group" aria-label="Pi 面板"><button type="button" aria-pressed={assistantView === "chat"} onClick={() => setAssistantView("chat")}>对话</button><button type="button" aria-pressed={assistantView === "notes"} onClick={() => setAssistantView("notes")}>笔记审阅{pending > 0 && <span className="desk-count">{pending}</span>}</button></div>
          {workspace && <div className="desk-scope"><span>{pdf ? `PDF 授权页 ${ranges}` : "仅项目 · 未挂载 PDF"}</span><span>{canAskKnowledge(workspace) ? "项目知识有索引" : "当前无可用知识索引"}</span></div>}
          <div className="desk-thread" ref={thread}>
          {assistantView === "chat" ? <>
            {!workspace?.turns.length && <div className="desk-thread-empty"><span aria-hidden="true">π</span><h3>{workspace ? "你想研究什么？" : "还没有开启对话"}</h3><p>{workspace ? (pdf ? "从授权页出发，询问方法、结果或证据。" : "可以讨论研究目标；有索引时也可查询当前项目知识。") : stage ? "可以先问项目问题，也可以打开论文再带着原文问。" : "选中项目后即可开始对话。"}</p>{workspace ? <button type="button" disabled={blocked} onClick={() => { setDraftMessage(pdf ? "请概括这几页的核心方法，标注页码，并区分作者结论和你的推断。" : "请帮我拆解当前研究目标，并列出还需要收集的证据。"); composer.current?.focus(); }}>填入一个示例问题</button> : stage && <button className="desk-primary" type="button" disabled={Boolean(busy)} onClick={() => void openProjectChat()}>{busy === "project-chat" ? "正在开启…" : "开启项目对话"}</button>}</div>}
            {workspace?.turns.map((turn) => <article className="desk-turn" key={turn.turn_id}><p className="desk-user">{turn.message}</p><div className="desk-turn-answer"><strong className="desk-eyebrow">Pi</strong>{turn.status === "completed" ? <SafeMarkdown content={turn.answer || ""} /> : <p>{turnStateText(turn.status, turn.error)}</p>}{turn.sources?.map((source, index) => <details className="reading-source" key={source.reference_id || index}><summary>{source.title || "来源"}</summary><small>{source.reference_id}</small><p>{source.text}</p>{uniquePdfSources(source.pdf_sources || []).map((ref) => ref.import_id === pdf?.import_id ? <button type="button" key={`${ref.import_id}-${ref.anchor?.page_start}`} onClick={() => goToPage(ref.anchor?.page_start || 1)}>打开第 {ref.anchor?.page_start} 页</button> : <a key={`${ref.import_id}-${ref.anchor?.page_start}`} href={`${api}/imports/${encodeURIComponent(ref.import_id || "")}/pdf#page=${ref.anchor?.page_start}`} target="_blank" rel="noreferrer">打开来源 PDF 第 {ref.anchor?.page_start} 页 ↗</a>)}</details>)}</div></article>)}
          </> : <>
          {proposals.length === 0 && <div className="desk-thread-empty"><h3>还没有笔记提案</h3><p>{pdf ? "点击下方「生成阅读笔记」，完成后可核对原文、编辑并确认。" : "打开 PDF 后可以生成带页码的阅读笔记。"}</p></div>}
          {proposals.map((proposal) => {
            const id = proposal.proposal.proposal_id;
            const editing = review?.proposalId === id && review.proposalHash === proposal.proposal_hash;
            if (!workspace) return null;
            const recoverable = proposal.phase === "rejected" && Boolean(readReviewDraft(browserStorage(), workspace.stage_id, workspace.workspace_id, id, proposal.proposal_hash)?.submitted);
            return <article className="reading-note" key={id}>
              <span className="desk-tag">{proposalText[proposal.phase] || proposal.phase}</span>
              <h3>{proposal.proposal.normalized_changes?.title || "阅读笔记"}</h3>
              <SafeMarkdown content={proposal.proposal.normalized_changes?.content || ""} />
              <details><summary>核对原文来源</summary>{proposal.proposal.normalized_changes?.source_snapshot?.sources?.map((source) => <div className="reading-source" key={source.source_id}>{source.anchor?.page_start && <button type="button" onClick={() => goToPage(source.anchor!.page_start!)}>打开第 {source.anchor.page_start} 页</button>}<p>{source.text}</p></div>)}</details>
              {["pending", "approved_pending"].includes(proposal.phase) && <p>请逐项核对原文；AI 可能把原文概括得过强。</p>}
              {editing && review && <div className="reading-review-editor"><label>修订标题<input aria-label="修订标题" maxLength={1000} value={review.title} disabled={review.submitted} onChange={(event) => updateReview({ ...review, title: event.target.value })} /></label><label>修订正文<textarea aria-label="修订正文" maxLength={12000} rows={10} value={review.content} disabled={review.submitted} onChange={(event) => updateReview({ ...review, content: event.target.value })} /></label><small>{review.submitted ? "修订已提交处理中断；请重试完成，原版本不会保存。" : reviewStorageAvailable ? "修订草稿仅保存在当前浏览器；生成新提案后仍需确认。" : "浏览器无法保存草稿，离开前请自行复制。"}</small><button type="button" disabled={blocked || !review.title.trim() || !review.content.trim()} onClick={() => void submitReview()}>{review.submitted ? "重试完成修订" : "生成修订提案"}</button>{!review.submitted && <button type="button" onClick={() => { writeReviewDraft(browserStorage(), workspace.stage_id, workspace.workspace_id, null, id, proposal.proposal_hash); setReview(null); }}>取消编辑</button>}</div>}
              {!editing && (proposal.phase === "pending" || recoverable) && <button type="button" disabled={blocked} onClick={() => startReview(proposal)}>{recoverable ? "恢复未完成修订" : "编辑提案"}</button>}
              {!editing && ["pending", "approved_pending"].includes(proposal.phase) && <div className="desk-actions"><button className="desk-primary" type="button" onClick={() => void decide(proposal, "approve")} disabled={blocked}>确认保存并入库</button><button className="desk-danger" type="button" onClick={() => void decide(proposal, "reject")} disabled={blocked}>拒绝</button></div>}
            </article>;
          })}
          {workspace?.pdf && workspace.knowledge.status !== "not_queued" && <p className="desk-muted" role="status">此笔记索引：{knowledgeText[workspace.knowledge.status] || workspace.knowledge.status}</p>}
          {workspace && (["failed", "stale", "interrupted"].includes(workspace.knowledge.status) || (workspace.knowledge.status === "not_queued" && workspace.proposals.some((item) => item.phase === "committed"))) && <p className="reading-error">笔记已保存，知识入库尚未完成。{workspace.knowledge.error_code && ` ${workspace.knowledge.error_code}`} <button type="button" onClick={() => void retryIndex()} disabled={blocked}>重试入库</button></p>}
          </>}
        </div>
        {workspace && <form className="desk-composer" onSubmit={(event) => { event.preventDefault(); if (!blocked && assistantView === "chat") void send("chat"); }}>
          {workspace.provider === "deepseek" && <label className="reading-consent"><input type="checkbox" checked={externalConsent} onChange={(event) => setExternalConsent(event.target.checked)} />同意将消息、授权 PDF 页及本项目检索到的内容发送至 DeepSeek。</label>}
          {assistantView === "chat" && <textarea ref={composer} aria-label="给 Pi 发消息" maxLength={6000} rows={3} value={message} onChange={(event) => setDraftMessage(event.target.value)} placeholder={pdf ? "询问当前授权页…" : "向 Pi 提问…"} />}
          <div className="desk-actions">
            {pdf && <button type="button" onClick={() => void send("note")} disabled={blocked}>生成阅读笔记</button>}
            {assistantView === "chat" && <><button type="button" title={canAskKnowledge(workspace) ? "基于当前项目已索引证据回答" : "当前项目没有可用索引"} onClick={() => void send("knowledge")} disabled={!canAskKnowledge(workspace) || !message.trim() || blocked}>问知识库</button><button className="desk-primary desk-send" type="submit" disabled={!message.trim() || blocked}>发送 <Glyph name="send" /></button></>}
          </div>
          {assistantView === "chat" && !draftStorageAvailable && <small role="status">浏览器无法保存草稿，离开前请自行复制。</small>}
        </form>}
        </section>
      </div>
      <dialog ref={projectForm} className="desk-dialog" aria-labelledby="desk-create-heading" onCancel={(event) => { event.preventDefault(); if (!busy) closeProject(); }}>
        <div className="desk-intake-head"><div><span className="desk-eyebrow">项目</span><h2 id="desk-create-heading">新建研究项目</h2></div><button className="desk-tool" type="button" onClick={closeProject} disabled={Boolean(busy)} aria-label="关闭新建项目"><Glyph name="close" /></button></div>
        {error && creating && <p className="reading-error" role="alert">{error}</p>}
        <form onSubmit={createPreview}>
          <label>项目名称<input maxLength={200} value={name} onChange={(event) => { previewRevision.current++; setPreview(null); setName(event.target.value); }} required /></label>
          <label>阶段标题<input maxLength={200} value={title} onChange={(event) => { previewRevision.current++; setPreview(null); setTitle(event.target.value); }} required /></label>
          <label>研究目标<textarea maxLength={4000} rows={3} value={goal} onChange={(event) => { previewRevision.current++; setPreview(null); setGoal(event.target.value); }} required /></label>
          <button type="submit" disabled={Boolean(busy)}>生成预览</button>
        </form>
        {preview && <div className="reading-preview desk-preview"><span className="desk-eyebrow">请确认实际保存的内容</span><strong>{preview.changes.project_name}</strong><span>{preview.changes.title}</span><p>{preview.changes.goal}</p><button className="desk-primary" type="button" onClick={() => void commitProject()} disabled={Boolean(busy) || !previewMatches(preview, name, title, goal)}>确认创建项目</button></div>}
      </dialog>
    </main>
  );
}

function Brand() {
  return <div className="desk-brand"><img src="/brand/scholarflow-icon.png" alt="" width={32} height={32} /><strong>ScholarFlow</strong></div>;
}
