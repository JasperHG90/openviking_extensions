/**
 * The same-origin guard, and the download header.
 *
 * These exist because an adversarial pass found the guard was stated
 * negatively — it refused a *mismatched* Origin, and let a request carrying no
 * Origin at all through. Hono's csrf did not cover the gap: it only inspects
 * form-shaped content types, so a JSON DELETE was protected by nothing but the
 * absence of a CORS middleware.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  buildServices,
  contentDisposition,
  createApp,
  safeZipEntry,
} from "../src/server/app";
import { loadConfig } from "../src/server/env";

const ORIGIN = "http://localhost:8080";

function devConfig(extra: Record<string, string> = {}) {
  return loadConfig({
    OV_URL: "http://openviking:1933",
    PUBLIC_ORIGIN: ORIGIN,
    SESSION_SECRET: "a".repeat(32),
    AUTH_MODE: "dev",
    DEV_USER: "jasper",
    KEY_SOURCE: "env",
    OV_API_KEY: "ov_test",
    ...extra,
  });
}

function appFor(config = devConfig()) {
  return createApp(buildServices(config));
}

/** Answer OpenViking calls, and record that they happened. */
function stubOv(result: unknown = null) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request) => {
      calls.push(typeof input === "string" ? input : input.toString());
      return new Response(JSON.stringify({ status: "ok", result, time: 0 }), {
        headers: { "content-type": "application/json" },
      });
    }),
  );
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

const MEMORY = "/api/memories?uri=viking://user/jasper/memories/preferences/a.md";

describe("a state-changing call must prove where it came from", () => {
  it("refuses a JSON delete carrying no Origin and no Sec-Fetch-Site", async () => {
    // The exact hole: Hono's csrf ignores application/json, and a guard that
    // only refuses a *mismatched* Origin sees nothing to compare.
    const calls = stubOv();
    const response = await appFor().request(MEMORY, {
      method: "DELETE",
      headers: { "content-type": "application/json" },
    });

    expect(response.status).toBe(403);
    const body = (await response.json()) as { error: { code: string } };
    expect(body.error.code).toBe("BAD_ORIGIN");
    // And nothing reached OpenViking.
    expect(calls).toHaveLength(0);
  });

  it("refuses a cross-site Origin", async () => {
    const response = await appFor().request(MEMORY, {
      method: "DELETE",
      headers: { origin: "https://evil.example" },
    });
    expect(response.status).toBe(403);
  });

  it("allows the dashboard's own page", async () => {
    stubOv();
    const response = await appFor().request(MEMORY, {
      method: "DELETE",
      headers: { origin: ORIGIN },
    });
    expect(response.status).toBe(204);
  });

  it("allows a same-origin request that carries only Sec-Fetch-Site", async () => {
    // A same-origin fetch sends no Origin on some methods, so treating a
    // missing one as failure would refuse the dashboard's own calls.
    stubOv();
    const response = await appFor().request(MEMORY, {
      method: "DELETE",
      headers: { "sec-fetch-site": "same-origin" },
    });
    expect(response.status).toBe(204);
  });
});

describe("reads that are expensive are guarded like writes", () => {
  it("refuses a cross-site search", async () => {
    // Eight terms across two scopes is sixteen greps at about twenty seconds
    // each. That is worth a hostile page's while even though it changes
    // nothing, and in trusted-header and dev the identity rides along.
    const calls = stubOv({ matches: [], count: 0 });
    const response = await appFor().request("/api/search?q=a+b+c&mode=exact", {
      headers: { origin: "https://evil.example", "sec-fetch-site": "cross-site" },
    });
    expect(response.status).toBe(403);
    expect(calls).toHaveLength(0);
  });

  it("refuses a cross-site folder download", async () => {
    const calls = stubOv();
    const response = await appFor().request(
      "/api/download?uri=viking://user/jasper/resources",
      { headers: { origin: "https://evil.example" } },
    );
    expect(response.status).toBe(403);
    expect(calls).toHaveLength(0);
  });

  it("leaves an ordinary read alone", async () => {
    stubOv([]);
    const response = await appFor().request("/api/tree", {
      headers: { origin: "https://evil.example" },
    });
    // Cheap reads are not guarded here: a web page cannot read the answer
    // anyway, since no Access-Control-Allow-Origin is ever sent.
    expect(response.status).toBe(200);
  });

  it("still answers a script with no browser headers at all", async () => {
    stubOv({ memories: [], resources: [], skills: [], total: 0 });
    const response = await appFor().request("/api/search?q=x&mode=meaning");
    expect(response.status).toBe(200);
  });
});

describe("the download filename header", () => {
  it("survives a name that cannot go in a header byte string", () => {
    // Header values are ByteStrings, so a code point above U+00FF throws
    // inside the response constructor and the download dies as a bare 500.
    const header = contentDisposition("日本語ノート.md");
    expect(() => new Headers({ "content-disposition": header })).not.toThrow();
    expect(header).toContain("filename*=UTF-8''");
    expect(header).toContain(encodeURIComponent("日本語ノート.md"));
  });

  it("keeps an ASCII fallback for clients that ignore filename*", () => {
    const header = contentDisposition("Q3 Review — final.pdf");
    const ascii = /filename="([^"]*)"/.exec(header)?.[1] ?? "";
    expect(ascii).toMatch(/^[\x20-\x7E]*$/);
    expect(() => new Headers({ "content-disposition": header })).not.toThrow();
  });

  it("cannot be broken out of with a quote or a newline", () => {
    const header = contentDisposition('evil".md\r\nX-Injected: 1');
    expect(header).not.toContain("\r");
    expect(header).not.toContain("\n");
    const ascii = /filename="([^"]*)"/.exec(header)?.[1] ?? "";
    expect(ascii).not.toContain('"');
  });
});

describe("zip entries cannot escape the archive", () => {
  it("drops traversal from a name OpenViking supplied", () => {
    expect(safeZipEntry("../../etc/passwd")).toBe("etc/passwd");
    expect(safeZipEntry("/absolute/path.md")).toBe("absolute/path.md");
    expect(safeZipEntry("a/../../b.md")).toBe("a/b.md");
  });

  it("keeps an ordinary nested path intact", () => {
    expect(safeZipEntry("notes/meetings/standup.md")).toBe("notes/meetings/standup.md");
  });

  it("never yields an empty entry name", () => {
    expect(safeZipEntry("../..")).toBe("file");
  });
});
