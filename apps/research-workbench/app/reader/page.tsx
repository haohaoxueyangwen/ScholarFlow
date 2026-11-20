"use client";

import { useEffect, useState } from "react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { CitationGraph } from "@/app/components/citation-graph";
import { PaperReader } from "@/app/components/paper-reader";
import { API_BASE, apiGet, apiPost, establishWorkbenchSession } from "@/app/lib/api";
import { buildReaderHandoff, READER_HANDOFF_CHANNEL, storeReaderHandoff } from "@/app/lib/reader-handoff";
import { statusLabel, text } from "@/app/lib/presentation";
import type {
  ConnectionState,
  Item,
  PaperReaderData,
  ReaderAnnotation,
  ReaderImport,
} from "@/app/lib/types";

const AnnotatablePdf = dynamic(
  () => import("@/app/components/annotatable-pdf").then((module) => module.AnnotatablePdf),
  {
    ssr: false,
    loading: () => <div className="reader-fullstage-loading">正在准备大阅读页…</div>,
  },
);

type ReadingView = "pdf" | "structure" | "citations";

const pdfReason: Record<string, string> = {
  paper_pdf_not_linked: "这条论文尚未关联 Zotero 条目。",
  paper_pdf_not_found: "Zotero 条目中没有可读取的 PDF 附件。",
  paper_pdf_zotero_local_required: "请保持 Zotero Desktop 打开并启用本地 API。",
  paper_pdf_zotero_unavailable: "Zotero 当前无法解析这份附件。",
  paper_pdf_invalid_source: "Zotero 返回的附件不是受支持的本地 PDF。",
  paper_pdf_too_large: "PDF 超过当前 80 MB 阅读上限。",
};

function authors(value: unknown) {
  return Array.isArray(value) ? value.map((item) => text(item)).filter(Boolean).join("、") : "";
}

