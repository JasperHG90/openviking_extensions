/**
 * The OpenID Connect authorization-code flow, with PKCE.
 *
 * The dashboard is its own relying party: it sends the browser to the provider,
 * receives a code, swaps that code for an ID token, and verifies the token's
 * signature against the provider's published keys. Nothing upstream of the
 * dashboard has to be an authenticating proxy for this to work.
 *
 * Two things about Vault's provider shape this module. It advertises only the
 * authorization-code grant — no device flow, no client credentials, and no
 * refresh — so the ID token is used once, at login, to learn who someone is and
 * is then discarded. And its `sub` is an entity id rather than a username, so
 * the identity a person maps to comes from a configurable claim (see
 * {@link Config.IDENTITY_FROM}), not from `sub`.
 */

import { createHash, randomBytes } from "node:crypto";
import { createRemoteJWKSet, jwtVerify } from "jose";
import { z } from "zod";
import type { Viewer } from "../shared/schemas";
import type { Config } from "./env";
import { redirectUri } from "./env";
import { requireUserId } from "./identity";

/** The parts of a discovery document this flow needs. */
const discoverySchema = z.object({
  issuer: z.string(),
  authorization_endpoint: z.string().url(),
  token_endpoint: z.string().url(),
  jwks_uri: z.string().url(),
  token_endpoint_auth_methods_supported: z.array(z.string()).optional(),
  grant_types_supported: z.array(z.string()).optional(),
  code_challenge_methods_supported: z.array(z.string()).optional(),
});
export type Discovery = z.infer<typeof discoverySchema>;

const tokenResponseSchema = z.object({
  access_token: z.string().optional(),
  id_token: z.string(),
  token_type: z.string().optional(),
  expires_in: z.number().optional(),
});

/** Raised when the provider or the browser gives us something unusable. */
export class OidcError extends Error {
  /**
   * @param message - What went wrong, safe to log.
   * @param status - HTTP status to answer the browser with.
   */
  constructor(
    message: string,
    readonly status = 502,
  ) {
    super(message);
    this.name = "OidcError";
  }
}

/**
 * A provider whose discovery document has been fetched and whose signing keys
 * are cached.
 */
export class OidcClient {
  private constructor(
    private readonly config: Config,
    readonly discovery: Discovery,
    private readonly jwks: ReturnType<typeof createRemoteJWKSet>,
  ) {}

  /**
   * Fetch the provider's discovery document and prepare its key set.
   *
   * @param config - Validated configuration; `OIDC_ISSUER` must be set.
   * @returns A ready client.
   * @throws OidcError - When discovery cannot be fetched or parsed, or the
   *   provider does not support the authorization-code grant.
   */
  static async discover(config: Config): Promise<OidcClient> {
    if (!config.OIDC_ISSUER) {
      throw new OidcError("OIDC_ISSUER is not set", 500);
    }
    const url = new URL(
      ".well-known/openid-configuration",
      `${config.OIDC_ISSUER.replace(/\/$/, "")}/`,
    );

    let body: unknown;
    try {
      const response = await fetch(url, {
        headers: { accept: "application/json" },
        signal: AbortSignal.timeout(config.OV_TIMEOUT_MS),
      });
      if (!response.ok) {
        throw new OidcError(`discovery failed: ${url} returned HTTP ${response.status}`);
      }
      body = await response.json();
    } catch (error) {
      if (error instanceof OidcError) throw error;
      throw new OidcError(`discovery failed: ${(error as Error).message}`);
    }

    const parsed = discoverySchema.safeParse(body);
    if (!parsed.success) {
      throw new OidcError(
        `discovery document from ${url} is missing required fields: ${parsed.error.issues
          .map((i) => i.path.join("."))
          .join(", ")}`,
      );
    }

    // OpenID Connect Discovery requires the document's `issuer` to equal the
    // issuer we asked about. Skipping this lets whatever answered the fetch
    // name its own issuer and its own JWKS, and every later check then
    // validates against values it chose — including the email claim that
    // decides which Vault key this login receives.
    const declared = parsed.data.issuer.replace(/\/$/, "");
    const configured = config.OIDC_ISSUER.replace(/\/$/, "");
    if (declared !== configured) {
      throw new OidcError(
        `discovery document declares issuer ${declared}, but OIDC_ISSUER is ${configured}`,
      );
    }

    const grants = parsed.data.grant_types_supported;
    if (grants && !grants.includes("authorization_code")) {
      throw new OidcError(
        `provider does not advertise the authorization_code grant (it offers: ${grants.join(", ")})`,
      );
    }

    return new OidcClient(
      config,
      parsed.data,
      createRemoteJWKSet(new URL(parsed.data.jwks_uri)),
    );
  }

