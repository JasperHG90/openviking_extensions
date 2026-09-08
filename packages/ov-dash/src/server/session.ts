/**
 * Browser sessions.
 *
 * The cookie names a session; it is not the session. It carries a signed,
 * unguessable id and nothing else — no identity, no credential — and the thing
 * it names lives in {@link SessionStore} on the server.
 *
 * That is what makes signing out mean something. The cookie used to carry the
 * session itself, so ending one meant asking the browser to forget it: any copy
 * taken beforehand kept working until it expired. Now the server drops the
 * entry and every copy dies with it.
 *
 * The login cookie below is the exception and stays self-contained: it holds
 * PKCE state for the few seconds of a redirect, is useless to anyone who is not
 * mid-login, and keeping it stateless means a restart in the middle of a
 * sign-in is survivable.
 */

import { hkdfSync } from "node:crypto";
import type { Context } from "hono";
import { deleteCookie, getCookie, setCookie } from "hono/cookie";
import { SignJWT, jwtVerify } from "jose";
import { z } from "zod";
import type { Viewer } from "../shared/schemas";
import type { Config } from "./env";
import type { Session, SessionStore } from "./store";

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

/** All the cookie says: which session this is. */
const sessionClaimsSchema = z.object({ sid: z.string().min(1) });

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
 * `SESSION_SECRET` has a length floor but no entropy requirement, so a
 * passphrase-shaped secret is allowed. Feeding it to an algorithm raw makes a
 * captured cookie an offline guessing target; HKDF puts a derivation in the
 * way. The purpose string is what keeps a future second use from sharing this
 * key — today both cookies are signed with the same one and told apart by
 * their audience.
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

/** Name a session in the cookie. The id is all that travels. */
async function writeCookie(
  c: Context,
  config: Config,
  sid: string,
  ttlSeconds: number,
): Promise<void> {
  const token = await sign(config, { sid }, SESSION_AUDIENCE, ttlSeconds);
  setCookie(c, config.SESSION_COOKIE_NAME, token, cookieOptions(config, ttlSeconds));
}

/**
 * Read the session id out of the cookie.
 *
 * The signature is checked before the id is used, so a forged or tampered
 * cookie never reaches the store. The id is unguessable on its own; the
 * signature is what makes a garbage cookie cheap to reject.
 */
async function readSid(c: Context, config: Config): Promise<string | null> {
  const token = getCookie(c, config.SESSION_COOKIE_NAME);
  if (!token) return null;
  const payload = await verify(config, token, SESSION_AUDIENCE);
  if (!payload) return null;
  const parsed = sessionClaimsSchema.safeParse(payload);
  return parsed.success ? parsed.data.sid : null;
}

/**
 * Replace whatever session this browser was already holding.
 *
 * Signing in again overwrites the cookie, so the previous id becomes something
 * the person can never present and the sign-out button can never reach — while
 * the entry behind it stays live for its whole TTL. Anyone holding a copy of
 * the *old* cookie would keep working through a sign-out, which is the exact
 * hole the store was added to close. So the old one goes as the new one is made.
 */
async function replacePrevious(
  c: Context,
  config: Config,
  store: SessionStore,
): Promise<void> {
  const previous = await readSid(c, config);
  if (previous) store.drop(previous);
}

/**
 * Start a session that carries an identity.
 *
 * Used where the credential is resolved per request from configuration, so
 * there is nothing to hold but who this is.
 *
 * @param store - Where the session lives.
 * @param viewer - Who it is for.
 */
export async function startSession(
  c: Context,
  config: Config,
  store: SessionStore,
  viewer: Viewer,
): Promise<void> {
  await replacePrevious(c, config, store);
  const ttl = config.SESSION_TTL_SECONDS;
  const sid = store.create(viewer, Math.floor(Date.now() / 1000) + ttl);
  await writeCookie(c, config, sid, ttl);
}

/**
 * Read the current session.
 *
 * @returns The session, or null when the cookie is absent, forged, expired, or
 *   names a session that has been signed out.
 */
export async function readSession(
  c: Context,
  config: Config,
  store: SessionStore,
): Promise<Session | null> {
  const sid = await readSid(c, config);
  return sid ? store.read(sid) : null;
}

/**
 * End the session.
 *
 * Drops the entry first and clears the cookie second. The clearing is a
 * courtesy to the browser that asked; the drop is what actually ends it, for
 * every copy of that cookie at once.
 */
export async function endSession(
  c: Context,
  config: Config,
  store: SessionStore,
): Promise<void> {
  const sid = await readSid(c, config);
  if (sid) store.drop(sid);
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
 * Start a session that carries an OpenViking credential.
 *
 * The token never reaches the browser at all — not even encrypted. It sits in
 * the store beside the identity it speaks for, and the cookie names the pair.
 *
 * @param c - The request context.
 * @param config - Validated configuration.
 * @param store - Where the session lives.
 * @param viewer - Identity the credential speaks for.
 * @param token - The OpenViking credential.
 * @param expiresAt - Unix seconds the credential expires, when it says so. The
 *   session never outlives the token it carries.
 */
export async function startCredentialSession(
  c: Context,
  config: Config,
  store: SessionStore,
  viewer: Viewer,
  token: string,
  expiresAt: number | null,
): Promise<void> {
  const now = Math.floor(Date.now() / 1000);
  // `!== null`, not truthiness: an `exp` of 0 is a token from 1970, and reading
  // it as "no expiry given" would give it a full-length session.
  const budget =
    expiresAt !== null ? Math.max(0, expiresAt - now) : config.SESSION_TTL_SECONDS;
  const ttl = Math.min(config.SESSION_TTL_SECONDS, budget);
  if (ttl <= 0) {
    // A typed error, so this surfaces as "sign in again" rather than as the
    // generic 500 a bare Error falls through to.
    throw new SessionError(
      "that credential has already expired \u2014 sign in again",
      401,
    );
  }

  await replacePrevious(c, config, store);
  const sid = store.create(viewer, now + ttl, token);
  await writeCookie(c, config, sid, ttl);
}

/**
 * Read a session that carries a credential.
 *
 * @returns The viewer, their OpenViking credential, and the unix second the
 *   session ends at. Null when there is no session, or when the one named
 *   carries no credential — an identity session must never be mistaken for a
 *   credential one, and here that is a property of the stored entry rather than
 *   of how the cookie was labelled.
 */
export async function readCredentialSession(
  c: Context,
  config: Config,
  store: SessionStore,
): Promise<{ viewer: Viewer; token: string; expiresAt: number } | null> {
  const session = await readSession(c, config, store);
  if (!session?.token) return null;
  return { viewer: session.viewer, token: session.token, expiresAt: session.expiresAt };
}
