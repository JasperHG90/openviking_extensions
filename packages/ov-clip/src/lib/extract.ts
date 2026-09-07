/**
 * Getting the article out of the tab you are looking at.
 *
 * This is the reason the clipper is an extension rather than something the
 * server does. The server would have to fetch the page itself and would get
 * whatever a bot gets: a consent wall, a login form, an empty shell waiting for
 * JavaScript. The extension reads the page that is already rendered in front of
 * you, so what gets saved is what you were reading.
 */

import { Readability } from "@mozilla/readability";
import TurndownService from "turndown";
import type { ExtractResult } from "../types";
import { extractArticleImages } from "./images";
import { extractMetadata } from "./metadata";
import { hostnameOf } from "./url";

/** How long the whole image pass may take before the article is saved anyway. */
const IMAGE_BUDGET_MS = 30_000;

/** What kind of thing the tab is, which decides how it gets saved. */
export type PageKind = "article" | "document";

export interface Extraction {
  kind: PageKind;
  result: ExtractResult;
  /** Set when the page was read but no article could be found in it. */
  warning?: string;
}

function turndown(): TurndownService {
  return new TurndownService({ headingStyle: "atx", codeBlockStyle: "fenced" });
}

/**
 * Decide whether a URL points at a document to upload rather than a page to read.
 *
 * The extension check catches the common case without a request. The HEAD is
 * for the addresses that do not admit what they are, like arXiv's `/pdf/2401.1`.
 */
export async function isDocument(url: string): Promise<boolean> {
  try {
    if (new URL(url).pathname.toLowerCase().endsWith(".pdf")) return true;
  } catch {
    return false;
  }
  try {
    // With the site's cookies. The popup's origin is `moz-extension://…`, so
    // the default of `same-origin` sends none, and a PDF behind a login would
    // answer with the login page and be mistaken for an article.
    const response = await fetch(url, { method: "HEAD", credentials: "include" });
    return (response.headers.get("content-type") ?? "").includes("application/pdf");
  } catch {
    return false;
  }
}

/** How far into a file the PDF header may sit. Readers tolerate some junk. */
const PDF_HEADER_WINDOW = 1024;

/**
 * Whether these bytes are actually a PDF.
 *
 * Judged on the content, not on the `Content-Type`. A site that wants you
 * signed in answers with its login page, HTTP 200, and whatever header it
 * likes — including none, which is exactly the case a header check waves
 * through. Without this the clipper would store that page under a `.pdf` name
 * and the failure would surface much later, as an unreadable file in
 * OpenViking.
 *
 * `isDocument` only ever routes PDFs down this path, so one magic number is
 * the whole rule.
 *
 * @param bytes - The downloaded file.
 * @returns True when the PDF header appears near the start.
 */
export function looksLikePdf(bytes: ArrayBuffer): boolean {
  const head = new Uint8Array(bytes, 0, Math.min(PDF_HEADER_WINDOW, bytes.byteLength));
  return new TextDecoder("latin1").decode(head).includes("%PDF-");
}

/**
 * Resolve `blob:` images inside the page and hand back their bytes.
 *
 * Blob URLs are page-scoped and ephemeral: the background page cannot fetch
 * one, and Readability strips the `data:` URLs they would become. So they are
 * resolved in the page, returned as a side channel keyed by alt text, and
 * matched back up after Readability has run.
 */
