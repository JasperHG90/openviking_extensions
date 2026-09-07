import { describe, expect, it } from "vitest";
import { canonicalizeUrl, hostnameOf } from "../src/lib/url";

describe("canonicalizeUrl", () => {
  it("drops the fragment", () => {
    expect(canonicalizeUrl("https://x.example/a#section")).toBe("https://x.example/a");
  });

  it("drops tracking parameters but keeps real ones", () => {
    expect(canonicalizeUrl("https://x.example/a?id=7&utm_source=news&fbclid=abc")).toBe(
      "https://x.example/a?id=7",
    );
  });

  it("sorts what is left, so parameter order stops mattering", () => {
    expect(canonicalizeUrl("https://x.example/a?b=2&a=1")).toBe(
      canonicalizeUrl("https://x.example/a?a=1&b=2"),
    );
  });

  it("drops a trailing slash, except on the root", () => {
    expect(canonicalizeUrl("https://x.example/a/")).toBe("https://x.example/a");
    expect(canonicalizeUrl("https://x.example/")).toBe("https://x.example/");
  });

  it("hands back anything it cannot parse, unchanged", () => {
    expect(canonicalizeUrl("not a url")).toBe("not a url");
    expect(canonicalizeUrl("")).toBe("");
  });

  it("tidies a non-http URL rather than choking on it", () => {
    // `about:` and `file:` parse, so they go through the same cleaning.
    expect(canonicalizeUrl("about:blank#x")).toBe("about:blank");
  });
});

describe("hostnameOf", () => {
  it("returns the host, or nothing at all", () => {
    expect(hostnameOf("https://x.example/a")).toBe("x.example");
    expect(hostnameOf("garbage")).toBe("");
  });
});
