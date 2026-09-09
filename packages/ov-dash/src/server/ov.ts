/**
 * Talking to OpenViking on behalf of one signed-in person.
 *
 * The official SDK does the HTTP; this module does the narrowing. Most SDK
 * methods answer `unknown[]` or `JsonObject`, because OpenViking's payloads are
 * wider than any one caller needs. Everything is parsed here, so a shape change
 * upstream surfaces as one clear error at the boundary rather than as an
 * undefined halfway through rendering a page.
 */

import { OpenVikingClient, isOpenVikingError } from "@openviking/sdk";
import { z } from "zod";
import type { Viewer } from "../shared/schemas";
import type { Match, Node, SearchHit, SearchMode, SessionRow } from "../shared/schemas";
import type { Config } from "./env";
import type { KeyResolver } from "./keys";

/** Raised when OpenViking refuses or answers with something unusable. */
export class OvError extends Error {
  /**
   * @param message - What failed, safe to show a user.
   * @param status - HTTP status to answer with.
   * @param code - OpenViking's own error code, when it sent one.
   */
  constructor(
    message: string,
    readonly status = 502,
    readonly code = "UPSTREAM_ERROR",
  ) {
    super(message);
    this.name = "OvError";
  }
}

/** An `ls` entry, as documented for GET /api/v1/fs/ls. */
const entrySchema = z.object({
  name: z.string(),
  uri: z.string(),
  isDir: z.boolean().default(false),
  size: z.number().default(0),
  modTime: z.string().default(""),
});

const findItemSchema = z.object({
  uri: z.string(),
  score: z.number().default(0),
  abstract: z.string().nullable().default(""),
  overview: z.string().nullable().default(""),
  category: z.string().nullable().default(""),
  match_reason: z.string().nullable().default(""),
});

const grepResultSchema = z.object({
  matches: z
    .array(
      z.object({
        uri: z.string(),
        line: z.number().default(0),
        content: z.string().default(""),
      }),
    )
    .default([]),
  count: z.number().default(0),
});

/**
 * What `GET /api/v1/sessions` actually returns.
 *
 * Just an id, a uri and a mod time — no name, no counts, no status. An earlier
 * schema here asked for those with `.default(0)` fallbacks, so the page showed
 * a confident "0 messages" for every run. Defaults on fields the API never
 * sends do not describe missing data, they invent it.
 */
const sessionListSchema = z.object({
  session_id: z.string(),
  uri: z.string().default(""),
  mod_time: z.string().default(""),
});

/** What `GET /api/v1/sessions/{id}` adds: the part worth showing. */
const sessionDetailSchema = z.object({
  session_id: z.string(),
  created_at: z.string().nullable().default(""),
  updated_at: z.string().nullable().default(""),
  last_message_at: z.string().nullable().default(""),
  total_message_count: z.number().nullable().default(0),
  commit_count: z.number().nullable().default(0),
  memories_extracted: z.unknown().optional(),
  llm_token_usage: z.record(z.unknown()).nullable().default({}),
});

/** Extensions the dashboard will inline as text on a file page. */
const TEXT_KINDS = new Set([
  "MD",
  "TXT",
  "JSON",
  "YAML",
  "YML",
  "TOML",
  "CSV",
  "TSV",
  "LOG",
  "XML",
  "HTML",
  "CSS",
  "JS",
  "TS",
  "JSX",
  "TSX",
  "PY",
  "RS",
  "GO",
  "SH",
  "BASH",
  "ZSH",
  "SQL",
  "INI",
  "CONF",
  "HCL",
  "TF",
  "DOCKERFILE",
  "MAKEFILE",
  "SVELTE",
  "VUE",
  "JAVA",
  "C",
  "H",
  "CPP",
  "RB",
]);

/** Derive the Kind column from a name. Directories are always DIR. */
export function kindOf(name: string, isDir: boolean): string {
  if (isDir) return "DIR";
  const dot = name.lastIndexOf(".");
  if (dot <= 0 || dot === name.length - 1) return name.toUpperCase().slice(0, 8);
  return name.slice(dot + 1).toUpperCase();
}

/** Whether a file's bytes are worth inlining as text. */
export function isTextKind(kind: string): boolean {
  return TEXT_KINDS.has(kind);
}

