/**
 * @vitest-environment jsdom
 *
 * Front matter in a markdown file.
 *
 * `marked` does not know YAML front matter, so the opening `---` became a rule
 * and the YAML ran together into one paragraph above the page.
 */

import { describe, expect, it } from "vitest";
import { renderMarkdown } from "../src/client/lib/markdown";

const PAGE = `---
title: "Bifrost"
tags: [gateway, llm]
---

# Bifrost

Routes requests.
`;

function parse(html: string): Document {
  return new DOMParser().parseFromString(html, "text/html");
}

describe("front matter", () => {
  it("keeps the YAML out of the page body", () => {
    const doc = parse(renderMarkdown(PAGE));
    expect(doc.querySelector("hr")).toBeNull();
    const body = [...doc.body.children].filter((el) => el.tagName !== "DETAILS");
    expect(body.map((el) => el.textContent).join("\n")).not.toContain("title:");
    expect(doc.querySelector("h1")?.textContent).toBe("Bifrost");
  });

  it("shows the YAML folded away, as escaped text", () => {
    const doc = parse(renderMarkdown(PAGE));
    const folded = doc.querySelector("details.frontmatter pre");
    expect(folded?.textContent).toBe('title: "Bifrost"\ntags: [gateway, llm]');
  });

  it("does not run markup hidden in the YAML", () => {
    const doc = parse(
      renderMarkdown('---\nx: "<img src=x onerror=alert(1)>"\n---\nbody'),
    );
    expect(doc.querySelector("img")).toBeNull();
  });

  it("leaves a rule that is not at the very top alone", () => {
    const doc = parse(renderMarkdown("intro\n\n---\n\nmore"));
    expect(doc.querySelector("hr")).not.toBeNull();
    expect(doc.querySelector("details")).toBeNull();
  });

  it("accepts Windows line endings", () => {
    const doc = parse(renderMarkdown("---\r\na: 1\r\n---\r\n# T\r\n"));
    expect(doc.querySelector("details.frontmatter pre")?.textContent).toBe("a: 1");
    expect(doc.querySelector("h1")?.textContent).toBe("T");
  });
});

describe("what is not front matter", () => {
  it("treats a rule at the top followed by text as a rule", () => {
    const doc = parse(renderMarkdown("---\n\n# Intro\n\nText\n\n---\n\nMore"));
    expect(doc.querySelector("details")).toBeNull();
    expect(doc.querySelector("h1")?.textContent).toBe("Intro");
  });

  it("folds an empty block away", () => {
    const doc = parse(renderMarkdown("---\n---\n# T"));
    expect(doc.querySelector("hr")).toBeNull();
    expect(doc.querySelector("h1")?.textContent).toBe("T");
  });

  it("accepts a byte order mark and trailing spaces on the fences", () => {
    const doc = parse(renderMarkdown("﻿--- \na: 1\n---  \n# T"));
    expect(doc.querySelector("details.frontmatter pre")?.textContent).toBe("a: 1");
    expect(doc.querySelector("h1")?.textContent).toBe("T");
  });
});
