"use client";

import type { Item, PaperReaderData } from "@/app/lib/types";
import { text } from "@/app/lib/presentation";

function shortTitle(value: unknown, fallback: string) {
  const normalized = text(value, fallback);
  return normalized.length > 34 ? `${normalized.slice(0, 32)}…` : normalized;
}

export function CitationGraph({ title, reader }: { title: string; reader: PaperReaderData | null }) {
  const references = (reader?.document?.references ?? []).slice(0, 14);
  const warnings = (reader?.document?.warnings ?? []).map((warning) => text(warning));
  const usesAuthorYearFallback = warnings.includes("bibliography_unstructured:author_year_mentions_only");
  const citationCounts = new Map<string, number>();
  for (const citation of reader?.document?.citations ?? []) {
    for (const referenceId of Array.isArray(citation.reference_ids) ? citation.reference_ids : []) {
      const id = text(referenceId);
      citationCounts.set(id, (citationCounts.get(id) ?? 0) + 1);
    }
  }
  const width = 900;
  const height = 620;
  const center = { x: width / 2, y: height / 2 };
  const radius = Math.min(width, height) * .37;
  const nodes = references.map((reference, index) => {
    const angle = -Math.PI / 2 + (Math.PI * 2 * index) / Math.max(references.length, 1);
    return {
      reference,
      x: center.x + Math.cos(angle) * radius,
      y: center.y + Math.sin(angle) * radius,
    };
  });

  if (!reader?.available) {
    return <div className="citation-graph-empty"><span>↗</span><h3>引用图谱需要结构化 PDF</h3><p>先将 PDF 正式暂存并解析，或为 Zotero 论文生成解析计划。</p></div>;
  }
  if (!references.length) {
    return <div className="citation-graph-empty"><span>0</span><h3>没有抽取到参考文献</h3><p>这可能是扫描件、参考文献格式特殊，或解析器尚未识别该区域。</p></div>;
  }

  return <section className="citation-graph-view" aria-label="论文引用关系图">
    <header>
      <div><span>{usesAuthorYearFallback ? "CITATION MENTIONS" : "CITATION MAP"}</span><h3>当前论文引用了哪些工作</h3></div>
      <p>{usesAuthorYearFallback
        ? `参考文献区未被完整识别；当前仅显示前 ${references.length} 条正文中明确出现的作者—年份引用提及，不补造题名或 DOI。`
        : `显示前 ${references.length} 条结构化参考文献；连线粗细代表正文引用次数。`}</p>
    </header>
    <div className="citation-canvas">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${title} 的引用关系图`}>
        <defs><filter id="node-shadow"><feDropShadow dx="0" dy="5" stdDeviation="8" floodOpacity=".12" /></filter></defs>
        {nodes.map(({ reference, x, y }) => {
          const id = text(reference.reference_id);
          const count = citationCounts.get(id) ?? 0;
          return <line key={`edge-${id}`} x1={center.x} y1={center.y} x2={x} y2={y} className="citation-edge" strokeWidth={1.5 + Math.min(count, 5)} />;
        })}
        <g className="citation-center" transform={`translate(${center.x},${center.y})`} filter="url(#node-shadow)">
          <circle r="84" />
          <text textAnchor="middle" y="-8">当前论文</text>
          <text className="title" textAnchor="middle" y="18">{shortTitle(title, "Paper")}</text>
        </g>
        {nodes.map(({ reference, x, y }) => {
          const item = reference as Item;
          const id = text(item.reference_id);
          const count = citationCounts.get(id) ?? 0;
          const label = shortTitle(item.title || item.raw_text, id);
          return <g key={id} className="citation-node" transform={`translate(${x},${y})`}>
            <circle r="55" />
            <text textAnchor="middle" y="-10">{id}</text>
            <text className="title" textAnchor="middle" y="12">{label.slice(0, 22)}</text>
            <text className="count" textAnchor="middle" y="32">正文引用 {count} 次</text>
          </g>;
        })}
      </svg>
    </div>
    <div className="citation-reference-strip">{references.map((reference) => {
      const id = text(reference.reference_id);
      const doi = text(reference.doi, "");
      return <article key={id}><span>{id}</span><p>{text(reference.raw_text)}</p>{doi ? <a href={`https://doi.org/${doi}`} target="_blank" rel="noreferrer">DOI ↗</a> : <small>未解析 DOI</small>}</article>;
    })}</div>
  </section>;
}
