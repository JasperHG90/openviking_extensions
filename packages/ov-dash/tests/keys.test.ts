import { describe, expect, it, vi } from "vitest";
import { loadConfig } from "../src/server/env";
import { KeyError, KeyResolver } from "../src/server/keys";

function vaultConfig(extra: Record<string, string> = {}) {
  return loadConfig({
    OV_URL: "http://openviking:1933",
    SESSION_SECRET: "a".repeat(32),
    AUTH_MODE: "dev",
    KEY_SOURCE: "vault",
    VAULT_ADDR: "https://vault.example",
    VAULT_TOKEN: "s.roottoken",
    ...extra,
  });
}

describe("static sources", () => {
  it("gives every caller the same key in env mode", async () => {
    const config = loadConfig({
      OV_URL: "http://o:1933",
      SESSION_SECRET: "a".repeat(32),
      AUTH_MODE: "dev",
      KEY_SOURCE: "env",
      OV_API_KEY: "ov_single",
    });
    const keys = new KeyResolver(config);
    expect(await keys.forUser("anyone")).toBe("ov_single");
  });

  it("gives each caller their own key from a map", async () => {
    const config = loadConfig({
      OV_URL: "http://o:1933",
      SESSION_SECRET: "a".repeat(32),
      AUTH_MODE: "dev",
      KEY_SOURCE: "static-map",
      OV_API_KEY_MAP: JSON.stringify({ jasper: "ov_jasper", ada: "ov_ada" }),
    });
    const keys = new KeyResolver(config);
    expect(await keys.forUser("jasper")).toBe("ov_jasper");
    expect(await keys.forUser("ada")).toBe("ov_ada");
  });

  it("refuses a caller with no key rather than falling back to another", async () => {
    const config = loadConfig({
      OV_URL: "http://o:1933",
      SESSION_SECRET: "a".repeat(32),
      AUTH_MODE: "dev",
      KEY_SOURCE: "static-map",
      OV_API_KEY_MAP: JSON.stringify({ jasper: "ov_jasper" }),
    });
    const keys = new KeyResolver(config);
    await expect(keys.forUser("mallory")).rejects.toThrow(KeyError);
    await expect(keys.forUser("mallory")).rejects.toThrow(/no API key configured/);
  });
});

describe("vault source", () => {
  it("reads the key from the path built for that user", async () => {
    const seen: string[] = [];
    const fetchMock = vi.fn(async (input: string | URL) => {
      seen.push(String(input));
      return new Response(JSON.stringify({ data: { data: { api_key: "ov_jasper" } } }));
    });
    vi.stubGlobal("fetch", fetchMock);

    const keys = new KeyResolver(vaultConfig());
    expect(await keys.forUser("jasper")).toBe("ov_jasper");
    expect(seen[0]).toBe("https://vault.example/v1/secret/data/openviking-users/jasper");

    vi.unstubAllGlobals();
  });

  it("asks for a different path for a different user", async () => {
    const seen: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL) => {
        seen.push(String(input));
        const user = String(input).split("/").pop();
        return new Response(
          JSON.stringify({ data: { data: { api_key: `ov_${user}` } } }),
        );
      }),
    );

    const keys = new KeyResolver(vaultConfig());
    expect(await keys.forUser("jasper")).toBe("ov_jasper");
    expect(await keys.forUser("ada")).toBe("ov_ada");
    expect(seen).toHaveLength(2);
    expect(seen[1]).toContain("/openviking-users/ada");

    vi.unstubAllGlobals();
  });

  it("caches within the TTL and re-reads after it", async () => {
    const fetchMock = vi.fn(
      async () => new Response(JSON.stringify({ data: { data: { api_key: "ov_x" } } })),
    );
    vi.stubGlobal("fetch", fetchMock);

    let now = 1_000_000;
    const keys = new KeyResolver(vaultConfig({ KEY_CACHE_TTL_SECONDS: "60" }), () => now);

    await keys.forUser("jasper");
    await keys.forUser("jasper");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    now += 61_000;
    await keys.forUser("jasper");
    expect(fetchMock).toHaveBeenCalledTimes(2);

    vi.unstubAllGlobals();
  });

  it("treats a missing secret as a refusal for that person", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("", { status: 404 })),
    );
    const keys = new KeyResolver(vaultConfig());
    await expect(keys.forUser("nobody")).rejects.toThrow(/has no OpenViking key/);
    vi.unstubAllGlobals();
  });

  it("reports a missing field rather than returning an empty key", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ data: { data: { other: "x" } } }))),
    );
    const keys = new KeyResolver(vaultConfig());
    await expect(keys.forUser("jasper")).rejects.toThrow(/no string field "api_key"/);
    vi.unstubAllGlobals();
  });

  it("logs in with AppRole when it has no token", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL) => {
        const url = String(input);
        calls.push(url);
        if (url.includes("/auth/approle/login")) {
          return new Response(JSON.stringify({ auth: { client_token: "s.fresh" } }));
        }
        return new Response(JSON.stringify({ data: { data: { api_key: "ov_k" } } }));
      }),
    );

    const config = loadConfig({
      OV_URL: "http://o:1933",
      SESSION_SECRET: "a".repeat(32),
      AUTH_MODE: "dev",
      KEY_SOURCE: "vault",
      VAULT_ADDR: "https://vault.example",
      VAULT_ROLE_ID: "role",
      VAULT_SECRET_ID: "secret",
    });
    const keys = new KeyResolver(config);
    expect(await keys.forUser("jasper")).toBe("ov_k");
    expect(calls[0]).toContain("/v1/auth/approle/login");

    vi.unstubAllGlobals();
  });

  it("re-logs in once when the token has expired, then gives up", async () => {
    let logins = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL) => {
        const url = String(input);
        if (url.includes("/auth/approle/login")) {
          logins += 1;
          return new Response(JSON.stringify({ auth: { client_token: "s.stale" } }));
        }
        return new Response("", { status: 403 });
      }),
    );

    const config = loadConfig({
      OV_URL: "http://o:1933",
      SESSION_SECRET: "a".repeat(32),
      AUTH_MODE: "dev",
      KEY_SOURCE: "vault",
      VAULT_ADDR: "https://vault.example",
      VAULT_ROLE_ID: "role",
      VAULT_SECRET_ID: "secret",
    });
    const keys = new KeyResolver(config);
    await expect(keys.forUser("jasper")).rejects.toThrow(/rejected the dashboard/);
    expect(logins).toBe(2);

    vi.unstubAllGlobals();
  });
});
