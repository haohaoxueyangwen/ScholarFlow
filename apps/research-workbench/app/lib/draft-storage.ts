export const PROSE_DRAFT_KEY = "sf-manuscript-prose-v1";

export function readProseDrafts(raw: string | null): Record<string, string> {
  const value: unknown = JSON.parse(raw || "{}");
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid draft storage");
  return Object.fromEntries(Object.entries(value).filter(([, text]) => typeof text === "string"));
}

export function saveProseDraft(storage: Pick<Storage, "getItem" | "setItem">, key: string, value: string, expected: string) {
  const drafts = readProseDrafts(storage.getItem(PROSE_DRAFT_KEY));
  const current = drafts[key] ?? "";
  if (current !== expected && current !== value) return { saved: false, current };
  storage.setItem(PROSE_DRAFT_KEY, JSON.stringify({ ...drafts, [key]: value }));
  return { saved: true, current: value };
}
