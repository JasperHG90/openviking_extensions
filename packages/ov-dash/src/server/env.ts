/**
 * Configuration, read from the environment once and checked before the server
 * listens.
 *
 * A bad value fails at boot with the offending key named, rather than on the
 * first request that happens to need it.
 */

import { z } from "zod";

const bool = z
  .enum(["true", "false", "1", "0"])
  .transform((v) => v === "true" || v === "1");

const secret = z
  .string()
  .min(32, "must be at least 32 characters — it signs session cookies");

/**
 * How the dashboard decides who is calling.
 *
 * `vault-userpass` is the one that works against a Vault-backed OpenViking:
 * the person types their Vault username and password into the dashboard, the
 * server logs them in and mints the identity token OpenViking accepts. It needs
 * no CLI, and no OIDC client registered anywhere.
 *
 * `oidc` runs the authorization-code flow itself. Useful when the provider
 * issues tokens OpenViking will accept — Vault's own provider does not, since
 * its ID tokens carry neither the audience nor the `ov_account`/`ov_user`
 * claims that OpenViking maps identity from. `trusted-header` reads the headers
 * an authenticating proxy already injected. `dev` fixes one identity so the UI
 * can be worked on without any of this.
 */
const authModeSchema = z.enum(["oidc", "vault-userpass", "trusted-header", "dev"]);
export type AuthMode = z.infer<typeof authModeSchema>;

/**
 * Which claim becomes the OpenViking user id.
 *
 * OpenViking derives identity from the API key alone, so the dashboard has to
 * map an OIDC claim onto the same `(account, user)` pair the key would give.
 * Vault's `sub` is an entity id and never matches, so the default takes the
 * local part of the email — `jasper@example.com` becomes `jasper`.
 */
const identityFromSchema = z.enum(["email-local", "email", "sub", "preferred_username"]);
export type IdentityFrom = z.infer<typeof identityFromSchema>;

/** Where a person's OpenViking API key comes from. */
const keySourceSchema = z.enum(["vault", "env", "static-map"]);
export type KeySource = z.infer<typeof keySourceSchema>;

