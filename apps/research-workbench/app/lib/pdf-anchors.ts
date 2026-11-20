import type { PdfTextAnchor } from "./types";

export function normalizeRects(rects: Array<{ left: number; top: number; width: number; height: number }>, page: { left: number; top: number; width: number; height: number }) {
  if (page.width <= 0 || page.height <= 0) return [];
  const seen = new Set<string>();
  return rects.filter((rect) => rect.width > 0 && rect.height > 0).map((rect) => ({
    x: Math.max(0, (rect.left - page.left) / page.width), y: Math.max(0, (rect.top - page.top) / page.height),
    width: Math.min(1, rect.width / page.width), height: Math.min(1, rect.height / page.height),
  })).filter((rect) => {
    const key = [rect.x, rect.y, rect.width, rect.height].map((value) => value.toFixed(5)).join(":");
    if (seen.has(key) || rect.x + rect.width > 1.002 || rect.y + rect.height > 1.002) return false;
    seen.add(key); return true;
  }).slice(0, 300);
}

export function capturePdfAnchor(viewport: HTMLElement, selection: Selection | null, fingerprint: string): { quote: string; anchor: PdfTextAnchor } | null {
  if (!selection?.rangeCount || !fingerprint) return null;
  const range = selection.getRangeAt(0);
  const element = range.startContainer.nodeType === Node.ELEMENT_NODE ? range.startContainer as Element : range.startContainer.parentElement;
  const layer = element?.closest(".react-pdf__Page__textContent");
  const page = layer?.closest<HTMLElement>(".react-pdf__Page");
  if (!layer || !page || !viewport.contains(page) || !layer.contains(range.startContainer) || !layer.contains(range.endContainer)) return null;
  const quote = range.toString();
  if (!quote.trim() || quote.length > 5000) return null;
  const point = (node: Node, offset: number) => {
    const element = node.nodeType === Node.ELEMENT_NODE ? node as Element : node.parentElement;
    const item = element?.closest<HTMLElement>("[data-sf-text-item]");
    if (!item) return null;
    const prefix = document.createRange(); prefix.selectNodeContents(item); prefix.setEnd(node, offset);
    return { item_index: Number(item.dataset.sfTextItem), offset: prefix.toString().length };
  };
  const start = point(range.startContainer, range.startOffset), end = point(range.endContainer, range.endOffset);
  const rects = normalizeRects(Array.from(range.getClientRects()), page.getBoundingClientRect());
  if (!start || !end || !rects.length) return null;
  return { quote, anchor: { kind: "pdf_text_range_v1", document_fingerprint: fingerprint, page: Number(page.dataset.pageNumber), start, end, rects } };
}
