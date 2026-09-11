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
  type FolderMade,
  type Home,
  type Job,
  type JobState,
  type MemoryGroup,
  type Opened,
  type SearchMode,
  type SessionState,
  type Tree,
  type UploadResult,
  agentSessionSchema,
  apiErrorSchema,
  destinationsSchema,
  fileDetailSchema,
  folderMadeSchema,
  homeSchema,
  jobSchema,
  jobStateSchema,
  memoryGroupSchema,
  movedSchema,
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
 * What to do when the server stops recognising this session.
 *
 * A Vault sign-in mints a token with a fixed lifetime and nothing renews it —
 * the password that would is never stored. So sessions end while people are
 * still looking at the page, and on a dashboard reached from anywhere that is
 * the ordinary way they end, not an edge case. The shell reads `/api/session`
 * once at boot, so without this the page stayed signed-in-looking and answered
 * every click with a toast. The shell registers a handler here instead.
 */
let onSessionEnd: () => void = () => {};

/** Register what happens when the session ends. Called once, by the shell. */
export function whenSessionEnds(handler: () => void): void {
  onSessionEnd = handler;
}

/** Act on a 401 wherever one arrives, not only where it was expected. */
function noticeSessionEnd(status: number): void {
  if (status !== 401) return;
  // Everything cached was read as somebody the server no longer knows.
  invalidate();
  onSessionEnd();
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

  if (!response.ok) throw await refusal(response);

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

/**
 * Turn a failed response into the error to throw.
 *
 * The server sends its own code and message in every refusal, and those are
 * written to be shown — "that folder is outside the scopes this dashboard
 * writes to" tells somebody what to do next, where "HTTP 403" does not.
 */
async function refusal(response: Response): Promise<ApiError> {
  noticeSessionEnd(response.status);
  const body: unknown = await response.json().catch(() => null);
  const parsed = apiErrorSchema.safeParse(body);
  return new ApiError(
    parsed.success ? parsed.data.error.message : `HTTP ${response.status}`,
    parsed.success ? parsed.data.error.code : "HTTP_ERROR",
    response.status,
  );
}

/** Run a route that answers with no body worth reading. */
async function send(path: string, init: RequestInit): Promise<void> {
  const response = await fetch(path, { credentials: "same-origin", ...init }).catch(
    (error: Error) => {
      throw new ApiError(`could not reach the dashboard: ${error.message}`, "OFFLINE", 0);
    },
  );
  if (!response.ok) throw await refusal(response);
}

/** Send a JSON body, the way every write on this API takes one. */
function posting(body: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  };
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
    if (!response.ok) {
      // Not routed through `call`, so the 401 has to be noticed here too.
      noticeSessionEnd(response.status);
      throw new ApiError("could not forget that", "DELETE_FAILED", response.status);
    }
    // The memory is gone upstream, so every view that counted it is now wrong.
    invalidate("memories");
    invalidate("home");
    invalidate(`file:${uri}`);
    invalidate(`open:${uri}`);
  },

  /**
   * Delete a file or folder outright.
   *
   * Everything cached is dropped rather than picked over: a file's own entries
   * are the obvious loss, but the tree that listed it, the home counts, the
   * folder it sat in and any search that returned it are all wrong now too.
   */
  async remove(uri: string): Promise<void> {
    await send(`/api/file?uri=${encodeURIComponent(uri)}`, { method: "DELETE" });
    invalidate();
  },

  /**
   * Move a file or folder into another folder.
   *
   * @returns Where it ended up. The server derives that from the uri it moved
   *   rather than from anything the browser sent, so this is the one answer
   *   worth trusting about the new path — a caller that rebuilt it locally
   *   would be guessing at the same string twice.
   */
  async move(from: string, into: string): Promise<string> {
    const response = await fetch("/api/move", {
      credentials: "same-origin",
      ...posting({ from, into }),
    }).catch((error: Error) => {
      throw new ApiError(`could not reach the dashboard: ${error.message}`, "OFFLINE", 0);
    });
    if (!response.ok) throw await refusal(response);
    // Both paths changed, and the tree drew them both.
    invalidate();

    // 204 means it was already there, so nothing moved and nothing is sent.
    if (response.status === 204) return from;

    // Parsed the way `call` parses, not with a bare `.parse`. A zod failure
    // thrown from here reaches a toast, and a page of zod issues is not a
    // sentence anybody can act on.
    const parsed = movedSchema.safeParse(await response.json().catch(() => null));
    if (!parsed.success) {
      throw new ApiError(
        "the server answered /api/move in an unexpected shape",
        "BAD_SHAPE",
        response.status,
      );
    }
    return parsed.data.uri;
  },

  /**
   * Make a folder to put things in.
   *
   * @param into - Where it goes. Empty means wherever an upload with no
   *   destination would land.
   * @param name - What to call it. The server cleans it, so the uri and name it
   *   answers with are what was actually made, not what was asked for.
   * @param description - What the folder is for. OpenViking stores it as the
   *   folder's abstract and vectorizes it, so this is what makes the folder
   *   findable. Optional.
   */
  async newFolder(into: string, name: string, description = ""): Promise<FolderMade> {
    const made = await call(
      "/api/folder",
      folderMadeSchema,
      posting({ into, name, description }),
    );
    // The tree has a row it did not have, and the folder above it has a child.
    invalidate();
    return made;
  },

  /** Hand a file or folder back to OpenViking to describe again. */
  describe: (uri: string): Promise<Job> =>
    call("/api/describe", jobSchema, posting({ uri })),

  /** How a background job is getting on. */
  job: (taskId: string): Promise<JobState> =>
    call(`/api/job?id=${encodeURIComponent(taskId)}`, jobStateSchema),

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
    const response = await fetch("/auth/logout", {
      method: "POST",
      credentials: "same-origin",
    });
    // Everything cached was read as the person signing out. Left in place, the
    // next person to sign in on this tab would be shown it.
    invalidate();
    if (!response.ok) {
      throw new ApiError("could not sign out", "LOGOUT_FAILED", response.status);
    }
  },

  /** The URL a download link points at. Files stream; folders arrive zipped. */
  downloadUrl: (uri: string) => `/api/download?uri=${encodeURIComponent(uri)}`,

  /** Where an image's own bytes are, for an `<img>` in the reading pane. */
  imageUrl: (uri: string) => `/api/image?uri=${encodeURIComponent(uri)}`,
};

export type {
  AgentSession,
  Destinations,
  Opened,
  FileDetail,
  FolderMade,
  Home,
  Job,
  JobState,
  MemoryGroup,
  SessionState,
  Tree,
};
