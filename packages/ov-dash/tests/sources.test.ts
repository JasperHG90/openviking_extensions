/**
 * Every file in the package is text git can show.
 *
 * This exists because of a defect every other gate waved through. A single NUL
 * byte, typed into a block comment in `src/server/app.ts` by a script that meant
 * to write the six characters of an escape sequence, made git classify the file holding every route
 * and every guard as **binary**: `git diff`, `git show` and `git blame` all stopped
 * being able to display it, and a patch-based review of it became impossible.
 * `tsc`, `svelte-check`, `biome` and `vitest` all passed, because a NUL inside a
 * comment bothers none of them and nothing in the suite read its own source.
 *
 * It has been widened twice, and both times by finding something. Walking `src/`
 * only closed the instance and not the class — thirty-seven files sit outside it,
 * `tests/` among them, and that is where most scripted edits land. Then asking git
 * for *tracked* files alone left the same hole one step along: a file written
 * minutes ago is not tracked yet, which is exactly where a fresh mistake lives.
 * This file proved it. Written to catch a NUL, it contained one — in the sentence
 * describing the original defect, put there by the same kind of scripted edit —
 * and the gate could not see it until the listing included untracked files.
 *
 * The byte test is deliberately stricter than git's own. Git only looks for a NUL
 * in the first 8000 bytes of a file, so a NUL further in diffs as text today and
 * would be a trap tomorrow.
 */

import { execFileSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";

/** The package root. `fileURLToPath`, not `.pathname`: a path with a space in it
 * arrives percent-encoded from the latter and nothing can open it. */
const ROOT = fileURLToPath(new URL("..", import.meta.url));

/**
 * Extensions whose bytes are meant to be bytes.
 *
 * None are tracked today — every file in the package is text — but a favicon or a
 * fixture image is an ordinary thing to add, and a gate that fails on one would be
 * a gate somebody deletes rather than reads.
 */
const BINARY = new Set([
  ".png",
  ".jpg",
  ".jpeg",
  ".gif",
  ".webp",
  ".avif",
  ".ico",
  ".woff",
  ".woff2",
  ".ttf",
  ".otf",
  ".pdf",
  ".zip",
  ".gz",
  ".ovpack",
]);

/** Directories a walk must not descend into. Git already ignores all of these. */
const SKIP_DIRS = new Set(["node_modules", "dist", ".git", ".svelte-kit", "coverage"]);

/**
 * Every file in this package, as absolute paths.
 *
 * Git first, because it already knows what is generated and what is checked in,
 * so `node_modules` and `dist` are excluded without this file having to list them.
 * `-z` so a filename containing a newline cannot split one entry into two.
 *
 * But not *only* git. A tarball, a vendored copy, or a fresh `npm pack` has no
 * repository, and `execFileSync` throws rather than returning empty — so a gate
 * that only knew how to ask git would take the whole suite down in a checkout
 * where the thing being guarded is just as important. The walk is the fallback,
 * and it is why `SKIP_DIRS` exists at all.
 */
function sourceFiles(root: string = ROOT): string[] {
  try {
    const listing = execFileSync(
      "git",
      // `--others --exclude-standard` as well as the default `--cached`. Tracked
      // files alone was a hole exactly where it matters most: a file written
      // minutes ago is not tracked yet, and that is where a fresh mistake lives.
      // This gate found its own NUL only once the walk reached it.
      ["ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "."],
      {
        cwd: root,
        encoding: "utf8",
        maxBuffer: 32 * 1024 * 1024,
        stdio: ["ignore", "pipe", "ignore"],
      },
    );
    return (
      listing
        .split("\0")
        .filter(Boolean)
        .map((relative) => `${root}${relative}`)
        // `ls-files` reads the index, which still lists a file deleted from the
        // working tree but not yet staged as a deletion. Reading one throws ENOENT
        // and would fail the suite for a reason that is not a defect.
        .filter((path) => existsSync(path))
    );
  } catch {
    return walk(root);
  }
}

/** Every file under a directory, skipping the generated ones. */
function walk(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    if (SKIP_DIRS.has(entry.name)) return [];
    const path = `${dir}${entry.name}`;
    // `withFileTypes` reports a symlink as a symlink rather than following it, so
    // a broken one is skipped here instead of throwing on the read.
    if (entry.isDirectory()) return walk(`${path}/`);
    return entry.isFile() ? [path] : [];
  });
}

