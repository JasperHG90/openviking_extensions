/**
 * Bounds both ends have to agree on.
 *
 * Here for the same reason `names.ts` and `media.ts` are: the server decides
 * what it will accept, and the browser has to know before it offers a control
 * that leads somewhere the server refuses. One table means an Edit button
 * cannot appear on a file too long to save.
 */

/**
 * Most characters one edit may save.
 *
 * Generous for prose and far below the upload limit, which is the point: a save
 * arrives as a JSON string, so it is parsed and held in memory whole. The route
 * checks `content-length` first, so an honest oversize body is refused before it
 * is buffered; this bounds what is actually stored.
 */
export const MAX_EDIT_CHARS = 2_000_000;

/**
 * Most characters of the reason given for keeping an upload.
 *
 * OpenViking hands it to the model as the parsing instruction, so length is not
 * free: a page pasted in here becomes the prompt that decides how the resource
 * is described. A sentence or two is what it is for.
 */
export const MAX_REASON_CHARS = 1000;

/** Say a character budget the way a person reads one. */
export function describeLimit(chars: number): string {
  return chars >= 1_000_000
    ? `${chars / 1_000_000} million characters`
    : `${chars} characters`;
}
