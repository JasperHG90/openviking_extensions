/**
 * The HTTP surface: the login dance, the dashboard's own API, and the built
 * client.
 *
 * One process serves all three. The browser only ever talks to this server; it
 * never holds an OpenViking key and never reaches OpenViking directly.
 */

import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { zipSync } from "fflate";
import { Hono } from "hono";
import type { Context, MiddlewareHandler } from "hono";
import { csrf } from "hono/csrf";
import { HTTPException } from "hono/http-exception";
import type {
  FileDetail,
  Home,
  Memory,
  MemoryGroup,
  Node,
  Opened,
  SessionState,
  Tree,
  Viewer,
} from "../shared/schemas";
import { searchModeSchema } from "../shared/schemas";
import type { Config } from "./env";
import { IdentityError, isSafeUserId } from "./identity";
import { KeyError, KeyResolver } from "./keys";
import { OidcClient, OidcError } from "./oidc";
import {
  MAX_CONCURRENT_UPSTREAM,
  OvClient,
  OvError,
  inBatches,
  isTextKind,
  kindOf,
  relativeTo,
} from "./ov";
import {
  SessionError,
  endSession,
  readCredentialSession,
  readSession,
  startCredentialSession,
  startLogin,
  startSession,
  takeLogin,
} from "./session";
import { SessionStore } from "./store";
import { VaultError, signIn } from "./vault";

/** Everything a request handler needs, built once at boot. */
export interface Services {
  config: Config;
  keys: KeyResolver;
  /** The sessions this process is holding. Signing out deletes from here. */
  sessions: SessionStore;
  /** Resolved lazily: a provider that is down at boot should not stop the app. */
  oidc: () => Promise<OidcClient>;
}

type Env = { Variables: { viewer: Viewer; ov: OvClient } };

/** How many entries the tree asks OpenViking for before it stops. */
const TREE_NODE_LIMIT = 2000;

/**
 * Most bytes one folder download may pull into memory before it is refused.
 *
 * The zip is built in memory at level 0, so the peak is roughly twice this.
 * A file-count limit is not a substitute: 2000 large files is the same OOM as
 * one enormous one.
 */
const MAX_ZIP_BYTES = 512 * 1024 * 1024;

/** Most bytes one single-file download may buffer before it is refused. */
const MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024;

/** Most bytes accepted from one upload. */
const MAX_UPLOAD_BYTES = 256 * 1024 * 1024;

/** Build the services from config, memoizing the OIDC discovery. */
export function buildServices(config: Config): Services {
  const keys = new KeyResolver(config);
  const sessions = new SessionStore();
  let discovered: Promise<OidcClient> | null = null;
  return {
    config,
    keys,
    sessions,
    oidc: () => {
      if (!discovered) {
        discovered = OidcClient.discover(config).catch((error: unknown) => {
          // Clear the memo so the next attempt retries rather than serving a
          // cached failure until the process restarts.
          discovered = null;
          throw error;
        });
      }
      return discovered;
    },
  };
}

/**
 * Who is calling, and the credential they arrived with.
 *
 * In `oidc` and `vault-userpass` modes this comes from the cookie the sign-in
 * wrote. In `trusted-header` mode it is whatever the authenticating proxy
 * injected, read fresh each request. In `dev` mode it is a fixed identity.
 *
 * Identity and credential are returned together because in `vault-userpass`
 * both live in one stored session, and reading it twice left a window where the
 * session could expire between the two reads.
 */
async function resolveCaller(
  c: Context,
  config: Config,
  store: SessionStore,
): Promise<{ viewer: Viewer; credential?: string; expiresAt?: number | null } | null> {
  if (config.AUTH_MODE === "vault-userpass") {
    const session = await readCredentialSession(c, config, store);
    return session
      ? {
          viewer: session.viewer,
          credential: session.token,
          expiresAt: session.expiresAt,
        }
      : null;
  }

  if (config.AUTH_MODE === "dev") {
    const user = config.DEV_USER;
    return {
      viewer: {
        sub: user,
        name: user,
        email: config.DEV_EMAIL,
        account: config.OV_ACCOUNT || user,
        user,
      },
    };
  }

  if (config.AUTH_MODE === "trusted-header") {
    const raw = c.req.header(config.TRUSTED_USER_HEADER)?.trim();
    const email = c.req.header(config.TRUSTED_EMAIL_HEADER)?.trim() ?? "";
    if (!raw) return null;
    const user =
      config.IDENTITY_FROM === "email-local" ? (raw.split("@")[0] ?? raw) : raw;
    // A header of "@example.com" splits to an empty local part, which would
    // render a templated root as its own parent — `viking://users/` — and hand
    // the caller everybody's tree. The OIDC path refuses this in
    // `viewerFromClaims`; the header path has to refuse it too.
    if (!user) return null;
    return {
      viewer: {
        sub: raw,
        name: user,
        email: email || raw,
        account: config.OV_ACCOUNT || user,
        user,
      },
    };
  }

  const session = await readSession(c, config, store);
  return session ? { viewer: session.viewer, expiresAt: session.expiresAt } : null;
}

