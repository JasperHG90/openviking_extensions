/**
 * Signing in by being sent to Vault.
 *
 * The provider here is a stub, but a signing one: it mints a real RS256 token
 * and publishes the key it used, so the callback goes through the same
 * signature, issuer, audience and nonce checks a real login does. What the
 * tests are about is everything that happens *after* those pass — because the
 * one thing this mode does that `oidc` does not is keep the token, rather than
 * read an identity off it and throw it away.
 */

import { type Server, createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { type KeyLike, SignJWT, exportJWK, generateKeyPair } from "jose";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { buildServices, createApp } from "../src/server/app";
import { loadConfig } from "../src/server/env";

const ORIGIN = "http://localhost:8080";
const CLIENT_ID = "4AlI3ttLxDkuez3gO9vKVBfS0K5le07o";
const KID = "test-key";

/**
 * The provider is a real HTTP server, not a stubbed `fetch`.
 *
 * Not a preference: jose fetches a JWKS through node's own `http.get`, which a
 * stubbed global `fetch` never sees. Stubbing it left every signature check
 * resolving `vault.example` for real and failing — so the keys are published
 * over a socket, and the verification in the callback is the genuine one.
 */
let server: Server;
let base = "";
let ISSUER = "";
let keys: { privateKey: KeyLike; jwk: Record<string, unknown> };
/** Set per test: what the token endpoint hands back. */
let mintToken: () => Promise<string>;
/** What the dashboard sent to the token endpoint, for the tests about it. */
let exchange: { headers: Record<string, string>; body: string } | null = null;

function discovery() {
  return {
    issuer: ISSUER,
    authorization_endpoint: `${base}/ui/vault/identity/oidc/provider/ovdash/authorize`,
    token_endpoint: `${ISSUER}/token`,
    jwks_uri: `${ISSUER}/.well-known/keys`,
    grant_types_supported: ["authorization_code"],
    token_endpoint_auth_methods_supported: ["none", "client_secret_basic"],
    code_challenge_methods_supported: ["S256"],
    scopes_supported: ["openviking", "openid"],
  };
}

beforeAll(async () => {
  const pair = await generateKeyPair("RS256", { extractable: true });
  keys = {
    privateKey: pair.privateKey,
    jwk: { ...(await exportJWK(pair.publicKey)), kid: KID, alg: "RS256", use: "sig" },
  };

  server = createServer((req, res) => {
    const url = req.url ?? "";
    const answer = (body: unknown, type = "application/json") => {
      res.writeHead(200, { "content-type": type });
      res.end(JSON.stringify(body));
    };

    if (url.includes(".well-known/openid-configuration")) return answer(discovery());
    if (url.includes(".well-known/keys")) {
      return answer({ keys: [keys.jwk] }, "application/jwk-set+json");
    }
    if (url.endsWith("/token")) {
      const chunks: Buffer[] = [];
      req.on("data", (chunk: Buffer) => chunks.push(chunk));
      req.on("end", () => {
        const headers: Record<string, string> = {};
        for (const [name, value] of Object.entries(req.headers)) {
          headers[name] = Array.isArray(value) ? value.join(",") : (value ?? "");
        }
        exchange = { headers, body: Buffer.concat(chunks).toString("utf8") };
        void mintToken().then((id_token) =>
          answer({
            access_token: "hvb.opaque-to-everything-but-userinfo",
            id_token,
            token_type: "Bearer",
            expires_in: 1800,
          }),
        );
      });
      return;
    }
    res.writeHead(404);
    res.end("{}");
  });

  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  ISSUER = `${base}/v1/identity/oidc/provider/ovdash`;
});

afterAll(async () => {
  await new Promise<void>((resolve) => server.close(() => resolve()));
});

function config(extra: Record<string, string> = {}) {
  return loadConfig({
    OV_URL: "http://openviking:1933",
    PUBLIC_ORIGIN: ORIGIN,
    SESSION_SECRET: "a".repeat(32),
    AUTH_MODE: "vault-oidc",
    OIDC_ISSUER: ISSUER,
    OIDC_CLIENT_ID: CLIENT_ID,
    OIDC_CLIENT_SECRET: "hvo_secret",
    SESSION_COOKIE_SECURE: "false",
    ...extra,
  });
}

/** A token shaped like the one Vault's provider issues for this client. */
async function idToken(
  claims: Record<string, unknown>,
  { expiresIn = 1800, audience = CLIENT_ID, issuer = ISSUER } = {},
): Promise<string> {
  return new SignJWT(claims)
    .setProtectedHeader({ alg: "RS256", kid: KID })
    .setIssuer(issuer)
    .setAudience(audience)
    .setSubject("18b9e45e-2364-841d-0bb4-ee233218cf46")
    .setIssuedAt()
    .setExpirationTime(`${expiresIn}s`)
    .sign(keys.privateKey);
}

interface Call {
  url: string;
  headers: Record<string, string>;
}

/**
 * Answer OpenViking, and let anything aimed at the provider through.
 *
 * @param mint - What the token endpoint hands back. Called once the nonce of
 *   the login in flight is known, so a token can carry it.
 */
function stubOv(mint: () => Promise<string>) {
  mintToken = mint;
  exchange = null;
  const calls: Call[] = [];
  const real = globalThis.fetch;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.startsWith(base)) return real(input as string, init);

      const headers: Record<string, string> = {};
      new Headers(init?.headers).forEach((value, key) => {
        headers[key] = value;
      });
      calls.push({ url, headers });
      // An empty listing rather than an empty object: the page this test calls
      // maps over the result, and `{}` makes it throw where nothing is on trial.
      return new Response(JSON.stringify({ status: "ok", result: [], time: 0.01 }), {
        headers: { "content-type": "application/json" },
      });
    }),
  );
  return calls;
}

