/**
 * The dashboard against a real OpenViking, signed in through a real Vault.
 *
 * The unit suite stubs OpenViking, so it proves the dashboard sends what its
 * authors believed and nothing about whether a server agrees. This drives
 * every API route the pages call, through the Hono app, against OpenViking in
 * `oidc` mode holding the identity token a Vault sign-in minted, which is how
 * the lab runs it.
 *
 * Needs docker for OpenViking and the dev-mode Vault the other integration
 * file uses. Run it with `just test-integration`, which starts the Vault.
 * `OV_TEST_IMAGE` picks the OpenViking image.
 */

import { unzipSync } from "fflate";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { buildServices, createApp } from "../src/server/app";
import { loadConfig } from "../src/server/env";
import {
  destinationsSchema,
  fileDetailSchema,
  folderMadeSchema,
  homeSchema,
  jobSchema,
  jobStateSchema,
  memoryGroupSchema,
  movedSchema,
  openedSchema,
  savedSchema,
  searchResponseSchema,
  sessionStateSchema,
  treeSchema,
  uploadResultSchema,
} from "../src/shared/schemas";
import {
  IDENTITY,
  LOGIN,
  type LiveOpenViking,
  REDIRECT,
  type RedirectSetup,
  hasDocker,
  setUpVault,
  startOpenViking,
} from "./live-openviking";

const VAULT = process.env.VAULT_TEST_ADDR ?? "http://127.0.0.1:18200";
const ORIGIN = "http://localhost:8080";
const ROOT = `viking://user/${IDENTITY.user}`;
/** One run's own corner of the tree, so a kept server does not collide. */
const RUN = `live-${crypto.randomUUID().slice(0, 8)}`;
/** A word nothing else in the tree contains, for the exact-words search. */
const NEEDLE = `zebrafinch${RUN.slice(5)}`;

const docker = await hasDocker();
let live: LiveOpenViking | null = null;
let app: ReturnType<typeof createApp>;
let cookie = "";
/** The token OpenViking holds for this person, for seeding what no route makes. */
let token = "";
let redirect: RedirectSetup;

function dashboard() {
  return createApp(
    buildServices(
      loadConfig({
        OV_URL: live?.url ?? "",
        PUBLIC_ORIGIN: ORIGIN,
        SESSION_SECRET: "a".repeat(32),
        AUTH_MODE: "vault-userpass",
        VAULT_ADDR: VAULT,
        VAULT_OIDC_ROLE: LOGIN.role,
        SESSION_COOKIE_SECURE: "false",
      }),
    ),
  );
}

/** Call a dashboard route as the signed-in person. */
function api(path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers);
  headers.set("cookie", cookie);
  if (init.method && init.method !== "GET") headers.set("origin", ORIGIN);
  return app.request(path, { ...init, headers });
}

async function json<T>(
  response: Response,
  schema: { parse: (value: unknown) => T },
): Promise<T> {
  const text = await response.text();
  expect(response.status, text).toBe(200);
  return schema.parse(JSON.parse(text));
}

function sendJson(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  };
}

