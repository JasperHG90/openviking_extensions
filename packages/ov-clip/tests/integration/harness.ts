/**
 * A real OpenViking to test against, signed into the way the extension is.
 *
 * Points at one you are already running when `$OV_TEST_URL` and
 * `$OV_TEST_KEY` are set. Otherwise starts throwaway containers, which is what
 * makes this runnable on a laptop with nothing set up. With neither, the suite
 * skips rather than fails, so `npm test` stays green anywhere.
 *
 * The extension holds a Vault identity token that ovx minted, and the lab's
 * OpenViking runs in `oidc` mode trusting Vault. So this starts both: a
 * dev-mode Vault whose `openviking` role puts `ov_account` and `ov_user` in
 * its tokens the way the lab's does, and OpenViking in `oidc` mode trusting
 * it. The suite's credential is a token minted from that role.
 *
 * Things the containers need that are not obvious, each learned by watching
 * one refuse to boot or refuse a request:
 *
 * - OpenViking will not start without an embedding provider, and the bundled
 *   local one needs `llama-cpp-python` that the image does not ship. So a stub
 *   OpenAI-compatible endpoint runs in this process and the container is
 *   pointed at it. Nothing here depends on what it answers.
 * - A token's `iss` is the address Vault was told it lives at, `127.0.0.1`
 *   from here, which the OpenViking container cannot reach. So it is given the
 *   issuer as Vault states it and the key set at `host.docker.internal`.
 * - In `oidc` mode the root key is refused like any other non-JWT, so there is
 *   no admin setup: the account exists as soon as a token names it.
 */

