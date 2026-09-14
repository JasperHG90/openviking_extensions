/**
 * What a person can now do to the tree: delete a file, move one, make a folder
 * to move it into, and hand a file back to OpenViking to describe again.
 *
 * All of them are writes, so the interesting cases are the refusals. A delete
 * that reaches outside the caller's scopes, a move that drops a folder inside
 * itself, a folder made on top of one that is already there, and a reindex that
 * runs the cheap mode are each a bug you would only notice against a live
 * cluster, which is exactly why they are pinned here.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { buildServices, createApp } from "../src/server/app";
import { loadConfig } from "../src/server/env";
import { statusFor } from "../src/server/ov";
import { MAX_EDIT_CHARS, MAX_REASON_CHARS } from "../src/shared/limits";

const ORIGIN = "http://localhost:8080";

/** What the dashboard's own pages send on a state-changing request. */
const FROM_THE_APP = { origin: ORIGIN, "content-type": "application/json" };

function devConfig(extra: Record<string, string> = {}) {
  return loadConfig({
    OV_URL: "http://openviking:1933",
    PUBLIC_ORIGIN: ORIGIN,
    SESSION_SECRET: "a".repeat(32),
    AUTH_MODE: "dev",
    DEV_USER: "jasper",
    KEY_SOURCE: "static-map",
    OV_API_KEY_MAP: JSON.stringify({ jasper: "ov_jasper" }),
    OV_ROOT: "viking://user/{user}",
    ...extra,
  });
}

function appFor(config = devConfig()) {
  return createApp(buildServices(config));
}

/** One upstream call, as this suite wants to read it back. */
interface Call {
  method: string;
  url: string;
  body: unknown;
}

/**
 * Answer OpenViking with the documented envelope, recording every call.
 *
 * A handler may return a `Response` of its own to answer with a refusal
 * instead; anything else is wrapped as a success.
 */
function stubOv(handler: (url: string, init?: RequestInit) => unknown = () => ({})) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      let body: unknown = null;
      if (typeof init?.body === "string") {
        try {
          body = JSON.parse(init.body);
        } catch {
          body = init.body;
        }
      }
      calls.push({ method: init?.method ?? "GET", url, body });
      const result = handler(url, init);
      if (result instanceof Response) return result;
      return new Response(JSON.stringify({ status: "ok", result, time: 0.01 }), {
        headers: { "content-type": "application/json" },
      });
    }),
  );
  return calls;
}

/** OpenViking's own refusal envelope, for a uri nothing is stored at. */
function notFound(uri: string) {
  return new Response(
    JSON.stringify({
      status: "error",
      error: { code: "NOT_FOUND", message: `${uri} not found` },
    }),
    { status: 404, headers: { "content-type": "application/json" } },
  );
}

/**
 * Stat answers for a known set of uris.
 *
 * `null` means nothing is stored there, which is a real answer the move route
 * asks for. A uri that is in neither list throws, so a test that forgets to
 * say what exists fails loudly rather than passing on a guess.
 */
