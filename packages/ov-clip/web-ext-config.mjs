/**
 * What `web-ext` treats as the extension.
 *
 * The package root doubles as the loadable extension, so everything that is
 * source, tooling or test has to be named here or it ends up inside the
 * shipped archive.
 */
export default {
  ignoreFiles: [
    // Both spellings of each directory: the `/**` form drops the files, and
    // the bare name drops the now-empty directory entry that would otherwise
    // still be listed in the archive.
    "src",
    "src/**",
    "tests",
    "tests/**",
    "node_modules",
    "node_modules/**",
    "dist",
    "dist/**",
    "*.ts",
    "*.mjs",
    "*.json",
    "!manifest.json",
    "*.md",
    ".gitignore",
  ],
  build: {
    overwriteDest: true,
  },
};
