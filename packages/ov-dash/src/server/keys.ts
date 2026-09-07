/**
 * Resolving a person's OpenViking API key, server-side.
 *
 * OpenViking derives identity from the key alone — there is no header that says
 * who you are. So the dashboard authenticates someone with OIDC, then looks up
 * the key that *is* that person and calls OpenViking with it. The key stays in
 * this process; the browser is never given one.
 *
 * Vault is the real source. The `env` and `static-map` sources exist so the UI
 * can be run against a single-user instance without standing Vault up first.
 */

import { z } from "zod";
import type { Config } from "./env";
import { requireUserId } from "./identity";

/** Raised when a key cannot be produced for a caller. */
export class KeyError extends Error {
  /**
   * @param message - What failed, safe to log. Never contains the key.
   * @param status - HTTP status the request should end with.
   */
  constructor(
    message: string,
    readonly status = 502,
  ) {
    super(message);
    this.name = "KeyError";
  }
}

const kvReadSchema = z.object({
  data: z.object({
    data: z.record(z.unknown()),
  }),
});

const loginSchema = z.object({
  auth: z.object({
    client_token: z.string(),
    lease_duration: z.number().optional(),
  }),
});

interface CacheEntry {
  key: string;
  expiresAt: number;
}

/**
 * Looks up API keys and remembers them briefly.
 *
 * The cache exists so a page that makes six calls does not make six Vault round
 * trips. It is deliberately short: revoking someone's key should take effect in
 * minutes without restarting the dashboard.
 */
export class KeyResolver {
  private readonly cache = new Map<string, CacheEntry>();
  private vaultToken: string | null = null;
  private staticMap: Record<string, string> | null = null;

  /**
   * @param config - Validated configuration naming the key source.
   * @param now - Clock, injectable so tests can expire the cache without
   *   waiting.
   */
  constructor(
    private readonly config: Config,
    private readonly now: () => number = Date.now,
  ) {
    if (config.KEY_SOURCE === "static-map" && config.OV_API_KEY_MAP) {
      // Shape was checked at boot by loadConfig, so this parse cannot surprise
      // us at request time.
      this.staticMap = JSON.parse(config.OV_API_KEY_MAP) as Record<string, string>;
    }
    if (config.VAULT_TOKEN) this.vaultToken = config.VAULT_TOKEN;
  }

  /**
   * Return the OpenViking API key for one user.
   *
   * @param user - The OpenViking user id, as mapped from the ID token.
   * @returns The key to send as `X-API-Key`.
   * @throws KeyError - When no key exists for that user, or Vault refuses.
   */
  async forUser(user: string): Promise<string> {
    // Checked here as well as at the point the identity is mapped. This is the
    // sink that matters: `user` becomes a path segment in the Vault URL, and
    // `fetch` normalizes `..` away before Vault ever sees it, so an unchecked
    // id reads whatever else the dashboard's own credentials can reach.
    requireUserId(user, "resolved user");

    const cached = this.cache.get(user);
    if (cached && cached.expiresAt > this.now()) return cached.key;

    const key = await this.lookup(user);
    if (this.config.KEY_CACHE_TTL_SECONDS > 0) {
      this.cache.set(user, {
        key,
        expiresAt: this.now() + this.config.KEY_CACHE_TTL_SECONDS * 1000,
      });
    }
    return key;
  }

  /** Forget a cached key, so the next call re-reads it. */
  forget(user: string): void {
    this.cache.delete(user);
  }

  private async lookup(user: string): Promise<string> {
    switch (this.config.KEY_SOURCE) {
      case "env": {
        const key = this.config.OV_API_KEY;
        if (!key) throw new KeyError("OV_API_KEY is not set", 500);
        return key;
      }
      case "static-map": {
        // hasOwn, not a plain index: `__proto__`, `constructor` and `toString`
        // all resolve on an object literal, and each would return something
        // truthy and non-string where a key belongs.
        const map = this.staticMap;
        const key = map && Object.hasOwn(map, user) ? map[user] : undefined;
        if (typeof key !== "string" || !key) {
          throw new KeyError(`no API key configured for user ${user}`, 403);
        }
        return key;
      }
      case "vault":
        return this.readFromVault(user);
    }
  }