function statting(entries: Record<string, { isDir: boolean } | null>) {
  return (url: string) => {
    if (!url.includes("/fs/stat")) return {};
    const uri = decodeURIComponent(new URL(url).searchParams.get("uri") ?? "");
    if (!(uri in entries)) throw new Error(`no stat stubbed for ${uri}`);
    const entry = entries[uri];
    if (!entry) return notFound(uri);
    const name = uri.split("/").pop() ?? uri;
    return { name, uri, isDir: entry.isDir, size: 10, modTime: "" };
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("deleting a file", () => {
  it("deletes what was asked for", async () => {
    const calls = stubOv(
      statting({ "viking://user/jasper/resources/a.md": { isDir: false } }),
    );

    const response = await appFor().request(
      "/api/file?uri=viking://user/jasper/resources/a.md",
      { method: "DELETE", headers: FROM_THE_APP },
    );

    expect(response.status).toBe(204);
    const deleted = calls.find((call) => call.method === "DELETE");
    expect(deleted?.url).toContain("/api/v1/fs");
    // A file needs no recursion, and waiting is what makes the listing fetched
    // straight afterwards tell the truth.
    expect(deleted?.url).toContain("recursive=false");
    expect(deleted?.url).toContain("wait=true");
  });

  it("passes recursive for a folder, which OpenViking refuses without it", async () => {
    const calls = stubOv(
      statting({ "viking://user/jasper/resources/notes": { isDir: true } }),
    );

    const response = await appFor().request(
      "/api/file?uri=viking://user/jasper/resources/notes",
      { method: "DELETE", headers: FROM_THE_APP },
    );

    expect(response.status).toBe(204);
    expect(calls.find((call) => call.method === "DELETE")?.url).toContain(
      "recursive=true",
    );
  });

  it("refuses a uri outside the caller's scopes before touching OpenViking", async () => {
    const calls = stubOv();

    const response = await appFor().request(
      "/api/file?uri=viking://user/ada/resources/a.md",
      {
        method: "DELETE",
        headers: FROM_THE_APP,
      },
    );

    expect(response.status).toBe(403);
    expect(((await response.json()) as { error: { code: string } }).error.code).toBe(
      "OUT_OF_SCOPE",
    );
    expect(calls).toHaveLength(0);
  });

  it("refuses to delete a scope root, and says why in those words", async () => {
    // "Delete everything I have" is not a request one stray click should make,
    // and an upload's destination check allows a bare root on purpose. The
    // message matters: reusing "outside the scopes" here told people that
    // viking://user/jasper was outside viking://user/jasper.
    const calls = stubOv();

    for (const root of ["viking://user/jasper", "viking://resources"]) {
      const response = await appFor().request(
        `/api/file?uri=${encodeURIComponent(root)}`,
        { method: "DELETE", headers: FROM_THE_APP },
      );
      expect(response.status, root).toBe(403);
      const body = (await response.json()) as { error: { message: string } };
      expect(body.error.message, root).toBe(
        `${root} is a whole scope — pick something inside it`,
      );
    }
    expect(calls).toHaveLength(0);
  });

  it("refuses a delete that did not come from this dashboard", async () => {
    const calls = stubOv();

    const response = await appFor().request(
      "/api/file?uri=viking://user/jasper/resources/a.md",
      { method: "DELETE", headers: { origin: "https://evil.example" } },
    );

    expect(response.status).toBe(403);
    expect(((await response.json()) as { error: { code: string } }).error.code).toBe(
      "BAD_ORIGIN",
    );
    expect(calls).toHaveLength(0);
  });
});

describe("moving a file", () => {
  const FROM = "viking://user/jasper/resources/a.md";
  const INTO = "viking://user/jasper/resources/notes";

  it("keeps the name and puts it under the destination folder", async () => {
    const calls = stubOv(statting({ [INTO]: { isDir: true }, [`${INTO}/a.md`]: null }));

    const response = await appFor().request("/api/move", {
      method: "POST",
      headers: FROM_THE_APP,
      body: JSON.stringify({ from: FROM, into: INTO }),
    });

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ uri: `${INTO}/a.md` });
    const moved = calls.find((call) => call.url.includes("/fs/mv"));
    expect(moved?.body).toEqual({ from_uri: FROM, to_uri: `${INTO}/a.md` });
  });

  it("refuses to land on a name the destination already holds", async () => {
    // OpenViking will not stop this. Its `mv` refuses a destination that is a
    // directory and copies straight over an existing file, so without the
    // check here dragging `a.md` into a folder that already has one destroys
    // the second with nothing said and nothing to undo.
    const calls = stubOv(
      statting({ [INTO]: { isDir: true }, [`${INTO}/a.md`]: { isDir: false } }),
    );

    const response = await appFor().request("/api/move", {
      method: "POST",
      headers: FROM_THE_APP,
      body: JSON.stringify({ from: FROM, into: INTO }),
    });

    expect(response.status).toBe(409);
    expect(((await response.json()) as { error: { code: string } }).error.code).toBe(
      "ALREADY_EXISTS",
    );
    expect(calls.some((call) => call.url.includes("/fs/mv"))).toBe(false);
  });

  it("does not read an outage as an empty destination", async () => {
    // `exists` answers false on a 404 and rethrows everything else. Were it to
    // swallow a 502 as well, an OpenViking hiccup would look like a free name
    // and the move would overwrite on the strength of it.
    const calls = stubOv((url) =>
      url.includes("/fs/stat") &&
      decodeURIComponent(new URL(url).searchParams.get("uri") ?? "") === `${INTO}/a.md`
        ? new Response(
            JSON.stringify({
              status: "error",
              error: { code: "UNAVAILABLE", message: "upstream is down" },
            }),
            { status: 503, headers: { "content-type": "application/json" } },
          )
        : { name: "notes", uri: INTO, isDir: true, size: 0, modTime: "" },
    );

    const response = await appFor().request("/api/move", {
      method: "POST",
      headers: FROM_THE_APP,
      body: JSON.stringify({ from: FROM, into: INTO }),
    });

    expect(response.status).toBe(502);
    expect(calls.some((call) => call.url.includes("/fs/mv"))).toBe(false);
  });

  it("does nothing when it is already there", async () => {
    const calls = stubOv();

    const response = await appFor().request("/api/move", {
      method: "POST",
      headers: FROM_THE_APP,
      body: JSON.stringify({ from: FROM, into: "viking://user/jasper/resources" }),
    });

    expect(response.status).toBe(204);
    expect(calls).toHaveLength(0);
  });

  it("refuses to drop a folder inside itself", async () => {
    const calls = stubOv();
    const folder = "viking://user/jasper/resources/notes";

    for (const into of [folder, `${folder}/deep`]) {
      const response = await appFor().request("/api/move", {
        method: "POST",
        headers: FROM_THE_APP,
        body: JSON.stringify({ from: folder, into }),
      });
      expect(response.status, into).toBe(400);
    }
    expect(calls).toHaveLength(0);
  });

  it("refuses a destination that is a file", async () => {
    // The tree only offers folders as drop targets. This is what stops a
    // request that did not come from the tree scrambling the layout.
    const calls = stubOv(
      statting({ "viking://user/jasper/resources/b.md": { isDir: false } }),
    );

    const response = await appFor().request("/api/move", {
      method: "POST",
      headers: FROM_THE_APP,
      body: JSON.stringify({ from: FROM, into: "viking://user/jasper/resources/b.md" }),
    });

    expect(response.status).toBe(400);
    expect(calls.some((call) => call.url.includes("/fs/mv"))).toBe(false);
  });

  it("refuses either end being outside the caller's scopes", async () => {
    const calls = stubOv();

    const cases = [
      { from: "viking://user/ada/resources/a.md", into: INTO },
      { from: FROM, into: "viking://user/ada/resources" },
    ];
    for (const body of cases) {
      const response = await appFor().request("/api/move", {
        method: "POST",
        headers: FROM_THE_APP,
        body: JSON.stringify(body),
      });
      expect(response.status, JSON.stringify(body)).toBe(403);
    }
    expect(calls).toHaveLength(0);
  });

  it("refuses a body that is not two viking uris", async () => {
    stubOv();

    for (const body of [{}, { from: FROM }, { from: FROM, into: "/etc" }]) {
      const response = await appFor().request("/api/move", {
        method: "POST",
        headers: FROM_THE_APP,
        body: JSON.stringify(body),
      });
      expect(response.status, JSON.stringify(body)).toBe(400);
    }
  });
});

