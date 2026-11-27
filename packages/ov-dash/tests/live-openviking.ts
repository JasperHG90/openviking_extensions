/**
 * A real OpenViking for the integration suite, trusting the suite's Vault.
 *
 * The dashboard signs people in through Vault and hands OpenViking the
 * identity token it minted. The lab's OpenViking runs in `oidc` mode, checking
 * that token against Vault's keys. This starts OpenViking the same way, in a
 * container, pointed at the dev-mode Vault the rest of the integration suite
 * already uses (`just vault-up`, or CI's step).
 *
 * It sets up its own Vault user, entity and identity-token role instead of
 * sharing `vault-live.integration.ts`'s. Vitest runs the two files at once,
 * and the role OpenViking accepts needs `client_id = "openviking"` (the
 * audience the lab checks), which the other file's role does not set.
 *
 * Not obvious, each learned by watching the container refuse:
 *
 * - OpenViking will not start without an embedding provider, so a stub
 *   OpenAI-compatible endpoint runs in this process. Nothing here depends on
 *   what it answers.
 * - The token's `iss` is the address Vault was told it lives at, which the
 *   container cannot reach. So OpenViking gets the issuer as Vault states it
 *   and the key set at `host.docker.internal`, which `--add-host` makes
 *   resolve on Linux too.
 */

import { execFile } from "node:child_process";
import { type Server, createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { promisify } from "node:util";

const run = promisify(execFile);

/** The OpenViking release the dashboard is checked against. */
export const IMAGE = process.env.OV_TEST_IMAGE ?? "ghcr.io/volcengine/openviking:v0.5.6";
const CONTAINER = "ovdash-integration-openviking";
const DIMENSION = 1024;
const BOOT_TIMEOUT_MS = 180_000;

/** The Vault login and role this suite owns. */
export const LOGIN = {
  username: "ov-live",
  password: "hunter2",
  role: "openviking-live",
};
/** The identity the role's tokens carry, which is who OpenViking sees. */
export const IDENTITY = { account: "lab", user: "jasper" };
/** The redirect sign-in's provider, client and JWT mount, named for this suite. */
export const REDIRECT = {
  provider: "ovdash-live",
  mount: "jwt-live",
  jwtRole: "ov-dash-live",
};

/** What Vault generated while setting up the redirect sign-in. */
export interface RedirectSetup {
  issuer: string;
  clientId: string;
  clientSecret: string;
}

/** A running OpenViking, and how to stop it. */
export interface LiveOpenViking {
  url: string;
  stop: () => Promise<void>;
}

/** Whether a container runtime answers. */
export async function hasDocker(): Promise<boolean> {
  try {
    await run("docker", ["info", "--format", "{{.ID}}"], { timeout: 15_000 });
    return true;
  } catch {
    return false;
  }
}

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const probe = createServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const { port } = probe.address() as AddressInfo;
      probe.close(() => resolve(port));
    });
  });
}

/** Embeddings and chat completions, OpenAI-shaped, with fixed answers. */
function startModelStub(): Promise<Server> {
  const server = createServer((request, response) => {
    let body = "";
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      let inputs: unknown = [""];
      try {
        inputs = (JSON.parse(body || "{}") as { input?: unknown }).input ?? [""];
      } catch {
        // A malformed body still gets an answer.
      }
      const texts = Array.isArray(inputs) ? inputs : [inputs];
      const payload = request.url?.endsWith("/embeddings")
        ? {
            object: "list",
            model: "stub",
            data: texts.map((text, index) => ({
              object: "embedding",
              index,
              // Varied a little by the text, so a vector search has an order.
              embedding: new Array(DIMENSION)
                .fill(0)
                .map((_, i) => ((String(text).charCodeAt(i % 8) || 1) % 13) / 13),
            })),
            usage: { prompt_tokens: 1, total_tokens: 1 },
          }
        : {
            id: "stub",
            object: "chat.completion",
            model: "stub",
            choices: [
              {
                index: 0,
                finish_reason: "stop",
                message: { role: "assistant", content: "A stub summary." },
              },
            ],
            usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
          };
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify(payload));
    });
  });
  // Every interface: the container reaches it through the host gateway.
  return new Promise((resolve) => server.listen(0, "0.0.0.0", () => resolve(server)));
}