/** Build the app. Exported separately from `main` so tests can mount it. */
export function createApp(services: Services) {
  const { config } = services;
  const app = new Hono<Env>();

  // ── the login dance ──────────────────────────────────────────

  app.get("/auth/login", async (c) => {
    const returnTo = safeReturnTo(c.req.query("returnTo"));

    if (config.AUTH_MODE !== "oidc") {
      // Nothing to negotiate: identity comes from a header or from config.
      return c.redirect(returnTo);
    }

    const client = await services.oidc();
    const { url, state, nonce, verifier } = client.authorize();
    await startLogin(c, config, { state, nonce, verifier, returnTo });
    return c.redirect(url);
  });

  app.get("/auth/callback", async (c) => {
    if (config.AUTH_MODE !== "oidc") return c.redirect("/");

    const error = c.req.query("error");
    if (error) {
      const description = c.req.query("error_description") ?? "";
      throw new OidcError(`the provider refused the login: ${error} ${description}`, 401);
    }

    const code = c.req.query("code");
    const state = c.req.query("state");
    const pending = await takeLogin(c, config);

    if (!pending) {
      throw new OidcError(
        "this login has expired or was already used — start again from the sign-in page",
        400,
      );
    }
    if (!code || !state || state !== pending.state) {
      throw new OidcError("the callback did not match this login", 400);
    }

    const client = await services.oidc();
    const viewer = await client.exchange(code, pending.verifier, pending.nonce);
    await startSession(c, config, services.sessions, viewer);
    return c.redirect(pending.returnTo);
  });

  // Same reasoning as the `/api/*` guard below: without it any page could sign
  // somebody out of the dashboard by submitting a form at it.
  app.post(
    "/auth/logout",
    sameOriginOnly(config),
    csrf({ origin: originOf(config) }),
    async (c) => {
      await endSession(c, config, services.sessions);
      return c.body(null, 204);
    },
  );

  // ── signing in through Vault ─────────────────────────────────
  //
  // The password is posted here and used once, server-side, to get a Vault
  // session; what is kept is the OpenViking token minted from it. Nothing
  // stores the password, and the browser never sees the token.
  //
  // Guarded like every other state-changing route. Without it any page could
  // post a form here and have the browser answer with a Set-Cookie for an
  // account the attacker controls — same cookie name, so the victim's own
  // session is replaced and everything they add afterwards lands in the
  // attacker's OpenViking tree. SameSite=Lax does not help: it governs when a
  // cookie is sent, not whether a cross-site response may set one.
  app.post(
    "/auth/vault-login",
    sameOriginOnly(config),
    csrf({ origin: originOf(config) }),
    async (c) => {
      if (config.AUTH_MODE !== "vault-userpass") {
        throw new VaultError("this dashboard does not sign in through Vault", 404);
      }

      const body = await c.req.parseBody();
      const username = typeof body.username === "string" ? body.username.trim() : "";
      const password = typeof body.password === "string" ? body.password : "";

      const credential = await signIn(config, username, password);
      await startCredentialSession(
        c,
        config,
        services.sessions,
        credential.viewer,
        credential.token,
        credential.expiresAt,
      );
      return c.json({ signedIn: true, viewer: credential.viewer });
    },
  );

  // ── who am I ─────────────────────────────────────────────────

  /*
   * Every API answer is one person's private data.
   *
   * The shell is no-store and the hashed assets are immutable, but the JSON
   * between them had no cache directive at all, which leaves it heuristically
   * cacheable. Browsers happen to be conservative about that; a shared cache
   * deliberately placed in front of the dashboard would not be, and would
   * serve one person's tree to another.
   */
  app.use("/api/*", async (c, next) => {
    await next();
    c.header("cache-control", "private, no-store");
    c.header("vary", "cookie");
  });

  app.get("/api/session", async (c) => {
    const caller = await resolveCaller(c, config, services.sessions);
    const cookieBacked =
      config.AUTH_MODE === "oidc" || config.AUTH_MODE === "vault-userpass";
    const state: SessionState = caller
      ? {
          signedIn: true,
          viewer: caller.viewer,
          canSignOut: cookieBacked,
          expiresAt: caller.expiresAt ?? null,
        }
      : {
          signedIn: false,
          loginUrl: "/auth/login",
          mode: config.AUTH_MODE === "vault-userpass" ? "vault-userpass" : "redirect",
        };
    return c.json(state);
  });

  // ── everything below needs a viewer and a key ────────────────

  // A cookie and a proxy header are *ambient*: the browser attaches them to
  // whatever request it is told to make, including one a hostile page started.
  // `POST /api/upload` takes multipart, which needs no preflight, so without
  // this any page the person visits could write files into their OpenViking
  // tree. `oidc` mode survives on the session cookie's SameSite=Lax, which is
  // an accident of cookie policy rather than a check; `trusted-header` and
  // `dev` have no such luck.
  //
  // Stated positively: a state-changing call must *prove* it came from here.
  // An earlier version only refused a mismatched `Origin`, which let a request
  // carrying neither `Origin` nor `Sec-Fetch-Site` straight through — and
  // Hono's csrf only inspects form-shaped content types, so a JSON DELETE was
  // covered by nothing but the absence of a CORS middleware. That is a
  // property of what is missing, not a check.
  //
  // Some expensive reads are guarded too. "GET changes nothing" is true about
  // state and false about cost: one search can be sixteen twenty-second greps,
  // and one folder download half a gigabyte. Those are worth a hostile page's
  // while in exactly the modes whose identity is ambient.
  const EXPENSIVE_GETS = new Set(["/api/search", "/api/download"]);
  app.use("/api/*", sameOriginOnly(config, EXPENSIVE_GETS));

  app.use("/api/*", async (c, next) => {
    // Asking who you are must not require being somebody, or the client's
    // re-read after a 401 would answer 401 and loop. Registration order already
    // spares this route — the handler above returns before this runs — so the
    // line is what keeps that true if the two are ever reordered.
    if (c.req.path === "/api/session") return next();

    const caller = await resolveCaller(c, config, services.sessions);
    if (!caller) {
      return c.json(
        { error: { code: "UNAUTHENTICATED", message: "not signed in" } },
        401,
      );
    }
    const { viewer, credential } = caller;
    c.set("viewer", viewer);
    // Read once, not twice. Looking the session up again here left a window
    // where it could expire between the two reads, dropping the request through
    // to the key resolver — which in this mode has no per-user key to find, so
    // an expired session surfaced as a confusing NO_KEY rather than "sign in
    // again".
    c.set("ov", await OvClient.forViewer(config, services.keys, viewer, credential));
    return next();
  });

  /**
   * Where this person may put a file.
   *
   * The Add page reads it so it can say where an upload will land before you
   * send one, rather than leaving a failure to explain it afterwards.
   */
  app.get("/api/scopes", async (c) => {
    const roots = rootsFor(config, c.get("viewer"));
    const scopes = [
      {
        id: "user",
        label: "My files",
        uri: `${roots.user.replace(/\/+$/, "")}/resources`,
      },
    ];
    if (roots.shared) {
      scopes.push({ id: "shared", label: "Shared with the account", uri: roots.shared });
    }
    return c.json(scopes);
  });

  /**
   * The landing page: three counts, six files, three memories.
   *
   * Deliberately cheap. An earlier version read every memory file and fetched
   * forty session details to show three numbers, which made the first thing
   * anyone saw the slowest thing in the app. Counting needs a listing, not the
   * contents, so only the three memories actually shown are read.
   */
  /**
   * Folders a file could be added to.
   *
   * The Add page offers these rather than asking someone to type a
   * `viking://` uri from memory. Folders only — a file is not a destination.
   */
  app.get("/api/destinations", async (c) => {
    const ov = c.get("ov");
    const roots = rootsFor(config, c.get("viewer"));

    const scopes = [
      {
        id: "user",
        label: "My files",
        uri: `${roots.user.replace(/\/+$/, "")}/resources`,
      },
      ...(roots.shared
        ? [{ id: "shared", label: "Shared with the account", uri: roots.shared }]
        : []),
    ];

    const listings = await inBatches(scopes, MAX_CONCURRENT_UPSTREAM, async (scope) => {
      const nodes = await ov.list(scope.uri, true, TREE_NODE_LIMIT).catch(() => []);
      return nodes
        .filter((node) => node.isDir && isOrganisingFolder(node.name))
        .map((node) => ({
          uri: node.uri.replace(/\/+$/, ""),
          label: relativeTo(scope.uri, node.uri).replace(/\/+$/, ""),
          scope: scope.id,
        }));
    });

    return c.json({
      scopes,
      folders: listings.flat().sort((a, b) => a.label.localeCompare(b.label)),
    });
  });

  app.get("/api/home", async (c) => {
    const ov = c.get("ov");
    const viewer = c.get("viewer");

    const [files, memoryFiles, sessions] = await Promise.all([
      ov.list(ov.root, true, TREE_NODE_LIMIT),
      listMemoryFiles(ov),
      ov.sessionCount().catch(() => 0),
    ]);

    const recentFiles = files
      .filter((node) => !node.isDir)
      .sort(byNewest)
      .slice(0, 6);

    const newest = [...memoryFiles].sort(byNewest).slice(0, 3);
    const recentMemories = await inBatches(newest, MAX_CONCURRENT_UPSTREAM, (node) =>
      readMemory(ov, node),
    );

    const home: Home = {
      viewer,
      stats: {
        files: files.filter((n) => !n.isDir).length,
        memories: memoryFiles.length,
        sessions,
      },
      recentFiles,
      recentMemories,
    };
    return c.json(home);
  });

  app.get("/api/tree", async (c) => {
    const ov = c.get("ov");
    const nodes = await ov.list(ov.root, true, TREE_NODE_LIMIT);
    const tree: Tree = {
      root: ov.root,
      nodes: nodes.sort((a, b) => a.relPath.localeCompare(b.relPath)),
      truncated: nodes.length >= TREE_NODE_LIMIT,
      summary: "",
    };
    return c.json(tree);
  });

  app.get("/api/folder", async (c) => {
    const ov = c.get("ov");
    const uri = requireUri(c);
    // Listed and summarised together: the reader shows both at once, so two
    // round trips from the browser would only make it flash.
    const [nodes, summary] = await Promise.all([ov.list(uri, false), ov.abstract(uri)]);
    return c.json({
      root: uri,
      nodes: nodes.sort(directoriesFirst),
      truncated: false,
      summary,
    } satisfies Tree);
  });

  app.get("/api/file", async (c) => {
    const ov = c.get("ov");
    const uri = requireUri(c);
    const node = await ov.stat(uri);
    const text = isTextKind(node.kind);

    const [content, abstract] = await Promise.all([
      text ? ov.read(uri) : Promise.resolve(""),
      ov.abstract(uri),
    ]);

    const detail: FileDetail = { node, content, abstract, binary: !text };
    return c.json(detail);
  });

  /**
   * Open whatever is at a uri.
   *
   * OpenViking's internal links point at files and folders alike and look
   * identical, so this stats first and answers with the matching shape. The
   * alternative — the browser guessing "file", failing, then retrying as a
   * folder — shows the reader an error it then has to take back.
   */
  app.get("/api/open", async (c) => {
    const ov = c.get("ov");
    const requested = requireUri(c);
    // Resolves a link that dropped the .md, and tells us what it really is.
    const node = await ov.statResolving(requested);
    const uri = node.uri || requested;

    if (node.isDir) {
      const [nodes, summary] = await Promise.all([ov.list(uri, false), ov.abstract(uri)]);
      return c.json({
        kind: "folder",
        name: node.name,
        folder: {
          root: uri,
          nodes: nodes.sort(directoriesFirst),
          truncated: false,
          summary,
        },
      } satisfies Opened);
    }

    const text = isTextKind(node.kind);
    const [content, abstract] = await Promise.all([
      text ? ov.read(uri) : Promise.resolve(""),
      ov.abstract(uri),
    ]);
    return c.json({
      kind: "file",
      file: { node, content, abstract, binary: !text },
    } satisfies Opened);
  });

  app.get("/api/download", async (c) => {
    const ov = c.get("ov");
    const uri = requireUri(c);
    const node = await ov.stat(uri);

    if (!node.isDir) {
      // The folder branch below has always been bounded; this one was not,
      // even with the declared size sitting right here. One 4 GB resource is
      // one allocation large enough to end the process.
      if (node.size > MAX_DOWNLOAD_BYTES) {
        throw new OvError(
          `${node.name} is larger than ${Math.round(MAX_DOWNLOAD_BYTES / 1_000_000)} MB — fetch it from OpenViking directly`,
          413,
          "TOO_LARGE",
        );
      }
      const bytes = await ov.download(uri);
      return c.body(bytes as unknown as ArrayBuffer, 200, {
        "content-type": "application/octet-stream",
        "content-disposition": contentDisposition(node.name),
      });
    }

    // OpenViking has no archive endpoint, so a folder is fetched file by file
    // and zipped here. Both bounds matter and they are different: the entry
    // count says whether the listing was complete, and the byte budget is what
    // actually keeps this from exhausting memory.
    const entries = await ov.list(uri, true, TREE_NODE_LIMIT);
    // Compared against every entry, not just the files. OpenViking's limit
    // counts directories too, so testing the file count means a folder holding
    // even one subdirectory can never reach the limit — and a truncated
    // listing would be zipped and sent as if it were the whole folder.
    if (entries.length >= TREE_NODE_LIMIT) {
      throw new OvError(
        `${node.name} holds more than ${TREE_NODE_LIMIT} entries — download it in parts`,
        413,
        "TOO_LARGE",
      );
    }
    const files = entries.filter((entry) => !entry.isDir);

    const contents: Record<string, Uint8Array> = {};
    let bytes = 0;
    for (const file of files) {
      // Checked against the declared size first, so an over-budget archive
      // stops before the file that would blow it is pulled into memory.
      if (bytes + file.size > MAX_ZIP_BYTES) {
        throw new OvError(
          `${node.name} is larger than ${Math.round(MAX_ZIP_BYTES / 1_000_000)} MB — download it in parts`,
          413,
          "TOO_LARGE",
        );
      }
      const body = await ov.download(file.uri);
      bytes += body.length;
      if (bytes > MAX_ZIP_BYTES) {
        throw new OvError(
          `${node.name} is larger than ${Math.round(MAX_ZIP_BYTES / 1_000_000)} MB — download it in parts`,
          413,
          "TOO_LARGE",
        );
      }
      // The entry name comes from OpenViking, not from this dashboard, so it
      // is not a path we may trust: a resource named with "../" would write
      // outside the archive root when extracted.
      contents[safeZipEntry(relativeTo(uri, file.uri))] = body;
    }
    const zipped = zipSync(contents, { level: 0 });
    return c.body(zipped as unknown as ArrayBuffer, 200, {
      "content-type": "application/zip",
      "content-disposition": contentDisposition(`${node.name}.zip`),
    });
  });

  app.get("/api/search", async (c) => {
    const ov = c.get("ov");
    const query = (c.req.query("q") ?? "").trim();
    const mode = searchModeSchema.catch("hybrid").parse(c.req.query("mode"));

    if (!query) {
      return c.json({ query, mode, hits: [], counts: { meaning: 0, exact: 0 } });
    }
    const { hits, counts } = await ov.search(query, mode);
    return c.json({ query, mode, hits, counts });
  });

  app.get("/api/memories", async (c) => {
    const ov = c.get("ov");
    const memories = await loadMemories(ov);

    const groups = new Map<string, Memory[]>();
    for (const memory of memories) {
      const bucket = groups.get(memory.category);
      if (bucket) bucket.push(memory);
      else groups.set(memory.category, [memory]);
    }

    const result: MemoryGroup[] = [...groups.entries()]
      .map(([category, items]) => ({
        category,
        memories: items.sort((a, b) => a.name.localeCompare(b.name)),
      }))
      .sort((a, b) => a.category.localeCompare(b.category));
    return c.json(result);
  });

  app.delete("/api/memories", async (c) => {
    const ov = c.get("ov");
    const uri = requireUri(c);
    // The separator is load-bearing. A bare prefix test lets any sibling that
    // merely starts with the same letters through, so `…/memories-archive/x`
    // would be accepted by a guard whose whole job is "this is a memory", and
    // the delete would go upstream.
    if (!uri.startsWith(`${ov.memoryRoot}/`) || uri.includes("..")) {
      throw new OvError("that URI is not a memory", 400, "INVALID_ARGUMENT");
    }
    await ov.remove(uri);
    return c.body(null, 204);
  });

  app.get("/api/sessions", async (c) => {
    const ov = c.get("ov");
    return c.json(await ov.sessions());
  });

  app.post("/api/upload", async (c) => {
    const ov = c.get("ov");

    /*
     * Checked before the body is read, not after.
     *
     * `parseBody` materialises the whole multipart body and `arrayBuffer`
     * copies it again, so a limit applied downstream is a limit applied once
     * the damage is done — measured, a 300 MB post cost ~1.8 GB of RSS before
     * the 413 came back. Content-Length can be absent or a lie, so the
     * post-parse check below stays as the real bound; this only stops the
     * honest large upload from being buffered at all.
     */
    const declared = Number(c.req.header("content-length") ?? "0");
    if (Number.isFinite(declared) && declared > MAX_UPLOAD_BYTES) {
      throw new OvError(
        `that upload is larger than ${Math.round(MAX_UPLOAD_BYTES / 1_000_000)} MB`,
        413,
        "TOO_LARGE",
      );
    }

    const body = await c.req.parseBody();
    const file = body.file;
    if (!(file instanceof File)) {
      throw new OvError("no file was uploaded", 400, "INVALID_ARGUMENT");
    }

    const chosen = resolveTarget(
      config,
      c.get("viewer"),
      typeof body.to === "string" ? body.to : "",
    );

    // The stored name is the sanitized one, so that is what gets reported
    // back — answering with the name as uploaded would hand the caller a URI
    // to a file that is not there under that name.
    const stored = sanitizeName(file.name);

    /*
     * Every file gets a folder of its own, named after it.
     *
     * OpenViking is inconsistent left to itself: given a scope root it invents
     * a folder from the file's stem, but given any other folder it drops the
     * file in flat beside whatever is already there. Measured both ways. So
     * the folder is made here instead, and the result is the same wherever the
     * file is going. `folderFor` is derived from the already-sanitized name,
     * so it cannot carry a separator out of the scope that was just checked.
     */
    const target = `${chosen.replace(/\/+$/, "")}/${folderFor(stored)}`;

    // The SDK uploads from a path, so the bytes land in a private temp file
    // that is removed whether or not the import succeeds.
    const dir = await mkdtemp(join(tmpdir(), "ov-dash-"));
    const path = join(dir, stored);
    try {
      const bytes = Buffer.from(await file.arrayBuffer());
      if (bytes.length > MAX_UPLOAD_BYTES) {
        throw new OvError(
          `that file is larger than ${Math.round(MAX_UPLOAD_BYTES / 1_000_000)} MB`,
          413,
          "TOO_LARGE",
        );
      }
      await writeFile(path, bytes, { mode: 0o600 });
      await importWithRetry(ov, path, target);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }

    return c.json({ uri: `${target}/${stored}`, name: stored });
  });

  // ── failures ─────────────────────────────────────────────────

  app.onError((error, c) => {
    if (error instanceof OvError) {
      return c.json(
        { error: { code: error.code, message: error.message } },
        error.status as 400,
      );
    }
    if (error instanceof SessionError) {
      return c.json(
        { error: { code: "SESSION_EXPIRED", message: error.message } },
        error.status as 400,
      );
    }
    if (error instanceof VaultError) {
      return c.json(
        { error: { code: "VAULT_REFUSED", message: error.message } },
        error.status as 400,
      );
    }
    if (error instanceof IdentityError) {
      return c.json(
        { error: { code: "BAD_IDENTITY", message: error.message } },
        error.status as 400,
      );
    }
    if (error instanceof KeyError) {
      return c.json(
        { error: { code: "NO_KEY", message: error.message } },
        error.status as 400,
      );
    }
    if (error instanceof OidcError) {
      return c.json(
        { error: { code: "AUTH_FAILED", message: error.message } },
        error.status as 400,
      );
    }
    if (error instanceof HTTPException) {
      // Hono's own refusals — a failed CSRF check, a body it could not parse.
      // Without this they fall into the catch-all below and are reported as a
      // server fault, which is both wrong and unhelpful to the caller.
      // Hono builds some of its refusals from a bare Response, which leaves
      // `message` empty, so the fallbacks are what makes this readable at all.
      return c.json(
        {
          error: {
            code: "REFUSED",
            message: error.message || error.res?.statusText || "refused",
          },
        },
        error.status as 400,
      );
    }
    console.error("unhandled error", error);
    return c.json({ error: { code: "INTERNAL", message: "something went wrong" } }, 500);
  });

  return app;
}

