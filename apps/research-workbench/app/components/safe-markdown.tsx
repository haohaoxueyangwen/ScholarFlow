"use client";

import Markdown from "react-markdown";
import { markdownOptions } from "@/app/lib/markdown";

export function SafeMarkdown({ content }: { content: string }) {
  return <div className="safe-markdown"><Markdown {...markdownOptions} components={{ a: ({ href, children }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> }}>{content}</Markdown></div>;
}
