/**
 * The rule that turns what somebody typed into one path segment.
 *
 * This is the control the New folder route's security rests on: the name is
 * appended to a uri *after* `requireInScope` has judged the parent, so nothing
 * downstream re-checks it. If a separator, a climb or a percent escape could
 * survive `safeSegment`, the scope check would be judging a prefix of a path
 * somebody else finished. Pinned directly here rather than only through the
 * route, because the route tests would not tell you which of the two broke.
 *
 * It is also the rule the New folder box previews with, so both ends reading
 * the same table is the whole reason it lives in shared.
 */

import { describe, expect, it } from "vitest";
import {
  MAX_SEGMENT,
  folderNameProblem,
  namesNothing,
  safeSegment,
} from "../src/shared/names";

describe("reducing a name to one segment", () => {
  it("keeps a name that is already one", () => {
    expect(safeSegment("notes")).toBe("notes");
    expect(safeSegment("Q3-report_v2.md")).toBe("Q3-report_v2.md");
  });

  it("keeps only the last segment, whichever separator was used", () => {
    // Both, because a backslash is a separator to some readers and not others,
    // and "whose separator wins" is not a question worth leaving open.
    expect(safeSegment("../../etc/passwd")).toBe("passwd");
    expect(safeSegment("..\\..\\windows\\system32")).toBe("system32");
    expect(safeSegment("/")).toBe("");
    expect(safeSegment("notes/")).toBe("");
  });

  it("replaces everything outside the allowed characters", () => {
    expect(safeSegment("Q3 notes")).toBe("Q3_notes");
    expect(safeSegment("what's new?")).toBe("what_s_new_");
    expect(safeSegment("a\nb")).toBe("a_b");
    expect(safeSegment("café")).toBe("caf_");
  });

  it("leaves no percent escape for anything downstream to decode", () => {
    // `requireInScope` refuses a uri carrying any `%`, and the folder name is
    // appended after it runs. An allowlist is what makes that safe: `%2e%2e%2f`
    // comes out as text, not as `../`.
    expect(safeSegment("%2e%2e%2f")).toBe("_2e_2e_2f");
    expect(safeSegment("a%20b")).toBe("a_20b");
  });

  it("is not fooled by a separator that only looks like one", () => {
    // A fullwidth solidus is not a separator to `split`, so it has to fall to
    // the character rule instead — which it does, being outside the allowlist.
    expect(safeSegment("a／..／b")).toBe("a_.._b");
  });
});

describe("a segment that names nothing", () => {
  it("is the empty one and the all-dots ones", () => {
    for (const value of ["", ".", "..", "..."]) {
      expect(namesNothing(value), value).toBe(true);
    }
  });

  it("is nothing else, including a name that merely starts with a dot", () => {
    for (const value of [".gitignore", "a", "..a", "a.."]) {
      expect(namesNothing(value), value).toBe(false);
    }
  });
});

describe("why a name cannot be a folder", () => {
  it("passes an ordinary name", () => {
    expect(folderNameProblem("notes")).toBe("");
    expect(folderNameProblem("Q3_notes")).toBe("");
  });

  it("refuses a name that survived to nothing", () => {
    for (const value of ["", ".", "..", "..."]) {
      expect(folderNameProblem(value), value).toBe("a folder needs a name");
    }
  });

  it("refuses a leading dot", () => {
    // OpenViking writes `.abstract.md` inside the folder this would sit beside,
    // and lists without `-a`, so such a folder would be invisible in the tree
    // and in the way of OpenViking's own files.
    for (const value of [".abstract.md", ".overview.md", ".hidden"]) {
      expect(folderNameProblem(value), value).toContain("starting with a dot");
    }
  });

  it("refuses a name longer than one path component may be", () => {
    expect(folderNameProblem("a".repeat(MAX_SEGMENT))).toBe("");
    expect(folderNameProblem("a".repeat(MAX_SEGMENT + 1))).toContain(
      `${MAX_SEGMENT} characters`,
    );
  });
});