/** Strip a root prefix off a URI, leaving a display path. */
export function relativeTo(root: string, uri: string): string {
  const base = root.endsWith("/") ? root : `${root}/`;
  return uri.startsWith(base) ? uri.slice(base.length) : uri;
}

/** How a viking uri starts, and the shortest one there can be. */
const SCHEME = "viking://";

/**
 * The folder a uri sits in.
 *
 * A uri with nothing above it answers with itself, so a caller walking upward
 * stops rather than producing `viking:/` and asking OpenViking about it.
 */
export function parentOf(uri: string): string {
  const trimmed = uri.replace(/\/+$/, "");
  // Stripping the trailing slashes off a bare `viking://` eats the scheme's
  // own, and the guard below would then hand back `viking:` — further from a
  // usable uri than what came in.
  if (trimmed.length < SCHEME.length) return uri;
  const cut = trimmed.lastIndexOf("/");
  return cut < SCHEME.length ? trimmed : trimmed.slice(0, cut);
}

/**
 * One person's view of OpenViking.
 *
 * Built per request from the session, because the API key is what tells
 * OpenViking who is asking — two viewers must never share a client.
 */
export class OvClient {
  private constructor(
    private readonly sdk: OpenVikingClient,
    readonly root: string,
    private readonly config: Config,
    private readonly apiKey: string,
    private readonly account: string,
    private readonly user: string,
    /**
     * The account-wide scope, when the deployment has one.
     *
     * Search covers it as well as the person's own tree. OpenViking's own
     * default with no target does exactly this — "the current user root and
     * shared viking://resources" — and scoping to the user tree alone silently
     * hides every shared document from the search box.
     */
    private readonly sharedRoot: string | null,
  ) {}

  /**
   * Build a client for one viewer, resolving their API key first.
   *
   * @param config - Validated configuration.
   * @param keys - Resolver that turns a user id into an API key. Unused when
   *   `credential` is given.
   * @param viewer - Who is asking.
   * @returns A client scoped to that person.
   */
  static async forViewer(
    config: Config,
    keys: KeyResolver,
    viewer: Viewer,
    /**
     * A credential the caller already holds.
     *
     * `AUTH_MODE=vault-userpass` mints one during sign-in and keeps it in the
     * session, so there is nothing to look up: passing it here skips the key
     * resolver entirely rather than asking Vault for a key that does not
     * exist.
     */
    credential?: string,
  ): Promise<OvClient> {
    const apiKey = credential ?? (await keys.forUser(viewer.user));
    const sdk = new OpenVikingClient({
      baseUrl: config.OV_URL,
      apiKey,
      account: viewer.account,
      user: viewer.user,
      // Milliseconds. The TypeScript SDK differs from the Python one here —
      // Python's `timeout` is seconds, this one's is not (its default is 6e4).
      // Dividing by 1000 gave every call a 30ms budget, so everything timed
      // out against a real server and nothing did locally against a stub.
      timeout: config.OV_TIMEOUT_MS,
    });
    const root = config.OV_ROOT.replaceAll("{user}", viewer.user).replaceAll(
      "{account}",
      viewer.account,
    );
    const shared = config.OV_SHARED_ROOT.trim();
    const sharedRoot = shared
      ? shared.replaceAll("{user}", viewer.user).replaceAll("{account}", viewer.account)
      : null;
    return new OvClient(
      sdk,
      root,
      config,
      apiKey,
      viewer.account,
      viewer.user,
      sharedRoot,
    );
  }

  /** The memories directory for this viewer. */
  get memoryRoot(): string {
    return `${this.root.replace(/\/$/, "")}/memories`;
  }

  /**
   * Run an SDK call, turning its failures into {@link OvError}.
   *
   * @param what - Short description used in the error message.
   * @param call - The SDK call to run.
   */
  private async guard<T>(what: string, call: () => Promise<T>): Promise<T> {
    try {
      return await call();
    } catch (error) {
      if (isOpenVikingError(error)) {
        const code = error.code ?? "UPSTREAM_ERROR";
        const status =
          code === "NOT_FOUND" ? 404 : code === "PERMISSION_DENIED" ? 403 : 502;
        throw new OvError(`${what}: ${error.message}`, status, code);
      }
      throw new OvError(`${what}: ${(error as Error).message}`);
    }
  }

