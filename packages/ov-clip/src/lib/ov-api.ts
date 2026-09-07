/**
 * Talking to OpenViking.
 *
 * Directly, with the Vault identity token ovx minted. OpenViking resolves who
 * you are from that token's claims, so this carries no account or user of its
 * own — whatever the token says, that is who the file belongs to.
 *
 * Importing a file is two calls, which is how the official SDK does it too:
 * the bytes go to `resources/temp_upload` and come back as an id, then that id
 * is imported to a destination.
 */

/** How long one call may take. An import of a large PDF is not instant. */
const TIMEOUT_MS = 120_000;

/** Where a file may be written. */
export interface Scope {
  id: "user" | "shared";
  label: string;
  uri: string;
}

/** One entry in a directory listing. */
export interface Node {
  uri: string;
  name: string;
  isDir: boolean;
}

/** Everything a call needs to reach OpenViking as somebody. */
export interface Session {
  /** Base URL, without a trailing slash. */
  url: string;
  /** The Vault identity token from ovx. */
  token: string;
}

/** Raised when OpenViking refuses, or cannot be reached. */
export class OvError extends Error {
  /**
   * @param message - What went wrong, safe to show someone.
   * @param status - HTTP status, or 0 when the request never landed.
   * @param code - OpenViking's own error code, when it sent one.
   */
  constructor(
    message: string,
    readonly status = 0,
    readonly code = "UNREACHABLE",
  ) {
    super(message);
    this.name = "OvError";
  }

  /** Whether a fresh login is what would fix this. */
  get needsLogin(): boolean {
    return this.status === 401 || this.status === 403;
  }
}

/** Strip a trailing slash so paths can be appended without doubling it. */
export function normalizeUrl(raw: string): string {
  return raw.trim().replace(/\/+$/, "");
}

/**
 * The credential headers OpenViking accepts.
 *
 * Both, deliberately. The server reads `X-API-Key` and treats a value with two
 * dots as a JWT — which a Vault identity token is — while the Rust CLI also
 * sends it as a bearer. Sending both is what `ov` does today, so this works
 * against a server configured either way rather than guessing which.
 */
function authHeaders(session: Session): Record<string, string> {
  return {
    "x-api-key": session.token,
    authorization: `Bearer ${session.token}`,
  };
}

/** Whether an unknown value is a plain object we can read fields off. */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Unwrap OpenViking's response envelope.
 *
 * Every endpoint answers `{status, result, time}`, so the interesting part is
 * always one level down. A body that is not that shape is returned as-is,
 * because some endpoints stream instead.
 */
function unwrap(body: unknown): unknown {
  if (isRecord(body) && "result" in body) return body.result;
  return body;
}

/**
 * Pull the failure out of OpenViking's envelope.
 *
 * The useful text is at `error.message`, one level in — the same envelope the
 * success path unwraps. Reading only the top level threw it away and left the
 * reader with "OpenViking answered 400" where the server had said "to must
 * target resource content". `detail` is FastAPI's own shape, for the failures
 * that never reach OpenViking's handlers.
 */
function envelopeError(text: string): { code: string; message: string } | null {
  try {
    const body = JSON.parse(text) as {
      error?: { code?: unknown; message?: unknown };
      detail?: unknown;
      message?: unknown;
    };
    const message =
      (typeof body.error?.message === "string" && body.error.message) ||
      (typeof body.message === "string" && body.message) ||
      (typeof body.detail === "string" && body.detail) ||
      "";
    const code = typeof body.error?.code === "string" ? body.error.code : "";
    return message || code ? { code, message } : null;
  } catch {
    return null;
  }
}

function messageFor(text: string, status: number): string {
  const said = envelopeError(text);
  if (said?.message) return said.message;
  if (status === 401 || status === 403) {
    return "OpenViking refused the token. It may have expired.";
  }
  return `OpenViking answered ${status}.`;
}

/**
 * Call OpenViking and hand back the unwrapped body.
 *
 * @param session - Where to call, and what to call with.
 * @param path - Path below the base URL, starting with a slash.
 * @param init - Extra fetch options; credentials are added here.
 * @returns The `result` field of the response envelope.
 * @throws OvError - On a transport failure, a timeout, or a non-2xx answer.
 */
