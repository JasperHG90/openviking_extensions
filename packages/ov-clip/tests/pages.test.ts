/**
 * The wiring between each page's script and its HTML.
 *
 * `el()` throws when an id is absent, and both scripts call it at module load,
 * so a renamed id does not degrade — the page is blank and the console holds
 * one line. Nothing else in the suite would notice, because neither page is
 * ever loaded. This checks the join by reading both sides.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { looksLikePdf } from "../src/lib/extract";

/** Read a file relative to the package root, which is vitest's own root. */
const read = (path: string) => readFileSync(resolve(process.cwd(), path), "utf8");

/** Every id the script asks `el()` for. */
function requestedIds(source: string): string[] {
  return [...source.matchAll(/\bel<[^>]*>\(\s*"([^"]+)"\s*\)/g)].map(
    (match) => match[1] ?? "",
  );
}

/** Every id the HTML defines. */
function definedIds(html: string): Set<string> {
  return new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1] ?? ""));
}

describe.each([
  { page: "popup", script: "src/popup/popup.ts", html: "src/popup/popup.html" },
  { page: "options", script: "src/options/options.ts", html: "src/options/options.html" },
])("$page", ({ page, script, html }) => {
  const source = read(script);
  const markup = read(html);

  it("asks for at least a handful of elements", () => {
    // Guards the test itself: a regex that stopped matching would otherwise
    // make this file pass by checking nothing.
    expect(requestedIds(source).length).toBeGreaterThan(3);
  });

  it("only asks for ids the page defines", () => {
    const defined = definedIds(markup);
    const missing = requestedIds(source).filter((id) => !defined.has(id));
    expect(missing, `${page}.ts asks for ids ${page}.html does not define`).toEqual([]);
  });

  it("loads the script and stylesheet it ships with", () => {
    expect(markup).toContain(`src="${page}.js"`);
    expect(markup).toContain(`href="${page}.css"`);
  });
});

describe("the manifest", () => {
  const manifest = JSON.parse(read("manifest.json")) as {
    manifest_version: number;
    version: string;
    permissions: string[];
    host_permissions: string[];
    browser_specific_settings: { gecko: { id: string; strict_min_version: string } };
    background: { scripts: string[] };
    action: { default_popup: string };
    options_ui: { page: string };
  };

  it("asks for the permissions the code actually uses", () => {
    // `nativeMessaging` to ask ovx for a token, `scripting` to read the page,
    // `storage` for preferences — no credential is kept, so nothing here holds
    // one. `<all_urls>` lets the background page fetch an article's images past
    // the site's CORS policy, and lets the popup reach whatever OpenViking the
    // profile names.
    expect(manifest.permissions).toEqual(
      expect.arrayContaining(["storage", "scripting", "nativeMessaging"]),
    );
    expect(manifest.host_permissions).toContain("<all_urls>");
  });

  it("no longer asks for identity, which only the old pairing flow needed", () => {
    expect(manifest.permissions).not.toContain("identity");
  });

  it("needs a Firefox that grants host permissions at install", () => {
    // An MV3 host permission is *optional*, and before Firefox 127 it was
    // never granted at install at all. The extension asks for none of it back
    // at runtime, so on 115–126 it would install clean and then fail every
    // save with a network error: OpenViking is reached from the popup and
    // images from the background page, both under `<all_urls>`.
    //
    // 127 is therefore the real floor. Lowering it means adding a
    // `permissions.request()` path, not just editing this number.
    expect(manifest.browser_specific_settings.gecko.strict_min_version).toBe("127.0");
  });

  it("keeps activeTab, which is not redundant next to <all_urls>", () => {
    // Still not redundant at 127+: a host permission granted at install can be
    // revoked afterwards, while `activeTab` is granted outright when the
    // toolbar button is clicked — the only way this popup opens. So it is what
    // keeps `tab.url`, `tab.title` and script injection working on the page
    // being clipped even for someone who has revoked the rest.
    expect(manifest.permissions).toContain("activeTab");
  });

  it("asks for nothing beyond those", () => {
    expect([...manifest.permissions].sort()).toEqual([
      "activeTab",
      "nativeMessaging",
      "scripting",
      "storage",
    ]);
  });

  it("declares the id ovx's host manifest allows", () => {
    // The most fragile join in the design, and it spans two packages: this has
    // to equal `OV_CLIP_ID` in ovx's native_install.py, which is what Firefox
    // checks before starting the host. If they drift, the host never starts
    // and the extension reports "ovx is not installed", which is not the
    // cause. ovx's own suite pins the other side to the same string.
    expect(manifest.browser_specific_settings.gecko.id).toBe("ov-clip@openviking");
  });

  it("carries the placeholder version, not a real one", () => {
    // The version lives in the git tag and nowhere else; the release stamps it
    // in. A real number committed here would be free to disagree with the tag,
    // and Firefox refuses to install two builds claiming the same version.
    expect(manifest.version).toBe("0.0.0");
  });

  it("points at files the build actually produces", () => {
    expect(manifest.background.scripts).toEqual(["background.js"]);
    expect(manifest.action.default_popup).toBe("popup/popup.html");
    expect(manifest.options_ui.page).toBe("options/options.html");
  });
});

describe("looksLikePdf", () => {
  const bytesOf = (text: string) => new TextEncoder().encode(text).buffer as ArrayBuffer;

  it("rejects the login page a site answers with instead of the file", () => {
    // Judged on content rather than on Content-Type, so a login page still
    // fails when the site sends no header at all — the case a header check
    // waves through.
    expect(looksLikePdf(bytesOf("<!doctype html><title>Sign in</title>"))).toBe(false);
    expect(looksLikePdf(bytesOf(""))).toBe(false);
    expect(looksLikePdf(bytesOf("not a pdf"))).toBe(false);
  });

  it("accepts a real PDF", () => {
    expect(looksLikePdf(bytesOf("%PDF-1.7\n%âãÏÓ\n1 0 obj"))).toBe(true);
    // Readers tolerate junk before the header, so this does too.
    expect(looksLikePdf(bytesOf(`${"\n".repeat(50)}%PDF-1.4`))).toBe(true);
  });

  it("does not go looking arbitrarily far in", () => {
    // An HTML page that merely mentions "%PDF-" a long way down is not one.
    expect(looksLikePdf(bytesOf(`${"x".repeat(4000)}%PDF-1.4`))).toBe(false);
  });
});
