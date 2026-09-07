import { describe, expect, it } from "vitest";
import {
  buildDocument,
  buildFrontmatter,
  yamlList,
  yamlString,
} from "../src/lib/frontmatter";

/** Read the frontmatter block back as key/value pairs. */
function parse(document: string): Record<string, string> {
  const match = /^---\n([\s\S]*?)\n---\n/.exec(document);
  if (!match?.[1]) return {};
  const fields: Record<string, string> = {};
  for (const line of match[1].split("\n")) {
    const at = line.indexOf(":");
    if (at > 0) fields[line.slice(0, at)] = line.slice(at + 1).trim();
  }
  return fields;
}

describe("yaml scalars", () => {
  it("quotes everything, so nothing is read as a keyword or a number", () => {
    expect(yamlString("plain")).toBe('"plain"');
    expect(yamlString("true")).toBe('"true"');
    expect(yamlString("12:30")).toBe('"12:30"');
    expect(yamlString("# not a comment")).toBe('"# not a comment"');
  });

  it("escapes what would end the scalar early", () => {
    expect(yamlString('He said "hi"')).toBe('"He said \\"hi\\""');
    expect(yamlString("back\\slash")).toBe('"back\\\\slash"');
  });

  it("flattens a newline instead of breaking the document", () => {
    expect(yamlString("two\nlines")).toBe('"two lines"');
    expect(yamlString("crlf\r\nlines")).toBe('"crlf lines"');
  });

  it("renders a list as a flow sequence", () => {
    expect(yamlList(["a", "b c"])).toBe('["a", "b c"]');
    expect(yamlList([])).toBe("[]");
  });
});

describe("buildFrontmatter", () => {
  it("records the title and the source", () => {
    const fields = parse(
      buildFrontmatter({
        title: "The Vault Runbook",
        url: "https://x.example/runbook",
        hostname: "x.example",
      }),
    );
    expect(fields.title).toBe('"The Vault Runbook"');
    expect(fields.source_url).toBe('"https://x.example/runbook"');
    expect(fields.hostname).toBe('"x.example"');
  });

  it("leaves out what is not known, rather than writing empty fields", () => {
    const block = buildFrontmatter({ title: "t", url: "u", hostname: "h" });
    expect(block).not.toContain("author");
    expect(block).not.toContain("tags");
    expect(block).not.toContain("publish_date");
  });

  it("ignores fields that are only whitespace", () => {
    const block = buildFrontmatter({
      title: "t",
      url: "u",
      hostname: "h",
      byline: "   ",
      tags: ["", "  "],
    });
    expect(block).not.toContain("author");
    expect(block).not.toContain("tags");
  });

  it("survives a title that would otherwise break the YAML", () => {
    const block = buildFrontmatter({
      title: 'Rotation: "why", and\nwhen',
      url: "u",
      hostname: "h",
    });
    // One key per line, and the whole title on the title line.
    expect(block.split("\n").filter((line) => line.startsWith("title:"))).toHaveLength(1);
    expect(parse(block).title).toBe('"Rotation: \\"why\\", and when"');
  });

  it("names a title-less page rather than leaving it blank", () => {
    expect(parse(buildFrontmatter({ title: "", url: "u", hostname: "h" })).title).toBe(
      '"Untitled"',
    );
  });
});

describe("buildDocument", () => {
  const article = { title: "T", url: "https://x.example/a", hostname: "x.example" };

  it("puts the reader's note above the article", () => {
    const document = buildDocument(article, "The body.", "Worth keeping.");
    const note = document.indexOf("> Worth keeping.");
    const body = document.indexOf("The body.");
    expect(note).toBeGreaterThan(-1);
    expect(note).toBeLessThan(body);
  });

  it("quotes every line of a multi-line note", () => {
    const document = buildDocument(article, "Body", "first\nsecond");
    expect(document).toContain("> first\n> second");
  });

  it("writes no note block when there is nothing to say", () => {
    expect(buildDocument(article, "Body", "   ")).not.toContain(">");
  });

  it("ends with exactly one newline", () => {
    const document = buildDocument(article, "Body\n\n\n", "");
    expect(document.endsWith("Body\n")).toBe(true);
  });
});
