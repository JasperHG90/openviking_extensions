/**
 * What the dashboard refuses.
 *
 * Two things are being defended here. A cookie and a proxy header are
 * *ambient* — the browser attaches them to whatever request it is told to
 * make, including one a hostile page started — so a state-changing call has to
 * prove it came from this site. And an upload's destination has to stay inside
 * the scopes this dashboard offers, however it is spelled.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { buildServices, createApp, resolveTarget, rootsFor } from "../src/server/app";
import { loadConfig } from "../src/server/env";
import type { Viewer } from "../src/shared/schemas";

const ORIGIN = "https://dash.example";

const JASPER: Viewer = {
  sub: "jasper",
  name: "jasper",
  email: "jasper@example.com",
  account: "jasper",
  user: "jasper",
};

function devConfig(extra: Record<string, string> = {}) {
  return loadConfig({
    OV_URL: "http://openviking:1933",
    PUBLIC_ORIGIN: ORIGIN,
    SESSION_SECRET: "a".repeat(32),
    AUTH_MODE: "dev",
    DEV_USER: "jasper",
    KEY_SOURCE: "static-map",
    OV_API_KEY_MAP: JSON.stringify({ jasper: "ov_jasper" }),
    OV_ROOT: "viking://user",
    ...extra,
  });
}

function appFor(config = devConfig()) {
  return createApp(buildServices(config));
}

/** Answer OpenViking calls with the documented envelope, recording each one. */
function stubOv(handler: (url: string) => unknown = () => []) {
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
      return new Response(
        JSON.stringify({ status: "ok", result: handler(url), time: 0.01 }),
        { headers: { "content-type": "application/json" } },
      );
    }),
  );
  return calls;
}

/** An import is two upstream calls: bytes to `temp_upload`, then the id. */
function stubImport() {
  return stubOv((url) =>
    url.includes("temp_upload") ? { temp_file_id: "tmp_1" } : { ok: true },
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a web page trying to use somebody's ambient credentials", () => {
  /** Upload a file the way a hostile page would: cross-site. */
  function upload(app: ReturnType<typeof appFor>, headers: Record<string, string>) {
    const form = new FormData();
    form.append("file", new File(["pwned"], "note.md", { type: "text/markdown" }));
    form.append("to", "viking://user");
    return app.request("/api/upload", { method: "POST", headers, body: form });
  }

  // Multipart needs no preflight, so the browser sends it and the server acts
  // on it. In these two modes the identity rides along on every request.
  for (const mode of ["dev", "trusted-header"] as const) {
    it(`refuses a cross-site upload in ${mode} mode`, async () => {
      const calls = stubOv();
      const response = await upload(appFor(devConfig({ AUTH_MODE: mode })), {
        origin: "https://evil.example",
        "sec-fetch-site": "cross-site",
        "x-forwarded-user": "jasper@example.com",
      });

      expect(response.status).toBe(403);
      expect(calls).toHaveLength(0);
    });
  }

  it("refuses a cross-site delete too", async () => {
    const calls = stubOv();
    const response = await appFor().request(
      "/api/memories?uri=viking://user/memories/a.md",
      { method: "DELETE", headers: { origin: "https://evil.example" } },
    );
    expect(response.status).toBe(403);
    expect(calls).toHaveLength(0);
  });

  it("refuses a cross-site logout, so a page cannot sign somebody out", async () => {
    const response = await appFor().request("/auth/logout", {
      method: "POST",
      headers: {
        origin: "https://evil.example",
        "content-type": "application/x-www-form-urlencoded",
      },
    });
    expect(response.status).toBe(403);
    expect(response.headers.get("set-cookie")).toBeNull();
  });

  it("says why, rather than answering with an empty message", async () => {
    // Hono builds a CSRF rejection from a bare Response, which leaves the
    // message empty unless the error handler fills it in.
    const response = await appFor().request(
      "/api/memories?uri=viking://user/memories/a.md",
      {
        method: "DELETE",
        headers: { origin: "https://evil.example" },
      },
    );
    const body = (await response.json()) as { error: { code: string; message: string } };
    // A mismatched Origin now names both sides. The generic REFUSED fallback
    // still covers Hono's other csrf refusals, but "refused" on its own sent
    // people hunting through server code for what was a misconfigured
    // PUBLIC_ORIGIN.
    expect(body.error.code).toBe("BAD_ORIGIN");
    expect(body.error.message).toMatch(/PUBLIC_ORIGIN/);
  });

  it("still lets the dashboard's own pages through", async () => {
    stubOv(() => null);
    const response = await appFor().request(
      "/api/memories?uri=viking://user/memories/a.md",
      { method: "DELETE", headers: { origin: ORIGIN } },
    );
    expect(response.status).toBe(204);
  });

  it("accepts the browser's own same-origin form post", async () => {
    // A same-site request carries Sec-Fetch-Site instead of a matching Origin
    // when it goes through the dev proxy, and both are accepted.
    stubOv(() => null);
    const response = await appFor().request(
      "/api/memories?uri=viking://user/memories/a.md",
      { method: "DELETE", headers: { "sec-fetch-site": "same-origin" } },
    );
    expect(response.status).toBe(204);
  });

  it("leaves GET alone, which changes nothing and is unreadable cross-origin", async () => {
    stubOv();
    const response = await appFor().request("/api/tree", {
      headers: { origin: "https://evil.example" },
    });
    expect(response.status).toBe(200);
    expect(response.headers.get("access-control-allow-origin")).toBeNull();
  });
});