import { execFile } from "node:child_process";
import { type Server, createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { promisify } from "node:util";

const run = promisify(execFile);

/** The OpenViking release this package is checked against. */
const IMAGE = process.env.OV_TEST_IMAGE ?? "ghcr.io/volcengine/openviking:v0.5.6";
const VAULT_IMAGE = "hashicorp/vault:2.1.0";
const CONTAINER = "ov-clip-integration";
const VAULT_CONTAINER = "ov-clip-integration-vault";
const ACCOUNT = "lab";
const USER = "jasper";
const PASSWORD = "hunter2";
const DIMENSION = 1024;

/** How long to wait for a container to answer. */
const BOOT_TIMEOUT_MS = 180_000;

export interface Live {
  url: string;
  /** A Vault identity token, which is what the extension holds. */
  key: string;
  /** The account and user the token's claims name, for asserting on paths. */
  account: string;
  user: string;
}

/** The stub embedder, kept so it can be closed again. */
let embedder: Server | null = null;

/** Whether a container runtime is usable. */
async function hasDocker(): Promise<boolean> {
  try {
    await run("docker", ["info", "--format", "{{.ID}}"], { timeout: 15_000 });
    return true;
  } catch {
    return false;
  }
}

/** A port nothing is listening on, so two suites never fight over one. */
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

/**
 * Serve OpenAI-compatible embeddings and chat completions with fixed answers.
 *
 * OpenViking validates the embedder at boot and calls both while indexing.
 * Listens on every interface: the container reaches it through the host
 * gateway, not loopback.
 */
function startEmbedder(): Promise<Server> {
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
        // A malformed body still gets an answer; the server only cares that
        // the endpoint responds.
      }
      const texts = Array.isArray(inputs) ? inputs : [inputs];
      const payload = request.url?.endsWith("/embeddings")
        ? {
            object: "list",
            model: "stub",
            data: texts.map((_, index) => ({
              object: "embedding",
              index,
              embedding: new Array(DIMENSION).fill(0.1),
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
  return new Promise((resolve) => server.listen(0, "0.0.0.0", () => resolve(server)));
}

async function answers(url: string): Promise<boolean> {
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(3000) });
    return response.ok;
  } catch {
    return false;
  }
}

async function waitFor(url: string, container: string, what: string): Promise<void> {
  const deadline = Date.now() + BOOT_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (await answers(url)) return;
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  const { stdout } = await run("docker", ["logs", "--tail", "30", container]).catch(
    () => ({ stdout: "(no logs)" }),
  );
  throw new Error(`${what} did not come up:\n${stdout}`);
}

/** Call Vault and return the parsed body, or throw with what it said. */
async function vault(
  base: string,
  path: string,
  init: { method?: string; body?: unknown; token?: string } = {},
): Promise<Record<string, unknown>> {
  const response = await fetch(`${base}/v1/${path}`, {
    method: init.method ?? "POST",
    headers: {
      "x-vault-token": init.token ?? "root",
      "content-type": "application/json",
    },
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
    signal: AbortSignal.timeout(30_000),
  });
  const text = await response.text();
  if (!response.ok) throw new Error(`Vault ${path} failed: ${response.status} ${text}`);
  return text ? (JSON.parse(text) as Record<string, unknown>) : {};
}

/** Mirror the lab: userpass, an entity carrying ov_* metadata, one role. */
async function configureVault(base: string): Promise<void> {
  await vault(base, "sys/auth/userpass", { body: { type: "userpass" } });
  await vault(base, "sys/policies/acl/openviking", {
    method: "PUT",
    body: { policy: 'path "identity/oidc/token/openviking" { capabilities = ["read"] }' },
  });
  await vault(base, `auth/userpass/users/${USER}`, {
    body: { password: PASSWORD, policies: "default,openviking" },
  });
  const mounts = (await vault(base, "sys/auth", { method: "GET" })).data as Record<
    string,
    { accessor: string }
  >;
  const entity = (await vault(base, "identity/entity", {
    body: { name: USER, metadata: { ov_account: ACCOUNT, ov_user: USER } },
  })) as { data: { id: string } };
  await vault(base, "identity/entity-alias", {
    body: {
      name: USER,
      canonical_id: entity.data.id,
      mount_accessor: mounts["userpass/"]?.accessor,
    },
  });
  await vault(base, "identity/oidc/key/identity_tokens", {
    body: { allowed_client_ids: ["*"], rotation_period: "24h", verification_ttl: "24h" },
  });
  // Unquoted placeholders: Vault emits each substitution as JSON already.
  const template =
    '{"ov_account":{{identity.entity.metadata.ov_account}},"ov_user":{{identity.entity.metadata.ov_user}}}';
  await vault(base, "identity/oidc/role/openviking", {
    body: {
      key: "identity_tokens",
      // The audience the lab's OpenViking checks, set the way the lab sets it.
      client_id: "openviking",
      ttl: "1h",
      template: Buffer.from(template).toString("base64"),
    },
  });
}

/** Log in with userpass and mint the token ovx would hand the extension. */
async function mintToken(base: string): Promise<string> {
  const login = (await vault(base, `auth/userpass/login/${USER}`, {
    body: { password: PASSWORD },
    token: "",
  })) as { auth: { client_token: string } };
  const minted = (await vault(base, "identity/oidc/token/openviking", {
    method: "GET",
    token: login.auth.client_token,
  })) as { data: { token: string } };
  return minted.data.token;
}

/**
 * Bring up Vault and OpenViking and hand back how to reach it as a real user.
 *
 * @returns The live server, or null when there is neither a configured server
 *   nor a container runtime — the caller should skip.
 */
export async function startOpenViking(): Promise<Live | null> {
  const configured = process.env.OV_TEST_URL;
  if (configured && process.env.OV_TEST_KEY) {
    return {
      url: configured.replace(/\/+$/, ""),
      key: process.env.OV_TEST_KEY,
      account: process.env.OV_TEST_ACCOUNT ?? "",
      user: process.env.OV_TEST_USER ?? "",
    };
  }
  if (!(await hasDocker())) return null;

  embedder = await startEmbedder();
  const modelPort = (embedder.address() as AddressInfo).port;
  const vaultPort = await freePort();
  const ovPort = await freePort();
  const vaultUrl = `http://127.0.0.1:${vaultPort}`;

  await run("docker", ["rm", "-f", CONTAINER, VAULT_CONTAINER]).catch(() => undefined);
  await run("docker", [
    "run",
    "-d",
    "--name",
    VAULT_CONTAINER,
    "-p",
    `${vaultPort}:8200`,
    "-e",
    "VAULT_DEV_ROOT_TOKEN_ID=root",
    // The issuer is built from this. Dev mode's default is an address nothing
    // outside the container can use.
    "-e",
    `VAULT_API_ADDR=${vaultUrl}`,
    VAULT_IMAGE,
  ]);
  await waitFor(`${vaultUrl}/v1/sys/health`, VAULT_CONTAINER, "Vault");
  await configureVault(vaultUrl);

  const modelBase = `http://host.docker.internal:${modelPort}/v1`;
  const config = {
    server: {
      port: 1933,
      host: "0.0.0.0",
      auth_mode: "oidc",
      root_api_key: `root-${crypto.randomUUID()}`,
      oidc: {
        issuer: `${vaultUrl}/v1/identity/oidc`,
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

  await run("docker", [
    "run",
    "-d",
    "--name",
    CONTAINER,
    // Docker Desktop resolves host.docker.internal on its own; a Linux daemon
    // does not, and the config above is written in terms of it. Left out, the
    // server boots and answers, then every import hangs on an embedding call
    // that cannot connect: the document never finishes indexing and the
    // destination lock is never let go, so the next import to the same place
    // fails with a 409 nobody can explain from the message.
    "--add-host",
    "host.docker.internal:host-gateway",
    "-p",
    `${ovPort}:1933`,
    "-e",
    "OPENVIKING_WITH_BOT=0",
    "-e",
    `OPENVIKING_CONF_CONTENT=${JSON.stringify(config)}`,
    IMAGE,
  ]);
  const url = `http://127.0.0.1:${ovPort}`;
  await waitFor(`${url}/health`, CONTAINER, `OpenViking (${IMAGE})`);

  return { url, key: await mintToken(vaultUrl), account: ACCOUNT, user: USER };
}

/** Tear down whatever `startOpenViking` brought up. */
export async function stopOpenViking(): Promise<void> {
  embedder?.close();
  embedder = null;
  if (process.env.OV_TEST_URL) return;
  if (process.env.OV_TEST_KEEP) return;
  await run("docker", ["rm", "-f", CONTAINER, VAULT_CONTAINER]).catch(() => undefined);
}

/** List a scope recursively, for asserting on what a save actually wrote. */
export async function listTree(live: Live, uri: string): Promise<string[]> {
  const response = await fetch(
    `${live.url}/api/v1/fs/ls?uri=${encodeURIComponent(uri)}&recursive=true&node_limit=100`,
    { headers: { "x-api-key": live.key }, signal: AbortSignal.timeout(30_000) },
  );
  const parsed = (await response.json()) as { result?: { uri: string }[] };
  return (parsed.result ?? []).map((entry) => entry.uri).sort();
}
