import { afterEach, describe, expect, it, vi } from "vitest";
import { buildServices, createApp, safeReturnTo } from "../src/server/app";
import { loadConfig } from "../src/server/env";

/** The origin the dashboard is served from in these tests. */
const ORIGIN = "http://localhost:8080";

/**
 * What the dashboard's own pages send on a state-changing request.
 *
 * Anything without it is treated as coming from somewhere else and refused,
 * which is what stops a web page writing files with a signed-in person's
 * ambient credentials.
 */
const FROM_THE_APP = { origin: ORIGIN };

/** Config for a signed-in-by-default app, so routes can be exercised directly. */
function devConfig(extra: Record<string, string> = {}) {
  return loadConfig({
    OV_URL: "http://openviking:1933",
    PUBLIC_ORIGIN: ORIGIN,
    SESSION_SECRET: "a".repeat(32),
    AUTH_MODE: "dev",
    DEV_USER: "jasper",
    KEY_SOURCE: "static-map",
    OV_API_KEY_MAP: JSON.stringify({ jasper: "ov_jasper", ada: "ov_ada" }),
    ...extra,
  });
}

function appFor(config = devConfig()) {
  return createApp(buildServices(config));
}

/** Answer OpenViking calls with the documented envelope. */
function stubOv(handler: (url: string, init?: RequestInit) => unknown) {
  const calls: { url: string; headers: Record<string, string> }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const headers: Record<string, string> = {};
      new Headers(init?.headers).forEach((value, key) => {
        headers[key] = value;
      });
      calls.push({ url, headers });
      const result = handler(url, init);
      return new Response(JSON.stringify({ status: "ok", result, time: 0.01 }), {
        headers: { "content-type": "application/json" },
      });
    }),
  );
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("who is calling", () => {
  it("reports the fixed identity in dev mode", async () => {
    const response = await appFor().request("/api/session");
    expect(await response.json()).toEqual({
      signedIn: true,
      // Nothing here holds the session, so the page must not offer to end it.
      canSignOut: false,
      // Nothing expires here, so the Account page shows no time.
      expiresAt: null,
      viewer: {
        sub: "jasper",
        name: "jasper",
        email: "dev@localhost",
        account: "jasper",
        user: "jasper",
      },
    });
  });

  it("refuses the API when nobody is signed in", async () => {
    const config = devConfig({
      AUTH_MODE: "oidc",
      OIDC_ISSUER: "https://vault.example",
      OIDC_CLIENT_ID: "id",
      OIDC_CLIENT_SECRET: "secret",
    });
    const response = await appFor(config).request("/api/tree");
    expect(response.status).toBe(401);
    expect(await response.json()).toEqual({
      error: { code: "UNAUTHENTICATED", message: "not signed in" },
    });
  });

  it("offers a login URL rather than data when signed out", async () => {
    const config = devConfig({
      AUTH_MODE: "oidc",
      OIDC_ISSUER: "https://vault.example",
      OIDC_CLIENT_ID: "id",
      OIDC_CLIENT_SECRET: "secret",
    });
    const response = await appFor(config).request("/api/session");
    expect(await response.json()).toEqual({
      signedIn: false,
      loginUrl: "/auth/login",
      mode: "redirect",
    });
  });

  it("reads the identity a trusted proxy injected", async () => {
    const config = devConfig({ AUTH_MODE: "trusted-header" });
    const response = await appFor(config).request("/api/session", {
      headers: { "x-forwarded-user": "ada@example.com" },
    });
    const body = (await response.json()) as { viewer: { user: string } };
    expect(body.viewer.user).toBe("ada");
  });
});

