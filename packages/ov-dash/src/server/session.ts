/**
 * Browser sessions, carried in a signed cookie.
 *
 * The cookie holds an identity and nothing else. A person's OpenViking API key
 * never reaches the browser: the server resolves it per request from the
 * identity in this cookie, so a stolen cookie is worth a session, not a
 * credential that outlives one.
 *
 * The payload is signed rather than encrypted. Its contents — a name, an email,
 * an account — are already known to the person holding it; what matters is that
 * they cannot change them, and a signature gives that.
 */

import { hkdfSync } from "node:crypto";
import type { Context } from "hono";
import { deleteCookie, getCookie, setCookie } from "hono/cookie";
import { EncryptJWT, SignJWT, jwtDecrypt, jwtVerify } from "jose";
import { z } from "zod";
import type { Viewer } from "../shared/schemas";
import type { Config } from "./env";

const ISSUER = "ov-dash";

/** Raised when a session cannot be built or read. */
export class SessionError extends Error {
  /**
   * @param message - What went wrong, safe to show.
   * @param status - HTTP status the request should end with.
   */
  constructor(
    message: string,
    readonly status = 400,
  ) {
    super(message);
    this.name = "SessionError";
  }
}
const SESSION_AUDIENCE = "ov-dash/session";
const LOGIN_AUDIENCE = "ov-dash/login";

/** How long a half-finished login may sit before the callback is refused. */
const LOGIN_TTL_SECONDS = 600;

const sessionClaimsSchema = z.object({
  sub: z.string(),
  name: z.string(),
  email: z.string(),
  account: z.string(),
  user: z.string(),
});

/**
 * The state a login carries across the redirect to the provider and back.
 *
 * It rides in its own short-lived cookie rather than in server memory, so a
 * restart mid-login is survivable and two replicas need nothing shared.
 */
const loginClaimsSchema = z.object({
  state: z.string(),
  nonce: z.string(),
  verifier: z.string(),
  returnTo: z.string(),
});
export type LoginState = z.infer<typeof loginClaimsSchema>;

/**
 * Derive a purpose-specific key from the configured secret.
 *
 * The signing key and the credential-encryption key used to be the same
 * secret fed to two algorithms — one raw, one through a bare SHA-256. Same
 * input, no domain separation, and `SESSION_SECRET` has a length floor but no
 * entropy requirement, so a passphrase-shaped secret plus one captured cookie
 * is an offline guess that ends in a live OpenViking token. HKDF gives each
 * use its own key, so breaking one tells you nothing about the other.
 */
function derive(config: Config, purpose: string, bytes = 32): Uint8Array {
  return new Uint8Array(
    hkdfSync("sha256", config.SESSION_SECRET, "ov-dash", purpose, bytes),
  );
}

function key(config: Config): Uint8Array {
  return derive(config, "session-signing");
}

function loginCookieName(config: Config): string {
  return `${config.SESSION_COOKIE_NAME}_login`;
}

async function sign(
  config: Config,
  claims: Record<string, string>,
  audience: string,
  ttlSeconds: number,
): Promise<string> {
  return new SignJWT(claims)
    .setProtectedHeader({ alg: "HS256" })
    .setIssuedAt()
    .setIssuer(ISSUER)
    .setAudience(audience)
    .setExpirationTime(`${ttlSeconds}s`)
    .sign(key(config));
}

async function verify(
  config: Config,
  token: string,
  audience: string,
): Promise<Record<string, unknown> | null> {
  try {
    const { payload } = await jwtVerify(token, key(config), {
      issuer: ISSUER,
      audience,
    });
    return payload;
  } catch {
    return null;
  }
}

function cookieOptions(config: Config, maxAge: number) {
  return {
    httpOnly: true,
    secure: config.SESSION_COOKIE_SECURE,
    // Lax, not Strict: the provider redirects the browser back to us with a
    // top-level GET, and Strict would withhold the cookie on exactly that hop.
    sameSite: "Lax" as const,
    path: "/",
    maxAge,
  };
}

/** Write the signed-in cookie for `viewer`. */
export async function startSession(
  c: Context,
  config: Config,
  viewer: Viewer,
): Promise<void> {
  const token = await sign(
    config,
    { ...viewer },
    SESSION_AUDIENCE,
    config.SESSION_TTL_SECONDS,
  );
  setCookie(
    c,
    config.SESSION_COOKIE_NAME,
    token,
    cookieOptions(config, config.SESSION_TTL_SECONDS),
  );
}

/**
 * Read the current viewer.
 *
 * @returns The viewer, or null when the cookie is absent, expired, or fails
 *   its signature check.
 */
