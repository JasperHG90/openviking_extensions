/**
 * The contract between the server and the browser.
 *
 * Every route answers with one of these shapes, and the client parses what it
 * receives rather than trusting it. OpenViking's own payloads are wider and
 * looser than the dashboard needs; the server narrows them here so a change
 * upstream breaks at the boundary instead of somewhere inside a component.
 */

import { z } from "zod";

/** Who the browser is talking as. Never carries a credential. */
export const viewerSchema = z.object({
  /** Stable subject claim from the identity provider. */
  sub: z.string(),
  /** Display name, falling back to the email local part. */
  name: z.string(),
  email: z.string(),
  /** OpenViking account this person's key resolves to. */
  account: z.string(),
  /** OpenViking user id this person's key resolves to. */
  user: z.string(),
});
export type Viewer = z.infer<typeof viewerSchema>;

export const sessionStateSchema = z.discriminatedUnion("signedIn", [
  z.object({
    signedIn: z.literal(false),
    loginUrl: z.string(),
    /** How to sign in: a form this page posts, or a redirect to a provider. */
    mode: z.enum(["vault-userpass", "redirect"]),
  }),
  z.object({
    signedIn: z.literal(true),
    viewer: viewerSchema,
    /**
     * Whether signing out ends anything.
     *
     * Only a cookie-backed session can be ended from here. Under
     * `trusted-header` the identity arrives on every request from the proxy,
     * and under `dev` it is fixed by configuration — clearing a cookie neither
     * reads would be a button that lies.
     */
    canSignOut: z.boolean(),
  }),
]);
export type SessionState = z.infer<typeof sessionStateSchema>;

/** One entry in the file tree, flattened from OpenViking's fs payloads. */
export const nodeSchema = z.object({
  uri: z.string(),
  name: z.string(),
  isDir: z.boolean(),
  /** Bytes. OpenViking reports 0 for directories it has not measured. */
  size: z.number(),
  /** ISO 8601, or an empty string when upstream omits it. */
  modTime: z.string(),
  /** Upper-case extension, or DIR. Shown in the Kind column. */
  kind: z.string(),
  /** Path relative to the scope root, e.g. "notes/meetings/". */
  relPath: z.string(),
});
export type Node = z.infer<typeof nodeSchema>;

export const treeSchema = z.object({
  root: z.string(),
  nodes: z.array(nodeSchema),
  /** True when OpenViking hit its node_limit and stopped early. */
  truncated: z.boolean(),
  /**
   * What OpenViking says this folder is about, when it has decided.
   *
   * Empty while the abstract is still being built, which is the common case
   * for a folder written to a moment ago.
   */
  summary: z.string().default(""),
});
export type Tree = z.infer<typeof treeSchema>;

export const fileDetailSchema = z.object({
  node: nodeSchema,
  /** L2 content. Empty for binaries the server declined to inline. */
  content: z.string(),
  /** L0 abstract, when OpenViking has one. */
  abstract: z.string(),
  /** True when content was withheld because the file is not text. */
  binary: z.boolean(),
});
export type FileDetail = z.infer<typeof fileDetailSchema>;

export const searchModeSchema = z.enum(["hybrid", "meaning", "exact"]);
export type SearchMode = z.infer<typeof searchModeSchema>;

/**
 * How one result earned its place.
 *
 * `meaning` is the vector score from /search/find. `exact` lists the query
 * terms that /search/grep actually matched. A hybrid result may carry both,
 * and the UI says which — that is the whole point of showing the mode.
 */
export const matchSchema = z.object({
  meaning: z.number().nullable(),
  exact: z.array(z.string()),
});
export type Match = z.infer<typeof matchSchema>;

export const searchHitSchema = z.object({
  uri: z.string(),
  name: z.string(),
  relPath: z.string(),
  kind: z.string(),
  snippet: z.string(),
  match: matchSchema,
  /** Fused rank score. Only meaningful for ordering within one response. */
  score: z.number(),
});
export type SearchHit = z.infer<typeof searchHitSchema>;

export const searchResponseSchema = z.object({
  query: z.string(),
  mode: searchModeSchema,
  hits: z.array(searchHitSchema),
  /** Per-face counts, so the UI can say "12 by meaning, 3 exact". */
  counts: z.object({ meaning: z.number(), exact: z.number() }),
});
export type SearchResponse = z.infer<typeof searchResponseSchema>;

export const memorySchema = z.object({
  uri: z.string(),
  /** Directory under memories/, e.g. "preferences". Files sort into "profile". */
  category: z.string(),
  name: z.string(),
  text: z.string(),
  modTime: z.string(),
});
export type Memory = z.infer<typeof memorySchema>;

export const memoryGroupSchema = z.object({
  category: z.string(),
  memories: z.array(memorySchema),
});
export type MemoryGroup = z.infer<typeof memoryGroupSchema>;

/**
 * One agent run, and what it actually did.
 *
 * Every count here comes from the session detail endpoint. `detailed` says
 * whether that endpoint was read at all — a row without it reports zeros
 * because nothing was fetched, not because nothing happened, and the UI has to
 * be able to tell those apart.
 */
export const agentSessionSchema = z.object({
  id: z.string(),
  uri: z.string(),
  messages: z.number(),
  commits: z.number(),
  memories: z.number(),
  tokens: z.number(),
  createdAt: z.string(),
  updatedAt: z.string(),
  detailed: z.boolean(),
});
export type AgentSession = z.infer<typeof agentSessionSchema>;
export type SessionRow = AgentSession;

export const homeSchema = z.object({
  viewer: viewerSchema,
  stats: z.object({
    files: z.number(),
    memories: z.number(),
    sessions: z.number(),
  }),
  recentFiles: z.array(nodeSchema),
  recentMemories: z.array(memorySchema),
});
export type Home = z.infer<typeof homeSchema>;

/**
 * Whatever lives at a uri — a document or a folder.
 *
 * Links between memories point at both, and the browser cannot tell which
 * without asking, so the server stats it once and answers with the right shape
 * rather than leaving the client to guess and retry.
 */
export const openedSchema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("file"), file: fileDetailSchema }),
  z.object({ kind: z.literal("folder"), folder: treeSchema, name: z.string() }),
]);
export type Opened = z.infer<typeof openedSchema>;

/** Where a file may be added, and the folders already there. */
export const destinationsSchema = z.object({
  scopes: z.array(z.object({ id: z.string(), label: z.string(), uri: z.string() })),
  folders: z.array(z.object({ uri: z.string(), label: z.string(), scope: z.string() })),
});
export type Destinations = z.infer<typeof destinationsSchema>;

/** The error envelope every failing route returns. */
export const apiErrorSchema = z.object({
  error: z.object({
    code: z.string(),
    message: z.string(),
  }),
});
export type ApiError = z.infer<typeof apiErrorSchema>;

export const uploadResultSchema = z.object({
  uri: z.string(),
  name: z.string(),
});
export type UploadResult = z.infer<typeof uploadResultSchema>;