async function resolveBlobImages(tabId: number): Promise<Record<string, string>> {
  try {
    const results = await browser.scripting.executeScript({
      target: { tabId },
      func: async () => {
        const images = document.querySelectorAll<HTMLImageElement>('img[src^="blob:"]');
        const TIMEOUT = 10_000;
        const resolved: Record<string, string> = {};
        const altCounts: Record<string, number> = {};

        await Promise.all(
          Array.from(images).map(async (img) => {
            try {
              if (!img.complete) {
                await Promise.race([
                  new Promise<void>((resolve) => {
                    img.addEventListener("load", () => resolve(), { once: true });
                    img.addEventListener("error", () => resolve(), { once: true });
                  }),
                  new Promise<void>((resolve) => setTimeout(resolve, TIMEOUT)),
                ]);
              }

              let dataUrl: string | null = null;
              try {
                // Preferred: the original bytes, untouched.
                const blob = await (await fetch(img.src)).blob();
                dataUrl = await new Promise<string>((resolve, reject) => {
                  const reader = new FileReader();
                  reader.onloadend = () => resolve(reader.result as string);
                  reader.onerror = () => reject(reader.error);
                  reader.readAsDataURL(blob);
                });
              } catch {
                // The blob was revoked. Repaint it through a canvas instead,
                // which loses the original encoding but keeps the picture.
                if (img.naturalWidth > 0 && img.naturalHeight > 0) {
                  const canvas = document.createElement("canvas");
                  canvas.width = img.naturalWidth;
                  canvas.height = img.naturalHeight;
                  const context = canvas.getContext("2d");
                  if (context) {
                    context.drawImage(img, 0, 0);
                    try {
                      dataUrl = canvas.toDataURL("image/png");
                    } catch {
                      // Tainted by another origin. Nothing to be done.
                    }
                  }
                }
              }
              if (!dataUrl) return;

              const baseKey = img.alt || img.dataset.fileid || "blob-image";
              const count = altCounts[baseKey] ?? 0;
              altCounts[baseKey] = count + 1;
              resolved[count === 0 ? baseKey : `${baseKey}__${count}`] = dataUrl;
            } catch {
              // One image failing is not the article failing.
            }
          }),
        );
        return resolved;
      },
    });
    return (results?.[0]?.result as Record<string, string>) ?? {};
  } catch {
    return {};
  }
}

/**
 * Never fetch an image, but let the rewriter run.
 *
 * Images used to be downloaded and stored beside the article. They cannot be:
 * OpenViking wraps each imported document in a directory of its own, so a
 * picture imported to the same place is never a sibling of the markdown that
 * references it, and `![](image-0.png)` resolves to nothing. Measured against
 * a real server — see the integration suite.
 *
 * So the pass still runs, for what it does besides downloading: resolving
 * `srcset` and lazy-loaded `data-src` attributes to absolute URLs, which is
 * what makes the links in the saved markdown work at all.
 */
async function downloadImage(_url: string) {
  return { ok: false as const };
}

/**
 * Read the active tab.
 *
 * @returns What was found, and whether it should be uploaded whole or saved as
 *   markdown. Never throws for a page it cannot parse: an article that will not
 *   extract still has a title and a URL worth keeping, and the popup says so.
 * @throws Error - Only when there is no tab to read at all.
 */
export async function extractActiveTab(): Promise<Extraction> {
  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) throw new Error("No active tab to read.");

  const url = tab.url ?? "";
  const bare: ExtractResult = {
    title: tab.title ?? "",
    url,
    hostname: hostnameOf(url),
  };

  if (await isDocument(url)) {
    return { kind: "document", result: bare };
  }

  const blobImages = await resolveBlobImages(tab.id);

  const scripted = await browser.scripting.executeScript({
    target: { tabId: tab.id },
    func: () => document.documentElement.outerHTML,
  });
  const html = scripted?.[0]?.result as string | undefined;
  if (!html) {
    return { kind: "article", result: bare, warning: "Could not read this page." };
  }

  const parser = new DOMParser();
  const doc = parser.parseFromString(html, "text/html");
  // Relative links and images resolve against this, not against the popup.
  const base = doc.createElement("base");
  base.href = url;
  doc.head.prepend(base);

  // Readability mutates the document it is handed, so metadata comes off a
  // second copy rather than off the wreckage.
  const metadata = extractMetadata(parser.parseFromString(html, "text/html"));
  const article = new Readability(doc).parse();

  if (!article) {
    return {
      kind: "article",
      result: {
        ...bare,
        byline: metadata.author ?? "",
        publishedTime: metadata.publishedTime ?? "",
      },
      warning: "No article found on this page. You can still save the title and link.",
    };
  }

  let markdown = turndown().turndown(article.content ?? "");
  try {
    const rewritten = await extractArticleImages(
      article.content ?? "",
      url,
      downloadImage,
      IMAGE_BUDGET_MS,
      blobImages,
    );
    markdown = rewritten.markdown;
  } catch {
    // Falls back to the plain conversion, which still has the article in it.
  }

  return {
    kind: "article",
    result: {
      title: article.title || tab.title || "",
      url,
      hostname: hostnameOf(url),
      markdown,
      excerpt: article.excerpt ?? "",
      byline: metadata.author || article.byline || "",
      siteName: article.siteName ?? "",
      publishedTime: metadata.publishedTime || article.publishedTime || "",
    },
  };
}
