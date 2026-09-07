/**
 * The background page.
 *
 * It exists for one job: fetching images on the popup's behalf. The popup is
 * a page like any other and a cross-origin image fetch from it is subject to
 * the site's CORS policy, which most sites do not extend to strangers. The
 * background page carries the extension's `<all_urls>` host permission, so its
 * fetches are not, and it hands the bytes back as base64.
 *
 * It answers nothing else, and it never sees a credential.
 */

/** Below this an image is a tracking pixel or a spacer, not content. */
const MIN_IMAGE_BYTES = 200;
/** Above this an image is not worth putting in a knowledge base. */
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
/** How long one image may take before it is abandoned. */
const IMAGE_TIMEOUT_MS = 15_000;

interface DownloadImageMessage {
  action: "downloadImage";
  url: string;
}

/** What the popup gets back. `ok: false` means "skip this image", not "stop". */
export interface DownloadImageResponse {
  ok: boolean;
  base64?: string;
  contentType?: string;
}

/** Base64-encode bytes without blowing the call stack on a large image. */
function toBase64(bytes: Uint8Array): string {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 8192) {
    const chunk = bytes.subarray(i, i + 8192);
    for (const byte of chunk) binary += String.fromCharCode(byte);
  }
  return btoa(binary);
}

async function downloadImage(url: string): Promise<DownloadImageResponse> {
  try {
    const response = await fetch(url, {
      signal: AbortSignal.timeout(IMAGE_TIMEOUT_MS),
      // An image is not a credentialed resource; sending cookies to a third
      // party while clipping a page is not something the reader asked for.
      credentials: "omit",
    });
    if (!response.ok) return { ok: false };

    const contentType = response.headers.get("content-type") ?? "";
    if (!contentType.startsWith("image/")) return { ok: false };

    const buffer = await response.arrayBuffer();
    if (buffer.byteLength < MIN_IMAGE_BYTES || buffer.byteLength > MAX_IMAGE_BYTES) {
      return { ok: false };
    }
    return { ok: true, base64: toBase64(new Uint8Array(buffer)), contentType };
  } catch {
    return { ok: false };
  }
}

browser.runtime.onMessage.addListener(
  (message: unknown): Promise<DownloadImageResponse> | undefined => {
    const request = message as DownloadImageMessage;
    if (request?.action !== "downloadImage" || typeof request.url !== "string") {
      return undefined;
    }
    return downloadImage(request.url);
  },
);