  /**
   * List a directory, or the whole scope when `recursive` is set.
   *
   * The tree page uses the recursive form and builds its own hierarchy from the
   * URIs, because the SDK's `tree` helper does not expose a depth limit.
   *
   * @param uri - Directory to list.
   * @param recursive - Walk subdirectories too.
   * @param nodeLimit - Upper bound OpenViking applies before answering.
   * @returns Entries, narrowed onto the dashboard's node shape.
   */
  async list(uri: string, recursive = false, nodeLimit = 2000): Promise<Node[]> {
    const raw = await this.guard(`listing ${uri}`, () =>
      this.sdk.list(uri, { recursive, nodeLimit, output: "original" }),
    );
    return raw
      .map((item) => entrySchema.safeParse(item))
      .filter((parsed) => parsed.success)
      .map((parsed) => this.toNode(parsed.data));
  }

  private toNode(entry: z.infer<typeof entrySchema>): Node {
    const name = entry.name || entry.uri.replace(/\/$/, "").split("/").pop() || entry.uri;
    return {
      uri: entry.uri,
      name,
      isDir: entry.isDir,
      size: entry.size,
      modTime: entry.modTime,
      kind: kindOf(name, entry.isDir),
      relPath: relativeTo(this.root, entry.uri),
    };
  }

  /**
   * Stat a uri, retrying once with `.md` when the link left it off.
   *
   * Memories link to each other by slug and routinely omit the extension —
   * measured on real content: `…/how-to-size-gpus…` where the stored file is
   * `…/how-to-size-gpus….md`. Following the link as written 404s, so an
   * extensionless miss is retried once before giving up. Only `.md`, and only
   * when there is no extension already, so this cannot mask a genuine typo.
   */
  async statResolving(uri: string): Promise<Node> {
    try {
      return await this.stat(uri);
    } catch (error) {
      const last = uri.replace(/\/$/, "").split("/").pop() ?? "";
      if (!(error instanceof OvError) || error.status !== 404 || last.includes(".")) {
        throw error;
      }
      return this.stat(`${uri}.md`);
    }
  }

  /** Read one resource's status, as a node. */
  async stat(uri: string): Promise<Node> {
    const raw = await this.guard(`reading ${uri}`, () => this.sdk.stat(uri));
    const parsed = entrySchema.safeParse({ ...raw, uri: (raw.uri as string) ?? uri });
    if (!parsed.success) {
      throw new OvError(`OpenViking returned an unreadable stat for ${uri}`);
    }
    return this.toNode(parsed.data);
  }

  /** Read a resource's full text (L2). */
  async read(uri: string): Promise<string> {
    return this.guard(`reading ${uri}`, () => this.sdk.read(uri));
  }

  /**
   * Read a resource's abstract (L0).
   *
   * Missing abstracts are normal — a file OpenViking has not summarised yet has
   * none — so this answers with an empty string instead of failing the page.
   */
  async abstract(uri: string): Promise<string> {
    try {
      return usefulAbstract(await this.sdk.abstract(uri));
    } catch {
      return "";
    }
  }

  /**
   * Read a folder's overview (L1).
   *
   * Same contract as `abstract`: a folder OpenViking has not summarised yet
   * has none, and a page that wanted the descriptions inside it should still
   * render without them.
   */
  async overview(uri: string): Promise<string> {
    try {
      return (await this.sdk.overview(uri)).trim();
    } catch {
      return "";
    }
  }