export async function readSession(c: Context, config: Config): Promise<Viewer | null> {
  const token = getCookie(c, config.SESSION_COOKIE_NAME);
  if (!token) return null;
  const payload = await verify(config, token, SESSION_AUDIENCE);
  if (!payload) return null;
  const parsed = sessionClaimsSchema.safeParse(payload);
  return parsed.success ? parsed.data : null;
}

/** Drop the session cookie. */
export function endSession(c: Context, config: Config): void {
  deleteCookie(c, config.SESSION_COOKIE_NAME, { path: "/" });
}

/** Stash the PKCE verifier, state and nonce for the duration of the redirect. */
export async function startLogin(
  c: Context,
  config: Config,
  state: LoginState,
): Promise<void> {
  const token = await sign(config, { ...state }, LOGIN_AUDIENCE, LOGIN_TTL_SECONDS);
  setCookie(c, loginCookieName(config), token, cookieOptions(config, LOGIN_TTL_SECONDS));
}

/**
 * Take back the login state and clear its cookie.
 *
 * This is not single-use on the server. The state is a signed JWT, so
 * presenting the same cookie twice verifies twice; all this does is send a
 * clearing `Set-Cookie`, which a cooperating browser then honours. What
 * actually stops a replayed callback is the provider refusing to redeem an
 * authorization code a second time.
 *
 * Making it genuinely one-use would need server-side state or a stored jti,
 * which is a real cost for two replicas — worth paying only if the provider
 * cannot be trusted to enforce single-use codes.
 */
export async function takeLogin(c: Context, config: Config): Promise<LoginState | null> {
  const name = loginCookieName(config);
  const token = getCookie(c, name);
  deleteCookie(c, name, { path: "/" });
  if (!token) return null;
  const payload = await verify(config, token, LOGIN_AUDIENCE);
  if (!payload) return null;
  const parsed = loginClaimsSchema.safeParse(payload);
  return parsed.success ? parsed.data : null;
}

/**
 * The audience for a session that carries a credential, kept distinct from the
 * plain identity session so one can never be read as the other.
 */
const CREDENTIAL_AUDIENCE = "ov-dash/credential";

/**
 * Encryption key for the credential cookie.
 *
 * A256GCM needs exactly 32 bytes and `SESSION_SECRET` is any length, so it is
 * hashed rather than truncated — truncating would quietly throw away entropy
 * from a long secret.
 */
function encryptionKey(config: Config): Uint8Array {
  return derive(config, "credential-encryption");
}

/**
 * Start a session that carries an OpenViking credential.
 *
 * Encrypted, not merely signed. The other session cookie holds an identity the
 * person already knows; this one holds a token that speaks to OpenViking as
 * them, so its contents must not be readable by anything that gets hold of the
 * cookie — including the browser it is stored in.
 *
 * @param c - The request context.
 * @param config - Validated configuration.
 * @param viewer - Identity the credential speaks for.
 * @param token - The OpenViking credential.
 * @param expiresAt - Unix seconds the credential expires, when it says so. The
 *   cookie never outlives the token it carries.
 */
export async function startCredentialSession(
  c: Context,
  config: Config,
  viewer: Viewer,
  token: string,
  expiresAt: number | null,
): Promise<void> {
  const now = Math.floor(Date.now() / 1000);
  const budget = expiresAt ? Math.max(0, expiresAt - now) : config.SESSION_TTL_SECONDS;
  const ttl = Math.min(config.SESSION_TTL_SECONDS, budget);
  if (ttl <= 0) {
    // A typed error, so this surfaces as "sign in again" rather than as the
    // generic 500 a bare Error falls through to.
    throw new SessionError("that credential has already expired — sign in again", 401);
  }

  const jwe = await new EncryptJWT({ ...viewer, ov: token })
    .setProtectedHeader({ alg: "dir", enc: "A256GCM" })
    .setIssuedAt()
    .setIssuer(ISSUER)
    .setAudience(CREDENTIAL_AUDIENCE)
    .setExpirationTime(`${ttl}s`)
    .encrypt(encryptionKey(config));

  setCookie(c, config.SESSION_COOKIE_NAME, jwe, cookieOptions(config, ttl));
}

/**
 * Read a credential session.
 *
 * @returns The viewer and their OpenViking credential, or null when the cookie
 *   is absent, expired, or does not decrypt.
 */
export async function readCredentialSession(
  c: Context,
  config: Config,
): Promise<{ viewer: Viewer; token: string } | null> {
  const cookie = getCookie(c, config.SESSION_COOKIE_NAME);
  if (!cookie) return null;

  try {
    const { payload } = await jwtDecrypt(cookie, encryptionKey(config), {
      issuer: ISSUER,
      audience: CREDENTIAL_AUDIENCE,
    });
    const viewer = sessionClaimsSchema.safeParse(payload);
    const token = payload.ov;
    if (!viewer.success || typeof token !== "string" || !token) return null;
    return { viewer: viewer.data, token };
  } catch {
    return null;
  }
}
