/**
 * The browser's view of the server.
 *
 * Every response is parsed against the shared schema before it reaches a
 * component, so a server change shows up here as one clear error instead of as
 * an `undefined` deep inside a render.
 */

import { z } from "zod";
import {
  type AgentSession,
  type Destinations,
  type FileDetail,
  type Home,
  type MemoryGroup,
  type Opened,
  type SearchMode,
  type SearchResponse,
  type SessionState,
  type Tree,
  type UploadResult,
  agentSessionSchema,
  apiErrorSchema,
  destinationsSchema,
  fileDetailSchema,
  homeSchema,
  memoryGroupSchema,
  openedSchema,
  searchResponseSchema,
  sessionStateSchema,
  treeSchema,
  uploadResultSchema,
} from "../../shared/schemas";
import { cached, invalidate } from "./cache";

/** A failed call, carrying the server's own code and message. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/**
 * Fetch and parse one route.
 *
 * The return type is the schema's *output*, which matters wherever a field has
 * a `.default()`: input and output differ there, and inferring from the input
 * would make every defaulted field optional downstream.
 */
async function call<S extends z.ZodTypeAny>(
  path: string,
  schema: S,
  init?: RequestInit,
): Promise<z.output<S>> {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...init,
  }).catch((error: Error) => {
    throw new ApiError(`could not reach the dashboard: ${error.message}`, "OFFLINE", 0);
  });

  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    const parsed = apiErrorSchema.safeParse(body);
    throw new ApiError(
      parsed.success ? parsed.data.error.message : `HTTP ${response.status}`,
      parsed.success ? parsed.data.error.code : "HTTP_ERROR",
      response.status,
    );
  }

  const parsed = schema.safeParse(await response.json());
  if (!parsed.success) {
    throw new ApiError(
      `the server answered ${path} in an unexpected shape`,
      "BAD_SHAPE",
      response.status,
    );
  }
  return parsed.data;
}

/*
 * Reads go through the cache; anything that writes clears what it changed.
 * Search is not cached — the query is the point, and a stale answer to a new
 * question is worse than waiting.
 */
export const api = {
  session: () => call("/api/session", sessionStateSchema),
  home: () => cached("home", () => call("/api/home", homeSchema)),
  tree: () => cached("tree", () => call("/api/tree", treeSchema)),

  folder: (uri: string) =>
    cached(`folder:${uri}`, () =>
      call(`/api/folder?uri=${encodeURIComponent(uri)}`, treeSchema),
    ),

  file: (uri: string) =>
    cached(`file:${uri}`, () =>
      call(`/api/file?uri=${encodeURIComponent(uri)}`, fileDetailSchema),
    ),

  /** Open a uri without knowing whether it is a file or a folder. */
  open: (uri: string) =>
    cached(`open:${uri}`, () =>
      call(`/api/open?uri=${encodeURIComponent(uri)}`, openedSchema),
    ),

  /** Scopes and existing folders a file could be added to. */
  destinations: () =>
    cached("destinations", () => call("/api/destinations", destinationsSchema)),

  memories: () =>
    cached("memories", () => call("/api/memories", z.array(memoryGroupSchema))),
  sessions: () =>
    cached("sessions", () => call("/api/sessions", z.array(agentSessionSchema))),

  /** Forget everything cached, so the next read is fresh. */
  refresh: () => invalidate(),

  search: (query: string, mode: SearchMode) =>
    call(`/api/search?q=${encodeURIComponent(query)}&mode=${mode}`, searchResponseSchema),

  async forget(uri: string): Promise<void> {
    const response = await fetch(`/api/memories?uri=${encodeURIComponent(uri)}`, {
      method: "DELETE",
      credentials: "same-origin",
    });
    if (!response.ok)
      throw new ApiError("could not forget that", "DELETE_FAILED", response.status);
    // The memory is gone upstream, so every view that counted it is now wrong.
    invalidate("memories");
    invalidate("home");
    invalidate(`file:${uri}`);
    invalidate(`open:${uri}`);
  },

  async upload(file: File, to?: string): Promise<UploadResult> {
    const form = new FormData();
    form.set("file", file);
    if (to) form.set("to", to);
    const result = await call("/api/upload", uploadResultSchema, {
      method: "POST",
      body: form,
    });
    // A new file changes the tree, the counts, and the folder it landed in.
    invalidate();
    return result;
  },

  async signOut(): Promise<void> {
    await fetch("/auth/logout", { method: "POST", credentials: "same-origin" });
  },

  /** The URL a download link points at. Files stream; folders arrive zipped. */
  downloadUrl: (uri: string) => `/api/download?uri=${encodeURIComponent(uri)}`,
};

export type {
  AgentSession,
  Destinations,
  Opened,
  FileDetail,
  Home,
  MemoryGroup,
  SearchResponse,
  SessionState,
  Tree,
};
