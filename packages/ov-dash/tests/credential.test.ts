/**
 * The credential session, and the sign-in that creates it.
 *
 * This is the one cookie that carries a real secret — the OpenViking token
 * minted from a Vault login — so the properties worth pinning are that it is
 * opaque to the browser, that it never outlives the token inside it, and that
 * a page on another origin cannot cause one to be issued.
 */

import { Hono } from "hono";
import { describe, expect, it, vi } from "vitest";
import { buildServices, createApp } from "../src/server/app";
import { loadConfig } from "../src/server/env";
import { readCredentialSession, startCredentialSession } from "../src/server/session";
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

const VIEWER: Viewer = {
  sub: "jasper",
  name: "jasper",
  email: "",
  account: "lab",
  user: "jasper",
};

const TOKEN = "ov_secret_token_value";

function harness(cfg = config(), expiresAt: number | null = null) {
  const app = new Hono();
  app.get("/in", async (c) => {
    await startCredentialSession(c, cfg, VIEWER, TOKEN, expiresAt);
    return c.text("ok");
  });
  app.get("/who", async (c) => c.json(await readCredentialSession(c, cfg)));
  return app;
}

function cookieFrom(response: Response): string {
  return (response.headers.get("set-cookie") ?? "").split(";")[0] ?? "";
}

describe("the credential cookie", () => {
  it("round-trips the viewer and the token", async () => {
    const app = harness();
    const cookie = cookieFrom(await app.request("/in"));
    const body = await (await app.request("/who", { headers: { cookie } })).json();
    expect(body).toEqual({ viewer: VIEWER, token: TOKEN });
  });

  it("does not put the token where the browser can read it", async () => {
    const cookie = cookieFrom(await harness().request("/in"));
    // Encrypted, not merely signed: a JWE has five parts and no readable body.
    expect(cookie).not.toContain(TOKEN);
    const value = cookie.split("=")[1] ?? "";
    expect(value.split(".")).toHaveLength(5);
    for (const part of value.split(".")) {
      expect(Buffer.from(part, "base64url").toString("utf8")).not.toContain(TOKEN);
    }
  });

  it("cannot be read with a different secret", async () => {
    const cookie = cookieFrom(await harness().request("/in"));
    const other = harness(config({ SESSION_SECRET: "b".repeat(32) }));
    expect(
      await (await other.request("/who", { headers: { cookie } })).json(),
    ).toBeNull();
  });

  it("refuses a token that has already expired", async () => {
    // A typed SessionError, so a route answers "sign in again" rather than
    // falling through to the generic 500 a bare Error produced.
    const past = Math.floor(Date.now() / 1000) - 60;
    const app = new Hono();
    app.get("/in", async (c) => {
      await startCredentialSession(c, config(), VIEWER, TOKEN, past);
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
