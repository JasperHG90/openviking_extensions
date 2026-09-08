/**
 * Signing in and out against a real Vault.
 *
 * The unit suite stubs `fetch`, which proves the dashboard sends what we think
 * it sends and nothing about what Vault does with it. Three claims here are
 * about Vault's behaviour, not ours, and a stub cannot settle any of them:
 *
 * 1. `revoke-self` really kills the login token.
 * 2. The minted identity token survives that revocation. If it did not, every
 *    sign-in would be broken the moment the revoke landed — and the unit tests
 *    would still pass, because the stub answers whatever we tell it to.
 * 3. Vault's claim templating produces the `ov_account` / `ov_user` the
 *    dashboard reads identity from.
 *
 * Plus the one thing that is ours and matters most: a cookie copied before a
 * sign-out is worthless after it.
 *
 * Not part of `npm test`. Run it against a throwaway Vault:
 *
 *   docker run -d --name ovdash-vault -p 18200:8200 \
 *     -e VAULT_DEV_ROOT_TOKEN_ID=root hashicorp/vault:latest
 *   npm run test:integration
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

  it("reports a session that ends no later than the minted token", async () => {
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
