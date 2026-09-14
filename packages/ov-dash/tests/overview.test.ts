import { describe, expect, it } from "vitest";
import { parentOf } from "../src/server/ov";
import { describeFile, folderOverview, stripUnrendered } from "../src/server/overview";

/**
 * A folder overview, in the shape OpenViking writes them.
 *
 * Copied from the live server rather than invented: this is
 * `.../Draft_Agent_templates_and_scaffold/.overview.md`, cut down to the parts
 * the parser reads. Its truncation is the real thing too — OpenViking caps a
 * generated overview at 4000 characters and trims at a sentence boundary, so
 * the Detailed Description here stops after the first file and the other three
 * entries, the image among them, never got one.
 */
const OVERVIEW = `# Draft_Agent_templates_and_scaffold

This directory holds draft design documents for restructuring agent template
and scaffolding tooling into a single maintainable monorepo.

## Directory Coverage

Total direct entries: 4. All four are represented in this overview.

## Quick Navigation

What do you want to learn?

- **Why restructure, and what is the overall vision?** → [Draft_Agent_templates_and_scaffold_1.md](viking://user/jasper/x/Draft_Agent_templates_and_scaffold_1.md) — motivation, six-artifact split
- **How do the pieces connect visually?** → [page3_img1.png](viking://user/jasper/x/page3_img1.png) — developer, agent, and five monorepo components

## Detailed Description

### [Draft_Agent_templates_and_scaffold_1.md](viking://user/jasper/x/Draft_Agent_templates_and_scaffold_1.md)

A draft design document proposing that the agent template repository be
restructured into a single maintainable monorepo.

Three main sections: Motivation; Vision and repository split; and The
boundary: prompts may, code must.
`;

