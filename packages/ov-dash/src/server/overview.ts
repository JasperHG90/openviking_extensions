/**
 * Reading one file's description out of the folder's overview.
 *
 * OpenViking writes no abstract per file. Asking it for a file's abstract
 * answers with the *folder's* abstract — measured against the live server, the
 * same string byte for byte for every file in the folder, so a reader shown
 * that string learns nothing about the file they clicked.
 *
 * What OpenViking does write per file is part of the folder's `.overview.md`:
 * a `### ` section named after the file under `## Detailed Description`, and a
 * one-line entry under `## Quick Navigation`. This pulls those back out. The
 * section is the fuller of the two, so it wins; the nav line is the fallback,
 * and it is the one that survives when OpenViking truncates a long overview
 * before it reaches the detail for this file.
 */

/** A markdown heading line, at any level. */
const HEADING = /^#{1,6}\s+(.*)$/;

/**
 * The heading levels a per-file section may use.
 *
 * Three or deeper, never `#` or `##`. Every overview opens with `# <folder>`
 * and a folder holding a file of its own name — which is how OpenViking stores
 * an imported document — would otherwise match that title first and answer
 * with the folder's opening paragraph, dressed as the file's own description.
 */
const FILE_HEADING = /^#{3,6}\s+(.*)$/;

/** The heading OpenViking files its per-file sections under. */
const DETAIL_HEADING = /^#{1,6}\s+Detailed Description\s*$/i;

/** A heading that is nothing but one link, e.g. `### [notes.md](viking://…)`. */
const LINKED_HEADING = /^\[([^\]]*)\]\(([^)]*)\)$/;

/** A bullet, `-` or `*`, with its marker still attached. */
const BULLET = /^\s*[-*]\s+(.+)$/;

/** Inline `[text](target)`, captured to find which file a bullet points at. */
const INLINE_LINK = /\[([^\]]*)\]\(([^)]*)\)/g;

/**
 * Most characters of one line this will pick apart.
 *
 * `INLINE_LINK` costs time quadratic in the line length against text that
 * opens links and never closes them, and the event loop is the whole process.
 * A navigation bullet is one sentence; anything at this length is not one, and
 * skipping it loses nothing. Overview text is written by a model from
 * documents people upload, so its shape is not ours to assume.
 */
const MAX_LINE = 4000;

/** Characters that belong to a file name, for testing a bare mention's edges. */
const NAME_CHAR = /[A-Za-z0-9._-]/;

/**
 * A field the template asked for and did not get.
 *
 * OpenViking renders its memory templates with Jinja's `DebugUndefined`, which
 * prints the miss into the output instead of raising — so a `cases` file with
 * no `task_signature` gets an overview bullet reading
 * `{{ no such element: dict object['task_signature'] }}`, and that string is
 * then the file's description. It is a rendering failure in a description's
 * clothes: the pane showed it as what OpenViking makes of the file, when what
 * it means is that the file has no such field.
 *
 * Matched by the three shapes `DebugUndefined` actually emits, not by hunting
 * every `{{ … }}`. An expression is left alone — `{{ 2 + 2 }}`, `{{ x | upper }}`
 * — because those are prose about templating, not a failure.
 *
 * The bare-name branch is the one that costs something, and it is worth being
 * plain about: `{{ title }}` written on purpose in a description of a Jinja or
 * Vue file is the same bytes as `{{ title }}` printed by a template that did
 * not get `title`. Nothing can separate them, so a description that quotes the
 * plainest template snippet loses it. Kept anyway — a missing field is the
 * common case by far, and a page of error text is worse than a lost example.
 *
 * One space either side is part of the match so the seam can be closed in the
 * same pass. Collapsing runs of spaces afterwards was the obvious way and the
 * wrong one: it reached every line in the text, so a description holding an
 * indented code block came out with its indentation flattened, on lines
 * nowhere near a placeholder.
 *
 * Every quantifier here is bounded by something that cannot match what follows
 * it, which is what keeps this linear. The first draft ended `[^{}]*\s*\}\}`,
 * and `[^{}]*` matches spaces too — so the two fought over every space in a
 * run and the engine tried each split. Measured: 32k spaces after `{{ no such
 * element:` took 561ms, quadratic, on the thread that is the whole server.
 * `MAX_LINE` above exists for the same hazard in `INLINE_LINK`.
 */
const UNRENDERED =
  /([^\S\r\n]?)\{\{[^\S\r\n]*(?:(?:no such element:|undefined value printed:)[^{}]*|[A-Za-z_][\w.]*(?:\[[^\]\r\n]*\])?[^\S\r\n]*)\}\}([^\S\r\n]?)/g;

/**
 * Drop the failed renders out of generated text.
 *
 * Whatever survives is still shown: a description that is half real words and
 * half a missing field is worth the half that arrived, and one that is nothing
 * but the failure comes back empty, which callers read as "no description".
 *
 * A placeholder with words on both sides leaves one space behind, so the
 * sentence closes up; one at an edge leaves nothing, so "Written in {{ lang }}."
 * does not come back with a space before its full stop.
 */
export function stripUnrendered(text: string): string {
  if (!text.includes("{{")) return text;
  return text
    .replace(UNRENDERED, (_whole, before: string, after: string) =>
      before && after ? " " : "",
    )
    .trim();
}