/**
 * Bytes that have no business in a text file.
 *
 * Tab, newline and carriage return are the three control characters that do.
 * Everything else below 0x20 is either invisible or, in NUL's case, enough to make
 * git give up on the file.
 */
function controlBytes(data: Buffer): number[] {
  const found = new Set<number>();
  for (const byte of data) {
    if (byte < 0x20 && byte !== 0x09 && byte !== 0x0a && byte !== 0x0d) found.add(byte);
  }
  return [...found];
}

function extensionOf(path: string): string {
  const name = path.split("/").pop() ?? "";
  const dot = name.lastIndexOf(".");
  return dot <= 0 ? "" : name.slice(dot).toLowerCase();
}

describe("every source file is text git can read", () => {
  const tracked = sourceFiles();
  const textFiles = tracked.filter((path) => !BINARY.has(extensionOf(path)));

  it("finds a file list covering every corner, however it was obtained", () => {
    /*
     * Without this, a listing that came back short would make every assertion
     * below vacuously true — the shape of passing test that hides the thing it was
     * written to catch. The first version checked a count and one filename, and
     * two whole subtrees could have been missing and still satisfied it. So: one
     * named file from each place a scripted edit lands.
     */
    expect(tracked.length).toBeGreaterThan(40);
    for (const expected of [
      "src/server/app.ts",
      "src/client/lib/Reader.svelte",
      "src/client/views/Files.svelte",
      "src/shared/schemas.ts",
      "tests/edit.test.ts",
      "README.md",
      "vitest.config.ts",
      "package.json",
    ]) {
      expect(tracked, expected).toContain(`${ROOT}${expected}`);
    }
  });

  it("carries no control byte, which is what makes git treat a file as binary", () => {
    const offenders = textFiles
      .map((path) => ({ path, bytes: controlBytes(readFileSync(path)) }))
      .filter((entry) => entry.bytes.length > 0)
      .map(
        (entry) =>
          `${entry.path.slice(ROOT.length)}: ${entry.bytes
            .map((byte) => `0x${byte.toString(16).padStart(2, "0")}`)
            .join(", ")}`,
      );

    expect(offenders).toEqual([]);
  });

  it("decodes as UTF-8 without a replacement character", () => {
    // A lone surrogate or a mis-encoded paste survives every other gate too, and
    // shows up in the UI as a black diamond rather than as an error.
    const broken = textFiles
      .filter((path) => readFileSync(path).toString("utf8").includes("\uFFFD"))
      .map((path) => path.slice(ROOT.length));
    expect(broken).toEqual([]);
  });
});

/**
 * The gate's own defences, driven against throwaway directories.
 *
 * Each of these was added in response to a real failure and each survived being
 * mutated away — the gate kept passing without them, because the conditions they
 * handle do not occur in this checkout. A defence nothing exercises is a defence
 * that quietly stops working, which is the whole lesson of the NUL this file was
 * written for.
 */