describe("calling OpenViking as the right person", () => {
  it("sends each caller's own key through one shared, warm key cache", async () => {
    const calls = stubOv(() => []);
    const config = devConfig({ AUTH_MODE: "trusted-header" });

    // One app, so both requests share one KeyResolver and one cache. Two apps
    // would give each request its own cache and prove nothing about reuse,
    // which is the failure this test exists to catch.
    const app = appFor(config);

    await app.request("/api/tree", { headers: { "x-forwarded-user": "jasper@x.com" } });
    await app.request("/api/tree", { headers: { "x-forwarded-user": "ada@x.com" } });
    // Jasper again, now that ada's key has been through the same cache.
    await app.request("/api/tree", { headers: { "x-forwarded-user": "jasper@x.com" } });

    const keys = calls.map((call) => call.headers["x-api-key"]);
    expect(keys).toEqual(["ov_jasper", "ov_ada", "ov_jasper"]);
  });

  it("refuses a user id that would escape its path", async () => {
    const calls = stubOv(() => []);
    const config = devConfig({ AUTH_MODE: "trusted-header" });

    // `fetch` normalizes `..` away, so an unchecked id here reads a Vault path
    // outside the configured prefix entirely.
    for (const hostile of ["../../ops/admin@x.com", "..@x.com", "a/b@x.com"]) {
      const response = await appFor(config).request("/api/tree", {
        headers: { "x-forwarded-user": hostile },
      });
      expect(response.status).toBe(403);
    }
    // Nothing reached OpenViking, so nothing reached Vault either.
    expect(calls).toHaveLength(0);
  });

  it("turns a flat listing into nodes with kinds and relative paths", async () => {
    stubOv(() => [
      {
        name: "notes",
        uri: "viking://user/jasper/notes/",
        isDir: true,
        size: 0,
        modTime: "2026-09-01T10:00:00Z",
      },
      {
        name: "runbook.md",
        uri: "viking://user/jasper/notes/runbook.md",
        isDir: false,
        size: 2048,
        modTime: "2026-09-02T10:00:00Z",
      },
    ]);

    const response = await appFor().request("/api/tree");
    const body = (await response.json()) as {
      nodes: { name: string; kind: string; relPath: string }[];
    };
    expect(body.nodes).toHaveLength(2);
    expect(body.nodes.map((n) => n.kind)).toEqual(["DIR", "MD"]);
    expect(body.nodes.map((n) => n.relPath)).toEqual(["notes/", "notes/runbook.md"]);
  });
});

describe("guards", () => {
  it("keeps a post-login redirect on this site", () => {
    expect(safeReturnTo("/files")).toBe("/files");
    expect(safeReturnTo("https://evil.example")).toBe("/");
    expect(safeReturnTo("//evil.example")).toBe("/");
    expect(safeReturnTo(undefined)).toBe("/");
    expect(safeReturnTo("javascript:alert(1)")).toBe("/");
  });

  it("keeps a redirect on this site when it uses a backslash", () => {
    // Browsers resolve Location with WHATWG rules, where a backslash is a path
    // separator: "/\\evil.example" passes a naive "one leading slash" test and
    // then resolves to https://evil.example/.
    for (const bad of ["/\\evil.example", "/\\\\evil.example", "/\\/evil.example"]) {
      const got = safeReturnTo(bad);
      expect(new URL(got, "https://dash.example").origin, bad).toBe(
        "https://dash.example",
      );
    }
  });

  it("keeps the path, query and fragment of a real destination", () => {
    expect(safeReturnTo("/files?open=notes#/file")).toBe("/files?open=notes#/file");
  });

  it("refuses a memory sibling that merely shares the prefix", async () => {
    const calls = stubOv(() => null);
    const response = await appFor().request(
      "/api/memories?uri=viking://user/jasper/memories-archive/old.md",
      { method: "DELETE", headers: FROM_THE_APP },
    );
    expect(response.status).toBe(400);
    // The delete must not have gone upstream.
    expect(calls).toHaveLength(0);
  });

  it("rejects a uri that is not a viking uri", async () => {
    const response = await appFor().request("/api/file?uri=http://evil.example");
    expect(response.status).toBe(400);
    const body = (await response.json()) as { error: { code: string } };
    expect(body.error.code).toBe("INVALID_URI");
  });

  it("refuses to delete anything that is not a memory", async () => {
    const response = await appFor().request(
      "/api/memories?uri=viking://user/jasper/notes/runbook.md",
      { method: "DELETE", headers: FROM_THE_APP },
    );
    expect(response.status).toBe(400);
    const body = (await response.json()) as { error: { message: string } };
    expect(body.error.message).toMatch(/not a memory/);
  });

  it("allows deleting a real memory", async () => {
    stubOv(() => null);
    const response = await appFor().request(
      "/api/memories?uri=viking://user/jasper/memories/preferences/tone.md",
      { method: "DELETE", headers: FROM_THE_APP },
    );
    expect(response.status).toBe(204);
  });
});

