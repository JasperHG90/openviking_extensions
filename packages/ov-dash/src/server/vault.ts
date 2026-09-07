/**
 * Signing in through Vault, and minting the token OpenViking accepts.
 *
 * There are two different Vault OIDC features here and they are not
 * interchangeable, which is the whole reason this module exists:
 *
 * - `identity/oidc/provider/<name>` is the browser login. Measured against the
 *   lab provider, its ID tokens carry `scopes_supported: ["openid"]` and
 *   `claims_supported: []`, and their issuer is Vault's `api_addr`. They say
 *   almost nothing about who you are.
 * - `identity/oidc/token/<role>` mints an *identity token* for whoever is
 *   calling. Those carry the `ov_account` and `ov_user` claims OpenViking maps
 *   identity from, under the issuer OpenViking trusts.
 *
 * So the login flow cannot hand its ID token to OpenViking — wrong issuer,
 * wrong audience, none of the claims. What works is: authenticate the person to
 * Vault, then mint an identity token *as them*. That is exactly what the `ov`
 * CLI does, done server-side so nobody needs the CLI.
 */

import { decodeJwt } from "jose";
import { z } from "zod";
import type { Viewer } from "../shared/schemas";
import type { Config } from "./env";
import { requireUserId } from "./identity";

/** Raised when Vault refuses, or answers with something unusable. */
export class VaultError extends Error {
  /**
   * @param message - What failed. Never contains a password or a token.
   * @param status - HTTP status the request should end with.
   */
  constructor(
    message: string,
    readonly status = 502,
  ) {
    super(message);
    this.name = "VaultError";
  }
}

const loginSchema = z.object({
  auth: z.object({
    client_token: z.string(),
    lease_duration: z.number().optional(),
  }),
});

const mintSchema = z.object({
  data: z.object({
    token: z.string(),
    ttl: z.number().optional(),
  }),
});

/** The identity claims OpenViking reads out of a minted token. */
const claimsSchema = z.object({
  ov_account: z.string(),
  ov_user: z.string(),
  exp: z.number().optional(),
});

/** A minted credential and the identity it speaks for. */
export interface VaultCredential {
  /** The signed JWT to present to OpenViking. */
  token: string;
  viewer: Viewer;
  /** Unix seconds the token expires at, or null when it says nothing. */
  expiresAt: number | null;
}

function base(config: Config): string {
  if (!config.VAULT_ADDR) throw new VaultError("VAULT_ADDR is not set", 500);
  return config.VAULT_ADDR.replace(/\/$/, "");
}

function headers(config: Config, token?: string): Record<string, string> {
  const out: Record<string, string> = { accept: "application/json" };
  if (token) out["x-vault-token"] = token;
  if (config.VAULT_NAMESPACE) out["x-vault-namespace"] = config.VAULT_NAMESPACE;
  return out;
}

/**
 * Read Vault's own error text, which names the cause better than a status can.
 *
 * Vault answers a bad password with a 400 and `invalid username or password`;
 * passing that through turns "login failed" into something actionable. The
 * body is short and contains no credential.
 */
async function detail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { errors?: unknown };
    const errors = Array.isArray(body.errors) ? body.errors : [];
    const first = errors[0];
    return typeof first === "string" ? first : "";
  } catch {
    return "";
  }
}

/**
 * Exchange a username and password for a Vault session token.
 *
 * @param config - Supplies the Vault address and the userpass mount.
 * @param username - Vault username.
 * @param password - Their password. Never logged, never stored.
 * @returns The Vault client token.
 * @throws VaultError - When Vault refuses the login or cannot be reached.
 */