/** The nonce of the login in flight, so a token can carry it back. */
let lastNonce = "";

/** The ordinary token: a person Vault knows, with the claims the scope adds. */
function signedIn(): Promise<string> {
  return idToken({ ov_account: "lab", ov_user: "jasper", nonce: lastNonce });
}

function cookieNamed(response: Response, name: string): string {
  for (const header of response.headers.getSetCookie()) {
    const pair = header.split(";")[0] ?? "";
    if (pair.startsWith(`${name}=`) && pair !== `${name}=`) return pair;
  }
  return "";
}

/**
 * Read the login cookie the way the dashboard will.
 *
 * The state and nonce are what a real provider echoes back, and the only place
 * they exist between the two requests is this cookie — so the test takes them
 * from there rather than inventing a pair the callback would refuse.
 */
function loginState(cookie: string): { state: string; nonce: string } {
  const payload = cookie.split("=")[1]?.split(".")[1] ?? "";
  return JSON.parse(Buffer.from(payload, "base64url").toString("utf8")) as {
    state: string;
    nonce: string;
  };
}

/** Walk the whole dance: login, provider, callback. */
async function signIn(app: ReturnType<typeof createApp>, returnTo = "/#/home") {
  const start = await app.request(`/auth/login?returnTo=${encodeURIComponent(returnTo)}`);
  const pending = cookieNamed(start, "ovdash_session_login");
  const { state, nonce } = loginState(pending);
  lastNonce = nonce;
  const back = await app.request(`/auth/callback?code=abc&state=${state}`, {
    headers: { cookie: pending },
  });
  return { start, back, cookie: cookieNamed(back, "ovdash_session") };
}

afterEach(() => {
  vi.unstubAllGlobals();
  lastNonce = "";
});

describe("being sent to Vault", () => {
  it("asks for the scope that carries the identity claims", async () => {
    stubOv(async () => signedIn());
    const response = await createApp(buildServices(config())).request("/auth/login");

    expect(response.status).toBe(302);
    const url = new URL(response.headers.get("location") ?? "");
    // Without the second scope Vault issues a token with no ov_account and no
    // ov_user, the sign-in gets as far as the callback, and only then fails.
    expect(url.searchParams.get("scope")).toBe("openid openviking");
    expect(url.searchParams.get("client_id")).toBe(CLIENT_ID);
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    expect(url.origin + url.pathname).toBe(discovery().authorization_endpoint);
  });

  it("names a Vault scope of another name when told to", async () => {
    stubOv(async () => signedIn());
    const app = createApp(buildServices(config({ OIDC_SCOPES: "openid ov-identity" })));
    const url = new URL((await app.request("/auth/login")).headers.get("location") ?? "");
    expect(url.searchParams.get("scope")).toBe("openid ov-identity");
  });
});