async function call(
  session: Session,
  path: string,
  init: RequestInit = {},
): Promise<unknown> {
  if (!session.url) {
    throw new OvError("No OpenViking address to call.", 0, "NO_URL");
  }

  let response: Response;
  try {
    response = await fetch(`${session.url}${path}`, {
      ...init,
      headers: { accept: "application/json", ...authHeaders(session), ...init.headers },
      // The token is the credential; a cookie from some other session on the
      // same host must not join in.
      credentials: "omit",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    throw new OvError(`Could not reach ${session.url}: ${reason}`);
  }

  const text = await response.text();
  if (!response.ok) {
    throw new OvError(
      messageFor(text, response.status),
      response.status,
      envelopeError(text)?.code || "HTTP_ERROR",
    );
  }

  try {
    return unwrap(JSON.parse(text));
  } catch {
    throw new OvError(
      `${session.url}${path} answered with something that is not JSON. Is that address really an OpenViking server?`,
      response.status,
      "NOT_JSON",
    );
  }
}

/**
 * Check the token is accepted, and report who it belongs to.
 *
 * Uses the cheapest authenticated endpoint there is. A 401 here is the useful
 * signal — it means re-login, not that the address is wrong.
 *
 * @returns True when the call was accepted.
 * @throws OvError - When it was not.
 */
export async function checkAccess(session: Session, scopeUri: string): Promise<boolean> {
  await call(session, `/api/v1/fs/ls?uri=${encodeURIComponent(scopeUri)}&node_limit=1`);
  return true;
}

/**
 * List the directories directly inside a scope, for the folder suggestions.
 *
 * @param uri - The `viking://` URI to list.
 * @returns Names of the directories in it.
 */
export async function listFolders(session: Session, uri: string): Promise<string[]> {
  const body = await call(
    session,
    `/api/v1/fs/ls?uri=${encodeURIComponent(uri)}&node_limit=200`,
  );
  if (!Array.isArray(body)) return [];
  return body
    .filter(isRecord)
    .filter((entry) => entry.isDir === true)
    .map((entry) => {
      // The name comes off the URI. `fs/ls` entries carry `uri`, `size`,
      // `isDir`, `modTime` and `abstract` — and no `name` at all, so reading
      // one gave an empty list against every real server. ov-dash derives it
      // the same way.
      const uri = typeof entry.uri === "string" ? entry.uri : "";
      return uri.replace(/\/+$/, "").split("/").pop() ?? "";
    })
    .filter(Boolean);
}

/**
 * Upload bytes and import them to a destination.
 *
 * @param file - The bytes, the name to store them under, and where they go.
 * @returns The URI the file was imported to.
 * @throws OvError - When either half fails. A failed import leaves a temp file
 *   behind on the server, which OpenViking reaps on its own schedule.
 */
export async function addResource(
  session: Session,
  file: {
    bytes: ArrayBuffer | Uint8Array;
    filename: string;
    contentType: string;
    to: string;
  },
): Promise<string> {
  const view = file.bytes instanceof Uint8Array ? file.bytes : new Uint8Array(file.bytes);
  const form = new FormData();
  form.append(
    "file",
    new Blob([view as unknown as BlobPart], { type: file.contentType }),
    file.filename,
  );

  const uploaded = await call(session, "/api/v1/resources/temp_upload", {
    method: "POST",
    body: form,
  });
  const tempFileId = isRecord(uploaded) ? uploaded.temp_file_id : undefined;
  if (typeof tempFileId !== "string" || !tempFileId) {
    throw new OvError("OpenViking accepted the upload but returned no id.", 502);
  }

  const imported = await call(session, "/api/v1/resources", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      temp_file_id: tempFileId,
      source_name: file.filename,
      to: file.to,
      // Answer once it is stored rather than once it is fully indexed. The
      // popup is a window someone is waiting in front of, and indexing a PDF
      // can take minutes.
      wait: false,
    }),
  });

  // Where it landed comes from the server, never composed here. OpenViking
  // wraps each imported document in a directory it names, and renames the file
  // from the markdown's own `title` — so `report.md` titled "Quarterly Review"
  // is not at `<to>/report.md`, and saying it was would hand back a URI to
  // nothing.
  const root = isRecord(imported) ? imported.root_uri : undefined;
  if (typeof root !== "string" || !root) {
    throw new OvError("OpenViking imported the file but did not say where.", 502);
  }
  return root;
}
