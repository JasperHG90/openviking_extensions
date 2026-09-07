/**
 * Shapes shared across the extension.
 *
 * Only one lives here: what the popup pulled out of the page, which both the
 * extractor and the save planner need. Everything to do with talking to a
 * server belongs to the module that does the talking — `ov-api.ts` for
 * OpenViking, `ovx.ts` for the token — so those types are declared there.
 */

/** What the popup pulled out of the page. */
export interface ExtractResult {
  title: string;
  url: string;
  hostname: string;
  /**
   * The article, with every image link resolved to an absolute URL.
   *
   * The pictures are linked, never stored: OpenViking gives each imported
   * document a directory of its own, so an image imported alongside is not a
   * sibling of the markdown and a relative link would resolve to nothing.
   */
  markdown?: string;
  excerpt?: string;
  byline?: string;
  siteName?: string;
  publishedTime?: string;
}