  private async readFromVault(user: string): Promise<string> {
    const path = this.config.VAULT_KEY_PATH.replaceAll("{user}", user);
    const mount = this.config.VAULT_KV_MOUNT;
    const url = `${this.config.VAULT_ADDR?.replace(/\/$/, "")}/v1/${mount}/data/${path}`;

    let body = await this.vaultGet(url);
    if (body === "unauthenticated") {
      // The token expired or was revoked. One re-login, then give up, so a
      // permanently broken AppRole fails fast instead of looping.
      this.vaultToken = null;
      body = await this.vaultGet(url);
    }
    if (body === "unauthenticated") {
      throw new KeyError("Vault rejected the dashboard's own credentials", 502);
    }
    if (body === "not-found") {
      // The path is logged, not returned: it names the mount and the layout,
      // which the browser has no use for and an attacker does.
      console.warn(`no OpenViking key at ${mount}/${path}`);
      throw new KeyError(`user ${user} has no OpenViking key`, 403);
    }

    const parsed = kvReadSchema.safeParse(body);
    if (!parsed.success) {
      throw new KeyError(`Vault returned an unexpected body for ${mount}/${path}`);
    }
    const field = this.config.VAULT_KEY_FIELD;
    const value = parsed.data.data.data[field];
    if (typeof value !== "string" || !value) {
      throw new KeyError(`secret ${mount}/${path} has no string field "${field}"`);
    }
    return value;
  }

  private async vaultGet(
    url: string,
  ): Promise<unknown | "unauthenticated" | "not-found"> {
    const token = await this.token();
    const response = await fetch(url, {
      headers: this.vaultHeaders(token),
      signal: AbortSignal.timeout(this.config.OV_TIMEOUT_MS),
    }).catch((error: Error) => {
      throw new KeyError(`could not reach Vault: ${error.message}`);
    });

    if (response.status === 403 || response.status === 401) return "unauthenticated";
    if (response.status === 404) return "not-found";
    if (!response.ok) {
      throw new KeyError(`Vault returned HTTP ${response.status}`);
    }
    return response.json();
  }

  private vaultHeaders(token: string): Record<string, string> {
    const headers: Record<string, string> = {
      "x-vault-token": token,
      accept: "application/json",
    };
    if (this.config.VAULT_NAMESPACE) {
      headers["x-vault-namespace"] = this.config.VAULT_NAMESPACE;
    }
    return headers;
  }

  /** Return a live Vault token, logging in with AppRole when there is none. */
  private async token(): Promise<string> {
    if (this.vaultToken) return this.vaultToken;

    const { VAULT_ROLE_ID, VAULT_SECRET_ID, VAULT_APPROLE_MOUNT, VAULT_ADDR } =
      this.config;
    if (!VAULT_ROLE_ID || !VAULT_SECRET_ID) {
      throw new KeyError("no Vault token and no AppRole credentials to get one", 500);
    }

    const url = `${VAULT_ADDR?.replace(/\/$/, "")}/v1/auth/${VAULT_APPROLE_MOUNT}/login`;
    const response = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify({ role_id: VAULT_ROLE_ID, secret_id: VAULT_SECRET_ID }),
      signal: AbortSignal.timeout(this.config.OV_TIMEOUT_MS),
    }).catch((error: Error) => {
      throw new KeyError(`AppRole login could not reach Vault: ${error.message}`);
    });

    if (!response.ok) {
      throw new KeyError(`AppRole login failed: HTTP ${response.status}`);
    }
    const parsed = loginSchema.safeParse(await response.json());
    if (!parsed.success) {
      throw new KeyError("AppRole login returned no client_token");
    }
    this.vaultToken = parsed.data.auth.client_token;
    return this.vaultToken;
  }
}
