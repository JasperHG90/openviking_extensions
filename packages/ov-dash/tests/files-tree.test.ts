/**
 * @vitest-environment jsdom
 */

/**
 * The Files tree, driven as a component against a fake server.
 *
 * The tree loads one folder at a time, so what matters is the wiring: which
 * folders get fetched after a click, a move or a cut-off listing. The pure
 * helpers under it were correct while that wiring left folders open and empty.
 */

import { cleanup, render } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../src/client/lib/api";
import Files from "../src/client/views/Files.svelte";

const ROOT = "viking://user/jasper/resources";

/** Folders by uri, each holding the uris inside it. A uri ending in `/` is a folder. */
type Store = Map<string, string[]>;

function entry(uri: string) {
  const isDir = uri.endsWith("/");
  const name = uri.replace(/\/$/, "").split("/").pop() ?? uri;
  return {
    uri,
    name,
    isDir,
    size: isDir ? 0 : 10,
    modTime: "2026-11-18T10:00:00Z",
    kind: isDir ? "DIR" : "MD",
    relPath: uri.slice(ROOT.length + 1),
  };
}

function listing(root: string, uris: string[], truncated = false) {
  return { root, nodes: uris.map(entry), truncated, summary: "", overview: "" };
}

/** Answer the routes the tree uses from `store`, and record each request. */
function fakeServer(store: Store, truncatedFolders: string[] = []) {
  const asked: string[] = [];
  const json = (body: unknown) =>
    new Response(JSON.stringify(body), {
      headers: { "content-type": "application/json" },
    });

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input, "http://localhost");
      asked.push(url.pathname + url.search);
      if (url.pathname === "/api/tree") return json(listing(ROOT, store.get(ROOT) ?? []));
      if (url.pathname === "/api/list") {
        const uri = url.searchParams.get("uri") ?? "";
        return json(listing(uri, store.get(uri) ?? [], truncatedFolders.includes(uri)));
      }
      if (url.pathname === "/api/destinations") return json({ scopes: [], folders: [] });
      if (url.pathname === "/api/move") {
        const { from, into } = JSON.parse(String(init?.body)) as {
          from: string;
          into: string;
        };
        const landed = `${into}${from.split("/").pop()}`;
        for (const [folder, items] of store)
          store.set(
            folder,
            items.filter((u) => u !== from),
          );
        store.set(into, [...(store.get(into) ?? []), landed]);
        return json({ uri: landed });
      }
      return new Response(
        JSON.stringify({ error: { code: "NOT_FOUND", message: url.pathname } }),
        {
          status: 404,
        },
      );
    }),
  );
  return asked;
}

const settle = async () => {
  for (let i = 0; i < 10; i++) await new Promise((resolve) => setTimeout(resolve, 0));
};

function row(name: string): HTMLElement {
  const found = [...document.querySelectorAll<HTMLElement>("[role=treeitem]")].find(
    (element) => element.querySelector(".nm")?.textContent?.includes(name),
  );
  if (!found) throw new Error(`no row named ${name}`);
  return found;
}

const rowNames = () =>
  [...document.querySelectorAll<HTMLElement>("[role=treeitem] .nm")].map((element) =>
    (element.textContent ?? "").trim(),
  );

afterEach(() => {
  cleanup();
  api.refresh();
  vi.unstubAllGlobals();
});

describe("the Files tree", () => {
  it("loads only the top level until a folder is opened", async () => {
    const asked = fakeServer(
      new Map([
        [ROOT, [`${ROOT}/docs/`]],
        [`${ROOT}/docs/`, [`${ROOT}/docs/a.md`]],
      ]),
    );
    render(Files);
    await settle();

    expect(rowNames()).toEqual(["docs"]);
    expect(asked.some((path) => path.startsWith("/api/list"))).toBe(false);
  });

  it("shows a file dropped onto a closed folder inside that folder", async () => {
    fakeServer(
      new Map([
        [ROOT, [`${ROOT}/notes/`, `${ROOT}/a.md`]],
        [`${ROOT}/notes/`, []],
      ]),
    );
    render(Files);
    await settle();

    row("a.md").dispatchEvent(new Event("dragstart", { bubbles: true }));
    row("notes").dispatchEvent(
      new Event("dragover", { bubbles: true, cancelable: true }),
    );
    row("notes").dispatchEvent(new Event("drop", { bubbles: true, cancelable: true }));
    await settle();

    expect(row("notes").getAttribute("aria-expanded")).toBe("true");
    expect(rowNames()).toEqual(["notes", "a.md"]);
    expect(row("a.md").querySelectorAll(".ind")).toHaveLength(1);
  });

  it("says so when a folder's listing came back cut off", async () => {
    fakeServer(
      new Map([
        [ROOT, [`${ROOT}/big/`]],
        [`${ROOT}/big/`, [`${ROOT}/big/a.md`]],
      ]),
      [`${ROOT}/big/`],
    );
    render(Files);
    await settle();
    expect(document.body.textContent).not.toContain("hit OpenViking's limit");

    (row("big").querySelector("button.tw") as HTMLButtonElement).click();
    await settle();

    expect(document.body.textContent).toContain("hit OpenViking's limit");
  });

  it("opens every level with Expand all", async () => {
    fakeServer(
      new Map([
        [ROOT, [`${ROOT}/a/`]],
        [`${ROOT}/a/`, [`${ROOT}/a/b/`]],
        [`${ROOT}/a/b/`, [`${ROOT}/a/b/c/`]],
        [`${ROOT}/a/b/c/`, [`${ROOT}/a/b/c/deep.md`]],
      ]),
    );
    render(Files);
    await settle();

    const expand = [...document.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Expand all"),
    ) as HTMLButtonElement;
    expand.click();
    await settle();

    expect(rowNames()).toEqual(["a", "b", "c", "deep.md"]);
  });
});