export async function login(
  config: Config,
  username: string,
  password: string,
): Promise<string> {
  if (!username || !password) {
    throw new VaultError("a username and password are required", 400);
  }

  const url = `${base(config)}/v1/auth/${config.VAULT_USERPASS_MOUNT}/login/${encodeURIComponent(username)}`;
  const response = await fetch(url, {
    method: "POST",
    headers: { ...headers(config), "content-type": "application/json" },
    body: JSON.stringify({ password }),
    // Vault forwards standby requests itself, so nothing here needs a redirect
    // — and following one would resend the password to wherever it pointed.
    redirect: "manual",
    signal: AbortSignal.timeout(config.OV_TIMEOUT_MS),
  }).catch((error: Error) => {
    throw new VaultError(`could not reach Vault: ${error.message}`);
  });

  if (response.status === 400 || response.status === 401 || response.status === 403) {
    const why = await detail(response);
    throw new VaultError(why || "Vault rejected that username or password", 401);
  }
  if (!response.ok) {
    throw new VaultError(`Vault returned HTTP ${response.status}`);
  }

  const parsed = loginSchema.safeParse(await response.json());
  if (!parsed.success) {
    throw new VaultError("Vault accepted the login but returned no token");
  }
  return parsed.data.auth.client_token;
}

/**
 * Mint an OpenViking identity token for whoever holds `vaultToken`.
 *
 * The role decides the audience, the claims and the lifetime, so this asks for
 * none of them. The identity in the result is read back out of the token
 * itself rather than assumed: it is what OpenViking will answer as, so it is
 * the only trustworthy source for who this session is.
 *
 * @param config - Supplies the Vault address and the identity-token role.
 * @param vaultToken - A live Vault session token.
 * @returns The token to present to OpenViking, plus the identity it carries.
 * @throws VaultError - When Vault refuses, or the token lacks the claims
 *   OpenViking maps identity from.
 */
export async function mint(config: Config, vaultToken: string): Promise<VaultCredential> {
  const role = config.VAULT_OIDC_ROLE;
  const url = `${base(config)}/v1/identity/oidc/token/${encodeURIComponent(role)}`;

  const response = await fetch(url, {
    headers: headers(config, vaultToken),
    redirect: "manual",
    signal: AbortSignal.timeout(config.OV_TIMEOUT_MS),
  }).catch((error: Error) => {
    throw new VaultError(`could not reach Vault: ${error.message}`);
  });

  if (response.status === 403 || response.status === 401) {
    throw new VaultError(
      `Vault would not mint from role ${role} for this user — check the role's allowed_client_ids and the user's policy`,
      403,
    );
  }
  if (!response.ok) {
    throw new VaultError(`minting from role ${role} returned HTTP ${response.status}`);
  }

  const parsed = mintSchema.safeParse(await response.json());
  if (!parsed.success) {
    throw new VaultError(`Vault returned no token for role ${role}`);
  }

  return credentialFrom(parsed.data.data.token);
}

/**
 * Read the identity out of a minted token.
 *
 * Exported so a token supplied by configuration can go through the same
 * checks as a freshly minted one.
 *
 * @param token - A Vault identity token.
 * @returns The credential and the viewer it speaks for.
 * @throws VaultError - When the token is unreadable or carries no identity.
 */
export function credentialFrom(token: string): VaultCredential {
  let raw: unknown;
  try {
    raw = decodeJwt(token);
  } catch (error) {
    throw new VaultError(`that token is not a readable JWT: ${(error as Error).message}`);
  }

  const parsed = claimsSchema.safeParse(raw);
  if (!parsed.success) {
    throw new VaultError(
      "that token carries no ov_account/ov_user claims, so OpenViking cannot resolve an identity from it — " +
        "the Vault role has to template those claims",
      403,
    );
  }

  const { ov_account: account, ov_user: user, exp } = parsed.data;
  // The same check every other identity path gets: this becomes a path segment
  // in viking://user/<id>, so it must be one segment.
  requireUserId(user, "ov_user");

  return {
    token,
    viewer: { sub: user, name: user, email: "", account, user },
    expiresAt: exp ?? null,
  };
}

/**
 * Sign in and mint in one step.
 *
 * @param config - Validated configuration.
 * @param username - Vault username.
 * @param password - Their password.
 * @returns The OpenViking credential and the identity it carries.
 */
export async function signIn(
  config: Config,
  username: string,
  password: string,
): Promise<VaultCredential> {
  const vaultToken = await login(config, username, password);
  return mint(config, vaultToken);
}