describe("who the caller is", () => {
  it("refuses a proxy header with no local part to it", async () => {
    // "@example.com" splits to an empty user, which would render a templated
    // root as its own parent — everybody's tree.
    const config = devConfig({ AUTH_MODE: "trusted-header" });
    const response = await appFor(config).request("/api/tree", {
      headers: { "x-forwarded-user": "@example.com" },
    });
    expect(response.status).toBe(401);
  });

  it("refuses to build a scope for a caller with no user id", () => {
    const templated = devConfig({ OV_ROOT: "viking://user/{user}" });
    expect(() => rootsFor(templated, { ...JASPER, user: "" })).toThrow(/no user id/);
  });

  it("fills the user and account into a templated root", () => {
    const templated = devConfig({ OV_ROOT: "viking://user/{user}", OV_ACCOUNT: "lab" });
    expect(rootsFor(templated, JASPER).user).toBe("viking://user/jasper");
  });
});

describe("where a file may land", () => {
  const config = devConfig();

  it("defaults an empty destination to the person's own scope", () => {
    // Files land under resources: OpenViking refuses a bare root with "to must
    // target resource content", which is a real 502 against the cluster.
    expect(resolveTarget(config, JASPER, "")).toBe("viking://user/resources");
    expect(resolveTarget(config, JASPER, "   ")).toBe("viking://user/resources");
  });

  it("accepts a folder inside either scope", () => {
    expect(resolveTarget(config, JASPER, "viking://user/clips")).toBe(
      "viking://user/clips",
    );
    expect(resolveTarget(config, JASPER, "viking://resources/clips/")).toBe(
      "viking://resources/clips",
    );
  });

  it("refuses a destination outside both scopes", () => {
    for (const bad of [
      "viking://user2/notes",
      "viking://userland",
      "viking://system/config",
      "file:///etc/passwd",
    ]) {
      expect(() => resolveTarget(config, JASPER, bad), bad).toThrow(/outside the scopes/);
    }
  });

  it("refuses a destination that walks up out of its scope", () => {
    expect(() => resolveTarget(config, JASPER, "viking://user/../system")).toThrow(
      /walks outside/,
    );
  });

  it("refuses a climb that arrives percent-encoded", () => {
    // Asserted on the traversal message specifically, not on "it threw
    // something": the blanket `%` rule below would also reject every one of
    // these, so a looser assertion would still pass with the decoding removed
    // and the traversal check would be untested.
    for (const bad of [
      "viking://user/%2e%2e/other",
      "viking://user/..%2fother",
      "viking://user/%2E%2E/other",
      "viking://user/a%2f..%2fb",
    ]) {
      expect(() => resolveTarget(config, JASPER, bad), bad).toThrow(/walks outside/);
    }
  });

  it("refuses a climb written with backslashes", () => {
    for (const bad of ["viking://user/..\\..\\system", "viking://user/a\\..\\b"]) {
      expect(() => resolveTarget(config, JASPER, bad), bad).toThrow(/walks outside/);
    }
  });

  it("refuses an escape even where it decodes to something harmless", () => {
    // Allowing any escape means deciding whose decoding wins, ours or
    // OpenViking's. Refusing them all is what closes double encoding without a
    // decode loop.
    for (const bad of ["viking://user/my%20clips", "viking://user/%252e%252e/x"]) {
      expect(() => resolveTarget(config, JASPER, bad), bad).toThrow(/escape/);
    }
  });

  it("offers no shared scope when the deployment has none", () => {
    const alone = devConfig({ OV_SHARED_ROOT: "" });
    expect(rootsFor(alone, JASPER).shared).toBeNull();
    expect(() => resolveTarget(alone, JASPER, "viking://resources/x")).toThrow(
      /outside the scopes/,
    );
  });

  it("reports the name the file was actually stored under", async () => {
    // The bytes land under a sanitized name, so answering with the name as
    // uploaded would hand back a URI to a file that is not there.
    stubImport();
    const form = new FormData();
    form.append(
      "file",
      new File(["hi"], "my report (final).md", { type: "text/markdown" }),
    );
    form.append("to", "viking://user/clips");

    const response = await appFor(config).request("/api/upload", {
      method: "POST",
      headers: { origin: ORIGIN },
      body: form,
    });

    expect(response.status).toBe(200);
    const body = (await response.json()) as { uri: string; name: string };
    expect(body.name).toBe("my_report__final_.md");
    // Each file gets a folder of its own, named after it. OpenViking does this
    // itself at a scope root but drops files in flat anywhere else, so the
    // dashboard makes the folder and the result is the same either way.
    expect(body.uri).toBe("viking://user/clips/my_report__final_/my_report__final_.md");
  });

  it("refuses an out-of-scope upload before touching OpenViking", async () => {
    const calls = stubOv();
    const form = new FormData();
    form.append("file", new File(["hello"], "note.md", { type: "text/markdown" }));
    form.append("to", "viking://somebody-else/notes");

    const response = await appFor(config).request("/api/upload", {
      method: "POST",
      headers: { origin: ORIGIN },
      body: form,
    });
    expect(response.status).toBe(403);
    const body = (await response.json()) as { error: { code: string } };
    expect(body.error.code).toBe("OUT_OF_SCOPE");
    expect(calls).toHaveLength(0);
  });
});
