/**
 * What a save actually writes.
 *
 * These are the decisions a reader notices: whether their clip landed as one
 * file or a folder, what it got called, and whether the pictures came with it.
 */

import { describe, expect, it } from "vitest";
import {
  base64ToBytes,
  contentTypeFor,
  planArticleSave,
  planDocumentSave,
} from "../src/lib/save";
import type { ExtractResult } from "../src/types";

const SAVED_AT = "2026-09-07T12:00:00.000Z";

const ARTICLE: ExtractResult = {
  title: "The Vault Runbook",
  url: "https://x.example/runbook?utm_source=news#top",
  hostname: "x.example",
  markdown: "Body with ![a](https://x.example/image.jpg)",
  byline: "Ada",
  siteName: "Example",
  publishedTime: "2026-01-02",
};

function articleInput(over: Partial<Parameters<typeof planArticleSave>[0]> = {}) {
  return {
    article: ARTICLE,
    title: "The Vault Runbook",
    scopeUri: "viking://user",
    folder: "",
    tags: [] as string[],
    note: "",
    savedAt: SAVED_AT,
    ...over,
  };
}

describe("planning an article", () => {
  it("gives the article a directory of its own", () => {
    // Not decoration. OpenViking treats a destination as *the* resource, so
    // two clips sharing one do not merge — the second replaces the first and
    // the first is gone, abstract and overview with it. Measured; see the
    // integration suite.
    const plan = planArticleSave(articleInput({ folder: "clips" }));
    expect(plan.target).toBe("viking://user/clips");
    expect(plan.files).toHaveLength(1);
    expect(plan.files[0]?.filename).toBe("the-vault-runbook.md");
    expect(plan.files[0]?.to).toBe("viking://user/clips/the-vault-runbook");
  });

  it("aims two different articles at two different directories", () => {
    const one = planArticleSave(articleInput({ title: "First Piece" }));
    const two = planArticleSave(articleInput({ title: "Second Piece" }));
    expect(one.files[0]?.to).not.toBe(two.files[0]?.to);
  });

  it("aims a re-clip of the same page at the same directory", () => {
    // Deliberate: re-clipping updates the page rather than piling up copies.
    const first = planArticleSave(articleInput());
    const again = planArticleSave(articleInput({ note: "second thoughts" }));
    expect(again.files[0]?.to).toBe(first.files[0]?.to);
  });

  it("links the pictures where they live rather than storing them", () => {
    // Storing them beside the article was tried against a real OpenViking and
    // does not work: each imported document gets a directory of its own, so an
    // image imported to the same place is never a sibling and a relative link
    // resolves to nothing. See the integration suite.
    const plan = planArticleSave(articleInput());
    expect(plan.files).toHaveLength(1);
    expect(plan.files[0]?.text).toContain("https://x.example/image.jpg");
    expect(plan.files[0]?.text).not.toContain("(image-0.jpg)");
  });

  it("canonicalizes the source URL it records", () => {
    const plan = planArticleSave(articleInput());
    expect(plan.files[0]?.text).toContain('source_url: "https://x.example/runbook"');
  });

  it("lets the popup's edits win over what was extracted", () => {
    const plan = planArticleSave(
      articleInput({
        title: "A Better Title",
        byline: "Grace",
        publishedTime: "2026-02-03",
      }),
    );
    expect(plan.files[0]?.filename).toBe("a-better-title.md");
    expect(plan.files[0]?.text).toContain('author: "Grace"');
    expect(plan.files[0]?.text).toContain('publish_date: "2026-02-03"');
  });

  it("carries the tags and the note into the file", () => {
    const plan = planArticleSave(
      articleInput({ tags: ["vault", "ops"], note: "Read before rotating." }),
    );
    expect(plan.files[0]?.text).toContain('tags: ["vault", "ops"]');
    expect(plan.files[0]?.text).toContain("> Read before rotating.");
  });

  it("still produces a file for a page no article could be found in", () => {
    const plan = planArticleSave(
      articleInput({
        article: { title: "Bare", url: "https://x.example/", hostname: "x.example" },
        title: "Bare",
      }),
    );
    expect(plan.files).toHaveLength(1);
    expect(plan.files[0]?.text).toContain('title: "Bare"');
  });

  it("refuses a folder that climbs out of the scope", () => {
    expect(() => planArticleSave(articleInput({ folder: "../elsewhere" }))).toThrow(
      /\.\./,
    );
  });
});

