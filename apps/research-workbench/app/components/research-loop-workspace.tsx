"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { API_BASE, apiGet, apiPost, apiUploadPdf } from "@/app/lib/api";
import { SafeMarkdown } from "@/app/components/safe-markdown";

type Project = { stage_id: string; project_name: string; title: string };
type SourceAnchor = { page_start?: number; page_end?: number; block_id?: string };
type PdfSource = { import_id: string; anchor: SourceAnchor };
type Source = { reference_id: string; title?: string; text?: string; evidence_level?: string; pdf_sources?: PdfSource[] };
type Turn = { turn_id: string; message: string; status: string; answer?: string; sources?: Source[]; error?: string; tool_errors?: string[] };
type Proposal = { phase: string; proposal_hash: string; proposal: { proposal_id: string; normalized_changes: { title?: string; content?: string; source_snapshot?: { sources?: Array<{ source_id: string; text?: string; anchor?: SourceAnchor }> } } } };
type Workspace = {
  workspace_id: string; stage_id: string; project_title: string; provider: string; knowledge_available: boolean;
  pdf: { import_id: string; filename: string; page_count: number; page_ranges: number[][]; note_blocks: number; total_page_blocks: number } | null;
  knowledge: { status: string; error_code?: string; attempts?: number };
  proposals: Proposal[]; turns: Turn[];
};
type SavedWorkspace = { workspace_id: string; import_id: string | null };

const statusText: Record<string, string> = {
  not_queued: "当前笔记尚未入库", queued: "等待入库", running: "正在入库", indexed: "已入 AI 知识库",
  failed: "入库失败，可重试", stale: "来源已变化，需要重新确认", interrupted: "处理已中断，请检查后重试",
};
const messageOf = (error: unknown) => error instanceof Error ? error.message : "请求未完成，请检查本地连接。";
const draftKey = (project: string, workspace: string) => `scholarflow:research-loop:draft:${project}:${workspace}`;
function readDraft(project: string, workspace: string): { value: string; available: boolean } {
  try { return { value: window.localStorage.getItem(draftKey(project, workspace)) || "", available: true }; }
  catch { return { value: "", available: false }; }
}
function writeDraft(project: string, workspace: string, value: string): boolean {
  try {
    if (value) window.localStorage.setItem(draftKey(project, workspace), value);
    else window.localStorage.removeItem(draftKey(project, workspace));
    return true;
  } catch { return false; }
}
const uniquePdfPages = (sources: PdfSource[]) => Array.from(new Map(sources.filter((pdf) => /^pdf_[a-f0-9]{24}$/.test(pdf.import_id) && pdf.anchor?.page_start).map((pdf) => [`${pdf.import_id}:${pdf.anchor.page_start}`, pdf])).values());

