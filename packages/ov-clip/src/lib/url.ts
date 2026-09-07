/** Working with the address of the page being saved. */

/**
 * Query parameters that identify a campaign or a click, not a document.
 *
 * Stripping them means the same article shared through two channels is
 * recorded under one address, which is what makes `source_url` worth grepping.
 */
const TRACKING_PARAMS = new Set([
  "utm_source",
  "utm_medium",
  "utm_campaign",
  "utm_term",
  "utm_content",
  "utm_id",
  "fbclid",
  "gclid",
  "gad_source",
  "mc_cid",
  "mc_eid",
  "ref",
  "ref_src",
  "ref_url",
]);

/**
 * Reduce a URL to a stable form.
 *
 * Drops the fragment, the tracking parameters above, and a trailing slash, and
 * sorts what is left so parameter order stops mattering.
 *
 * @param raw - The address as the browser reported it.
 * @returns The canonical form, or `raw` unchanged when it will not parse.
 */
export function canonicalizeUrl(raw: string): string {
  try {
    const url = new URL(raw);
    url.hash = "";
    for (const param of TRACKING_PARAMS) url.searchParams.delete(param);
    url.searchParams.sort();
    if (url.pathname.length > 1 && url.pathname.endsWith("/")) {
      url.pathname = url.pathname.slice(0, -1);
    }
    return url.toString();
  } catch {
    return raw;
  }
}

/** The hostname of a URL, or an empty string when it will not parse. */
export function hostnameOf(raw: string): string {
  try {
    return new URL(raw).hostname;
  } catch {
    return "";
  }
}