describe("planning a document", () => {
  const bytes = new TextEncoder().encode("%PDF-1.7").buffer as ArrayBuffer;

  function documentInput(over: Partial<Parameters<typeof planDocumentSave>[0]> = {}) {
    return {
      article: {
        title: "Attention Is All You Need",
        url: "https://arxiv.org/pdf/1706.03762",
        hostname: "arxiv.org",
      },
      title: "Attention Is All You Need",
      scopeUri: "viking://resources",
      folder: "papers",
      tags: [] as string[],
      note: "",
      savedAt: SAVED_AT,
      bytes,
      contentType: "application/pdf",
      ...over,
    };
  }

  it("uploads the document as it arrived, into a directory of its own", () => {
    const plan = planDocumentSave(documentInput());
    expect(plan.target).toBe("viking://resources/papers");
    expect(plan.files).toHaveLength(1);
    expect(plan.files[0]?.filename).toBe("1706-03762.pdf");
    expect(plan.files[0]?.bytes).toBe(bytes);
    expect(plan.files[0]?.to).toBe("viking://resources/papers/attention-is-all-you-need");
  });

  it("adds a markdown companion only when there is a note or a tag to keep", () => {
    expect(planDocumentSave(documentInput({ note: "  " })).files).toHaveLength(1);

    const withNote = planDocumentSave(documentInput({ note: "Skim section 3." }));
    expect(withNote.files).toHaveLength(2);
    expect(withNote.files[1]?.filename).toBe("attention-is-all-you-need.md");
    expect(withNote.files[1]?.text).toContain("> Skim section 3.");
    // The companion links to the document it sits beside.
    expect(withNote.files[1]?.text).toContain("(1706-03762.pdf)");
    // Beside the document, never inside it: sharing a destination would have
    // the note replace the document it describes.
    expect(withNote.files[1]?.to).not.toBe(withNote.files[0]?.to);
    expect(withNote.files[1]?.to).toBe(
      "viking://resources/papers/attention-is-all-you-need-notes",
    );

    expect(planDocumentSave(documentInput({ tags: ["papers"] })).files).toHaveLength(2);
  });
});

describe("no two files may share a destination", () => {
  it("holds for every plan this module can produce", () => {
    // The invariant the whole layout rests on. A plan that broke it would
    // silently destroy one of its own files.
    const plans = [
      planArticleSave(articleInput()),
      planArticleSave(articleInput({ folder: "clips" })),
      planDocumentSave({
        article: { title: "Doc", url: "https://x.example/a.pdf", hostname: "x.example" },
        title: "Doc",
        scopeUri: "viking://user",
        folder: "papers",
        tags: ["a"],
        note: "keep this",
        savedAt: SAVED_AT,
        bytes: new TextEncoder().encode("%PDF").buffer as ArrayBuffer,
        contentType: "application/pdf",
      }),
    ];
    for (const plan of plans) {
      const destinations = plan.files.map((file) => file.to);
      expect(new Set(destinations).size).toBe(destinations.length);
      expect(destinations.every(Boolean)).toBe(true);
    }
  });
});

describe("file plumbing", () => {
  it("names a content type from an extension", () => {
    expect(contentTypeFor("a.png")).toBe("image/png");
    expect(contentTypeFor("a.JPEG")).toBe("image/jpeg");
    expect(contentTypeFor("a.md")).toBe("text/markdown");
    expect(contentTypeFor("a.unknown")).toBe("application/octet-stream");
    expect(contentTypeFor("noextension")).toBe("application/octet-stream");
  });

  it("decodes base64 back to the original bytes", () => {
    const original = new Uint8Array([0, 1, 254, 255, 128]);
    const base64 = btoa(String.fromCharCode(...original));
    expect(Array.from(base64ToBytes(base64))).toEqual(Array.from(original));
  });
});
