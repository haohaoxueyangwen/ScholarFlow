"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import "react-pdf/dist/Page/TextLayer.css";
import { normalizeRects } from "@/app/lib/pdf-anchors";
import { fitPaperWidth, researchApiBase, researchRequest } from "@/app/lib/research-desk";

pdfjs.GlobalWorkerOptions.workerSrc = new URL("pdfjs-dist/build/pdf.worker.min.mjs", import.meta.url).toString();

type Rect = { x: number; y: number; width: number; height: number };
type Selection = { text: string; page: number; x: number; y: number; above: boolean; rects: Rect[]; markId: string };
type Mark = { mark_id: string; page: number; quote: string; note: string; rects: Rect[] };
type Translation = { key: string; state: "loading" | "completed" | "failed"; text: string };
type Props = {
  fileUrl: string; filename: string; page: number; pageCount: number; authorized: boolean;
  workspaceId: string; stageId: string; provider: string;
  onPage: (page: number) => void;
  onDraft: (text: string, page: number) => string | null;
};

// A read-only document surface; annotations and publication belong to domain services.
const pdfOptions = { isEvalSupported: false, disableFontFace: false, enableXfa: false };

async function readerJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await researchRequest(researchApiBase(), path, init);
  const payload = await response.json().catch(() => null);
  if (!response.ok || !payload) throw new Error(payload?.error?.message || `阅读操作未完成（${response.status}）`);
  return payload as T;
}

const post = (value: unknown, signal?: AbortSignal): RequestInit => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value), signal });