  /**
   * Build the URL to send the browser to, and the state to remember.
   *
   * @returns The authorize URL and the PKCE/state values to stash in a cookie.
   *   The caller is responsible for stashing them; this method holds no state,
   *   so two logins in two tabs cannot overwrite each other's verifier.
   */
  authorize(): {
    url: string;
    state: string;
    nonce: string;
    verifier: string;
  } {
    const state = randomBytes(32).toString("base64url");
    const nonce = randomBytes(32).toString("base64url");
    const verifier = randomBytes(64).toString("base64url");
    const challenge = createHash("sha256").update(verifier).digest("base64url");

    const url = new URL(this.discovery.authorization_endpoint);
    url.searchParams.set("response_type", "code");
    url.searchParams.set("client_id", this.config.OIDC_CLIENT_ID ?? "");
    url.searchParams.set("redirect_uri", redirectUri(this.config));
    url.searchParams.set("scope", this.config.OIDC_SCOPES);
    url.searchParams.set("state", state);
    url.searchParams.set("nonce", nonce);
    url.searchParams.set("code_challenge", challenge);
    url.searchParams.set("code_challenge_method", "S256");

    return { url: url.toString(), state, nonce, verifier };
  }

  /**
   * Swap an authorization code for a verified identity.
   *
   * @param code - The `code` parameter the provider sent back.
   * @param verifier - The PKCE verifier stashed when the flow began.
   * @param nonce - The nonce stashed when the flow began; the ID token must
   *   carry it back, which is what stops a token minted for another login from
   *   being replayed into this one.
   * @returns The claims, mapped onto a viewer.
   * @throws OidcError - When the exchange fails or the token does not verify.
   */
  async exchange(code: string, verifier: string, nonce: string): Promise<Viewer> {
    const body = new URLSearchParams({
      grant_type: "authorization_code",
      code,
      redirect_uri: redirectUri(this.config),
      code_verifier: verifier,
    });

    const headers: Record<string, string> = {
      "content-type": "application/x-www-form-urlencoded",
      accept: "application/json",
    };

    const clientId = this.config.OIDC_CLIENT_ID ?? "";
    const clientSecret = this.config.OIDC_CLIENT_SECRET ?? "";
    const methods = this.discovery.token_endpoint_auth_methods_supported;
    const useBasic = !methods || methods.includes("client_secret_basic");
    if (useBasic) {
      const credential = Buffer.from(
        `${encodeURIComponent(clientId)}:${encodeURIComponent(clientSecret)}`,
      ).toString("base64");
      headers.authorization = `Basic ${credential}`;
    } else {
      body.set("client_id", clientId);
      body.set("client_secret", clientSecret);
    }

    let payload: unknown;
    try {
      const response = await fetch(this.discovery.token_endpoint, {
        method: "POST",
        headers,
        body,
        signal: AbortSignal.timeout(this.config.OV_TIMEOUT_MS),
      });
      const text = await response.text();
      if (!response.ok) {
        // The provider's error body is short and names the cause; passing it
        // through turns "login broken" into "invalid_grant: code expired".
        throw new OidcError(
          `token exchange failed: HTTP ${response.status} ${text.slice(0, 300)}`,
        );
      }
      payload = JSON.parse(text);
    } catch (error) {
      if (error instanceof OidcError) throw error;
      throw new OidcError(`token exchange failed: ${(error as Error).message}`);
    }

    const parsed = tokenResponseSchema.safeParse(payload);
    if (!parsed.success) {
      throw new OidcError("token response carried no id_token");
    }

    const { payload: claims } = await jwtVerify(parsed.data.id_token, this.jwks, {
      issuer: this.discovery.issuer,
      audience: clientId,
    }).catch((error: Error) => {
      throw new OidcError(`id_token did not verify: ${error.message}`, 401);
    });

    if (claims.nonce !== nonce) {
      throw new OidcError("id_token nonce did not match this login", 401);
    }

    return viewerFromClaims(this.config, claims);
  }
}

