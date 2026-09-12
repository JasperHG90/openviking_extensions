/**
 * Signing in and out against a real Vault, both ways in.
 *
 * The unit suite stubs the world, which proves the dashboard sends what we
 * think it sends and nothing about what Vault does with it. These claims are
 * about Vault's behaviour, not ours, and a stub cannot settle any of them:
 *
 * 1. `revoke-self` really kills the login token.
 * 2. The minted identity token survives that revocation. If it did not, every
 *    sign-in would be broken the moment the revoke landed — and the unit tests
 *    would still pass, because the stub answers whatever we tell it to.
 * 3. Vault's claim templating produces the `ov_account` / `ov_user` the
 *    dashboard reads identity from — in a minted token, *and* in an ID token
 *    from its OIDC provider, which is a different feature with its own
 *    templating and no obligation to agree.
 * 4. The redirect sign-in works end to end against the real endpoints: real
 *    discovery, a real authorization code, a real PKCE exchange, and a
 *    signature checked against keys Vault actually published.
 *
 * Plus the one thing that is ours and matters most: a cookie copied before a
 * sign-out is worthless after it.
 *
 * Not part of `npm test`. Run it with `just test-integration`, which starts the
 * container if it is not up. By hand:
 *
 *   docker run -d --name ovdash-vault -p 18200:8200 \
 *     -e VAULT_DEV_ROOT_TOKEN_ID=root \
 *     -e VAULT_API_ADDR=http://127.0.0.1:18200 hashicorp/vault:latest
 *   npm run test:integration
 *
 * VAULT_API_ADDR matters: a provider's issuer and JWKS URL are built from it,
 * and dev mode's default names an address this suite cannot fetch.
 *
 * The setup below is idempotent, so re-running against the same container is
 * fine.
 */

import { beforeAll, describe, expect, it } from "vitest";
import { buildServices, createApp } from "../src/server/app";
import { loadConfig } from "../src/server/env";

const VAULT = process.env.VAULT_TEST_ADDR ?? "http://127.0.0.1:18200";
const ROOT = process.env.VAULT_TEST_ROOT_TOKEN ?? "root";
const ORIGIN = "http://localhost:8080";
const USER = "jasper";
const PASSWORD = "hunter2";
const ROLE = "openviking";
const PROVIDER = "ovdash";
/** The OIDC scope that puts the OpenViking claims in an ID token. */
const SCOPE = "openviking";
/** Filled in by the setup: Vault generates both. */
let clientId = "";
let clientSecret = "";

async function vault(
  path: string,
  init: RequestInit & { token?: string } = {},
): Promise<Response> {
  const { token = ROOT, ...rest } = init;
  return fetch(`${VAULT}/v1/${path}`, {
    ...rest,
    headers: { "x-vault-token": token, ...(rest.headers ?? {}) },
  });
}

async function json<T>(response: Response): Promise<T> {
  return (await response.json()) as T;
}

/**
 * Put the mounts, the entity and the role in place.
 *
 * Deliberately the same shape the README asks an operator for: a userpass
 * mount, an entity carrying `ov_account`/`ov_user` metadata, an alias joining
 * the two, and a role whose template lifts that metadata into the token.
 */