async function vault(
  addr: string,
  path: string,
  init: { method?: string; body?: unknown; token?: string } = {},
): Promise<Record<string, unknown>> {
  const response = await fetch(`${addr}/v1/${path}`, {
    method: init.method ?? "POST",
    headers: {
      "x-vault-token": init.token ?? process.env.VAULT_TEST_ROOT_TOKEN ?? "root",
      "content-type": "application/json",
    },
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
  });
  const text = await response.text();
  // The one refusal let through: enabling an auth mount that is already there.
  const remount = response.status === 400 && path.startsWith("sys/auth/");
  if (!response.ok && !remount) {
    throw new Error(`Vault ${path} failed: ${response.status} ${text}`);
  }
  return text ? (JSON.parse(text) as Record<string, unknown>) : {};
}

/**
 * Give the suite a Vault login whose minted token OpenViking accepts, and the
 * provider the lab's redirect sign-in goes through.
 *
 * Idempotent, so a re-run against the same Vault container works. A 400 from
 * enabling a mount that is already enabled is the one refusal let through.
 *
 * @param origin - Where the dashboard under test says it is served from.
 */
export async function setUpVault(addr: string, origin: string): Promise<RedirectSetup> {
  await vault(addr, "sys/auth/userpass", { body: { type: "userpass" } });
  await vault(addr, `sys/policies/acl/${LOGIN.role}`, {
    method: "PUT",
    body: {
      policy: [
        `path "identity/oidc/token/${LOGIN.role}" { capabilities = ["read"] }`,
        // The dashboard hands its Vault login back right after minting.
        'path "auth/token/revoke-self" { capabilities = ["update"] }',
      ].join("\n"),
    },
  });
  await vault(addr, `auth/userpass/users/${LOGIN.username}`, {
    body: { password: LOGIN.password, policies: `default,${LOGIN.role}` },
  });
  const mounts = (await vault(addr, "sys/auth", { method: "GET" })).data as Record<
    string,
    { accessor: string }
  >;
  await vault(addr, "identity/entity", {
    body: {
      name: LOGIN.username,
      metadata: { ov_account: IDENTITY.account, ov_user: IDENTITY.user },
    },
  });
  const entity = (await vault(addr, `identity/entity/name/${LOGIN.username}`, {
    method: "GET",
  })) as { data: { id: string } };
  await vault(addr, "identity/entity-alias", {
    body: {
      name: LOGIN.username,
      canonical_id: entity.data.id,
      mount_accessor: mounts["userpass/"]?.accessor,
    },
  });
  await vault(addr, `identity/oidc/key/${LOGIN.role}`, {
    body: { allowed_client_ids: ["*"], rotation_period: "24h", verification_ttl: "24h" },
  });
  const template =
    '{"ov_account":{{identity.entity.metadata.ov_account}},"ov_user":{{identity.entity.metadata.ov_user}}}';
  // Deleted first: a role's client_id is fixed when it is created.
  await vault(addr, `identity/oidc/role/${LOGIN.role}`, { method: "DELETE" });
  await vault(addr, `identity/oidc/role/${LOGIN.role}`, {
    body: {
      key: LOGIN.role,
      client_id: "openviking",
      ttl: "1h",
      template: Buffer.from(template).toString("base64"),
    },
  });
  return setUpRedirect(addr, origin, entity.data.id, template);
}

/**
 * The `vault-oidc` sign-in: a provider issuing an ID token with the same
 * claims, and a JWT mount that trades it back for a Vault login.
 */