  /**
   * Download a resource's original bytes.
   *
   * The published SDK (0.1.0) exposes no public download method — only a
   * private download-to-file — so this calls the documented endpoint directly
   * with the same credential and identity headers the SDK would have sent —
   * all four, or this one call resolves as a different caller than every other
   * call on the same page. Swap it back to the SDK once a release wraps
   * `GET /api/v1/content/download`.
   */
  async download(uri: string): Promise<Uint8Array> {
    const url = new URL("/api/v1/content/download", this.config.OV_URL);
    url.searchParams.set("uri", uri);

    const response = await fetch(url, {
      headers: {
        "x-api-key": this.apiKey,
        "x-openviking-account": this.account,
        "x-openviking-user": this.user,
        accept: "application/octet-stream",
      },
      // Do not follow. A redirect would re-send the API key to wherever it
      // pointed, and OpenViking has no reason to redirect this.
      redirect: "manual",
      signal: AbortSignal.timeout(this.config.OV_TIMEOUT_MS),
    }).catch((error: Error) => {
      throw new OvError(`downloading ${uri}: ${error.message}`);
    });

    if (response.status === 404) {
      throw new OvError(`downloading ${uri}: not found`, 404, "NOT_FOUND");
    }
    if (response.status >= 300 && response.status < 400) {
      throw new OvError(`downloading ${uri}: unexpected redirect`, 502);
    }
    if (!response.ok) {
      throw new OvError(`downloading ${uri}: HTTP ${response.status}`);
    }
    return new Uint8Array(await response.arrayBuffer());
  }

  /** Delete a resource. */
  async remove(uri: string): Promise<void> {
    await this.guard(`deleting ${uri}`, () => this.sdk.remove(uri));
  }

  /** Import a resource from a local path or a remote URL. */
  async addResource(source: string, to: string): Promise<void> {
    await this.guard(`adding ${source}`, () => this.sdk.addResource(source, { to }));
  }

  /**
   * List this viewer's agent sessions, newest first, with what each one did.
   *
   * The list endpoint carries no counts, so the detail of the newest sessions
   * is fetched too — bounded, and only for the page's worth being shown, since
   * a hundred runs would otherwise be a hundred round trips.
   *
   * @param detailFor - How many of the newest sessions to enrich.
   */
  /** How many sessions exist, without fetching a single one's detail. */
  async sessionCount(): Promise<number> {
    const raw = await this.guard("listing sessions", () => this.sdk.listSessions());
    return raw.length;
  }

  async sessions(detailFor = 40): Promise<SessionRow[]> {
    const raw = await this.guard("listing sessions", () => this.sdk.listSessions());
    const listed = raw
      .map((item) => sessionListSchema.safeParse(item))
      .filter((parsed) => parsed.success)
      .map((parsed) => parsed.data)
      .sort((a, b) => b.mod_time.localeCompare(a.mod_time));

    const enriched = await inBatches(
      listed.slice(0, detailFor),
      MAX_CONCURRENT_UPSTREAM,
      async (session) => {
        try {
          const detail = await this.sdk.getSession(session.session_id);
          const parsed = sessionDetailSchema.safeParse(detail);
          return parsed.success ? this.toRow(session, parsed.data) : this.toRow(session);
        } catch {
          // One unreadable session must not empty the page.
          return this.toRow(session);
        }
      },
    );

    return [...enriched, ...listed.slice(detailFor).map((s) => this.toRow(s))];
  }

  private toRow(
    listed: z.infer<typeof sessionListSchema>,
    detail?: z.infer<typeof sessionDetailSchema>,
  ): SessionRow {
    const usage = detail?.llm_token_usage ?? {};
    const tokens = typeof usage.total_tokens === "number" ? usage.total_tokens : 0;

    // `memories_extracted` arrives as a map of category to count, so it is
    // summed rather than shown raw — printing the object gave "[object Object]".
    let memories = 0;
    const extracted = detail?.memories_extracted;
    if (typeof extracted === "number") memories = extracted;
    else if (extracted && typeof extracted === "object") {
      for (const value of Object.values(extracted as Record<string, unknown>)) {
        if (typeof value === "number") memories += value;
      }
    }

    return {
      id: listed.session_id,
      uri: listed.uri,
      messages: detail?.total_message_count ?? 0,
      commits: detail?.commit_count ?? 0,
      memories,
      tokens,
      updatedAt: detail?.updated_at || detail?.last_message_at || listed.mod_time,
      createdAt: detail?.created_at ?? "",
      /** False when only the listing was read, so the UI can say "not loaded". */
      detailed: detail !== undefined,
    };
  }

