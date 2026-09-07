/**
 * Asking ovx for a token.
 *
 * The extension holds no credential of its own. It cannot: a Firefox extension
 * has no filesystem access, so it cannot read `~/.ovx/tokens/<profile>.json`,
 * and storing a copy would mean a second place a token can leak from and go
 * stale. Instead it asks ovx over native messaging each time it needs one, and
 * ovx renews from the live Vault session exactly as it does for `ov`.
 *
 * Which means: no Vault password here, no Vault calls here, and nothing on
 * disk to steal. Closing Firefox loses nothing, because there was nothing.
 */

/** The native host's name, matching what `ovx --install-firefox-host` writes. */
const HOST = "ovx";

/** What a successful token request comes back with. */
export interface OvxToken {
  /** The Vault identity token, to be sent to OpenViking. */
  token: string;
  /** Profile it was minted for. */
  profile: string;
  /**
   * OpenViking address from the profile, so nothing has to be typed twice.
   *
   * The profile's `account` and `user` deliberately do not come with it.
   * OpenViking reads identity from the token's claims, so this client sends no
   * identity headers — unlike the SDK, which still offers them — and asking
   * for fields nothing reads would put them on the wire for no reason.
   */
  url: string;
}

/** Raised when ovx cannot be reached, or answers that it cannot help. */
export class OvxError extends Error {
  /**
   * @param message - What went wrong, safe to show someone.
   * @param hint - What to do about it, usually a command to run.
   * @param missing - True when ovx is not reachable at all, rather than
   *   reachable and refusing. The two need different advice.
   */
  constructor(
    message: string,
    readonly hint = "",
    readonly missing = false,
  ) {
    super(message);
    this.name = "OvxError";
  }
}

/** The shape ovx answers with. Parsed rather than trusted. */
interface Reply {
  ok?: unknown;
  error?: unknown;
  hint?: unknown;
  [key: string]: unknown;
}

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/**
 * Send one message to ovx and hand back its reply.
 *
 * @param message - The request body, which must name an `action`.
 * @returns The decoded reply.
 * @throws OvxError - When the host cannot be started, or answers `ok: false`.
 */
async function ask(message: Record<string, unknown>): Promise<Reply> {
  let reply: unknown;
  try {
    reply = await browser.runtime.sendNativeMessage(HOST, message);
  } catch (error) {
    // Firefox says "no such native application" whether ovx is not installed,
    // the manifest was never written, or Firefox has not been restarted since.
    // It cannot tell them apart, so the hint names all three.
    const reason = error instanceof Error ? error.message : String(error);
    throw new OvxError(
      `Could not reach ovx: ${reason}`,
      "Install ovx, run `ovx --install-firefox-host`, then restart Firefox.",
      true,
    );
  }

  if (typeof reply !== "object" || reply === null) {
    throw new OvxError("ovx answered with something unreadable.");
  }
  const body = reply as Reply;
  if (body.ok !== true) {
    throw new OvxError(str(body.error) || "ovx refused the request.", str(body.hint));
  }
  return body;
}

/**
 * Check that ovx is there, and say which version.
 *
 * @returns The ovx version string.
 */
export async function ping(): Promise<string> {
  return str((await ask({ action: "ping" })).version);
}

/**
 * List the profiles ovx knows about, for the options page's picker.
 *
 * @returns Profile names, in the order ovx reports them.
 */
export async function listProfiles(): Promise<string[]> {
  const profiles = (await ask({ action: "profiles" })).profiles;
  if (!Array.isArray(profiles)) return [];
  return profiles.filter((name): name is string => typeof name === "string");
}

/**
 * Fetch a live token for one profile.
 *
 * Called per save rather than cached. A token here would be a second copy that
 * can go stale while ovx's is fresh, and the call is a local pipe — cheaper
 * than the upload that follows it.
 *
 * @param profile - Profile name, as `ovx -l` lists it.
 * @returns The token and where to spend it.
 * @throws OvxError - When the profile has no stored login, or its login cannot
 *   be renewed. The hint says which `ovx` command fixes it.
 */
export async function fetchToken(profile: string): Promise<OvxToken> {
  if (!profile) {
    throw new OvxError("No ovx profile chosen.", "Pick one in the extension's settings.");
  }
  const body = await ask({ action: "token", profile });
  const token = str(body.token);
  if (!token) {
    throw new OvxError("ovx answered without a token.");
  }
  return {
    token,
    profile: str(body.profile) || profile,
    url: str(body.url),
  };
}