/**
 * Read every memory under the viewer's memories directory.
 *
 * Memories are ordinary files upstream, so this is a recursive listing plus a
 * read per file. The category is the first path segment below `memories/`,
 * which is what puts `preferences/api-docs.md` under "preferences" and a bare
 * `profile.md` under "profile".
 */
async function loadMemories(ov: OvClient): Promise<Memory[]> {
  const files = await listMemoryFiles(ov);
  // Bounded: one read per memory, and a person with a thousand of them would
  // otherwise open a thousand concurrent connections.
  return inBatches(files, MAX_CONCURRENT_UPSTREAM, (node) => readMemory(ov, node));
}

/**
 * Every memory file, without reading any of them.
 *
 * Counting memories only needs the listing, and reading them all to produce a
 * number is what made the home page slow.
 */
async function listMemoryFiles(ov: OvClient): Promise<Node[]> {
  try {
    const nodes = await ov.list(ov.memoryRoot, true, TREE_NODE_LIMIT);
    return nodes.filter((node) => !node.isDir);
  } catch {
    // A person with no memories yet has no directory, which is not an error.
    return [];
  }
}

/** Read one memory, deriving its category from where it sits. */
async function readMemory(ov: OvClient, node: Node): Promise<Memory> {
  const rel = relativeTo(ov.memoryRoot, node.uri);
  const segments = rel.split("/").filter(Boolean);
  const category =
    segments.length > 1 ? segments[0]! : (segments[0] ?? "").replace(/\.md$/, "");
  return {
    uri: node.uri,
    category: category || "other",
    name: node.name,
    text: (await ov.read(node.uri).catch(() => "")).slice(0, 2000),
    modTime: node.modTime,
  };
}

