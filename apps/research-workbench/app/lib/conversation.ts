import type { AgentEvent, ConversationItem, Item } from "./types";

export function mergeMessages(current: ConversationItem[], incoming: ConversationItem[]): ConversationItem[] {
  const messages = new Map(current.map((item) => [item.id, item]));
  for (const item of incoming) messages.set(item.id, item);
  return [...messages.values()].sort((a, b) => (a.sequence ?? 0) - (b.sequence ?? 0));
}

// Compatibility projection for older API/fixtures. Raw delta whitespace is data.
export function conversationItems(events: AgentEvent[]): ConversationItem[] {
  const items: ConversationItem[] = [];
  let assistant: ConversationItem | undefined;
  const seen = new Set<string>();
  for (const event of events) {
    const identity = `${event.session_id ?? ""}:${event.sequence}`;
    if (seen.has(identity)) continue;
    seen.add(identity);
    const id = (kind: string, key: string | number) => `${event.session_id ? `${event.session_id}:` : ""}${kind}-${key}`;
    if (event.type === "workbench_user_message") {
      if (assistant) assistant.pending = false;
      assistant = undefined;
      items.push({ id: id("user", event.sequence), sequence: event.sequence, kind: "user", text: String(event.message ?? ""), detail: ((event.context_refs ?? []) as unknown[]).map(String).join(" · ") });
    } else if (event.type === "message_update") {
      const delta = (event.assistantMessageEvent ?? {}) as Item;
      if (delta.type !== "text_delta" || typeof delta.delta !== "string") continue;
      if (!assistant) { assistant = { id: id("assistant", event.sequence), sequence: event.sequence, kind: "assistant", text: "", pending: true }; items.push(assistant); }
      assistant.text += delta.delta;
    } else if (event.type === "message_end") {
      const message = (event.message ?? {}) as Item;
      if (message.role !== "assistant") continue;
      const complete = Array.isArray(message.content) ? message.content.filter((part) => part.type === "text").map((part) => part.text).join("\n") : typeof message.content === "string" ? message.content : "";
      if (assistant && complete) assistant.text = complete;
      else if (complete) items.push({ id: id("assistant", event.sequence), sequence: event.sequence, kind: "assistant", text: complete });
      if (assistant) assistant.pending = false;
      assistant = undefined;
    } else if (event.type === "tool_execution_start") {
      if (assistant) assistant.pending = false;
      assistant = undefined;
      items.push({ id: id("tool", String(event.toolCallId ?? event.sequence)), sequence: event.sequence, kind: "tool", text: String(event.toolName ?? "ScholarFlow tool"), detail: "调用开始", pending: true });
    } else if (event.type === "tool_execution_end") {
      const prior = items.findLast((item) => item.id === id("tool", String(event.toolCallId ?? event.sequence)));
      if (prior) { prior.pending = false; prior.detail = event.isError ? "调用失败" : "调用完成"; }
    } else if (["agent_settled", "workbench_prompt_completed", "workbench_prompt_failed"].includes(event.type)) {
      if (assistant) assistant.pending = false;
      assistant = undefined;
      if (event.type === "workbench_prompt_failed") items.push({ id: id("error", event.sequence), sequence: event.sequence, kind: "error", text: String(event.error ?? "Pi 会话失败") });
    }
  }
  return items;
}
