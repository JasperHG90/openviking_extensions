/**
 * The credential session, and the sign-in that creates it.
 *
 * The OpenViking token minted from a Vault login is the one real secret here,
 * and it no longer travels: the cookie names a session and the token stays in
 * the store. So the properties worth pinning are that nothing about the person
 * or their credential is in the cookie at all, that the session never outlives
 * the token behind it, and that a page on another origin cannot cause one to
 * be issued.
 */

import { Hono } from "hono";
import { describe, expect, it, vi } from "vitest";
import { buildServices, createApp } from "../src/server/app";
import { loadConfig } from "../src/server/env";
import { readCredentialSession, startCredentialSession } from "../src/server/session";
import { SessionStore } from "../src/server/store";
import type { Viewer } from "../src/shared/schemas";

const ORIGIN = "http://localhost:8080";

function config(extra: Record<string, string> = {}) {
  return loadConfig({
    OV_URL: "http://openviking:1933",
    PUBLIC_ORIGIN: ORIGIN,
    SESSION_SECRET: "a".repeat(32),
    AUTH_MODE: "vault-userpass",
    VAULT_ADDR: "https://vault.example",
    SESSION_COOKIE_SECURE: "false",
    ...extra,
  });
}

// A real address, because the test below is about identity not reaching the
// cookie and an empty one would be in every string ever written.
const VIEWER: Viewer = {
  sub: "jasper",
  name: "jasper",
  email: "jasper@example.com",
  account: "lab",
  user: "jasper",
};

const TOKEN = "ov_secret_token_value";

function harness(
  cfg = config(),
  expiresAt: number | null = null,
  store = new SessionStore(),
) {
  const app = new Hono();
  app.get("/in", async (c) => {
    await startCredentialSession(c, cfg, store, VIEWER, TOKEN, expiresAt);
    return c.text("ok");
  });
  app.get("/who", async (c) => c.json(await readCredentialSession(c, cfg, store)));
  return app;
}

function cookieFrom(response: Response): string {
  return (response.headers.get("set-cookie") ?? "").split(";")[0] ?? "";
}

