/**
 * What the reading pane is told about one file.
 *
 * OpenViking keeps no abstract per file: `abstract(fileUri)` answers with the
 * folder's, so every file in a folder used to come back with the same words
 * under its name. The per-file description lives in the folder's overview, and
 * these cover reading it from there — and serving an image's bytes so the pane
 * can show the picture instead of a download button.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { buildServices, createApp } from "../src/server/app";
import { loadConfig } from "../src/server/env";

const ORIGIN = "http://localhost:8080";

/** The folder both files in these tests sit in. */
const FOLDER = "viking://user/jasper/resources/notes";

/** A folder overview shaped the way OpenViking writes them. */
const OVERVIEW = `# notes

This folder holds the design notes.

## Quick Navigation

- **How do the pieces connect?** → [diagram.png](${FOLDER}/diagram.png) — developer, agent, and five components

## Detailed Description

### [plan.md](${FOLDER}/plan.md)

The plan, in full.
`;

const FOLDER_ABSTRACT = "This folder holds the design notes.";

function devConfig(extra: Record<string, string> = {}) {
  return loadConfig({
    OV_URL: "http://openviking:1933",
    PUBLIC_ORIGIN: ORIGIN,
    SESSION_SECRET: "a".repeat(32),
    AUTH_MODE: "dev",
    DEV_USER: "jasper",
    KEY_SOURCE: "static-map",
    OV_API_KEY_MAP: JSON.stringify({ jasper: "ov_jasper" }),
    ...extra,
  });
}

function appFor(config = devConfig()) {
  return createApp(buildServices(config));
}

/**
 * One file in the folder, as OpenViking's `stat` describes it.
 *
 * `size` is optional here on purpose: upstream may omit the field, and
 * `entrySchema` then defaults it to 0.
 */
interface Entry {
  name: string;
  size?: number;
  /** Bytes to answer a download with. Defaults to three. */
  bytes?: number;
}

/**
 * Answer the calls a file view makes.
 *
 * `stat` is answered from `entries`, and abstract and overview only ever for
 * the folder — asking for either on a file is exactly the mistake being fixed,
 * so this refuses it rather than quietly returning the folder's words.
 */
function stubOv(entries: Entry[], overview = OVERVIEW) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL) => {
      const url = new URL(typeof input === "string" ? input : input.toString());
      const uri = url.searchParams.get("uri") ?? "";
      const path = url.pathname;

      if (path === "/api/v1/content/download") {
        const name = uri.split("/").pop() ?? "";
        const entry = entries.find((candidate) => candidate.name === name);
        return new Response(new Uint8Array(entry?.bytes ?? 3), {
          headers: { "content-type": "application/octet-stream" },
        });
      }

      let result: unknown = null;
      if (path === "/api/v1/fs/stat") {
        const name = uri.split("/").pop() ?? "";
        const entry = entries.find((candidate) => candidate.name === name);
        result = {
          name,
          uri,
          isDir: !entry,
          // Omitted entirely when the entry declares none, which is what a
          // `stat` without a size field looks like to the schema.
          ...(entry?.size === undefined ? {} : { size: entry.size }),
          modTime: "2026-09-09T10:00:00Z",
        };
      } else if (path === "/api/v1/content/read") {
        result = `the text of ${uri.split("/").pop()}`;
      } else if (path === "/api/v1/content/abstract") {
        result = uri === FOLDER ? FOLDER_ABSTRACT : "";
      } else if (path === "/api/v1/content/overview") {
        result = uri === FOLDER ? overview : "";
      }

      return new Response(JSON.stringify({ status: "ok", result, time: 0.01 }), {
        headers: { "content-type": "application/json" },
      });
    }),
  );
}