/**
 * Refuse a request that cannot show it came from this dashboard.
 *
 * Stated positively on purpose. Refusing only a *mismatched* `Origin` lets a
 * request carrying no origin header at all through, and Hono's `csrf` only
 * inspects form-shaped content types — so a JSON `DELETE` was covered by
 * nothing but the absence of a CORS middleware, which is a property of what is
 * missing rather than a check.
 *
 * @param config - Supplies the origin people actually reach.
 * @param costlyGets - Paths where a GET is expensive enough to be worth a
 *   hostile page's while, and so is guarded like a write.
 */
export function sameOriginOnly(
  config: Config,
  costlyGets: ReadonlySet<string> = new Set(),
): MiddlewareHandler {
  const expected = originOf(config);

  return async (c, next) => {
    const origin = c.req.header("origin");
    const site = c.req.header("sec-fetch-site");
    const changesState = c.req.method !== "GET" && c.req.method !== "HEAD";
    if (!changesState && !costlyGets.has(c.req.path)) return next();

    // A same-origin GET carries no Origin at all, so a missing one cannot be
    // treated as failure there; Sec-Fetch-Site is what separates it from a
    // cross-site load, and every browser able to mount this attack sends it.
    const proven =
      origin === expected ||
      (origin === undefined && (site === "same-origin" || site === "none"));

    // A costly read with neither header is a script or an old client, not a
    // cross-site page. Refusing it would break curl for no gain.
    if (!changesState && origin === undefined && site === undefined) return next();

    if (!proven) {
      return c.json(
        {
          error: {
            code: "BAD_ORIGIN",
            message: origin
              ? `this request came from ${origin}, but the dashboard is configured as ${expected} — set PUBLIC_ORIGIN to the address people actually reach`
              : `this request did not prove it came from ${expected}`,
          },
        },
        403,
      );
    }
    return next();
  };
}