describe("coming back from Vault", () => {
  it("keeps the token Vault issued and answers OpenViking with it", async () => {
    let issued = "";
    const calls = stubOv(async () => {
      issued = await signedIn();
      return issued;
    });
    const app = createApp(buildServices(config()));
    const { back, cookie } = await signIn(app);

    expect(back.status).toBe(302);
    expect(back.headers.get("location")).toBe("/#/home");

    const session = (await (
      await app.request("/api/session", { headers: { cookie } })
    ).json()) as {
      signedIn: boolean;
      canSignOut: boolean;
      viewer: { account: string; user: string; email: string };
    };
    expect(session.signedIn).toBe(true);
    // Read out of the token, not derived from an email that is not there.
    expect(session.viewer).toMatchObject({ account: "lab", user: "jasper", email: "" });
    // The session is ours to end, so the page may offer the button.
    expect(session.canSignOut).toBe(true);

    // The point of the mode: the ID token is the credential, so no key is
    // looked up anywhere and OpenViking is called with the token itself.
    await app.request("/api/home", { headers: { cookie } });
    const upstream = calls.filter((call) => call.url.includes("openviking:1933"));
    expect(upstream.length).toBeGreaterThan(0);
    for (const call of upstream) expect(call.headers["x-api-key"]).toBe(issued);
  });

  it("sends the client secret to the token endpoint, and the code once", async () => {
    stubOv(async () => signedIn());
    await signIn(createApp(buildServices(config())));

    expect(exchange).not.toBeNull();
    const credential = Buffer.from(
      (exchange?.headers.authorization ?? "").replace(/^Basic /, ""),
      "base64",
    ).toString("utf8");
    expect(credential).toBe(`${encodeURIComponent(CLIENT_ID)}:hvo_secret`);
    // PKCE, not just the secret: the code alone must not be redeemable.
    expect(new URLSearchParams(exchange?.body ?? "").get("code_verifier")).toBeTruthy();
  });

  it("ends the session no later than the token", async () => {
    stubOv(async () =>
      idToken(
        { ov_account: "lab", ov_user: "jasper", nonce: lastNonce },
        { expiresIn: 60 },
      ),
    );
    const { back } = await signIn(
      createApp(buildServices(config({ SESSION_TTL_SECONDS: "28800" }))),
    );

    const header = back.headers
      .getSetCookie()
      .find((h) => h.startsWith("ovdash_session="));
    const maxAge = Number(/Max-Age=(\d+)/.exec(header ?? "")?.[1] ?? "0");
    // Vault advertises no refresh grant, so a session outliving the token would
    // be a signed-in-looking shell that cannot read anything.
    expect(maxAge).toBeGreaterThan(0);
    expect(maxAge).toBeLessThanOrEqual(60);
  });

  it("refuses a token carrying no OpenViking identity", async () => {
    // What a provider with no `openviking` scope assigned to the client issues:
    // a perfectly valid token that says nothing OpenViking can act on.
    stubOv(async () => idToken({ nonce: lastNonce }));
    const app = createApp(buildServices(config()));
    const { back } = await signIn(app);

    expect(back.status).toBe(403);
    expect(cookieNamed(back, "ovdash_session")).toBe("");
    expect(JSON.stringify(await back.json())).toMatch(/ov_account\/ov_user/);
  });

  it("refuses a user id that is not one path segment", async () => {
    // It becomes a segment of viking://user/<id>, and Vault's metadata is only
    // as careful as whoever set it.
    stubOv(async () =>
      idToken({ ov_account: "lab", ov_user: "../../resources", nonce: lastNonce }),
    );
    const { back } = await signIn(createApp(buildServices(config())));

    expect(back.status).toBe(403);
    expect(cookieNamed(back, "ovdash_session")).toBe("");
  });

  it("refuses an account that is not one path segment either", async () => {
    // `{account}` is templated into OV_ROOT, and every read builds its root
    // straight from the viewer — so an account of "../.." reached OpenViking as
    // a uri climbing out of the tree it was supposed to name. The write paths
    // refused it; the read paths had nothing to refuse it with.
    stubOv(async () =>
      idToken({ ov_account: "../..", ov_user: "jasper", nonce: lastNonce }),
    );
    const app = createApp(
      buildServices(config({ OV_ROOT: "viking://a/{account}/{user}" })),
    );
    const { back } = await signIn(app);

    expect(back.status).toBe(403);
    expect(cookieNamed(back, "ovdash_session")).toBe("");
  });

  it("refuses an account that is empty", async () => {
    // An empty claim is a broken Vault template, and it would otherwise sign
    // somebody into a dashboard that reads but cannot write — the SDK drops the
    // account header when it is empty, and the Add page has nothing to scope to.
    stubOv(async () => idToken({ ov_account: "", ov_user: "jasper", nonce: lastNonce }));
    const { back } = await signIn(createApp(buildServices(config())));

    expect(back.status).toBe(403);
    expect(cookieNamed(back, "ovdash_session")).toBe("");
  });

  it("refuses a token that never expires", async () => {
    // The session is capped by the token's own expiry, so a token saying
    // nothing would quietly take the ceiling — eight hours of credential the
    // issuer never vouched for. Vault always sets `exp`; nothing stops this
    // mode being pointed at an issuer that does not.
    stubOv(async () =>
      new SignJWT({ ov_account: "lab", ov_user: "jasper", nonce: lastNonce })
        .setProtectedHeader({ alg: "RS256", kid: KID })
        .setIssuer(ISSUER)
        .setAudience(CLIENT_ID)
        .setSubject("entity")
        .setIssuedAt()
        .sign(keys.privateKey),
    );
    const { back } = await signIn(createApp(buildServices(config())));

    expect(back.status).toBe(403);
    expect(cookieNamed(back, "ovdash_session")).toBe("");
    expect(JSON.stringify(await back.json())).toMatch(/does not say when it expires/);
  });

  it("refuses a token minted for another audience", async () => {
    // aud is the client id. A token for a different client of the same Vault
    // verifies against the same keys and must still not sign anyone in here.
    stubOv(async () =>
      idToken(
        { ov_account: "lab", ov_user: "mallory", nonce: lastNonce },
        { audience: "some-other-client" },
      ),
    );
    const { back } = await signIn(createApp(buildServices(config())));

    expect(back.status).toBe(401);
    expect(cookieNamed(back, "ovdash_session")).toBe("");
  });

  it("never hands the token to the browser", async () => {
    // The headline claim of the mode. The suite proves the token reaches
    // OpenViking; this is the other half — that it reaches nothing else.
    let issued = "";
    stubOv(async () => {
      issued = await signedIn();
      return issued;
    });
    const app = createApp(buildServices(config()));
    const { back, cookie } = await signIn(app);
    // The signature as well as the whole string: a cookie carrying only that
    // would still be the token, minus two guessable segments.
    const signature = issued.split(".")[2] ?? "";
    expect(signature.length).toBeGreaterThan(20);

    for (const header of back.headers.getSetCookie()) {
      expect(header).not.toContain(issued);
      expect(header).not.toContain(signature);
    }
    const answer = await (
      await app.request("/api/session", { headers: { cookie } })
    ).text();
    expect(answer).not.toContain(issued);
    expect(answer).not.toContain(signature);
  });

  it("sends a tab whose login was superseded back to the sign-in page", async () => {
    // One login cookie per browser, so opening Sign in twice leaves the second
    // tab's state in it. Whichever tab comes back first no longer matches, and
    // a JSON refusal in the address bar is a worse answer than the door.
    stubOv(async () => signedIn());
    const app = createApp(buildServices(config()));

    const first = await app.request("/auth/login");
    const firstState = loginState(cookieNamed(first, "ovdash_session_login")).state;
    const second = await app.request("/auth/login");
    const held = cookieNamed(second, "ovdash_session_login");

    const back = await app.request(`/auth/callback?code=abc&state=${firstState}`, {
      headers: { cookie: held },
    });
    expect(back.status).toBe(302);
    expect(back.headers.get("location")).toBe("/");
    expect(cookieNamed(back, "ovdash_session")).toBe("");
  });

  it("refuses a token replayed from another login", async () => {
    stubOv(async () =>
      idToken({ ov_account: "lab", ov_user: "jasper", nonce: "a-different-login" }),
    );
    const { back } = await signIn(createApp(buildServices(config())));

    expect(back.status).toBe(401);
    expect(cookieNamed(back, "ovdash_session")).toBe("");
  });
});

