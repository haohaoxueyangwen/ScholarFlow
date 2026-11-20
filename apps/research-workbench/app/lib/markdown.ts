import remarkGfm from "remark-gfm";

// Do not fetch model-supplied images or interpret model-supplied HTML.
export const markdownOptions = {
  remarkPlugins: [remarkGfm],
  skipHtml: true,
  disallowedElements: ["img", "iframe", "script", "style"],
};
