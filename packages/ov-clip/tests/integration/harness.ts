/**
 * A real OpenViking to test against.
 *
 * Points at one you are already running when `$OV_TEST_URL` and
 * `$OV_TEST_KEY` are set. Otherwise starts a throwaway container, which is
 * what makes this runnable on a laptop with nothing set up. With neither, the
 * suite skips rather than fails, so `npm test` stays green anywhere.
 *
 * Two things the container needs that are not obvious, both learned by
 * watching it refuse to boot:
 *
 * - `dev` auth mode refuses to bind anything but localhost, so this uses
 *   `api_key` with a root key — which is the path the extension exercises
 *   anyway.
 * - It will not start without an embedding provider, and the bundled local one
 *   needs `llama-cpp-python` that the image does not ship. So a stub
 *   OpenAI-compatible endpoint runs in this process and the container is
 *   pointed at it. Nothing here embeds anything meaningfully; it exists so the
 *   server boots.
 */

import { execFile } from "node:child_process";
import { type Server, createServer } from "node:http";
import { promisify } from "node:util";

const run = promisify(execFile);

/** Pinned: the image the deployment builds from. */
const IMAGE = process.env.OV_TEST_IMAGE ?? "ghcr.io/volcengine/openviking:v0.4.17.1";
const CONTAINER = "ov-clip-integration";
const OV_PORT = 19330;
const EMBED_PORT = 19331;
const ROOT_KEY = "ov-clip-integration-root";
const ACCOUNT = "acme";
const USER = "jasper";

/** How long to wait for the server to answer /health. */
const BOOT_TIMEOUT_MS = 180_000;

export interface Live {
  url: string;
  /** A user key, not the root key: root cannot touch tenant-scoped data. */
  key: string;
  /** The account and user the key resolves to, for asserting on paths. */
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

/**
 * Serve an OpenAI-compatible embeddings endpoint that returns fixed vectors.
 *
 * OpenViking validates the shape at boot and calls it while indexing; nothing
 * in these tests depends on the values being meaningful.
 */
function startEmbedder(dimension: number): Promise<Server> {
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
        // A malformed body still gets one vector back; the server only cares
        // that the endpoint answers.
      }
      const texts = Array.isArray(inputs) ? inputs : [inputs];
      const payload = JSON.stringify({
        object: "list",
        model: "stub",
        data: texts.map((_, index) => ({
          object: "embedding",
          index,
          embedding: new Array(dimension).fill(0.1),
        })),
        usage: { prompt_tokens: 1, total_tokens: 1 },
      });
      response.writeHead(200, { "content-type": "application/json" });
      response.end(payload);
    });
  });
  return new Promise((resolve) => server.listen(EMBED_PORT, () => resolve(server)));
}

async function healthy(url: string): Promise<boolean> {
  try {
    const response = await fetch(`${url}/health`, { signal: AbortSignal.timeout(3000) });
    return response.ok;
  } catch {
    return false;
  }
}

/** Call an admin endpoint with the root key and return its `result`. */
async function admin(
  url: string,
  path: string,
  body: unknown,
): Promise<Record<string, unknown>> {
  const response = await fetch(`${url}${path}`, {
    method: "POST",
    headers: { "x-api-key": ROOT_KEY, "content-type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(30_000),
  });
  const parsed = (await response.json()) as {
    result?: unknown;
    error?: { message?: string };
  };
  if (!response.ok || parsed.error) {
    throw new Error(`${path} failed: ${parsed.error?.message ?? response.status}`);
  }
  return (parsed.result ?? {}) as Record<string, unknown>;
}

/**
 * Bring up an OpenViking and hand back how to reach it as a real user.
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

  embedder = await startEmbedder(1024);

  const config = {
    server: {
      port: 1933,
      host: "0.0.0.0",
      auth_mode: "api_key",
      root_api_key: ROOT_KEY,
    },
    embedding: {
      dense: {
        provider: "openai",
        model: "stub",
        api_key: "stub",
        api_base: `http://host.docker.internal:${EMBED_PORT}/v1`,
        dimension: 1024,
        input: "text",
      },
    },
  };

  await run("docker", ["rm", "-f", CONTAINER]).catch(() => undefined);
  await run("docker", [
    "run",
    "-d",
    "--name",
    CONTAINER,
    // Docker Desktop resolves host.docker.internal on its own; a Linux daemon
    // does not, and the embedding config above is written in terms of it. Left
    // out, the server boots and answers, then every import hangs on an
    // embedding call that cannot connect: the document never finishes indexing
    // and the destination lock is never let go, so the next import to the same
    // place fails with a 409 nobody can explain from the message.
    "--add-host",
    "host.docker.internal:host-gateway",
    "-p",
    `${OV_PORT}:1933`,
    "-e",
    "OPENVIKING_WITH_BOT=0",
    "-e",
    `OPENVIKING_CONF_CONTENT=${JSON.stringify(config)}`,
    IMAGE,
  ]);

  const url = `http://127.0.0.1:${OV_PORT}`;
  const deadline = Date.now() + BOOT_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (await healthy(url)) break;
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  if (!(await healthy(url))) {
    const { stdout } = await run("docker", ["logs", "--tail", "30", CONTAINER]).catch(
      () => ({ stdout: "(no logs)" }),
    );
    throw new Error(`OpenViking did not become healthy:\n${stdout}`);
  }

  // The root key cannot touch tenant-scoped data — the server says so in as
  // many words — so an account with an admin user is created and its key used.
  const created = await admin(url, "/api/v1/admin/accounts", {
    account_id: ACCOUNT,
    name: ACCOUNT,
    admin_user_id: USER,
  });
  const key = created.user_key;
  if (typeof key !== "string" || !key) {
    throw new Error("account was created without a user key");
  }
  return { url, key, account: ACCOUNT, user: USER };
}

/** Tear down whatever `startOpenViking` brought up. */
export async function stopOpenViking(): Promise<void> {
  embedder?.close();
  embedder = null;
  if (process.env.OV_TEST_URL) return;
  if (process.env.OV_TEST_KEEP) return;
  await run("docker", ["rm", "-f", CONTAINER]).catch(() => undefined);
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