  /**
   * Search, fusing the two faces OpenViking offers.
   *
   * `meaning` is `/search/find`, the vector face. `exact` is `/search/grep`,
   * which matches the literal words. `hybrid` runs both and merges them by
   * reciprocal rank, so a document that places well on either face surfaces,
   * and one that places well on both surfaces higher.
   *
   * Neither face is a database query the dashboard composes, so there is no
   * scope filter to get wrong: OpenViking answers each call as the caller whose
   * API key it carries.
   *
   * @param query - What the person typed.
   * @param mode - Which face or faces to run.
   * @param limit - Maximum hits to return.
   */
  async search(
    query: string,
    mode: SearchMode,
    limit = 30,
  ): Promise<{
    hits: SearchHit[];
    counts: { meaning: number; exact: number };
  }> {
    const terms = query.split(/\s+/).filter(Boolean);
    const wantMeaning = mode === "meaning" || mode === "hybrid";
    const wantExact = mode === "exact" || mode === "hybrid";

    const [meaning, exact] = await Promise.all([
      wantMeaning ? this.find(query, limit) : Promise.resolve([]),
      wantExact ? this.grepAll(terms, limit) : Promise.resolve(new Map()),
    ]);

    const byUri = new Map<string, SearchHit>();
    const rankScore = (rank: number) => 1 / (60 + rank);

    meaning.forEach((item, index) => {
      const name = item.uri.replace(/\/$/, "").split("/").pop() ?? item.uri;
      byUri.set(item.uri, {
        uri: item.uri,
        name,
        relPath: this.displayPath(item.uri),
        kind: kindOf(name, item.uri.endsWith("/")),
        snippet: (item.abstract || item.overview || "").slice(0, 400),
        match: { meaning: item.score, exact: [] },
        score: rankScore(index),
      });
    });

    let exactRank = 0;
    for (const [uri, hit] of exact) {
      const name = uri.replace(/\/$/, "").split("/").pop() ?? uri;
      const existing = byUri.get(uri);
      if (existing) {
        existing.match.exact = hit.terms;
        existing.score += rankScore(exactRank);
        if (!existing.snippet) existing.snippet = hit.line;
      } else {
        byUri.set(uri, {
          uri,
          name,
          relPath: this.displayPath(uri),
          kind: kindOf(name, uri.endsWith("/")),
          snippet: hit.line.slice(0, 400),
          match: { meaning: null, exact: hit.terms },
          score: rankScore(exactRank),
        });
      }
      exactRank += 1;
    }

    const hits = [...byUri.values()].sort((a, b) => b.score - a.score).slice(0, limit);

    return {
      hits,
      counts: { meaning: meaning.length, exact: exact.size },
    };
  }

  /** Every scope search should cover: the person's own tree, plus the shared one. */
  private get searchRoots(): string[] {
    return this.sharedRoot ? [this.root, this.sharedRoot] : [this.root];
  }

  /**
   * Shorten a uri for display against whichever scope it came from.
   *
   * A search hit can live in the shared scope, so stripping only the user root
   * would leave those results showing a full `viking://…` uri next to
   * neighbours showing a tidy relative path.
   */
  private displayPath(uri: string): string {
    for (const root of this.searchRoots) {
      const rel = relativeTo(root, uri);
      if (rel !== uri) return rel;
    }
    return uri;
  }

  /**
   * Vector search, one call per scope.
   *
   * Deliberately not one call passing both scopes as an array. Measured
   * against v0.4.17.1: a multi-scope `find` returns its results at
   * `limit <= 10` and *nothing* at `limit >= 20`, while either scope on its
   * own answers correctly at any limit. One call per scope avoids that path
   * and matches how grep already works here.
   */
  private async find(query: string, limit: number) {
    const perRoot = await inBatches(
      this.searchRoots,
      MAX_CONCURRENT_UPSTREAM,
      async (root) => {
        const result = await this.guard("searching", () =>
          this.sdk.find(query, { targetUri: root, limit }),
        );
        return [result.resources, result.memories, result.skills];
      },
    );

    const items: z.infer<typeof findItemSchema>[] = [];
    const seen = new Set<string>();
    for (const groups of perRoot) {
      for (const group of groups) {
        if (!Array.isArray(group)) continue;
        for (const item of group) {
          const parsed = findItemSchema.safeParse(item);
          // Scopes can overlap, so the same uri may arrive twice.
          if (parsed.success && !seen.has(parsed.data.uri)) {
            seen.add(parsed.data.uri);
            items.push(parsed.data);
          }
        }
      }
    }
    return items.sort((a, b) => b.score - a.score).slice(0, limit);
  }

