/**
 * The three things a person can now do to a file: delete it, move it, and hand
 * it back to OpenViking to describe again.
 *
 * All three are writes, so the interesting cases are the refusals. A delete
 * that reaches outside the caller's scopes, a move that drops a folder inside
 * itself, and a reindex that runs the cheap mode are each a bug you would only
 * notice against a live cluster, which is exactly why they are pinned here.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { buildServices, createApp } from "../src/server/app";
import { loadConfig } from "../src/server/env";
import { statusFor } from "../src/server/ov";

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