/** The origin the dashboard is served from, used for the CSRF check. */
export function originOf(config: Config): string {
  return new URL(config.PUBLIC_ORIGIN).origin;
}

/** The two roots this person may write into. `shared` is null when disabled. */
export function rootsFor(
  config: Config,
  viewer: Viewer,
): { user: string; shared: string | null } {
  // Checked the same way as `account` below. This was previously truthiness
  // only, which fails closed today because every live path validates `user`
  // earlier — and is a trap for the next caller that does not.
  if (!isSafeUserId(viewer.user)) {
    // A root templated on an empty user renders as its own parent, which is
    // everybody's tree. Nothing upstream should produce this; refusing here
    // means a future caller that does cannot turn it into a scope.
    throw new OvError("this caller has no user id to scope to", 403, "NO_IDENTITY");
  }
  // The same hazard, for the same reason. A deployment templating on
  // `{account}` gets the identical collapse, and `ov_account` reaches here
  // straight from a Vault token without passing through requireUserId.
  if (!isSafeUserId(viewer.account)) {
    throw new OvError(
      "this caller has no usable account to scope to",
      403,
      "NO_IDENTITY",
    );
  }
  const fill = (template: string) =>
    template.replaceAll("{user}", viewer.user).replaceAll("{account}", viewer.account);
  const shared = config.OV_SHARED_ROOT.trim();
  return { user: fill(config.OV_ROOT), shared: shared ? fill(shared) : null };
}

