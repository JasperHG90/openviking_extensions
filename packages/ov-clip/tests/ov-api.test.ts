/**
 * Talking to OpenViking directly.
 *
 * Two things matter: every call carries the token in the shape the server
 * actually reads, and an import is the two-call dance the server expects
 * rather than one hopeful POST.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  OvError,
  type Session,
  addResource,
  checkAccess,
  listFolders,
  normalizeUrl,
} from "../src/lib/ov-api";

const SESSION: Session = { url: "https://ov.example", token: "head.body.sig" };

interface Call {
  url: string;
  method: string;
  headers: Record<string, string>;
  credentials?: RequestCredentials;
  body: unknown;
}

/** Answer OpenViking calls with the documented envelope, recording each one. */
function stubOv(handler: (url: string) => unknown = () => []): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      const headers: Record<string, string> = {};
      new Headers(init?.headers).forEach((value, key) => {
        headers[key] = value;
      });
      calls.push({
        url,
        method: init?.method ?? "GET",
        headers,
        credentials: init?.credentials,
        body: init?.body,
      });
      return new Response(
        JSON.stringify({ status: "ok", result: handler(url), time: 0.01 }),
        { headers: { "content-type": "application/json" } },
      );
    }),
  );
  return calls;
}

/** Read a Blob's text. jsdom's Blob predates `Blob.text()`. */
function readBlob(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("normalizeUrl", () => {
  it("drops trailing slashes and surrounding space", () => {
    expect(normalizeUrl("  https://ov.example//  ")).toBe("https://ov.example");
  });
});

describe("every call", () => {
  it("carries the token both ways the server might read it", async () => {
    // The server reads X-API-Key and treats a two-dot value as a JWT; the Rust
    // CLI also sends it as a bearer. Sending both is what `ov` does, so this
    // works against a server configured either way.
    const calls = stubOv();
    await checkAccess(SESSION, "viking://~");
    expect(calls[0]?.headers["x-api-key"]).toBe("head.body.sig");
    expect(calls[0]?.headers.authorization).toBe("Bearer head.body.sig");
  });

  it("sends no cookies", async () => {
    // The token is the credential. A cookie from some other session on the
    // same host must not join in and change who the server thinks is asking.
    const calls = stubOv();
    await checkAccess(SESSION, "viking://~");
    expect(calls[0]?.credentials).toBe("omit");
  });

  it("refuses to call with no address", async () => {
    const calls = stubOv();
    await expect(checkAccess({ ...SESSION, url: "" }, "viking://~")).rejects.toThrow(
      OvError,
    );
    expect(calls).toHaveLength(0);
  });

  it("says which server it could not reach", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("NetworkError");
      }),
    );
    await expect(checkAccess(SESSION, "viking://~")).rejects.toThrow(
      /https:\/\/ov\.example.*NetworkError/,
    );
  });

  it("marks a refused token as something a fresh login would fix", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("nope", { status: 401 })),
    );
    const error = (await checkAccess(SESSION, "viking://~").catch(
      (e: unknown) => e,
    )) as OvError;
    expect(error.needsLogin).toBe(true);
    expect(error.message).toMatch(/expired/);
  });

  it("does not mistake a server error for a login problem", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("boom", { status: 500 })),
    );
    const error = (await checkAccess(SESSION, "viking://~").catch(
      (e: unknown) => e,
    )) as OvError;
    expect(error.needsLogin).toBe(false);
  });

  it("blames the address when the answer is not JSON", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("<!doctype html>", { status: 200 })),
    );
    await expect(checkAccess(SESSION, "viking://~")).rejects.toThrow(
      /really an OpenViking server/,
    );
  });

  it("passes the server's own message through", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ message: "no such scope" }), { status: 404 }),
      ),
    );
    await expect(checkAccess(SESSION, "viking://nope")).rejects.toThrow("no such scope");
  });
});

describe("listFolders", () => {
  it("returns the directories only", async () => {
    // Exactly the shape a real server sends: uri, size, isDir, modTime — and
    // no `name`. Reading a `name` here returned an empty list against every
    // real OpenViking, which the integration suite caught and this did not.
    const calls = stubOv(() => [
      { uri: "viking://~/clips/", isDir: true, size: 0, modTime: "" },
      { uri: "viking://~/note.md", isDir: false, size: 12, modTime: "" },
    ]);
    expect(await listFolders(SESSION, "viking://~")).toEqual(["clips"]);
    expect(calls[0]?.url).toContain("/api/v1/fs/ls?uri=viking%3A%2F%2F~");
  });

  it("answers an empty list when the body is not one", async () => {
    stubOv(() => ({ nope: true }));
    expect(await listFolders(SESSION, "viking://~")).toEqual([]);
  });
});