describe("making a folder", () => {
  const INTO = "viking://user/jasper/resources/notes";

  /** Say what exists, and answer a mkdir with OpenViking's own envelope. */
  function tree(entries: Record<string, { isDir: boolean } | null>) {
    return stubOv((url) => {
      if (url.includes("/fs/mkdir")) return {};
      return statting(entries)(url);
    });
  }

  it("makes it under the folder that was asked for", async () => {
    const calls = tree({ [INTO]: { isDir: true }, [`${INTO}/drafts`]: null });

    const response = await appFor().request("/api/folder", {
      method: "POST",
      headers: FROM_THE_APP,
      body: JSON.stringify({ into: INTO, name: "drafts" }),
    });

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ uri: `${INTO}/drafts`, name: "drafts" });
    const made = calls.find((call) => call.url.includes("/fs/mkdir"));
    expect(made?.body).toEqual({ uri: `${INTO}/drafts` });
  });

  it("passes a description through, because that is the folder's abstract", async () => {
    // OpenViking writes it into `.abstract.md` and vectorizes it — fs_service
    // mkdir — so this is what makes a new folder findable at all. Dropped on
    // the way, every folder made here would be a row nothing can search for.
    const calls = tree({ [INTO]: { isDir: true }, [`${INTO}/drafts`]: null });

    await appFor().request("/api/folder", {
      method: "POST",
      headers: FROM_THE_APP,
      body: JSON.stringify({
        into: INTO,
        name: "drafts",
        description: "  half-written  ",
      }),
    });

    expect(calls.find((call) => call.url.includes("/fs/mkdir"))?.body).toEqual({
      uri: `${INTO}/drafts`,
      description: "half-written",
    });
  });

  it("keeps the name to one segment, so a typed path cannot climb out", async () => {
    const calls = tree({ [INTO]: { isDir: true }, [`${INTO}/passwd`]: null });

    const response = await appFor().request("/api/folder", {
      method: "POST",
      headers: FROM_THE_APP,
      body: JSON.stringify({ into: INTO, name: "../../etc/passwd" }),
    });

    // Everything above the last separator is gone before this is a uri at all,
    // so the `..` never reaches a path anything resolves.
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ uri: `${INTO}/passwd`, name: "passwd" });
    expect(calls.find((call) => call.url.includes("/fs/mkdir"))?.body).toEqual({
      uri: `${INTO}/passwd`,
    });
  });

  it("stores a typed name the way the box previewed it", async () => {
    // The New folder box shows the cleaned name as you type, from the same
    // rule in shared/names.ts. If the two drifted, people would type one name
    // and find another in the tree.
    const calls = tree({ [INTO]: { isDir: true }, [`${INTO}/Q3_notes`]: null });

    const response = await appFor().request("/api/folder", {
      method: "POST",
      headers: FROM_THE_APP,
      body: JSON.stringify({ into: INTO, name: "Q3 notes" }),
    });

    expect(await response.json()).toEqual({ uri: `${INTO}/Q3_notes`, name: "Q3_notes" });
    expect(calls.find((call) => call.url.includes("/fs/mkdir"))?.body).toEqual({
      uri: `${INTO}/Q3_notes`,
    });
  });

  it("refuses a name that is nothing once it is cleaned", async () => {
    const calls = stubOv();

    for (const name of ["", "   ", ".", "..", "..."]) {
      const response = await appFor().request("/api/folder", {
        method: "POST",
        headers: FROM_THE_APP,
        body: JSON.stringify({ into: INTO, name }),
      });
      expect(response.status, JSON.stringify(name)).toBe(400);
    }
    expect(calls).toHaveLength(0);
  });

  it("refuses a name starting with a dot, which is where OpenViking keeps its own", async () => {
    // `.abstract.md` is the file OpenViking writes inside a folder, so a
    // *directory* at that name sits exactly where it will later want to write.
    // A dotted folder is also invisible: every listing here goes out without
    // `-a`. The `exists` check would hide the first case behind a confusing
    // 409 on any folder that had already been described.
    const calls = stubOv();

    for (const name of [".abstract.md", ".overview.md", ".hidden"]) {
      const response = await appFor().request("/api/folder", {
        method: "POST",
        headers: FROM_THE_APP,
        body: JSON.stringify({ into: INTO, name }),
      });
      expect(response.status, name).toBe(400);
      const body = (await response.json()) as { error: { message: string } };
      expect(body.error.message, name).toContain("starting with a dot");
    }
    expect(calls).toHaveLength(0);
  });

  it("refuses a name longer than one path component may be", async () => {
    const calls = stubOv();

    const response = await appFor().request("/api/folder", {
      method: "POST",
      headers: FROM_THE_APP,
      body: JSON.stringify({ into: INTO, name: "a".repeat(256) }),
    });

    // Past this, OpenViking answers with whatever the storage layer says about
    // a path component, which is not a sentence anybody can act on.
    expect(response.status).toBe(400);
    expect(calls).toHaveLength(0);
  });

  it("refuses a parent that is a file", async () => {
    // OpenViking will not stop this: `mkdir` makes the parents it needs, and
    // `_ensure_parent_dirs` logs what it could not make and carries on. So the
    // answer would be an opaque storage error, or a directory nested inside a
    // document. `/api/move` refuses the same shape for the same reason.
    const calls = tree({ "viking://user/jasper/resources/todo.md": { isDir: false } });

    const response = await appFor().request("/api/folder", {
      method: "POST",
      headers: FROM_THE_APP,
      body: JSON.stringify({
        into: "viking://user/jasper/resources/todo.md",
        name: "kid",
      }),
    });

    expect(response.status).toBe(400);
    expect(calls.some((call) => call.url.includes("/fs/mkdir"))).toBe(false);
  });

  it("refuses a name the folder already holds", async () => {
    // OpenViking's own mkdir runs with exist_ok=False, so this would fail
    // anyway — with a message about a path on disk. The point of checking here
    // is the sentence somebody reads.
    const calls = tree({ [INTO]: { isDir: true }, [`${INTO}/drafts`]: { isDir: true } });

    const response = await appFor().request("/api/folder", {
      method: "POST",
      headers: FROM_THE_APP,
      body: JSON.stringify({ into: INTO, name: "drafts" }),
    });

    expect(response.status).toBe(409);
    const body = (await response.json()) as { error: { code: string; message: string } };
    expect(body.error.code).toBe("ALREADY_EXISTS");
    expect(body.error.message).toBe("notes already holds something called drafts");
    expect(calls.some((call) => call.url.includes("/fs/mkdir"))).toBe(false);
  });

  it("does not read an outage as a free name", async () => {
    // `exists` answers false on a 404 and rethrows everything else. Were a 502
    // swallowed as well, an OpenViking hiccup would look like an empty slot and
    // mkdir would be asked for a folder that is already there.
    //
    // Only the child's stat fails. Failing every call would have the parent's
    // stat throw first, and this would pass while testing nothing about
    // `exists` at all.
    const calls = stubOv((url) => {
      const uri = decodeURIComponent(new URL(url).searchParams.get("uri") ?? "");
      if (uri === `${INTO}/drafts`) {
        return new Response(
          JSON.stringify({
            status: "error",
            error: { code: "UNAVAILABLE", message: "upstream is down" },
          }),
          { status: 503, headers: { "content-type": "application/json" } },
        );
      }
      return { name: "notes", uri: INTO, isDir: true, size: 0, modTime: "" };
    });

    const response = await appFor().request("/api/folder", {
      method: "POST",
      headers: FROM_THE_APP,
      body: JSON.stringify({ into: INTO, name: "drafts" }),
    });

    expect(response.status).toBe(502);
    expect(calls.some((call) => call.url.includes("/fs/mkdir"))).toBe(false);
  });

  it("makes the folder when the parent is not there yet", async () => {
    // `mkdir` makes the whole chain it needs, and nothing in this dashboard
    // creates the fixed `resources` subtree — only OpenViking's own
    // `initialize_user_directories`, from routes ov-dash never calls. Refusing
    // a missing parent would mean the first folder in an empty tree could not
    // be made, which is the tree most in need of one.
    const calls = tree({
      "viking://user/jasper/resources": null,
      "viking://user/jasper/resources/drafts": null,
    });

    const response = await appFor().request("/api/folder", {
      method: "POST",
      headers: FROM_THE_APP,
      body: JSON.stringify({ name: "drafts" }),
    });

    expect(response.status).toBe(200);
    expect(calls.find((call) => call.url.includes("/fs/mkdir"))?.body).toEqual({
      uri: "viking://user/jasper/resources/drafts",
    });
  });

  it("still refuses a parent the stat could not be read for", async () => {
    // Only a 404 means "not there yet". An outage reading the parent must not
    // read as one, or a hiccup would have mkdir build a chain under a uri that
    // is really a file.
    //
    // The outage is on the parent's stat alone, and the child answers 404.
    // A stub where everything fails cannot tell you which guard refused: the
    // parent's catch could swallow the 503 as "not there yet" and the 502 would
    // still arrive, one call later, from `exists`.
    const calls = stubOv((url) => {
      const uri = decodeURIComponent(new URL(url).searchParams.get("uri") ?? "");
      if (uri !== INTO) return notFound(uri);
      return new Response(
        JSON.stringify({
          status: "error",
          error: { code: "UNAVAILABLE", message: "upstream is down" },
        }),
        { status: 503, headers: { "content-type": "application/json" } },
      );
    });

    const response = await appFor().request("/api/folder", {
      method: "POST",
      headers: FROM_THE_APP,
      body: JSON.stringify({ into: INTO, name: "drafts" }),
    });

    expect(response.status).toBe(502);
    expect(calls.some((call) => call.url.includes("/fs/mkdir"))).toBe(false);
  });

  it("falls back to where an upload would land when no folder is named", async () => {
    const calls = tree({
      "viking://user/jasper/resources": { isDir: true },
      "viking://user/jasper/resources/drafts": null,
    });

    const response = await appFor().request("/api/folder", {
      method: "POST",
      headers: FROM_THE_APP,
      body: JSON.stringify({ name: "drafts" }),
    });

    expect(response.status).toBe(200);
    // Not the bare scope root: OpenViking refuses `viking://user/jasper` as a
    // destination, because that root only holds the fixed subtrees.
    expect(await response.json()).toEqual({
      uri: "viking://user/jasper/resources/drafts",
      name: "drafts",
    });
    expect(calls.find((call) => call.url.includes("/fs/mkdir"))?.body).toEqual({
      uri: "viking://user/jasper/resources/drafts",
    });
  });

  it("refuses a parent outside the caller's scopes", async () => {
    const calls = stubOv();

    const response = await appFor().request("/api/folder", {
      method: "POST",
      headers: FROM_THE_APP,
      body: JSON.stringify({ into: "viking://user/ada/resources", name: "drafts" }),
    });

    expect(response.status).toBe(403);
    expect(calls).toHaveLength(0);
  });

  it("refuses one that did not come from this dashboard", async () => {
    const calls = stubOv();

    const response = await appFor().request("/api/folder", {
      method: "POST",
      headers: { "content-type": "application/json", origin: "https://evil.example" },
      body: JSON.stringify({ into: INTO, name: "drafts" }),
    });

    expect(response.status).toBe(403);
    expect(((await response.json()) as { error: { code: string } }).error.code).toBe(
      "BAD_ORIGIN",
    );
    expect(calls).toHaveLength(0);
  });
});

