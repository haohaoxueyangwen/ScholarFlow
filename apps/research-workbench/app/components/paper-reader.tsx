"use client";

import { useEffect, useMemo, useState } from "react";
import { apiGet } from "@/app/lib/api";
import { text } from "@/app/lib/presentation";
import type { ConnectionState, Item, PaperReaderData } from "@/app/lib/types";

type ReaderView = "structure" | "figures" | "references";

function pageLabel(block: Item) {
  const start = text(block.page_start, "?");
  const end = text(block.page_end, start);
  return start === end ? `p. ${start}` : `pp. ${start}–${end}`;
}

function sectionKey(value: unknown) {
  return Array.isArray(value) ? value.map((item) => text(item)).join(" / ") : "";
}

export function PaperReader({
  paperId,
  connection,
  readerPath,
}: {
  paperId: string;
  connection: ConnectionState;
  readerPath?: string;
}) {
  const [remote, setRemote] = useState<{ paperId: string; data: PaperReaderData } | null>(null);
  const [errorPaperId, setErrorPaperId] = useState("");
  const [view, setView] = useState<ReaderView>("structure");
  const [activeSection, setActiveSection] = useState("");
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState(false);

  useEffect(() => {
    if (!paperId || connection !== "connected") return;
    let cancelled = false;
    const base = readerPath || `/papers/${encodeURIComponent(paperId)}`;
    void apiGet<PaperReaderData>(`${base}/reader?limit=500`)
      .then((data) => {
        if (!cancelled) {
          setRemote({ paperId, data });
          setErrorPaperId("");
        }
      })
      .catch(() => {
        if (!cancelled) setErrorPaperId(paperId);
      });
    return () => { cancelled = true; };
  }, [connection, paperId, readerPath]);

  const reader = remote?.paperId === paperId ? remote.data : null;
  const document = reader?.available ? reader.document : undefined;
  const citationsByBlock = useMemo(() => {
    const grouped = new Map<string, Item[]>();
    for (const citation of document?.citations ?? []) {
      const blockId = text(citation.block_id, "");
      if (blockId) grouped.set(blockId, [...(grouped.get(blockId) ?? []), citation]);
    }
    return grouped;
  }, [document?.citations]);

  async function loadMore() {
    if (!document?.has_more || loadingMore) return;
    setLoadingMore(true);
    setLoadMoreError(false);
    try {
      const offset = document.offset + document.blocks.length;
      const base = readerPath || `/papers/${encodeURIComponent(paperId)}`;
      const next = await apiGet<PaperReaderData>(`${base}/reader?offset=${offset}&limit=500`);
      if (next.available && next.document) {
        const nextDocument = next.document;
        setRemote((current) => current?.paperId === paperId && current.data.document
          ? {
              paperId,
              data: {
                ...next,
                document: {
                  ...nextDocument,
                  offset: current.data.document.offset,
                  blocks: [...current.data.document.blocks, ...nextDocument.blocks],
                },
              },
            }
          : { paperId, data: next });
      }
    } catch {
      setLoadMoreError(true);
    } finally {
      setLoadingMore(false);
    }
  }

  if (connection !== "connected") {
    return <section className="paper-reader-unavailable"><div><p className="eyebrow">STRUCTURED READER</p><h3>连接本地工作台后读取论文结构</h3></div><p>演示模式不会猜测 PDF 内容。启动本地 API 并同步受控附件后，章节与页码锚点会出现在这里。</p></section>;
  }
  if (errorPaperId === paperId) {
    return <section className="paper-reader-unavailable warning"><div><p className="eyebrow">STRUCTURED READER</p><h3>结构化快照暂时无法读取</h3></div><p>论文记录仍可使用；请检查本地知识库状态后重试。</p></section>;
  }
  if (!reader) {
    return <section className="paper-reader-loading" aria-live="polite">正在核对结构化 PDF 与证据锚点…</section>;
  }
  if (!reader.available || !document) {
    return (
      <section className="paper-reader-unavailable">
        <div><p className="eyebrow">STRUCTURED READER</p><h3>这篇论文还没有可读的结构化 PDF</h3></div>
        <p>{reader.reason === "structured_pdf_not_indexed" ? "已找到论文来源，但附件尚未完成结构化入库。" : "当前 Paper 尚未与已批准的 Zotero 附件或 PDF 来源建立映射。"}</p>
        <ol><li>在 Zotero 中确认该条目存在 PDF 附件</li><li>到“AI 知识库”生成同步计划并确认</li><li>处理完成后返回此处查看章节、图表和引用</li></ol>
      </section>
    );
  }

  const parser = document.parser ?? {};
  const blocks = document.blocks ?? [];
  const availableSections = new Set(document.toc.map((section) => sectionKey(section.section_path)));
  const selectedSection = availableSections.has(activeSection) ? activeSection : "";
  const structureBlocks = blocks.filter((block) => {
    if (text(block.kind) === "reference_entry") return false;
    return !selectedSection || sectionKey(block.section_path) === selectedSection;
  });
  const figureBlocks = blocks.filter((block) => /figure|table|caption/i.test(text(block.kind)));
  const tabs: Array<[ReaderView, string, number]> = [
    ["structure", "正文结构", document.block_count],
    ["figures", "图表", figureBlocks.length],
    ["references", "参考文献", document.references.length],
  ];

  return (
    <section className="paper-reader">
      <header className="paper-reader-header">
        <div><p className="eyebrow">STRUCTURED PAPER · TRACEABLE</p><h3>{text(document.title, "科学论文结构")}</h3><p>正文来自受控解析快照；每段保留页码、章节和 block id，不暴露本机文件路径。</p></div>
        <div className="paper-reader-stats">
          <span><strong>{document.page_count}</strong>页</span>
          <span><strong>{document.block_count}</strong>区块</span>
          <span><strong>{document.references.length}</strong>参考文献</span>
        </div>
      </header>
      <div className="paper-reader-provenance"><span>解析器 <strong>{text(parser.name)} {text(parser.version)}</strong></span><span>来源 <code>{text(reader.source?.source_ref)}</code></span>{document.warnings.length ? <span className="warning">{document.warnings.length} 条解析提示</span> : <span>结构快照已处理</span>}</div>
      <nav className="paper-reader-tabs" aria-label="论文阅读视图">{tabs.map(([id, label, count]) => <button key={id} className={view === id ? "active" : ""} aria-current={view === id ? "page" : undefined} onClick={() => setView(id)}>{label}<span>{count}</span></button>)}</nav>

      {view === "structure" && <div className="paper-reader-layout">
        <nav className="paper-toc" aria-label="论文目录">
          <button className={!selectedSection ? "active" : ""} onClick={() => setActiveSection("")}><strong>全文结构</strong><small>{blocks.length} 个区块</small></button>
          {document.toc.map((section) => { const key = sectionKey(section.section_path); return <button key={key} className={selectedSection === key ? "active" : ""} onClick={() => setActiveSection(key)}><strong>{text(section.title)}</strong><small>p. {text(section.page_start, "?")} · {text(section.block_count)} 段</small></button>; })}
        </nav>
        <div className="paper-blocks">
          {structureBlocks.length ? structureBlocks.map((block) => {
            const kind = text(block.kind, "paragraph");
            const citations = citationsByBlock.get(text(block.block_id)) ?? [];
            if (kind === "heading") return <h4 className="paper-block-heading" key={text(block.block_id)}><span>{pageLabel(block)}</span>{text(block.text)}</h4>;
            return <article className={`paper-block ${/caption|figure|table/i.test(kind) ? "caption" : ""}`} key={text(block.block_id)}><div className="paper-block-anchor"><span>{pageLabel(block)}</span><code>{text(block.block_id)}</code><small>{kind}</small></div><p>{text(block.text)}</p>{citations.length ? <div className="paper-citations">{citations.flatMap((citation) => Array.isArray(citation.reference_ids) ? citation.reference_ids : []).map((reference) => <span key={`${text(block.block_id)}-${text(reference)}`}>{text(reference)}</span>)}</div> : null}</article>;
          }) : <div className="paper-reader-empty">所选章节没有可显示的正文区块。</div>}
          {document.has_more ? <button className="paper-reader-more" disabled={loadingMore} onClick={() => void loadMore()}>{loadingMore ? "正在读取后续区块…" : `继续读取（当前 ${document.blocks.length} / ${document.block_count}）`}</button> : null}
          {loadMoreError ? <p className="paper-reader-more-error" role="alert">后续区块读取失败，当前已加载内容仍可继续使用。</p> : null}
        </div>
      </div>}

      {view === "figures" && <div className="paper-reader-collection">{figureBlocks.length ? figureBlocks.map((block) => <article key={text(block.block_id)}><div><span>{pageLabel(block)}</span><code>{text(block.block_id)}</code></div><strong>{text(block.text)}</strong><small>{sectionKey(block.section_path) || "未识别章节"}</small></article>) : <div className="paper-reader-empty">当前解析快照没有识别出图题或表题。</div>}</div>}

      {view === "references" && <div className="paper-reference-list">{document.references.length ? document.references.map((reference) => <article key={text(reference.reference_id)}><span>{text(reference.reference_id)}</span><div><p>{text(reference.raw_text)}</p><small>{text(reference.doi, "无 DOI")} · p. {text(reference.page, "?")}</small></div></article>) : <div className="paper-reader-empty">当前解析快照没有结构化参考文献。</div>}</div>}
    </section>
  );
}
