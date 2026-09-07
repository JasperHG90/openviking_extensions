/**
 * Rendering a file's markdown for the reading pane.
 *
 * Everything here comes out of OpenViking, which means it was written by an
 * agent or uploaded by somebody — not trusted input. So the HTML `marked`
 * produces is sanitized before it reaches the DOM, rather than trusting the
 * renderer to have no escape hatches.
 */

import DOMPurify from "dompurify";
import { marked } from "marked";

marked.setOptions({
  // Newlines inside a paragraph become <br>, which is what people writing
  // notes expect even though strict markdown disagrees.
  breaks: true,
  gfm: true,
});

/**
 * Render markdown to sanitized HTML.
 *
 * @param source - The file's text.
 * @returns HTML safe to insert, with scripts, event handlers and non-http
 *   schemes removed.
 */
export function renderMarkdown(source: string): string {
  const html = marked.parse(source, { async: false });
  return DOMPurify.sanitize(html, {
    ALLOWED_URI_REGEXP: /^(?:https?:|mailto:|viking:|#|\/)/i,
    ADD_ATTR: ["target", "rel"],
  });
}

/** Whether a file is worth rendering as markdown rather than as plain text. */
export function isMarkdown(kind: string): boolean {
  return kind === "MD" || kind === "MARKDOWN";
}
