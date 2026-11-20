"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { TranslationPopup } from "@/app/components/translation-popup";
import { Document, Page, pdfjs } from "react-pdf";
import "react-pdf/dist/Page/AnnotationLayer.css";
import "react-pdf/dist/Page/TextLayer.css";
import { apiPost, apiResumeReaderTranslation, apiRunReaderTranslation, ReaderTranslationTerminalError, type ReaderTranslationJob } from "@/app/lib/api";
import type { PdfTextAnchor, ReaderAnnotation } from "@/app/lib/types";
import { capturePdfAnchor } from "@/app/lib/pdf-anchors";

pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  "pdfjs-dist/build/pdf.worker.min.mjs",
  import.meta.url,
).toString();

const colorLabels: Record<ReaderAnnotation["color"], string> = {
  yellow: "重点",
  mint: "方法",
  blue: "证据",
  rose: "疑问",
};

function shouldAutoOpenTranslateQuote(quote: string) {
  const trimmed = quote.trim().replace(/\s+/g, " ");
  if (!trimmed) return false;
  if (/[\u3400-\u9fff]/.test(trimmed)) return trimmed.length >= 6;
  return trimmed.length >= 12 && trimmed.split(" ").length >= 2;
}

function escapeHtml(value: string) {
  return value.replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#039;",
  })[char] || char);
}