async function fileDetail(app: ReturnType<typeof appFor>, name: string) {
  const response = await app.request(
    `/api/file?uri=${encodeURIComponent(`${FOLDER}/${name}`)}`,
  );
  expect(response.status).toBe(200);
  return (await response.json()) as {
    abstract: string;
    folderSummary: string;
    binary: boolean;
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("describing one file", () => {
  it("answers with the description written for that file", async () => {
    stubOv([{ name: "plan.md" }]);
    const detail = await fileDetail(appFor(), "plan.md");
    expect(detail.abstract).toBe("The plan, in full.");
  });

  it("falls back to the navigation line when the detail was truncated away", async () => {
    stubOv([{ name: "diagram.png" }]);
    const detail = await fileDetail(appFor(), "diagram.png");
    expect(detail.abstract).toBe("developer, agent, and five components");
  });

  it("does not give two files in a folder the same description", async () => {
    // The bug this replaced: every file in a folder came back with the
    // folder's abstract, so a described image looked undescribed.
    stubOv([{ name: "plan.md" }, { name: "diagram.png" }]);
    const app = appFor();
    const plan = await fileDetail(app, "plan.md");
    const diagram = await fileDetail(app, "diagram.png");
    expect(plan.abstract).not.toBe(diagram.abstract);
    expect(plan.abstract).not.toBe(FOLDER_ABSTRACT);
    expect(diagram.abstract).not.toBe(FOLDER_ABSTRACT);
  });

  it("carries the folder's own summary separately, to be labelled as such", async () => {
    stubOv([{ name: "plan.md" }]);
    const detail = await fileDetail(appFor(), "plan.md");
    expect(detail.folderSummary).toBe(FOLDER_ABSTRACT);
  });

  it("leaves the description empty when the overview says nothing about it", async () => {
    stubOv([{ name: "stray.png" }]);
    const detail = await fileDetail(appFor(), "stray.png");
    expect(detail.abstract).toBe("");
    // The folder's summary still comes back, for the pane to fall back to.
    expect(detail.folderSummary).toBe(FOLDER_ABSTRACT);
  });

  it("still answers when the folder has no overview at all", async () => {
    stubOv([{ name: "plan.md" }], "");
    const detail = await fileDetail(appFor(), "plan.md");
    expect(detail.abstract).toBe("");
  });
});

describe("showing an image in the reading pane", () => {
  async function image(name: string, entry: Partial<Entry> = {}) {
    stubOv([{ name, size: 1024, ...entry }]);
    return appFor().request(`/api/image?uri=${encodeURIComponent(`${FOLDER}/${name}`)}`, {
      // What an <img> on our own page sends: no Origin, and the fetch metadata
      // that separates it from a picture loaded by somebody else's site.
      headers: { "sec-fetch-site": "same-origin" },
    });
  }

  it("serves a png as a png, and tells the browser not to guess", async () => {
    const response = await image("diagram.png");
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("image/png");
    expect(response.headers.get("content-disposition")).toBe("inline");
    expect((await response.arrayBuffer()).byteLength).toBe(3);
  });

  /*
   * The two headers below are the actual defence, not the format allowlist:
   * the content-type is chosen from the file's name, so a `.png` holding
   * markup is still served as `image/png`. `nosniff` is what stops the browser
   * acting on it. Asserted separately so that deleting either one fails a test
   * that says why it mattered.
   */
  it("forbids the browser reinterpreting what it was handed", async () => {
    const response = await image("diagram.png");
    expect(response.headers.get("x-content-type-options")).toBe("nosniff");
  });

  it("lets the response fetch nothing of its own", async () => {
    const response = await image("diagram.png");
    expect(response.headers.get("content-security-policy")).toBe(
      "default-src 'none'; sandbox",
    );
  });

  it("refuses a picture loaded by somebody else's page", async () => {
    stubOv([{ name: "diagram.png", size: 1024 }]);
    const response = await appFor().request(
      `/api/image?uri=${encodeURIComponent(`${FOLDER}/diagram.png`)}`,
      { headers: { origin: "https://evil.example", "sec-fetch-site": "cross-site" } },
    );
    expect(response.status).toBe(403);
    const body = (await response.json()) as { error: { code: string } };
    expect(body.error.code).toBe("BAD_ORIGIN");
  });

  it("refuses an svg, which is a document that can carry script", async () => {
    const response = await image("logo.svg");
    expect(response.status).toBe(400);
    const body = (await response.json()) as { error: { message: string } };
    expect(body.error.message).toMatch(/not an image the reader can show/);
  });

  it("refuses a file that is not an image", async () => {
    const response = await image("plan.md");
    expect(response.status).toBe(400);
  });

  it("refuses an image too large to hold in memory for a page", async () => {
    const response = await image("huge.png", { size: 40 * 1024 * 1024 });
    expect(response.status).toBe(413);
  });

  it("refuses an oversized image whose stat declared no size at all", async () => {
    // `entrySchema` defaults a missing size to 0, so an up-front check alone
    // waves through a file of any length.
    const response = await image("huge.png", {
      size: undefined,
      bytes: 40 * 1024 * 1024,
    });
    expect(response.status).toBe(413);
  });
});
