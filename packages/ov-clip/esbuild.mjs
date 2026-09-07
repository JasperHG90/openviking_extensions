/**
 * Build the loadable extension.
 *
 * Firefox loads a directory that has `manifest.json` at its root, so the
 * compiled JS lands beside the HTML that references it — `popup/popup.js` next
 * to `popup/popup.html` — rather than in a build directory the manifest would
 * have to point into. The generated paths are in `.gitignore`.
 */

import { cp, mkdir } from "node:fs/promises";
import * as esbuild from "esbuild";

const PAGES = ["popup", "options"];

for (const page of PAGES) {
  await mkdir(page, { recursive: true });
  for (const asset of [`${page}.html`, `${page}.css`]) {
    await cp(`src/${page}/${asset}`, `${page}/${asset}`);
  }
}

/** @type {import('esbuild').BuildOptions} */
const shared = {
  bundle: true,
  format: "iife",
  // The floor the manifest declares. Anything newer than this in the output
  // would install and then fail at runtime on a supported Firefox.
  target: "firefox115",
  minify: false,
  logLevel: "info",
};

await Promise.all([
  esbuild.build({
    ...shared,
    entryPoints: ["src/background.ts"],
    outfile: "background.js",
  }),
  ...PAGES.map((page) =>
    esbuild.build({
      ...shared,
      entryPoints: [`src/${page}/${page}.ts`],
      outfile: `${page}/${page}.js`,
    }),
  ),
]);