/**
 * Check an upload's destination and return it normalized.
 *
 * OpenViking answers as whoever the key belongs to, so this is not what stops
 * one person writing into another's tree — that is the key. What it stops is a
 * paired extension, or a mistyped path, quietly scattering files outside the
 * two places the dashboard offers.
 */
export function resolveTarget(config: Config, viewer: Viewer, requested: string): string {
  const roots = rootsFor(config, viewer);
  const trim = (value: string) => value.replace(/\/+$/, "");
  const userRoot = trim(roots.user);

  // A bare user root is not a writable destination: OpenViking refuses it with
  // "to must target resource content", because that root only holds the fixed
  // memories/resources/sessions/skills subtrees. Files belong under resources —
  // unless the configured root already points inside one of them, in which case
  // appending again would invent a path nobody asked for.
  const WRITABLE = /\/(resources|skills)(\/|$)/;
  const fallback = WRITABLE.test(userRoot) ? userRoot : `${userRoot}/resources`;
  const target = trim(requested.trim()) || fallback;

  // Decoded before it is judged, because `%2e%2e` and `..%2f` are `..` to
  // anything that resolves the path downstream, and checking the raw text alone
  // would let an encoded climb through. Backslashes are separators to some
  // readers, so they are normalized here too.
  let decoded = target;
  try {
    decoded = decodeURIComponent(target);
  } catch {
    // A malformed escape is not a path; the blanket rule below rejects it.
  }
  if (decoded.replace(/\\/g, "/").split("/").includes("..")) {
    throw new OvError(
      `${target} walks outside the scopes this dashboard writes to`,
      400,
      "INVALID_ARGUMENT",
    );
  }

  // Then refuse any escape at all, harmless ones included. Allowing them means
  // deciding whose decoding wins, ours or OpenViking's, and refusing them all
  // is what closes double encoding without a decode loop.
  if (target.includes("%")) {
    throw new OvError(
      `${target} contains a percent escape, which this dashboard does not accept in a destination`,
      400,
      "INVALID_ARGUMENT",
    );
  }

  const scopes = [trim(roots.user), roots.shared ? trim(roots.shared) : null].filter(
    (value): value is string => value !== null,
  );
  const inside = scopes.some(
    (scope) => target === scope || target.startsWith(`${scope}/`),
  );
  if (!inside) {
    // 403, not 400: the destination is well formed, the caller just may not
    // write there. A malformed path is the caller's mistake; this is a refusal.
    throw new OvError(
      `${target} is outside the scopes this dashboard writes to (${scopes.join(", ")})`,
      403,
      "OUT_OF_SCOPE",
    );
  }
  return target;
}

