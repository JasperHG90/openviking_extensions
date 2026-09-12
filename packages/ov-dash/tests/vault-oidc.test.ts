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
/** Vault's API, which is not the provider: the stub answers for this one. */
const VAULT = "https://vault.example";
const JWT_ROLE = "ov-dash";

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
    VAULT_ADDR: VAULT,
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
  body: string;
}

/** A token shaped like the one `identity/oidc/token/<role>` mints. */
async function mintedToken(
  claims: Record<string, unknown> = { ov_account: "lab", ov_user: "jasper" },
  expiresIn = 3600,
): Promise<string> {
  return new SignJWT(claims)
    .setProtectedHeader({ alg: "RS256", kid: KID })
    .setIssuer(`${VAULT}/v1/identity/oidc`)
    .setAudience("openviking")
    .setSubject("18b9e45e-2364-841d-0bb4-ee233218cf46")
    .setIssuedAt()
    .setExpirationTime(`${expiresIn}s`)
    .sign(keys.privateKey);
}

/** How Vault behaves for one test. */
interface VaultStub {
  /** What the JWT mount answers. `false` refuses the login outright. */
  login?: boolean;
  /** What the mint returns, or null to have Vault refuse it. */
  minted?: () => Promise<string | null>;
  /** Whether `revoke-self` works. Vault refuses it for a batch token. */
  revoke?: boolean;
  /** The mount and role this Vault is standing in for. */
  mount?: string;
  role?: string;
  /** Status for anything that is not a refusal — a sealed or unwell Vault. */
  unwell?: number;
}

/**
 * Answer Vault and OpenViking; let anything aimed at the provider through.
 *
 * The provider is a real socket (see above) and Vault is not, because the two
 * are asked different questions: the provider has to produce a signature jose
 * will check, while Vault only has to answer in its own envelope.
 *
 * @param mint - What the provider's token endpoint hands back, once the nonce
 *   of the login in flight is known.
 * @param vault - How Vault behaves. The default is a working one.
 */
function stubWorld(mint: () => Promise<string>, vault: VaultStub = {}) {
  mintToken = mint;
  exchange = null;
  const {
    login = true,
    minted = () => mintedToken(),
    revoke = true,
    mount = "jwt",
    role = JWT_ROLE,
    unwell = 0,
  } = vault;
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
      const body = typeof init?.body === "string" ? init.body : "";
      calls.push({ url, headers, body });

      if (url.startsWith(VAULT)) {
        if (unwell) {
          return new Response(JSON.stringify({ errors: ["Vault is sealed"] }), {
            status: unwell,
          });
        }
        if (url.endsWith(`/auth/${mount}/login`)) {
          if (!login) {
            return new Response(JSON.stringify({ errors: ["invalid role or JWT"] }), {
              status: 400,
            });
          }
          // What a real JWT role checks before it answers. A fake that hands
          // out a session for any body makes `bound_audiences` and `user_claim`
          // — the two settings the README calls load-bearing — untestable, and
          // a dashboard that stopped sending the right token would still pass.
          const sent = JSON.parse(body || "{}") as { role?: string; jwt?: string };
          const claims = JSON.parse(
            Buffer.from(sent.jwt?.split(".")[1] ?? "", "base64url").toString("utf8") ||
              "{}",
          ) as { aud?: string; iss?: string; ov_user?: string };
          if (sent.role !== role) {
            return new Response(
              JSON.stringify({ errors: [`role "${sent.role}" could not be found`] }),
              { status: 400 },
            );
          }
          if (claims.aud !== CLIENT_ID || claims.iss !== ISSUER) {
            return new Response(JSON.stringify({ errors: ["invalid audience (aud)"] }), {
              status: 400,
            });
          }
          if (!claims.ov_user) {
            return new Response(
              JSON.stringify({ errors: ["claim 'ov_user' not found in token"] }),
              { status: 400 },
            );
          }
          return new Response(
            JSON.stringify({ auth: { client_token: "hvs.a-session-for-that-person" } }),
          );
        }
        if (url.includes("/identity/oidc/token/")) {
          const token = await minted();
          if (!token) {
            return new Response(JSON.stringify({ errors: ["permission denied"] }), {
              status: 403,
            });
          }
          return new Response(JSON.stringify({ data: { token, ttl: 3600 } }));
        }
        if (url.endsWith("/auth/token/revoke-self")) {
          if (!revoke) {
            return new Response(
              JSON.stringify({ errors: ["batch tokens cannot be revoked"] }),
              { status: 400 },
            );
          }
          return new Response(null, { status: 204 });
        }
        // What a real Vault answers for a path that is not a mount: the
        // likeliest operator mistake in this whole change, and it must land in
        // the refusal branch rather than the outage one.
        return new Response(JSON.stringify({ errors: ["permission denied"] }), {
          status: 403,
        });
      }

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
    stubWorld(async () => signedIn());
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
    stubWorld(async () => signedIn());
    const app = createApp(buildServices(config({ OIDC_SCOPES: "openid ov-identity" })));
    const url = new URL((await app.request("/auth/login")).headers.get("location") ?? "");
    expect(url.searchParams.get("scope")).toBe("openid ov-identity");
  });
});

