/**
 * The header that turns a scraped page into a document with provenance.
 *
 * OpenViking reads the file as markdown, so the frontmatter is there for the
 * person who opens it later and for grep: where this came from, who wrote it,
 * when it was published, and when it was clipped.
 */

/**
 * Render a string as a YAML scalar.
 *
 * Always quoted, never bare. A bare scalar has to be checked against a list of
 * things YAML would read as something else — `true`, `null`, `12:30`, a leading
 * `#` — and a title is exactly the kind of free text that eventually contains
 * one. Quoting unconditionally means there is no list to keep correct.
 */
export function yamlString(value: string): string {
  const escaped = value
    .replace(/\\/g, "\\\\")
    .replace(/"/g, '\\"')
    // A literal newline would end the scalar and break the document.
    .replace(/\r?\n/g, " ")
    .trim();
  return `"${escaped}"`;
}

/** Render a list of strings as a YAML flow sequence. */
export function yamlList(values: string[]): string {
  return `[${values.map(yamlString).join(", ")}]`;
}

/** What a saved article records about itself. */
export interface ArticleFrontmatter {
  title: string;
  /** Canonical page address; see `canonicalizeUrl`. */
  url: string;
  hostname: string;
  byline?: string;
  siteName?: string;
  /** Publication date as `YYYY-MM-DD`, when the page declared one. */
  publishedTime?: string;
  tags?: string[];
  /** When this was clipped, as an ISO 8601 string. */
  savedAt?: string;
}

/**
 * Build the frontmatter block, including its closing `---` and blank line.
 *
 * Empty fields are left out rather than written as `""`, so the header shows
 * what is known instead of a form with blanks in it.
 *
 * @param article - What is known about the page.
 * @returns The block, ending in a newline.
 */
export function buildFrontmatter(article: ArticleFrontmatter): string {
  const lines = ["---", `title: ${yamlString(article.title || "Untitled")}`];

  const optional: [string, string | undefined][] = [
    ["source_url", article.url],
    ["hostname", article.hostname],
    ["author", article.byline],
    ["site_name", article.siteName],
    ["publish_date", article.publishedTime],
    ["saved_at", article.savedAt],
  ];
  for (const [key, value] of optional) {
    if (value?.trim()) lines.push(`${key}: ${yamlString(value)}`);
  }

  const tags = (article.tags ?? []).map((tag) => tag.trim()).filter(Boolean);
  if (tags.length) lines.push(`tags: ${yamlList(tags)}`);

  lines.push("---", "");
  return `${lines.join("\n")}\n`;
}

/**
 * Assemble the whole file: frontmatter, the reader's own note, then the page.
 *
 * The note goes above the article on purpose. It is the reason this page was
 * kept, and it is what the reader wants first when the clip resurfaces in a
 * search result months later.
 *
 * @param article - Provenance for the frontmatter block.
 * @param body - The page, already converted to markdown.
 * @param note - What the person typed in the popup, possibly empty.
 * @returns The complete file contents.
 */
export function buildDocument(
  article: ArticleFrontmatter,
  body: string,
  note = "",
): string {
  const parts = [buildFrontmatter(article)];
  if (note.trim()) parts.push(`> ${note.trim().replace(/\r?\n/g, "\n> ")}\n\n`);
  parts.push(body.trim());
  return `${parts.join("").trimEnd()}\n`;
}