export function AnnotatablePdf({
  file,
  targetRef,
  annotations,
  onCreate,
  onDelete,
  onAskPi,
  continuous = false,
  notebook = false,
}: {
  file: string | File;
  targetRef: string;
  annotations: ReaderAnnotation[];
  onCreate: (payload: Pick<ReaderAnnotation, "page" | "quote" | "note" | "color" | "anchor">) => Promise<void>;
  onDelete: (annotationId: string) => Promise<void>;
  onAskPi: (annotation: ReaderAnnotation) => void;
  continuous?: boolean;
  notebook?: boolean;
}) {
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const pageRefs = useRef(new Map<number, HTMLDivElement>());
  const [pageCount, setPageCount] = useState(0);
  const [page, setPage] = useState(1);
  const [width, setWidth] = useState(640);
  const [zoom, setZoom] = useState(1);
  const [quote, setQuote] = useState("");
  const [note, setNote] = useState("");
  const [translationPage, setTranslationPage] = useState(1);
  const [color, setColor] = useState<ReaderAnnotation["color"]>("yellow");
  const [showNotes, setShowNotes] = useState(notebook);
  const [noteQuery, setNoteQuery] = useState("");
  const [notesScope, setNotesScope] = useState<"all" | "page">("all");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [fingerprint, setFingerprint] = useState("");
  const [anchor, setAnchor] = useState<PdfTextAnchor | null>(null);
  const [draftPage, setDraftPage] = useState(1);
  const [drawerMode, setDrawerMode] = useState<"translate" | "notes">("notes");
  const [translateSource, setTranslateSource] = useState("");
  const [translateResult, setTranslateResult] = useState("");
  const [translateResultSource, setTranslateResultSource] = useState("");
  const [translateBusy, setTranslateBusy] = useState(false);
  const translateBusyRef = useRef(false);
  const updateTranslationBusy = useCallback((busy: boolean) => { translateBusyRef.current = busy; setTranslateBusy(busy); }, []);
  const [translateStatus, setTranslateStatus] = useState("");
  const [translateTaskId, setTranslateTaskId] = useState("");
  const [translateTaskSource, setTranslateTaskSource] = useState("");
  const [translateError, setTranslateError] = useState("");
  const restored = useRef(false);
  const translationStartedHere = useRef(false);
  const translateAbortRef = useRef<AbortController | null>(null);
  const translateRunRef = useRef(0);

  useEffect(() => {
    let saved: { page?: number; zoom?: number; quote?: string; note?: string; anchor?: PdfTextAnchor; draftPage?: number; translationPage?: number; translateSource?: string; translateResult?: string; translateResultSource?: string; translateTaskId?: string; translateTaskSource?: string } = {};
    try { saved = JSON.parse(sessionStorage.getItem(`sf-reader:${targetRef}`) || "{}"); } catch { /* optional local state */ }
    const timer = window.setTimeout(() => {
      setPage(saved.page || 1); setZoom(saved.zoom || 1); setQuote(saved.quote || ""); setNote(saved.note || ""); setAnchor(saved.anchor || null); setDraftPage(saved.draftPage || saved.page || 1); setTranslationPage(saved.translationPage || saved.page || 1); setTranslateSource(saved.translateSource || ""); setTranslateResult(saved.translateResult || ""); setTranslateResultSource(saved.translateResultSource || ""); setTranslateTaskId(saved.translateTaskId || ""); setTranslateTaskSource(saved.translateTaskSource || ""); restored.current = true;
    }, 0);
    return () => window.clearTimeout(timer);
  }, [targetRef]);

  useEffect(() => {
    if (!restored.current) return;
    try { sessionStorage.setItem(`sf-reader:${targetRef}`, JSON.stringify({ page, zoom, quote, note, anchor, draftPage, translationPage, translateSource, translateResult, translateResultSource, translateTaskId, translateTaskSource })); } catch { /* storage can be disabled */ }
  }, [anchor, draftPage, note, page, quote, targetRef, translateResult, translateResultSource, translateSource, translateTaskId, translateTaskSource, translationPage, zoom]);

  useEffect(() => {
    const body = bodyRef.current;
    const viewport = viewportRef.current;
    if (!body || !viewport) return;
    let frame = 0;
    const measure = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        const currentTarget = viewportRef.current;
        if (!currentTarget) return;
        const style = getComputedStyle(currentTarget);
        const padding = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
        const nextWidth = Math.max(160, Math.floor(currentTarget.clientWidth - padding));
        setWidth((current) => (Math.abs(current - nextWidth) < 2 ? current : nextWidth));
      });
    };
    let observer: ResizeObserver | null = null;
    measure();
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(() => {
        measure();
      });
      observer.observe(viewport);
    }
    window.addEventListener("resize", measure, { passive: true });
    document.addEventListener("fullscreenchange", measure);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", measure);
      document.removeEventListener("fullscreenchange", measure);
      window.cancelAnimationFrame(frame);
    };
  }, []);

  const pageAnnotations = useMemo(
    () => annotations.filter((item) => (notesScope === "all" || item.page === page) && (item.quote + " " + item.note).toLowerCase().includes(noteQuery.toLowerCase())),
    [annotations, page, notesScope, noteQuery],
  );
  const renderedPages = continuous && pageCount > 0
    ? Array.from({ length: pageCount }, (_, index) => index + 1)
    : [page];
  const showingPreviousTranslation = Boolean(
    translateResult
    && translateResultSource.trim()
    && translateResultSource.trim() !== translateSource.trim(),
  );

  useEffect(() => {
    if (!continuous) return;
    const viewport = viewportRef.current;
    if (!viewport) return;
    let animationFrame = 0;
    const updateCurrentPage = () => {
      const viewportTop = viewport.getBoundingClientRect().top;
      const viewportMiddle = viewportTop + viewport.clientHeight * 0.28;
      let low = 1;
      let high = pageCount;
      while (low < high) {
        const middle = Math.floor((low + high) / 2);
        const top = pageRefs.current.get(middle)?.getBoundingClientRect().top ?? Number.POSITIVE_INFINITY;
        if (top < viewportMiddle) low = middle + 1;
        else high = middle;
      }
      const candidates = [low, Math.max(1, low - 1)];
      const closestPage = candidates.reduce((best, candidate) => {
        const top = pageRefs.current.get(candidate)?.getBoundingClientRect().top ?? Number.POSITIVE_INFINITY;
        const bestTop = pageRefs.current.get(best)?.getBoundingClientRect().top ?? Number.POSITIVE_INFINITY;
        return Math.abs(top - viewportMiddle) < Math.abs(bestTop - viewportMiddle) ? candidate : best;
      }, candidates[0]);
      setPage((current) => current === closestPage ? current : closestPage);
    };
    const scheduleUpdate = () => {
      if (animationFrame) return;
      animationFrame = window.requestAnimationFrame(() => { animationFrame = 0; updateCurrentPage(); });
    };
    scheduleUpdate();
    viewport.addEventListener("scroll", scheduleUpdate, { passive: true });
    return () => { window.cancelAnimationFrame(animationFrame); viewport.removeEventListener("scroll", scheduleUpdate); };
  }, [continuous, pageCount]);

  function goToPage(nextPage: number) {
    const normalized = Math.max(1, Math.min(pageCount || 1, nextPage));
    setPage(normalized);
    if (continuous) {
      // Keep virtualized page navigation atomic. Smooth scrolling emits many
      // intermediate scroll events; the viewport tracker then moves the render
      // window away from the requested page before the animation reaches it.
      pageRefs.current.get(normalized)?.scrollIntoView({ block: "start" });
    }
  }

  const readCurrentSelection = useCallback((clearSelection = false) => {
    const selected = viewportRef.current ? capturePdfAnchor(viewportRef.current, window.getSelection(), fingerprint) : null;
    if (!selected) {
      if (clearSelection) window.getSelection()?.removeAllRanges();
      return null;
    }
    if (clearSelection) window.getSelection()?.removeAllRanges();
    return selected;
  }, [fingerprint]);

  function captureSelection() {
    const selected = readCurrentSelection(true);
    if (!selected) {
      setError("先在当前 PDF 页面中选中一段文字。扫描件如果没有文字层，需要先完成 OCR。");
      return;
    }
    setQuote(selected.quote);
    setAnchor(selected.anchor);
    setDraftPage(selected.anchor.page);
    setTranslationPage(selected.anchor.page);
    setShowNotes(true);
    setDrawerMode("notes");
    setError("");
  }

  function captureForTranslation() {
    const selected = readCurrentSelection(true);
    if (!selected) {
      setError("先在当前 PDF 页面中选中一段文字，翻译浮窗才会出现。");
      return null;
    }
    setTranslateSource(selected.quote);
    setTranslateStatus("已载入选中文本，准备翻译。");
    setTranslationPage(selected.anchor.page);
    setDrawerMode("translate");
    setTranslateError("");
    setError("");
    return selected.quote;
  }

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport || !fingerprint) return;
    let timer: number | undefined;
    const autoOpenTranslation = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        const selected = readCurrentSelection(false);
        if (!selected || !shouldAutoOpenTranslateQuote(selected.quote)) return;
        if (
          drawerMode === "translate"
          && translateSource.trim() === selected.quote.trim()
        ) return;
        setTranslateSource((current) => current === selected.quote ? current : selected.quote);
        setTranslateStatus("已自动捕获选区，可直接翻译。");
        setTranslationPage(selected.anchor.page);
        setDrawerMode("translate");
        setTranslateError("");
        setError("");
      }, 180);
    };
    viewport.addEventListener("mouseup", autoOpenTranslation);
    viewport.addEventListener("keyup", autoOpenTranslation);
    return () => {
      window.clearTimeout(timer);
      viewport.removeEventListener("mouseup", autoOpenTranslation);
      viewport.removeEventListener("keyup", autoOpenTranslation);
    };
  }, [drawerMode, fingerprint, readCurrentSelection, showNotes, translateSource]);

  const cancelTranslation = useCallback((reason = "已取消当前翻译。", quiet = false) => {
    const hadActiveRequest = Boolean(translateAbortRef.current);
    translateRunRef.current += 1;
    translateAbortRef.current?.abort(new Error(reason));
    translateAbortRef.current = null;
    setTranslateTaskId("");
    updateTranslationBusy(false);
    setTranslateError("");
    if (!quiet || hadActiveRequest) setTranslateStatus(reason);
  }, [updateTranslationBusy]);

  useEffect(() => () => cancelTranslation("翻译已结束。"), [cancelTranslation]);

  useEffect(() => {
    if (!restored.current || translationStartedHere.current || !translateTaskId || translateBusyRef.current || translateTaskSource.trim() !== translateSource.trim() || (translateResult && translateResultSource.trim() === translateSource.trim())) return;
    const controller = new AbortController();
    translateAbortRef.current = controller;
    const timer = window.setTimeout(() => {
      updateTranslationBusy(true);
      setTranslateStatus("正在恢复这份阅读器中的翻译任务…");
      void apiResumeReaderTranslation(translateTaskId, controller.signal, (job: ReaderTranslationJob) => {
        setTranslateStatus(job.status === "queued" ? "翻译任务排队中…" : job.status === "running" ? "翻译进行中…" : "正在核对翻译结果…");
      }).then((job) => {
        if (controller.signal.aborted) return;
        setTranslateResult(job.translation || "");
        setTranslateResultSource(translateTaskSource);
        setTranslateTaskId("");
        setTranslateTaskSource("");
        setTranslateStatus("翻译完成。");
      }).catch((caught) => { if (!controller.signal.aborted) { setTranslateStatus("翻译未完成。"); setTranslateError(caught instanceof Error ? caught.message : "翻译失败。"); setTranslateTaskId(""); setTranslateTaskSource(""); } }).finally(() => {
        if (translateAbortRef.current === controller) translateAbortRef.current = null;
        updateTranslationBusy(false);
      });
    }, 0);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [targetRef, translateResult, translateResultSource, translateSource, translateTaskId, translateTaskSource, updateTranslationBusy]);

  async function runTranslation(explicitQuote?: string) {
    const source = (explicitQuote || translateSource).trim();
    if (!source) {
      const captured = captureForTranslation();
      if (!captured) return;
      return runTranslation(captured);
    }
    translationStartedHere.current = true;
    const matchesPendingTask = Boolean(translateTaskId && translateTaskSource.trim() === source);
    const taskId = matchesPendingTask ? translateTaskId : `tr_${crypto.randomUUID().replaceAll("-", "")}`;
    if (translateTaskId && !matchesPendingTask) void apiPost(`/reader/translations/${encodeURIComponent(translateTaskId)}/cancel`, {}, { timeoutMs: 12000 }).catch(() => {});
    cancelTranslation("已取消上一轮翻译，开始新的请求。", true);
    const controller = new AbortController();
    const runId = translateRunRef.current + 1;
    translateRunRef.current = runId;
    translateAbortRef.current = controller;
    updateTranslationBusy(true);
    setTranslateError("");
    setTranslateStatus("正在调用独立翻译 API…");
    try {
      if (translateRunRef.current !== runId) return;
      setTranslateStatus("正在向独立翻译 API 提交请求…");
      setTranslateTaskId(taskId);
      setTranslateTaskSource(source);
      sessionStorage.setItem(`sf-reader:${targetRef}`, JSON.stringify({ page, zoom, quote, note, anchor, draftPage, translationPage, translateSource: source, translateResult, translateResultSource, translateTaskId: taskId, translateTaskSource: source }));
      const payload = await apiRunReaderTranslation(source, taskId, controller.signal, (job: ReaderTranslationJob) => {
        setTranslateTaskId(job.task_id);
        setTranslateStatus(job.status === "queued" ? "翻译任务排队中…" : job.status === "running" ? "翻译进行中…" : "正在读取翻译结果…");
      });
      if (controller.signal.aborted || translateRunRef.current !== runId) return;
      setTranslateResult(String(payload.translation || "").trim());
      setTranslateResultSource(source);
      setTranslateTaskId("");
      setTranslateTaskSource("");
      setTranslateStatus("翻译完成。");
      // Keep a dismissed popup closed when a background request finishes.
    } catch (caught) {
      if (controller.signal.aborted || translateRunRef.current !== runId) return;
      setTranslateStatus("翻译未完成。");
      setTranslateError(caught instanceof Error ? caught.message : "翻译失败。");
      if (caught instanceof ReaderTranslationTerminalError) { setTranslateTaskId(""); setTranslateTaskSource(""); }
    } finally {
      if (translateRunRef.current === runId) {
        translateAbortRef.current = null;
        updateTranslationBusy(false);
      }
    }
  }

  async function saveAnnotation() {
    if (!quote.trim() && !note.trim()) return;
    setBusy(true);
    setError("");
    try {
      await onCreate({ page: draftPage, quote: quote.trim(), note: note.trim(), color, anchor });
      setQuote("");
      setNote("");
      setAnchor(null);
      setShowNotes(true);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "批注保存失败。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className={`annotatable-pdf${notebook ? " notebook-reader" : ""}`} aria-label="可批注 PDF 阅读器">
      <div className="pdf-reader-toolbar">
        <div>
          <button disabled={page <= 1} onClick={() => goToPage(page - 1)}>←</button>
          <label><input type="number" min={1} max={pageCount || 1} value={page} onChange={(event) => goToPage(Number(event.target.value) || 1)} /><span>/ {pageCount || "—"}</span></label>
          <button disabled={!pageCount || page >= pageCount} onClick={() => goToPage(page + 1)}>→</button>
        </div>
        <div>
          <button onClick={() => setZoom((current) => Math.max(.7, current - .1))}>−</button>
          <span>{Math.round(zoom * 100)}%</span>
          <button onClick={() => setZoom((current) => Math.min(1.8, current + .1))}>＋</button>
          <button className="highlight-selection" onClick={captureSelection}>高亮所选</button>
          <button onClick={() => setDrawerMode("translate")}>翻译窗口</button>
          <button className="translate-selection" onClick={() => { const selected = captureForTranslation(); if (selected) void runTranslation(selected); }}>翻译所选</button>
          <button className={showNotes ? "active" : ""} onClick={() => { if (!quote && !note) setDraftPage(page); setShowNotes((current) => !current); }}>批注 {annotations.length}</button>
        </div>
      </div>
      {error && <div className="pdf-reader-error">{error}</div>}
      <div className="pdf-reader-body" ref={bodyRef}>
        <div className="pdf-page-viewport" ref={viewportRef} tabIndex={0} role="region" aria-label="PDF 页面阅读区">
          <Document
            file={file}
            loading={<div className="literature-loading">正在加载 PDF 与文字层…</div>}
            error={<div className="pdf-unavailable"><h3>PDF 加载失败</h3><p>请重新打开文件，或确认 Zotero 仍在运行。</p></div>}
            onLoadSuccess={({ numPages, fingerprints }) => {
              setFingerprint(fingerprints[0] || "");
              setPageCount(numPages);
              setPage((current) => Math.min(Math.max(current, 1), numPages));
            }}
            onLoadError={(reason) => setError(reason instanceof Error ? reason.message : "PDF 加载失败。")}
          >
            {renderedPages.map((pageNumber) => {
              const pageHighlights = annotations.filter((annotation) => annotation.page === pageNumber && annotation.anchor?.document_fingerprint === fingerprint);
              return (
                <div
                  key={pageNumber}
                  className={`pdf-page-shell${pageNumber === page ? " active" : ""}`}
                  data-page-number={pageNumber}
                  ref={(node) => {
                    if (node) pageRefs.current.set(pageNumber, node);
                    else pageRefs.current.delete(pageNumber);
                  }}
                >
                  <Page
                    pageNumber={pageNumber}
                    width={Math.round(width * zoom)}
                    renderTextLayer
                    renderAnnotationLayer
                    customTextRenderer={({ str, itemIndex }) => `<span data-sf-text-item="${itemIndex}">${escapeHtml(str)}</span>`}
                  >
                    <div className="pdf-highlight-layer" aria-hidden="true">{pageHighlights.flatMap((annotation) => annotation.anchor!.rects.map((rect, index) => <span key={`${annotation.annotation_id}:${index}`} className={`pdf-highlight ${annotation.color}`} style={{ left: `${rect.x * 100}%`, top: `${rect.y * 100}%`, width: `${rect.width * 100}%`, height: `${rect.height * 100}%` }} />))}</div>
                  </Page>
                </div>
              );
            })}
          </Document>
        </div>
        {showNotes && <aside className="pdf-annotation-drawer" aria-label="笔记与批注">
          <div className="notebook-heading"><strong>笔记与批注</strong><button onClick={() => { if (!quote && !note) setDraftPage(page); document.getElementById("reader-note-input")?.focus(); }}>写笔记</button></div>

          <div className="annotation-editor">
            <strong>第 {draftPage} 页批注{draftPage !== page ? "（草稿保留在选中页）" : ""}</strong>
            <textarea aria-label="原文摘录" value={quote} onChange={(event) => { setQuote(event.target.value); setAnchor(null); }} placeholder="先在 PDF 中划词，或手动粘贴原文摘录" maxLength={5000} />
            <textarea id="reader-note-input" aria-label="阅读笔记" value={note} onChange={(event) => setNote(event.target.value)} placeholder="写下你的理解、疑问或下一步" maxLength={5000} />
            <div className="annotation-colors">{(Object.keys(colorLabels) as ReaderAnnotation["color"][]).map((key) => <button key={key} className={`${key} ${color === key ? "active" : ""}`} onClick={() => setColor(key)}>{colorLabels[key]}</button>)}</div>
            <button className="primary-button" disabled={busy || (!quote.trim() && !note.trim())} onClick={() => void saveAnnotation()}>{busy ? "保存中…" : "保存批注"}</button>
          </div>
          <div className="annotation-filter"><input aria-label="搜索批注" placeholder="搜索摘录和笔记" value={noteQuery} onChange={(event) => setNoteQuery(event.target.value)} /><select aria-label="批注范围" value={notesScope} onChange={(event) => setNotesScope(event.target.value as "all" | "page")}><option value="all">全文批注</option><option value="page">当前页</option></select></div>
          <div className="annotation-list">
            {pageAnnotations.map((annotation) => <article key={annotation.annotation_id} className={annotation.color}>
              <button className="annotation-page-link" onClick={() => goToPage(annotation.page)}>第 {annotation.page} 页 · {colorLabels[annotation.color]}</button>
              {annotation.quote && <blockquote>{annotation.quote}</blockquote>}
              {annotation.note && <p>{annotation.note}</p>}
              {!annotation.anchor && <small>未定位笔记：不在原文中猜测高亮位置。</small>}
              {annotation.anchor && annotation.anchor.document_fingerprint !== fingerprint && <small>文档版本变化，请重新定位原文。</small>}
              <div><button onClick={() => onAskPi(annotation)}>交给 Pi</button><button onClick={() => void onDelete(annotation.annotation_id).catch((caught) => setError(caught instanceof Error ? caught.message : "删除失败"))}>删除</button></div>
            </article>)}
            {!pageAnnotations.length && <p className="annotation-empty">暂无匹配批注。可以划词高亮，也可以直接写笔记。</p>}
          </div>
        </aside>}
        {drawerMode === "translate" && <TranslationPopup onClose={() => setDrawerMode("notes")}>
          <div className="annotation-translate-panel">
            <strong>第 {translationPage} 页 · 选段翻译</strong>
            <textarea aria-label="待翻译原文" value={translateSource} onChange={(event) => setTranslateSource(event.target.value)} placeholder="先在 PDF 中选中一段原文，再点“翻译所选”" maxLength={5000} />
            <div className="translation-actions">
              <button className="primary-button" disabled={translateBusy || !translateSource.trim()} onClick={() => void runTranslation(translateSource)}>
                {translateBusy ? "翻译中…" : translateResultSource.trim() === translateSource.trim() && translateResult ? "重新翻译当前选段" : "翻译当前选段"}
              </button>
              {translateBusy && <button className="secondary-button" onClick={() => cancelTranslation()}>取消当前翻译</button>}
            </div>
            {translateStatus && <p className={`translation-status${translateBusy ? " pending" : translateResult ? " success" : ""}`}>{translateStatus}</p>}
            {translateError && <p className="translation-error">{translateError}</p>}
            <div className="translation-result" aria-live="polite">
              {translateResult ? <article><span>{showingPreviousTranslation ? "上一条翻译结果" : "中文翻译"}</span>{showingPreviousTranslation && <small>当前显示的是上一段选文的翻译，新结果返回后会自动替换。</small>}<div>{translateResult}</div></article> : <p>这里会显示你选中段落的中文翻译和关键术语解释。</p>}
            </div>
          </div>
          {translateResult && <div className="translation-actions popup-result-actions">
            <button onClick={() => void navigator.clipboard.writeText(translateResult).then(() => setTranslateStatus("译文已复制。")).catch(() => setTranslateError("复制失败，请手动选择译文。"))}>复制译文</button>
            <button disabled={showingPreviousTranslation} onClick={() => {
              if ((quote.trim() || note.trim()) && !window.confirm("替换当前未保存批注草稿？")) return;
              setQuote(translateResultSource); setNote("译文（AI 生成，需核对）：\n" + translateResult);
              setDraftPage(translationPage); setAnchor(null); setShowNotes(true); setDrawerMode("notes");
            }}>转为批注草稿</button>
          </div>}
        </TranslationPopup>}
      </div>
    </section>
  );
}