describe("coming back from Vault", () => {
  it("trades the ID token for the one OpenViking accepts, and calls with that", async () => {
    let issued = "";
    let minted = "";
    const calls = stubWorld(
      async () => {
        issued = await signedIn();
        return issued;
      },
      {
        minted: async () => {
          minted = await mintedToken();
          return minted;
        },
      },
    );
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
    // Read out of the minted token, not derived from an email that is not there.
    expect(session.viewer).toMatchObject({ account: "lab", user: "jasper", email: "" });
    // The session is ours to end, so the page may offer the button.
    expect(session.canSignOut).toBe(true);

    // The ID token went to Vault's JWT mount, named with the role...
    const traded = calls.find((call) => call.url === `${VAULT}/v1/auth/jwt/login`);
    expect(traded).toBeDefined();
    expect(JSON.parse(traded?.body ?? "{}")).toEqual({ role: JWT_ROLE, jwt: issued });

    // ...the session token minted from it...
    const mint = calls.find((call) => call.url.includes("/identity/oidc/token/"));
    expect(mint?.headers["x-vault-token"]).toBe("hvs.a-session-for-that-person");

    // ...and it was handed back, as the password flow does.
    expect(calls.some((call) => call.url.endsWith("/auth/token/revoke-self"))).toBe(true);

    // The point of the whole trade: OpenViking is called with the minted token.
    // The ID token it started from would be refused — wrong issuer, wrong
    // audience — and no key is looked up anywhere.
    await app.request("/api/home", { headers: { cookie } });
    const upstream = calls.filter((call) => call.url.includes("openviking:1933"));
    expect(upstream.length).toBeGreaterThan(0);
    for (const call of upstream) {
      expect(call.headers["x-api-key"]).toBe(minted);
      expect(call.headers["x-api-key"]).not.toBe(issued);
    }
  });

  it("hands the session token back, and only that one", async () => {
    // The password flow has five tests for this; the redirect flow had one that
    // any revoke would satisfy. Two mutations survived it: revoking outside the
    // `finally`, which leaks a session on every failed sign-in, and revoking
    // the ID token instead, which leaks on every sign-in and posts the ID token
    // to Vault in an x-vault-token header.
    let issued = "";
    const calls = stubWorld(async () => {
      issued = await signedIn();
      return issued;
    });
    await signIn(createApp(buildServices(config())));

    const revoked = calls.filter((call) => call.url.endsWith("/auth/token/revoke-self"));
    expect(revoked).toHaveLength(1);
    expect(revoked[0]?.headers["x-vault-token"]).toBe("hvs.a-session-for-that-person");
    expect(revoked[0]?.headers["x-vault-token"]).not.toBe(issued);
  });

  it("hands it back even when the mint fails", async () => {
    // The sign-in is refused either way; the question is whether a live Vault
    // session is left behind when it is. That is the case the `finally` exists
    // for, and the one a happy-path assertion never reaches.
    const calls = stubWorld(async () => signedIn(), { minted: async () => null });
    const { back } = await signIn(createApp(buildServices(config())));

    expect(back.status).toBe(403);
    const revoked = calls.filter((call) => call.url.endsWith("/auth/token/revoke-self"));
    expect(revoked).toHaveLength(1);
    expect(revoked[0]?.headers["x-vault-token"]).toBe("hvs.a-session-for-that-person");
  });

  it("signs somebody in even when the revoke fails", async () => {
    // Best effort, deliberately: a token that outlives its use is untidy, and
    // refusing somebody a session they have already earned is worse.
    stubWorld(async () => signedIn(), { revoke: false });
    const { back, cookie } = await signIn(createApp(buildServices(config())));

    expect(back.status).toBe(302);
    expect(cookie).not.toBe("");
  });

  it("trades at the mount and role it was configured with", async () => {
    // Both are settings, and both were unasserted: hard-coding either as a
    // string literal passed the whole suite.
    const calls = stubWorld(async () => signedIn(), {
      mount: "ov-jwt",
      role: "dashboard",
    });
    const app = createApp(
      buildServices(config({ VAULT_JWT_MOUNT: "ov-jwt", VAULT_JWT_ROLE: "dashboard" })),
    );
    const { back } = await signIn(app);

    expect(back.status).toBe(302);
    const traded = calls.find((call) => call.url === `${VAULT}/v1/auth/ov-jwt/login`);
    expect(traded).toBeDefined();
    expect(JSON.parse(traded?.body ?? "{}").role).toBe("dashboard");
  });

  it("tells an outage apart from a refusal", async () => {
    // A sealed Vault, a standby redirect, a rate limit. Reported as a refused
    // role, all three read as something an operator did — and none of them show
    // up as a 5xx anywhere.
    stubWorld(async () => signedIn(), { unwell: 503 });
    const { back } = await signIn(createApp(buildServices(config())));

    expect(back.status).toBe(502);
    const body = JSON.stringify(await back.json());
    expect(body).toMatch(/could not answer/);
    // The code, not just the status: this is what a log or an alert reads, and
    // VAULT_REFUSED on an outage is what sent an operator looking at config.
    expect(body).toMatch(/VAULT_UNAVAILABLE/);
    expect(cookieNamed(back, "ovdash_session")).toBe("");
  });

  it("refuses when Vault will not take the sign-in", async () => {
    // An unbound audience, a role that does not exist, a mount with no config.
    // The person is signed in at Vault and still gets nothing here, which is
    // the only honest answer: there is no credential to hold.
    stubWorld(async () => signedIn(), { login: false });
    const { back } = await signIn(createApp(buildServices(config())));

    expect(back.status).toBe(403);
    expect(cookieNamed(back, "ovdash_session")).toBe("");
    expect(JSON.stringify(await back.json())).toMatch(/JWT role does not accept/);
  });

  it("refuses when Vault mints for somebody else", async () => {
    // The two identities come from one entity by two routes: the provider's
    // scope and the role's template. They disagree only when the JWT mount's
    // entity alias points at the wrong entity — and that hands this person
    // another person's credential, which neither token shows on its own.
    stubWorld(async () => signedIn(), {
      minted: () => mintedToken({ ov_account: "lab", ov_user: "mallory" }),
    });
    const { back } = await signIn(createApp(buildServices(config())));

    expect(back.status).toBe(403);
    expect(cookieNamed(back, "ovdash_session")).toBe("");
    expect(JSON.stringify(await back.json())).toMatch(/different identity/);
  });

  it("refuses when Vault mints for the same name in another account", async () => {
    // The misroute that a user-only comparison misses: two entities sharing a
    // user name and differing in account — `jasper` in `lab` and `jasper` in
    // `prod` — with the JWT mount's alias on the wrong one. The account is the
    // header on every call and `{account}` in the templated root, so this is
    // somebody else's tree with a familiar name on it.
    stubWorld(async () => signedIn(), {
      minted: () => mintedToken({ ov_account: "prod", ov_user: "jasper" }),
    });
    const { back } = await signIn(createApp(buildServices(config())));

    expect(back.status).toBe(403);
    expect(cookieNamed(back, "ovdash_session")).toBe("");
    expect(JSON.stringify(await back.json())).toMatch(/different identity/);
  });

  it("refuses when the minted token carries no identity at all", async () => {
    // What Vault mints for an entity with no metadata — which is exactly what a
    // JWT login with no entity alias creates. Measured against a real Vault:
    // the mint succeeds and both claims come back empty.
    stubWorld(async () => signedIn(), {
      minted: () => mintedToken({ ov_account: "", ov_user: "" }),
    });
    const { back } = await signIn(createApp(buildServices(config())));

    expect(back.status).toBe(403);
    expect(cookieNamed(back, "ovdash_session")).toBe("");
  });

  it("sends the client secret to the token endpoint, and the code once", async () => {
    stubWorld(async () => signedIn());
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

  it("ends the session no later than the minted token", async () => {
    // The minted token's expiry, not the ID token's: the ID token is spent at
    // the JWT mount and never held, so its lifetime governs nothing here.
    stubWorld(async () => signedIn(), {
      minted: () => mintedToken({ ov_account: "lab", ov_user: "jasper" }, 60),
    });
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

  it("refuses a sign-in Vault could not tell apart", async () => {
    // What a provider with no `openviking` scope assigned to the client issues:
    // a valid token with no ov_user. Vault's JWT role takes the person's id
    // from that claim, so this is refused here rather than upstream, where the
    // message would name a claim nobody configured by hand.
    stubWorld(async () => idToken({ nonce: lastNonce }));
    const { back } = await signIn(createApp(buildServices(config())));

    expect(back.status).toBe(403);
    expect(cookieNamed(back, "ovdash_session")).toBe("");
    expect(JSON.stringify(await back.json())).toMatch(/scope has to template both/);
  });

  it("refuses a user id that is not one path segment", async () => {
    // It becomes a segment of viking://user/<id>, and Vault's metadata is only
    // as careful as whoever set it. Both tokens carry the same bad value, which
    // is the realistic shape — one entity, one bad metadata field, templated
    // into the scope and the role alike. So they agree, the cross-check passes,
    // and what refuses this is the path check on the minted token and nothing
    // else.
    stubWorld(
      async () =>
        idToken({ ov_account: "lab", ov_user: "../../resources", nonce: lastNonce }),
      { minted: () => mintedToken({ ov_account: "lab", ov_user: "../../resources" }) },
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
    //
    // Same value on both sides again: the cross-check has nothing to say about
    // two tokens that agree, so this is the path check or nothing.
    stubWorld(
      async () => idToken({ ov_account: "../..", ov_user: "jasper", nonce: lastNonce }),
      { minted: () => mintedToken({ ov_account: "../..", ov_user: "jasper" }) },
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
    stubWorld(async () => signedIn(), {
      minted: () => mintedToken({ ov_account: "", ov_user: "jasper" }),
    });
    const { back } = await signIn(createApp(buildServices(config())));

    expect(back.status).toBe(403);
    expect(cookieNamed(back, "ovdash_session")).toBe("");
  });

  it("refuses a minted token that never expires", async () => {
    // The session is capped by the credential's own expiry, so a token saying
    // nothing would quietly take the ceiling — eight hours of credential the
    // issuer never vouched for. Vault always sets `exp`; a role misconfigured
    // with no ttl is the case this refuses.
    stubWorld(async () => signedIn(), {
      minted: () =>
        new SignJWT({ ov_account: "lab", ov_user: "jasper" })
          .setProtectedHeader({ alg: "RS256", kid: KID })
          .setIssuer(`${VAULT}/v1/identity/oidc`)
          .setAudience("openviking")
          .setSubject("entity")
          .setIssuedAt()
          .sign(keys.privateKey),
    });
    const { back } = await signIn(createApp(buildServices(config())));

    expect(back.status).toBe(403);
    expect(cookieNamed(back, "ovdash_session")).toBe("");
    expect(JSON.stringify(await back.json())).toMatch(/does not say when it expires/);
  });

  it("refuses a token minted for another audience", async () => {
    // aud is the client id. A token for a different client of the same Vault
    // verifies against the same keys and must still not sign anyone in here.
    stubWorld(async () =>
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
    // Both tokens: the credential, and the ID token that bought it, which is a
    // Vault session for that person to anyone who catches it in flight.
    let issued = "";
    let minted = "";
    stubWorld(
      async () => {
        issued = await signedIn();
        return issued;
      },
      {
        minted: async () => {
          minted = await mintedToken();
          return minted;
        },
      },
    );
    const app = createApp(buildServices(config()));
    const { back, cookie } = await signIn(app);

    // The signature as well as the whole string: a cookie carrying only that
    // would still be the token, minus two guessable segments.
    const secrets = [issued, minted].flatMap((token) => {
      const signature = token.split(".")[2] ?? "";
      expect(signature.length).toBeGreaterThan(20);
      return [token, signature];
    });

    const answer = await (
      await app.request("/api/session", { headers: { cookie } })
    ).text();
    for (const secret of secrets) {
      for (const header of back.headers.getSetCookie()) {
        expect(header).not.toContain(secret);
      }
      expect(answer).not.toContain(secret);
    }
  });

  it("sends a tab whose login was superseded back to the sign-in page", async () => {
    // One login cookie per browser, so opening Sign in twice leaves the second
    // tab's state in it. Whichever tab comes back first no longer matches, and
    // a JSON refusal in the address bar is a worse answer than the door.
    stubWorld(async () => signedIn());
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
    stubWorld(async () =>
      idToken({ ov_account: "lab", ov_user: "jasper", nonce: "a-different-login" }),
    );
    const { back } = await signIn(createApp(buildServices(config())));

    expect(back.status).toBe(401);
    expect(cookieNamed(back, "ovdash_session")).toBe("");
  });
});

describe("the password form is gone in this mode", () => {
  it("answers the sign-out page with the Vault door", async () => {
    stubWorld(async () => signedIn());
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
    stubWorld(async () => signedIn());
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
    stubWorld(async () => signedIn());
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