export default function ReaderPage() {
  const searchParams = useSearchParams();
  const kind = searchParams.get("kind") === "import" ? "import" : "paper";
  const id = searchParams.get("id") || "";
  const initialView = searchParams.get("view");
  const defaultView: ReadingView = initialView === "structure" || initialView === "citations" ? initialView : "pdf";

  const [connection, setConnection] = useState<ConnectionState>("loading");
  const [view, setView] = useState<ReadingView>(defaultView);
  const [readerData, setReaderData] = useState<PaperReaderData | null>(null);
  const [importRecord, setImportRecord] = useState<ReaderImport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [pdfRevision, setPdfRevision] = useState(0);
  const [annotations, setAnnotations] = useState<ReaderAnnotation[]>([]);
  const [focusMode, setFocusMode] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);
  const [handoffStatus, setHandoffStatus] = useState("");
  const [targetSessionId, setTargetSessionId] = useState("");
  const [targetSessions, setTargetSessions] = useState<Item[]>([]);
  useEffect(() => {
    if (connection !== "connected") return;
    let cancelled = false;
    const refresh = () => void apiGet<{ items: Item[] }>("/agent/sessions")
      .then((result) => { if (!cancelled) setTargetSessions(result.items); })
      .catch(() => { if (!cancelled) setHandoffStatus("无法读取目标会话；摘录仍可保留到工作台手动接收。"); });
    refresh();
    const timer = window.setInterval(refresh, 15000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [connection]);
  useEffect(() => {
    const sync = () => setFullscreen(Boolean(document.fullscreenElement));
    document.addEventListener("fullscreenchange", sync);
    return () => document.removeEventListener("fullscreenchange", sync);
  }, []);
  async function toggleFullscreen() {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await document.documentElement.requestFullscreen();
    } catch { setHandoffStatus("浏览器未开启全屏，可使用 F11 或纯净阅读。"); }
  }

  useEffect(() => {
    if (typeof BroadcastChannel === "undefined") return;
    const channel = new BroadcastChannel(READER_HANDOFF_CHANNEL);
    channel.onmessage = (event: MessageEvent) => {
      if (event.data?.type === "accepted") setHandoffStatus("已送到 ScholarFlow 工作台的 Pi 输入框；检查后再发送。");
    };
    return () => channel.close();
  }, []);

  useEffect(() => {
    let cancelled = false;
    void establishWorkbenchSession()
      .then(() => {
        if (!cancelled) setConnection("connected");
      })
      .catch(() => {
        if (!cancelled) setConnection("offline");
      });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!id || connection !== "connected") return;
    let cancelled = false;
    const readerPath = kind === "import"
      ? `/reader/imports/${encodeURIComponent(id)}/reader?limit=500`
      : `/papers/${encodeURIComponent(id)}/reader?limit=500`;

    const tasks = kind === "import"
      ? Promise.all([
          apiGet<PaperReaderData>(readerPath),
          apiGet<{ items?: ReaderImport[] }>("/reader/imports"),
        ]).then(([reader, imports]) => ({ reader, record: (imports.items ?? []).find((item) => item.import_id === id) ?? null }))
      : Promise.all([
          apiGet<PaperReaderData>(readerPath),
          Promise.resolve({ items: [] as ReaderImport[] }),
        ]).then(([reader]) => ({ reader, record: null }));

    void tasks
      .then(({ reader, record }) => {
        if (cancelled) return;
        setReaderData(reader);
        setImportRecord(record);
      })
      .catch((caught) => {
        if (cancelled) return;
        setError(caught instanceof Error ? caught.message : "独立阅读页加载失败");
        setReaderData(null);
        setImportRecord(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [connection, id, kind]);

  const annotationTarget = id ? (kind === "import" ? `local_pdf:${id}` : `paper:${id}`) : "";
  useEffect(() => {
    if (!annotationTarget || connection !== "connected") return;
    let cancelled = false;
    void apiGet<{ items: ReaderAnnotation[] }>(`/reader/annotations?target_ref=${encodeURIComponent(annotationTarget)}`)
      .then((result) => {
        if (!cancelled) setAnnotations(result.items ?? []);
      })
      .catch(() => {
        if (!cancelled) setAnnotations([]);
      });
    return () => { cancelled = true; };
  }, [annotationTarget, connection]);

  async function createAnnotation(payload: Pick<ReaderAnnotation, "page" | "quote" | "note" | "color" | "anchor">) {
    const created = await apiPost<ReaderAnnotation>("/reader/annotations", {
      target_ref: annotationTarget,
      ...payload,
    });
    setAnnotations((current) => [...current, created]);
  }

  async function deleteAnnotation(annotationId: string) {
    await apiPost<Item>(`/reader/annotations/${encodeURIComponent(annotationId)}/delete`, {});
    setAnnotations((current) => current.filter((item) => item.annotation_id !== annotationId));
  }

  function handoffAnnotation(annotation: ReaderAnnotation) {
    const rawProjects = importRecord?.project_stage_ids ?? readerData?.paper?.project_stage_ids;
    const projectIds = Array.isArray(rawProjects) ? rawProjects.filter((value): value is string => typeof value === "string") : [];
    const handoff = buildReaderHandoff({ title: activeTitle, sourceRef: annotationTarget, page: annotation.page, quote: annotation.quote, note: annotation.note, projectIds, sessionId: targetSessionId });
    try {
      storeReaderHandoff(handoff, window.localStorage);
      if (typeof BroadcastChannel !== "undefined") {
        const channel = new BroadcastChannel(READER_HANDOFF_CHANNEL);
        channel.postMessage({ type: "prepare", handoff });
        window.setTimeout(() => channel.close(), 1000);
      }
      setHandoffStatus(targetSessionId ? "摘录已排队到指定会话。请在工作台选择该会话并接收；不会自动发送给 Pi。" : "已送到 ScholarFlow 工作台。Pi 只会在你检查并点击发送后收到这段内容。");
    } catch {
      setHandoffStatus("浏览器暂存失败；请复制原文和批注到工作台。");
    }
  }

  const readerPath = kind === "import"
    ? `/reader/imports/${encodeURIComponent(id)}`
    : `/papers/${encodeURIComponent(id)}`;
  const activeTitle = kind === "import"
    ? text(importRecord?.title || readerData?.document?.title || readerData?.paper?.title, "PDF 阅读")
    : text(readerData?.paper?.title || readerData?.document?.title, "PDF 阅读");
  const activeAuthors = kind === "import"
    ? authors(importRecord?.authors || readerData?.document?.authors || [])
    : authors(readerData?.paper?.authors || readerData?.document?.authors || []);
  const pdfFile = kind === "import"
    ? `${API_BASE}/reader/imports/${encodeURIComponent(id)}/pdf`
    : readerData?.pdf?.available
      ? `${API_BASE}/papers/${encodeURIComponent(id)}/pdf`
      : null;
  const available = Boolean(id) && (kind === "import" || readerData?.paper);

  return (
    <main className={`reader-fullstage${focusMode ? " focus-mode" : ""}`}>
      <header className="reader-fullstage-bar">
        <div className="reader-fullstage-title">
          <span>{kind === "import" ? "本地 PDF 阅读器" : "论文阅读器"}</span>
          <h1 title={activeTitle}>{activeTitle}</h1>
          <p>{activeAuthors || "专注阅读模式，适合单独打开进行大窗口查看。"}</p>
        </div>
        <div className="reader-fullstage-actions">
          <div className="reader-fullstage-switch" aria-label="阅读视图">
            <button className={view === "pdf" ? "active" : ""} onClick={() => setView("pdf")}>PDF</button>
            <button className={view === "structure" ? "active" : ""} onClick={() => setView("structure")}>结构化阅读</button>
            <button className={view === "citations" ? "active" : ""} onClick={() => setView("citations")}>引用图谱</button>
          </div>
          <div className="reader-fullstage-utility">
            <label>摘录目标 <select aria-label="摘录目标会话" value={targetSessionId} onChange={(event) => setTargetSessionId(event.target.value)}>
              <option value="">工作台手动接收</option>
              {targetSessions.map((session) => <option key={text(session.session_id)} value={text(session.session_id)}>{text(session.title || session.name, "研究会话")} · {text(session.scope, "global")} · {text(session.session_id).slice(-8)}</option>)}
            </select></label>
            <button className="reader-focus-toggle" onClick={() => void toggleFullscreen()}>{fullscreen ? "退出全屏" : "全屏阅读"}</button>
            <button className={`reader-focus-toggle${focusMode ? " active" : ""}`} onClick={() => setFocusMode((current) => !current)}>
              {focusMode ? "退出纯净模式" : "纯净阅读"}
            </button>
            <Link className="reader-fullstage-back" href="/">返回工作台</Link>
          </div>
        </div>
      </header>
      {focusMode && <button className="reader-exit-focus" onClick={() => setFocusMode(false)}>退出纯净阅读</button>}

      <section className="reader-fullstage-meta">
        <article><span>阅读模式</span><strong>{view === "pdf" ? "原始 PDF" : view === "structure" ? "结构化阅读" : "引用图谱"}</strong></article>
        <article><span>连接状态</span><strong>{connection === "connected" ? "已连接" : connection === "loading" ? "连接中" : "离线"}</strong></article>
        <article><span>文档来源</span><strong>{kind === "import" ? "本地暂存 PDF" : statusLabel(readerData?.paper?.status)}</strong></article>
      </section>

      {!id && <section className="reader-fullstage-empty"><h2>没有指定要打开的论文</h2><p>请回到文献工作台，选择论文后使用“独立窗口打开”。</p></section>}
      {handoffStatus && <p className="reader-handoff-status" role="status">{handoffStatus}</p>}
      {error && <section className="reader-fullstage-empty warning"><h2>独立阅读页加载失败</h2><p>{error}</p></section>}
      {loading && id && <section className="reader-fullstage-loading">正在打开独立阅读页…</section>}

      {!loading && !error && available && (
        <section className="reader-fullstage-body reader-notebook-layout">
          {view === "pdf" && (
            <div className="reader-fullstage-panel pdf">
              {pdfFile && annotationTarget ? (
                <AnnotatablePdf
                  key={`${annotationTarget}-${pdfRevision}`}
                  file={pdfFile}
                  targetRef={annotationTarget}
                  annotations={annotations}
                  onCreate={createAnnotation}
                  onDelete={deleteAnnotation}
                  onAskPi={handoffAnnotation}
                  continuous
                  notebook={!focusMode}
                />
              ) : (
                <div className="pdf-unavailable fullpage">
                  <span>PDF</span>
                  <h3>当前还没有可直接显示的 PDF</h3>
                  <p>{pdfReason[readerData?.pdf?.reason ?? ""] || "这条记录暂时没有可用的 PDF 源。"}</p>
                  <div>
                    <button className="ghost-button" onClick={() => setPdfRevision((current) => current + 1)}>重新载入</button>
                    <button className="primary-button" onClick={() => setView("structure")}>查看结构化阅读</button>
                  </div>
                </div>
              )}
            </div>
          )}

          {view === "structure" && (
            <div className="reader-fullstage-panel structure">
              <PaperReader
                paperId={id}
                readerPath={kind === "import" ? readerPath : undefined}
                connection={connection}
              />
            </div>
          )}

          {view === "citations" && (
            <div className="reader-fullstage-panel structure">
              <CitationGraph title={activeTitle} reader={readerData} />
            </div>
          )}
        </section>
      )}
    </main>
  );
}