export function DocumentReader({ fileUrl, filename, page, pageCount, authorized, workspaceId, stageId, provider, onPage, onDraft }: Props) {
  const surface = useRef<HTMLDivElement>(null);
  const viewport = useRef<HTMLDivElement>(null);
  const sheet = useRef<HTMLDivElement>(null);
  const [available, setAvailable] = useState({ width: 540, height: 640 });
  const [aspect, setAspect] = useState(0.707);
  const [fit, setFit] = useState<"width" | "page">("width");
  const [zoom, setZoom] = useState(1);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [notice, setNotice] = useState("");
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [marks, setMarks] = useState<Mark[]>([]);
  const [showMarks, setShowMarks] = useState(false);
  const [markError, setMarkError] = useState("");
  const [markReload, setMarkReload] = useState(0);
  const [editingNote, setEditingNote] = useState(false);
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [translation, setTranslation] = useState<Translation | null>(null);
  const [allowExternal, setAllowExternal] = useState(false);
  const translateAbort = useRef<AbortController | null>(null);
  const file = useMemo(() => ({ url: fileUrl, withCredentials: true }), [fileUrl]);
  const excerpt = selection?.page === page ? selection : null;
  const popupAbove = Boolean(excerpt?.above && Math.min(excerpt.y, available.height) >= Math.min(350, available.height * .65) + 20);
  const displayedTranslation = translation?.key === excerpt?.markId ? translation : null;
  const marksUrl = `/workspaces/${encodeURIComponent(workspaceId)}/marks`;
  const paperWidth = fitPaperWidth(available.width, available.height, aspect, fit, zoom);
  const paperHeight = paperWidth / aspect;
  // Scale the raster, not the CSS paper, to bound memory on very large windows.
  const dpr = Math.min(typeof window === "undefined" ? 1 : window.devicePixelRatio, 1.5, 4096 / Math.max(paperWidth, paperHeight), Math.sqrt(8_000_000 / (paperWidth * paperHeight)));

  useEffect(() => {
    if (!viewport.current) return;
    const observer = new ResizeObserver(([entry]) => {
      const width = Math.floor(entry.contentRect.width), height = Math.floor(entry.contentRect.height);
      if (width > 0 && height > 0) setAvailable(previous => previous.width === width && previous.height === height ? previous : { width, height });
    });
    observer.observe(viewport.current);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void readerJson<{ items: Mark[] }>(`${marksUrl}?stage_id=${encodeURIComponent(stageId)}`, { signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) { setMarks(current => Array.from(new Map([...value.items, ...current].map(item => [item.mark_id, item])).values())); setMarkError(""); } })
      .catch((error: Error) => { if (!controller.signal.aborted) setMarkError(error.message); });
    return () => controller.abort();
  }, [marksUrl, stageId, markReload]);

  useEffect(() => () => translateAbort.current?.abort(), [excerpt?.markId]);

  function moveTo(target: number) {
    if (!Number.isInteger(target) || target < 1 || target > pageCount) { setNotice(`请输入 1 到 ${pageCount} 之间的页码。`); return; }
    setSelection(null); setNotice("");
    if (viewport.current) viewport.current.scrollTop = 0;
    onPage(target);
  }

  function captureSelection() {
    if (saving || (editingNote && note.trim())) return;
    const active = window.getSelection();
    if (!active?.rangeCount || active.isCollapsed || !active.anchorNode || !active.focusNode || !sheet.current?.contains(active.anchorNode) || !sheet.current.contains(active.focusNode) || !surface.current) {
      setSelection(null); return;
    }
    const range = active.getRangeAt(0);
    const layer = sheet.current.querySelector(".react-pdf__Page__textContent");
    const pageElement = sheet.current.querySelector(".react-pdf__Page");
    if (!layer?.contains(range.startContainer) || !layer.contains(range.endContainer) || !pageElement) { setSelection(null); return; }
    const box = range.getBoundingClientRect();
    const frame = surface.current.getBoundingClientRect();
    const text = active.toString().replace(/\s+/g, " ").trim();
    if (!text || !box.height) { setSelection(null); return; }
    const cardWidth = Math.min(330, frame.width - 20);
    const x = Math.max(10, Math.min(box.left - frame.left, frame.width - cardWidth - 10));
    const above = box.bottom + 235 > frame.bottom && box.top - frame.top > 235;
    const y = above ? box.top - frame.top - 8 : Math.max(12, box.bottom - frame.top + 8);
    setSelection({ text, page, x, y, above, rects: normalizeRects(Array.from(range.getClientRects()), pageElement.getBoundingClientRect()).slice(0, 80), markId: `mark-${crypto.randomUUID().replaceAll("-", "")}` });
    setNote(""); setEditingNote(false); setAllowExternal(false);
    setNotice("");
  }

  function draft() {
    if (!excerpt || !authorized || excerpt.text.length > 1500) return;
    const error = onDraft(excerpt.text, excerpt.page);
    if (error) { setNotice(error); return; }
    setSelection(null); window.getSelection()?.removeAllRanges();
    setNotice("摘录已放入 Pi 草稿，尚未发送。");
  }

  async function translate() {
    if (!excerpt || !authorized || excerpt.text.length > 1500 || displayedTranslation?.state === "loading" || (provider === "deepseek" && !allowExternal)) return;
    const controller = new AbortController();
    translateAbort.current?.abort(); translateAbort.current = controller;
    const key = excerpt.markId;
    setTranslation({ key, state: "loading", text: "" });
    try {
      const value = await readerJson<{ translation: string }>(`/workspaces/${encodeURIComponent(workspaceId)}/translate`, post({ stage_id: stageId, page: excerpt.page, excerpt: excerpt.text, external_consent: allowExternal }, controller.signal));
      if (!controller.signal.aborted) setTranslation({ key, state: "completed", text: value.translation });
    } catch (error) {
      if (!controller.signal.aborted) setTranslation({ key, state: "failed", text: error instanceof Error ? error.message : "翻译未完成。" });
    }
  }

  async function saveMark(noteText: string) {
    if (!excerpt || saving || !excerpt.rects.length || excerpt.text.length > 1500) return;
    setSaving(true);
    try {
      const value = await readerJson<Mark>(marksUrl, post({ stage_id: stageId, mark_id: excerpt.markId, page: excerpt.page, quote: excerpt.text, note: noteText, rects: excerpt.rects }));
      setMarks(items => [...items.filter(item => item.mark_id !== value.mark_id), value]);
      setSelection(current => current?.markId === excerpt.markId ? null : current);
      setNotice(noteText.trim() ? "批注和高亮已保存。" : "高亮已保存。");
      window.getSelection()?.removeAllRanges();
    } catch (error) { setNotice(error instanceof Error ? error.message : "保存标注失败，可以重试。"); }
    finally { setSaving(false); }
  }

  async function copy() {
    if (!excerpt) return;
    try {
      await navigator.clipboard.writeText(`【${filename} · 第 ${excerpt.page} 页】\n${excerpt.text}`);
      setNotice("已复制带页码的摘录。");
    } catch { setNotice("浏览器未允许访问剪贴板，可使用系统复制快捷键。"); }
  }

  return <div className="document-reader" onKeyDown={(event) => { if (event.key === "Escape") { setSelection(null); window.getSelection()?.removeAllRanges(); } }}>
    <div className="document-controls" role="group" aria-label="论文阅读工具栏">
      <div className="document-control-group">
        <button type="button" title="上一页" aria-label="上一页" disabled={page <= 1} onClick={() => moveTo(page - 1)}>‹</button>
        <form onSubmit={(event) => { event.preventDefault(); moveTo(Number(new FormData(event.currentTarget).get("page"))); }}>
          <input key={page} name="page" inputMode="numeric" defaultValue={page} aria-label="跳至页码" title="输入页码后按 Enter" maxLength={8} />
        </form>
        <span className="document-page-total" aria-label={`共 ${pageCount} 页`}>/ {pageCount}</span>
        <button type="button" title="下一页" aria-label="下一页" disabled={page >= pageCount} onClick={() => moveTo(page + 1)}>›</button>
      </div>
      <div className="document-control-group" aria-label="缩放">
        <button type="button" title="缩小" aria-label="缩小" disabled={zoom <= 0.75} onClick={() => { setZoom(zoom - .25); setSelection(null); }}>−</button>
        <output className="document-zoom" aria-label="相对缩放比例">{Math.round(zoom * 100)}%</output>
        <button type="button" title="放大" aria-label="放大" disabled={zoom >= 1.75} onClick={() => { setZoom(zoom + .25); setSelection(null); }}>＋</button>
      </div>
      <div className="document-control-group" aria-label="适配画布">
        <button type="button" aria-pressed={fit === "width" && zoom === 1} title="随窗口宽度缩放" onClick={() => { setFit("width"); setZoom(1); setSelection(null); }}>适合宽度</button>
        <button type="button" aria-pressed={fit === "page" && zoom === 1} title="随窗口宽度和高度缩放" onClick={() => { setFit("page"); setZoom(1); setSelection(null); }}>适合整页</button>
      </div>
      <button className="document-mark-toggle" type="button" aria-expanded={showMarks} aria-controls="document-marks" onClick={() => setShowMarks(!showMarks)}>标注{marks.length ? ` ${marks.length}` : ""}{markError ? " · 读取失败" : ""}</button>
    </div>
    <div className="document-surface" ref={surface}>
      <div className="document-viewport" ref={viewport} onScroll={() => { if (!editingNote && !displayedTranslation) setSelection(null); }} aria-label="PDF 正文" tabIndex={0}>
        {failed ? <div className="document-fallback" role="alert"><strong>阅读画布暂时无法显示</strong><p>可以重试，或者用上方「打开原件」继续阅读。</p><button type="button" onClick={() => { setFailed(false); setAttempt(attempt + 1); }}>重新载入</button></div> :
          <div className="document-sheet" ref={sheet} onPointerUp={captureSelection} onKeyUp={captureSelection} style={{ width: paperWidth }}>
            <Document key={attempt} file={file} options={pdfOptions} loading={<div className="document-loading" role="status">正在渲染论文…</div>} onLoadError={() => setFailed(true)} onSourceError={() => setFailed(true)}>
              <Page pageNumber={page} width={paperWidth} devicePixelRatio={dpr} renderAnnotationLayer={false} onLoadSuccess={(pdfPage) => { const bounds = pdfPage.getViewport({ scale: 1 }); if (bounds.width > 0 && bounds.height > 0) setAspect(bounds.width / bounds.height); }} onRenderError={() => setFailed(true)} onLoadError={() => setFailed(true)} loading={<div className="document-loading" role="status">正在渲染第 {page} 页…</div>} />
            </Document>
            <div className="document-mark-overlay" aria-hidden="true">{marks.filter(mark => mark.page === page).flatMap(mark => mark.rects.map((rect, index) => <span key={`${mark.mark_id}-${index}`} style={{ left: `${rect.x * 100}%`, top: `${rect.y * 100}%`, width: `${rect.width * 100}%`, height: `${rect.height * 100}%` }} />))}</div>
          </div>}
      </div>
      {showMarks && <aside className="document-marks" id="document-marks" aria-label="此论文的标注">
        <div className="document-selection-head"><strong>此项目 · 论文标注</strong><button type="button" aria-label="关闭标注" onClick={() => setShowMarks(false)}>×</button></div>
        {markError && <div role="alert"><p>{markError}</p><button type="button" onClick={() => setMarkReload(value => value + 1)}>重试读取</button></div>}
        {!marks.length && !markError && <p>划选原文，即可保存高亮或批注。</p>}
        {marks.map(mark => <article key={mark.mark_id}><button type="button" onClick={() => { moveTo(mark.page); setShowMarks(false); }}>第 {mark.page} 页</button><p>{mark.quote}</p>{mark.note && <p className="document-user-note">{mark.note}</p>}</article>)}
      </aside>}
      {excerpt && <div className="document-selection" role="group" aria-label="选中文字操作" data-above={popupAbove} style={{ left: `clamp(10px, ${excerpt.x}px, max(10px, 100% - 340px))`, top: popupAbove ? `clamp(8px, ${excerpt.y}px, 100% - 8px)` : `clamp(8px, ${excerpt.y}px, max(8px, 100% - min(350px, 50dvh)))` }}>
        <div className="document-selection-head"><strong>第 {excerpt.page} 页 · 选中原文</strong><button type="button" aria-label="关闭选中文字操作" onClick={() => setSelection(null)}>×</button></div>
        <p>{excerpt.text.slice(0, 160)}{excerpt.text.length > 160 ? "…" : ""}</p>
        {!authorized ? <small>此页未授权给 Pi，仅供本人阅读。</small> : excerpt.text.length > 1500 ? <small>请缩短选区至 1500 字以内，再放入 Pi 草稿。</small> : null}
        <div className="desk-actions">
          {authorized && excerpt.text.length <= 1500 && <><button type="button" className="desk-primary" disabled={displayedTranslation?.state === "loading" || (provider === "deepseek" && !allowExternal)} onClick={() => void translate()}>{displayedTranslation?.state === "failed" ? "重试翻译" : "翻译"}</button><button type="button" onClick={draft}>送到 Pi 草稿</button></>}
          <button type="button" disabled={saving || !excerpt.rects.length || excerpt.text.length > 1500} onClick={() => void saveMark("")}>高亮</button>
          <button type="button" disabled={saving || !excerpt.rects.length || excerpt.text.length > 1500} onClick={() => setEditingNote(!editingNote)}>批注</button>
          <button className="desk-quiet" type="button" onClick={() => void copy()}>复制</button>
        </div>
        {authorized && provider === "deepseek" && excerpt.text.length <= 1500 && <label className="reading-consent"><input type="checkbox" checked={allowExternal} onChange={event => setAllowExternal(event.target.checked)} />仅将这段原文发送至 DeepSeek。</label>}
        {displayedTranslation && <div className="document-translation" role={displayedTranslation.state === "failed" ? "alert" : "status"}>
          <strong>译文</strong><div>{displayedTranslation.state === "loading" ? "正在翻译…" : displayedTranslation.text}</div>
        </div>}
        {editingNote && <label className="document-note">我的批注<textarea aria-label="批注内容" maxLength={2000} rows={3} value={note} onChange={event => setNote(event.target.value)} /><button type="button" className="desk-primary" disabled={saving || !note.trim()} onClick={() => void saveMark(note)}>{saving ? "保存中…" : "保存批注"}</button></label>}
      </div>}
    </div>
    <footer className="document-status"><span title={filename}>第 {page} 页 · {authorized ? "Pi 已获读取权限" : "不在 Pi 授权范围"}</span><span role="status">{notice || "划选正文可操作摘录"}</span></footer>
  </div>;
}
