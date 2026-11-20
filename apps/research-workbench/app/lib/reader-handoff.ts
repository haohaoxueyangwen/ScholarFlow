export const READER_HANDOFF_STORAGE_KEY = "scholarflow:pending-reader-handoffs:v1";
export const READER_HANDOFF_CHANNEL = "scholarflow-reader-handoff-v1";

export type ReaderHandoff = {
  schema: 1;
  id: string;
  createdAt: number;
  title: string;
  sourceRef: string;
  page: number;
  prompt: string;
  projectIds?: string[];
  sessionId?: string;
};

export function buildReaderHandoff(input: { title: string; sourceRef: string; page: number; quote: string; note: string; projectIds?: string[]; sessionId?: string }): ReaderHandoff {
  const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), (value) => value.toString(16).padStart(2, "0")).join("");
  const title = String(input.title || "PDF").slice(0, 500);
  const sourceRef = String(input.sourceRef || "").slice(0, 200);
  const page = Math.max(1, Math.floor(Number(input.page) || 1));
  const quote = String(input.quote || "").slice(0, 5000);
  const note = String(input.note || "").slice(0, 5000);
  return {
    schema: 1, id, createdAt: Date.now(), title, sourceRef, page,
    projectIds: (input.projectIds ?? []).filter((id) => typeof id === "string" && id.length > 0),
    ...(input.sessionId ? { sessionId: input.sessionId } : {}),
    prompt: [
      `请根据我在《${title}》第 ${page} 页选择的片段协助阅读。`,
      `文档引用：${sourceRef}`,
      `原文选段：\n${quote || "（未提供）"}`,
      `我的个人批注：\n${note || "（无）"}`,
      "请只解释或翻译这段内容，指出无法仅凭选段判断的地方，并把个人批注视为我的笔记；不要把它写成论文已经证实的结论。",
    ].join("\n\n"),
  };
}

export function storeReaderHandoff(handoff: ReaderHandoff, storage: Pick<Storage, "getItem" | "setItem">): void {
  let pending: ReaderHandoff[] = [];
  try {
    const saved: unknown = JSON.parse(storage.getItem(READER_HANDOFF_STORAGE_KEY) || "[]");
    if (Array.isArray(saved)) pending = saved.filter((item): item is ReaderHandoff => Boolean(item && item.schema === 1 && typeof item.id === "string" && typeof item.prompt === "string" && Date.now() - Number(item.createdAt) < 86_400_000));
  } catch { /* discard malformed local handoff metadata */ }
  storage.setItem(READER_HANDOFF_STORAGE_KEY, JSON.stringify([...pending, handoff].slice(-10)));
}

export function takeReaderHandoffs(raw: string | null): { items: ReaderHandoff[]; malformed: boolean } {
  if (!raw) return { items: [], malformed: false };
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return { items: [], malformed: true };
    const items = parsed.filter((item): item is ReaderHandoff => Boolean(item && item.schema === 1 && typeof item.id === "string" && typeof item.prompt === "string" && Date.now() - Number(item.createdAt) < 86_400_000));
    return { items, malformed: false };
  } catch { return { items: [], malformed: true }; }
}
