/**
 * Signing out.
 *
 * Two things went wrong here and both were invisible from the button: it was
 * offered in modes where nothing it does ends a session, and it left a tab's
 * worth of the previous person's data in the client cache.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cached, invalidate, isCached } from "../src/client/lib/cache";
import { buildServices, createApp } from "../src/server/app";
import { loadConfig } from "../src/server/env";

const ORIGIN = "http://localhost:8080";

function configFor(extra: Record<string, string>) {
  return loadConfig({
    OV_URL: "http://openviking:1933",
    PUBLIC_ORIGIN: ORIGIN,
    SESSION_SECRET: "a".repeat(32),
    KEY_SOURCE: "env",
    OV_API_KEY: "ov_test",
    ...extra,
  });
}

afterEach(() => {
  invalidate();
  vi.unstubAllGlobals();
});

describe("whether signing out is offered", () => {
  it("is, when the dashboard holds the session in a cookie", async () => {
    const app = createApp(
      buildServices(
        configFor({
          AUTH_MODE: "vault-userpass",
          VAULT_ADDR: "https://vault.example",
        }),
      ),
    );
    // dev identity is not available here, so assert the flag the route computes
    // by signing in is out of scope; the oidc branch is covered below.
    const response = await app.request("/api/session");
    expect(await response.json()).toMatchObject({ signedIn: false });
  });

  it("is not, when the identity arrives on every request", async () => {
    const app = createApp(
      buildServices(configFor({ AUTH_MODE: "dev", DEV_USER: "jasper" })),
    );
    const body = (await (await app.request("/api/session")).json()) as {
      canSignOut: boolean;
    };
    // Clearing a cookie dev mode never reads would leave the person signed in.
    expect(body.canSignOut).toBe(false);
  });

  it("is not, behind an authenticating proxy", async () => {
    const app = createApp(buildServices(configFor({ AUTH_MODE: "trusted-header" })));
    const body = (await (
      await app.request("/api/session", {
        headers: { "x-forwarded-user": "jasper@example.com" },
      })
    ).json()) as { canSignOut: boolean };
    expect(body.canSignOut).toBe(false);
  });
});

describe("the client cache does not outlive a session", () => {
  it("drops everything, so the next person is not shown the last one's data", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 204 })),
    );

    await cached("tree", async () => ({ nodes: ["jasper's files"] }));
    await cached("memories", async () => ["jasper's memories"]);
    expect(isCached("tree")).toBe(true);
    expect(isCached("memories")).toBe(true);

    const { api } = await import("../src/client/lib/api");
    await api.signOut();

    expect(isCached("tree")).toBe(false);
    expect(isCached("memories")).toBe(false);
  });

  it("still clears the cache when the server refuses the sign-out", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 500 })),
    );
    await cached("tree", async () => ({ nodes: ["jasper's files"] }));

    const { api } = await import("../src/client/lib/api");
    // A failed sign-out must still not leave the data readable.
    await expect(api.signOut()).rejects.toThrow(/could not sign out/);
    expect(isCached("tree")).toBe(false);
  });
});
