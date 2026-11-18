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
  const { yaml, body } = splitFrontMatter(source);
  const folded = yaml === null ? "" : frontMatterBlock(yaml);
  const html = folded + marked.parse(body, { async: false });
  return DOMPurify.sanitize(html, {
    ALLOWED_URI_REGEXP: /^(?:https?:|mailto:|viking:|#|\/)/i,
    ADD_ATTR: ["target", "rel"],
  });
}

// `marked` does not know YAML front matter: it drew the opening `---` as a rule
// and ran the YAML into one paragraph above the page.
// A block may be empty, but may not open with a blank line: that is a rule
// at the top of the page, not YAML.
const FRONT_MATTER =
  /^\uFEFF?---[ \t]*\r?\n(?:(?![ \t]*\r?\n)([\s\S]*?)\r?\n)?---[ \t]*(?:\r?\n|$)/;

function splitFrontMatter(source: string): { yaml: string | null; body: string } {
  const match = FRONT_MATTER.exec(source);
  if (!match) return { yaml: null, body: source };
  return {
    yaml: (match[1] ?? "").replace(/\r\n/g, "\n"),
    body: source.slice(match[0].length),
  };
}

/** The YAML as escaped text, folded shut above the page. */
function frontMatterBlock(yaml: string): string {
  const escaped = yaml.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return `<details class="frontmatter"><summary>Front matter</summary><pre>${escaped}</pre></details>`;
}

/** Whether a file is worth rendering as markdown rather than as plain text. */
export function isMarkdown(kind: string): boolean {
  return kind === "MD" || kind === "MARKDOWN";
}