async function setUpVault(): Promise<void> {
  await vault("sys/auth/userpass", {
    method: "POST",
    body: JSON.stringify({ type: "userpass" }),
  });

  const policy = [
    `path "identity/oidc/token/${ROLE}" { capabilities = ["read"] }`,
    'path "auth/token/revoke-self" { capabilities = ["update"] }',
    'path "auth/token/lookup-self" { capabilities = ["read"] }',
  ].join("\n");
  await vault("sys/policies/acl/ov-dash", {
    method: "PUT",
    body: JSON.stringify({ policy }),
  });

  await vault(`auth/userpass/users/${USER}`, {
    method: "POST",
    body: JSON.stringify({ password: PASSWORD, policies: "default,ov-dash" }),
  });

  const mounts = await json<Record<string, { accessor: string }>>(
    await vault("sys/auth").then(async (r) => {
      const body = (await r.json()) as { data: Record<string, { accessor: string }> };
      return new Response(JSON.stringify(body.data));
    }),
  );
  const accessor = mounts["userpass/"]?.accessor;
  if (!accessor) throw new Error("userpass mount has no accessor");

  const entity = await json<{ data?: { id: string } }>(
    await vault("identity/entity", {
      method: "POST",
      body: JSON.stringify({
        name: USER,
        metadata: { ov_account: "lab", ov_user: USER },
      }),
    }),
  );
  // Vault answers 204 with no body when the entity already exists, so the id is
  // looked up rather than assumed.
  const id =
    entity.data?.id ??
    (await json<{ data: { id: string } }>(await vault(`identity/entity/name/${USER}`)))
      .data.id;

  await vault("identity/entity-alias", {
    method: "POST",
    body: JSON.stringify({ name: USER, canonical_id: id, mount_accessor: accessor }),
  });

  await vault("identity/oidc/key/ovkey", {
    method: "POST",
    body: JSON.stringify({
      allowed_client_ids: ["*"],
      rotation_period: "24h",
      verification_ttl: "24h",
    }),
  });

  const template = Buffer.from(
    '{"ov_account": {{identity.entity.metadata.ov_account}}, "ov_user": {{identity.entity.metadata.ov_user}}}',
  ).toString("base64");
  await vault(`identity/oidc/role/${ROLE}`, {
    method: "POST",
    body: JSON.stringify({ key: "ovkey", ttl: "1h", template }),
  });

  // ── the browser login: a client, and a scope that says who you are ──
  //
  // The same template as the role's. Nothing shares it between the two
  // features: a provider with no custom scope issues a token carrying neither
  // claim, which is exactly the sign-in this setup has to be able to reproduce.
  await vault(`identity/oidc/scope/${SCOPE}`, {
    method: "POST",
    body: JSON.stringify({ template, description: "OpenViking identity" }),
  });
  await vault(`identity/oidc/assignment/${PROVIDER}`, {
    method: "POST",
    body: JSON.stringify({ entity_ids: [id] }),
  });

  // Deleted first: Vault refuses to move an existing client to another key, so
  // a container left over from a different setup would fail every run after.
  await vault(`identity/oidc/client/${PROVIDER}`, { method: "DELETE" });
  await vault(`identity/oidc/client/${PROVIDER}`, {
    method: "POST",
    body: JSON.stringify({
      key: "ovkey",
      redirect_uris: [`${ORIGIN}/auth/callback`],
      assignments: [PROVIDER],
      client_type: "confidential",
      id_token_ttl: "30m",
      access_token_ttl: "30m",
    }),
  });
  const client = await json<{ data: { client_id: string; client_secret: string } }>(
    await vault(`identity/oidc/client/${PROVIDER}`),
  );
  clientId = client.data.client_id;
  clientSecret = client.data.client_secret;

  await vault(`identity/oidc/provider/${PROVIDER}`, {
    method: "POST",
    body: JSON.stringify({
      allowed_client_ids: [clientId],
      scopes_supported: [SCOPE],
    }),
  });
}

function dashboard() {
  return createApp(
    buildServices(
      loadConfig({
        OV_URL: "http://openviking.invalid:1933",
        PUBLIC_ORIGIN: ORIGIN,
        SESSION_SECRET: "a".repeat(32),
        AUTH_MODE: "vault-userpass",
        VAULT_ADDR: VAULT,
        VAULT_OIDC_ROLE: ROLE,
        SESSION_COOKIE_SECURE: "false",
      }),
    ),
  );
}

/** The same dashboard, signing people in by sending them to Vault. */
function redirectDashboard() {
  return createApp(
    buildServices(
      loadConfig({
        OV_URL: "http://openviking.invalid:1933",
        PUBLIC_ORIGIN: ORIGIN,
        SESSION_SECRET: "a".repeat(32),
        AUTH_MODE: "vault-oidc",
        OIDC_ISSUER: `${VAULT}/v1/identity/oidc/provider/${PROVIDER}`,
        OIDC_CLIENT_ID: clientId,
        OIDC_CLIENT_SECRET: clientSecret,
        SESSION_COOKIE_SECURE: "false",
      }),
    ),
  );
}

/** Pick one cookie out of a response; the callback sets two. */
function cookieNamed(response: Response, name: string): string {
  for (const header of response.headers.getSetCookie()) {
    const pair = header.split(";")[0] ?? "";
    if (pair.startsWith(`${name}=`) && pair !== `${name}=`) return pair;
  }
  return "";
}

/**
 * Do what the browser does between `/auth/login` and `/auth/callback`.
 *
 * The dashboard sends people to Vault's UI, which authenticates them and then
 * makes this very request with the parameters it was given. Here the person's
 * Vault token stands in for that session, so the code is a real one: Vault
 * checks the client, the redirect URI, the assignment and the PKCE challenge
 * before issuing it.
 */
async function authorizeAt(location: string, token: string) {
  const query = new URL(location).searchParams;
  const response = await fetch(
    `${VAULT}/v1/identity/oidc/provider/${PROVIDER}/authorize?${query}`,
    { headers: { "x-vault-token": token } },
  );
  const body = await json<{ code?: string; state?: string; error_description?: string }>(
    response,
  );
  if (!body.code) {
    throw new Error(`Vault would not authorize: ${JSON.stringify(body)}`);
  }
  return body as { code: string; state: string };
}