  /**
   * Grep each term and collect which terms hit which file.
   *
   * OpenViking's grep takes one pattern, so a multi-word query becomes one call
   * per word. That is what lets a result say *which* words it matched, which is
   * the whole content of the "Exact words" mode.
   *
   * It is also a fan-out a caller controls by typing, so the terms are capped
   * and the calls are run a few at a time. Without both, one GET with a
   * thousand words in it is a thousand concurrent requests at OpenViking.
   */
  private async grepAll(
    terms: string[],
    limit: number,
  ): Promise<Map<string, { terms: string[]; line: string }>> {
    const found = new Map<string, { terms: string[]; line: string }>();
    if (terms.length === 0) return found;

    // grep takes one uri, so a second scope is a second call per term.
    // Capped once, then spread across the scopes. Slicing inside the map made
    // the cap per-scope, so a two-scope deployment ran twice what the constant
    // promises — about sixty seconds of upstream work for one GET.
    const capped = terms.slice(0, MAX_GREP_TERMS);
    const jobs = this.searchRoots.flatMap((root) =>
      capped.map((term) => ({ root, term })),
    );
    const results = await inBatches(jobs, MAX_CONCURRENT_UPSTREAM, async (job) => {
      const raw = await this.guard(`grepping ${job.term}`, () =>
        this.sdk.grep(job.root, escapeRegex(job.term), {
          caseInsensitive: true,
          nodeLimit: limit,
        }),
      );
      const parsed = grepResultSchema.safeParse(raw);
      return { term: job.term, matches: parsed.success ? parsed.data.matches : [] };
    });

    for (const { term, matches } of results) {
      for (const match of matches) {
        const existing = found.get(match.uri);
        if (existing) {
          if (!existing.terms.includes(term)) existing.terms.push(term);
        } else {
          found.set(match.uri, { terms: [term], line: match.content });
        }
      }
    }

    // A file that matched every word is a better hit than one that matched one.
    return new Map(
      [...found.entries()].sort((a, b) => b[1].terms.length - a[1].terms.length),
    );
  }
}

/**
 * Most words one query will grep for.
 *
 * Each becomes its own upstream call, so this is what stops a long paste in
 * the search box from turning into a fan-out against OpenViking.
 */
export const MAX_GREP_TERMS = 8;

/** Most upstream calls in flight at once for one dashboard request. */
export const MAX_CONCURRENT_UPSTREAM = 6;

/**
 * Map over items with a bound on how many run at once.
 *
 * `Promise.all` over a caller-sized list starts every call at the same moment,
 * which is the failure this exists to avoid. Results keep the input's order.
 *
 * @param items - What to work through.
 * @param limit - Most calls in flight at once.
 * @param work - What to do with one item.
 */
export async function inBatches<T, R>(
  items: T[],
  limit: number,
  work: (item: T) => Promise<R>,
): Promise<R[]> {
  const out: R[] = new Array(items.length);
  let next = 0;

  const runner = async (): Promise<void> => {
    while (next < items.length) {
      const index = next++;
      out[index] = await work(items[index] as T);
    }
  };

  await Promise.all(
    Array.from({ length: Math.min(limit, items.length) }, () => runner()),
  );
  return out;
}

/**
 * Drop an abstract that has nothing in it.
 *
 * OpenViking answers with a placeholder — "[Directory abstract is not ready]"
 * and friends — for anything it has not summarised yet. Rendered as a callout
 * above a document that is right there, that is pure noise, so an unfinished
 * abstract is treated as no abstract.
 */
export function usefulAbstract(text: string): string {
  const trimmed = text.trim();
  if (!trimmed) return "";
  const withoutHeading = trimmed.replace(/^#\s*\S+\s*/, "").trim();
  if (/^\[[^\]]*not ready[^\]]*\]$/i.test(withoutHeading)) return "";
  if (/^\[[^\]]*\]$/.test(withoutHeading)) return "";
  return trimmed;
}

/** Escape a user's word so grep treats it literally, not as a pattern. */
export function escapeRegex(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** The empty match, for results that arrived by neither face. */
export const NO_MATCH: Match = { meaning: null, exact: [] };