describe("reading a file's description out of its folder's overview", () => {
  it("returns the detail section written for that file", () => {
    const described = describeFile(OVERVIEW, "Draft_Agent_templates_and_scaffold_1.md");
    expect(described).toMatch(/^A draft design document proposing/);
    expect(described).toMatch(/prompts may, code must\.$/);
  });

  it("falls back to the navigation line when the detail was truncated away", () => {
    // The exact case that made the dashboard look like it had no description
    // for an image OpenViking had in fact looked at and described.
    expect(describeFile(OVERVIEW, "page3_img1.png")).toBe(
      "developer, agent, and five monorepo components",
    );
  });

  it("gives nothing for a file the overview never mentions", () => {
    expect(describeFile(OVERVIEW, "not_in_here.png")).toBe("");
  });

  it("gives nothing when there is no overview to read", () => {
    expect(describeFile("", "page3_img1.png")).toBe("");
    expect(describeFile("   \n  ", "page3_img1.png")).toBe("");
  });

  it("stops a section at the next heading instead of swallowing it", () => {
    const doc = `## Detailed Description

### [first.md](viking://x/first.md)

The first file.

### [second.md](viking://x/second.md)

The second file.
`;
    expect(describeFile(doc, "first.md")).toBe("The first file.");
    expect(describeFile(doc, "second.md")).toBe("The second file.");
  });

  it("matches a name that the link target had to percent-encode", () => {
    // OpenViking writes `image_%281%29.png` for a file called `image_(1).png`,
    // so a parser comparing the raw strings finds nothing. Here the heading's
    // text does not name the file either, leaving the target as the only way
    // through.
    const doc = `## Detailed Description

### [the ritual board](viking://user/jasper/x/image_%281%29.png)

A screenshot of the ritual board.
`;
    expect(describeFile(doc, "image_(1).png")).toBe("A screenshot of the ritual board.");
  });

  it("matches an encoded name in a navigation line too", () => {
    const doc = `## Quick Navigation

- **See it** → [the board](viking://user/jasper/x/image_%281%29.png) — a whiteboard photo
`;
    expect(describeFile(doc, "image_(1).png")).toBe("a whiteboard photo");
  });

  it("keeps the heading that names the file plainly, target encoded", () => {
    // The shape OpenViking actually writes: plain name as the link text.
    const doc = `### [image_(1).png](viking://user/jasper/x/image_%281%29.png)

A screenshot of the ritual board.
`;
    expect(describeFile(doc, "image_(1).png")).toBe("A screenshot of the ritual board.");
  });

  it("takes the first section when a file is named by two headings", () => {
    const doc = `## Detailed Description

### [a.md](viking://x/a.md)

The first word on it.

## Appendix

### [a.md](viking://x/a.md)

A later aside.
`;
    expect(describeFile(doc, "a.md")).toBe("The first word on it.");
  });

  it("matches a navigation line whose link text is not the file name", () => {
    const doc = `## Quick Navigation

- **See the diagram** → [the architecture diagram](viking://x/page3_img1.png) — five boxes and two actors
`;
    expect(describeFile(doc, "page3_img1.png")).toBe("five boxes and two actors");
  });

  it("skips a navigation line that names the file but describes nothing", () => {
    const doc = `## Quick Navigation

- **Open it** → [notes.md](viking://x/notes.md)
- **Read the notes** → [notes.md](viking://x/notes.md) — what was decided on Tuesday
`;
    expect(describeFile(doc, "notes.md")).toBe("what was decided on Tuesday");
  });

  it("does not mistake a dash inside the question for the one before the description", () => {
    const doc = `## Quick Navigation

- **What is the build — and why?** → [build.md](viking://x/build.md) — the release story
`;
    expect(describeFile(doc, "build.md")).toBe("the release story");
  });

  it("does not read the folder's own title as a section about a file", () => {
    // OpenViking stores an imported document as a folder named after it, so a
    // folder holding a file of its own name is the normal case, not a corner.
    // Matching the opening `# <folder>` handed that file the folder's first
    // paragraph — a folder blurb, captioned as the file's own description,
    // which is the whole defect this module exists to fix.
    const doc = `# introducing-agentic-video.md

This directory holds one ingested web page, plus its extracted images.

## Detailed Description

### [introducing-agentic-video.md](viking://x/introducing-agentic-video.md)

A blog post announcing agentic video in Gemini.
`;
    expect(describeFile(doc, "introducing-agentic-video.md")).toBe(
      "A blog post announcing agentic video in Gemini.",
    );
  });

  it("ignores a level-two heading that happens to name the file", () => {
    const doc = `## notes.md

Not a per-file section.

### [notes.md](viking://x/notes.md)

What was decided on Tuesday.
`;
    expect(describeFile(doc, "notes.md")).toBe("What was decided on Tuesday.");
  });

  it("does not give a file the bullet written for a longer-named sibling", () => {
    // `plan.md` is a suffix of `old-plan.md`, so a substring test handed it a
    // real description of the wrong file.
    const doc = `## Quick Navigation

- **The old one** → [old-plan.md](viking://x/old-plan.md) — what we used to do
- **The current one** → [plan.md](viking://x/plan.md) — what we do now
`;
    expect(describeFile(doc, "plan.md")).toBe("what we do now");
    expect(describeFile(doc, "old-plan.md")).toBe("what we used to do");
  });

  it("says nothing when only a longer-named sibling is described", () => {
    const doc = `## Quick Navigation

- **The old one** → [old-plan.md](viking://x/old-plan.md) — what we used to do
`;
    expect(describeFile(doc, "plan.md")).toBe("");
  });

  it("still finds a bare name standing on its own in a bullet", () => {
    const doc = `## Quick Navigation

- Open plan.md — what we do now
`;
    expect(describeFile(doc, "plan.md")).toBe("what we do now");
  });

  it("looks past a first hit that turned out to be inside a longer name", () => {
    const doc = `## Quick Navigation

- Not old-plan.md but plan.md — what we do now
`;
    expect(describeFile(doc, "plan.md")).toBe("what we do now");
  });

  it("leaves a bullet alone once it links some other file", () => {
    // A bullet that links another file is that file's. Our name appearing in
    // it — inside a percent-encoded target, inside a containing folder's path,
    // or just mentioned in the prose — is not a description of ours.
    const encoded =
      "- **See** → [old plan.md](viking://x/old%20plan.md) — what we used to do";
    const folder =
      "- **The picture** → [thumb.png](viking://x/plan.md/thumb.png) — a thumbnail";
    const prose =
      "- **The chart** → [chart.png](viking://x/chart.png) (supersedes plan.md) — the new numbers";

    expect(describeFile(encoded, "plan.md")).toBe("");
    expect(describeFile(folder, "plan.md")).toBe("");
    expect(describeFile(prose, "plan.md")).toBe("");
    // The files those bullets are actually about still get their descriptions.
    expect(describeFile(folder, "thumb.png")).toBe("a thumbnail");
    expect(describeFile(prose, "chart.png")).toBe("the new numbers");
  });

  it("ignores a section-shaped heading outside Detailed Description", () => {
    const doc = `## Quick Navigation

### a.md

Prose that happens to be shaped like a section.

## Detailed Description

### [a.md](viking://x/a.md)

The real description.
`;
    expect(describeFile(doc, "a.md")).toBe("The real description.");
  });

  it("still reads a section when the overview files it under no heading", () => {
    // Losing a description that is plainly there would be a poor trade for
    // strictness about a heading OpenViking might one day rename.
    const doc = `# notes

### [a.md](viking://x/a.md)

The only description here.
`;
    expect(describeFile(doc, "a.md")).toBe("The only description here.");
  });

  it("skips a line too long to be a navigation bullet", () => {
    // Unclosed links cost time quadratic in the line's length, and the event
    // loop is the whole process. Overview prose is written by a model from
    // documents people upload, so its shape is not ours to assume.
    const flood = `- ${"[a](".repeat(30_000)} plan.md — never reached`;
    const started = Date.now();
    expect(describeFile(flood, "plan.md")).toBe("");
    expect(Date.now() - started).toBeLessThan(1_000);
  });

  it("prefers the detail section over the navigation line", () => {
    const doc = `## Quick Navigation

- **Look** → [a.md](viking://x/a.md) — the short version

## Detailed Description

### [a.md](viking://x/a.md)

The long version.
`;
    expect(describeFile(doc, "a.md")).toBe("The long version.");
  });
});

