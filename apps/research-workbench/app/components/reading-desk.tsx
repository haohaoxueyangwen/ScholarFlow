"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { SafeMarkdown } from "@/app/components/safe-markdown";
import { browserStorage, canAskKnowledge, isSameScope, previewMatches, readDraft, readReviewDraft, researchApiBase, researchRequest, turnStateText, uniquePdfSources, writeDraft, writeReviewDraft, type DeskScope, type ReviewDraft } from "@/app/lib/research-desk";

const deskFetch = (path: string, init?: RequestInit) => researchRequest(researchApiBase(), path, init);

type Project = { stage_id: string; project_name: string; title: string; goal: string };
type Preview = { request_id: string; stage_id: string; proposal_hash: string; created: boolean; changes: { project_name?: string; title?: string; goal?: string } };
type PdfInfo = { import_id: string; filename: string; page_ranges: number[][]; note_blocks: number; total_page_blocks: number };
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
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const current = useRef<DeskScope>({ stage: "", id: "", generation: 0 });
  const messageRef = useRef("");
  const previewRevision = useRef(0);

  function chooseProject(nextStage: string) {
    current.current = { stage: nextStage, id: "", generation: current.current.generation + 1 };
    setStage(nextStage); setWorkspace(null); setSaved([]); setSavedError(""); setFile(null);
    setMessage(""); messageRef.current = ""; setReview(null); setPreview(null); previewRevision.current++; setExternalConsent(false); setError(""); setBusy("");
  }

  function showWorkspace(next: Workspace, expected: DeskScope) {
    if (!isSameScope(current.current, expected) || next.stage_id !== expected.stage) return;
    current.current = { ...expected, id: next.workspace_id };
    setWorkspace(next); setPdfPage(1); setReview(null); setExternalConsent(false);
    const draft = readDraft(browserStorage(), next.stage_id, next.workspace_id);
    setMessage(draft.value); messageRef.current = draft.value; setDraftStorageAvailable(draft.available);
  }

  async function refreshWorkspace(scope: DeskScope) {
    const next = await readJson<Workspace>(await deskFetch(`/workspaces/${encodeURIComponent(scope.id)}?stage_id=${encodeURIComponent(scope.stage)}`));
    if (isSameScope(current.current, scope)) setWorkspace(next);
  }

  async function refreshProjects() {
    const payload = await readJson<{ items: Project[] }>(await deskFetch("/projects"));
    setProjects(payload.items);
  }

  useEffect(() => {
    let alive = true;
    deskFetch("/projects").then((response) => readJson<{ items: Project[] }>(response)).then((payload) => {
      if (alive) { setProjects(payload.items); setOnline(true); }
    }).catch(() => { if (alive) setOnline(false); });
    return () => { alive = false; };
  }, [connectionAttempt]);

  useEffect(() => {
    if (!workspace) return;
    const waiting = workspace.turns.some((turn) => turn.status === "queued" || turn.status === "running")
      || ["queued", "running"].includes(workspace.knowledge.status);
    if (!waiting) return;
    const scope = { ...current.current };
    const timer = window.setInterval(() => {
      void refreshWorkspace(scope).catch((reason: unknown) => {
        if (isSameScope(current.current, scope)) setError(reason instanceof Error ? reason.message : "无法刷新工作区");
      });
    }, 2000);
    return () => window.clearInterval(timer);
  }, [workspace]);

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
      chooseProject(created.stage_id); setPreview(null); setName(""); setTitle(""); setGoal("");
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
      .catch((reason: unknown) => { if (alive) setSavedError(reason instanceof Error ? reason.message : "无法读取已有工作区"); });
    return () => { alive = false; };
  }, [stage, workspace?.workspace_id]);

  async function resume(id: string) {
    const scope = { stage, id, generation: current.current.generation + 1 };
    current.current = scope;
    setWorkspace(null); setMessage(""); messageRef.current = ""; setExternalConsent(false);
    setBusy("resume"); setError("");
    try {
      const next = await readJson<Workspace>(await deskFetch(`/workspaces/${encodeURIComponent(id)}?stage_id=${encodeURIComponent(scope.stage)}`));
      showWorkspace(next, scope);
    } catch (reason) { if (isSameScope(current.current, scope)) setError(reason instanceof Error ? reason.message : "无法恢复工作区"); }
    finally { if (isSameScope(current.current, scope)) setBusy(""); }
  }

  async function openPdf() {
    if (!stage || !file) return;
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

  async function send(mode: "chat" | "note" | "knowledge") {
    if (!workspace) return;
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

  if (online === false) {
    return <main className="reading-offline"><h1>研究服务未连接</h1><p>请使用 ScholarFlow 阅读台启动入口，或检查本机服务与网页端口。</p><button type="button" onClick={() => { setOnline(null); setConnectionAttempt((value) => value + 1); }}>重新连接</button></main>;
  }
  if (online === null) return <main className="reading-offline"><p>正在连接研究服务…</p></main>;

  const ranges = workspace?.pdf?.page_ranges.map(([start, end]) => start === end ? String(start) : `${start}-${end}`).join(", ");
  const waiting = Boolean(workspace?.turns.some((turn) => ["queued", "running"].includes(turn.status)) || ["queued", "running"].includes(workspace?.knowledge.status || ""));
  const blocked = Boolean(busy) || waiting;
  const api = researchApiBase();

  return (
    <main className="reading-desk">
      <section className="reading-rail" aria-label="项目">
        <p className="reading-kicker">项目</p>
        <ul>{projects.map((project) => <li key={project.stage_id}><button type="button" className={project.stage_id === stage ? "selected" : ""} onClick={() => chooseProject(project.stage_id)}>{project.project_name || project.title}<small>{project.goal}</small></button></li>)}</ul>
        <form onSubmit={createPreview}>
          <p className="reading-kicker">新建</p>
          <input aria-label="项目名称" placeholder="项目名称" value={name} onChange={(event) => { previewRevision.current++; setPreview(null); setName(event.target.value); }} required />
          <input aria-label="阶段标题" placeholder="阶段标题" value={title} onChange={(event) => { previewRevision.current++; setPreview(null); setTitle(event.target.value); }} required />
          <textarea aria-label="研究目标" placeholder="研究目标" value={goal} onChange={(event) => { previewRevision.current++; setPreview(null); setGoal(event.target.value); }} required />
          <button type="submit" disabled={Boolean(busy)}>生成预览</button>
        </form>
        {preview && <div className="reading-preview"><p>请核对项目，确认后创建：</p><strong>{preview.changes.project_name}</strong><span>{preview.changes.title}</span><p>{preview.changes.goal}</p><button type="button" onClick={() => void commitProject()} disabled={Boolean(busy) || !previewMatches(preview, name, title, goal)}>确认创建项目</button></div>}
        <label className="reading-file" onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); setFile(event.dataTransfer.files[0] ?? null); }}>PDF：{file?.name || "选择文件或拖到这里"}<input key={stage} aria-label="选择 PDF" type="file" accept="application/pdf,.pdf" onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></label>
        <label>授权页<input aria-label="授权页" value={pages} onChange={(event) => setPages(event.target.value)} /></label>
        <label>新工作区使用的模型<select aria-label="选择模型" value={provider} onChange={(event) => setProvider(event.target.value)}><option value="scholarflow-local">本地 Qwen</option><option value="deepseek">DeepSeek（外部 API）</option></select></label>
        <small>{provider === "scholarflow-local" ? "需要本地模型服务可用；不会自动切换外部模型。" : "使用前还需要在对话区确认允许资料外发。"}</small>
        <button type="button" onClick={() => void openPdf()} disabled={!stage || !file || Boolean(busy)}>{busy === "pdf" ? "正在解析…" : "打开 PDF"}</button>
        {savedError && <p className="reading-error" role="alert">已有工作区读取失败：{savedError}</p>}
        {saved.length > 0 && <div className="reading-resume"><p className="reading-kicker">继续</p>{saved.map((item) => <button type="button" key={item.workspace_id} onClick={() => void resume(item.workspace_id)} disabled={Boolean(busy)}>继续 {item.filename || item.workspace_id.slice(-6)}</button>)}</div>}
      </section>
      <section className="reading-pdf" aria-label="PDF">
        {workspace?.pdf ? <iframe key={pdfPage} title={workspace.pdf.filename} src={`${api}/imports/${encodeURIComponent(workspace.pdf.import_id)}/pdf#page=${pdfPage}`} /> : <div className="reading-empty"><strong>还没有打开 PDF</strong><p>选择项目、文件和允许阅读的页码。</p></div>}
      </section>
      <section className="reading-pi" aria-label="Pi">
        <header><strong>Pi {workspace && <small>· {workspace.provider === "deepseek" ? "DeepSeek 外部 API" : "本地 Qwen"}</small>}</strong><span>{workspace ? knowledgeText[workspace.knowledge.status] || workspace.knowledge.status : "请选择项目和 PDF"}</span></header>
        {workspace?.pdf && <p className="reading-scope">授权页 {ranges} · 笔记来源 {workspace.pdf.note_blocks}/{workspace.pdf.total_page_blocks} 块</p>}
        {error && <p className="reading-error" role="alert">{error}</p>}
        <div className="reading-thread">
          {workspace?.turns.map((turn) => <article key={turn.turn_id}><p className="reading-user">{turn.message}</p>{turn.status === "completed" ? <SafeMarkdown content={turn.answer || ""} /> : <p>{turnStateText(turn.status, turn.error)}</p>}{turn.sources?.map((source) => <div className="reading-source" key={source.reference_id || source.title}><strong>{source.title || "来源"}</strong><small>{source.reference_id}</small><p>{source.text}</p>{uniquePdfSources(source.pdf_sources || []).map((pdf) => pdf.import_id === workspace.pdf?.import_id ? <button type="button" key={`${pdf.import_id}-${pdf.anchor?.page_start}`} onClick={() => setPdfPage(pdf.anchor?.page_start || 1)}>打开第 {pdf.anchor?.page_start} 页</button> : <a key={`${pdf.import_id}-${pdf.anchor?.page_start}`} href={`${api}/imports/${encodeURIComponent(pdf.import_id || "")}/pdf#page=${pdf.anchor?.page_start}`} target="_blank" rel="noreferrer">打开来源 PDF 第 {pdf.anchor?.page_start} 页</a>)}</div>)}</article>)}
          {workspace?.proposals.map((proposal) => {
            const id = proposal.proposal.proposal_id;
            const editing = review?.proposalId === id && review.proposalHash === proposal.proposal_hash;
            const recoverable = proposal.phase === "rejected" && Boolean(readReviewDraft(browserStorage(), workspace.stage_id, workspace.workspace_id, id, proposal.proposal_hash)?.submitted);
            return <article className="reading-note" key={id}>
              <span>{proposal.phase === "committed" ? "已保存" : proposal.phase === "rejected" ? "已拒绝或被修订" : proposal.phase === "pending" ? "待审阅" : proposal.phase}</span>
              <h3>{proposal.proposal.normalized_changes?.title || "阅读笔记"}</h3>
              <SafeMarkdown content={proposal.proposal.normalized_changes?.content || ""} />
              <details><summary>核对原文来源</summary>{proposal.proposal.normalized_changes?.source_snapshot?.sources?.map((source) => <div className="reading-source" key={source.source_id}><button type="button" onClick={() => setPdfPage(source.anchor?.page_start || 1)}>打开第 {source.anchor?.page_start || "?"} 页</button><p>{source.text}</p></div>)}</details>
              {["pending", "approved_pending"].includes(proposal.phase) && <p>请逐项核对原文；AI 可能把原文概括得过强。</p>}
              {editing && review && <div className="reading-review-editor"><label>修订标题<input aria-label="修订标题" maxLength={1000} value={review.title} disabled={review.submitted} onChange={(event) => updateReview({ ...review, title: event.target.value })} /></label><label>修订正文<textarea aria-label="修订正文" maxLength={12000} rows={10} value={review.content} disabled={review.submitted} onChange={(event) => updateReview({ ...review, content: event.target.value })} /></label><small>{review.submitted ? "修订已提交处理中断；请重试完成，原版本不会保存。" : reviewStorageAvailable ? "修订草稿仅保存在当前浏览器；生成新提案后仍需确认。" : "浏览器无法保存草稿，离开前请自行复制。"}</small><button type="button" disabled={blocked || !review.title.trim() || !review.content.trim()} onClick={() => void submitReview()}>{review.submitted ? "重试完成修订" : "生成修订提案"}</button>{!review.submitted && <button type="button" onClick={() => { writeReviewDraft(browserStorage(), workspace.stage_id, workspace.workspace_id, null, id, proposal.proposal_hash); setReview(null); }}>取消编辑</button>}</div>}
              {!editing && (proposal.phase === "pending" || recoverable) && <button type="button" disabled={blocked} onClick={() => startReview(proposal)}>{recoverable ? "恢复未完成修订" : "编辑提案"}</button>}
              {!editing && ["pending", "approved_pending"].includes(proposal.phase) && <div><button type="button" onClick={() => void decide(proposal, "approve")} disabled={blocked}>确认保存并入库</button><button type="button" onClick={() => void decide(proposal, "reject")} disabled={blocked}>拒绝</button></div>}
            </article>;
          })}
          {workspace && (["failed", "stale", "interrupted"].includes(workspace.knowledge.status) || (workspace.knowledge.status === "not_queued" && workspace.proposals.some((item) => item.phase === "committed"))) && <p className="reading-error">笔记已保存，知识入库尚未完成。{workspace.knowledge.error_code && ` ${workspace.knowledge.error_code}`} <button type="button" onClick={() => void retryIndex()} disabled={blocked}>重试入库</button></p>}
        </div>
        <form onSubmit={(event) => { event.preventDefault(); void send("chat"); }}>
          {workspace?.provider === "deepseek" && <label className="reading-consent"><input type="checkbox" checked={externalConsent} onChange={(event) => setExternalConsent(event.target.checked)} />我允许将所选 PDF 授权页及本项目检索到的知识发送至 DeepSeek。</label>}
          <button type="button" onClick={() => void send("note")} disabled={!workspace || blocked}>生成阅读笔记</button>
          <button type="button" onClick={() => void send("knowledge")} disabled={!workspace || !canAskKnowledge(workspace) || !message.trim() || blocked}>从知识库回答</button>
          <textarea aria-label="给 Pi 发消息" value={message} onChange={(event) => { const value = event.target.value; setMessage(value); messageRef.current = value; if (workspace) setDraftStorageAvailable(writeDraft(browserStorage(), workspace.stage_id, workspace.workspace_id, value)); }} placeholder="询问当前授权页" />
          {workspace && <small>{draftStorageAvailable ? "草稿保存在当前浏览器。" : "浏览器无法保存草稿，离开前请自行复制。"}</small>}
          <button type="submit" disabled={!workspace || !message.trim() || blocked}>发送</button>
        </form>
      </section>
    </main>
  );
}
