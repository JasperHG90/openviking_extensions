import { describe, expect, it } from "vitest";
import { parentOf } from "../src/server/ov";
import { describeFile } from "../src/server/overview";

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
