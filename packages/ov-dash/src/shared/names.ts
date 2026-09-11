/**
 * What a name may look like once it becomes a path segment.
 *
 * The rule lives in shared because both ends need it and they must agree: the
 * server decides what it will put in a uri, and the New folder box shows the
 * name you will actually get before you press the button. One table means a
 * person cannot type something that previews one way and lands another.
 */

/**
 * Reduce a name to one path segment, with anything a uri should not carry
 * replaced.
 *
 * Separators go first, so a name carrying a path cannot climb out of the folder
 * it was typed into. What is left keeps letters, digits, dot, underscore and
 * hyphen; everything else — spaces, quotes, slashes already gone — becomes an
 * underscore.
 */
export function safeSegment(name: string): string {
  const base = name.split(/[/\\]/).pop() ?? "";
  return base.replace(/[^A-Za-z0-9._-]/g, "_");
}

/**
 * Whether a segment names nothing.
 *
 * Empty, or only dots. `.` and `..` are the path-walking cases, and a segment
 * of nothing but dots is not a name anybody meant to type — it reads as a
 * directory on every filesystem this will ever touch.
 */
export function namesNothing(segment: string): boolean {
  return /^\.*$/.test(segment);
}

/**
 * Longest one path segment may be.
 *
 * 255 bytes is the limit on every filesystem this lands on, and the cleaned
 * name is ASCII, so characters and bytes are the same count here.
 */
export const MAX_SEGMENT = 255;

/**
 * Why a cleaned name cannot be a folder, or "" when it can.
 *
 * Shared so the New folder box can refuse a name in the same words the server
 * would, before anybody presses the button.
 */
export function folderNameProblem(segment: string): string {
  if (namesNothing(segment)) return "a folder needs a name";
  if (segment.startsWith(".")) {
    // OpenViking lists without `-a`, so a dotted folder would not appear in the
    // tree at all — and `.abstract.md` and `.overview.md` are names it writes
    // its own files under, inside the very folder this would sit in.
    return "a name starting with a dot is hidden from the tree, and OpenViking writes its own files under those names";
  }
  if (segment.length > MAX_SEGMENT) {
    return `a name can be at most ${MAX_SEGMENT} characters`;
  }
  return "";
}
