/**
 * What counts as a usable OpenViking user id.
 *
 * The identity the dashboard mints is not just a label. It becomes a path
 * segment in two different systems: the Vault KV path a key is read from, and
 * the `viking://user/<id>/...` prefix every file lives under. A value carrying
 * a separator escapes both.
 *
 * OpenViking enforces this on its own side — resource targets must be "safe
 * single-segment identifiers", and values with path separators, `.`, `..`, `:`
 * or `+` are rejected — but the dashboard is what turns a claim into that
 * identifier, so the check belongs here too, before the value is ever used.
 */

/**
 * A safe single path segment: alphanumeric, then dots, dashes and underscores.
 *
 * Deliberately narrower than "anything without a slash". A leading dot would
 * allow `.` and `..`; a colon or a percent sign would let a value change what
 * a URL parser thinks the path is.
 */
const USER_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

/** Raised when a claim cannot safely become an OpenViking user id. */
export class IdentityError extends Error {
  /**
   * @param message - What was wrong, safe to show. Never echoes the raw value,
   *   which could carry a path or a credential-shaped string.
   * @param status - HTTP status the request should end with.
   */
  constructor(
    message: string,
    readonly status = 403,
  ) {
    super(message);
    this.name = "IdentityError";
  }
}

/**
 * Whether a value is safe to use as an OpenViking user id.
 *
 * Rejects `..` explicitly even though the pattern already does, because that is
 * the case worth being able to point at when this changes.
 */
export function isSafeUserId(value: string): boolean {
  if (value === "." || value === "..") return false;
  return USER_ID.test(value);
}

/**
 * Check a mapped user id, or refuse.
 *
 * @param value - The candidate id, already extracted from a claim or header.
 * @param source - Where it came from, named in the error so a failing login is
 *   diagnosable without logging the value.
 * @returns The same id, once it is known to be safe.
 * @throws IdentityError - When the value could escape a path.
 */
export function requireUserId(value: string, source: string): string {
  if (!isSafeUserId(value)) {
    throw new IdentityError(
      `the ${source} claim did not produce a usable OpenViking user id — it must be a single path segment of letters, digits, dots, dashes or underscores`,
    );
  }
  return value;
}