describe("finding the folder a file sits in", () => {
  it("walks up one level", () => {
    expect(parentOf("viking://user/jasper/notes/plan.md")).toBe(
      "viking://user/jasper/notes",
    );
    expect(parentOf("viking://user/jasper/notes/")).toBe("viking://user/jasper");
  });

  it("stops at a uri with nothing above it, rather than eating the scheme", () => {
    // Stripping trailing slashes off a bare `viking://` leaves `viking:`, and
    // handing that back would be further from a usable uri than what came in.
    expect(parentOf("viking://")).toBe("viking://");
    expect(parentOf("viking://user")).toBe("viking://user");
    expect(parentOf("")).toBe("");
  });
});

/**
 * Descriptions that are really a template that failed to render.
 *
 * OpenViking renders memory templates with Jinja's `DebugUndefined`, which
 * prints the miss into the file instead of raising. Copied from the live
 * server: this is `viking://user/jasper/memories/cases/.overview.md`, where
 * every one of eight bullets came out this way because the files under
 * `cases/` carry none of the fields that memory type declares.
 */
const BROKEN_OVERVIEW = `---
directory: viking://user/jasper/memories/cases/
---

# Cases Overview

- [{{ no such element: dict object['case_name'] }}](./mem_079fbe147f15.md) — {{ no such element: dict object['task_signature'] }}

- [{{ no such element: dict object['case_name'] }}](./mem_f2fe076b2f78.md) — {{ no such element: dict object['task_signature'] }}
`;