describe("describing a file again", () => {
  const URI = "viking://user/jasper/resources/shot.png";

  it("asks for the mode that reruns the model, not the one that re-embeds", async () => {
    // `vectors_only` — the SDK's default — would re-embed the description that
    // is already stored, so a file that never got one would come back empty and
    // the button would look broken.
    const calls = stubOv(() => ({ task_id: "task_1", status: "accepted" }));

    const response = await appFor().request("/api/describe", {
      method: "POST",
      headers: FROM_THE_APP,
      body: JSON.stringify({ uri: URI }),
    });

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ taskId: "task_1" });
    const asked = calls.find((call) => call.url.includes("/content/reindex"));
    expect(asked?.body).toMatchObject({
      uri: URI,
      mode: "semantic_and_vectors",
      wait: false,
    });
  });

  it("answers with no job when OpenViking opened none", async () => {
    stubOv(() => ({}));

    const response = await appFor().request("/api/describe", {
      method: "POST",
      headers: FROM_THE_APP,
      body: JSON.stringify({ uri: URI }),
    });

    expect(await response.json()).toEqual({ taskId: "" });
  });

  it("refuses a uri outside the caller's scopes", async () => {
    const calls = stubOv();

    const response = await appFor().request("/api/describe", {
      method: "POST",
      headers: FROM_THE_APP,
      body: JSON.stringify({ uri: "viking://user/ada/resources/a.md" }),
    });

    expect(response.status).toBe(403);
    expect(calls).toHaveLength(0);
  });

  it("reports a reindex already in flight as a conflict, not a gateway failure", async () => {
    // OpenViking refuses a second reindex of the same uri. That is something
    // the person can act on — wait — so answering 502 sends them looking for an
    // outage that is not there.
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              status: "error",
              error: { code: "CONFLICT", message: "already has a reindex in progress" },
            }),
            { status: 409, headers: { "content-type": "application/json" } },
          ),
      ),
    );

    const response = await appFor().request("/api/describe", {
      method: "POST",
      headers: FROM_THE_APP,
      body: JSON.stringify({ uri: URI }),
    });

    expect(response.status).toBe(409);
  });
});

