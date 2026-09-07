import { describe, expect, it, vi } from "vitest";
import { loadConfig } from "../src/server/env";
import { IdentityError, isSafeUserId, requireUserId } from "../src/server/identity";
import { KeyResolver } from "../src/server/keys";
import { viewerFromClaims } from "../src/server/oidc";

describe("what may become a user id", () => {
  it("accepts ordinary names", () => {
    for (const ok of ["jasper", "ada-lovelace", "web_visitor.1", "a", "A9"]) {
      expect(isSafeUserId(ok)).toBe(true);
    }
  });

  it("rejects anything that could move up or across a path", () => {
    for (const bad of [
      "..",
      ".",
      "../../ops/admin",
      "a/b",
      "a\\b",
      ".hidden",
      "a:b",
      "a+b",
      "a%2fb",
      "",
      " jasper",
      "a".repeat(65),
    ]) {
      expect(isSafeUserId(bad), bad).toBe(false);
    }
  });

  it("names where a bad id came from without echoing it", () => {
    try {
      requireUserId("../../ops/admin", "email-local");
      expect.unreachable("should have thrown");
    } catch (error) {
      expect(error).toBeInstanceOf(IdentityError);
      expect((error as Error).message).toContain("email-local");
      expect((error as Error).message).not.toContain("../../ops/admin");
    }
  });
});

describe("the Vault path a hostile id would have reached", () => {
  function vaultConfig() {
    return loadConfig({
      OV_URL: "http://openviking:1933",
      SESSION_SECRET: "a".repeat(32),
      AUTH_MODE: "dev",
      KEY_SOURCE: "vault",
      VAULT_ADDR: "https://vault.example",
      VAULT_TOKEN: "s.token",
    });
  }

  it("never issues the request at all", async () => {
    const fetchMock = vi.fn(
      async () => new Response(JSON.stringify({ data: { data: { api_key: "ov_x" } } })),
    );
    vi.stubGlobal("fetch", fetchMock);

    const keys = new KeyResolver(vaultConfig());
    await expect(keys.forUser("../../ops/admin")).rejects.toBeInstanceOf(IdentityError);
    expect(fetchMock).not.toHaveBeenCalled();

    vi.unstubAllGlobals();
  });

  it("would have escaped the configured prefix without the check", () => {
    // Why the check exists: WHATWG URL parsing collapses `..` before Vault
    // ever sees the path, so the `openviking-users/` prefix is not a boundary.
    const escaped = new URL(
      "https://vault.example/v1/secret/data/openviking-users/../../ops/admin",
    );
    expect(escaped.pathname).toBe("/v1/secret/ops/admin");
  });
});

describe("prototype names are not keys", () => {
  function mapResolver() {
    return new KeyResolver(
      loadConfig({
        OV_URL: "http://o:1933",
        SESSION_SECRET: "a".repeat(32),
        AUTH_MODE: "dev",
        KEY_SOURCE: "static-map",
        OV_API_KEY_MAP: JSON.stringify({ jasper: "ov_jasper" }),
      }),
    );
  }

  it("refuses __proto__ at the id check, before any lookup", async () => {
    // A leading underscore is not a legal id, so this never reaches the map.
    await expect(mapResolver().forUser("__proto__")).rejects.toBeInstanceOf(
      IdentityError,
    );
  });

  it("refuses names that ARE legal ids but only exist on the prototype", async () => {
    // These pass the id check, so the map lookup is what has to refuse them.
    // A plain index would return a function here and send it as an API key.
    const keys = mapResolver();
    for (const name of ["constructor", "toString", "valueOf", "hasOwnProperty"]) {
      await expect(keys.forUser(name), name).rejects.toThrow(/no API key configured/);
    }
  });

  it("still finds a real entry", async () => {
    await expect(mapResolver().forUser("jasper")).resolves.toBe("ov_jasper");
  });
});

describe("mapping an identity from an email", () => {
  function emailConfig(extra: Record<string, string> = {}) {
    return loadConfig({
      OV_URL: "http://o:1933",
      SESSION_SECRET: "a".repeat(32),
      AUTH_MODE: "oidc",
      OIDC_ISSUER: "https://vault.example",
      OIDC_CLIENT_ID: "id",
      OIDC_CLIENT_SECRET: "secret",
      KEY_SOURCE: "env",
      OV_API_KEY: "k",
      ...extra,
    });
  }

  it("refuses an unverified address", () => {
    expect(() =>
      viewerFromClaims(emailConfig(), { sub: "s", email: "jasper@x.com" }),
    ).toThrow(/did not mark that address verified/);
  });

  it("accepts a verified one", () => {
    const viewer = viewerFromClaims(emailConfig(), {
      sub: "s",
      email: "jasper@x.com",
      email_verified: true,
    });
    expect(viewer.user).toBe("jasper");
  });

  it("can be told to stop checking, for a provider that omits the claim", () => {
    const viewer = viewerFromClaims(
      emailConfig({ OIDC_REQUIRE_EMAIL_VERIFIED: "false" }),
      { sub: "s", email: "jasper@x.com" },
    );
    expect(viewer.user).toBe("jasper");
  });

  it("keeps two domains from collapsing onto one OpenViking user", () => {
    const config = emailConfig({ OIDC_ALLOWED_EMAIL_DOMAINS: "corp.example" });

    const inside = viewerFromClaims(config, {
      sub: "s",
      email: "jasper@corp.example",
      email_verified: true,
    });
    expect(inside.user).toBe("jasper");

    // Same local part, different company. Without the allow-list this returns
    // user "jasper" too, and gets the other Jasper's key and files.
    expect(() =>
      viewerFromClaims(config, {
        sub: "s2",
        email: "jasper@contractor.example",
        email_verified: true,
      }),
    ).toThrow(/domain is not one this dashboard accepts/);
  });

  it("refuses a claim that is not a safe path segment", () => {
    expect(() =>
      viewerFromClaims(emailConfig({ IDENTITY_FROM: "email" }), {
        sub: "s",
        email: "jasper@x.com",
        email_verified: true,
      }),
    ).toThrow(IdentityError);
  });

  it("does not check the email rules when mapping from sub", () => {
    const viewer = viewerFromClaims(emailConfig({ IDENTITY_FROM: "sub" }), {
      sub: "entity-abc",
      email: "unverified@x.com",
    });
    expect(viewer.user).toBe("entity-abc");
  });
});
