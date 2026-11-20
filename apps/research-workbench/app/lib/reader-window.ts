/** Keep PDF.js canvas and text layers near the visible page only. */
export function readerPageWindow(currentPage: number, pageCount: number, radius = 2): number[] {
  const total = Math.max(0, Math.floor(pageCount));
  if (!total) return [];
  const current = Math.max(1, Math.min(total, Math.floor(currentPage) || 1));
  const overscan = Math.max(0, Math.min(4, Math.floor(radius)));
  const first = Math.max(1, current - overscan);
  const last = Math.min(total, current + overscan);
  return Array.from({ length: last - first + 1 }, (_, index) => first + index);
}
