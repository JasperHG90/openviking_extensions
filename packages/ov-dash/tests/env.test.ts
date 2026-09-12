import { describe, expect, it } from "vitest";
import { loadConfig, redirectUri, scopes } from "../src/server/env";

const BASE = {
  OV_URL: "http://openviking:1933",
  SESSION_SECRET: "a".repeat(32),
  AUTH_MODE: "dev",
  KEY_SOURCE: "env",
  OV_API_KEY: "ov_test",
};

describe("loadConfig", () => {
  it("accepts a minimal dev configuration", () => {
    const config = loadConfig({ ...BASE });
    expect(config.PORT).toBe(8080);
    // The canonical per-user root. The uid-less "viking://user" is rejected
    // by OpenViking outright, so it is not a valid default.
    expect(config.OV_ROOT).toBe("viking://user/{user}");
  });

  it("rejects a session secret short enough to brute force", () => {
    expect(() => loadConfig({ ...BASE, SESSION_SECRET: "short" })).toThrow(
      /SESSION_SECRET/,
    );
  });

  it("requires the OIDC client when the dance is turned on", () => {
    expect(() =>
      loadConfig({ ...BASE, AUTH_MODE: "oidc", OIDC_ISSUER: "https://vault.example" }),
    ).toThrow(/OIDC_CLIENT_ID/);
  });

  it("accepts a complete OIDC configuration", () => {
    const config = loadConfig({
      ...BASE,
      AUTH_MODE: "oidc",
      OIDC_ISSUER: "https://vault.example/v1/identity/oidc/provider/default",
      OIDC_CLIENT_ID: "ov-dash",
      OIDC_CLIENT_SECRET: "secret",
    });
    expect(config.AUTH_MODE).toBe("oidc");
  });

  it("requires a way to authenticate to Vault when Vault holds the keys", () => {
    expect(() =>
      loadConfig({ ...BASE, KEY_SOURCE: "vault", VAULT_ADDR: "https://vault.example" }),
    ).toThrow(/VAULT_TOKEN or both VAULT_ROLE_ID/);
  });

  it("accepts AppRole credentials as that way", () => {
    const config = loadConfig({
      ...BASE,
      KEY_SOURCE: "vault",
      VAULT_ADDR: "https://vault.example",
      VAULT_ROLE_ID: "role",
      VAULT_SECRET_ID: "secret",
    });
    expect(config.KEY_SOURCE).toBe("vault");
  });

  it("rejects a key map that is not a JSON object", () => {
    expect(() =>
      loadConfig({ ...BASE, KEY_SOURCE: "static-map", OV_API_KEY_MAP: "[1,2]" }),
    ).toThrow(/JSON object/);
  });

  it("derives the redirect URI from the public origin", () => {
    const config = loadConfig({ ...BASE, PUBLIC_ORIGIN: "https://dash.example" });
    expect(redirectUri(config)).toBe("https://dash.example/auth/callback");
  });

  it("lets an explicit redirect URI win", () => {
    const config = loadConfig({
      ...BASE,
      PUBLIC_ORIGIN: "https://dash.example",
      OIDC_REDIRECT_URI: "https://other.example/cb",
    });
    expect(redirectUri(config)).toBe("https://other.example/cb");
  });
});

describe("signing in through Vault's own login", () => {
  const VAULT_OIDC = {
    ...BASE,
    AUTH_MODE: "vault-oidc",
    OIDC_ISSUER: "https://vault.example/v1/identity/oidc/provider/ovdash",
    OIDC_CLIENT_ID: "ov-dash",
    OIDC_CLIENT_SECRET: "secret",
    VAULT_ADDR: "https://vault.example",
  };

  it("needs a registered client, like any other redirect", () => {
    expect(() => loadConfig({ ...VAULT_OIDC, OIDC_CLIENT_SECRET: "" })).toThrow(
      /OIDC_CLIENT_SECRET is required when AUTH_MODE=vault-oidc/,
    );
  });

  it("asks for no key source, because the sign-in produces the credential", () => {
    const config = loadConfig({
      OV_URL: "http://openviking:1933",
      SESSION_SECRET: "a".repeat(32),
      AUTH_MODE: "vault-oidc",
      OIDC_ISSUER: "https://vault.example/v1/identity/oidc/provider/ovdash",
      OIDC_CLIENT_ID: "ov-dash",
      OIDC_CLIENT_SECRET: "secret",
      VAULT_ADDR: "https://vault.example",
    });
    expect(config.AUTH_MODE).toBe("vault-oidc");
    // Defaults, so a deployment only names them when Vault calls them
    // something else.
    expect(config.VAULT_JWT_MOUNT).toBe("jwt");
    expect(config.VAULT_JWT_ROLE).toBe("ov-dash");
  });

  it("needs Vault's address, because the ID token has to be traded there", () => {
    // The redirect ends with a token OpenViking refuses -- wrong issuer, wrong
    // audience. Without Vault there is nothing to trade it at, and the failure
    // would land on the first person to sign in rather than at boot.
    const { VAULT_ADDR, ...without } = VAULT_OIDC;
    expect(() => loadConfig(without)).toThrow(
      /VAULT_ADDR is required when AUTH_MODE=vault-oidc/,
    );
  });

  it("asks Vault for the scope carrying ov_account and ov_user", () => {
    // Vault advertises neither `email` nor `groups`, and asking for one is an
    // invalid_scope refusal — so the default cannot be shared with `oidc`.
    expect(scopes(loadConfig(VAULT_OIDC))).toBe("openid openviking");
    expect(scopes(loadConfig({ ...BASE, AUTH_MODE: "dev" }))).toBe("openid email groups");
  });

  it("lets a differently named scope be set", () => {
    expect(scopes(loadConfig({ ...VAULT_OIDC, OIDC_SCOPES: "openid ov" }))).toBe(
      "openid ov",
    );
  });
});