describe("how the file list is gathered", () => {
  /** A throwaway directory, removed when the test ends. */
  function scratch(): string {
    const dir = mkdtempSync(join(tmpdir(), "ovdash-sources-"));
    temporary.push(dir);
    return dir;
  }

  const temporary: string[] = [];
  afterEach(() => {
    for (const dir of temporary.splice(0)) rmSync(dir, { recursive: true, force: true });
  });

  /** A git repo with one commit, so `ls-files` has an index to read. */
  function repoWith(files: Record<string, string>): string {
    const dir = scratch();
    const git = (...args: string[]) =>
      execFileSync("git", args, { cwd: dir, stdio: ["ignore", "ignore", "ignore"] });
    git("init", "-q");
    git("config", "user.email", "gate@example.invalid");
    git("config", "user.name", "gate");
    for (const [name, body] of Object.entries(files)) {
      mkdirSync(join(dir, name, ".."), { recursive: true });
      writeFileSync(join(dir, name), body);
    }
    git("add", "-A");
    git("commit", "-qm", "one");
    return dir;
  }

  const namesIn = (root: string) =>
    sourceFiles(`${root}/`)
      .map((path) => path.slice(root.length + 1))
      .sort();

  it("includes a file that is not tracked yet", () => {
    /*
     * The hole that let this gate miss a NUL in its own source. A file written
     * minutes ago is not tracked, and that is exactly where a fresh mistake lives —
     * every file added in a session is untracked until somebody commits it.
     */
    const dir = repoWith({ "tracked.ts": "// tracked\n" });
    writeFileSync(join(dir, "brand-new.ts"), "// written just now\n");

    expect(namesIn(dir)).toContain("brand-new.ts");
    expect(namesIn(dir)).toContain("tracked.ts");
  });

  it("leaves out what the repo ignores, so generated files are not checked", () => {
    // `--others` without `--exclude-standard` would drag in `node_modules`, which
    // is megabytes of other people's bytes and not ours to judge.
    const dir = repoWith({ ".gitignore": "ignored/\n" });
    mkdirSync(join(dir, "ignored"), { recursive: true });
    writeFileSync(join(dir, "ignored", "generated.ts"), "// not ours\n");

    expect(namesIn(dir)).not.toContain("ignored/generated.ts");
  });

  it("survives a file deleted from the tree but still in the index", () => {
    // `ls-files` reads the index, so it still names the file. Reading it throws
    // ENOENT, which would fail the suite for something that is not a defect.
    const dir = repoWith({ "kept.ts": "// kept\n", "removed.ts": "// gone\n" });
    rmSync(join(dir, "removed.ts"));

    const names = namesIn(dir);
    expect(names).toContain("kept.ts");
    expect(names).not.toContain("removed.ts");
  });

  it("falls back to a walk where there is no repository at all", () => {
    /*
     * A tarball, a vendored copy, a fresh `npm pack`. `execFileSync` throws rather
     * than answering empty, so without the fallback the whole suite dies in a
     * checkout where the bytes still matter just as much.
     */
    const dir = scratch();
    mkdirSync(join(dir, "src"), { recursive: true });
    writeFileSync(join(dir, "src", "a.ts"), "// a\n");
    writeFileSync(join(dir, "b.md"), "# b\n");

    expect(namesIn(dir)).toEqual(["b.md", "src/a.ts"]);
  });

  it("does not descend into the generated directories on that fallback", () => {
    // The walk has to be told what git already knows.
    const dir = scratch();
    for (const skipped of ["node_modules", "dist", ".git"]) {
      mkdirSync(join(dir, skipped), { recursive: true });
      writeFileSync(join(dir, skipped, "noise.ts"), "// not ours\n");
    }
    writeFileSync(join(dir, "mine.ts"), "// mine\n");

    expect(namesIn(dir)).toEqual(["mine.ts"]);
  });

  it("steps over a broken symlink rather than throwing on it", () => {
    // `withFileTypes` reports the link as a link, so nothing tries to read it.
    const dir = scratch();
    writeFileSync(join(dir, "real.ts"), "// real\n");
    symlinkSync(join(dir, "nothing-here.ts"), join(dir, "dangling.ts"));

    expect(() => namesIn(dir)).not.toThrow();
    expect(namesIn(dir)).toEqual(["real.ts"]);
  });
});