describe("following the job", () => {
  function jobIs(record: unknown) {
    return stubOv(() => record);
  }

  it("reads a finished job as done", async () => {
    jobIs({ task_id: "t", status: "completed" });
    const response = await appFor().request("/api/job?id=t");
    expect(await response.json()).toEqual({ state: "done", error: "" });
  });

  it("carries the reason a job failed", async () => {
    jobIs({ task_id: "t", status: "failed", error: "the model timed out" });
    const response = await appFor().request("/api/job?id=t");
    expect(await response.json()).toEqual({
      state: "failed",
      error: "the model timed out",
    });
  });

  it("reads a running job as still waiting", async () => {
    jobIs({ task_id: "t", status: "running" });
    const response = await appFor().request("/api/job?id=t");
    expect(await response.json()).toEqual({ state: "waiting", error: "" });
  });

  it("reads a dropped record as gone rather than as success", async () => {
    // OpenViking keeps task records for a while and then forgets them. A job
    // that failed and then expired must not read as one that finished.
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              status: "error",
              error: { code: "NOT_FOUND", message: "Task not found or expired" },
            }),
            { status: 404, headers: { "content-type": "application/json" } },
          ),
      ),
    );

    const response = await appFor().request("/api/job?id=t");
    expect(await response.json()).toEqual({ state: "gone", error: "" });
  });

  it("needs a job id", async () => {
    stubOv();
    const response = await appFor().request("/api/job?id=");
    expect(response.status).toBe(400);
  });
});

describe("what OpenViking's refusals become", () => {
  it("keeps 502 for anything it cannot place", () => {
    // 502 says the dashboard could not get an answer, which beats guessing
    // which side was at fault.
    expect(statusFor("UNAVAILABLE")).toBe(502);
    expect(statusFor("")).toBe(502);
  });

  it("passes on the ones a person can act on", () => {
    // A name already taken, or a reindex already running, is something to do
    // something about. Reported as 502 it sends people looking for an outage
    // that is not there.
    expect(statusFor("NOT_FOUND")).toBe(404);
    expect(statusFor("PERMISSION_DENIED")).toBe(403);
    expect(statusFor("INVALID_ARGUMENT")).toBe(400);
    expect(statusFor("CONFLICT")).toBe(409);
    expect(statusFor("ALREADY_EXISTS")).toBe(409);
  });
});

/**
 * The traversal rules, on the routes that act rather than the one that stores.
 *
 * `guards.test.ts` exercises these through `resolveTarget`, which is where they
 * started and where they only ever guarded an upload's *destination*. The claim
 * this change makes is that the same rules now cover every write, and a claim
 * about a shared function is not a claim about the routes that call it.
 */
describe("the traversal rules on every write", () => {
  const BAD = [
    "viking://user/jasper/../ada/x.md",
    "viking://user/jasper/%2e%2e/ada/x.md",
    "viking://user/jasper/..\\ada",
    // `.` normalizes to a different uri than it reads as, which is how a
    // request would name the scope root the delete route just refused.
    "viking://user/jasper/.",
    "viking://user/jasper/./x.md",
    "viking://user/jasper/my%20clips/x.md",
  ];

  it("refuses them on a delete", async () => {
    const calls = stubOv();
    for (const bad of BAD) {
      const response = await appFor().request(
        `/api/file?uri=${encodeURIComponent(bad)}`,
        { method: "DELETE", headers: FROM_THE_APP },
      );
      expect(response.status, bad).toBe(400);
    }
    expect(calls).toHaveLength(0);
  });

  it("refuses them at either end of a move", async () => {
    const calls = stubOv();
    for (const bad of BAD) {
      for (const body of [
        { from: bad, into: "viking://user/jasper/resources/notes" },
        { from: "viking://user/jasper/resources/a.md", into: bad },
      ]) {
        const response = await appFor().request("/api/move", {
          method: "POST",
          headers: FROM_THE_APP,
          body: JSON.stringify(body),
        });
        expect(response.status, JSON.stringify(body)).toBe(400);
      }
    }
    expect(calls).toHaveLength(0);
  });

  it("refuses them on a describe", async () => {
    const calls = stubOv();
    for (const bad of BAD) {
      const response = await appFor().request("/api/describe", {
        method: "POST",
        headers: FROM_THE_APP,
        body: JSON.stringify({ uri: bad }),
      });
      expect(response.status, bad).toBe(400);
    }
    expect(calls).toHaveLength(0);
  });

  it("refuses a describe of a whole scope", async () => {
    // Reindexing a scope root reruns the model over every document under it.
    // That is an hour of work and a bill, and no button should start it by
    // accident — so describe refuses a root the way delete does.
    const calls = stubOv();
    for (const root of ["viking://user/jasper", "viking://resources"]) {
      const response = await appFor().request("/api/describe", {
        method: "POST",
        headers: FROM_THE_APP,
        body: JSON.stringify({ uri: root }),
      });
      expect(response.status, root).toBe(403);
    }
    expect(calls).toHaveLength(0);
  });
});

/**
 * Saving an edit.
 *
 * The pane could read a memory and change nothing in it, so fixing a line of
 * `soul.md` meant going to a terminal. The interesting cases are the refusals
 * again: a save that reaches outside the caller's scopes, one aimed at a folder,
 * and one aimed at a file whose bytes are not text — that last one is the
 * dangerous one, because the reader has no bytes for a binary and accepting it
 * would write a JSON string over a PNG.
 */
