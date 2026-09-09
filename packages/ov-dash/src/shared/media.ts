/**
 * Which files the reading pane shows as pictures rather than as a download.
 *
 * The list lives here, in shared, because both ends need it and they must
 * agree: the server decides what content-type it is willing to serve inline,
 * and the client decides what to put in an `<img>`. One table means the two
 * cannot drift apart — a client that tries a format the server refuses shows a
 * broken image for no reason.
 */

/**
 * Image kinds the dashboard renders inline, mapped to the type it serves them
 * as. Keys are `kindOf`'s upper-case extensions.
 *
 * Raster formats only, and that is deliberate. An SVG is a document: it can
 * carry script, and served from this origin that script would run with the
 * session cookie in reach. SVGs keep the download button instead.
 */
export const INLINE_IMAGE_TYPES: Readonly<Record<string, string>> = {
  PNG: "image/png",
  JPG: "image/jpeg",
  JPEG: "image/jpeg",
  GIF: "image/gif",
  WEBP: "image/webp",
  AVIF: "image/avif",
  BMP: "image/bmp",
  ICO: "image/x-icon",
};

/** Whether a file of this kind can be shown inline. */
export function isInlineImage(kind: string): boolean {
  return Object.hasOwn(INLINE_IMAGE_TYPES, kind);
}

/** The content-type to serve this kind as, or undefined if it is not one. */
export function inlineImageType(kind: string): string | undefined {
  return isInlineImage(kind) ? INLINE_IMAGE_TYPES[kind] : undefined;
}