/** Require a `uri` query parameter that names a Viking resource. */
function requireUri(c: Context): string {
  const uri = c.req.query("uri");
  if (!uri || !uri.startsWith("viking://")) {
    throw new OvError("a viking:// uri is required", 400, "INVALID_URI");
  }
  return uri;
}

/**
 * Keep a post-login redirect on this site.
 *
 * Without this an attacker could send someone to `/auth/login?returnTo=https://
 * elsewhere` and have the dashboard bounce them off-site right after they
 * authenticate.
 *
 * Checking the first characters by hand is not enough. Browsers resolve a
 * `Location` with WHATWG rules, where a backslash is a path separator for
 * http(s): `/\evil.example` passes a "starts with one slash" test and then
 * resolves to `https://evil.example/`. So the value is parsed the way the
 * browser will parse it, and kept only if it stayed on this origin.
 */
export function safeReturnTo(value: string | undefined): string {
  if (!value || !value.startsWith("/")) return "/";

  // A fixed, opaque base: what matters is whether the value can move the
  // origin, not what this dashboard's own origin happens to be.
  const base = "https://ov-dash.invalid";
  let resolved: URL;
  try {
    resolved = new URL(value, base);
  } catch {
    return "/";
  }
  if (resolved.origin !== base) return "/";

  return `${resolved.pathname}${resolved.search}${resolved.hash}`;
}