describe("saving a file's text", () => {
  const URI = "viking://user/jasper/memories/soul.md";

  /** Say what exists, and answer a write with OpenViking's own envelope. */
  function tree(entries: Record<string, { isDir: boolean } | null>) {
    return stubOv((url) => {
      if (url.includes("/content/write")) return { uri: URI, mode: "replace" };
      return statting(entries)(url);
    });
  }

  function save(uri: string, body: unknown, headers = FROM_THE_APP) {
    return appFor().request(`/api/file?uri=${encodeURIComponent(uri)}`, {
      method: "PUT",
      headers,
      body: JSON.stringify(body),
    });
  }

  it("replaces the file with what was sent", async () => {
    const calls = tree({ [URI]: { isDir: false } });

    const response = await save(URI, { content: "# Soul\n\nBe kind.\n" });

    // 200 with the file's new stamp, not 204: the editor compares its next save
    // against this rather than against the version it opened on, or a second save
    // would be refused as a clash with the first.
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ modTime: "", size: 10 });
    const written = calls.find((call) => call.url.includes("/content/write"));
    expect(written?.body).toMatchObject({
      uri: URI,
      content: "# Soul\n\nBe kind.\n",
      // Replace, not append: an edit is the new whole text of the file.
      mode: "replace",
    });
  });

  it("lets a file be emptied, which is a thing somebody may mean", async () => {
    // `body.content || ""` would read an absent field as a deliberate clear.
    // These are different requests and the route has to tell them apart.
    const calls = tree({ [URI]: { isDir: false } });

    expect((await save(URI, { content: "" })).status).toBe(200);
    expect(calls.find((call) => call.url.includes("/content/write"))?.body).toMatchObject(
      { content: "" },
    );
  });

  it("refuses a body with no content field at all", async () => {
    const calls = stubOv();
    for (const body of [{}, { content: null }, { content: 12 }, { text: "hi" }]) {
      const response = await save(URI, body);
      expect(response.status, JSON.stringify(body)).toBe(400);
    }
    expect(calls).toHaveLength(0);
  });

  it("refuses to write text over a file that is not text", async () => {
    // The reader never offers this — it has no bytes for a binary — so the only
    // way here is a hand-made request, and the cost of letting it through is a
    // PNG replaced by a JSON string with nothing to undo it.
    const png = "viking://user/jasper/resources/shot/shot.png";
    const calls = tree({ [png]: { isDir: false } });

    const response = await save(png, { content: "not a picture" });

    expect(response.status).toBe(400);
    const body = (await response.json()) as { error: { message: string } };
    expect(body.error.message).toContain("cannot edit as text");
    expect(calls.some((call) => call.url.includes("/content/write"))).toBe(false);
  });

  it("refuses to write over a folder", async () => {
    const folder = "viking://user/jasper/resources/notes";
    const calls = tree({ [folder]: { isDir: true } });

    const response = await save(folder, { content: "hello" });

    expect(response.status).toBe(400);
    expect(calls.some((call) => call.url.includes("/content/write"))).toBe(false);
  });

  it("refuses a uri nothing is stored at, rather than creating one", async () => {
    // `content/write` in replace mode makes a missing file, so without the stat
    // a typo in the address bar writes a new document nobody asked for.
    const calls = tree({ [URI]: null });

    const response = await save(URI, { content: "hello" });

    expect(response.status).toBe(404);
    expect(calls.some((call) => call.url.includes("/content/write"))).toBe(false);
  });

  it("refuses a uri outside the caller's scopes", async () => {
    const calls = stubOv();
    const response = await save("viking://user/ada/memories/soul.md", {
      content: "hello",
    });
    expect(response.status).toBe(403);
    expect(calls).toHaveLength(0);
  });

  it("refuses a scope root, which is not a file", async () => {
    const calls = stubOv();
    const response = await save("viking://user/jasper", { content: "hello" });
    expect(response.status).toBe(403);
    expect(calls).toHaveLength(0);
  });

  it("refuses the traversal spellings, like every other write", async () => {
    const calls = stubOv();
    for (const bad of [
      "viking://user/jasper/../ada/x.md",
      "viking://user/jasper/%2e%2e/ada/x.md",
      "viking://user/jasper/./x.md",
    ]) {
      const response = await save(bad, { content: "hello" });
      expect(response.status, bad).toBe(400);
    }
    expect(calls).toHaveLength(0);
  });

  it("refuses more text than it will hold", async () => {
    const calls = stubOv();
    const response = await save(URI, { content: "x".repeat(MAX_EDIT_CHARS + 1) });
    expect(response.status).toBe(413);
    // Refused before the stat, so an oversize paste costs no upstream call.
    expect(calls).toHaveLength(0);
  });

  it("refuses a save that did not come from this dashboard", async () => {
    const calls = stubOv();
    const response = await save(
      URI,
      { content: "hello" },
      { "content-type": "application/json", origin: "https://evil.example" },
    );
    expect(response.status).toBe(403);
    expect(((await response.json()) as { error: { code: string } }).error.code).toBe(
      "BAD_ORIGIN",
    );
    expect(calls).toHaveLength(0);
  });
});

/**
 * Saying why a file is worth keeping.
 *
 * Not a label filed beside the bytes: OpenViking passes `reason` to the parser
 * as the instruction when nothing else gives one, so it steers the abstract and
 * overview the model writes and therefore what the file is later found by.
 * Dropped on the way, this would be a box that changes nothing.
 */