describe("a description that is a failed render", () => {
  it("comes back as no description at all", () => {
    // It used to reach the reading pane verbatim, captioned "What OpenViking
    // makes of this" — an error message dressed as an opinion about the file.
    expect(describeFile(BROKEN_OVERVIEW, "mem_079fbe147f15.md")).toBe("");
    expect(describeFile(BROKEN_OVERVIEW, "mem_f2fe076b2f78.md")).toBe("");
  });

  it("keeps the words that did arrive around one that did not", () => {
    const doc = [
      "# Cases Overview",
      "",
      "- [a](./a.md) — Fixes the {{ no such element: dict object['x'] }} import path.",
    ].join("\n");
    expect(describeFile(doc, "a.md")).toBe("Fixes the import path.");
  });

  it("drops a bare unresolved variable too, and closes up after it", () => {
    // The other shape DebugUndefined emits: a top-level name it never got,
    // printed as `{{ language }}` rather than as a sentence about elements.
    // Nothing follows it but a full stop, so the space before it goes as well
    // — "Written in ." reads as a typo, which is a different kind of broken.
    const doc = "### notes.md\n\nWritten in {{ language }}.";
    expect(describeFile(doc, "notes.md")).toBe("Written in.");
  });

  it("leaves indentation alone on lines nowhere near a placeholder", () => {
    // The seam is closed where the placeholder was, not everywhere. Collapsing
    // runs of spaces across the whole text flattened indented code — and it
    // fired on any text merely containing braces, placeholder or not.
    const doc = [
      "### render.md",
      "",
      "Renders {{ x }} like so:",
      "",
      "    def render(ctx):",
      "        return tpl.render(ctx)",
    ].join("\n");
    expect(describeFile(doc, "render.md")).toBe(
      "Renders like so:\n\n    def render(ctx):\n        return tpl.render(ctx)",
    );
  });

  it("stays linear against a run of whitespace inside a placeholder", () => {
    // The first draft was quadratic here: `[^{}]*` and the `\s*` after it both
    // matched spaces, so the engine tried every split. 32k spaces took 561ms
    // on the single thread that serves every request. Bound is generous on
    // purpose — the point is the shape of the curve, not the clock.
    const hostile = `{{ no such element:${" ".repeat(64_000)}`;
    const started = Date.now();
    expect(stripUnrendered(hostile)).toBe(hostile.trim());
    expect(Date.now() - started).toBeLessThan(500);
  });

  it("is not applied to the overview document itself", () => {
    // A deliberate hole: `.overview.md` opened for reading should still show
    // its own failure. Pinned because a comment alone does not stop anyone.
    const raw =
      "# Cases Overview\n\n- [a](./a.md) — {{ no such element: dict object['x'] }}";
    expect(stripUnrendered(raw)).not.toBe(raw);
    expect(raw.includes("no such element")).toBe(true);
  });

  it("leaves prose that merely contains braces alone", () => {
    // A memory about templating is a real description, not a failure. The
    // shapes above have no spaces inside; an expression does.
    const doc = "### calc.md\n\nThe template renders {{ 2 + 2 }} as four.";
    expect(describeFile(doc, "calc.md")).toBe(
      "The template renders {{ 2 + 2 }} as four.",
    );
  });

  it("hands back text with no braces as the very same string", () => {
    // Identity, not equality-after-cleanup: the old version of this asserted a
    // trimmed result, which `detailSection` had already trimmed before the
    // strip ever ran, so it passed with the strip deleted.
    const real = "A plan, with an em dash — and    aligned    columns.";
    expect(stripUnrendered(real)).toBe(real);
  });
});

/**
 * Showing a folder's overview, which is the fix for the image complaint.
 *
 * The overview text below is the real thing, captured from the lab cluster on
 * 13 Sep 2026 by uploading a 4600x900 PNG — over the 4096px threshold in
 * OpenViking's `ImageConfig`, so it took the large-image path. That is worth
 * knowing about, because it is what made the dashboard look broken: OpenViking
 * stores a large image as a preview, a grid and a set of tiles and does *not*
 * keep the original, and the thorough description of what it saw goes into the
 * folder's overview. The dashboard read that overview only to mine one file's
 * section out of it and never showed it, so the folder page carried a one-line
 * abstract and looked like a page where nothing had been described.
 */