describe("the credential cookie", () => {
  it("round-trips the viewer, the token, and when it runs out", async () => {
    const app = harness();
    const cookie = cookieFrom(await app.request("/in"));
    const body = (await (await app.request("/who", { headers: { cookie } })).json()) as {
      viewer: Viewer;
      token: string;
      expiresAt: number;
    };
    expect(body.viewer).toEqual(VIEWER);
    expect(body.token).toBe(TOKEN);
    // Nothing renews this session, so the Account page shows when it ends.
    // Read off the stored entry rather than recomputed, so the two agree.
    expect(body.expiresAt).toBeGreaterThan(Math.floor(Date.now() / 1000));
  });

  it("carries nothing but a name for the session", async () => {
    const cookie = cookieFrom(await harness().request("/in"));
    const value = cookie.split("=")[1] ?? "";

    // Not the token, and not the identity either — the cookie used to carry
    // both, encrypted; now it carries neither in any form.
    for (const part of value.split(".")) {
      const decoded = Buffer.from(part, "base64url").toString("utf8");
      expect(decoded).not.toContain(TOKEN);
      expect(decoded).not.toContain(VIEWER.account);
      // The address itself. This used to look for a bare "@", which decoded
      // ciphertext produces on its own roughly one run in seven -- a test that
      // failed at random while proving nothing, since the fixture had no email
      // to leak.
      expect(decoded).not.toContain(VIEWER.email);
    }
  });

  it("is refused when signed with a different secret", async () => {
    const store = new SessionStore();
    const cookie = cookieFrom(await harness(config(), null, store).request("/in"));
    // Same store, different signing secret: the id inside is a real one, so
    // what refuses this is the signature check and nothing else.
    const other = harness(config({ SESSION_SECRET: "b".repeat(32) }), null, store);
    expect(
      await (await other.request("/who", { headers: { cookie } })).json(),
    ).toBeNull();
  });

  it("is refused when it names a session this server never had", async () => {
    const cookie = cookieFrom(await harness().request("/in"));
    // Correctly signed, but a different store — which is what a restart looks
    // like, and what a second replica would look like.
    const restarted = harness(config(), null, new SessionStore());
    expect(
      await (await restarted.request("/who", { headers: { cookie } })).json(),
    ).toBeNull();
  });

  it("refuses a token that has already expired", async () => {
    // A typed SessionError, so a route answers "sign in again" rather than
    // falling through to the generic 500 a bare Error produced.
    const past = Math.floor(Date.now() / 1000) - 60;
    const app = new Hono();
    app.get("/in", async (c) => {
      await startCredentialSession(c, config(), new SessionStore(), VIEWER, TOKEN, past);
      return c.text("unreachable");
    });
    app.onError((error, c) => c.json({ message: error.message }, 401));

    const response = await app.request("/in");
    expect(response.status).toBe(401);
    expect(JSON.stringify(await response.json())).toMatch(/already expired/);
  });

  it("refuses a token whose exp is zero, rather than reading it as absent", async () => {
    // `exp: 0` is a token from 1970. Read with truthiness it looks like "this
    // token says nothing about when it expires", which would wrap a long-dead
    // credential in a full-length session cookie.
    const app = new Hono();
    app.get("/in", async (c) => {
      await startCredentialSession(c, config(), new SessionStore(), VIEWER, TOKEN, 0);
      return c.text("unreachable");
    });
    app.onError((error, c) => c.json({ message: error.message }, 401));

    const response = await app.request("/in");
    expect(response.status).toBe(401);
    expect(JSON.stringify(await response.json())).toMatch(/already expired/);
  });

  it("never outlives the token it carries", async () => {
    // Token expires in a minute; the session TTL is hours. The shorter wins.
    const soon = Math.floor(Date.now() / 1000) + 60;
    const response = await harness(
      config({ SESSION_TTL_SECONDS: "28800" }),
      soon,
    ).request("/in");
    const header = response.headers.get("set-cookie") ?? "";
    const maxAge = Number(/Max-Age=(\d+)/.exec(header)?.[1] ?? "0");
    expect(maxAge).toBeLessThanOrEqual(60);
    expect(maxAge).toBeGreaterThan(0);
  });
});

describe("a page on another origin cannot sign somebody in", () => {
  function stubVault() {
    const claims = Buffer.from(
      JSON.stringify({
        ov_account: "lab",
        ov_user: "mallory",
        exp: Math.floor(Date.now() / 1000) + 3600,
      }),
    ).toString("base64url");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL) => {
        const url = String(input);
        if (url.includes("/auth/userpass/login/")) {
          return new Response(JSON.stringify({ auth: { client_token: "s.tok" } }));
        }
        return new Response(
          JSON.stringify({ data: { token: `eyJhbGciOiJSUzI1NiJ9.${claims}.sig` } }),
        );
      }),
    );
  }

  it("refuses a cross-site sign-in", async () => {
    stubVault();
    const app = createApp(buildServices(config()));
    const response = await app.request("/auth/vault-login", {
      method: "POST",
      headers: {
        origin: "https://evil.example",
        "content-type": "application/x-www-form-urlencoded",
      },
      body: new URLSearchParams({ username: "mallory", password: "pw" }),
    });

    // Without this guard the response carried a Set-Cookie for mallory under
    // the same cookie name, replacing the victim's own session — so every file
    // they added afterwards landed in the attacker's OpenViking tree.
    expect(response.status).toBe(403);
    expect(response.headers.get("set-cookie")).toBeNull();
    vi.unstubAllGlobals();
  });

  it("still lets the dashboard's own page sign in", async () => {
    stubVault();
    const app = createApp(buildServices(config()));
    const response = await app.request("/auth/vault-login", {
      method: "POST",
      headers: { origin: ORIGIN, "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ username: "mallory", password: "pw" }),
    });
    expect(response.status).toBe(200);
    expect(response.headers.get("set-cookie")).toContain("ovdash_session");
    vi.unstubAllGlobals();
  });
});