describe("why an upload is worth keeping", () => {
  /** An import is two upstream calls: bytes to `temp_upload`, then the id. */
  function stubImport() {
    return stubOv((url) =>
      url.includes("temp_upload") ? { temp_file_id: "tmp_1" } : { ok: true },
    );
  }

  function upload(fields: Record<string, string>) {
    const form = new FormData();
    form.append("file", new File(["the numbers"], "q3.md", { type: "text/markdown" }));
    for (const [key, value] of Object.entries(fields)) form.append(key, value);
    return appFor().request("/api/upload", {
      method: "POST",
      headers: { origin: ORIGIN },
      body: form,
    });
  }

  /** The body of the `POST /resources` call, which is the import itself. */
  function imported(calls: Call[]): Record<string, unknown> {
    const call = calls.find(
      (candidate) =>
        candidate.url.endsWith("/api/v1/resources") && candidate.method === "POST",
    );
    return (call?.body ?? {}) as Record<string, unknown>;
  }

  it("passes the reason to OpenViking, where it becomes the instruction", async () => {
    const calls = stubImport();

    const response = await upload({ why: "the pricing numbers for the Q3 argument" });

    expect(response.status).toBe(200);
    expect(imported(calls).reason).toBe("the pricing numbers for the Q3 argument");
  });

  it("trims it, so a stray newline is not part of the prompt", async () => {
    const calls = stubImport();
    await upload({ why: "  for the Q3 argument \n" });
    expect(imported(calls).reason).toBe("for the Q3 argument");
  });

  it("sends none at all when nothing was typed", async () => {
    // Not an empty string: `reason: ""` is a field OpenViking would then read,
    // and an empty instruction is not the same as no instruction.
    const calls = stubImport();
    await upload({ why: "   " });
    expect(imported(calls)).not.toHaveProperty("reason");
  });

  it("still uploads when no reason is given, because it is optional", async () => {
    const calls = stubImport();
    expect((await upload({})).status).toBe(200);
    expect(imported(calls)).not.toHaveProperty("reason");
  });

  it("refuses one long enough to be a document in its own right", async () => {
    // This is a prompt, not a note. A page pasted in here becomes the
    // instruction that decides how the file is described.
    const calls = stubOv();
    const response = await upload({ why: "x".repeat(MAX_REASON_CHARS + 1) });
    expect(response.status).toBe(400);
    expect(calls).toHaveLength(0);
  });
});

/**
 * The bounds a save is held to before its body is even read.
 *
 * Two different defences, and both matter. The character cap bounds what gets
 * *stored*; the content-length check bounds what gets *buffered*. `readJson`
 * materialises and parses the whole body, so a cap applied to the parsed string
 * is applied once the memory is spent — the lesson `/api/upload` already records
 * in a comment, from a measured 300 MB post that cost ~1.8 GB of RSS.
 */
describe("what a save is refused for before it is read", () => {
  const URI = "viking://user/jasper/memories/soul.md";

  it("refuses an enormous body on its declared length, before parsing it", async () => {
    const calls = stubOv();

    /*
     * The body is a stream that fails on the first read.
     *
     * That is what makes the assertion mean something. A route that reached
     * `readJson` could only answer with a parse failure — `readJson` treats an
     * unparseable body as `{}`, so `content` would be missing and the answer
     * would be 400 INVALID_ARGUMENT. Getting 413 TOO_LARGE is only reachable
     * from the check that runs *before* the read.
     */
    const response = await appFor().request(`/api/file?uri=${encodeURIComponent(URI)}`, {
      method: "PUT",
      headers: { ...FROM_THE_APP, "content-length": String(9_000_000_000) },
      body: new ReadableStream({
        pull() {
          throw new Error("this body cannot be read");
        },
      }),
      // Node's fetch requires this for a stream body.
      duplex: "half",
    } as RequestInit);

    expect(response.status).toBe(413);
    expect(((await response.json()) as { error: { code: string } }).error.code).toBe(
      "TOO_LARGE",
    );
    expect(calls).toHaveLength(0);
  });

  it("still refuses an oversize body that lied about its length", async () => {
    // Content-Length can be absent or wrong, which is why the character check
    // stays as the real bound rather than being replaced by the one above.
    const calls = stubOv();
    const response = await appFor().request(`/api/file?uri=${encodeURIComponent(URI)}`, {
      method: "PUT",
      headers: { ...FROM_THE_APP, "content-length": "10" },
      body: JSON.stringify({ content: "x".repeat(MAX_EDIT_CHARS + 1) }),
    });

    expect(response.status).toBe(413);
    expect(calls).toHaveLength(0);
  });

  it("lets a legitimate save through at the character cap", async () => {
    /*
     * What pins the byte factor, and it has to be at the cap to do that.
     *
     * The first version of this test sent a thousand control characters — six
     * kilobytes against a twelve-megabyte gate — so every factor from x1 upward
     * passed it. Measured: with that fixture, changing `* 6` to `* 1` left all
     * 385 tests green, which is the exact failure the test was written to prevent.
     *
     * A control character escapes to six bytes for one unit of `String.length`,
     * so a body at the cap is 6 x MAX_EDIT_CHARS plus whatever JSON wraps it.
     * That is over `x 6` alone, which is why the constant carries slack: refusing
     * a save that is inside the cap people are told about is the one thing this
     * gate must not do.
     *
     * Sent *with* the stamp, because that is the request the editor actually
     * makes and because the envelope is what broke last time: the slack was fitted
     * to `{"content":"..."}` at fourteen bytes, the `seen` field took the envelope
     * to seventy-one, and a save at exactly the cap started coming back 413. A
     * body-shape change must fail here rather than in somebody's editor.
     */
    const content = "\u0001".repeat(MAX_EDIT_CHARS);
    const seen = { modTime: "2026-09-14T06:19:55Z", size: MAX_EDIT_CHARS };
    const body = JSON.stringify({ content, seen });
    // Six bytes per character plus the envelope, and the envelope is bigger than
    // the text-only one it was once sized against.
    expect(body.length).toBeGreaterThan(MAX_EDIT_CHARS * 6 + 14);

    const uri = "viking://user/jasper/resources/ctl.md";
    const calls = stubOv((url) => {
      if (url.includes("/content/write")) return { uri, mode: "replace" };
      if (!url.includes("/fs/stat")) return {};
      return { name: "ctl.md", uri, isDir: false, ...seen };
    });
    const response = await appFor().request(`/api/file?uri=${encodeURIComponent(uri)}`, {
      method: "PUT",
      headers: { ...FROM_THE_APP, "content-length": String(body.length) },
      body,
    });

    expect(response.status).toBe(200);
    expect(calls.some((call) => call.url.includes("/content/write"))).toBe(true);
  });

  it("checks the scope before the size, so a stranger learns nothing", async () => {
    const calls = stubOv();
    const response = await appFor().request(
      `/api/file?uri=${encodeURIComponent("viking://user/ada/memories/soul.md")}`,
      {
        method: "PUT",
        headers: { ...FROM_THE_APP, "content-length": String(9_000_000_000) },
        body: JSON.stringify({ content: "hello" }),
      },
    );

    expect(response.status).toBe(403);
    expect(calls).toHaveLength(0);
  });
});