async function setUpRedirect(
  addr: string,
  origin: string,
  entityId: string,
  template: string,
): Promise<RedirectSetup> {
  const { provider, mount, jwtRole } = REDIRECT;
  await vault(addr, `identity/oidc/scope/${provider}`, {
    body: { template: Buffer.from(template).toString("base64") },
  });
  await vault(addr, `identity/oidc/assignment/${provider}`, {
    body: { entity_ids: [entityId] },
  });
  // Deleted first: Vault refuses to move an existing client to another key.
  await vault(addr, `identity/oidc/client/${provider}`, { method: "DELETE" });
  await vault(addr, `identity/oidc/client/${provider}`, {
    body: {
      key: LOGIN.role,
      redirect_uris: [`${origin}/auth/callback`],
      assignments: [provider],
      client_type: "confidential",
    },
  });
  const client = (await vault(addr, `identity/oidc/client/${provider}`, {
    method: "GET",
  })) as { data: { client_id: string; client_secret: string } };
  await vault(addr, `identity/oidc/provider/${provider}`, {
    body: { allowed_client_ids: [client.data.client_id], scopes_supported: [provider] },
  });
  const issuer = (
    (await (
      await fetch(
        `${addr}/v1/identity/oidc/provider/${provider}/.well-known/openid-configuration`,
      )
    ).json()) as { issuer: string }
  ).issuer;

  await vault(addr, `sys/auth/${mount}`, { body: { type: "jwt" } });
  await vault(addr, `auth/${mount}/config`, {
    body: {
      // Fetched by Vault from inside its own container, where the host's
      // mapped port does not exist.
      jwks_url: `http://127.0.0.1:8200/v1/identity/oidc/provider/${provider}/.well-known/keys`,
      bound_issuer: issuer,
    },
  });
  await vault(addr, `auth/${mount}/role/${jwtRole}`, {
    body: {
      role_type: "jwt",
      user_claim: "ov_user",
      bound_audiences: [client.data.client_id],
      token_policies: [LOGIN.role],
      token_type: "service",
      token_ttl: "5m",
    },
  });
  const mounts = (await vault(addr, "sys/auth", { method: "GET" })).data as Record<
    string,
    { accessor: string }
  >;
  // Ties the JWT login to the entity carrying the metadata. Without it Vault
  // invents an entity with none, and the minted claims come back empty.
  await vault(addr, "identity/entity-alias", {
    body: {
      name: IDENTITY.user,
      canonical_id: entityId,
      mount_accessor: mounts[`${mount}/`]?.accessor,
    },
  });
  return {
    issuer,
    clientId: client.data.client_id,
    clientSecret: client.data.client_secret,
  };
}

/** Start OpenViking in `oidc` mode, trusting the Vault at `vaultAddr`. */
export async function startOpenViking(vaultAddr: string): Promise<LiveOpenViking> {
  const stub = await startModelStub();
  const modelPort = (stub.address() as AddressInfo).port;
  const port = await freePort();
  const vaultPort = new URL(vaultAddr).port || "80";
  const discovery = (await (
    await fetch(`${vaultAddr}/v1/identity/oidc/.well-known/openid-configuration`)
  ).json()) as { issuer: string };

  const modelBase = `http://host.docker.internal:${modelPort}/v1`;
  const config = {
    server: {
      host: "0.0.0.0",
      port: 1933,
      auth_mode: "oidc",
      root_api_key: `root-${crypto.randomUUID()}`,
      oidc: {
        issuer: discovery.issuer,
        audience: "openviking",
        jwks_uri: `http://host.docker.internal:${vaultPort}/v1/identity/oidc/.well-known/keys`,
        identity: {
          account_id: { source: "claim", claim: "ov_account" },
          user_id: { source: "claim", claim: "ov_user" },
        },
      },
    },
    embedding: {
      dense: {
        provider: "openai",
        model: "stub",
        api_key: "stub",
        api_base: modelBase,
        dimension: DIMENSION,
        input: "text",
      },
    },
    vlm: { provider: "openai", model: "stub", api_key: "stub", api_base: modelBase },
  };

  await run("docker", ["rm", "-f", CONTAINER]).catch(() => undefined);
  await run("docker", [
    "run",
    "-d",
    "--name",
    CONTAINER,
    "--add-host",
    "host.docker.internal:host-gateway",
    "-p",
    `127.0.0.1:${port}:1933`,
    "-e",
    "OPENVIKING_WITH_BOT=0",
    "-e",
    `OPENVIKING_CONF_CONTENT=${JSON.stringify(config)}`,
    IMAGE,
  ]);

  const url = `http://127.0.0.1:${port}`;
  const stop = async () => {
    stub.close();
    if (!process.env.OV_TEST_KEEP) {
      await run("docker", ["rm", "-f", CONTAINER]).catch(() => undefined);
    }
  };
  const deadline = Date.now() + BOOT_TIMEOUT_MS;
  while (Date.now() < deadline) {
    const healthy = await fetch(`${url}/health`, { signal: AbortSignal.timeout(3000) })
      .then((r) => r.ok)
      .catch(() => false);
    if (healthy) return { url, stop };
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  const { stdout } = await run("docker", ["logs", "--tail", "30", CONTAINER]).catch(
    () => ({ stdout: "(no logs)" }),
  );
  await stop();
  throw new Error(`OpenViking (${IMAGE}) did not come up:\n${stdout}`);
}
