/**
 * The Files tree loads one folder at a time.
 *
 * It used to ask OpenViking for one recursive listing of the whole scope. That
 * listing stops at 2000 entries and at three levels, so on a full store whole
 * branches went missing, and a folder that came back with some of its children
 * was never asked for the rest.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { buildTree, unfetchedFolders } from "../src/client/lib/tree";
import { buildServices, createApp } from "../src/server/app";
import { loadConfig } from "../src/server/env";
import type { Node } from "../src/shared/schemas";

function appFor() {
  return createApp(
    buildServices(
      loadConfig({
        OV_URL: "http://openviking:1933",
        PUBLIC_ORIGIN: "http://localhost:8080",
        SESSION_SECRET: "a".repeat(32),
        AUTH_MODE: "dev",
        DEV_USER: "jasper",
        KEY_SOURCE: "static-map",
        OV_API_KEY_MAP: JSON.stringify({ jasper: "ov_jasper" }),
      }),
    ),
  );
}

function node(relPath: string, isDir: boolean): Node {
  return {
    uri: `viking://user/jasper/${relPath}`,
    name: relPath.replace(/\/$/, "").split("/").pop() ?? relPath,
    isDir,
    size: isDir ? 0 : 100,
    modTime: "2026-09-07T16:42:00Z",
    kind: isDir ? "DIR" : "MD",
    relPath,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("/api/tree", () => {
  it("lists only the top level, not the whole scope", async () => {
    const urls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request) => {
        urls.push(typeof input === "string" ? input : input.toString());
        return new Response(JSON.stringify({ status: "ok", result: [], time: 0 }), {
          headers: { "content-type": "application/json" },
        });
      }),
    );

    await appFor().request("/api/tree");

    const listings = urls.filter((url) => url.includes("/api/v1/fs/ls"));
    expect(listings).toHaveLength(1);
    expect(listings[0]).toContain("recursive=false");
  });
});

describe("/api/list", () => {
  it("lists one folder without asking for its abstract or overview", async () => {
    const urls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request) => {
        urls.push(typeof input === "string" ? input : input.toString());
        return new Response(JSON.stringify({ status: "ok", result: [], time: 0 }), {
          headers: { "content-type": "application/json" },
        });
      }),
    );

    const response = await appFor().request(
      `/api/list?uri=${encodeURIComponent("viking://user/jasper/resources/docs/")}`,
    );

    expect(response.status).toBe(200);
    expect(urls).toHaveLength(1);
    expect(urls[0]).toContain("/api/v1/fs/ls");
    expect(urls[0]).toContain("recursive=false");
  });
});

describe("which folders still need fetching", () => {
  it("includes a folder that already shows some children but was never fetched", () => {
    // A cut-off listing can bring a folder with part of its contents. Treating
    // any child as "already loaded" hid the rest of that folder for good.
    const roots = buildTree([node("docs/", true), node("docs/a.md", false)]);
    expect(unfetchedFolders(roots, new Set()).map((n) => n.relPath)).toEqual(["docs/"]);
  });

  it("skips a folder once it has been fetched, even when it was empty", () => {
    const roots = buildTree([node("docs/", true), node("empty/", true)]);
    const fetched = new Set([
      "viking://user/jasper/docs/",
      "viking://user/jasper/empty/",
    ]);
    expect(unfetchedFolders(roots, fetched)).toEqual([]);
  });

  it("finds folders at any depth", () => {
    const roots = buildTree([node("a/", true), node("a/b/", true), node("a/b/c/", true)]);
    const fetched = new Set(["viking://user/jasper/a/"]);
    expect(unfetchedFolders(roots, fetched).map((n) => n.relPath)).toEqual([
      "a/b/",
      "a/b/c/",
    ]);
  });
});