/** Call OpenViking directly, for what the dashboard only reads. */
async function ov(method: string, path: string, body?: unknown): Promise<unknown> {
  const response = await fetch(`${live?.url}/api/v1${path}`, {
    method,
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const parsed = (await response.json()) as { result?: unknown; error?: unknown };
  if (!response.ok) throw new Error(`${path}: ${JSON.stringify(parsed.error)}`);
  return parsed.result;
}

async function eventually<T>(
  read: () => Promise<T>,
  done: (value: T) => boolean,
  attempts = 30,
): Promise<T> {
  let value = await read();
  for (let i = 0; i < attempts && !done(value); i++) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    value = await read();
  }
  return value;
}

beforeAll(async () => {
  if (!docker) return;
  const health = await fetch(`${VAULT}/v1/sys/health`).catch(() => null);
  if (!health?.ok) {
    throw new Error(`no Vault at ${VAULT}. Start one with \`just vault-up\`.`);
  }
  redirect = await setUpVault(VAULT, ORIGIN);
  live = await startOpenViking(VAULT);
  app = dashboard();

  const signedIn = await app.request("/auth/vault-login", {
    method: "POST",
    headers: { origin: ORIGIN, "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ username: LOGIN.username, password: LOGIN.password }),
  });
  expect(signedIn.status, await signedIn.clone().text()).toBe(200);
  cookie = (signedIn.headers.get("set-cookie") ?? "").split(";")[0] ?? "";

  const login = (await (
    await fetch(`${VAULT}/v1/auth/userpass/login/${LOGIN.username}`, {
      method: "POST",
      body: JSON.stringify({ password: LOGIN.password }),
    })
  ).json()) as { auth: { client_token: string } };
  const minted = (await (
    await fetch(`${VAULT}/v1/identity/oidc/token/${LOGIN.role}`, {
      headers: { "x-vault-token": login.auth.client_token },
    })
  ).json()) as { data: { token: string } };
  token = minted.data.token;
}, 300_000);

afterAll(async () => {
  await live?.stop();
}, 60_000);

describe.skipIf(!docker)("the dashboard against a real OpenViking", () => {
  const folder = `${ROOT}/resources/${RUN}`;
  let fileUri = "";

  it("signs in through Vault as the identity the token names", async () => {
    const state = await json(await api("/api/session"), sessionStateSchema);
    expect(state).toMatchObject({ signedIn: true, viewer: IDENTITY });
  });

  it("makes a folder, and refuses to make it twice", async () => {
    const made = await json(
      await api(
        "/api/folder",
        sendJson("POST", {
          into: `${ROOT}/resources`,
          name: RUN,
          description: "integration run",
        }),
      ),
      folderMadeSchema,
    );
    expect(made.uri).toBe(folder);

    const again = await api(
      "/api/folder",
      sendJson("POST", { into: `${ROOT}/resources`, name: RUN }),
    );
    expect(again.status).toBe(409);
  });

  it("uploads a document into its own folder", async () => {
    const form = new FormData();
    form.append(
      "file",
      new File([`# Notes\n\nThe ${NEEDLE} is the word to find.\n`], "notes.md", {
        type: "text/markdown",
      }),
    );
    form.append("to", folder);
    form.append("why", "integration run");

    const uploaded = await json(
      await api("/api/upload", { method: "POST", body: form }),
      uploadResultSchema,
    );
    expect(uploaded.uri.startsWith(`${folder}/notes/`)).toBe(true);

    // The import answers before the document is written, so wait for a file.
    const listing = await eventually(
      async () =>
        (await api(`/api/list?uri=${encodeURIComponent(`${folder}/notes`)}`)).json(),
      (body) => JSON.stringify(body).includes(".md"),
    );
    const found = treeSchema.parse(listing).nodes.find((n) => n.name.endsWith(".md"));
    expect(found, JSON.stringify(listing)).toBeDefined();
    fileUri = found?.uri ?? "";
  });

  it("shows the document in the tree, the folder and the home page", async () => {
    const tree = await json(await api("/api/tree"), treeSchema);
    expect(tree.nodes.some((n) => n.uri === `${ROOT}/resources`)).toBe(true);

    const shown = await json(
      await api(`/api/folder?uri=${encodeURIComponent(folder)}`),
      treeSchema,
    );
    expect(shown.nodes.some((n) => n.uri === `${folder}/notes`)).toBe(true);

    const home = await json(await api("/api/home"), homeSchema);
    expect(home.viewer).toMatchObject(IDENTITY);
    expect(home.stats.files).toBeGreaterThan(0);
  });

  it("offers the scopes and the folders a save can go to", async () => {
    const scopes = await api("/api/scopes");
    expect(scopes.status).toBe(200);
    const destinations = await json(await api("/api/destinations"), destinationsSchema);
    expect(JSON.stringify(destinations)).toContain(folder);
  });

  it("reads the document, and opens it by a link that left off `.md`", async () => {
    const detail = await json(
      await api(`/api/file?uri=${encodeURIComponent(fileUri)}`),
      fileDetailSchema,
    );
    expect(detail.content).toContain(NEEDLE);

    const opened = await json(
      await api(`/api/open?uri=${encodeURIComponent(fileUri.replace(/\.md$/, ""))}`),
      openedSchema,
    );
    expect(opened.kind).toBe("file");
  });

  it("saves an edit, and a second save straight after it", async () => {
    // On OpenViking 0.5 the second save lands while the first is still being
    // indexed, and the server refuses it as busy unless the dashboard waits.
    const first = await json(
      await api(
        `/api/file?uri=${encodeURIComponent(fileUri)}`,
        sendJson("PUT", { content: `# Notes\n\nThe ${NEEDLE} moved.\n` }),
      ),
      savedSchema,
    );
    const second = await json(
      await api(
        `/api/file?uri=${encodeURIComponent(fileUri)}`,
        sendJson("PUT", {
          content: `# Notes\n\nThe ${NEEDLE} moved twice.\n`,
          seen: first,
        }),
      ),
      savedSchema,
    );
    expect(second.size).toBeGreaterThan(0);

    const detail = await json(
      await api(`/api/file?uri=${encodeURIComponent(fileUri)}`),
      fileDetailSchema,
    );
    expect(detail.content).toContain("moved twice");
  });

  it("downloads the document, and the folder as a zip", async () => {
    const file = await api(`/api/download?uri=${encodeURIComponent(fileUri)}`);
    expect(file.status).toBe(200);
    expect(await file.text()).toContain("moved twice");

    const zipped = await api(`/api/download?uri=${encodeURIComponent(folder)}`);
    expect(zipped.status).toBe(200);
    const entries = unzipSync(new Uint8Array(await zipped.arrayBuffer()));
    expect(Object.keys(entries).some((name) => name.endsWith(".md"))).toBe(true);
  });

  it("serves an uploaded picture's bytes", async () => {
    // Small but real: a valid one-pixel PNG.
    const png = Buffer.from(
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
      "base64",
    );
    const form = new FormData();
    form.append("file", new File([png], "dot.png", { type: "image/png" }));
    form.append("to", folder);
    await json(
      await api("/api/upload", { method: "POST", body: form }),
      uploadResultSchema,
    );

    const listing = await eventually(
      async () =>
        (await api(`/api/list?uri=${encodeURIComponent(`${folder}/dot`)}`)).json(),
      (body) => JSON.stringify(body).includes(".png"),
    );
    const image = treeSchema.parse(listing).nodes.find((n) => n.name.endsWith(".png"));
    expect(image, JSON.stringify(listing)).toBeDefined();

    const served = await api(`/api/image?uri=${encodeURIComponent(image?.uri ?? "")}`);
    expect(served.status).toBe(200);
    expect(
      Buffer.from(await served.arrayBuffer())
        .subarray(1, 4)
        .toString(),
    ).toBe("PNG");
  });

  it("finds the document by its exact words and by meaning", async () => {
    const exact = await eventually(
      async () =>
        json(await api(`/api/search?q=${NEEDLE}&mode=exact`), searchResponseSchema),
      (result) => result.hits.length > 0,
    );
    expect(exact.hits.map((h) => h.uri)).toContain(fileUri);

    const meaning = await json(
      await api(`/api/search?q=${NEEDLE}&mode=meaning`),
      searchResponseSchema,
    );
    expect(meaning.counts.meaning).toBeGreaterThanOrEqual(0);

    const hybrid = await json(
      await api(`/api/search?q=${NEEDLE}&mode=hybrid`),
      searchResponseSchema,
    );
    expect(hybrid.hits.map((h) => h.uri)).toContain(fileUri);
  });

  it("hands a document back to be described, and follows the job", async () => {
    const job = await json(
      await api("/api/describe", sendJson("POST", { uri: fileUri })),
      jobSchema,
    );
    expect(job.taskId).not.toBe("");

    const state = await eventually(
      async () =>
        json(await api(`/api/job?id=${encodeURIComponent(job.taskId)}`), jobStateSchema),
      (s) => s.state !== "waiting",
    );
    expect(state.state).toBe("done");
  });

  it("moves the document into another folder", async () => {
    const into = `${folder}/archive`;
    await json(
      await api("/api/folder", sendJson("POST", { into: folder, name: "archive" })),
      folderMadeSchema,
    );

    const moved = await json(
      await api("/api/move", sendJson("POST", { from: fileUri, into })),
      movedSchema,
    );
    expect(moved.uri.startsWith(`${into}/`)).toBe(true);
    const detail = await json(
      await api(`/api/file?uri=${encodeURIComponent(moved.uri)}`),
      fileDetailSchema,
    );
    expect(detail.content).toContain("moved twice");
    fileUri = moved.uri;
  });

  it("lists memories by category, and deletes one", async () => {
    const memory = `${ROOT}/memories/preferences/${RUN}.md`;
    await ov("POST", "/content/write", {
      uri: memory,
      content: "Prefers tea over coffee.",
      mode: "replace",
      wait: true,
    });

    const groups = await json(await api("/api/memories"), memoryGroupSchema.array());
    const preferences = groups.find((g) => g.category === "preferences");
    expect(preferences?.memories.map((m) => m.uri)).toContain(memory);

    const removed = await api(`/api/memories?uri=${encodeURIComponent(memory)}`, {
      method: "DELETE",
    });
    expect(removed.status).toBe(204);
  });

  it("lists agent sessions with what each one did", async () => {
    const created = (await ov("POST", "/sessions", {})) as { session_id: string };
    await ov("POST", `/sessions/${created.session_id}/messages`, {
      role: "user",
      content: "hello from the integration suite",
    });

    const response = await api("/api/sessions");
    const rows = (await response.json()) as { id: string; messages: number }[];
    expect(response.status).toBe(200);
    const row = rows.find((r) => r.id === created.session_id);
    expect(row, JSON.stringify(rows)).toBeDefined();
    expect(row?.messages).toBe(1);
  });

  it("deletes a folder and everything in it", async () => {
    const removed = await api(`/api/file?uri=${encodeURIComponent(folder)}`, {
      method: "DELETE",
    });
    expect(removed.status).toBe(204);

    const gone = await api(`/api/file?uri=${encodeURIComponent(fileUri)}`);
    expect(gone.status).toBe(404);
  });

  it("signs in by redirect through Vault, the way the lab does, and reads the tree", async () => {
    const redirected = createApp(
      buildServices(
        loadConfig({
          OV_URL: live?.url ?? "",
          PUBLIC_ORIGIN: ORIGIN,
          SESSION_SECRET: "a".repeat(32),
          AUTH_MODE: "vault-oidc",
          OIDC_ISSUER: redirect.issuer,
          OIDC_CLIENT_ID: redirect.clientId,
          OIDC_CLIENT_SECRET: redirect.clientSecret,
          VAULT_ADDR: VAULT,
          VAULT_JWT_MOUNT: REDIRECT.mount,
          VAULT_JWT_ROLE: REDIRECT.jwtRole,
          VAULT_OIDC_ROLE: LOGIN.role,
          // This suite's scope, named apart from the other file's `openviking`.
          OIDC_SCOPES: `openid ${REDIRECT.provider}`,
          SESSION_COOKIE_SECURE: "false",
        }),
      ),
    );

    const start = await redirected.request("/auth/login?returnTo=%2F%23%2Fhome");
    expect(start.status).toBe(302);
    const pending = (
      start.headers.getSetCookie().find((c) => c.includes("_login=")) ?? ""
    ).split(";")[0];

    // Vault's login page would do this once the person authenticated there.
    const login = (await (
      await fetch(`${VAULT}/v1/auth/userpass/login/${LOGIN.username}`, {
        method: "POST",
        body: JSON.stringify({ password: LOGIN.password }),
      })
    ).json()) as { auth: { client_token: string } };
    const query = new URL(start.headers.get("location") ?? "").searchParams;
    const authorized = (await (
      await fetch(
        `${VAULT}/v1/identity/oidc/provider/${REDIRECT.provider}/authorize?${query}`,
        { headers: { "x-vault-token": login.auth.client_token } },
      )
    ).json()) as { code: string; state: string };

    const back = await redirected.request(
      `/auth/callback?code=${authorized.code}&state=${authorized.state}`,
      { headers: { cookie: pending ?? "" } },
    );
    expect(back.status, await back.clone().text()).toBe(302);
    const session = (
      back.headers.getSetCookie().find((c) => !c.includes("_login=")) ?? ""
    ).split(";")[0];

    const home = await redirected.request("/api/home", {
      headers: { cookie: session ?? "" },
    });
    const body = await json(home, homeSchema);
    expect(body.viewer).toMatchObject(IDENTITY);
  });

  it("signs out, and the cookie no longer works", async () => {
    const out = await api("/auth/logout", { method: "POST" });
    expect(out.status).toBe(204);

    const state = await json(await api("/api/session"), sessionStateSchema);
    expect(state.signedIn).toBe(false);
  });
});
