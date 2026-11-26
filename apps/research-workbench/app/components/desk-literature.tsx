"use client";

import { useEffect, useRef, useState } from "react";
import { researchApiBase, researchRequest } from "@/app/lib/research-desk";

type Paper = {
  candidate_id: string; title: string; authors?: string[]; year?: number | null; venue?: string;
  primary_url?: string; doi?: string; provider: string; pdf_available: boolean;
};
type Results = { items: Paper[]; page: number; has_more: boolean; provider: string };
type Props = {
  stage: string; project: string; active: boolean; busy: boolean; opening: boolean;
  pages: string; setPages: (pages: string) => void; provider: string; setProvider: (provider: string) => void;
  onOpen: (candidate: string) => Promise<boolean>;
};

function landingUrl(value?: string) {
  try {
    const url = value ? new URL(value) : null;
    return url && url.protocol === "https:" && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

const providerName = (name: string) => name === "openalex" ? "OpenAlex" : name === "crossref" ? "Crossref" : "文献检索";

export function DeskLiterature({stage, project, active, busy, opening, pages, setPages, provider, setProvider, onOpen}: Props) {
  const [query, setQuery] = useState("");
  const [searched, setSearched] = useState("");
  const [results, setResults] = useState<Results | null>(null);
  const [selected, setSelected] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  const searchable = query.trim().length >= 2 && !loading && !busy;

  async function search(value: string, page: number) {
    if (busy || loading || value.trim().length < 2) return;
    const controller = new AbortController();
    request.current?.abort();
    request.current = controller;
    setSelected(""); setError(""); setLoading(true);
    try {
      const response = await researchRequest(researchApiBase(), "/literature/search", {
        method: "POST", signal: controller.signal, headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ stage_id: stage, query: value.trim(), page }),
      });
      const data = await response.json();
      if (!response.ok || !Array.isArray(data.items)) throw new Error(data?.error?.message || "未能读取文献服务。");
      if (!controller.signal.aborted) { setSearched(value.trim()); setResults(data); }
    } catch (reason) {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "搜索未完成，请重试。");
    } finally { if (!controller.signal.aborted) setLoading(false); }
  }

  return <div className="desk-literature" hidden={!active}>
    <header className="desk-lit-heading"><span className="desk-eyebrow">项目资料 / {project}</span><h1>找论文</h1><p>搜索题名、关键词或 DOI。找到开放 PDF 后，可直接加入当前项目并开始精读。</p></header>
    <form className="desk-lit-search" onSubmit={event => { event.preventDefault(); void search(query, 1); }}>
      <label htmlFor="desk-lit-query">文献搜索</label><div><input id="desk-lit-query" value={query} onChange={event => setQuery(event.target.value)} maxLength={400} placeholder="例如：X-ray wavefront sensing" /><button type="submit" className="desk-primary" disabled={!searchable}>{loading ? "搜索中…" : "搜索"}</button></div>
      <small>查询词会发给学术检索服务；不会上传项目笔记或本地文档。</small>
    </form>
    {error && <p className="reading-error" role="alert">{error}</p>}
    {loading && <p className="desk-muted" role="status">正在查询文献服务…</p>}
    {results && <>
      <div className="desk-lit-summary"><strong>“{searched}”</strong><span>{providerName(results.provider)} · 第 {results.page} 页 · {results.items.length} 条</span></div>
      {results.items.length === 0 && <p className="desk-muted">这页没有结果。可换一组关键词或使用 DOI。</p>}
      <ol className="desk-lit-results">{results.items.map(paper => {
        const source = landingUrl(paper.primary_url);
        return <li key={paper.candidate_id}>
          <div className="desk-lit-item">
            <div><span className={paper.pdf_available ? "desk-lit-oa" : "desk-lit-meta"}>{paper.pdf_available ? "开放 PDF" : "仅题录"}</span><h2>{paper.title}</h2>
              <p>{[paper.authors?.slice(0, 4).join("、"), paper.venue, paper.year].filter(Boolean).join(" · ")}</p>
              {paper.doi && <small>DOI {paper.doi}</small>}
            </div>
            <div className="desk-lit-actions">
              {paper.pdf_available && <button type="button" aria-expanded={selected === paper.candidate_id} disabled={busy || loading} onClick={() => setSelected(selected === paper.candidate_id ? "" : paper.candidate_id)}>下载并加入</button>}
              {source && <a href={source} target="_blank" rel="noreferrer">打开题录 ↗</a>}
            </div>
          </div>
          {selected === paper.candidate_id && <section className="desk-lit-confirm" aria-label="加入项目并精读">
            <h3>加入「{project}」并精读</h3>
            <div className="desk-options"><label>Pi 可读页<input value={pages} maxLength={160} onChange={event => setPages(event.target.value)} disabled={busy} placeholder="例如 1 或 1-3" /></label><label>阅读模型<select value={provider} onChange={event => setProvider(event.target.value)} disabled={busy}><option value="scholarflow-local">本地 Qwen</option><option value="deepseek">DeepSeek（外部 API）</option></select></label></div>
            <p>本人可读整份 PDF，Pi 只读授权页。加入项目资料不等于写入 AI 知识库。</p>
            <button type="button" className="desk-primary" disabled={busy || !pages.trim()} onClick={() => { void onOpen(paper.candidate_id).then(ok => { if (ok) setSelected(""); }); }}>{opening ? "正在下载和解析…" : "确认加入并打开"}</button>
          </section>}
        </li>;
      })}</ol>
      {(results.page > 1 || results.has_more) && <div className="desk-actions"><button type="button" disabled={loading || busy || results.page === 1} onClick={() => void search(searched, results.page - 1)}>上一页结果</button><button type="button" disabled={loading || busy || !results.has_more} onClick={() => void search(searched, results.page + 1)}>下一页结果</button></div>}
    </>}
  </div>;
}