/** Log the test user into Vault and return their token. */
async function vaultToken(): Promise<string> {
  const login = await json<{ auth: { client_token: string } }>(
    await fetch(`${VAULT}/v1/auth/userpass/login/${USER}`, {
      method: "POST",
      body: JSON.stringify({ password: PASSWORD }),
    }),
  );
  return login.auth.client_token;
}

/** Sign in through the dashboard's own route and return its cookie. */
async function signIn(app: ReturnType<typeof dashboard>): Promise<string> {
  const response = await app.request("/auth/vault-login", {
    method: "POST",
    headers: { origin: ORIGIN, "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ username: USER, password: PASSWORD }),
  });
  if (response.status !== 200) {
    throw new Error(`sign-in failed: ${response.status} ${await response.text()}`);
  }
  return (response.headers.get("set-cookie") ?? "").split(";")[0] ?? "";
}

beforeAll(async () => {
  const health = await fetch(`${VAULT}/v1/sys/health`).catch(() => null);
  if (!health?.ok) {
    throw new Error(
      `no Vault at ${VAULT}. Start one: docker run -d --name ovdash-vault -p 18200:8200 -e VAULT_DEV_ROOT_TOKEN_ID=root hashicorp/vault:latest`,
    );
  }
  await setUpVault();
}, 30_000);

describe("against a real Vault", () => {
  it("signs in, and the token carries the claims OpenViking maps identity from", async () => {
    const app = dashboard();
    const response = await app.request("/auth/vault-login", {
      method: "POST",
      headers: { origin: ORIGIN, "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ username: USER, password: PASSWORD }),
    });

    expect(response.status).toBe(200);
    const body = (await response.json()) as { viewer: { account: string; user: string } };
    // Read back out of the minted token by the dashboard, not assumed by it.
    expect(body.viewer).toMatchObject({ account: "lab", user: USER });
  });

  it("answers both ways of being wrong with the same words", async () => {
    // Vault distinguishes a bad password from an unknown user, which tells an
    // attacker which half they got right. The person signing in cannot act on
    // that difference, so both must come back identical from here — and only a
    // real Vault produces the two different answers being flattened.
    const app = dashboard();
    const refusal = async (username: string, password: string) => {
      const response = await app.request("/auth/vault-login", {
        method: "POST",
        headers: { origin: ORIGIN, "content-type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ username, password }),
      });
      const body = (await response.json()) as { error: { message: string } };
      return { status: response.status, message: body.error.message };
    };

    const badPassword = await refusal(USER, "wrong");
    const noSuchUser = await refusal("nobody-at-all", "wrong");

    expect(badPassword.status).toBe(401);
    expect(badPassword).toEqual(noSuchUser);
    expect(badPassword.message).toMatch(/were not accepted/);
  });

  it("hands the Vault login token back, and Vault agrees it is dead", async () => {
    // The dashboard revokes inside the sign-in, so the token it used is not
    // observable from here. Do what it does, then check Vault's own answer.
    const login = await json<{ auth: { client_token: string } }>(
      await fetch(`${VAULT}/v1/auth/userpass/login/${USER}`, {
        method: "POST",
        body: JSON.stringify({ password: PASSWORD }),
      }),
    );
    const token = login.auth.client_token;

    const minted = await json<{ data: { token: string } }>(
      await vault(`identity/oidc/token/${ROLE}`, { token }),
    );

    expect((await vault("auth/token/lookup-self", { token })).status).toBe(200);
    expect((await vault("auth/token/revoke-self", { method: "POST", token })).ok).toBe(
      true,
    );
    expect((await vault("auth/token/lookup-self", { token })).status).toBe(403);

    // The claim the whole revoke rests on: killing the parent must not kill the
    // credential the session runs on.
    const introspected = await json<{ active: boolean }>(
      await vault("identity/oidc/introspect", {
        method: "POST",
        body: JSON.stringify({ token: minted.data.token }),
      }),
    );
    expect(introspected.active).toBe(true);
  });

  it("signing out kills a cookie copied beforehand", async () => {
    const app = dashboard();
    const cookie = await signIn(app);

    const before = (await (
      await app.request("/api/session", { headers: { cookie } })
    ).json()) as { signedIn: boolean };
    expect(before.signedIn).toBe(true);

    const out = await app.request("/auth/logout", {
      method: "POST",
      headers: { origin: ORIGIN, cookie },
    });
    expect(out.status).toBe(204);

    // The copy. Same bytes the browser had a moment ago.
    const after = (await (
      await app.request("/api/session", { headers: { cookie } })
    ).json()) as { signedIn: boolean };
    expect(after.signedIn).toBe(false);
  });

  it("signing one person out leaves another signed in", async () => {
    const app = dashboard();
    const first = await signIn(app);
    const second = await signIn(app);

    await app.request("/auth/logout", {
      method: "POST",
      headers: { origin: ORIGIN, cookie: first },
    });

    const survivor = (await (
      await app.request("/api/session", { headers: { cookie: second } })
    ).json()) as { signedIn: boolean };
    expect(survivor.signedIn).toBe(true);
  });

  it("mints a token whose lifetime the session respects", async () => {
    const app = dashboard();
    const cookie = await signIn(app);
    const body = (await (
      await app.request("/api/session", { headers: { cookie } })
    ).json()) as { expiresAt: number };

    const now = Math.floor(Date.now() / 1000);
    expect(body.expiresAt).toBeGreaterThan(now);
    // The role's ttl is an hour, and nothing may quietly outlive it.
    expect(body.expiresAt).toBeLessThanOrEqual(now + 3600);
  });
});

describe("signing in by being sent to Vault", () => {
  it("comes back with the token OpenViking accepts, and knows who it is for", async () => {
    const app = redirectDashboard();

    const start = await app.request("/auth/login?returnTo=%2F%23%2Fhome");
    expect(start.status).toBe(302);
    const location = start.headers.get("location") ?? "";
    // Vault's own login page, not a form of ours.
    expect(location).toContain(`/identity/oidc/provider/${PROVIDER}/authorize`);
    expect(new URL(location).searchParams.get("scope")).toBe(`openid ${SCOPE}`);

    const pending = cookieNamed(start, "ovdash_session_login");
    const { code, state } = await authorizeAt(location, await vaultToken());

    const back = await app.request(`/auth/callback?code=${code}&state=${state}`, {
      headers: { cookie: pending },
    });
    expect(back.status).toBe(302);
    expect(back.headers.get("location")).toBe("/#/home");

    const cookie = cookieNamed(back, "ovdash_session");
    const session = (await (
      await app.request("/api/session", { headers: { cookie } })
    ).json()) as {
      signedIn: boolean;
      canSignOut: boolean;
      expiresAt: number;
      viewer: { account: string; user: string };
    };
    expect(session.signedIn).toBe(true);
    expect(session.canSignOut).toBe(true);
    // Vault's own templating produced these, through the scope rather than the
    // role — a different feature, and the one thing a stub cannot vouch for.
    expect(session.viewer).toMatchObject({ account: "lab", user: USER });

    // The client's id_token_ttl is 30 minutes, and no refresh grant exists to
    // extend it, so the session must not pretend to last longer.
    const now = Math.floor(Date.now() / 1000);
    expect(session.expiresAt).toBeGreaterThan(now);
    expect(session.expiresAt).toBeLessThanOrEqual(now + 1800);
  });

  it("refuses to redeem the same authorization code twice", async () => {
    // The login cookie is a signed JWT, so presenting it again verifies again.
    // What actually stops a replayed callback is Vault refusing the code, which
    // only a real provider can be held to.
    const app = redirectDashboard();
    const start = await app.request("/auth/login");
    const pending = cookieNamed(start, "ovdash_session_login");
    const { code, state } = await authorizeAt(
      start.headers.get("location") ?? "",
      await vaultToken(),
    );

    const first = await app.request(`/auth/callback?code=${code}&state=${state}`, {
      headers: { cookie: pending },
    });
    expect(first.status).toBe(302);

    const replay = await app.request(`/auth/callback?code=${code}&state=${state}`, {
      headers: { cookie: pending },
    });
    expect(replay.status).toBeGreaterThanOrEqual(400);
    expect(cookieNamed(replay, "ovdash_session")).toBe("");
  });

  it("signs nobody in from a code whose login was never started here", async () => {
    // No login cookie: nothing holds the state, the nonce or the verifier, so
    // there is nothing to check the provider's answer against. A real code from
    // a real Vault, and it still gets the sign-in page rather than a session.
    const app = redirectDashboard();
    const start = await app.request("/auth/login");
    const { code, state } = await authorizeAt(
      start.headers.get("location") ?? "",
      await vaultToken(),
    );

    const back = await app.request(`/auth/callback?code=${code}&state=${state}`);
    expect(back.status).toBe(302);
    expect(back.headers.get("location")).toBe("/");
    expect(cookieNamed(back, "ovdash_session")).toBe("");
  });

  it("has no password form to post at", async () => {
    const response = await redirectDashboard().request("/auth/vault-login", {
      method: "POST",
      headers: { origin: ORIGIN, "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ username: USER, password: PASSWORD }),
    });
    expect(response.status).toBe(404);
  });
});
