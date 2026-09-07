import { describe, expect, it } from "vitest";
import { loadConfig, redirectUri } from "../src/server/env";

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
