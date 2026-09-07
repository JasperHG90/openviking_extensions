/**
 * Deciding what gets written, and where.
 *
 * Kept apart from the popup so the interesting decisions — one file or a
 * folder, what the note is attached to, what a thing ends up called — can be
 * tested without a browser.
 *
 * OpenViking imports one file per call, so an article with images is a folder
 * of siblings rather than a single document with attachments. The markdown
 * already refers to the images by bare filename, which is what makes them
 * resolve once they are stored next to it.
 */

import type { ExtractResult } from "../types";
import { type ArticleFrontmatter, buildDocument } from "./frontmatter";
import { documentFilename, joinTarget, slugify } from "./naming";
import { canonicalizeUrl, hostnameOf } from "./url";

/**
 * One file to send to OpenViking. Exactly one contents field is set.
 *
 * Each carries its own `to`, and no two may share one. OpenViking treats a
 * destination as *the* resource: importing a second file to a `to` that
 * already holds one replaces it outright — the first document is gone, not
 * merged, not renamed. Measured against a real server, twice, in the
 * integration suite.
 */
export interface PlannedFile {
  filename: string;
  contentType: string;
  /** The `viking://` directory this file becomes, owned by it alone. */
  to: string;
  /** Contents, for a file authored here. */
  text?: string;
  /** Contents, for bytes that arrived base64-encoded from the background page. */
  base64?: string;
  /** Contents, for bytes fetched whole — a PDF, say. */
  bytes?: ArrayBuffer;
}

/** Everything one save writes. */
export interface SavePlan {
  /** The folder the files are created under, for showing someone. */
  target: string;
  files: PlannedFile[];
}

/** What the popup knows when someone presses Save on an article. */
export interface ArticleSaveInput {
  article: ExtractResult;
  /** Title as edited in the popup, which wins over the extracted one. */
  title: string;
  /** Author as edited in the popup. */
  byline?: string;
  /** Publication date as edited in the popup. */
  publishedTime?: string;
  scopeUri: string;
  folder: string;
  tags: string[];
  note: string;
  /** When this clip happened, as an ISO 8601 string. */
  savedAt: string;
}

/** Provenance shared by both kinds of save. */
function frontmatterFor(
  input: {
    title: string;
    byline?: string;
    publishedTime?: string;
    tags: string[];
    savedAt: string;
  },
  article: ExtractResult,
): ArticleFrontmatter {
  const url = canonicalizeUrl(article.url);
  return {
    title: input.title || article.title || "Untitled",
    url,
    hostname: article.hostname || hostnameOf(url),
    byline: input.byline || article.byline,
    siteName: article.siteName,
    publishedTime: input.publishedTime || article.publishedTime,
    tags: input.tags,
    savedAt: input.savedAt,
  };
}

/**
 * Plan the save of an extracted article.
 *
 * One file, in a directory of its own named after the article. The directory
 * is not decoration: OpenViking stores a resource's abstract and overview
 * beside it, and two clips sharing a destination do not merge — the second
 * replaces the first and the first is gone.
 *
 * Two clips of the same title do collide, deliberately: re-clipping a page
 * updates it rather than piling up copies.
 *
 * The pictures are linked at their original addresses rather than stored, for
 * the same layout reason — an image imported alongside would be a separate
 * resource, not a sibling file, so a relative link would resolve to nothing.
 *
 * @returns The folder the article was created under, and the one file.
 * @throws Error - When the folder is not a usable path.
 */
export function planArticleSave(input: ArticleSaveInput): SavePlan {
  const { article } = input;
  const slug = slugify(input.title || article.title || "Untitled");
  const base = joinTarget(input.scopeUri, input.folder);
  return {
    target: base,
    files: [
      {
        filename: `${slug}.md`,
        contentType: "text/markdown",
        to: joinTarget(base, slug),
        text: buildDocument(
          frontmatterFor(input, article),
          article.markdown ?? "",
          input.note,
        ),
      },
    ],
  };
}

/** What the popup knows when someone presses Save on a PDF or other document. */
export interface DocumentSaveInput {
  article: ExtractResult;
  title: string;
  scopeUri: string;
  folder: string;
  tags: string[];
  note: string;
  savedAt: string;
  /** The document itself, already fetched. */
  bytes: ArrayBuffer;
  contentType: string;
}

/**
 * Plan the save of a document that is downloaded rather than extracted.
 *
 * The document goes in as it arrived, so OpenViking parses the real thing, in
 * a directory of its own. A note or a tag has nowhere to live inside a PDF, so
 * when either is given a small markdown companion is written *beside* that
 * directory rather than into it — sharing the destination would have the note
 * replace the document it describes.
 *
 * @returns The folder both were created under, and the files, document first.
 */
export function planDocumentSave(input: DocumentSaveInput): SavePlan {
  const base = joinTarget(input.scopeUri, input.folder);
  const filename = documentFilename(input.article.url, input.title);
  const slug = slugify(input.title || input.article.title || "Untitled");

  const files: PlannedFile[] = [
    {
      filename,
      contentType: input.contentType,
      to: joinTarget(base, slug),
      bytes: input.bytes,
    },
  ];

  const hasNote = Boolean(input.note.trim()) || input.tags.length > 0;
  if (hasNote) {
    const front = frontmatterFor(
      {
        title: input.title,
        tags: input.tags,
        savedAt: input.savedAt,
      },
      input.article,
    );
    const body = `Saved document: [\`${filename}\`](${filename})`;
    files.push({
      filename: `${slug}.md`,
      contentType: "text/markdown",
      // Its own directory beside the document's, never inside it.
      to: joinTarget(base, `${slug}-notes`),
      text: buildDocument(front, body, input.note),
    });
  }

  return { target: base, files };
}

/** Guess a content type from a filename, for the multipart part header. */
export function contentTypeFor(filename: string): string {
  const extension = filename.slice(filename.lastIndexOf(".") + 1).toLowerCase();
  const known: Record<string, string> = {
    png: "image/png",
    jpg: "image/jpeg",
    jpeg: "image/jpeg",
    gif: "image/gif",
    webp: "image/webp",
    svg: "image/svg+xml",
    avif: "image/avif",
    md: "text/markdown",
    pdf: "application/pdf",
  };
  return known[extension] ?? "application/octet-stream";
}

/** Decode base64 into the bytes an upload sends. */
export function base64ToBytes(base64: string): Uint8Array {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}