export function ResearchLoopWorkspace({ initialProject, onProjectChange, onClose, surface = "modal" }: {
  initialProject: string; onProjectChange: (id: string) => void; onClose?: () => void; surface?: "modal" | "page";
}) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [stage, setStage] = useState("");
  const [saved, setSaved] = useState<SavedWorkspace[]>([]);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [pages, setPages] = useState("1");
  const [message, setMessage] = useState("");
  const [externalConsent, setExternalConsent] = useState(false);
  const [draftStorageAvailable, setDraftStorageAvailable] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [dragging, setDragging] = useState(false);
  const current = useRef({ stage, id: "" });
  const scrollEnd = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let alive = true;
    void apiGet<{ items: Project[] }>("/research-loop/projects").then((result) => {
      if (!alive) return;
      setProjects(result.items);
      if (result.items.some((project) => project.stage_id === initialProject)) setStage(initialProject);
    }).catch((reason: unknown) => { if (alive) setError(messageOf(reason)); });
    return () => { alive = false; };
  }, [initialProject]);

  useEffect(() => {
    current.current = { stage, id: "" };
    if (!stage) return;
    let alive = true;
    void apiGet<{ items: SavedWorkspace[] }>(`/research-loop/workspaces?stage_id=${encodeURIComponent(stage)}`)
      .then((result) => { if (alive) setSaved(result.items); })
      .catch((reason: unknown) => { if (alive) setError(messageOf(reason)); });
    return () => { alive = false; };
  }, [stage]);

  const refresh = useCallback(async (id: string, project: string) => {
    const value = await apiGet<Workspace>(`/research-loop/workspaces/${encodeURIComponent(id)}?stage_id=${encodeURIComponent(project)}`, { timeoutMs: 60000 });
    if (current.current.stage === project && current.current.id === id) setWorkspace(value);
    return value;
  }, []);
  const waiting = Boolean(workspace?.turns.some((turn) => turn.status === "queued" || turn.status === "running") || ["queued", "running"].includes(workspace?.knowledge.status || ""));
  useEffect(() => {
    if (!workspace || !waiting) return;
    const timer = window.setInterval(() => { void refresh(workspace.workspace_id, workspace.stage_id).catch((reason: unknown) => setError(messageOf(reason))); }, 3500);
    return () => window.clearInterval(timer);
  }, [workspace, waiting, refresh]);
  useEffect(() => { scrollEnd.current?.scrollIntoView({ block: "nearest", behavior: "smooth" }); }, [workspace?.turns.length]);

  function selectFile(candidate: File | undefined) {
    if (!candidate) return;
    if (!candidate.name.toLowerCase().endsWith(".pdf") || candidate.size <= 0 || candidate.size > 80_000_000) {
      setError("请选择不超过 80 MB 的 PDF 文件。"); return;
    }
    setFile(candidate); setError("");
  }

  async function createWorkspace() {
    if (!stage || !file) return;
    setBusy("create"); setError("");
    try {
      const imported = await apiUploadPdf<{ import_id: string }>(file, stage);
      const result = await apiPost<Workspace>("/research-loop/workspaces", { stage_id: stage, import_id: imported.import_id, pages, provider: "deepseek" }, { timeoutMs: 120000 });
      current.current = { stage, id: result.workspace_id }; setWorkspace(result); setFile(null); setExternalConsent(false);
      const draft = readDraft(stage, result.workspace_id); setMessage(draft.value); setDraftStorageAvailable(draft.available);
      const list = await apiGet<{ items: SavedWorkspace[] }>(`/research-loop/workspaces?stage_id=${encodeURIComponent(stage)}`);
      if (current.current.stage === stage) setSaved(list.items);
    } catch (reason) { setError(messageOf(reason)); } finally { setBusy(""); }
  }

  async function resume(id: string) {
    if (!stage) return;
    setBusy("resume"); setError(""); setExternalConsent(false); current.current = { stage, id };
    try { await refresh(id, stage); const draft = readDraft(stage, id); setMessage(draft.value); setDraftStorageAvailable(draft.available); } catch (reason) { setError(messageOf(reason)); } finally { setBusy(""); }
  }

  async function send(mode: "chat" | "note" | "knowledge", value: string) {
    if (!workspace || !value.trim() || waiting || busy) return;
    if (workspace.provider === "deepseek" && !externalConsent) { setError("请先确认允许将授权页和相关项目知识发送至 DeepSeek。"); return; }
    setBusy("send"); setError("");
    try {
      const id = `turn-${crypto.randomUUID().replaceAll("-", "")}`;
      await apiPost(`/research-loop/workspaces/${encodeURIComponent(workspace.workspace_id)}/turns`, { stage_id: stage, request_id: id, mode, message: value, external_consent: externalConsent });
      if (mode !== "note") { setDraftStorageAvailable(writeDraft(stage, workspace.workspace_id, "")); setMessage(""); }
      await refresh(workspace.workspace_id, stage);
    } catch (reason) { setError(messageOf(reason)); } finally { setBusy(""); }
  }

  async function decide(proposal: Proposal, decision: "approve" | "reject") {
    if (!workspace) return;
    setBusy("approve"); setError("");
    try {
      await apiPost(`/research-loop/workspaces/${encodeURIComponent(workspace.workspace_id)}/approve`, {
        stage_id: stage, proposal_id: proposal.proposal.proposal_id, reviewed_hash: proposal.proposal_hash, decision,
      });
      await refresh(workspace.workspace_id, stage);
    } catch (reason) { setError(messageOf(reason)); } finally { setBusy(""); }
  }

  async function retryIndex() {
    if (!workspace) return;
    setBusy("index"); setError("");
    try {
      await apiPost(`/research-loop/workspaces/${encodeURIComponent(workspace.workspace_id)}/index`, { stage_id: stage });
      await refresh(workspace.workspace_id, stage);
    } catch (reason) { setError(messageOf(reason)); } finally { setBusy(""); }
  }

  function changeProject(next: string) {
    current.current = { stage: next, id: "" }; setWorkspace(null); setSaved([]); setFile(null); setError(""); setExternalConsent(false);
    setMessage(""); setStage(next); onProjectChange(next);
  }

  return (
    <div className={surface === "page" ? "research-loop-page" : "research-loop-backdrop"} onMouseDown={(event) => { if (surface === "modal" && event.target === event.currentTarget) onClose?.(); }}>
      <section className="research-loop" role={surface === "page" ? "main" : "dialog"} aria-modal={surface === "modal"} aria-labelledby="research-loop-title">
        <header className="research-loop-header">
          <div><span className="research-loop-eyebrow">SCHOLARFLOW · PI</span><h2 id="research-loop-title">{surface === "page" ? "阅读台" : "阅读与知识工作区"}</h2></div>
          {surface === "modal" ? <button className="research-loop-close" type="button" aria-label="关闭阅读工作区" onClick={onClose}>×</button> : <Link className="research-loop-legacy" href="/">首页</Link>}
        </header>
        <div className="research-loop-context">
          <label>研究项目<select aria-label="阅读工作区项目" value={stage} disabled={Boolean(busy)} onChange={(event) => changeProject(event.target.value)}>
            <option value="">选择项目</option>{projects.map((project) => <option value={project.stage_id} key={project.stage_id}>{project.project_name || project.title}{project.title && project.title !== project.project_name ? ` · ${project.title}` : ""} [{project.stage_id}]</option>)}
          </select></label>
          <span>当前使用网页的数据；不会自动合并其他数据目录。</span>
        </div>
        {error && <div role="alert" className="research-loop-error">{error}<button onClick={() => setError("")} aria-label="关闭错误">×</button></div>}
        {!workspace ? (
          <div className="research-loop-start">
            <div><h3>从一份 PDF 开始</h3><p>选择文件与允许 Pi 阅读的页码。原文件不会被修改。</p></div>
            <label className={`research-loop-drop ${dragging ? "dragging" : ""}`} onDragOver={(event) => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={(event) => { event.preventDefault(); setDragging(false); selectFile(event.dataTransfer.files[0]); }}>
              <input type="file" accept="application/pdf,.pdf" aria-label="选择 PDF" onChange={(event) => selectFile(event.target.files?.[0])} />
              <strong>{file ? file.name : "选择 PDF，或拖到这里"}</strong><span>PDF · 最大 80 MB</span>
            </label>
            <label className="research-loop-pages">允许阅读的页码<input aria-label="允许阅读的页码" value={pages} onChange={(event) => setPages(event.target.value)} placeholder="例如 1-3,5" /><small>默认第 1 页；当前 PDF 只向模型提供此范围内的文字。</small></label>
            <small>当前使用 DeepSeek 外部 API；知识库提问还可能发送所选项目已入库的笔记。请确认所选内容可以发送。</small>
            <button className="research-loop-primary" disabled={!stage || !file || !pages.trim() || Boolean(busy)} onClick={() => void createWorkspace()}>{busy === "create" ? "正在导入并解析…" : "打开 PDF 并开始阅读"}</button>
            {saved.length > 0 && <div className="research-loop-resume"><h4>继续之前的阅读</h4>{saved.map((item, index) => <button key={item.workspace_id} disabled={Boolean(busy)} onClick={() => void resume(item.workspace_id)}>继续阅读 {index + 1}<small>{item.workspace_id.slice(-8)}</small></button>)}</div>}
          </div>
        ) : (
          <div className="research-loop-body">
            <div className="research-loop-pdf">
              <div className="research-loop-file"><div><strong>{workspace.pdf?.filename}</strong><small>授权页 {workspace.pdf?.page_ranges.map(([a, b]) => a === b ? a : `${a}–${b}`).join(", ")} · 笔记来源 {workspace.pdf?.note_blocks}/{workspace.pdf?.total_page_blocks} 个文本块</small></div><button onClick={() => { current.current.id = ""; setWorkspace(null); }}>换一份</button></div>
              {workspace.pdf && <iframe title="PDF 阅读器" src={`${API_BASE}/reader/imports/${encodeURIComponent(workspace.pdf.import_id)}/pdf`} />}
            </div>
            <div className="research-loop-side">
              <div className="research-loop-side-head"><div><strong>Pi 研究助手</strong><small>{workspace.provider === "deepseek" ? "DeepSeek 外部 API" : "本地 Qwen"} · 当前项目独立会话{workspace.knowledge_available && workspace.knowledge.status !== "indexed" ? " · 可查询已有项目知识" : ""}</small></div><span className={`research-loop-state ${workspace.knowledge.status}`}>{statusText[workspace.knowledge.status] || workspace.knowledge.status}</span></div>
              <div className="research-loop-thread" aria-live="polite">
                {!workspace.turns.length && <div className="research-loop-empty"><strong>想先了解什么？</strong><p>可以让 Pi 解释当前授权页，或生成一份带来源的阅读笔记。</p></div>}
                {workspace.turns.map((turn) => <div className="research-loop-turn" key={turn.turn_id}>
                  <div className="research-loop-user">{turn.message}</div>
                  {turn.status === "completed" ? <div className="research-loop-answer"><SafeMarkdown content={turn.answer || ""} />
                    {Boolean(turn.tool_errors?.length) && <p className="research-loop-warning">部分工具未成功：{turn.tool_errors?.join("、")}。请核对后重试。</p>}
                    {Boolean(turn.sources?.length) && <details><summary>查看检索来源（{turn.sources?.length}）</summary>{turn.sources?.map((source) => <div className="research-loop-source" key={source.reference_id}><strong>{source.title && !/^[a-f0-9]{64}\.md$/.test(source.title) ? source.title : "PDF 阅读笔记"}</strong><small>{source.evidence_level === "derived_pdf_fragment_note" ? "PDF 片段派生笔记 · 二次证据" : "项目知识来源"} · {source.reference_id}</small><p>{source.text}</p>{uniquePdfPages(source.pdf_sources || []).map((pdf) => <a key={`${pdf.import_id}-${pdf.anchor.page_start}`} href={`${API_BASE}/reader/imports/${encodeURIComponent(pdf.import_id)}/pdf#page=${pdf.anchor.page_start}`} target="_blank" rel="noreferrer">查看笔记关联的原 PDF · 第 {pdf.anchor.page_start} 页</a>)}</div>)}</details>}
                  </div> : <div className="research-loop-answer">{turn.status === "failed" || turn.status === "interrupted" ? turn.error || "任务已中断，请检查后重新发送。" : "Pi 正在阅读和思考…"}</div>}
                </div>)}
                {workspace.proposals.map((proposal) => <section className="research-loop-proposal" key={proposal.proposal.proposal_id}>
                  <span>{proposal.phase === "committed" ? "已保存的笔记" : proposal.phase === "rejected" ? "已拒绝" : "待你审阅"}</span>
                  <h4>{proposal.proposal.normalized_changes.title || "阅读笔记"}</h4>
                  <SafeMarkdown content={proposal.proposal.normalized_changes.content || ""} />
                  <details><summary>查看笔记来源与确认版本</summary><small>内容版本：{proposal.proposal_hash}</small>{proposal.proposal.normalized_changes.source_snapshot?.sources?.map((source) => <div className="research-loop-source" key={source.source_id}><strong>第 {source.anchor?.page_start} 页 · {source.anchor?.block_id}</strong><p>{source.text}</p></div>)}</details>
                  {["pending", "approved_pending"].includes(proposal.phase) && <div className="research-loop-actions"><button className="research-loop-primary" disabled={Boolean(busy) || waiting} onClick={() => void decide(proposal, "approve")}>确认保存笔记并自动入库</button><button disabled={Boolean(busy) || waiting} onClick={() => void decide(proposal, "reject")}>拒绝此笔记</button></div>}
                </section>)}
                {(["failed", "stale", "interrupted"].includes(workspace.knowledge.status) || (workspace.knowledge.status === "not_queued" && workspace.proposals.some((p) => p.phase === "committed"))) && <div className="research-loop-warning">笔记保存与知识入库是两个状态。{workspace.knowledge.error_code && <span> {workspace.knowledge.error_code}</span>} <button onClick={() => void retryIndex()} disabled={Boolean(busy) || waiting}>重试入库</button></div>}
                <div ref={scrollEnd} />
              </div>
              <div className="research-loop-compose">
                {workspace.provider === "deepseek" && <label><input type="checkbox" checked={externalConsent} onChange={(event) => setExternalConsent(event.target.checked)} />我允许将所选 PDF 授权页及本项目检索到的知识发送至 DeepSeek。</label>}
                <div className="research-loop-actions"><button disabled={Boolean(busy) || waiting || workspace.proposals.some((p) => ["pending", "approved_pending", "committed"].includes(p.phase))} onClick={() => void send("note", "请根据当前授权片段整理中文阅读笔记，并提交给我审阅。")}>生成阅读笔记</button><button disabled={Boolean(busy) || waiting || !workspace.knowledge_available || !message.trim()} onClick={() => void send("knowledge", message)}>从知识库回答</button></div>
                <form onSubmit={(event: FormEvent) => { event.preventDefault(); void send("chat", message); }}><textarea aria-label="给 Pi 发消息" value={message} onChange={(event) => { setMessage(event.target.value); setDraftStorageAvailable(writeDraft(stage, workspace.workspace_id, event.target.value)); }} placeholder="询问这篇 PDF，或提出研究问题…" /><button type="submit" className="research-loop-primary" disabled={!message.trim() || Boolean(busy) || waiting}>发送</button></form>
                <small>{draftStorageAvailable ? "草稿保存在当前浏览器。" : "浏览器不允许保存草稿；刷新或关闭前请自行复制。"}</small>
              </div>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