describe("the password form is gone in this mode", () => {
  it("answers the sign-out page with the Vault door", async () => {
    stubOv(async () => signedIn());
    const response = await createApp(buildServices(config())).request("/api/session");
    expect(await response.json()).toEqual({
      signedIn: false,
      loginUrl: "/auth/login",
      // What makes the page draw one button that says where it sends you,
      // instead of a username and password.
      mode: "vault-oidc",
    });
  });

  it("refuses a password posted at it", async () => {
    stubOv(async () => signedIn());
    const response = await createApp(buildServices(config())).request(
      "/auth/vault-login",
      {
        method: "POST",
        headers: { origin: ORIGIN, "content-type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ username: "jasper", password: "hunter2" }),
      },
    );
    // The route exists for the other mode; here there is nothing behind it and
    // a password must not be accepted just because it was offered.
    expect(response.status).toBe(404);
  });
});

describe("signing out", () => {
  it("kills a cookie copied beforehand", async () => {
    stubOv(async () => signedIn());
    const app = createApp(buildServices(config()));
    const { cookie } = await signIn(app);

    const out = await app.request("/auth/logout", {
      method: "POST",
      headers: { origin: ORIGIN, cookie },
    });
    expect(out.status).toBe(204);

    const after = (await (
      await app.request("/api/session", { headers: { cookie } })
    ).json()) as { signedIn: boolean };
    expect(after.signedIn).toBe(false);
  });
});
