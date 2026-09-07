import { Hono } from "hono";
import { describe, expect, it } from "vitest";
import { loadConfig } from "../src/server/env";
import {
  endSession,
  readSession,
  startLogin,
  startSession,
  takeLogin,
} from "../src/server/session";
import type { Viewer } from "../src/shared/schemas";

const config = loadConfig({
  OV_URL: "http://openviking:1933",
  SESSION_SECRET: "a".repeat(32),
  AUTH_MODE: "oidc",
  OIDC_ISSUER: "https://vault.example",
  OIDC_CLIENT_ID: "id",
  OIDC_CLIENT_SECRET: "secret",
  KEY_SOURCE: "env",
  OV_API_KEY: "k",
  SESSION_COOKIE_SECURE: "false",
});

const VIEWER: Viewer = {
  sub: "entity-1",
  name: "Jasper",
  email: "jasper@example.com",
  account: "jasper",
  user: "jasper",
};

/** Mount a tiny app that writes a session, then reads it back. */
function app() {
  const instance = new Hono();
  instance.get("/in", async (c) => {
    await startSession(c, config, VIEWER);
    return c.text("ok");
  });
  instance.get("/who", async (c) => {
    const viewer = await readSession(c, config);
    return c.json(viewer);
  });
  instance.get("/out", (c) => {
    endSession(c, config);
    return c.text("ok");
  });
  return instance;
}

function cookieFrom(response: Response): string {
  const header = response.headers.get("set-cookie") ?? "";
  return header.split(";")[0] ?? "";
}

describe("sessions", () => {
  it("round-trips a viewer through a signed cookie", async () => {
    const instance = app();
    const signIn = await instance.request("/in");
    const cookie = cookieFrom(signIn);
    expect(cookie).toContain(config.SESSION_COOKIE_NAME);

    const who = await instance.request("/who", { headers: { cookie } });
    expect(await who.json()).toEqual(VIEWER);
  });

  it("marks the cookie HttpOnly and SameSite=Lax", async () => {
    const response = await app().request("/in");
    const header = response.headers.get("set-cookie") ?? "";
    expect(header).toMatch(/HttpOnly/i);
    // Lax and not Strict: the provider's redirect back is a cross-site GET and
    // Strict would drop the cookie on exactly that hop.
    expect(header).toMatch(/SameSite=Lax/i);
  });

  it("never puts a credential in the cookie", async () => {
    const response = await app().request("/in");
    const header = response.headers.get("set-cookie") ?? "";
    const [, payload] = (header.split(";")[0] ?? "").split(".");
    const claims = JSON.parse(
      Buffer.from(payload ?? "", "base64url").toString("utf8"),
    ) as Record<string, unknown>;
    expect(Object.keys(claims).sort()).toEqual(
      ["account", "aud", "email", "exp", "iat", "iss", "name", "sub", "user"].sort(),
    );
    expect(JSON.stringify(claims)).not.toContain("ov_");
  });

  it("rejects a tampered cookie", async () => {
    const instance = app();
    const cookie = cookieFrom(await instance.request("/in"));
    const [name, token] = cookie.split("=");
    const parts = (token ?? "").split(".");
    // Swap the payload for one claiming to be somebody else, keeping the
    // original signature.
    const forged = Buffer.from(
      JSON.stringify({ ...VIEWER, user: "someone-else" }),
    ).toString("base64url");
    const tampered = `${name}=${parts[0]}.${forged}.${parts[2]}`;

    const who = await instance.request("/who", { headers: { cookie: tampered } });
    expect(await who.json()).toBeNull();
  });

  it("reads nothing when there is no cookie", async () => {
    const who = await app().request("/who");
    expect(await who.json()).toBeNull();
  });

  it("clears the cookie on sign-out", async () => {
    const response = await app().request("/out");
    expect(response.headers.get("set-cookie")).toMatch(/Max-Age=0|Expires=/i);
  });
});

describe("login state", () => {
  it("hands back the verifier once and then forgets it", async () => {
    const instance = new Hono();
    instance.get("/begin", async (c) => {
      await startLogin(c, config, {
        state: "st",
        nonce: "no",
        verifier: "ve",
        returnTo: "/files",
      });
      return c.text("ok");
    });
    instance.get("/finish", async (c) => c.json(await takeLogin(c, config)));

    const cookie = cookieFrom(await instance.request("/begin"));
    const first = await instance.request("/finish", { headers: { cookie } });
    expect(await first.json()).toMatchObject({ state: "st", verifier: "ve" });

    // The cookie the response clears is what stops a replayed callback; a
    // browser that honoured it sends nothing the second time.
    const second = await instance.request("/finish");
    expect(await second.json()).toBeNull();
  });
});
