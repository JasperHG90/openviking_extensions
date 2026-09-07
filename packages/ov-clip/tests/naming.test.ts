import { describe, expect, it } from "vitest";
import {
  documentFilename,
  joinTarget,
  markdownFilename,
  slugify,
} from "../src/lib/naming";

describe("slugify", () => {
  it("makes a title into one lowercase path segment", () => {
    expect(slugify("The Vault Rotation Runbook")).toBe("the-vault-rotation-runbook");
  });

  it("folds accents rather than dropping the letters under them", () => {
    expect(slugify("Café Society")).toBe("cafe-society");
    expect(slugify("Ærø, Zürich & Straße")).toBe("aero-zurich-strasse");
  });

  it("drops anything that cannot be a path segment", () => {
    expect(slugify("a/b?c#d")).toBe("a-b-c-d");
    expect(slugify("  spaced  out  ")).toBe("spaced-out");
    expect(slugify("...dots...")).toBe("dots");
  });

  it("falls back rather than returning an empty name", () => {
    expect(slugify("")).toBe("untitled");
    expect(slugify("???")).toBe("untitled");
    expect(slugify("日本語")).toBe("untitled");
  });

  it("keeps the result short, and never ends on a hyphen", () => {
    const slug = slugify("word ".repeat(40));
    expect(slug.length).toBeLessThanOrEqual(60);
    expect(slug.endsWith("-")).toBe(false);
  });
});

describe("naming a file", () => {
  it("gives an article a .md name", () => {
    expect(markdownFilename("Hello World")).toBe("hello-world.md");
  });

  it("keeps a document's own extension", () => {
    expect(documentFilename("https://x.example/papers/Attention.PDF", "t")).toBe(
      "attention.pdf",
    );
    expect(documentFilename("https://x.example/a/report.docx", "t")).toBe("report.docx");
  });

  it("falls back to the title and pdf when the URL says nothing", () => {
    expect(documentFilename("https://arxiv.org/pdf/2401.01234", "Deep Nets")).toBe(
      "2401-01234.pdf",
    );
    expect(documentFilename("https://x.example/", "Quarterly Report")).toBe(
      "quarterly-report.pdf",
    );
  });

  it("survives a URL that will not parse", () => {
    expect(documentFilename("not a url", "A Title")).toBe("a-title.pdf");
  });
});

describe("joinTarget", () => {
  it("returns the scope itself when no folder is given", () => {
    expect(joinTarget("viking://user", "")).toBe("viking://user");
    expect(joinTarget("viking://user/", "  ")).toBe("viking://user");
  });

  it("appends a folder, tidying the slashes", () => {
    expect(joinTarget("viking://user", "clips")).toBe("viking://user/clips");
    expect(joinTarget("viking://user/", "/clips/2026/")).toBe("viking://user/clips/2026");
  });

  it("cleans a segment that could not be a path", () => {
    expect(joinTarget("viking://user", "my clips")).toBe("viking://user/my-clips");
    expect(joinTarget("viking://user", "a?b")).toBe("viking://user/a-b");
  });

  it("refuses to climb out of the scope", () => {
    expect(() => joinTarget("viking://user", "../resources")).toThrow(/\.\./);
    expect(() => joinTarget("viking://user", "clips/../../etc")).toThrow(/\.\./);
  });
});