const schema = z
  .object({
    NODE_ENV: z.enum(["development", "production", "test"]).default("development"),
    HOST: z.string().default("0.0.0.0"),
    PORT: z.coerce.number().int().positive().default(8080),
    /** Public origin the browser reaches, used to build redirect URIs. */
    PUBLIC_ORIGIN: z.string().url().default("http://localhost:8080"),

    // ── OpenViking ───────────────────────────────────────────────
    OV_URL: z.string().url(),
    /**
     * How long one OpenViking call may take, in milliseconds.
     *
     * Measured against a real instance rather than guessed: a recursive listing
     * of ~400 entries answers in under a second, but `search/grep` takes about
     * twenty for one term. A 30s budget therefore times out exact search while
     * everything else looks fine, so the default matches the SDK's own 60s.
     */
    OV_TIMEOUT_MS: z.coerce.number().int().positive().default(60_000),
    /**
     * Scope the dashboard shows, with `{user}` and `{account}` substituted.
     *
     * The default is the canonical per-user root. The uid-less spelling
     * `viking://user` is not a shorter way to say the same thing — OpenViking
     * rejects it outright and points at the `viking://~/...` home alias — so a
     * root without the id silently breaks memories and semantic search.
     */
    OV_ROOT: z.string().default("viking://user/{user}"),
    /**
     * Scope shared with everyone in the account.
     *
     * Writes can be aimed here instead of at the person's own tree. Set it
     * empty to offer no shared target at all, which is the right setting once
     * an account holds one person and `viking://resources` is a tree of one.
     */
    OV_SHARED_ROOT: z.string().default("viking://resources"),

    // ── who is calling ───────────────────────────────────────────
    AUTH_MODE: authModeSchema.default("oidc"),
    IDENTITY_FROM: identityFromSchema.default("email-local"),
    /** Account every user resolves into. Post-OV2 this is the person's name. */
    OV_ACCOUNT: z.string().default(""),

    // ── OIDC, when AUTH_MODE=oidc ────────────────────────────────
    /** Issuer URL; discovery appends /.well-known/openid-configuration. */
    OIDC_ISSUER: z.string().url().optional(),
    OIDC_CLIENT_ID: z.string().optional(),
    OIDC_CLIENT_SECRET: z.string().optional(),
    OIDC_SCOPES: z.string().default("openid email groups"),
    /** Overrides the redirect URI derived from PUBLIC_ORIGIN. */
    OIDC_REDIRECT_URI: z.string().url().optional(),
    /** Skip TLS verification when talking to the provider. Labs only. */
    OIDC_INSECURE_TLS: bool.default("false"),
    /**
     * Require `email_verified` before mapping an identity from an address.
     *
     * On by default. Turning it off on a provider where people can set their
     * own unverified email means anyone can claim another person's local part,
     * and with it that person's OpenViking key.
     */
    OIDC_REQUIRE_EMAIL_VERIFIED: bool.default("true"),
    /**
     * Comma-separated email domains allowed to sign in. Empty allows any.
     *
     * Worth setting whenever `IDENTITY_FROM=email-local`, which discards the
     * domain: without it, the same local part at two domains is one
     * OpenViking user sharing one tree.
     */
    OIDC_ALLOWED_EMAIL_DOMAINS: z.string().default(""),

    // ── trusted-header mode ──────────────────────────────────────
    TRUSTED_USER_HEADER: z.string().default("x-forwarded-user"),
    TRUSTED_EMAIL_HEADER: z.string().default("x-forwarded-email"),

    // ── dev mode ─────────────────────────────────────────────────
    DEV_USER: z.string().default("dev"),
    DEV_EMAIL: z.string().default("dev@localhost"),

    // ── sessions ─────────────────────────────────────────────────
    SESSION_SECRET: secret,
    /**
     * How long a signed-in browser stays signed in.
     *
     * Deliberately independent of the ID token's lifetime. Vault's provider
     * advertises no refresh grant, so its token dies in about an hour; the
     * dashboard only needs the token to learn who someone is, and calls
     * OpenViking with the server-held API key afterwards. Tying the session to
     * the token would sign people out hourly for no gain in safety.
     */
    SESSION_TTL_SECONDS: z.coerce.number().int().positive().default(28_800),
    SESSION_COOKIE_NAME: z.string().default("ovdash_session"),
    /** Set false only when serving plain HTTP on localhost. */
    SESSION_COOKIE_SECURE: bool.default("true"),

    // ── API keys ─────────────────────────────────────────────────
    KEY_SOURCE: keySourceSchema.default("vault"),
    /** KEY_SOURCE=env: one key for every caller. Single-user or dev only. */
    OV_API_KEY: z.string().optional(),
    /** KEY_SOURCE=static-map: JSON object of {"user": "key"}. */
    OV_API_KEY_MAP: z.string().optional(),
    /** Seconds to keep a resolved key in memory before re-reading Vault. */
    KEY_CACHE_TTL_SECONDS: z.coerce.number().int().nonnegative().default(300),

    // ── Vault, when KEY_SOURCE=vault ─────────────────────────────
    VAULT_ADDR: z.string().url().optional(),
    VAULT_NAMESPACE: z.string().optional(),
    /** KV v2 mount holding the per-user keys. */
    VAULT_KV_MOUNT: z.string().default("secret"),
    /** Path under the mount. `{user}` is replaced with the OpenViking user. */
    VAULT_KEY_PATH: z.string().default("openviking-users/{user}"),
    /** Field inside that secret carrying the key. */
    VAULT_KEY_FIELD: z.string().default("api_key"),
    /** A ready-made token. Otherwise AppRole below is used. */
    VAULT_TOKEN: z.string().optional(),
    VAULT_ROLE_ID: z.string().optional(),
    VAULT_SECRET_ID: z.string().optional(),
    VAULT_APPROLE_MOUNT: z.string().default("approle"),
    /** Mount of the userpass auth method people sign in against. */
    VAULT_USERPASS_MOUNT: z.string().default("userpass"),
    /**
     * Identity-token role minted from after a sign-in.
     *
     * The role decides the audience, the claims and the lifetime. It must
     * template `ov_account` and `ov_user`, because that is where OpenViking
     * reads identity from.
     */
    VAULT_OIDC_ROLE: z.string().default("openviking"),
    VAULT_SKIP_VERIFY: bool.default("false"),
  })
  .superRefine((cfg, ctx) => {
    const require = (key: keyof typeof cfg, why: string) => {
      if (!cfg[key]) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: [key],
          message: `${key} is required ${why}`,
        });
      }
    };

    if (cfg.AUTH_MODE === "vault-userpass") {
      require("VAULT_ADDR", "when AUTH_MODE=vault-userpass");
    }

    if (cfg.AUTH_MODE === "oidc") {
      require("OIDC_ISSUER", "when AUTH_MODE=oidc");
      require("OIDC_CLIENT_ID", "when AUTH_MODE=oidc");
      require("OIDC_CLIENT_SECRET", "when AUTH_MODE=oidc");
    }

    // The credential comes from the sign-in itself, so none of the key sources
    // apply and requiring one would ask for a secret nothing reads.
    if (cfg.AUTH_MODE === "vault-userpass") return;

    if (cfg.KEY_SOURCE === "vault") {
      require("VAULT_ADDR", "when KEY_SOURCE=vault");
      if (!cfg.VAULT_TOKEN && !(cfg.VAULT_ROLE_ID && cfg.VAULT_SECRET_ID)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["VAULT_TOKEN"],
          message:
            "KEY_SOURCE=vault needs either VAULT_TOKEN or both VAULT_ROLE_ID and VAULT_SECRET_ID",
        });
      }
    }

    if (cfg.KEY_SOURCE === "env") require("OV_API_KEY", "when KEY_SOURCE=env");

    if (cfg.KEY_SOURCE === "static-map") {
      require("OV_API_KEY_MAP", "when KEY_SOURCE=static-map");
      if (cfg.OV_API_KEY_MAP) {
        try {
          const parsed: unknown = JSON.parse(cfg.OV_API_KEY_MAP);
          if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
            throw new Error("not an object");
          }
        } catch {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            path: ["OV_API_KEY_MAP"],
            message: 'OV_API_KEY_MAP must be a JSON object, e.g. {"jasper":"ov_..."}',
          });
        }
      }
    }
  });

export type Config = z.infer<typeof schema>;

/**
 * Parse and check an environment.
 *
 * @param source - Variables to read. Defaults to the real environment.
 * @returns The validated configuration.
 * @throws When a required value is missing or malformed, with every offending
 *   key listed in the message.
 */
export function loadConfig(source: NodeJS.ProcessEnv = process.env): Config {
  const result = schema.safeParse(source);
  if (!result.success) {
    const lines = result.error.issues.map(
      (issue) => `  ${issue.path.join(".") || "(root)"}: ${issue.message}`,
    );
    throw new Error(`Invalid configuration:\n${lines.join("\n")}`);
  }
  return result.data;
}

/** The redirect URI sent to the provider, derived when not set explicitly. */
export function redirectUri(config: Config): string {
  return (
    config.OIDC_REDIRECT_URI ?? new URL("/auth/callback", config.PUBLIC_ORIGIN).toString()
  );
}