const IMAGE_OVERVIEW = `# ovdash_gridprobe_portal

The ovdash_gridprobe_portal directory appears to contain graphic design assets,
test images, and geometric visual compositions.

## Directory Coverage

Total direct entries: 3. All direct entries are represented below.

## Quick Navigation

What do you want to learn?

- View the detailed grid probe test image → [ovdash_gridprobe_portal_grid.jpg](viking://user/jasper/resources/ovdash_gridprobe_portal/ovdash_gridprobe_portal_grid.jpg)

## Detailed Description

### [ovdash_gridprobe_portal_grid.jpg](viking://user/jasper/resources/ovdash_gridprobe_portal/ovdash_gridprobe_portal_grid.jpg)
This image features three distinct geometric shapes overlaid on a vertically
striped background, divided into three vertical panels by red grid lines.
`;

describe("a folder's overview, ready to show", () => {
  it("keeps the description of every entry, which is the whole point", () => {
    const shown = folderOverview(IMAGE_OVERVIEW);
    expect(shown).toContain("three distinct geometric shapes");
    expect(shown).toContain("## Detailed Description");
    // The navigation links are viking:// uris the pane opens in place, so a
    // person lands on the grid image from here rather than hunting the tree.
    expect(shown).toContain(
      "viking://user/jasper/resources/ovdash_gridprobe_portal/ovdash_gridprobe_portal_grid.jpg",
    );
  });

  it("drops the title, because the pane prints it directly above", () => {
    const shown = folderOverview(IMAGE_OVERVIEW);
    expect(shown.startsWith("#")).toBe(false);
    expect(shown).not.toContain("# ovdash_gridprobe_portal\n");
    // The sentence under the title survives: it *is* the abstract, and this is
    // shown instead of the abstract rather than beside it.
    expect(shown).toMatch(/^The ovdash_gridprobe_portal directory/);
  });

  it("keeps Directory Coverage, which says how much the model sampled", () => {
    // Not tidied away with the title. It is not a truncation marker — the cut
    // lands in the `###` sections below and this line survives it — but it does
    // say whether the model looked at every entry, which is worth reading.
    expect(folderOverview(IMAGE_OVERVIEW)).toContain("Total direct entries: 3");
  });

  it("gives nothing for a folder OpenViking has not got to", () => {
    // The pane says so in its own words instead of printing the placeholder.
    expect(folderOverview("# notes\n\n[Directory overview is not ready]")).toBe("");
    expect(folderOverview("")).toBe("");
    expect(folderOverview("   \n\n  ")).toBe("");
    // A title and nothing under it says only what the folder is called, which
    // the pane prints anyway.
    expect(folderOverview("# notes\n")).toBe("");
  });

  it("strips a failed template render, like any other generated text", () => {
    const raw = "# notes\n\nA folder of {{ no such element: dict object['x'] }} plans.";
    expect(folderOverview(raw)).toBe("A folder of plans.");
  });

  it("only ever takes an H1, never a section heading", () => {
    /*
     * The title is model output, not a wrapper — `overview_generation.yaml` asks
     * for it — so a run that skips the H1 opens with its first section instead.
     * An earlier version stripped `#{1,2}` and ate that heading, leaving the
     * bullets under it orphaned with nothing to say what they were. No path
     * produces a `##` title, so the wider match bought nothing.
     */
    const noTitle = "## Quick Navigation\n\n- [a](./a.md) — the first plan";
    expect(folderOverview(noTitle)).toBe(noTitle);

    const coverageFirst = "## Directory Coverage\n\nTotal direct entries: 3.";
    expect(folderOverview(coverageFirst)).toBe(coverageFirst);

    // Deeper headings are just as safe, and the H1 still goes when there is one.
    expect(folderOverview("### [a.md](viking://x/a.md)\n\nThe plan.")).toBe(
      "### [a.md](viking://x/a.md)\n\nThe plan.",
    );
    expect(folderOverview("# notes\n\n## Quick Navigation\n\n- a")).toBe(
      "## Quick Navigation\n\n- a",
    );
  });

  it("leaves a body that merely starts with a hash-like line", () => {
    // A `#` that is not a heading is prose, and the title strip must not reach
    // it. Only the first line, and only when that line is an H1.
    expect(folderOverview("Notes on #hashtags and #tags.")).toBe(
      "Notes on #hashtags and #tags.",
    );
  });
});