describe("addResource", () => {
  it("uploads the bytes, then imports the id it got back", async () => {
    const calls = stubOv((url) =>
      url.includes("temp_upload")
        ? { temp_file_id: "tmp_1" }
        : { root_uri: "viking://user/jasper/resources/a/Hello.md" },
    );

    const uri = await addResource(SESSION, {
      bytes: new TextEncoder().encode("# hello"),
      filename: "a.md",
      contentType: "text/markdown",
      to: "viking://~/resources",
    });

    expect(calls).toHaveLength(2);
    expect(calls[0]?.url).toBe("https://ov.example/api/v1/resources/temp_upload");
    expect(calls[1]?.url).toBe("https://ov.example/api/v1/resources");
    // Where it landed is the server's answer, never composed here: OpenViking
    // wraps the document in a directory it names and renames the file from the
    // markdown's own title. Verified against a real server in the integration
    // suite; composing `${to}/${filename}` returned a URI to nothing.
    expect(uri).toBe("viking://user/jasper/resources/a/Hello.md");
  });

  it("sends the file as multipart, named", async () => {
    const calls = stubOv((url) =>
      url.includes("temp_upload")
        ? { temp_file_id: "tmp_1" }
        : { root_uri: "viking://x/y" },
    );
    await addResource(SESSION, {
      bytes: new TextEncoder().encode("# hello"),
      filename: "a.md",
      contentType: "text/markdown",
      to: "viking://~",
    });

    const form = calls[0]?.body as FormData;
    const file = form.get("file") as File;
    expect(file.name).toBe("a.md");
    expect(file.type).toBe("text/markdown");
    expect(await readBlob(file)).toBe("# hello");
    // The multipart boundary is the body's business, so no content-type is set.
    expect(calls[0]?.headers["content-type"]).toBeUndefined();
  });

  it("imports to the destination it was given, without waiting for indexing", async () => {
    const calls = stubOv((url) =>
      url.includes("temp_upload")
        ? { temp_file_id: "tmp_1" }
        : { root_uri: "viking://x/y" },
    );
    await addResource(SESSION, {
      bytes: new Uint8Array([1]),
      filename: "a.md",
      contentType: "text/markdown",
      to: "viking://resources/papers",
    });

    const body = JSON.parse(String(calls[1]?.body)) as Record<string, unknown>;
    expect(body.temp_file_id).toBe("tmp_1");
    expect(body.to).toBe("viking://resources/papers");
    expect(body.source_name).toBe("a.md");
    // The popup is a window someone is waiting in front of, and indexing a PDF
    // can take minutes.
    expect(body.wait).toBe(false);
  });

  it("accepts an ArrayBuffer as readily as a view", async () => {
    const calls = stubOv((url) =>
      url.includes("temp_upload")
        ? { temp_file_id: "tmp_1" }
        : { root_uri: "viking://x/y" },
    );
    await addResource(SESSION, {
      bytes: new TextEncoder().encode("%PDF").buffer as ArrayBuffer,
      filename: "a.pdf",
      contentType: "application/pdf",
      to: "viking://~",
    });
    const file = (calls[0]?.body as FormData).get("file") as File;
    expect(await readBlob(file)).toBe("%PDF");
  });

  it("complains when the import will not say where the file went", async () => {
    stubOv((url) =>
      url.includes("temp_upload") ? { temp_file_id: "tmp_1" } : { status: "success" },
    );
    await expect(
      addResource(SESSION, {
        bytes: new Uint8Array([1]),
        filename: "a.md",
        contentType: "text/markdown",
        to: "viking://~/resources",
      }),
    ).rejects.toThrow(/did not say where/);
  });

  it("stops rather than importing when the upload returns no id", async () => {
    const calls = stubOv(() => ({ nothing: true }));
    await expect(
      addResource(SESSION, {
        bytes: new Uint8Array([1]),
        filename: "a.md",
        contentType: "text/markdown",
        to: "viking://~",
      }),
    ).rejects.toThrow(/no id/);
    expect(calls).toHaveLength(1);
  });
});
