/**
 * Regressions for defects found by driving the real dashboard in a browser.
 *
 * Every one of these was reported from the UI, reproduced against the lab
 * cluster with Playwright, and traced to a specific line. The probe that found
 * them needs a running server and a live OpenViking; these do not.
 */

import { describe, expect, it } from "vitest";
import { buildTree, visibleRows } from "../src/client/lib/tree";
import { folderFor, isOrganisingFolder } from "../src/server/app";
import { loadConfig } from "../src/server/env";
import { MAX_GREP_TERMS, usefulAbstract } from "../src/server/ov";
import type { Node } from "../src/shared/schemas";

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

describe("the tree can show every level", () => {
  it("nests children under the folder that holds them, at any depth", () => {
    const roots = buildTree([
      node("memories/", true),
      node("memories/entities/", true),
      node("memories/entities/software/", true),
      node("memories/entities/software/openviking.md", false),
    ]);

    expect(roots).toHaveLength(1);
    const depths: number[] = [];
    const walk = (list: typeof roots): void => {
      for (const item of list) {
        depths.push(item.depth);
        walk(item.children);
      }
    };
    walk(roots);
    // Four levels, 0 through 3. OpenViking's recursive listing stops at 2, so
    // the deepest of these only exists because the client fetched it lazily and
    // merged it in — the tree must be able to hold it.
    expect(depths).toEqual([0, 1, 2, 3]);
  });

  it("shows a folder's children only once it is open", () => {
    const roots = buildTree([node("memories/", true), node("memories/a.md", false)]);
    const closed = visibleRows(roots, new Set());
    const open = visibleRows(roots, new Set(["viking://user/jasper/memories/"]));

    expect(closed.map((r) => r.name)).toEqual(["memories"]);
    expect(open.map((r) => r.name)).toEqual(["memories", "a.md"]);
  });

  it("keeps an entry whose parent never arrived, rather than dropping it", () => {
    // A lazily fetched folder can arrive before its parent is known. Dropping
    // it would silently hide files that exist.
    const roots = buildTree([node("orphan/deep/file.md", false)]);
    expect(roots).toHaveLength(1);
    expect(roots[0]?.name).toBe("file.md");
  });
});

describe("placeholder abstracts are not shown", () => {
  it("drops OpenViking's not-ready placeholders", () => {
    // Rendered as a callout above the document itself, this is pure noise.
    expect(usefulAbstract("[Directory abstract is not ready]")).toBe("");
    expect(usefulAbstract("[Directory overview is not ready]")).toBe("");
    expect(
      usefulAbstract(
        "# viking://user/jasper/memories/cases\n[Directory abstract is not ready]",
      ),
    ).toBe("");
  });

  it("keeps a real abstract", () => {
    const real = "This note records why the browser harness could not reach Chromium.";
    expect(usefulAbstract(real)).toBe(real);
  });

  it("treats whitespace as nothing", () => {
    expect(usefulAbstract("   \n  ")).toBe("");
  });

  it("drops a folder abstract that is a template that failed to render", () => {
    // The other way an abstract arrives saying nothing: Jinja's DebugUndefined
    // prints the miss instead of raising, so the failure is stored as the
    // abstract and the pane captioned a folder with it.
    expect(usefulAbstract("{{ no such element: dict object['task_signature'] }}")).toBe(
      "",
    );
    expect(usefulAbstract("{{ overview }}")).toBe("");
    expect(
      usefulAbstract(
        "# viking://user/jasper/memories/cases\n{{ no such element: dict object['case_name'] }}",
      ),
    ).toBe("");
  });

  it("keeps the half of an abstract that did render", () => {
    expect(
      usefulAbstract("Cases for {{ no such element: dict object['x'] }} training."),
    ).toBe("Cases for training.");
  });

  it("drops an abstract left as nothing but its title, however many words", () => {
    // The heading strip used to take one token, so "# cases" counted as
    // titled-and-empty and "# case index" did not — a rule made of word count.
    // Either way there is nothing under the title but the folder's own name,
    // which the page prints above it anyway.
    expect(usefulAbstract("# cases")).toBe("");
    expect(usefulAbstract("# case index")).toBe("");
    expect(usefulAbstract("# case index\n{{ no such element: dict object['x'] }}")).toBe(
      "",
    );
  });

  it("keeps a titled abstract that has something under the title", () => {
    const real = "# case index\n\nPost-mortems from the 2026-09-08 cron runs.";
    expect(usefulAbstract(real)).toBe(real);
  });
});

describe("the OpenViking timeout budget survives a slow grep", () => {
  it("defaults high enough that exact search does not time out", () => {
    // Measured: grep takes ~20s per term against the cluster while everything
    // else answers in under a second, so a 30s budget failed search alone.
    const config = loadConfig({
      OV_URL: "http://openviking:1933",
      SESSION_SECRET: "a".repeat(32),
      AUTH_MODE: "dev",
      KEY_SOURCE: "env",
      OV_API_KEY: "ov_test",
    });
    expect(config.OV_TIMEOUT_MS).toBeGreaterThanOrEqual(60_000);
  });
});

describe("every upload gets a folder of its own", () => {
  it("names the folder after the file, without its extension", () => {
    expect(folderFor("notes.md")).toBe("notes");
    expect(folderFor("my_report__final_.md")).toBe("my_report__final_");
    expect(folderFor("archive.tar.gz")).toBe("archive.tar");
  });

  it("copes with a name that is all extension", () => {
    // ".gitignore" has no stem, and a folder called "" would be the parent.
    expect(folderFor(".gitignore")).toBe("gitignore");
    expect(folderFor("...")).toBe("file");
  });
});

describe("only real folders are offered as destinations", () => {
  it("leaves out the directories OpenViking makes per resource", () => {
    // Each imported document becomes a directory named after it, so the raw
    // listing is mostly documents pretending to be folders.
    expect(isOrganisingFolder("introducing-agentic-video-in-gemini.md")).toBe(false);
    expect(isOrganisingFolder("report.pdf")).toBe(false);
    expect(isOrganisingFolder("assets")).toBe(false);
  });

  it("keeps the ones somebody would actually file into", () => {
    expect(isOrganisingFolder("blog-scraper")).toBe(true);
    expect(isOrganisingFolder("handoffs")).toBe(true);
    expect(isOrganisingFolder("model_gebruiksovereenkomst_zonder_HHR")).toBe(true);
  });
});

describe("the grep fan-out is capped once, not once per scope", () => {
  it("keeps the promised bound with a shared scope configured", () => {
    // The slice used to sit inside the per-scope map, so the default
    // OV_SHARED_ROOT doubled it: ~60s of upstream work for one GET.
    const terms = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"];
    const capped = terms.slice(0, MAX_GREP_TERMS);
    const scopes = ["viking://user/jasper", "viking://resources"];
    const jobs = scopes.flatMap((root) => capped.map((term) => ({ root, term })));

    expect(capped).toHaveLength(MAX_GREP_TERMS);
    expect(jobs).toHaveLength(MAX_GREP_TERMS * scopes.length);
    expect(new Set(jobs.map((j) => j.term)).size).toBe(MAX_GREP_TERMS);
  });
});