describe("search", () => {
  it("answers an empty query without calling OpenViking", async () => {
    const calls = stubOv(() => []);
    const response = await appFor().request("/api/search?q=%20&mode=hybrid");
    expect(await response.json()).toEqual({
      query: "",
      mode: "hybrid",
      hits: [],
      counts: { meaning: 0, exact: 0 },
    });
    expect(calls).toHaveLength(0);
  });

  it("falls back to hybrid when the mode is not one we know", async () => {
    stubOv((url) =>
      url.includes("grep")
        ? { matches: [], count: 0 }
        : { memories: [], resources: [], skills: [], total: 0 },
    );
    const response = await appFor().request("/api/search?q=vault&mode=nonsense");
    const body = (await response.json()) as { mode: string };
    expect(body.mode).toBe("hybrid");
  });

  it("says which words an exact hit matched", async () => {
    stubOv((url) => {
      if (url.includes("/search/grep")) {
        return {
          matches: [
            {
              uri: "viking://user/notes/vault.md",
              line: 3,
              content: "vault rotation is quarterly",
            },
          ],
          count: 1,
        };
      }
      return { memories: [], resources: [], skills: [], total: 0 };
    });

    const response = await appFor().request("/api/search?q=vault%20rotation&mode=exact");
    const body = (await response.json()) as {
      hits: { name: string; match: { exact: string[]; meaning: number | null } }[];
      counts: { exact: number };
    };
    expect(body.hits).toHaveLength(1);
    expect(body.hits[0]?.name).toBe("vault.md");
    expect(body.hits[0]?.match.exact.sort()).toEqual(["rotation", "vault"]);
    expect(body.hits[0]?.match.meaning).toBeNull();
  });

  it("marks a hit that came back on both faces", async () => {
    stubOv((url) => {
      if (url.includes("/search/grep")) {
        return {
          matches: [{ uri: "viking://user/notes/vault.md", line: 1, content: "vault" }],
          count: 1,
        };
      }
      return {
        memories: [],
        resources: [
          {
            uri: "viking://user/notes/vault.md",
            score: 0.81,
            abstract: "How the vault key is rotated.",
            overview: null,
            category: "",
            match_reason: "",
          },
        ],
        skills: [],
        total: 1,
      };
    });

    const response = await appFor().request("/api/search?q=vault&mode=hybrid");
    const body = (await response.json()) as {
      hits: { match: { exact: string[]; meaning: number | null } }[];
    };
    expect(body.hits).toHaveLength(1);
    expect(body.hits[0]?.match.meaning).toBeCloseTo(0.81);
    expect(body.hits[0]?.match.exact).toEqual(["vault"]);
  });

  it("does not caption a hit with a template that failed to render", async () => {
    // OpenViking prints a missing field into the text instead of raising, so
    // an abstract can arrive as its own error message. A result line reading
    // "no such element: dict object['task_signature']" helps nobody choose it.
    stubOv((url) => {
      if (url.includes("/search/grep")) return { matches: [], count: 0 };
      return {
        memories: [],
        resources: [
          {
            uri: "viking://user/memories/cases/mem_079fbe147f15.md",
            score: 0.7,
            abstract:
              "A case about {{ no such element: dict object['task_signature'] }} retries.",
            overview: null,
            category: "",
            match_reason: "",
          },
        ],
        skills: [],
        total: 1,
      };
    });

    const response = await appFor().request("/api/search?q=retries&mode=meaning");
    const body = (await response.json()) as { hits: { snippet: string }[] };
    expect(body.hits[0]?.snippet).toBe("A case about retries.");
  });
});