/**
 * Map verified ID-token claims onto the identity OpenViking will answer as.
 *
 * Everything here decides which Vault key the caller is handed, so each step
 * refuses rather than guesses. Three checks, in order:
 *
 * 1. An email-derived identity must come from a verified address. Without this,
 *    any provider that lets someone set an unverified email lets them claim
 *    another person's local part — and their key.
 * 2. The address must be in `OIDC_ALLOWED_EMAIL_DOMAINS`, when that is set.
 *    `email-local` throws the domain away, so without an allow-list
 *    `jasper@corp.example` and `jasper@contractor.example` are one OpenViking
 *    user sharing one tree.
 * 3. The result must be a safe single path segment, because it becomes one in
 *    both the Vault path and the `viking://user/<id>` prefix.
 *
 * @param config - Supplies `IDENTITY_FROM` and the account override.
 * @param claims - Verified claims from the ID token.
 * @returns The viewer to put in the session cookie.
 * @throws OidcError - When the configured claim is absent or fails a check,
 *   since guessing an identity here would point someone at another person's
 *   tree.
 * @throws IdentityError - When the claim cannot be a safe path segment.
 */
export function viewerFromClaims(
  config: Config,
  claims: Record<string, unknown>,
): Viewer {
  const str = (value: unknown): string => (typeof value === "string" ? value.trim() : "");

  const email = str(claims.email);
  const sub = str(claims.sub);
  const preferred = str(claims.preferred_username);
  const fromEmail =
    config.IDENTITY_FROM === "email-local" || config.IDENTITY_FROM === "email";

  if (fromEmail && config.OIDC_REQUIRE_EMAIL_VERIFIED && claims.email_verified !== true) {
    throw new OidcError(
      "this login maps to an OpenViking user through its email address, but the " +
        "provider did not mark that address verified",
      403,
    );
  }

  if (fromEmail && config.OIDC_ALLOWED_EMAIL_DOMAINS) {
    const allowed = config.OIDC_ALLOWED_EMAIL_DOMAINS.split(",")
      .map((domain) => domain.trim().toLowerCase())
      .filter(Boolean);
    const domain = email.split("@")[1]?.toLowerCase() ?? "";
    if (!allowed.includes(domain)) {
      throw new OidcError(
        "this login's email domain is not one this dashboard accepts",
        403,
      );
    }
  }

  let user: string;
  switch (config.IDENTITY_FROM) {
    case "email-local":
      user = email.split("@")[0] ?? "";
      break;
    case "email":
      user = email;
      break;
    case "sub":
      user = sub;
      break;
    case "preferred_username":
      user = preferred;
      break;
  }

  if (!user) {
    throw new OidcError(
      `the ID token carries no usable ${config.IDENTITY_FROM} claim, so this login cannot be mapped to an OpenViking user`,
      403,
    );
  }

  requireUserId(user, config.IDENTITY_FROM);

  return {
    sub: sub || user,
    name: str(claims.name) || preferred || email.split("@")[0] || user,
    email,
    account: config.OV_ACCOUNT || user,
    user,
  };
}