/**
 * Refusing a save that would replace somebody else's write.
 *
 * OpenViking offers nothing to build this on: `content/write` takes a uri and
 * content, there is no conditional write, and a read carries no version tag. So
 * the editor sends back the modified time and size it opened on, and the route
 * compares them against what is stored now — free, since it already stats the
 * file for two other checks.
 *
 * The realistic case is an agent or a cron writing a memory while somebody has it
 * open in the dashboard, which on this user's cluster is a daily occurrence.
 */
describe("saving over something that changed underneath", () => {
  const URI = "viking://user/jasper/memories/soul.md";

  /** Stat answers with a chosen stamp, and accepts the write. */
  function stored(modTime: string, size: number) {
    return stubOv((url) => {
      if (url.includes("/content/write")) return { uri: URI, mode: "replace" };
      if (!url.includes("/fs/stat")) return {};
      return { name: "soul.md", uri: URI, isDir: false, size, modTime };
    });
  }

  function save(body: unknown) {
    return appFor().request(`/api/file?uri=${encodeURIComponent(URI)}`, {
      method: "PUT",
      headers: FROM_THE_APP,
      body: JSON.stringify(body),
    });
  }

  const SEEN = { modTime: "2026-09-14T06:19:55Z", size: 12 };

  it("writes when the file is still what the editor opened on", async () => {
    const calls = stored(SEEN.modTime, SEEN.size);

    const response = await save({ content: "mine", seen: SEEN });

    expect(response.status).toBe(200);
    expect(calls.some((call) => call.url.includes("/content/write"))).toBe(true);
  });

  it("refuses when the time moved, and does not write", async () => {
    // Measured against the lab cluster: a write moves `modTime` from
    // 06:19:55Z to 06:19:59Z and the size from 12 to 35.
    const calls = stored("2026-09-14T06:19:59Z", 35);

    const response = await save({ content: "mine", seen: SEEN });

    expect(response.status).toBe(409);
    const body = (await response.json()) as { error: { code: string; message: string } };
    expect(body.error.code).toBe("STALE_EDIT");
    expect(body.error.message).toContain("changed since you opened it");
    expect(calls.some((call) => call.url.includes("/content/write"))).toBe(false);
  });

  it("refuses when only the size moved, which is what covers a same-second write", async () => {
    /*
     * `modTime` is second-resolution, so a write inside the same second is
     * invisible to it. The size is compared as well precisely for that: it closes
     * every same-second change except one that leaves the file exactly as long.
     */
    const calls = stored(SEEN.modTime, 999);

    expect((await save({ content: "mine", seen: SEEN })).status).toBe(409);
    expect(calls.some((call) => call.url.includes("/content/write"))).toBe(false);
  });

  it("writes regardless when the editor sends no stamp", async () => {
    // How "Overwrite theirs" gets its way once the clash has been reported. An
    // absent stamp is a decision, not a missing field.
    const calls = stored("2026-09-14T09:00:00Z", 4096);

    expect((await save({ content: "mine" })).status).toBe(200);
    expect(calls.some((call) => call.url.includes("/content/write"))).toBe(true);
  });

  it("ignores a half-formed stamp rather than refusing every save", async () => {
    // Compared against `undefined`, a partial stamp would never match and would
    // make the file permanently unsaveable.
    const calls = stored(SEEN.modTime, SEEN.size);

    for (const seen of [{}, { modTime: SEEN.modTime }, { size: 12 }, "nonsense", null]) {
      const response = await save({ content: "mine", seen });
      expect(response.status, JSON.stringify(seen)).toBe(200);
    }
    expect(calls.filter((call) => call.url.includes("/content/write"))).toHaveLength(5);
  });

  it("answers with the stamp from after the write, not the one it checked", async () => {
    /*
     * The distinction is the whole point, and it needs a stub that changes: with a
     * constant stat, echoing the *pre-write* stamp passes every assertion, and a
     * route that did so would make every second save from an open editor a false
     * 409 in production.
     *
     * So this stat answers differently once the write has gone through, the way a
     * real one does.
     */
    let written = false;
    const calls = stubOv((url) => {
      if (url.includes("/content/write")) {
        written = true;
        return { uri: URI, mode: "replace" };
      }
      if (!url.includes("/fs/stat")) return {};
      return written
        ? { name: "soul.md", uri: URI, isDir: false, size: 4, modTime: "AFTER" }
        : { name: "soul.md", uri: URI, isDir: false, ...SEEN };
    });

    const response = await save({ content: "mine", seen: SEEN });

    expect(await response.json()).toEqual({ modTime: "AFTER", size: 4 });
    // Two stats: the one that guarded the write, and the one that describes it.
    expect(calls.filter((call) => call.url.includes("/fs/stat"))).toHaveLength(2);
  });

  it("answers an empty stamp when it cannot see what the file became", async () => {
    /*
     * The post-write stat can fail — lock contention or a 5xx right after a write
     * is exactly when. Saying so lets the editor stop guarding rather than guard
     * against a stamp it would know to be wrong.
     */
    let written = false;
    stubOv((url) => {
      if (url.includes("/content/write")) {
        written = true;
        return { uri: URI, mode: "replace" };
      }
      if (!url.includes("/fs/stat")) return {};
      if (written) {
        return new Response(
          JSON.stringify({
            status: "error",
            error: { code: "UNAVAILABLE", message: "busy" },
          }),
          { status: 503, headers: { "content-type": "application/json" } },
        );
      }
      return { name: "soul.md", uri: URI, isDir: false, ...SEEN };
    });

    const response = await save({ content: "mine", seen: SEEN });

    // The write itself succeeded, so this is a 200 carrying an honest "unknown".
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ modTime: "", size: 0 });
  });
});