/**
 * Build a `Content-Disposition` for a download.
 *
 * Header values are ByteStrings, so a name carrying any code point above
 * U+00FF throws inside the response constructor — before the handler returns,
 * so it surfaces as a bare 500 and the browser's hidden download frame shows
 * nothing at all. That is not exotic input: a smart quote or an em dash in a
 * title is enough, and anything written by an agent rather than uploaded here
 * has never been through `sanitizeName`.
 *
 * RFC 6266 is the answer: a plain ASCII `filename` every client understands,
 * plus `filename*` carrying the real name percent-encoded as UTF-8.
 */
export function contentDisposition(name: string): string {
  const ascii =
    // eslint-disable-next-line no-control-regex
    name.replace(/[^\x20-\x7E]/g, "_").replace(/["\\]/g, "_") || "download";
  return `attachment; filename="${ascii}"; filename*=UTF-8''${encodeURIComponent(name)}`;
}

/**
 * Reduce one zip entry path to something safe to extract.
 *
 * Leading slashes, drive letters and any `..` segment are dropped, so an
 * archive cannot write outside the directory it is unpacked into.
 */
export function safeZipEntry(path: string): string {
  const cleaned = path
    .replace(/\\/g, "/")
    .split("/")
    .filter((part) => part && part !== "." && part !== "..")
    .join("/");
  return cleaned || "file";
}

/**
 * Whether a directory is somewhere to put a file, rather than a file itself.
 *
 * OpenViking stores each imported resource as a directory named after the
 * document — `introducing-agentic-video-in-gemini.md/` is a directory, not a
 * folder anyone means to add to. Offering those as destinations buries the
 * handful of real ones, so anything whose name carries a file extension is
 * left out.
 */
export function isOrganisingFolder(name: string): boolean {
  if (name === "assets") return false;
  return !/\.[A-Za-z0-9]{1,8}$/.test(name);
}

/**
 * The folder one file lives in, derived from its name.
 *
 * The extension is dropped so `notes.md` lands in `notes/`, which is the shape
 * OpenViking itself produces at a scope root. Input is already sanitized to a
 * single segment, so this only trims.
 */
export function folderFor(storedName: string): string {
  // Leading dots come off first. Stripping the extension from ".gitignore"
  // consumes the whole name and leaves nothing to call the folder.
  const bare = storedName.replace(/^\.+/, "");
  const stem = bare.includes(".") ? bare.replace(/\.[^.]+$/, "") : bare;
  return stem || "file";
}

/**
 * Import a resource, retrying while OpenViking holds a lock.
 *
 * Two uploads into the same folder in quick succession collide: the second
 * fails with CONFLICT and "lock acquire timed out". Measured against the
 * cluster while adding a queue of files. The lock is short, so a few spaced
 * retries turn a failure the person would have to notice into one they never
 * see.
 */
async function importWithRetry(
  ov: OvClient,
  path: string,
  target: string,
  attempts = 4,
): Promise<void> {
  for (let attempt = 1; ; attempt++) {
    try {
      await ov.addResource(path, target);
      return;
    } catch (error) {
      const locked = error instanceof OvError && error.code === "CONFLICT";
      if (!locked || attempt >= attempts) throw error;
      await new Promise((resolve) => setTimeout(resolve, 400 * attempt));
    }
  }
}

/** Reduce an uploaded name to one safe path segment. */
function sanitizeName(name: string): string {
  const base = name.split(/[/\\]/).pop() ?? "upload";
  const cleaned = base.replace(/[^A-Za-z0-9._-]/g, "_");
  return cleaned === "" || cleaned === "." || cleaned === ".." ? "upload" : cleaned;
}

function byNewest(a: Node, b: Node): number {
  return b.modTime.localeCompare(a.modTime);
}

function directoriesFirst(a: Node, b: Node): number {
  if (a.isDir !== b.isDir) return a.isDir ? -1 : 1;
  return a.name.localeCompare(b.name);
}

export { kindOf };
