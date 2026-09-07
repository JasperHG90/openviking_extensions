/**
 * Turning a page title into a name OpenViking can hold.
 *
 * A stored resource's name becomes part of a `viking://` URI, so it has to
 * survive being a path segment: no slashes, no spaces, nothing that needs
 * escaping to be typed at a shell or pasted into a link.
 */

/** Longest slug produced, so a URI stays readable and a filename stays legal. */
const MAX_SLUG = 60;

/**
 * Latin letters that decomposition leaves alone.
 *
 * NFKD splits an accented letter into a letter and a mark, which is why "é"
 * becomes "e". These are not accented letters, they are letters in their own
 * right, so nothing falls off them and "Straße" would otherwise slug to
 * "stra-e". The list stops at Latin on purpose: transliterating Greek or
 * Cyrillic is a different job with a much longer tail, and a title in a script
 * this cannot spell falls back to "untitled" instead of guessing.
 */
const STANDALONE_LETTERS: Record<string, string> = {
  æ: "ae",
  œ: "oe",
  ø: "o",
  ß: "ss",
  đ: "d",
  ð: "d",
  ł: "l",
  þ: "th",
};

/**
 * Reduce a title to one lowercase, hyphen-separated path segment.
 *
 * Accents are folded rather than dropped, so "Café" becomes "cafe" instead of
 * "caf" — the point is a recognisable name, not an ASCII purity test.
 *
 * @param title - The page title, or anything else being named.
 * @returns The slug, or "untitled" when nothing usable survives.
 */
export function slugify(title: string): string {
  const folded = title
    .normalize("NFKD")
    // Every combining mark the decomposition above left behind. `\p{M}` rather
    // than a hand-written range: decomposing anything past Latin puts marks
    // well outside U+0300\u2013U+036F, and this catches those too.
    .replace(/\p{M}/gu, "")
    .toLowerCase()
    .replace(
      /[\u00e6\u0153\u00f8\u00df\u0111\u00f0\u0142\u00fe]/g,
      (letter) => STANDALONE_LETTERS[letter] ?? letter,
    );

  const slug = folded
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, MAX_SLUG)
    // The slice may have landed mid-word and left a trailing hyphen.
    .replace(/-+$/, "");

  return slug || "untitled";
}

/**
 * Name the markdown file an article is saved as.
 *
 * @param title - The title as edited in the popup.
 * @returns A filename ending in `.md`.
 */
export function markdownFilename(title: string): string {
  return `${slugify(title)}.md`;
}

/**
 * Name the file a downloaded document is saved as.
 *
 * The extension is taken from the URL when there is one, because a PDF that
 * arrives as `/pdf/2401.01234` is still a PDF and OpenViking parses by
 * extension.
 *
 * @param url - Where the document was fetched from.
 * @param title - Fallback name when the URL's last segment is unusable.
 * @param fallbackExtension - Extension to apply when the URL carries none.
 * @returns A filename with an extension.
 */
export function documentFilename(
  url: string,
  title: string,
  fallbackExtension = "pdf",
): string {
  let last = "";
  try {
    last = new URL(url).pathname.split("/").filter(Boolean).pop() ?? "";
  } catch {
    last = "";
  }

  // An extension has to start with a letter. Without that rule arXiv's
  // `/pdf/2401.01234` reads as the file "2401" with extension "01234", and the
  // PDF lands under a name OpenViking will not parse as one.
  const match = /\.([A-Za-z][A-Za-z0-9]{0,7})$/.exec(last);
  const extension = (match?.[1] ?? fallbackExtension).toLowerCase();
  const stem = match ? last.slice(0, match.index) : last;
  const slug = slugify(stem || title);
  return `${slug}.${extension}`;
}

/**
 * Join a scope URI with an optional folder the person typed.
 *
 * @param scopeUri - A `viking://` root from the settings.
 * @param folder - A path below it, possibly empty, possibly slash-wrapped.
 * @returns The destination URI, with no trailing slash.
 * @throws Error - When the folder tries to climb out of the scope, so a typo
 *   fails here with something readable rather than as a 403 later.
 */
export function joinTarget(scopeUri: string, folder: string): string {
  const base = scopeUri.replace(/\/+$/, "");
  const segments = folder
    .split("/")
    .map((segment) => segment.trim())
    .filter(Boolean);

  if (segments.includes("..")) {
    throw new Error("A folder cannot contain '..'");
  }
  if (!segments.length) return base;
  return `${base}/${segments.map(slugifySegment).join("/")}`;
}

/** Keep a folder segment to characters that are safe in a URI path. */
function slugifySegment(segment: string): string {
  return segment.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "folder";
}