/**
 * The description OpenViking wrote for one file, or `""` if it wrote none.
 *
 * @param overview - The folder's overview document (L1).
 * @param fileName - The file's own name, e.g. `page3_img1.png`.
 */
export function describeFile(overview: string, fileName: string): string {
  if (!overview.trim() || !fileName) return "";
  const lines = overview.split(/\r?\n/);
  return stripUnrendered(
    detailSection(lines, fileName, true) ||
      detailSection(lines, fileName, false) ||
      navLine(lines, fileName),
  );
}

/**
 * The body of the `###` section whose heading names this file.
 *
 * Collection stops at the next heading of any level, so a section cannot run
 * on into the one after it — but only a `###` or deeper heading may start one.
 *
 * Run twice. The first pass reads only what sits under `## Detailed
 * Description`, where OpenViking puts these; a `###` anywhere else is prose
 * that happens to be shaped like a section, and letting it win would answer
 * with the wrong text. The second pass drops that requirement, because a
 * heading OpenViking might one day rename is a poor reason to lose a
 * description that is plainly there.
 */
function detailSection(lines: string[], fileName: string, scoped: boolean): string {
  const body: string[] = [];
  let collecting = false;
  let inDetail = !scoped;

  for (const line of lines) {
    const heading = HEADING.exec(line);
    if (heading) {
      if (collecting) break;
      if (scoped && DETAIL_HEADING.test(line)) {
        inDetail = true;
        continue;
      }
      const forFile = FILE_HEADING.exec(line);
      collecting = inDetail && forFile !== null && namesFile(forFile[1] ?? "", fileName);
      continue;
    }
    if (collecting) body.push(line);
  }

  return body.join("\n").trim();
}

/**
 * The description off the Quick Navigation bullet that points at this file.
 *
 * The bullets read `- **question** → [name](uri) — description`, so the part
 * worth showing is what follows the first dash *after* the file is named. A
 * bullet that mentions the file without describing it is skipped rather than
 * answered with the file's own name.
 *
 * The file has to be named whole. Testing for the name as a substring gave
 * `plan.md` the bullet written for `old-plan.md` — a real description, of the
 * wrong file, which is worse than none.
 */
function navLine(lines: string[], fileName: string): string {
  for (const line of lines) {
    if (line.length > MAX_LINE) continue;
    const bullet = BULLET.exec(line);
    if (!bullet) continue;

    const raw = bullet[1] ?? "";
    const at = pointsAtFile(raw, fileName);
    if (at === null) continue;

    // Read on from where the file was named, so a dash inside the question
    // ahead of it is not mistaken for the one introducing the description.
    const after = flatten(raw.slice(at));
    const dash = after.search(/[—–]/);
    if (dash < 0) continue;

    const described = after.slice(dash + 1).trim();
    if (described) return described;
  }
  return "";
}

/**
 * Where in a bullet this file is named, or null if it is not.
 *
 * A link naming the file counts by either face — its text or the last segment
 * of its target.
 *
 * A bullet that links some *other* file is that file's, full stop, and a bare
 * mention of ours in it proves nothing. `[old plan.md](…/old%20plan.md)` and
 * `[thumb.png](…/plan.md/thumb.png)` both contain the characters `plan.md`
 * with a separator either side, and prose like "supersedes plan.md" reads the
 * same to any test made of characters. Every one of those would hand `plan.md`
 * a confident description of a different file, which is worse than none. So
 * the bare name is only read where no link claimed the bullet first.
 */
function pointsAtFile(bullet: string, fileName: string): number | null {
  let linked = false;
  for (const link of bullet.matchAll(INLINE_LINK)) {
    linked = true;
    if (linkNames(link[1] ?? "", link[2] ?? "", fileName)) {
      return (link.index ?? 0) + link[0].length;
    }
  }
  if (linked) return null;

  // Every occurrence, not just the first: in "old-plan.md and plan.md — …"
  // the first hit for `plan.md` sits inside its sibling's name, and stopping
  // there would miss the mention that is genuinely this file's.
  for (
    let at = bullet.indexOf(fileName);
    at >= 0;
    at = bullet.indexOf(fileName, at + 1)
  ) {
    const before = bullet[at - 1];
    const after = bullet[at + fileName.length];
    if (before && NAME_CHAR.test(before)) continue;
    if (after && NAME_CHAR.test(after)) continue;
    return at + fileName.length;
  }
  return null;
}

/** Whether a link's text or target names this file. */
function linkNames(text: string, target: string, fileName: string): boolean {
  const label = decodeSafe(text.trim());
  const last = decodeSafe(target.trim().split("/").pop() ?? "");
  return label === fileName || last === fileName;
}

/** Whether a heading's text is this file's name, linked or bare. */
function namesFile(heading: string, fileName: string): boolean {
  const text = heading.trim();
  const linked = LINKED_HEADING.exec(text);
  if (!linked) return decodeSafe(text) === fileName;
  return linkNames(linked[1] ?? "", linked[2] ?? "", fileName);
}

/** Reduce a bullet to plain words: links become their text, emphasis goes. */
function flatten(line: string): string {
  return line
    .replace(INLINE_LINK, (_whole, text: string) => text)
    .replace(/\*\*|__|`/g, "")
    .trim();
}

/** Decode a percent-encoded name, leaving anything undecodable alone. */
function decodeSafe(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}
